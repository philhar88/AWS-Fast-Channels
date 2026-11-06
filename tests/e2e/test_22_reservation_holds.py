"""Reservation holds, end to end (ADR-0022, ADR-0036 "Reservation holds"): line ads that never air give their budget
back 10 to 15 minutes after they were reserved, and the ones that aired are delivered exactly once.

Runs only on a stage with the Pds and Release stacks and the demand module (``STAGE=fastci`` with ``modules.pds`` and
``modules.release``, after ``make deploy-demand``); skipped otherwise. The module fixture does the billable part once:

1. start the channel, ingest the test pattern while the census gate scales Prebid Server out, and wait until every
   running Prebid Server task's warm-up succeeded;
2. schedule the pattern with one 120.12 s break, read the own book's budgets, and join one viewer on the stable
   sibling;
3. send a burst of :data:`BURST` ad requests for the stable sibling, as MediaTailor sends them for sessions that leave
   before their break: each a session of its own, none ever played, so their reserved line ads never air;
4. play the viewer through the break, requesting every stitched ad segment (MediaTailor then fires the trackers), and
   stop the channel;
5. wait until the sweeper has released every reservation of the burst (at most the hold and one sweep after the last
   was made, plus a minute), then read the reservations, the budgets, the ledger and the ``pds.delivery`` records.

About 25 minutes, one channel run.
"""

import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
from _stack import Module, Stage
from harness import BREAK_POLICY, LEAD, Channel, Player, Tools
from harness_demand import DemandEdge
from harness_identity import wait_until_serving
from harness_pods import (
    SMART_TV_USER_AGENT,
    Logs,
    Sources,
    Viewer,
    ipv4_client,
    milliseconds,
    objects,
    playback_plan,
    record_pattern,
    wait_until_ready,
    watch,
)
from playback import break_windows

from fast_channels.core.ledger import ledger_table
from fast_channels.core.pbs_config import auction_body
from fast_channels.core.pds import names as pds_names
from fast_channels.core.pds.delivery import (
    BUDGET_SHARDS,
    PENDING,
    SWEEP_EVERY,
    Credit,
    State,
    budget_key,
    chain_id,
    release_after,
)
from fast_channels.core.pds.records import DELIVERY_RECORD, SELECT_RECORD

pytestmark = pytest.mark.e2e

BURST: Final = 20
"""Ad requests for sessions that never play their break: each reserves the pod's line ads."""
POD_SECONDS: Final = 120
LONG_BREAK: Final = {**BREAK_POLICY, "slate_gops": 60}
"""One 120.12 s break."""
TAIL: Final = timedelta(seconds=90)
SLACK: Final = timedelta(minutes=1)
"""What a release may take beyond its hold and one sweep: the scheduled run's start and its own work."""
LOGS_TIMEOUT_S: Final = 300
REQUEST_TIMEOUT_S: Final = 10


@dataclass
class Run:
    """The run: the viewer's session, the burst's breaks and what the PDS's table and logs hold for both."""

    stage: Stage
    hold: timedelta
    viewer: str
    """The viewer's MediaTailor session id."""
    burst: list[str]
    """The burst's decision ids (``<sid>.<evid>``)."""
    statuses: list[int]
    """The burst's HTTP statuses."""
    budgets_before: dict[str, Decimal]
    """Each line's ``remaining`` over its shards before the viewer joined and the burst, read at ``before_at``."""
    budgets_after: dict[str, Decimal]
    """The same once the burst's reservations were released, read at ``after_at``."""
    before_at: datetime
    after_at: datetime
    reservations: dict[str, list[dict[str, Any]]]
    """Each decision's reservation items, as read at the end."""
    viewer_decisions: list[str]
    deliveries: list[dict[str, Any]]
    """The viewer's ``pds.delivery`` records (the burst's reservations show any delivery of their own)."""
    ledger: list[dict[str, Any]]
    """The lines' money events (their ledger chains), each with its ``detail`` parsed."""


def remaining(dynamodb: Any, table: str, lines: list[str]) -> dict[str, Decimal]:
    """Each line's ``remaining`` over its budget shards (strongly consistent)."""
    found: dict[str, Decimal] = {}
    for line in lines:
        total = Decimal(0)
        for shard in range(BUDGET_SHARDS):
            key = {k: {"S": v} for k, v in budget_key(line, shard).items()}
            item = dynamodb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
            total += Decimal(item["remaining"]["N"]) if item else Decimal(0)
        found[line] = total
    return found


