"""Prefetch on a deployed stage (M3-06, DESIGN §10): prefetched breaks lose no creative to first sight, traffic
shaping spreads the ADS herd ahead of the break, and the PDS, the trackers and the reservations work on retrieval.

Runs only when the Release stack has the prefetcher (output ``PrefetchFunction``); the v3.0.0 leg skips it. It takes
about 40 minutes with the stage's first channel on air (BASIC), and is meant for a throwaway stage
(``STAGE=fastci``): it starts and stops the channel, publishes a plan of its own and writes prefetch schedules on the
siblings (with the operator's credentials, which the stage's release guard does not cover).

The run:

1. Starts the channel and waits for Prebid Server (the census gate scales it from zero, then the warm-up replays).
2. Publishes the e2e test pattern :data:`PROGRAMS` times, one 60.06 s break each (130 s apart), and runs the
   prefetcher at once, which schedules every break on both general siblings.
3. Sets the arms (breaks are B1, B2, … in airing order):
   - B1 on ``-p-a`` is real-time: its schedule is deleted (and deleted again if the prefetcher recreates it);
   - B3 and B4 on ``-p-a`` are prefetched without traffic shaping: their schedules are replaced by copies without it;
   - every other break of both siblings is prefetched with the stage's shaping window.
   B1 on ``-p-b`` is the first time MediaTailor sees the demand's creatives (their URLs are the stage's own), and
   it sees them through a prefetch retrieval, about 140 s ahead. ``-p-a``'s real-time requests at B1 come after
   retrievals of both siblings (B2's on ``-p-a`` opens before B1 airs), so they find the creatives prepared too.
4. Watches each general sibling with :data:`SESSIONS` SSAI sessions through the CDN, which poll their media playlist
   every 4 s and request each ad segment's beacon redirect (not its media), so the impression beacons fire.
5. Reads the siblings' vended ADS logs (``MAKING_ADS_REQUEST``, ``FILLED_AVAIL``) and the PDS's decision and
   delivery records, and prints the measurements.
"""

import json
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import httpx
import pytest
from _stack import Module, Plane, StackError, Stage, stage_id
from botocore.exceptions import ClientError
from harness import BREAK_POLICY, Channel, Tools, until, wait_for
from harness_prefetch import Avail, Request, avails, requests, spread

from fast_channels.core.monetization.prefetch import event_id_of

if TYPE_CHECKING:
    from fast_channels.functions.tools.library_store import Asset

pytestmark = pytest.mark.e2e

PROGRAMS: Final = 8
SESSIONS: Final = 40
"""Sessions per general sibling."""
POLL_S: Final = 4.0
LEAD: Final = timedelta(minutes=3, seconds=30)
WARM_UP: Final = timedelta(minutes=3)
"""After two Prebid Server tasks run: their warm-up replay (150 s) and target health."""
LOG_DELAY: Final = timedelta(minutes=5)
"""Vended logs and decision records reach their groups within minutes."""
SHAPED_MIN_WIDTH_S: Final = 0.5
"""A shaped break's p05-p95 arrival width is at least this share of the shaping window."""
HERD_MAX_WIDTH_S: Final = 12.0
"""Unshaped requests come with the sessions' playlist polls (every 4 s): within a few polls of the trigger."""


def module_outputs(stage: Stage, stack: str) -> dict[str, str]:
    try:
        [described] = stage.cloudformation.describe_stacks(StackName=f"{stage_id(stage.prefix)}-{stack}")["Stacks"]
    except ClientError:
        return {}
    return {o["OutputKey"]: o["OutputValue"] for o in described.get("Outputs", [])}


@pytest.fixture(scope="module")
def release(stage: Stage) -> dict[str, str]:
    """The Release stack's outputs; skips without the prefetcher."""
    try:
        outputs = stage.outputs(Module.RELEASE)
    except StackError:
        pytest.skip(f"stage {stage.prefix} has no Release stack (modules.release is off)")
    if "PrefetchFunction" not in outputs:
        pytest.skip(f"stage {stage.prefix} has no prefetcher (modules.release.prefetch is off)")
    return outputs


def insights(stage: Stage, group: str, start: datetime, end: datetime, query: str) -> list[dict[str, Any]]:
    """The ``@message`` JSON of each event a Logs Insights query returns (at most 10,000)."""
    logs = stage.session.client("logs", region_name=stage.region)
    query_id = logs.start_query(
        logGroupName=group,
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=query,
        limit=10_000,
    )["queryId"]
    while (result := logs.get_query_results(queryId=query_id))["status"] in {"Scheduled", "Running"}:
        time.sleep(2)
    assert result["status"] == "Complete", result["status"]
    rows = [{f["field"]: f["value"] for f in row} for row in result["results"]]
    return [json.loads(row["@message"]) for row in rows if "@message" in row]


