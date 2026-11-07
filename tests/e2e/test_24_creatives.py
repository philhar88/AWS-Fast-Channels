"""Advertisers' own creatives (M5, docs/creatives.md): a creative registered for a brand becomes READY, a buy that a
buyer agent books for that brand over AdCP is served with it, and it airs in a break with no first-sight skip,
counted as delivered.

Runs only on a stage with the Creatives and Selling stacks (``make deploy-demand`` with ``modules.creatives`` and
``modules.selling``, for example ``STAGE=fastcisell``); skipped otherwise. The module's fixtures do the billable parts
once:

1. ffmpeg renders a new 30.03 s 1080p spot (a test pattern and a tone of this run's own), which
   ``scripts/creatives.py`` uploads to the module's quarantine bucket and registers for this run's brand; the readiness
   state machine moderates it (Amazon Rekognition), conditions it with the stage's ingest pipeline, checks it against
   the ladder and publishes it READY;
2. the AWS reference buyer books a campaign for that brand over AdCP (``harness_selling.book``); its line serves the
   registered creative;
3. the channel airs the test pattern with two 60.06 s breaks to two smart-TV viewers on its stable, prefetched sibling;
   then the channel stops.
"""

import re
import shutil
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from _stack import Module, Stage
from boto3.dynamodb.conditions import Key
from creatives import Creatives
from harness_selling import (
    Aired,
    Booking,
    Buyer,
    Seller,
    air,
    book,
    buyer_of,
    run_brand,
    seller_of,
    snapshot,
    snapshot_line,
)

from fast_channels.core.creatives.names import CRID_PATTERN, READY_KEY
from fast_channels.core.creatives.registry import ReadyCreatives, State
from fast_channels.core.ledger import ledger_table
from fast_channels.core.pds import names as pds_names
from fast_channels.core.pds.delivery import Credit
from fast_channels.core.selling.products import product_id

if TYPE_CHECKING:
    from harness import Channel, Tools

pytestmark = pytest.mark.e2e

RUN_BRAND: Final = run_brand()
"""This run's brand domain: its creative is the only one registered for it."""
ADVERTISER: Final = "Acme Outdoor"
FRAMES: Final = 900
"""30.03 s at 30000/1001 fps: one ad slot of the ladder's grid."""
READY_TIMEOUT_S: Final = 1200
"""Moderation (about a minute), the ingest (two to four minutes) and the check."""
STATES: Final = ["RECEIVED", "APPROVED", "CONDITIONING", "CONDITIONED", "READY"]
FIRST_SIGHT: Final = frozenset({"NEW_CREATIVE", "IMPORT_IN_PROGRESS", "TRANSCODE_IN_PROGRESS"})
"""``SkippedReason`` values of a creative MediaTailor had not prepared yet."""


@pytest.fixture(scope="module")
def selling(stage: Stage) -> Stage:
    for module in (Module.CREATIVES, Module.SELLING):
        if stage.status(module) is None:
            pytest.skip(f"stage {stage.prefix} has no {module} stack (deploy-demand with modules.{module.lower()})")
    return stage


@pytest.fixture(scope="module")
def registry(selling: Stage) -> Creatives:
    return Creatives(selling)


@pytest.fixture(scope="module")
def spot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A new 30.03 s 1080p H.264/AAC spot: no earlier run registered it, so MediaTailor has never seen it."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    tone, hue = 300 + uuid.uuid4().int % 600, uuid.uuid4().int % 360
    path = tmp_path_factory.mktemp("creatives") / "spot.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=30000/1001",
            "-f",
            "lavfi",
            "-t",
            "30.03",
            "-i",
            f"sine=frequency={tone}:sample_rate=48000",
            "-vf",
            f"hue=h={hue}",
            "-frames:v",
            str(FRAMES),
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            "3M",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="module")
def registered(registry: Creatives, spot: Path) -> dict[str, Any]:
    """The spot, registered for this run's brand and followed to the end of its readiness execution."""
    crid, execution = registry.register_file(spot, advertiser=ADVERTISER, adomain=RUN_BRAND, by="e2e")
    print(f"registered {crid}: {execution}")
    ended = registry.wait(execution, timeout=READY_TIMEOUT_S)
    record = registry.record(crid)
    assert record is not None, crid
    print(f"readiness {ended['status']}: {[h['state'] for h in record.get('history', [])]}")
    return record | {"execution_status": ended["status"]}


def test_a_registered_creative_becomes_ready(registered: dict[str, Any], selling: Stage) -> None:
    """Moderated, conditioned by the ingest pipeline, conformant to the ladder and published to the own book."""
    assert registered["execution_status"] == "SUCCEEDED", registered.get("history")
    assert [h["state"] for h in registered["history"]] == STATES
    assert registered["state"] == State.READY
    conformance = registered["conformance"]
    assert conformance["problems"] == [] and int(conformance["slots"]) == 1, conformance
    assert int(registered["duration_ms"]) == 30030
    assert registered["moderation"]["labels"] == {}
    s3 = selling.session.client("s3", region_name=selling.region)
    ssm = selling.session.client("ssm", region_name=selling.region)
    name = pds_names.parameter_name(selling.prefix, pds_names.Parameter.CONFIG_BUCKET)
    bucket = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    ready = ReadyCreatives.model_validate_json(s3.get_object(Bucket=bucket, Key=READY_KEY)["Body"].read())
    assert registered["crid"] in {c.crid for c in ready.creatives}


