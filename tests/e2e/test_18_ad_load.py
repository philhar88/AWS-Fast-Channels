"""Ad load by viewer tier, end to end (ADR-0037): two signed-in viewers of different tiers in the same breaks.

Runs only on a stage with the Pds, Release and Identity stacks (``STAGE=fastci`` with ``modules.pds``,
``modules.release`` and ``modules.identity``); skipped otherwise. The module fixture does the billable part once:

1. seed Dana (``light``: one ad per break, above $20) and Ben (``holdout``: the control plan) with their PDS profiles
   (``scripts/seed_personas.py``), and sign them in;
2. start the channel, ingest the test pattern while the census gate scales Prebid Server out, and wait until every
   running Prebid Server task's warm-up succeeded;
3. schedule the pattern twice, each program with a 120.12 s break (four 30 s slots);
4. mint each viewer a playback ticket (``POST /playback``) and play both side by side, from before the first program
   until after the last break, requesting every stitched ad segment so that MediaTailor fires the ads' beacons;
5. stop the channel and read the PDS's ``pds.policy``, ``pds.select`` and ``pds.delivery`` records of both sessions,
   and the fast-ctv module's lines.

Each break is then checked for both viewers: bound and personalized, answered without a fallback, never empty, its
line ads reserved, and its pod the viewer's tier's. The lake view ``v_ad_load_kpis`` must show both tiers.
"""

import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
import seed_personas
from _stack import Module, Stage
from harness import BREAK_POLICY, LEAD, Channel, Tools, wait_for
from harness_demand import DemandEdge
from harness_identity import join, sign_in, wait_until_serving
from harness_pods import (
    SMART_TV_USER_AGENT,
    Logs,
    Sources,
    event_line,
    ipv4_client,
    line_creatives,
    milliseconds,
    objects,
    playback_plan,
    record_pattern,
    wait_until_ready,
    watch,
)
from playback import Window, break_windows

from fast_channels.core.adload import CONTROL_TIER, HOLDOUT_TIER, AdLoadReason
from fast_channels.core.pds.models import OWN_BOOK, Mode, PodReason, Reason
from fast_channels.core.pds.names import AD_LOAD_VIEW
from fast_channels.core.pds.pod import Outcome
from fast_channels.core.pds.records import DELIVERY_RECORD, POLICY_RECORD, SELECT_RECORD
from fast_channels.core.personas import EMAIL_DOMAIN, LIGHT_TIER, persona

pytestmark = pytest.mark.e2e

LIGHT: Final = "dana"
HOLDOUT: Final = "ben"
PERSONAS: Final = (LIGHT, HOLDOUT)
BAR_CPM: Final = 20.0
"""The ``light`` tier's bar in the shipped stage (``modules.pds.ad_load.light.bar_cpm``)."""
LONG_BREAKS: Final = {**BREAK_POLICY, "slate_gops": 60}
"""One 120.12 s break per program."""
BREAK_SECONDS: Final = [120.12, 120.12]
TAIL: Final = timedelta(seconds=90)
LOGS_TIMEOUT_S: Final = 420
LAKE_TIMEOUT_S: Final = 900
"""The decision records reach the lake through Firehose's buffer (up to 5 minutes) and the hour's partition."""
NOTHING_ELIGIBLE: Final = frozenset({PodReason.EMPTY.value, PodReason.SHORT_AVAIL.value})
SELECT_FALLBACK: Final = "SELECT_FALLBACK"


@dataclass
class Run:
    """The two viewers' run: their sessions, their breaks and what the PDS and the module logged."""

    stage: Stage
    channel: str
    joined: datetime
    windows: list[Window]
    vids: dict[str, str]
    """Each persona's viewer id."""
    sids: dict[str, str]
    """Each persona's MediaTailor session id."""
    policies: list[dict[str, Any]]
    selects: list[dict[str, Any]]
    deliveries: list[dict[str, Any]]
    module: list[dict[str, Any]]
    """The fast-ctv module's ``fast-ctv-pod`` lines of both sessions."""

    def of(self, who: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in records if r.get("sid") == self.sids[who]]

    def selects_by_break(self, who: str) -> dict[str, list[dict[str, Any]]]:
        """A viewer's ``pds.select`` records by the break's ``evid`` (a break may have more than one auction)."""
        found: dict[str, list[dict[str, Any]]] = {}
        for record in self.of(who, self.selects):
            found.setdefault(str(record["evid"]), []).append(record)
        return found

    def shared_breaks(self) -> list[str]:
        """The breaks both viewers were asked for."""
        return sorted(set(self.selects_by_break(LIGHT)) & set(self.selects_by_break(HOLDOUT)))

    def complete(self) -> bool:
        """Both viewers' breaks decided, and every reserved line ad's impression counted."""
        decided = all(len(self.selects_by_break(who)) >= len(self.windows) for who in PERSONAS)
        reserved = {(r["sid"], r["evid"], crid) for r in self.selects for crid in r.get("reserved", [])}
        counted = {(d["sid"], d["evid"], d["crid"]) for d in self.deliveries}
        return decided and reserved <= counted


