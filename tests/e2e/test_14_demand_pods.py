"""The demand path driven by MediaTailor (BUILD-PLAN M8-08, §12.3): viewers on a monetized channel's stable sibling,
through Prebid Server's fast-ctv module, the Pod Decision Service and the bidder simulator, replaying Phase-0 P0-20
on the repository's own stacks.

Runs only on a stage with the Pds and Release stacks (``make deploy-demand`` with ``modules.pds`` and
``modules.release``, for example ``STAGE=fastci``); skipped otherwise. The module fixture does the billable part once:

1. start the channel, ingest the test pattern while the census gate scales Prebid Server out, and wait until every
   running Prebid Server task's warm-up succeeded (the PDS is a sidecar of those tasks);
2. with ``modules.crawl``, put the pattern's catalog title and wait for its context object;
3. schedule the pattern three times: with a 30.03 s break, then twice with a 120.12 s break;
4. join four viewers on the sibling the channel's playback plan names (read through the CDN, never assumed), from
   before the first program until after the last break, two late joiners just after the second break's retrieval
   closed, and one viewer 40 s into the second break. Each requests every stitched ad segment, so that MediaTailor
   fires the ads' beacons. Their connections leave from IPv4, whose address ``checkip.amazonaws.com`` reports. The
   sibling's prefetch schedules are read before the first break;
5. stop the channel, and read the run's logs: the siblings' ADS log, the module's ``fast-ctv-pod`` lines, the PDS's
   records and the simulator's ``sim-bid-request`` lines.

With the release module's prefetch (ADR-0033) MediaTailor asks for each break ahead of it, at a retrieval spread over
the shaping window, and fills the break from that answer; a session that joins after the retrieval closed is asked in
real time at the break. Each request is an auction of its own. The retrieval imports the pod's creatives, so the
first-sight skips (``NEW_CREATIVE``) and partial pods that real-time insertion shows on a new stage do not happen on
retrieved breaks; the late joiners get the pod the retrievals made ready. The first-sight checks run when a winner was
new at its break, and say why they skip otherwise. A viewer who joins during a break is asked for the rest of
it (ADR-0021, M3-07): slate for what aired, then its ads.

Without prefetch, the schedule shows them: the 30 s break's pod is ``direct1`` with the standalone ``sim1``, so by the
first 120 s break both are known. That pod, ``direct1, sim2, sim3, direct2`` with the standalones ``sim1, house1,
house2``, then has three first-sight winners and one known standalone: a replacement in place, and a partial pod that
the threshold of 121 s keeps. By the second 120 s break every creative is known, so the whole pod plays in sequence
order.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Any, Final

import pytest
from _stack import Module, Plane, Stage
from botocore.exceptions import ClientError
from harness import BREAK_POLICY, CONTENT_ID, LEAD, Channel, Player, Tools, wait_for
from harness_demand import DemandEdge
from harness_pods import (
    ADS_EVENTS,
    FIRST_SIGHT,
    LOGGED,
    SMART_TV_USER_AGENT,
    Break,
    Creatives,
    IpAddress,
    Logs,
    Sources,
    Viewer,
    any_of,
    avail_ads,
    breaks,
    egress_address,
    event_line,
    gap_count,
    ipv4_client,
    line_creatives,
    log_gaps,
    masked,
    milliseconds,
    objects,
    playback_plan,
    pod_of,
    record_pattern,
    start_settled,
    timestamp,
    wait_until_ready,
    watch,
)
from playback import Window, break_windows

from fast_channels.core.catalog import ContentItem
from fast_channels.core.crawl import names as crawl
from fast_channels.core.crawl.catalog_context import context_key
from fast_channels.core.demand import Parameter
from fast_channels.core.demo_demand import demo_index_key
from fast_channels.core.events import CatalogItemChanged, put_events_entry
from fast_channels.core.monetization import names
from fast_channels.core.monetization.prefetch import PrefetchPolicy, schedule_name
from fast_channels.core.pbs_config import tracker_host
from fast_channels.core.pds.delivery import CDN_LOG_EVERY, CDN_LOG_LAG, Beacon, Credit, Source, beacon
from fast_channels.core.pds.records import DELIVERY_RECORD, POLICY_RECORD, SELECT_RECORD
from fast_channels.core.specs import vast_key
from fast_channels.functions.tools.catalog import catalog_item

pytestmark = pytest.mark.e2e

VIEWERS: Final = 4
"""Sessions on the sibling; with four, all of them landing in the crawl holdout (20%) is unlikely (0.16%)."""
LATE_VIEWERS: Final = 2
"""Sessions that join after the second break's retrieval closed: MediaTailor asks for their break in real time."""
LATE_JOIN_LEAD: Final = timedelta(seconds=PrefetchPolicy().retrieval_end_s - 10)
"""How long before the second break the late joiners join: 10 s after its retrieval closed (``retrieval_end_s``
before the break, the stage's default), with time left to reach its ad marker."""
MID_JOIN_INTO: Final = timedelta(seconds=40)
"""How far into the second break the mid-break joiner joins."""
FIRST_BREAK: Final = {**BREAK_POLICY, "slate_gops": 15}
"""One 30.03 s break per program (15 GOPs of 2.002 s)."""
LONG_BREAKS: Final = {**BREAK_POLICY, "slate_gops": 60}
"""One 120.12 s break per program."""
BREAK_SECONDS: Final = [30.03, 120.12, 120.12]
CONTEXT_TIMEOUT_S: Final = 120
LOGS_TIMEOUT_S: Final = 420
"""The delivery records follow the ADS log through a subscription; the module's lines follow Prebid Server's log."""
TAIL: Final = timedelta(seconds=90)
"""How long the viewers keep playing after the last break's scheduled end: the live playlist lags the wall clock."""
SEGMENT_SLACK_S: Final = 6.5
"""One content segment (6.006 s): a break's ads and slate are counted in whole segments."""
SIMULATOR: Final = "amt"
"""The bidder simulator's name in Prebid Server's configuration."""
NOTHING_ELIGIBLE: Final = frozenset({"empty", "short_avail"})
"""``/select`` reasons for a pod with nothing in it by rule (``core.pds.models.PodReason``)."""
SELECT_FALLBACK: Final = "SELECT_FALLBACK"
NOT_READY: Final = FIRST_SIGHT | {"TRANSCODE_IN_PROGRESS"}
"""Skip reasons of a creative MediaTailor has not prepared yet."""
OWN_SEATS: Final = frozenset({"ownbook", "fast-ctv"})
"""The seats of the own book's ads and of the module's own house ads (``HousePod.SEAT``)."""
"""The fastci stage's demo lines (``stages/fastci.json``) by creative."""
CDN_COUNT_WAIT: Final = CDN_LOG_LAG + 2 * CDN_LOG_EVERY + timedelta(minutes=5)
"""How long after the last firing the CDN-log runs have counted it: a log file about 5 minutes after its requests,
read :data:`CDN_LOG_LAG` after it arrived, by a run every :data:`CDN_LOG_EVERY`."""
CUT_MARGIN: Final = timedelta(seconds=60)
"""Firings this close to either end of the cut may be counted from either log."""


