"""Agentic selling (M7, docs/selling.md): a buyer agent books the stage's inventory over AdCP, the booking serves
through the PDS own book, and its delivery goes back to the buyer over AdCP.

Runs only on a stage with the Selling stack (``make deploy-demand`` with ``modules.selling``, for example
``STAGE=fastcisell``); skipped otherwise. The module's fixtures do the billable parts once:

1. the scripted buyer journey: the AWS reference buyer on AgentCore Runtime (its model, its own tools) discovers the
   seller, gets the products, binds the account, syncs a plan to the governance agent, checks the buy and books it
   over A2A 1.0, with a flight that starts in a few minutes;
2. the seller's bridge sends the buy to the sync function, which publishes its line; the snapshot publisher puts it in
   the own book;
3. the channel airs the test pattern with two 60.06 s breaks to two smart-TV viewers on its stable sibling, which
   request every stitched ad segment so that MediaTailor fires the beacons; then the channel stops;
4. the buyer asks the seller for the buy's delivery.
"""

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import pytest
from _stack import Module, Plane, Stage
from boto3.dynamodb.conditions import Key
from harness import BREAK_POLICY, LEAD, Channel, Player, Tools, wait_for
from harness_demand import DemandEdge
from harness_pods import (
    SMART_TV_USER_AGENT,
    Creatives,
    Logs,
    Viewer,
    ipv4_client,
    json_object,
    prebid_tasks,
    record_pattern,
    warmups,
    watch,
)
from harness_selling import BRAND, Buyer, Seller, delivery_question, journey, reply

from fast_channels.core.demand import warmup_project
from fast_channels.core.demo_demand import demo_index_key
from fast_channels.core.ledger import ledger_table
from fast_channels.core.monetization import names as release_names
from fast_channels.core.monetization.release import PlaybackPlan
from fast_channels.core.pds import names as pds_names
from fast_channels.core.pds.delivery import Credit
from fast_channels.core.pds.records import DELIVERY_RECORD
from fast_channels.core.pds.snapshot import SNAPSHOT_KEY, Snapshot
from fast_channels.core.selling.booking import line_id
from fast_channels.core.selling.products import product_id
from fast_channels.core.specs import vast_key

pytestmark = pytest.mark.e2e

BUDGET: Final = 50.0
"""The booked budget, US dollars (simulated): 1,250 impressions at the fastcisell stage's $40 CPM."""
FLIGHT_LEAD: Final = timedelta(minutes=4)
"""The flight starts this long after the journey: create_media_buy refuses a start in the past."""
TURNS: Final = 3
"""Turns the buyer gets to book (it may stop to report a step)."""
LINE_TIMEOUT_S: Final = 300
"""The bridge syncs every minute, then the publisher runs (seconds) and the sidecars read the snapshot (30 s)."""
READY_TIMEOUT_S: Final = 1500
LOGS_TIMEOUT_S: Final = 420
DELIVERY_TIMEOUT_S: Final = 240
VIEWERS: Final = 2
BREAKS: Final = 2
TAIL: Final = timedelta(seconds=90)
MEDIA_BUY: Final = re.compile(r"MEDIA_BUY_ID=(buy_[0-9a-f]+)")
RUN_BRAND: Final = f"e2e-{uuid.uuid4().hex[:8]}.{BRAND}"
"""This run's brand domain (``harness_selling.BRAND``)."""
ACCOUNT: Final = {"brand": {"domain": RUN_BRAND}, "operator": RUN_BRAND}
"""The account the buyer's journey syncs (its brand, which is also its operator)."""


@dataclass
class Booking:
    """What the buyer booked, as the seller and the own book know it."""

    media_buy_id: str
    session_id: str
    calls: list[dict[str, Any]]
    line: str
    starts: datetime


@pytest.fixture(scope="module")
def selling(stage: Stage) -> Stage:
    if stage.status(Module.SELLING) is None:
        pytest.skip(f"stage {stage.prefix} has no Selling stack (deploy-demand with modules.selling)")
    return stage