def lines_of(dynamodb: Any, table: str) -> list[str]:
    """The lines that have budget shards in the table (the snapshot's own book, and booked buys)."""
    pages = dynamodb.get_paginator("scan").paginate(
        TableName=table,
        FilterExpression="begins_with(pk, :budget)",
        ExpressionAttributeValues={":budget": {"S": "BUDGET#"}},
        ProjectionExpression="pk",
    )
    return sorted({item["pk"]["S"].removeprefix("BUDGET#") for page in pages for item in page["Items"]})


def reservations_of(dynamodb: Any, table: str, decision: str) -> list[dict[str, Any]]:
    """The reservation items of one break (``RES#<decision>``), one per line ad."""
    response = dynamodb.query(
        TableName=table,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": f"RES#{decision}"}},
        ConsistentRead=True,
    )
    return [{k: next(iter(v.values())) for k, v in item.items()} for item in response["Items"]]


def ledger_of(dynamodb: Any, table: str, lines: list[str]) -> list[dict[str, Any]]:
    """The money events of ``lines`` (their chains ``pds/line/<id>``), each with its ``detail`` parsed."""
    found = []
    for line in lines:
        pages = dynamodb.get_paginator("query").paginate(
            TableName=table,
            KeyConditionExpression="pk = :chain",
            ExpressionAttributeValues={":chain": {"S": chain_id(line)}},
            ConsistentRead=True,
        )
        for page in pages:
            for item in page["Items"]:
                entry = {k: next(iter(v.values())) for k, v in item.items()}
                if entry.get("type") == "entry":
                    found.append(entry | {"detail": json.loads(entry["detail"])})
    return found