@dataclass
class Smoke:
    """One run: the viewers, what aired, and what the logs say happened."""

    stage: Stage
    sibling: str
    joined: datetime
    windows: list[Window]
    egress: IpAddress
    images: set[str]
    """The image of every Prebid Server task once ready."""
    viewers: list[Viewer]
    creatives: Creatives
    breaks: dict[tuple[str, str], Break]
    """The viewers' breaks by (session id, ``evid``)."""
    beacons: list[Beacon]
    """Our tracker's firings in the viewers' ADS log (``BEACON_FIRED``)."""
    deliveries: list[dict[str, Any]]
    """The PDS's ``pds.delivery`` records of the viewers' sessions."""
    simulator: list[dict[str, Any]]
    """The simulator's ``sim-bid-request`` lines while the viewers' auctions ran."""
    policies: list[dict[str, Any]]
    """Every ``pds.policy`` record, of any session, while the viewers' auctions ran."""
    whole_address_events: int
    """Events of the simulator's log since the viewers joined that hold the viewers' full address."""
    lines: dict[str, str] = field(default_factory=dict)
    """The own book's line creatives (crid to line id) in the stage's snapshot when the viewers joined."""
    schedules: dict[str, dict[str, Any]] = field(default_factory=dict)
    """The sibling's prefetch schedules while the viewers played, by name."""
    late: set[str] = field(default_factory=set)
    """The late joiners' session ids (among :attr:`viewers`)."""
    late_joined: datetime | None = None
    mid: str | None = None
    """The mid-break joiner's session id (among :attr:`viewers`)."""
    mid_joined: datetime | None = None
    sources: Sources | None = None
    """The run's log groups (the delivery check reads the CDN-log runs' records later)."""
    cut: tuple[datetime, datetime] | None = None
    """When the delivery subscription to the siblings' ADS log was cut and put back (``--cut-ads-delivery``)."""

    @property
    def sids(self) -> set[str]:
        return {viewer.sid for viewer in self.viewers}

    def windows_of(self, sid: str) -> list[Window]:
        """The breaks a viewer was there for: every one, a late joiner's from the second, the mid-break joiner's from
        the one it joined during (it is asked for the rest of it)."""
        joined = self.mid_joined if sid == self.mid else self.late_joined if sid in self.late else None
        return [w for w in self.windows if w.ends > joined] if joined is not None else self.windows

    def breaks_in_order(self, sid: str) -> list[Break]:
        """A viewer's breaks in airing order (by their ``FILLED_AVAIL``, else their first request)."""
        return sorted(self.of(sid), key=lambda b: timestamp(b.filled) if b.filled else (b.requested_at or self.joined))

    def of(self, sid: str) -> list[Break]:
        return [found for found in self.breaks.values() if found.sid == sid]

    def stitched(self) -> set[tuple[str, str, str]]:
        """(session id, ``evid``, crid) of every ad MediaTailor stitched for the viewers."""
        return {
            (found.sid, found.evid, self.creatives.crid(ad) or str(ad.get("creativeUniqueId")))
            for found in self.breaks.values()
            for ad in (found.avail or {}).get("creativeAds") or []
        }

    def fired(self) -> set[tuple[str, str, str]]:
        return {(b.sid, b.evid, b.crid) for b in self.beacons}

    def counted(self) -> set[tuple[str, str, str]]:
        return {(d["sid"], d["evid"], d["crid"]) for d in self.deliveries}

    def gaps(self) -> set[tuple[str, str]]:
        """W-2 1.32's known gaps (``harness_pods.log_gaps``): breaks the ADS log left incomplete although the PDS
        decided them and our tracker shows them played."""
        played = {(b.sid, b.evid) for b in self.beacons}
        played |= {(d["sid"], d["evid"]) for d in self.deliveries if d.get("impression")}
        return log_gaps(self.breaks.values(), played)

    def complete(self) -> bool:
        """Every viewer's breaks answered, decided and rendered; every stitched ad's firing logged and counted."""
        decided = all(
            len(found := self.of(sid)) == len(self.windows_of(sid))
            and all(
                b.avail is not None and b.auctions() and all(a.policy and a.select and a.lines for a in b.auctions())
                for b in found
            )
            for sid in self.sids
        )
        return decided and self.stitched() <= self.fired() <= self.counted()

    def report(self) -> str:
        """The breaks' windows and prefetch schedules, then per break: MediaTailor's requests, what the PDS placed for
        the aired one and what MediaTailor stitched."""
        rows = [f"break {clock(w.starts)}-{clock(w.ends)}" for w in self.windows]
        rows += [
            f"{name}: {', '.join(f'{k} {clock(v) if isinstance(v, datetime) else v}' for k, v in s.items())}"
            for name, s in sorted(self.schedules.items())
        ]
        if self.late_joined is not None:
            rows.append(f"late joiners {sorted(sid[:8] for sid in self.late)} at {clock(self.late_joined)}")
        if self.mid is not None:
            rows.append(f"mid-break joiner {self.mid[:8]} at {clock(self.mid_joined)}")
        if self.cut is not None:
            rows.append(f"delivery subscription cut {clock(self.cut[0])}-{clock(self.cut[1])}")
        gaps = self.gaps()
        rows.append(f"ADS log gaps (W-2 1.32): {len(gaps)} {sorted((sid[:8], evid) for sid, evid in gaps)}")
        for found in sorted(self.breaks.values(), key=lambda b: (b.requested_at or self.joined, b.sid)):
            pod, standalones = found.rendered()
            avail, fill = found.avail or {}, found.fill(self.creatives)
            aired = found.aired
            line = aired.lines[-1] if aired and aired.lines else {}
            asked = ", ".join(f"{r.prefetch or 'real time'} {clock(r.at)}" for r in found.requests)
            at = clock(timestamp(found.filled)) if found.filled else None
            rows.append(
                f"{found.sid[:8]} evid {found.evid}: asked {asked}; filled from {found.filled_from or 'real time'} at "
                f"{at}; policy {line.get('policy_ms')} ms {line.get('policy_fallback')}, "
                f"select {line.get('select_ms')} ms {line.get('select')}; pod {pod} standalones {standalones}; filled "
                f"{avail.get('filledDuration')}/{avail.get('originAvailDuration')}; replaced {list(fill.replaced)}; "
                f"unfilled {list(fill.unfilled)}; problems {list(fill.problems)}"
            )
        return "\n".join(rows)