@pytest.fixture(scope="module")
def seller(selling: Stage) -> Seller:
    secrets = selling.session.client("secretsmanager", region_name=selling.region)
    token = secrets.get_secret_value(SecretId=selling.output(Module.SELLING, "BuyerTokenSecret"))["SecretString"]
    return Seller(selling.output(Module.SELLING, "SellerUrl"), token)


@pytest.fixture(scope="module")
def buyer(selling: Stage) -> Buyer:
    return Buyer(
        selling.session,
        selling.output(Module.SELLING, "BuyerRuntimeArn"),
        selling.output(Module.SELLING, "BuyerSessionsTable"),
    )


@pytest.fixture(scope="module")
def booking(selling: Stage, seller: Seller, buyer: Buyer, channel: Channel) -> Booking:
    """The scripted journey, run by the buyer agent; the buy it made, read back from the seller."""
    starts = (datetime.now(UTC) + FLIGHT_LEAD).replace(microsecond=0)
    prompt = journey(
        brand=RUN_BRAND,
        product_id=product_id(channel.name),
        plan_id=f"plan-e2e-{uuid.uuid4().hex[:8]}",
        budget=BUDGET,
        starts=starts,
        ends=starts + timedelta(days=1),
    )
    session_id = f"fast-e2e-selling-{uuid.uuid4().hex}"
    text, media_buy_id = "", None
    for turn in range(TURNS):
        answer = buyer.turn(prompt if turn == 0 else "Continue the steps and reply with MEDIA_BUY_ID.", session_id)
        text = reply(answer)
        print(f"buyer turn {turn + 1}: {text[:600]}")
        if (said := MEDIA_BUY.search(text)) and seller_buy(seller, said[1]) is not None:
            media_buy_id = said[1]
            break
    assert media_buy_id, f"the seller has no buy the buyer named after {TURNS} turns: {text[:1000]}"
    return Booking(media_buy_id, session_id, buyer.calls(session_id), line_id(media_buy_id, 1), starts)


def seller_buy(seller: Seller, media_buy_id: str) -> dict[str, Any] | None:
    """The seller's media buy (its account is the brand's), or None."""
    found = seller.call("get_media_buys", account=ACCOUNT, media_buy_ids=[media_buy_id]).get("media_buys") or []
    return next((buy for buy in found if buy["media_buy_id"] == media_buy_id), None)


def test_the_buyer_agent_books_through_governance(booking: Booking, seller: Seller) -> None:
    """The reference buyer's own tool calls: capabilities, products, account, plan, an approved governance check and
    the booking, which the seller confirmed (``pending_creatives``: the buyer sends no creative)."""
    results = {
        str(step["content"].get("toolName")): str(step["content"].get("output"))
        for step in booking.calls
        if step.get("step_type") == "tool_result" and isinstance(step.get("content"), dict)
    }
    print(json.dumps(sorted(results)))
    for tool in ("adcp_get_capabilities", "adcp_get_products", "adcp_sync_accounts", "adcp_sync_plans"):
        assert tool in results, f"the buyer recorded no {tool}: {sorted(results)}"
    check = json.loads(results["adcp_check_governance"])
    assert check["verdict"] == "approved", check
    assert json.loads(results["adcp_create_media_buy"])["media_buy_id"] == booking.media_buy_id
    buy = seller_buy(seller, booking.media_buy_id)
    assert buy is not None and buy["status"] == "pending_creatives", buy


@pytest.fixture(scope="module")
def line(selling: Stage, booking: Booking) -> dict[str, Any]:
    """The booked line, once the snapshot has it."""
    s3 = selling.session.client("s3", region_name=selling.region)
    ssm = selling.session.client("ssm", region_name=selling.region)
    name = pds_names.parameter_name(selling.prefix, pds_names.Parameter.CONFIG_BUCKET)
    bucket = ssm.get_parameter(Name=name)["Parameter"]["Value"]

    def published() -> dict[str, Any] | None:
        snapshot = Snapshot.model_validate_json(s3.get_object(Bucket=bucket, Key=SNAPSHOT_KEY)["Body"].read())
        found = next((line for line in snapshot.lines if line.id == booking.line), None)
        return found.model_dump(mode="json") if found else None

    return wait_for(published, timeout=LINE_TIMEOUT_S, interval=10, what=f"line {booking.line} in the snapshot")