@pytest.fixture(scope="module")
def tiered(stage: Stage) -> Stage:
    missing = [m.value for m in (Module.PDS, Module.RELEASE, Module.IDENTITY) if stage.status(m) is None]
    if missing:
        pytest.skip(f"stage {stage.prefix} lacks the stacks of {missing} (deploy-demand with pds, release, identity)")
    return stage


@pytest.fixture(scope="module")
def seeded(tiered: Stage) -> dict[str, str]:
    """Dana's and Ben's viewer ids, their users and PDS profiles written (idempotent: the shared stage keeps them)."""
    rows = seed_personas.seed(tiered, [{"id": who, "email": f"{who}@{EMAIL_DOMAIN}"} for who in PERSONAS])
    tiers = {row["persona"]: row.get("tier") for row in rows}
    assert tiers == {LIGHT: persona(LIGHT).tier, HOLDOUT: persona(HOLDOUT).tier}, f"profiles not written: {rows}"
    return {row["persona"]: row["vid"] for row in rows}


def schedule(tools: Tools, channel: str, asset_id: str) -> list[dict[str, Any]]:
    """The pattern twice, each program with one 120.12 s break, starting in a few minutes: the published entries."""
    start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
    plan = tools(
        "schedule___plan", channel=channel, picks=[asset_id] * 2, start=start.isoformat(), break_policy=LONG_BREAKS
    )
    assert plan["publishable"], f"plan {plan['plan_id']} has errors: {plan['violations']}"
    programs: list[dict[str, Any]] = tools("schedule___publish", plan_id=plan["plan_id"])["programs"]
    return programs


@pytest.fixture(scope="module")
def run(
    tiered: Stage, seeded: dict[str, str], channel: Channel, tools: Tools, request: pytest.FixtureRequest
) -> Iterator[Run]:
    stage, edge = tiered, DemandEdge(tiered.prefix, tiered.session)
    if not edge.deployed:
        pytest.skip(f"stage {stage.prefix} has no demand module deployed (make deploy-demand)")
    tokens = {who: sign_in(stage, who) for who in PERSONAS}
    headers = {"User-Agent": SMART_TV_USER_AGENT}
    with ipv4_client(follow_redirects=True, timeout=20.0, headers=headers) as http:
        try:
            tools("channels___start_channel", channel=channel.name)
            asset = request.getfixturevalue("ingested")  # while Prebid Server scales out
            wait_until_ready(stage, edge)
            windows = break_windows(schedule(tools, channel.name, asset.asset_id))
            assert [round((w.ends - w.starts).total_seconds(), 2) for w in windows] == BREAK_SECONDS, windows
            wait_until_serving(http, playback_plan(stage, channel.name).playback["hls"])
            joined = datetime.now(UTC)
            viewers = {who: join(stage, http, tokens[who], channel.name)[0] for who in PERSONAS}
            watch(list(viewers.values()), ends=windows[-1].ends + TAIL)
        finally:
            tools("channels___stop_channel", channel=channel.name)
    found = Run(
        stage=stage,
        channel=channel.name,
        joined=joined,
        windows=windows,
        vids=seeded,
        sids={who: viewer.sid for who, viewer in viewers.items()},
        policies=[],
        selects=[],
        deliveries=[],
        module=[],
    )
    read_logs(found, Sources.of(stage, edge))
    print(report(found))
    yield found