@pytest.fixture(scope="module")
def pds(stage: Stage) -> Stage:
    if stage.status(Module.PDS) is None or stage.status(Module.RELEASE) is None:
        pytest.skip(f"stage {stage.prefix} has no Pds and Release stacks (deploy-demand with modules.pds and release)")
    return stage


@pytest.fixture(scope="module")
def smoke(pds: Stage, channel: Channel, tools: Tools, run_id: str, request: pytest.FixtureRequest) -> Smoke:
    stage, edge = pds, DemandEdge(pds.prefix, pds.session)
    if not edge.deployed:
        pytest.skip(f"stage {stage.prefix} has no demand module deployed (make deploy-demand)")
    headers = {"User-Agent": SMART_TV_USER_AGENT}
    with ipv4_client(follow_redirects=True, timeout=20.0, headers=headers) as http:
        egress = egress_address(http)
        cleanups: list[Callable[[], None]] = []
        cut = DeliveryCut(stage) if request.config.getoption("cut_ads_delivery") else None
        try:
            start_settled(stage, tools, channel.name)
            asset = request.getfixturevalue("ingested")  # while Prebid Server scales out
            if stage.status(Module.CRAWL) is not None:
                cleanups.append(publish_context(stage, run_id))
            images = wait_until_ready(stage, edge)
            windows = break_windows(schedule(tools, channel.name, asset.asset_id))
            assert [round((w.ends - w.starts).total_seconds(), 2) for w in windows] == BREAK_SECONDS, windows
            plan = playback_plan(stage, channel.name)
            lines = line_creatives(stage, channel.name)
            joined = datetime.now(UTC)
            viewers = [Viewer.join(Player(client=http), plan.playback["hls"]) for _ in range(VIEWERS)]
            schedules = prefetch_schedules(stage, plan.configuration)
            watch(viewers, ends=windows[0].starts)
            schedules |= prefetch_schedules(stage, plan.configuration)
            watch(viewers, ends=windows[1].starts - LATE_JOIN_LEAD)
            if cut is not None:  # the first break's beacons are counted by now: its one ad fired at its start
                cut.cut()
                cleanups.append(cut.restore)
            late_joined = datetime.now(UTC)
            late = [Viewer.join(Player(client=http), plan.playback["hls"]) for _ in range(LATE_VIEWERS)]
            viewers += late
            watch(viewers, ends=windows[1].starts + MID_JOIN_INTO)
            mid_joined = datetime.now(UTC)
            mid = Viewer.join(Player(client=http), plan.playback["hls"])
            viewers.append(mid)
            if cut is not None:  # put back between the second break and the third
                watch(viewers, ends=min(windows[1].ends + timedelta(seconds=30), windows[2].starts))
                cut.restore()
            watch(viewers, ends=windows[-1].ends + TAIL)
        finally:
            tools("channels___stop_channel", channel=channel.name)
            for cleanup in cleanups:
                cleanup()
    sources = Sources.of(stage, edge)
    smoke = Smoke(
        stage=stage,
        sibling=plan.configuration,
        joined=joined,
        windows=windows,
        egress=egress,
        images=images,
        viewers=viewers,
        creatives=Creatives.read(
            stage.session.client("s3", region_name=stage.region),
            stage.output(Plane.DATA, "MediaBucketName"),
            index_key=demo_index_key(channel.ladder_version),
            vast_key=vast_key(channel.ladder_version),
        ),
        breaks={},
        beacons=[],
        deliveries=[],
        simulator=[],
        policies=[],
        whole_address_events=0,
        schedules=schedules,
        lines=lines,
        late={viewer.sid for viewer in late},
        late_joined=late_joined,
        mid=mid.sid,
        mid_joined=mid_joined,
        sources=sources,
        cut=cut.window if cut is not None else None,
    )
    read_logs(smoke, sources)
    print(smoke.report())
    return smoke


class DeliveryCut:
    """The delivery function's subscription to the siblings' ADS log, deleted and put back as it was
    (``--cut-ads-delivery``): MediaTailor still logs its beacons meanwhile, but the delivery function is not sent
    them, so only the CDN's log can count them."""

    def __init__(self, stage: Stage) -> None:
        self.logs = stage.session.client("logs", region_name=stage.region)
        self.group = stage.output(Module.RELEASE, "SiblingLogGroup")
        [self.filter] = [
            f
            for f in self.logs.describe_subscription_filters(logGroupName=self.group)["subscriptionFilters"]
            if f["filterName"] == f"{stage.prefix}-pds-delivery-release"
        ]
        self.at: datetime | None = None
        self.back: datetime | None = None

    def cut(self) -> None:
        self.logs.delete_subscription_filter(logGroupName=self.group, filterName=self.filter["filterName"])
        self.at = datetime.now(UTC)
        print(f"delivery subscription cut at {clock(self.at)}")

    def restore(self) -> None:
        if self.at is None or self.back is not None:
            return
        self.logs.put_subscription_filter(
            logGroupName=self.group,
            filterName=self.filter["filterName"],
            filterPattern=self.filter["filterPattern"],
            destinationArn=self.filter["destinationArn"],
        )
        self.back = datetime.now(UTC)
        print(f"delivery subscription put back at {clock(self.back)}")

    @property
    def window(self) -> tuple[datetime, datetime] | None:
        return (self.at, self.back) if self.at is not None and self.back is not None else None


def prefetch_schedules(stage: Stage, configuration: str) -> dict[str, dict[str, Any]]:
    """The configuration's prefetch schedules: their retrieval and consumption windows and matching value."""
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    found = {}
    for page in mediatailor.get_paginator("list_prefetch_schedules").paginate(
        PlaybackConfigurationName=configuration, ScheduleType="ALL"
    ):
        for item in page.get("Items", []):
            retrieval, consumption = item.get("Retrieval", {}), item.get("Consumption", {})
            found[str(item["Name"])] = {
                "type": item.get("ScheduleType"),
                "retrieval": f"{clock(retrieval.get('StartTime'))}-{clock(retrieval.get('EndTime'))}",
                "consumption": f"{clock(consumption.get('StartTime'))}-{clock(consumption.get('EndTime'))}",
                "retrieval_ends": retrieval.get("EndTime"),
                "variables": retrieval.get("DynamicVariables"),
            }
    return found