def running_tasks(stage: Stage) -> int:
    """Running Prebid Server tasks of the stage's guidance service."""
    ecs = stage.session.client("ecs", region_name=stage.region)
    count = 0
    for cluster in ecs.list_clusters()["clusterArns"]:
        if f"{stage.prefix}-pbs" in cluster:
            for service in ecs.list_services(cluster=cluster)["serviceArns"]:
                [described] = ecs.describe_services(cluster=cluster, services=[service])["services"]
                count += described["runningCount"]
    return count


def playback_url(stage: Stage, cdn: str, configuration: str) -> str:
    prefix = stage.session.client("mediatailor", region_name=stage.region).get_playback_configuration(
        Name=configuration
    )["HlsConfiguration"]["ManifestEndpointPrefix"]
    return f"{cdn}{prefix.split('.amazonaws.com', 1)[1]}hls.m3u8"


def watch(url: str, until_at: datetime, seen: Counter[str], run_id: str) -> None:
    """One SSAI session: its first media playlist every :data:`POLL_S`, and each new ad segment's beacon redirect."""
    fired: set[str] = set()
    with httpx.Client(timeout=10, headers={"User-Agent": f"fast-e2e-prefetch/{run_id}"}) as client:
        main = client.get(url, follow_redirects=True)
        if main.status_code != 200:
            seen[f"master {main.status_code}"] += 1
            return
        media = next(line for line in main.text.splitlines() if line and not line.startswith("#"))
        media_url = str(httpx.URL(str(main.url)).join(media))
        while datetime.now(UTC) < until_at:
            try:
                playlist = client.get(media_url, follow_redirects=True)
                seen[f"playlist {playlist.status_code}"] += 1
                for line in playlist.text.splitlines():
                    if line and not line.startswith("#") and "/segment/" in line and line not in fired:
                        fired.add(line)
                        beacon = client.get(str(httpx.URL(str(playlist.url)).join(line)), follow_redirects=False)
                        seen[f"beacon {beacon.status_code}"] += 1
            except httpx.HTTPError as error:
                seen[type(error).__name__] += 1
            time.sleep(POLL_S)


def schedules(stage: Stage, configuration: str) -> dict[int, dict[str, Any]]:
    """The prefetcher's active schedules of ``configuration`` by event id."""
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    items = [
        item
        for page in mediatailor.get_paginator("list_prefetch_schedules").paginate(
            PlaybackConfigurationName=configuration, ScheduleType="SINGLE"
        )
        for item in page.get("Items", [])
    ]
    return {e: dict(item) for item in items if (e := event_id_of(item["Name"])) is not None}


def without_shaping(stage: Stage, item: Mapping[str, Any]) -> None:
    """Replace a schedule by a copy without traffic shaping (same name, times and variables)."""
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    retrieval = {k: v for k, v in item["Retrieval"].items() if not k.startswith("TrafficShaping")}
    mediatailor.delete_prefetch_schedule(Name=item["Name"], PlaybackConfigurationName=item["PlaybackConfigurationName"])
    request: dict[str, Any] = {
        "Name": item["Name"],
        "PlaybackConfigurationName": item["PlaybackConfigurationName"],
        "ScheduleType": "SINGLE",
        "Retrieval": retrieval,
        "Consumption": item["Consumption"],
        "Tags": item.get("Tags") or {},
    }
    mediatailor.create_prefetch_schedule(**request)


def keep_deleted(stage: Stage, configuration: str, name: str, until_at: datetime) -> threading.Thread:
    """Delete ``name`` on ``configuration`` every 15 s until ``until_at`` (the prefetcher may create it again)."""
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)

    def run() -> None:
        while datetime.now(UTC) < until_at:
            try:
                mediatailor.delete_prefetch_schedule(Name=name, PlaybackConfigurationName=configuration)
            except ClientError as error:
                if error.response["Error"]["Code"] not in {"NotFoundException", "ResourceNotFoundException"}:
                    raise
            time.sleep(15)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.fixture(scope="module")