def read_logs(run: Run, sources: Sources) -> None:
    """Fill ``run`` from the logs, read again until it is complete or :data:`LOGS_TIMEOUT_S` passed."""
    client = run.stage.session.client("logs", region_name=run.stage.region)
    since = Logs(client, start_ms=milliseconds(run.joined - timedelta(minutes=1)))
    sids = set(run.sids.values())
    deadline = time.monotonic() + LOGS_TIMEOUT_S
    while True:
        run.policies = objects(since, sources.policy, record_pattern(POLICY_RECORD, sids))
        run.selects = objects(since, sources.select, record_pattern(SELECT_RECORD, sids))
        run.deliveries = objects(since, sources.delivery, record_pattern(DELIVERY_RECORD, sids))
        if run.complete() or time.monotonic() > deadline:
            break
        time.sleep(20)
    run.module = [
        line
        for sid in sorted(sids)
        for event in since.events(sources.pbs, f'"fast-ctv-pod" "{sid}"')
        if (line := event_line(str(event["message"]))) is not None
    ]


def report(run: Run) -> str:
    """Each viewer's breaks: the tier, the ad load and the pod with its prices."""
    rows = [f"break {w.starts:%H:%M:%S}-{w.ends:%H:%M:%S}" for w in run.windows]
    for who in PERSONAS:
        for evid, records in sorted(run.selects_by_break(who).items()):
            for record in records:
                prices = {b["ref"]: b["price"] for b in record["bids"]}
                pod = [f"{ref} ${prices.get(ref)}" for ref in record["pod"]]
                rows.append(
                    f"{who} {run.sids[who][:8]} evid {evid}: tier {record.get('tier')} ad_load {record.get('ad_load')}"
                    f" pod {pod} reserved {record.get('reserved')} reason {record['reason']}"
                )
    return "\n".join(rows)


def won(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The bids of a ``pds.select`` record's pod."""
    by_ref = {b["ref"]: b for b in record["bids"]}
    return [by_ref[ref] for ref in record["pod"]]


# --- the checks ----------------------------------------------------------------------------------------------------


def test_both_viewers_are_bound_and_personalized(run: Run) -> None:
    for who in PERSONAS:
        policies = run.of(who, run.policies)
        assert policies, f"{who}: no pds.policy record"
        assert {(p["reason"], p["mode"] in {Mode.FULL, Mode.PSEUDONYMOUS}) for p in policies} == {(Reason.BOUND, True)}


def test_both_viewers_were_asked_for_every_break(run: Run) -> None:
    assert len(run.shared_breaks()) == len(run.windows), report(run)


def test_the_light_viewer_gets_one_ad_above_the_bar(run: Run) -> None:
    for evid, records in run.selects_by_break(LIGHT).items():
        for record in records:
            load = record["ad_load"]
            assert (record["tier"], load["policy"], load["max_ads"]) == (LIGHT_TIER, LIGHT_TIER, 1), evid
            assert load["bar_cpm"] == BAR_CPM
            assert len(record["pod"]) == 1, f"evid {evid}: {record['pod']}"
            if load["reason"] == AdLoadReason.TIER:
                [ad] = won(record)
                assert not ad["house"] and ad["price"] > BAR_CPM, f"evid {evid}: {ad}"
            else:
                assert load["reason"] == AdLoadReason.MIN_LOAD, f"evid {evid}: {load}"
            turned_away = {b["outcome"] for b in record["bids"] if not b["house"] and b["ref"] not in record["pod"]}
            assert turned_away <= {
                Outcome.BAR,
                Outcome.AD_LOAD,
                Outcome.OUTBID,
                Outcome.SEPARATION,
                Outcome.STANDALONE,
                Outcome.UNRESERVED,
                Outcome.OFF_TARGET,
            }, f"evid {evid}: {turned_away}"


def test_the_holdout_viewer_gets_the_control_plan_and_more_ads(run: Run) -> None:
    light = run.selects_by_break(LIGHT)
    for evid, records in run.selects_by_break(HOLDOUT).items():
        for record in records:
            load = record["ad_load"]
            assert (record["tier"], load["policy"], load["reason"]) == (HOLDOUT_TIER, CONTROL_TIER, "control"), evid
            assert (load["max_ads"], load["bar_cpm"]) == (None, 0)
            assert not [b for b in record["bids"] if b["outcome"] in {Outcome.BAR, Outcome.AD_LOAD}], evid
            if evid in light:
                most = max(len(r["pod"]) for r in light[evid])
                assert len(record["pod"]) > most, f"evid {evid}: holdout {record['pod']}, light pods of {most}"


def test_in_the_same_break_the_bid_requests_were_the_same_for_both_tiers(run: Run) -> None:
    """Wire invariance: the bidders asked are the same for both tiers; only the PDS's choice differs."""
    for evid in run.shared_breaks():
        asked = {
            who: sorted({b["bidder"] for r in run.selects_by_break(who)[evid] for b in r["bids"] if not b["house"]})
            for who in PERSONAS
        }
        assert asked[LIGHT] == asked[HOLDOUT], f"evid {evid}: {asked}"


def test_no_pod_is_empty(run: Run) -> None:
    """Every auction's pod has an ad: the PDS chose one (never ``empty``), and the module rendered winners."""
    assert not [r for r in run.selects if r["reason"] in NOTHING_ELIGIBLE], report(run)
    assert run.module, "no fast-ctv-pod lines for the sessions"
    assert not [line for line in run.module if not line.get("winners")], run.module


def test_no_break_fell_back(run: Run) -> None:
    """Every auction had both answers: the module applied /policy and rendered /select's pod."""
    fallbacks = [
        line
        for line in run.module
        if line.get("policy_fallback") or str(line.get("select", "")).startswith(SELECT_FALLBACK)
    ]
    assert not fallbacks, fallbacks
    decided = {str(r["decision_id"]) for r in run.policies}
    assert {str(r["decision_id"]) for r in run.selects} <= decided, "a pod chosen without its break's policy"


def test_every_line_ad_in_a_pod_is_reserved_and_counted(run: Run) -> None:
    line_ads = set(line_creatives(run.stage, run.channel))  # the own book's lines: demo lines, booked buys
    for record in run.selects:
        by_ref = {b["ref"]: b for b in record["bids"]}
        answered = [by_ref[ref] for ref in (*record["pod"], *record["standalone"])]
        lines = {b["crid"] for b in answered if b["bidder"] == OWN_BOOK and not b["house"] and b["crid"] in line_ads}
        assert set(record["reserved"]) == lines, (
            f"{record['decision_id']}: reserved {record['reserved']}, lines {lines}"
        )
    counted = {(d["sid"], d["evid"], d["crid"]): d for d in run.deliveries}
    for record in run.selects:
        for crid in set(record["reserved"]) & {by["crid"] for by in won(record)}:
            delivery = counted.get((record["sid"], record["evid"], crid))
            assert delivery is not None, f"{record['decision_id']}: {crid} reserved, never counted"
            assert delivery["credit"] in {"delivered", "repeat"}, delivery


def _athena(stage: Stage, sql: str) -> list[dict[str, str | None]]:
    athena = stage.session.client("athena", region_name=stage.region)
    started = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=stage.output(Module.OPS, "LakeWorkGroup"),
        QueryExecutionContext={"Database": stage.output(Module.OPS, "LakeDatabase")},
    )
    query_id = started["QueryExecutionId"]
    while (state := athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]["Status"])["State"] in {
        "QUEUED",
        "RUNNING",
    }:
        time.sleep(2)
    assert state["State"] == "SUCCEEDED", state
    rows = athena.get_query_results(QueryExecutionId=query_id)["ResultSet"]["Rows"]
    header = [cell.get("VarCharValue") or "" for cell in rows[0]["Data"]]
    return [dict(zip(header, (cell.get("VarCharValue") for cell in row["Data"]), strict=True)) for row in rows[1:]]