def clock(moment: datetime | None) -> str:
    return f"{moment:%H:%M:%S.%f}"[:12] if moment is not None else "-"


def publish_context(stage: Stage, run_id: str) -> Callable[[], None]:
    """Put the pattern's catalog title, announce it and wait for its context object; returns the cleanup."""
    table = stage.session.resource("dynamodb", region_name=stage.region).Table(
        stage.output(Plane.DATA, "CatalogTableName")
    )
    s3, events = stage.session.client("s3", region_name=stage.region), stage.session.client("events")
    bucket = stage.output(Module.CRAWL, "CatalogContextBucketName")
    title = ContentItem(
        content_id=CONTENT_ID,
        title="End-to-end test pattern",
        rating="TV-G",
        genres=["documentary"],
        duration_ms=70_000,
        language="en",
    )
    table.put_item(Item=catalog_item(title))
    entry = put_events_entry(
        CatalogItemChanged(content_id=CONTENT_ID, correlation_id=run_id),
        prefix=stage.prefix,
        event_bus=stage.output(Plane.MEDIA, "EventBusName"),
    )
    response = events.put_events(Entries=[entry])  # type: ignore[list-item]  # put_events_entry builds a plain dict
    assert response["FailedEntryCount"] == 0, response

    def published() -> bool | None:
        try:
            s3.head_object(Bucket=bucket, Key=context_key(CONTENT_ID))
        except ClientError:
            return None
        return True

    wait_for(published, timeout=CONTEXT_TIMEOUT_S, interval=3, what=f"the context object of {CONTENT_ID}")

    def remove() -> None:
        table.delete_item(Key={"pk": f"CONTENT#{CONTENT_ID}", "sk": "META"})
        s3.delete_object(Bucket=bucket, Key=context_key(CONTENT_ID))

    return remove


def schedule(tools: Tools, channel: str, asset_id: str) -> list[dict[str, Any]]:
    """The pattern with a 30.03 s break, starting in a few minutes, then twice with a 120.12 s break: the published
    EPG entries."""
    start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
    programs: list[dict[str, Any]] = []
    for arguments in (
        {"picks": [asset_id], "start": start.isoformat(), "break_policy": FIRST_BREAK},
        {"picks": [asset_id] * 2, "break_policy": LONG_BREAKS},  # appended to the published schedule
    ):
        plan = tools("schedule___plan", channel=channel, **arguments)
        assert plan["publishable"], f"plan {plan['plan_id']} has errors: {plan['violations']}"
        programs += tools("schedule___publish", plan_id=plan["plan_id"])["programs"]
    return programs


def read_logs(smoke: Smoke, sources: Sources) -> None:
    """Fill ``smoke`` from the run's logs, read again until it is complete or :data:`LOGS_TIMEOUT_S` passed."""
    client = smoke.stage.session.client("logs", region_name=smoke.stage.region)
    since = Logs(client, start_ms=milliseconds(smoke.joined - timedelta(minutes=1)))
    sids = smoke.sids
    host = tracker_host(smoke.stage.output(Plane.MEDIA, "ContentBaseUrl"))
    deadline = time.monotonic() + LOGS_TIMEOUT_S
    while True:
        ads = objects(since, sources.ads, f"{{ {any_of('sessionId', sids)} }}")
        records = objects(since, sources.policy, record_pattern(POLICY_RECORD, sids))
        records += objects(since, sources.select, record_pattern(SELECT_RECORD, sids))
        lines = [
            line | {LOGGED: event["timestamp"]}
            for sid in sorted(sids)
            for event in since.events(sources.pbs, f'"fast-ctv-pod" "{sid}"')
            if (line := event_line(str(event["message"]))) is not None
        ]
        smoke.breaks = {key: found for key, found in breaks(ads, records, lines).items() if key[0] in sids}
        smoke.beacons = [b for b in (beacon(e, host=host) for e in ads) if isinstance(b, Beacon)]
        smoke.deliveries = objects(since, sources.delivery, record_pattern(DELIVERY_RECORD, sids))
        if smoke.complete() or time.monotonic() > deadline:
            break
        time.sleep(20)
    requested = [t for b in smoke.breaks.values() if (t := b.requested_at) is not None]
    answered = [timestamp(e) for b in smoke.breaks.values() for e in b.events if e.get("eventType") == "VAST_RESPONSE"]
    if requested and answered:
        auctions = Logs(
            client,
            start_ms=milliseconds(min(requested) - timedelta(seconds=5)),
            end_ms=milliseconds(max(answered) + timedelta(seconds=5)),
        )
        smoke.simulator = [
            line
            for event in auctions.events(sources.simulator, '"sim-bid-request"')
            if (line := event_line(str(event["message"]))) is not None
        ]
        smoke.policies = objects(auctions, sources.policy, f'{{ $.record = "{POLICY_RECORD}" }}')
    smoke.whole_address_events = len(since.events(sources.simulator, f'"{smoke.egress}"'))


# --- the checks ----------------------------------------------------------------------------------------------------


def test_every_break_is_asked_answered_as_xml_and_rendered_from_the_pds(smoke: Smoke) -> None:
    """Each viewer's every break: MediaTailor's POST and a VAST response it accepted (no ``ERROR_*`` event) and a
    ``FILLED_AVAIL``. Each of its ad server requests (the prefetch retrieval, and a real-time request at the break if
    MediaTailor made one) is one auction: one ``/policy`` and one ``/select`` record and one module line, with the
    patch applied and exactly that ``/select`` pod rendered. No record or line is outside those auctions. A known ADS
    log gap (W-2 1.32: decided and played, but not logged whole) is left out of the ADS-log checks."""
    gaps = smoke.gaps()
    for viewer in smoke.viewers:
        found = smoke.of(viewer.sid)
        assert len(found) == len(smoke.windows_of(viewer.sid)), (viewer.sid, [b.evid for b in found], viewer.errors[:5])
        for each in found:
            if (each.sid, each.evid) in gaps:
                continue
            assert set(each.event_types) >= ADS_EVENTS, (each.sid, each.evid, each.event_types)
            assert not [t for t in each.event_types if t.startswith("ERROR")], (each.sid, each.evid, each.event_types)
            auctions = each.auctions()
            assert each.aired is not None, (each.sid, each.evid, each.filled_from, [a.request for a in auctions])
            for auction in auctions:
                assert (len(auction.policy), len(auction.select), len(auction.lines)) == (1, 1, 1), auction
                [line], [select] = auction.lines, auction.select
                assert "error" not in line and line["policy_fallback"] is None, line
                assert not str(line["select"]).startswith(SELECT_FALLBACK), line
                pod, standalones = pod_of(select)
                seat = {str(bid["crid"]): str(bid["seat"]) for bid in select["bids"]}
                assert line["winners"] == [f"{seat[crid]}:{crid}" for crid in pod], (line, pod)
                assert line["standalones"] == [f"{seat[crid]}:{crid}" for crid in standalones], (line, standalones)
            held = [len(a.policy) + len(a.select) + len(a.lines) for a in auctions]
            assert sum(held) == len(each.policy) + len(each.select) + len(each.pod_lines), (each.sid, each.evid)