def on_air(tools: Tools, channel: Channel, stage: Stage) -> Iterator[datetime]:
    """The channel started, with Prebid Server up and warm; stopped at the end. Only this run's breaks may air: an
    earlier run's upcoming programs come off the schedule first, while the channel is stopped (Channel Assembly
    refuses to delete a program that is about to play)."""
    now = datetime.now(UTC)
    guide = tools("schedule___get_schedule", channel=channel.name)["programs"]
    if upcoming := [p["name"] for p in guide if datetime.fromisoformat(p["start"]) > now]:
        tools("schedule___remove_programs", channel=channel.name, programs=upcoming)
    tools("channels___start_channel", channel=channel.name)
    try:
        wait_for(lambda: running_tasks(stage) >= 2 or None, timeout=900, interval=15, what="two Prebid Server tasks")
        time.sleep(WARM_UP.total_seconds())
        yield datetime.now(UTC)
    finally:
        tools("channels___stop_channel", channel=channel.name)


def test_prefetch_removes_first_sight_and_spreads_the_ads_herd(
    stage: Stage,
    tools: Tools,
    channel: Channel,
    ingested: Asset,
    release: dict[str, str],
    on_air: datetime,
    run_id: str,
) -> None:
    siblings = json.loads(release["Siblings"])
    a, b = f"{channel.name}-p-a", f"{channel.name}-p-b"
    assert {a, b} <= set(siblings), siblings
    start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
    plan = tools(
        "schedule___plan",
        channel=channel.name,
        picks=[ingested.asset_id] * PROGRAMS,
        start=start.isoformat(),
        break_policy=BREAK_POLICY,
    )
    assert plan["publishable"], plan["violations"]
    tools("schedule___publish", plan_id=plan["plan_id"])
    lam = stage.session.client("lambda", region_name=stage.region)
    totals = json.loads(lam.invoke(FunctionName=release["PrefetchFunction"])["Payload"].read())
    print("prefetcher", totals)

    on_b = schedules(stage, b)
    breaks = {
        e: datetime.fromisoformat(str(i["Consumption"]["StartTime"])) + timedelta(seconds=20) for e, i in on_b.items()
    }
    order = sorted(breaks, key=breaks.__getitem__)
    assert len(order) >= PROGRAMS, f"{len(order)} schedules on {b}: {totals}"
    first, unshaped = order[0], order[2:4]
    on_a = schedules(stage, a)
    keeper = keep_deleted(stage, a, f"pf-{first}", breaks[first])
    for event_id in unshaped:
        without_shaping(stage, on_a[event_id])
    last_end = breaks[order[-1]] + timedelta(seconds=60.06)

    cdn = stage.output(Plane.MEDIA, "CdnBaseUrl").rstrip("/")
    urls = {name: playback_url(stage, cdn, name) for name in (a, b)}
    seen: Counter[str] = Counter()
    # The sessions join now, on filler slate: a retrieval happens on a session's playlist requests inside the window.
    assert datetime.now(UTC) < breaks[first] - timedelta(seconds=150), "too late for the first break's retrieval"
    sessions = [
        threading.Thread(target=watch, args=(urls[name], last_end + timedelta(seconds=40), seen, run_id), daemon=True)
        for name in (a, b)
        for _ in range(SESSIONS)
    ]
    for thread in sessions:
        thread.start()
        time.sleep(0.05)
    for thread in sessions:
        thread.join()
    keeper.join(timeout=1)
    print("sessions", dict(seen))

    window_end = datetime.now(UTC) + LOG_DELAY
    until(window_end)
    since = breaks[first] - timedelta(minutes=5)
    events = insights(
        stage,
        release["SiblingLogGroup"],
        since,
        window_end,
        "fields @message | filter eventType = 'MAKING_ADS_REQUEST' or eventType = 'FILLED_AVAIL'",
    )
    events = [e for e in events if e.get("originId") in {a, b}]
    made, filled = requests(events), avails(events)
    report = measure(made, filled, breaks, a=a, b=b, unshaped=set(unshaped), first=first)
    print("prefetch report", json.dumps(report, indent=1, default=str))

    shaping = int(
        json.loads(
            lam.get_function_configuration(FunctionName=release["PrefetchFunction"])["Environment"]["Variables"][
                "PREFETCH_POLICY"
            ]
        )["shaping_window_s"]
    )
    prefetched_avails = [x for x in filled if x.prefetched]
    assert prefetched_avails, "no avail was filled from a prefetch"
    assert sum(len(x.first_sight) for x in prefetched_avails) == 0, report["first_sight"]
    first_break = report["arms"]["b first break (first sight, prefetched)"]
    assert first_break["avails"] > 0
    assert first_break["filled_s"] >= 0.99 * first_break["avails"] * 60.06, first_break
    shaped = [s for s in report["spreads"].values() if s["arm"] == "shaped"]
    herded = [s for s in report["spreads"].values() if s["arm"] in {"real-time", "unshaped"}]
    assert shaped and herded
    assert min(s["width"] for s in shaped) >= SHAPED_MIN_WIDTH_S * shaping, report["spreads"]
    assert max(s["width"] for s in herded) <= HERD_MAX_WIDTH_S, report["spreads"]
    check_retrieval_requests(events, breaks)
    check_pds(stage, since, window_end, breaks, report)


