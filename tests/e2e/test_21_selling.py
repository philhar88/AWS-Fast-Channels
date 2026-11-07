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
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

import pytest
from _stack import Module, Plane, Stage
from boto3.dynamodb.conditions import Key
from harness import Channel, Tools, wait_for
from harness_pods import Creatives
from harness_selling import (
    BUDGET,
    Aired,
    Booking,
    Buyer,
    Seller,
    account,
    air,
    book,
    buyer_of,
    delivery_question,
    end_flight,
    reply,
    run_brand,
    seller_buy,
    seller_of,
    snapshot_line,
)

from fast_channels.core.demo_demand import demo_index_key
from fast_channels.core.ledger import ledger_table
from fast_channels.core.pds.delivery import Credit
from fast_channels.core.selling.products import product_id
from fast_channels.core.specs import vast_key

pytestmark = pytest.mark.e2e

DELIVERY_TIMEOUT_S: Final = 240
RUN_BRAND: Final = run_brand()
"""This run's brand domain (a subdomain of ``harness_selling.BRAND``), which no creative is registered for: its buy is
served with a demo creative."""
ACCOUNT: Final = account(RUN_BRAND)
"""The account the buyer's journey syncs (its brand, which is also its operator)."""


@pytest.fixture(scope="module")
def selling(stage: Stage) -> Stage:
    if stage.status(Module.SELLING) is None:
        pytest.skip(f"stage {stage.prefix} has no Selling stack (deploy-demand with modules.selling)")
    return stage


@pytest.fixture(scope="module")
def seller(selling: Stage) -> Seller:
    return seller_of(selling)


@pytest.fixture(scope="module")
def buyer(selling: Stage) -> Buyer:
    return buyer_of(selling)


@pytest.fixture(scope="module")
def booking(selling: Stage, seller: Seller, buyer: Buyer, channel: Channel) -> Iterator[Booking]:
    """The scripted journey, run by the buyer agent; the buy it made, read back from the seller. Its flight ends with
    the module: the line holds one of the stage's few pool creatives for a day otherwise, and a later run's buy would
    find none free (no line)."""
    booked = book(seller, buyer, brand=RUN_BRAND, product=product_id(channel.name))
    try:
        yield booked
    finally:
        end_flight(seller, booked.media_buy_id, booked.brand)


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
    buy = seller_buy(seller, booking.media_buy_id, RUN_BRAND)
    assert buy is not None and buy["status"] == "pending_creatives", buy


@pytest.fixture(scope="module")
def line(selling: Stage, booking: Booking) -> dict[str, Any]:
    """The booked line, once the snapshot has it."""
    return snapshot_line(selling, booking.line)


def test_the_booked_buy_is_a_line_of_the_own_book(line: dict[str, Any], booking: Booking, channel: Channel) -> None:
    """Budget, flight, CPM, creative and brand of the buy, sold on its product's channel."""
    assert line["adomain"] == RUN_BRAND
    assert Decimal(line["budget"]) == Decimal(str(BUDGET)) and Decimal(line["cpm"]) == 40
    assert line["channels"] == [channel.name]
    assert datetime.fromisoformat(line["starts"]) == booking.starts
    assert line["creatives"][0] in {"direct1", "direct2"}


@pytest.fixture(scope="module")
def aired(
    selling: Stage,
    line: dict[str, Any],
    booking: Booking,
    channel: Channel,
    tools: Tools,
    request: pytest.FixtureRequest,
) -> Aired:
    crid = str(line["creatives"][0])
    creatives = Creatives.read(
        selling.session.client("s3", region_name=selling.region),
        selling.output(Plane.DATA, "MediaBucketName"),
        index_key=demo_index_key(channel.ladder_version),
        vast_key=vast_key(channel.ladder_version),
    )
    creative_id = next(cid for cid, known in creatives.by_id.items() if known == crid)
    return air(
        selling,
        booking,
        channel,
        tools,
        lambda: request.getfixturevalue("ingested"),
        crid=crid,
        creative_id=creative_id,
    )


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