def retrieved(smoke: Smoke) -> list[Break]:
    """The breaks MediaTailor retrieved ahead under their prefetch schedule (``pf-<evid>``); skip when none was."""
    found = [b for b in smoke.breaks.values() if any(r.prefetch for r in b.requests)]
    if not found:
        pytest.skip("no break was retrieved ahead: the sibling has no prefetch schedule")
    return found


def test_a_retrieved_break_is_filled_from_its_retrieval(smoke: Smoke) -> None:
    """Prefetch (M3-06): a break MediaTailor retrieved ahead under its schedule ``pf-<evid>`` is filled from that
    retrieval, with no second ad server request at the break (P0-02: every session that retrieved filled from its
    schedule). A second request is a second auction: its bids, reservations and bid requests count twice. A known ADS
    log gap is left out."""
    found = [b for b in retrieved(smoke) if (b.sid, b.evid) not in smoke.gaps()]
    asked_again = [
        (b.sid[:8], b.evid, [(r.prefetch, clock(r.at)) for r in b.requests], b.filled_from)
        for b in found
        if b.filled_from != schedule_name(int(b.evid))
        or [r.prefetch for r in b.requests] != [schedule_name(int(b.evid))]
    ]
    assert not asked_again, f"{len(asked_again)} of {len(found)} retrieved breaks: {asked_again}\n{smoke.report()}"


def test_a_late_joiner_is_asked_in_real_time_and_served(smoke: Smoke) -> None:
    """A session that joins after a break's retrieval closed is not retrieved for it: MediaTailor asks for its break
    in real time (one ad server request, no prefetch schedule), which is accepted behaviour (ADR-0033: "a session
    that joins after a break's retrieval window gets real-time insertion for that break"), and serves it from that
    answer: a pod stitched whole or in part, and no error. Its pod is the earlier sessions' pod, which their retrievals
    made ready, so its first-sight risk is for creatives no session retrieved; the first-sight checks below run when
    one shows."""
    assert smoke.late and smoke.late_joined is not None, "no late joiner"
    for sid in sorted(smoke.late):
        found = sorted(smoke.of(sid), key=lambda b: b.requested_at or smoke.joined)
        assert found, f"late joiner {sid} had no break"
        first = found[0]
        schedule = smoke.schedules.get(schedule_name(int(first.evid)))
        if schedule is None:
            pytest.skip(f"no prefetch schedule for the late joiners' break {first.evid}")
        if (closed := schedule.get("retrieval_ends")) is None or closed > smoke.late_joined:
            pytest.skip(
                f"the run could not fit a late joiner: they joined {smoke.late_joined}, retrieval closed {closed}"
            )
        assert [r.prefetch for r in first.requests] == [None], (sid[:8], first.evid, first.requests)
        assert first.filled_from is None and first.aired is not None, (sid[:8], first.evid, first.filled_from)
        assert not [t for t in first.event_types if t.startswith("ERROR")], (sid[:8], first.evid, first.event_types)
        assert float((first.avail or {}).get("filledDuration", 0)) > 0, (sid[:8], first.evid, first.avail)


def test_a_mid_break_joiner_gets_the_rest_of_that_break_and_the_next_break_is_served(smoke: Smoke) -> None:
    """A session that joins during a break is asked for what is left of it: PRE_SESSION's
    ``aws.availSuppressionMode`` ``AFTER_LIVE_EDGE`` 00:00:00 with ``PARTIAL_AVAIL`` (ADR-0021, M3-07). One real-time
    request for a remainder shorter than the break; the slate covers what had aired, then the pod plays, every ad
    from its first segment (its impression fires); no error; its next break is served."""
    assert smoke.mid is not None and smoke.mid_joined is not None, "no mid-break joiner"
    joined_break = smoke.windows[1]
    assert joined_break.starts < smoke.mid_joined < joined_break.ends, (smoke.mid_joined, joined_break)
    early = next(sid for sid in sorted(smoke.sids - smoke.late) if sid != smoke.mid)
    aired = [b.evid for b in smoke.breaks_in_order(early)]
    found = smoke.breaks_in_order(smoke.mid)
    assert [b.evid for b in found] == aired[1:], (smoke.mid[:8], [b.evid for b in found], aired)
    joined = found[0]
    [request] = joined.requests
    break_ms = (joined_break.ends - joined_break.starts).total_seconds() * 1000
    assert request.prefetch is None and request.dur_ms and 0 < request.dur_ms < break_ms, (joined.evid, request)
    filled = float((joined.avail or {}).get("filledDuration", 0))
    assert 0 < filled <= request.dur_ms / 1000 + 0.1, (joined.evid, joined.avail, request)
    viewer = next(v for v in smoke.viewers if v.sid == smoke.mid)
    starts = splice_near(viewer, joined_break.starts) if viewer.splices else joined_break.starts
    inside = sorted(
        (s for s in viewer.segments.values() if s.starts is not None and starts <= s.starts < joined_break.ends),
        key=lambda s: s.starts or starts,
    )
    first_ad = next((n for n, s in enumerate(inside) if s.is_ad), None)
    assert first_ad is not None and first_ad > 0, (smoke.mid[:8], [(s.name, s.is_ad) for s in inside][:12])
    stitched = {key for key in smoke.stitched() if key[:2] == (joined.sid, joined.evid)}
    assert stitched and stitched <= smoke.fired(), (stitched, stitched - smoke.fired())
    for each in found:
        assert not [e for e in each.event_types if e.startswith("ERROR")], (each.evid, each.event_types)
        assert each.aired is not None, (each.evid, each.requests)
        assert float((each.avail or {}).get("filledDuration", 0)) > 0, (each.evid, each.avail)