def test_the_ad_load_view_shows_both_tiers(run: Run) -> None:
    since = (run.joined - timedelta(hours=1)).strftime("%Y/%m/%d/%H")
    sql = (
        "SELECT tier, policy, reason, sum(breaks) AS breaks, sum(ads) AS ads, max(winning_cpm_min) AS lowest "
        f"FROM {AD_LOAD_VIEW} WHERE datehour >= '{since}' AND tier IN ('{LIGHT_TIER}', '{HOLDOUT_TIER}') "
        "GROUP BY tier, policy, reason"
    )

    def found() -> list[dict[str, str | None]] | None:
        rows = _athena(run.stage, sql)
        return rows if {row["tier"] for row in rows} == {LIGHT_TIER, HOLDOUT_TIER} else None

    rows = wait_for(found, timeout=LAKE_TIMEOUT_S, interval=60, what=f"{AD_LOAD_VIEW} rows of both tiers")
    by_tier: dict[str | None, list[dict[str, str | None]]] = {}
    for row in rows:
        by_tier.setdefault(row["tier"], []).append(row)
    per_break = {
        tier: sum(int(r["ads"] or 0) for r in tier_rows) / sum(int(r["breaks"] or 0) for r in tier_rows)
        for tier, tier_rows in by_tier.items()
    }
    assert per_break[LIGHT_TIER] <= 1 < per_break[HOLDOUT_TIER], rows