def measure(
    made: list[Request],
    filled: list[Avail],
    breaks: Mapping[int, datetime],
    *,
    a: str,
    b: str,
    unshaped: set[int],
    first: int,
) -> dict[str, Any]:
    """Arrival spreads by (sibling, arm, break), first-sight skips by arm, and fill."""

    def arm(configuration: str, event_id: int | None, prefetched: bool) -> str:
        if not prefetched:
            return "real-time"
        return "unshaped" if configuration == a and event_id in unshaped else "shaped"

    arrivals: dict[tuple[str, str, int], list[datetime]] = defaultdict(list)
    for r in made:
        if r.event_id is not None and r.event_id in breaks:
            arrivals[(r.configuration, arm(r.configuration, r.event_id, r.prefetched), r.event_id)].append(r.at)
    spreads: dict[str, dict[str, Any]] = {}
    for (configuration, kind, event_id), moments in sorted(arrivals.items()):
        measured = spread(moments, start=breaks[event_id])
        if measured is not None:
            spreads[f"{configuration} {event_id}"] = {
                "arm": kind,
                "count": measured.count,
                "first": measured.first,
                "p05": measured.p05,
                "p50": measured.p50,
                "p95": measured.p95,
                "last": measured.last,
                "width": measured.width,
            }
    by_arm: dict[str, dict[str, Any]] = defaultdict(lambda: {"avails": 0, "first_sight": 0, "filled_s": 0.0})
    for x in filled:
        label = arm(x.configuration, x.event_id, x.prefetched)
        if x.configuration == b and x.event_id == first:
            label = "b first break (first sight, prefetched)" if x.prefetched else "b first break (real-time!)"
        elif x.configuration == a and x.event_id == first:
            label = "a first break (real-time)"
        by_arm[label]["avails"] += 1
        by_arm[label]["first_sight"] += len(x.first_sight)
        by_arm[label]["filled_s"] += x.filled_s
    return {
        "spreads": spreads,
        "arms": dict(by_arm),
        "first_sight": Counter(r for x in filled for r in x.first_sight),
    }


def check_retrieval_requests(events: list[dict[str, Any]], breaks: Mapping[int, datetime]) -> None:
    """R-S1 reached the ad decision server: every retrieval's URL carries its break's event id, its UPID and its
    (rounded) duration."""
    for event in events:
        if event.get("eventType") != "MAKING_ADS_REQUEST" or not event.get("prefetchScheduleName"):
            continue
        query = httpx.URL(event["adsRequestUrl"]).params
        event_id = event_id_of(event["prefetchScheduleName"])
        assert event_id in breaks
        assert (query.get("evid"), bool(query.get("upid")), query.get("dur")) in {
            (str(event_id), True, "60000"),
            (str(event_id), True, "60060"),
        }, event["adsRequestUrl"]


def check_pds(
    stage: Stage, since: datetime, end: datetime, breaks: Mapping[int, datetime], report: dict[str, Any]
) -> None:
    """The PDS answered every break with its session id, event id and UPID (the ``session_break`` and ``title``
    cache keys resolve on retrieval), and our tracker credited prefetched breaks."""
    pds = module_outputs(stage, "Pds")
    lam = stage.session.client("lambda", region_name=stage.region)

    def group(key: str) -> str:
        config = lam.get_function_configuration(FunctionName=pds[key])
        return str(config.get("LoggingConfig", {}).get("LogGroup") or f"/aws/lambda/{pds[key]}")

    decisions = [
        r
        for r in insights(stage, pds["DecisionLogGroup"], since, end, "fields @message | filter record = 'pds.policy'")
        if int(r.get("evid", 0)) in breaks
    ]
    retrievals = [r for r in decisions if r["dur_ms"] == 60_000]
    assert retrievals, "no /policy call with a prefetch retrieval's duration (60,000 ms)"
    assert all(r["sid"] and r["upid"] for r in retrievals), retrievals[:3]
    delivered = [
        r
        for r in insights(
            stage, group("DeliveryFunction"), since, end, "fields @message | filter record = 'pds.delivery'"
        )
        if int(r.get("evid", 0)) in breaks
    ]
    report["pds"] = {
        "decisions": len(decisions),
        "retrievals": len(retrievals),
        "deliveries": len(delivered),
        "credits": Counter(r.get("credit") for r in delivered),
    }
    print("pds", report["pds"])
    assert delivered, "no impression of our tracker on the run's breaks"