def test_a_retrieved_breaks_winners_are_ready_at_the_break(smoke: Smoke) -> None:
    """The retrieval has MediaTailor import and transcode the pod's creatives ahead of the break (P0-02 §4), so no
    winner of a retrieved break is skipped as not ready: prefetch removes first-sight loss."""
    late = [
        (b.sid[:8], b.evid, ad.crid, ad.reason)
        for b in retrieved(smoke)
        for ad in avail_ads(b.avail or {}, smoke.creatives)[1]
        if ad.sequence and ad.reason in NOT_READY
    ]
    assert not late, f"{late}\n{smoke.report()}"


def test_no_requested_break_is_ever_empty(smoke: Smoke) -> None:
    """Never empty: every ad server request MediaTailor made for a break got a pod (no ``EMPTY_VAST_RESPONSE``, a
    module line with winners), unless the PDS legitimately had nothing eligible: ``/select`` answered ``empty`` or
    ``short_avail`` with the ``/policy`` patch applied."""
    empty = []
    for found in smoke.breaks.values():
        for auction in found.auctions():
            request = auction.request.request_id
            answered_empty = any(
                e.get("eventType") == "EMPTY_VAST_RESPONSE" and e.get("requestId") == request for e in found.events
            )
            line = auction.lines[-1] if auction.lines else {}
            if not answered_empty and line.get("winners"):
                continue
            reason = auction.select[-1].get("reason") if auction.select else None
            legitimate = reason in NOTHING_ELIGIBLE and line.get("policy_fallback") is None and "error" not in line
            if not legitimate:
                empty.append((found.sid, found.evid, auction.request, found.event_types, line, reason))
    assert not empty, empty


def test_a_fallback_is_safe_and_still_a_pod_and_there_is_none(smoke: Smoke) -> None:
    """Fallbacks: a break served without the ``/policy`` patch says ``safe`` and has no third-party ad, a break whose
    pod is the module's (``SELECT_FALLBACK``, ``HOUSE_CONFIG``) has house ads only, and either still has a pod. On the
    sidecar there is none: the count must be 0."""
    lines = [line for found in smoke.breaks.values() for line in found.pod_lines]
    fallbacks = [
        line for line in lines if line.get("policy_fallback") or str(line.get("select", "")).startswith(SELECT_FALLBACK)
    ]
    for line in fallbacks:
        winners = [str(winner) for winner in line.get("winners") or []]
        assert winners, f"a fallback without a pod: {line}"
        if line.get("policy_fallback"):
            assert line.get("safe") is True, f"served without the patch but not SAFE: {line}"
            assert all(w.split(":", 1)[0] in OWN_SEATS for w in winners), f"a third party without the patch: {line}"
        if str(line.get("select", "")).startswith(SELECT_FALLBACK):
            assert all(w.split(":", 1)[0] in OWN_SEATS for w in winners), f"a paid ad in the house fallback: {line}"
    assert not fallbacks, f"{len(fallbacks)} of {len(lines)} auctions fell back on the sidecar build: {fallbacks}"


def test_the_stitched_order_is_the_pods_sequence(smoke: Smoke) -> None:
    """MediaTailor stitches every pod the module rendered in ``sequence`` order: each winner in its position, a
    standalone in the position of a winner it skipped, and nothing else; once every creative is known, a 120 s pod plays
    whole. A known ADS log gap is left out: without its request or its ``FILLED_AVAIL`` nothing can be compared."""
    gaps = smoke.gaps()
    problems = {
        key: fill.problems
        for key, b in smoke.breaks.items()
        if key not in gaps and (fill := b.fill(smoke.creatives)).problems
    }
    assert not problems, f"{problems}\n{smoke.report()}"
    whole = [
        b
        for b in smoke.breaks.values()
        if len(pod := b.rendered()[0]) == 4
        and [smoke.creatives.crid(ad) for ad in (b.avail or {}).get("creativeAds") or []] == pod
    ]
    assert whole, f"no 120 s pod was stitched whole\n{smoke.report()}"


def first_sight(smoke: Smoke) -> None:
    """Skip, saying why, when no winner was new to MediaTailor at its break.

    With prefetch every session's pod of a break is the one the retrievals made ready, the late joiners' real-time
    requests included (:func:`test_a_retrieved_breaks_winners_are_ready_at_the_break`). First sight on a real-time
    request is covered by P0-02 section 4 and by ADR-0033's evidence, where a real-time first sight on the same stage
    (its v3.0.0 configuration's house ads) skipped both creatives. Without prefetch, an earlier run on the stage may
    have imported them (MediaTailor keys its import on the creative id, which carries the stage's CDN host, never on the
    run)."""
    skipped_winners = [
        ad for b in smoke.breaks.values() for ad in avail_ads(b.avail or {}, smoke.creatives)[1] if ad.sequence
    ]
    if not [ad for ad in skipped_winners if ad.reason in FIRST_SIGHT]:
        if any(r.prefetch for b in smoke.breaks.values() for r in b.requests):
            pytest.skip(
                "no winner was new to MediaTailor at its break: the retrievals imported every creative of the pod "
                "ahead of the break, late joiners included"
            )
        pytest.skip("no winner was new to MediaTailor: an earlier run on this stage imported every creative")


def test_a_first_sight_winner_is_replaced_in_its_position_by_a_standalone(smoke: Smoke) -> None:
    """A winner MediaTailor had not imported yet (``NEW_CREATIVE`` for the first session to see it,
    ``IMPORT_IN_PROGRESS`` for the next ones) is replaced in its position by a standalone it had imported."""
    first_sight(smoke)
    replaced = [(b.sid, b.evid, swap) for b in smoke.breaks.values() for swap in b.fill(smoke.creatives).replaced]
    assert replaced, f"no standalone replaced a skipped winner\n{smoke.report()}"
    assert {reason for _, _, (_, reason, _) in replaced} <= FIRST_SIGHT, replaced