@pytest.fixture(scope="module")
def seller(selling: Stage) -> Seller:
    return seller_of(selling)


@pytest.fixture(scope="module")
def buyer(selling: Stage) -> Buyer:
    return buyer_of(selling)


@pytest.fixture(scope="module")
def booking(registered: dict[str, Any], seller: Seller, buyer: Buyer, channel: Channel) -> Booking:
    """The buyer agent's booking for the brand, once its creative is READY."""
    assert registered["state"] == State.READY, registered.get("history")
    return book(seller, buyer, brand=RUN_BRAND, product=product_id(channel.name))


@pytest.fixture(scope="module")
def line(selling: Stage, booking: Booking, registered: dict[str, Any]) -> dict[str, Any]:
    """The booked line, once the snapshot has it serving the registered creative."""
    return snapshot_line(selling, booking.line, crid=registered["crid"])


def test_the_brands_buy_is_served_with_its_registered_creative(
    line: dict[str, Any], registered: dict[str, Any], selling: Stage
) -> None:
    """Not the demo creative: the brand's own, which the own book bids marked ``registered``."""
    assert line["adomain"] == RUN_BRAND
    assert line["creatives"] == [registered["crid"]]
    [creative] = [c for c in snapshot(selling).creatives if c.crid == registered["crid"]]
    assert creative.registered and creative.creative_id == registered["creative_id"]
    assert creative.hls_uri == registered["hls_uri"]


@pytest.fixture(scope="module")
def aired(
    selling: Stage,
    line: dict[str, Any],
    booking: Booking,
    channel: Channel,
    tools: Tools,
    registered: dict[str, Any],
    request: pytest.FixtureRequest,
) -> Aired:
    return air(
        selling,
        booking,
        channel,
        tools,
        lambda: request.getfixturevalue("ingested"),
        crid=str(registered["crid"]),
        creative_id=str(registered["creative_id"]),
    )


def test_it_airs_in_a_break_without_a_first_sight_skip(aired: Aired) -> None:
    """MediaTailor stitched the new creative into the viewers' breaks, and never skipped it as not prepared: the
    prefetched sibling retrieved the break before it aired."""
    ads = [ad for avail in aired.stitched for ad in (avail.get("avail") or {}).get("creativeAds") or []]
    assert any(ad.get("creativeUniqueId") == aired.creative_id for ad in ads), (
        aired.creative_id,
        sorted({str(ad.get("creativeUniqueId")) for ad in ads}),
    )
    skipped = [
        skip
        for avail in aired.stitched
        for skip in (avail.get("avail") or {}).get("skippedAds") or []
        if skip.get("creativeUniqueId") == aired.creative_id
    ]
    assert not [skip for skip in skipped if skip.get("skippedReason") in FIRST_SIGHT], skipped


def test_a_standalone_backed_it_in_its_slot(aired: Aired) -> None:
    """The pod rules placed the registered creative only with a standalone of the same slot behind it (ad buffet)."""
    placed = [
        (record, bid)
        for record in aired.selects
        for bid in record.get("bids") or []
        if bid.get("crid") == aired.crid and bid.get("outcome") == "won"
    ]
    assert placed, [record.get("pod") for record in aired.selects]
    for record, bid in placed:
        backups = [b for b in record["bids"] if b["ref"] in record["standalone"] and b["impid"] == bid["impid"]]
        assert backups, (bid["ref"], record["standalone"])
        assert all(b["crid"] != aired.crid for b in backups)


def test_it_is_counted_as_delivered(aired: Aired, booking: Booking, selling: Stage) -> None:
    """Our tracker's impressions of the registered creative are delivery records credited to the line's
    reservations, and money events in its ledger chain."""
    assert re.fullmatch(CRID_PATTERN, aired.crid)
    delivered = [d for d in aired.deliveries if d.get("line") == booking.line and d.get("credit") == Credit.DELIVERED]
    assert delivered, [d for d in aired.deliveries if d.get("crid") == aired.crid]
    assert all(d["crid"] == aired.crid and Decimal(str(d["amount"])) == Decimal("0.04") for d in delivered), delivered
    table = selling.session.resource("dynamodb", region_name=selling.region).Table(ledger_table(selling.prefix))
    entries = table.query(KeyConditionExpression=Key("pk").eq(f"pds/line/{booking.line}"))["Items"]
    actions = {str(entry.get("action")) for entry in entries}
    assert {"money.budget", "money.reserve", "money.deliver"} <= actions, actions