def test_the_booked_buy_is_a_line_of_the_own_book(line: dict[str, Any], booking: Booking, channel: Channel) -> None:
    """Budget, flight, CPM, creative and brand of the buy, sold on its product's channel."""
    assert line["adomain"] == RUN_BRAND
    assert Decimal(line["budget"]) == Decimal(str(BUDGET)) and Decimal(line["cpm"]) == 40
    assert line["channels"] == [channel.name]
    assert datetime.fromisoformat(line["starts"]) == booking.starts
    assert line["creatives"][0] in {"direct1", "direct2"}


@dataclass
class Aired:
    """What the viewers' breaks did with the booked line."""

    crid: str
    creative_id: str
    stitched: list[dict[str, Any]]
    """The viewers' ``FILLED_AVAIL`` events."""
    deliveries: list[dict[str, Any]]
    """The ``pds.delivery`` records of the viewers' sessions."""


@pytest.fixture(scope="module")
def aired(
    selling: Stage,
    line: dict[str, Any],
    booking: Booking,
    channel: Channel,
    tools: Tools,
    request: pytest.FixtureRequest,
) -> Aired:
    stage, edge = selling, DemandEdge(selling.prefix, selling.session)
    crid = str(line["creatives"][0])
    creatives = Creatives.read(
        stage.session.client("s3", region_name=stage.region),
        stage.output(Plane.DATA, "MediaBucketName"),
        index_key=demo_index_key(channel.ladder_version),
        vast_key=vast_key(channel.ladder_version),
    )
    creative_id = next(cid for cid, known in creatives.by_id.items() if known == crid)
    with ipv4_client(follow_redirects=True, timeout=20.0, headers={"User-Agent": SMART_TV_USER_AGENT}) as http:
        try:
            tools("channels___start_channel", channel=channel.name)
            asset = request.getfixturevalue("ingested")  # while Prebid Server scales out
            wait_until_ready(stage, edge)
            wait_for(
                lambda: True if datetime.now(UTC) >= booking.starts else None,
                timeout=FLIGHT_LEAD.total_seconds() + 60,
                interval=10,
                what="the flight's start",
            )
            ends = schedule(tools, channel.name, asset.asset_id)
            plan = playback_plan(stage, channel.name)
            joined = datetime.now(UTC)
            viewers = [Viewer.join(Player(client=http), plan.playback["hls"]) for _ in range(VIEWERS)]
            watch(viewers, ends=ends + TAIL)
        finally:
            tools("channels___stop_channel", channel=channel.name)
    sids = {viewer.sid for viewer in viewers}
    logs = Logs(
        stage.session.client("logs", region_name=stage.region), start_ms=int(joined.timestamp() * 1000) - 60_000
    )
    ads_group = stage.output(Module.RELEASE, "SiblingLogGroup")
    delivery_group = function_log_group(stage, stage.output(Module.PDS, "DeliveryFunction"))
    deadline = time.monotonic() + LOGS_TIMEOUT_S
    while True:
        stitched = [
            found
            for event in logs.events(ads_group, '{ $.eventType = "FILLED_AVAIL" }')
            if (found := json_object(str(event["message"]))) and found.get("sessionId") in sids
        ]
        deliveries = [
            found
            for event in logs.events(delivery_group, record_pattern(DELIVERY_RECORD, sids))
            if (found := json_object(str(event["message"])))
        ]
        if any(d.get("line") == booking.line for d in deliveries) or time.monotonic() > deadline:
            break
        time.sleep(20)
    return Aired(crid, creative_id, stitched, deliveries)


def wait_until_ready(stage: Stage, edge: DemandEdge) -> None:
    """Until Prebid Server runs and every running task's warm-up succeeded (the PDS is their sidecar)."""
    ecs, codebuild = stage.session.client("ecs"), stage.session.client("codebuild")

    def ready() -> bool | None:
        tasks = prebid_tasks(ecs, cluster=edge.cluster, service=edge.service)
        warmed = warmups(codebuild, warmup_project(stage.prefix), tasks)
        return True if tasks and len(warmed) == len(tasks) and set(warmed.values()) == {"SUCCEEDED"} else None

    wait_for(ready, timeout=READY_TIMEOUT_S, interval=20, what="a warm Prebid Server")