def test_a_partial_pod_is_kept_and_the_break_slate_plays_the_rest(smoke: Smoke) -> None:
    """At ``PersonalizationThresholdSeconds`` 121 MediaTailor keeps a partial pod: the viewer gets its ads, then the
    channel's break slate for the rest of the avail (the first-sight winners leave it partial)."""
    first_sight(smoke)
    partial = [
        b
        for b in smoke.breaks.values()
        if (avail := b.avail) and 0 < float(avail["filledDuration"]) < float(avail["originAvailDuration"]) - 1
    ]
    assert partial, f"no partial pod\n{smoke.report()}"
    viewers = {viewer.sid: viewer for viewer in smoke.viewers}
    for found in partial:
        avail = found.avail or {}
        filled, origin = float(avail["filledDuration"]), float(avail["originAvailDuration"])
        reasons = {str(ad.get("skippedReason")) for ad in avail.get("skippedAds") or []}
        assert "LEFTOVER_AVAIL_EXCEEDED_THRESHOLD" not in reasons, avail
        viewer = viewers[found.sid]
        filled_at = timestamp(found.filled) if found.filled else None
        ads, slate = viewer.break_seconds(splice_near(viewer, filled_at), origin)
        assert abs(ads - filled) < SEGMENT_SLACK_S, (found.sid, found.evid, ads, filled)
        assert slate > origin - filled - SEGMENT_SLACK_S, (found.sid, found.evid, ads, slate, filled, origin)


def splice_near(viewer: Viewer, moment: datetime | None) -> datetime:
    """The start of the viewer's ad marker nearest ``moment``."""
    assert moment is not None and viewer.splices, (viewer.sid, moment)
    return min((s.starts for s in viewer.splices.values()), key=lambda starts: abs((starts - moment).total_seconds()))


def test_the_decision_records_carry_mediatailors_session_id(smoke: Smoke) -> None:
    """``X-Fast-Sid`` is MediaTailor's session id: every break MediaTailor asked about has the PDS's policy and select
    records and the module's line under the viewer's session id, its ``evid`` and the stable sibling. A known ADS log
    gap counts as asked: the PDS was."""
    gaps = smoke.gaps()
    for viewer in smoke.viewers:
        found = smoke.of(viewer.sid)
        asked = {b.evid for b in found if "MAKING_ADS_REQUEST" in b.event_types or (b.sid, b.evid) in gaps}
        assert asked, viewer.sid
        for kind in ("policy", "select", "pod_lines"):
            assert {b.evid for b in found if getattr(b, kind)} == asked, (viewer.sid, kind)
        for each in found:
            assert {r["pc"] for r in each.policy + each.select + each.pod_lines} == {smoke.sibling}, each.evid
            assert {(r.get("ip_rule"), r.get("devicetype"), r.get("mode")) for r in each.policy} == {
                ("ok", 3, "CONTEXTUAL")
            }


def test_no_client_header_reaches_the_simulator(smoke: Smoke) -> None:
    """Every bid request the simulator got while the viewers' auctions ran: no ``X-Fast-*`` header, no User-Agent,
    and one X-Forwarded-For entry (its load balancer's: Prebid Server forwards none); one per auction (a retrieval's
    or a real-time request's) whose applied patch added it."""
    assert smoke.simulator, "the simulator logged no bid request"
    for line in smoke.simulator:
        assert not [name for name in line["header_names"] if name.startswith("x-fast")], line
        assert "user-agent" not in line["header_names"], line
        assert line["xff_entries"] == 1, line
    assert {p["sid"] for p in smoke.policies} <= smoke.sids, "another session's auction ran in the window"
    called = [
        auction
        for b in smoke.breaks.values()
        for auction in b.auctions()
        if any(line.get("policy_fallback") is None for line in auction.lines)
        and any(SIMULATOR in p.get("bidders", []) for p in auction.policy)
    ]
    gaps = smoke.gaps()  # their unlogged auctions: the policy records no logged request holds
    unlogged = [
        record
        for b in smoke.breaks.values()
        if (b.sid, b.evid) in gaps
        for record in b.policy
        if not any(record in auction.policy for auction in b.auctions()) and SIMULATOR in record.get("bidders", [])
    ]
    assert len(smoke.simulator) == len(called) + len(unlogged), (
        "one simulator request per auction whose applied patch added it",
        len(called),
        len(unlogged),
    )


def test_the_simulator_gets_the_viewers_address_masked_and_never_whole(smoke: Smoke) -> None:
    """The PDS computes ``device.ip`` from the X-Forwarded-For chain (the viewer-IP rule:
    ``core/tests/pds/test_pds_xff.py``, ``core/tests/pds/test_pds_policy.py::test_device_fields_by_address_family``
    and the ``contracts/fixtures/pds/policy/contextual-spoofed-xff.json`` contract fixture), and Prebid Server masks it
    for every bidder but the own book
    (``tests/pbs/test_contract.py::test_the_policy_patch_reaches_the_request_and_ids_stay_with_the_own_book``). So the
    simulator gets the viewers' egress address cut to :data:`harness_pods.MASK_BITS`, and the full address is nowhere
    in its log (``test_prebid_guidance.py::test_o5_logs_the_device_address_as_received_and_never_a_header_value``)."""
    expected = masked(smoke.egress)
    field, other = ("device_ip", "device_ipv6") if smoke.egress.version == 4 else ("device_ipv6", "device_ip")
    assert smoke.simulator, "the simulator logged no bid request"
    for line in smoke.simulator:
        assert line[field] is not None and ip_address(line[field]) == expected, (line, str(expected))
        assert line[other] is None, line
    if expected == smoke.egress:
        pytest.skip(f"the egress {smoke.egress} is its own masked address: its absence cannot be told apart")
    assert smoke.whole_address_events == 0, "the viewers' full address is in the simulator's log"


def wait_for_deliveries(smoke: Smoke) -> None:
    """When a stitched ad or a firing has no ``pds.delivery`` record yet (the subscription was cut, or the ADS log left
    the beacon out), wait for the CDN-log runs (:data:`CDN_COUNT_WAIT` after the last firing) to count it."""
    if not (smoke.stitched() | smoke.fired()) - smoke.counted() or smoke.sources is None:
        return
    client = smoke.stage.session.client("logs", region_name=smoke.stage.region)
    since = Logs(client, start_ms=milliseconds(smoke.joined - timedelta(minutes=1)))
    last = max((b.at for b in smoke.beacons), default=datetime.now(UTC))
    while (missing := (smoke.stitched() | smoke.fired()) - smoke.counted()) and datetime.now(
        UTC
    ) < last + CDN_COUNT_WAIT:
        time.sleep(30)
        smoke.deliveries = objects(since, smoke.sources.delivery, record_pattern(DELIVERY_RECORD, smoke.sids))
    print(f"delivery records: {len(smoke.deliveries)}; not counted yet: {len(missing)}")