def burst_request(edge: DemandEdge, *, sibling: str, sid: str, evid: str) -> int:
    """MediaTailor's ad request for one session's 120 s break on ``sibling``: not a probe, so line ads are reserved."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": SMART_TV_USER_AGENT,
        "X-Fast-Sid": sid,
        "X-Fast-Pc": sibling,
        "X-Fast-Origin": edge.secret,
    }
    query = f"evid={evid}&upid=e2e-pattern&dur={POD_SECONDS * 1000}"
    response = httpx.post(
        f"https://{edge.domain}/openrtb2/auction?{query}",
        json=auction_body(POD_SECONDS),
        headers=headers,
        timeout=REQUEST_TIMEOUT_S,
    )
    return response.status_code


@pytest.fixture(scope="module")
def holding(stage: Stage) -> Stage:
    missing = [m.value for m in (Module.PDS, Module.RELEASE) if stage.status(m) is None]
    if missing:
        pytest.skip(f"stage {stage.prefix} lacks the stacks of {missing} (deploy-demand with pds and release)")
    return stage


@pytest.fixture(scope="module")
def run(holding: Stage, channel: Channel, tools: Tools, request: pytest.FixtureRequest) -> Iterator[Run]:
    stage, edge = holding, DemandEdge(holding.prefix, holding.session)
    if not edge.deployed:
        pytest.skip(f"stage {stage.prefix} has no demand module deployed (make deploy-demand)")
    functions = stage.session.client("lambda", region_name=stage.region)
    sweeper = functions.get_function_configuration(FunctionName=stage.output(Module.PDS, "SweeperFunction"))
    hold = timedelta(seconds=int(sweeper["Environment"]["Variables"]["PDS_RELEASE_AFTER_SECONDS"]))
    dynamodb = stage.session.client("dynamodb", region_name=stage.region)
    table = pds_names.table_name(stage.prefix)
    evid = str(uuid.uuid4().int % 9_000_000_000 + 1_000_000_000)
    burst = [f"{uuid.uuid4()}.{evid}" for _ in range(BURST)]
    with ipv4_client(follow_redirects=True, timeout=20.0, headers={"User-Agent": SMART_TV_USER_AGENT}) as http:
        try:
            tools("channels___start_channel", channel=channel.name)
            asset = request.getfixturevalue("ingested")  # while Prebid Server scales out
            wait_until_ready(stage, edge)
            start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
            plan = tools(
                "schedule___plan",
                channel=channel.name,
                picks=[asset.asset_id],
                start=start.isoformat(),
                break_policy=LONG_BREAK,
            )
            assert plan["publishable"], f"plan {plan['plan_id']} has errors: {plan['violations']}"
            [window] = break_windows(tools("schedule___publish", plan_id=plan["plan_id"])["programs"])
            playback = playback_plan(stage, channel.name)
            lines = lines_of(dynamodb, table)
            assert lines, "the own book has no line with a budget"
            before_at = datetime.now(UTC)
            before = remaining(dynamodb, table, lines)
            wait_until_serving(http, playback.playback["hls"])
            joined = datetime.now(UTC)
            viewer = Viewer.join(Player(client=http), playback.playback["hls"])
            statuses = [
                burst_request(edge, sibling=playback.configuration, sid=decision.split(".")[0], evid=evid)
                for decision in burst
            ]
            burst_at = datetime.now(UTC)
            watch([viewer], ends=window.ends + TAIL)
        finally:
            tools("channels___stop_channel", channel=channel.name)

    # Until every reservation of the burst left RESERVED, at most the hold and one sweep after the last was made
    # (and the slack): what is still open then fails the release check.
    deadline_at = burst_at + hold + SWEEP_EVERY + SLACK
    while datetime.now(UTC) < deadline_at:
        items = [item for decision in burst for item in reservations_of(dynamodb, table, decision)]
        if items and all(item["state"] != State.RESERVED for item in items):
            break
        time.sleep(30)

    logs = Logs(stage.session.client("logs", region_name=stage.region), start_ms=milliseconds(joined))
    sources = Sources.of(stage, edge)
    selects = objects(logs, sources.select, record_pattern(SELECT_RECORD, {viewer.sid}))
    viewer_decisions = sorted({str(record["decision_id"]) for record in selects})
    deadline = time.monotonic() + LOGS_TIMEOUT_S
    while True:  # the viewer's impressions are delivered within seconds of the firing; the records follow
        deliveries = objects(logs, sources.delivery, record_pattern(DELIVERY_RECORD, {viewer.sid}))
        reserved = {
            (decision, item["sk"])
            for decision in viewer_decisions
            for item in reservations_of(dynamodb, table, decision)
            if item["state"] == State.DELIVERED
        }
        counted = {(f"{d['sid']}.{d['evid']}", d["crid"]) for d in deliveries if d.get("impression")}
        if reserved <= counted or time.monotonic() > deadline:
            break
        time.sleep(20)
    decisions = set(burst) | set(viewer_decisions)
    found = Run(
        stage=stage,
        hold=hold,
        viewer=viewer.sid,
        burst=burst,
        statuses=statuses,
        budgets_before=before,
        budgets_after=remaining(dynamodb, table, lines),
        before_at=before_at,
        after_at=datetime.now(UTC),
        reservations={d: reservations_of(dynamodb, table, d) for d in sorted(decisions)},
        viewer_decisions=viewer_decisions,
        deliveries=deliveries,
        ledger=ledger_of(dynamodb, ledger_table(stage.prefix), lines),
    )
    print(report(found))
    yield found


def report(run: Run) -> str:
    burst = [item for d in run.burst for item in run.reservations[d]]
    held = [
        datetime.fromisoformat(item["released_at"]) - datetime.fromisoformat(item["reserved_at"])
        for item in burst
        if item.get("released_at")
    ]
    rows = [
        f"hold {run.hold}, burst {len(run.burst)} breaks ({run.statuses.count(200)} answered 200), "
        f"{len(burst)} line ads reserved",
        f"released after {min(held, default=None)} to {max(held, default=None)}",
        f"budgets before {run.budgets_before}, after {run.budgets_after}",
        f"viewer {run.viewer[:8]}: decisions {run.viewer_decisions}",
    ]
    rows += [
        f"  {decision[-12:]} {item['sk']}: {item['state']}"
        for decision in run.viewer_decisions
        for item in run.reservations[decision]
    ]
    return "\n".join(rows)


def burst_items(run: Run) -> list[dict[str, Any]]:
    return [item for decision in run.burst for item in run.reservations[decision]]


def test_the_stage_holds_a_reservation_for_its_prefetch_lead(run: Run) -> None:
    """With prefetch's default lead (150 s) the hold is 10 minutes; the sweeper runs every 5."""
    assert run.hold == release_after(150) == timedelta(minutes=10)


def test_the_burst_reserved_line_ads(run: Run) -> None:
    assert run.statuses == [200] * BURST, run.statuses
    items = burst_items(run)
    assert items, "no ad request of the burst reserved a line ad"
    assert {decision for decision in run.burst if run.reservations[decision]} == set(run.burst), (
        "every break's pod held a line ad"
    )