def schedule(tools: Tools, channel: str, asset_id: str) -> datetime:
    """The pattern twice, each with a 60.06 s break, starting in a few minutes; when the last program ends."""
    start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
    plan = tools(
        "schedule___plan",
        channel=channel,
        picks=[asset_id] * BREAKS,
        start=start.isoformat(),
        break_policy=BREAK_POLICY,
    )
    assert plan["publishable"], f"plan {plan['plan_id']} has errors: {plan['violations']}"
    programs = tools("schedule___publish", plan_id=plan["plan_id"])["programs"]
    return max(datetime.fromisoformat(str(p["end"])) for p in programs)


def playback_plan(stage: Stage, channel: str) -> PlaybackPlan:
    """The channel's playback plan, read through the CDN as a player reads it."""
    ssm = stage.session.client("ssm", region_name=stage.region)
    name = release_names.parameter_name(stage.prefix, release_names.Parameter.CDN_BASE_URL)
    cdn = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    with ipv4_client(timeout=10.0) as http:
        response = http.get(f"{cdn.rstrip('/')}/{release_names.plan_key(channel)}")
    response.raise_for_status()
    return PlaybackPlan.model_validate_json(response.text)


def function_log_group(stage: Stage, function: str) -> str:
    configuration = stage.session.client("lambda", region_name=stage.region).get_function_configuration(
        FunctionName=function
    )
    return str(configuration["LoggingConfig"]["LogGroup"])


def test_a_session_gets_the_booked_creative_in_a_break(aired: Aired) -> None:
    """MediaTailor stitched the line's creative into a viewer's break."""
    ads = [ad for avail in aired.stitched for ad in (avail.get("avail") or {}).get("creativeAds") or []]
    assert any(ad.get("creativeUniqueId") == aired.creative_id for ad in ads), (
        aired.creative_id,
        sorted({str(ad.get("creativeUniqueId")) for ad in ads}),
    )


def test_its_delivery_is_recorded_and_in_the_ledger(aired: Aired, booking: Booking, selling: Stage) -> None:
    """Our tracker's impressions of the line are delivery records credited to its reservations, and money events in
    its ledger chain ``pds/line/<line>``."""
    delivered = [d for d in aired.deliveries if d.get("line") == booking.line and d.get("credit") == Credit.DELIVERED]
    assert delivered, [d for d in aired.deliveries if d.get("crid") == aired.crid]
    assert all(d["crid"] == aired.crid and Decimal(str(d["amount"])) == Decimal("0.04") for d in delivered), delivered
    table = selling.session.resource("dynamodb", region_name=selling.region).Table(ledger_table(selling.prefix))
    entries = table.query(KeyConditionExpression=Key("pk").eq(f"pds/line/{booking.line}"))["Items"]
    actions = {str(entry.get("action")) for entry in entries}
    assert {"money.budget", "money.reserve", "money.deliver"} <= actions, actions


def test_the_buyer_reads_the_delivery_over_adcp(aired: Aired, booking: Booking, seller: Seller, buyer: Buyer) -> None:
    """The seller's ``get_media_buy_delivery`` reports our count of the line's impressions (a seller-side server count,
    not MRC), and the buyer agent reads it with its own tool."""
    impressions = len({(d["sid"], d["evid"]) for d in aired.deliveries if d.get("line") == booking.line})

    def reported() -> dict[str, Any] | None:
        answer = seller.call("get_media_buy_delivery", account=ACCOUNT, media_buy_ids=[booking.media_buy_id])
        [found] = answer["media_buy_deliveries"]
        return found if int(found["totals"]["impressions"]) >= impressions else None

    found = wait_for(reported, timeout=DELIVERY_TIMEOUT_S, interval=20, what="the delivery at the seller")
    assert Decimal(str(found["totals"]["spend"])) >= Decimal(impressions) * Decimal("0.04")
    text = reply(buyer.turn(delivery_question(booking.media_buy_id), f"{booking.session_id}-delivery"))
    print(f"buyer: {text[:400]}")
    said = re.search(r"IMPRESSIONS=\s*(\d+)", text)
    assert said is not None and int(said[1]) >= impressions, text