def test_delivery_is_counted_from_mediatailors_beacon_firings(smoke: Smoke) -> None:
    """Every stitched ad's tracker fired and is a ``pds.delivery`` record, with one impression per ad, counted from the
    ADS log's ``BEACON_FIRED`` or, where that log left it out, from the CDN's log (W-2 1.32); the direct lines' ads
    are credited to their reservations (late when only the CDN's log counted them, past the hold), the others to
    none. With ``--cut-ads-delivery`` the firings during the cut are counted from the CDN's log, and the others from
    the ADS log."""
    wait_for_deliveries(smoke)
    counted, fired = smoke.counted(), smoke.fired()
    assert smoke.stitched() <= counted, f"stitched ads without a tracker hit: {smoke.stitched() - counted}"
    assert fired <= counted, fired - counted
    first: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key in counted:
        records = [d for d in smoke.deliveries if (d["sid"], d["evid"], d["crid"]) == key]
        [first[key]] = [d for d in records if d["impression"]]
        line = smoke.lines.get(key[2])
        record = first[key]
        # A firing that only the CDN's log counted is read 10 to 25 minutes later, past the reservation's 10-minute
        # hold, so its line ad is delivered late (ADR-0022).
        credits = {Credit.UNRESERVED}
        if line:
            credits = {Credit.DELIVERED, Credit.LATE} if record["source"] == Source.CDN_LOG else {Credit.DELIVERED}
        assert record.get("line") == line and record["credit"] in credits, record
        assert record["pc"] == smoke.sibling, record
        if key not in fired:
            assert record["source"] == Source.CDN_LOG, f"counted but never fired: {record}"
    if smoke.cut is not None:
        cut, back = smoke.cut
        sources = {
            (b.sid, b.evid, b.crid): first[(b.sid, b.evid, b.crid)]["source"]
            for b in smoke.beacons
            if not (cut - CUT_MARGIN < b.at < cut + CUT_MARGIN or back - CUT_MARGIN < b.at < back + CUT_MARGIN)
        }
        inside = {key for b in smoke.beacons if cut < b.at < back and (key := (b.sid, b.evid, b.crid)) in sources}
        assert inside, f"no firing during the cut {clock(cut)}-{clock(back)}"
        wrong = {key: source for key, source in sources.items() if (key in inside) != (source == Source.CDN_LOG)}
        assert not wrong, f"counted from the wrong log for the cut {clock(cut)}-{clock(back)}: {wrong}"
        print(f"cut {clock(cut)}-{clock(back)}: {len(inside)} firings counted from the CDN's log")
    cdn_only = [key for key, record in first.items() if record["source"] == Source.CDN_LOG]
    print(f"impressions: {len(counted)}, of which the CDN's log counted {len(cdn_only)}")
    assert {crid for *_, crid in fired} >= set(smoke.lines), (smoke.lines, fired)


def test_the_ads_log_gaps_are_known_and_rare(smoke: Smoke) -> None:
    """W-2 1.32: the vended ADS log can leave out events of a break MediaTailor filled. The checks above skip such a
    known gap (decided by the PDS, played by our tracker); this one reports them and fails when more than
    :data:`harness_pods.GAP_SESSION_SHARE` of the sessions have one, from :data:`harness_pods.GAP_MIN_SESSIONS`
    sessions on: that is not the known defect's rate (:func:`harness_pods.gap_count`)."""
    gaps = smoke.gaps()
    count = gap_count(gaps, smoke.sids)
    print(f"ADS log gaps (W-2 1.32): {len(gaps)} breaks; {count}: {sorted(gaps)}")
    assert count.rare, f"{count}: {sorted(gaps)}"


def test_prebid_server_ran_the_deployed_image(pds: Stage, smoke: Smoke) -> None:
    assert smoke.images == {DemandEdge(pds.prefix, pds.session).parameters[Parameter.IMAGE_URI.value]}


def test_the_silent_loss_detector_stays_quiet(smoke: Smoke) -> None:
    """The stable sibling's markers-without-ads-request alarm (M3-04) counted the run's ad markers and never fired."""
    alarm = names.markers_alarm(smoke.sibling)
    cloudwatch = smoke.stage.session.client("cloudwatch", region_name=smoke.stage.region)
    assert cloudwatch.describe_alarms(AlarmNames=[alarm])["MetricAlarms"], f"no alarm {alarm}"
    history = cloudwatch.describe_alarm_history(
        AlarmName=alarm, HistoryItemType="StateUpdate", StartDate=smoke.joined, EndDate=datetime.now(UTC)
    )["AlarmHistoryItems"]
    fired = [h for h in history if json.loads(h["HistoryData"])["newState"]["stateValue"] == "ALARM"]
    assert not fired, fired
    markers = cloudwatch.get_metric_statistics(
        Namespace=names.METRICS_NAMESPACE,
        MetricName=names.MARKER_METRICS["AD_MARKER_FOUND"],
        Dimensions=[{"Name": "ConfigurationName", "Value": smoke.sibling}],
        StartTime=smoke.joined,
        EndTime=datetime.now(UTC),
        Period=60,
        Statistics=["Sum"],
    )["Datapoints"]
    assert sum(point["Sum"] for point in markers) > 0, "the detector counted no ad marker"


def test_crawl_covers_the_enriched_arms_breaks(pds: Stage, smoke: Smoke) -> None:
    """With the pattern's catalog context published, at least 99% of the enriched arm's breaks (``contributed.enr``
    1, with a UPID) carry it (``crawl`` contributed and ``contributed.ctx`` is not ``none``), as ``v_crawl_kpis``
    counts them."""
    if pds.status(Module.CRAWL) is None:
        pytest.skip(f"stage {pds.prefix} has no Crawl stack (modules.crawl is off)")
    policies = [record for found in smoke.breaks.values() for record in found.policy]
    enriched = [r for r in policies if r.get("contributed", {}).get(crawl.RECORD_ARM) == 1 and r.get("upid")]
    covered = [
        r
        for r in enriched
        if crawl.CONTRIBUTOR in r.get("contributors", [])
        and r["contributed"].get(crawl.RECORD_SOURCE, crawl.Source.NONE) != crawl.Source.NONE
    ]
    assert enriched, f"no break on the enriched arm: {[(r['sid'], r.get('contributed')) for r in policies]}"
    assert len(covered) / len(enriched) >= 0.99, (len(covered), len(enriched))