def test_every_unplayed_reservation_is_released_10_to_15_minutes_after_it_was_made(run: Run) -> None:
    for item in burst_items(run):
        assert item["state"] == State.RELEASED, item
        assert item.get("pending") != PENDING, item
        held = datetime.fromisoformat(item["released_at"]) - datetime.fromisoformat(item["reserved_at"])
        assert run.hold < held <= run.hold + SWEEP_EVERY + SLACK, (item["pk"], item["sk"], held)


def subject_of(decision: str, item: dict[str, Any]) -> str:
    return f"{decision}/{item['sk']}"


def test_the_budget_of_the_unplayed_ads_is_back(run: Run) -> None:
    """The burst's whole price came back to its lines; and each line's budget moved by exactly the ledger's money of
    the window (other breaks' reservations and releases, and the viewer's), so nothing else holds it."""
    burst = {subject_of(d, item) for d in run.burst for item in run.reservations[d]}
    reserved: dict[str, Decimal] = {}
    released: dict[str, Decimal] = {}
    for entry in run.ledger:
        if entry["subject"] in burst:
            into = {"money.reserve": reserved, "money.release": released}.get(entry["action"])
            if into is not None:
                line = entry["detail"]["line"]
                into[line] = into.get(line, Decimal(0)) + Decimal(entry["detail"]["amount"])
    assert reserved, "the burst reserved nothing"
    assert released == reserved, (reserved, released)

    window = [e for e in run.ledger if run.before_at <= datetime.fromisoformat(e["at"]) < run.after_at]
    assert not [e for e in window if e["action"] == "money.budget"], "a budget changed during the run"
    moved = dict.fromkeys(run.budgets_before, Decimal(0))
    for entry in window:
        amount, line = Decimal(entry["detail"]["amount"]), entry["detail"]["line"]
        match entry["action"], entry["detail"].get("late"):
            case "money.reserve", _:
                moved[line] -= amount
            case "money.release", _:
                moved[line] += amount
            case "money.deliver", True:
                moved[line] -= amount
    changed = {line: run.budgets_after[line] - total for line, total in run.budgets_before.items()}
    print(f"budgets moved {changed}; the window's ledger: {moved} ({len(window)} money events)")
    assert changed == moved, (changed, moved)


def test_the_ledger_has_a_release_for_each_unplayed_ad_and_none_for_the_played(run: Run) -> None:
    by_subject: dict[str, list[str]] = {}
    for entry in run.ledger:
        by_subject.setdefault(entry["subject"], []).append(entry["action"])
    for item in burst_items(run):
        subject = f"{item['pk'].removeprefix('RES#')}/{item['sk']}"
        assert sorted(by_subject.get(subject, [])) == ["money.release", "money.reserve"], subject
    for decision in run.viewer_decisions:
        for item in run.reservations[decision]:
            if item["state"] == State.DELIVERED:
                subject = f"{decision}/{item['sk']}"
                assert sorted(by_subject.get(subject, [])) == ["money.deliver", "money.reserve"], subject


def test_the_played_ads_are_delivered_exactly_once(run: Run) -> None:
    """The viewer's reserved ads that aired: DELIVERED once, never released, though the sweeper ran past their hold;
    a reserved ad that did not air (not stitched) is released like the burst's."""
    delivered = [
        (decision, item)
        for decision in run.viewer_decisions
        for item in run.reservations[decision]
        if item["state"] == State.DELIVERED
    ]
    assert delivered, f"none of the viewer's line ads was delivered: {run.reservations}"
    for decision, item in delivered:
        assert "released_at" not in item, item
        first = [
            d
            for d in run.deliveries
            if f"{d['sid']}.{d['evid']}" == decision and d["crid"] == item["sk"] and d.get("impression")
        ]
        assert len(first) == 1, (decision, item["sk"], first)
        assert first[0]["credit"] == Credit.DELIVERED, first[0]
    for decision in run.viewer_decisions:
        for item in run.reservations[decision]:
            assert item["state"] in {State.DELIVERED, State.RELEASED}, item


def test_nothing_was_delivered_late(run: Run) -> None:
    assert not [d for d in run.deliveries if d.get("credit") == Credit.LATE]
    assert not [item for item in burst_items(run) if item.get("delivered_at")], "a burst ad was delivered"
