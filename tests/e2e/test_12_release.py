"""The release module (``modules.release``) on a deployed stage (BUILD-PLAN M3-03, M3-04).

Runs only when the stage has the Release stack (``deploy-demand`` deploys it beside the guidance); skipped
otherwise. The first tests read the siblings, their Functions and vended-log deliveries, the playback plan through
the CDN and the ledger, and invoke the drift check and the log reconciliation once; they change nothing (the
reconciliation puts one more ``UnloggedFilledSeconds`` datapoint).

With ``--releases`` the rest make releases (``harness_release``; about 50 minutes):

- the silent-loss detector alarms on ``<prefix>-lab-x``, a VOD configuration whose body template is invalid, while
  ``AdDecisionServer.Errors`` stays 0;
- with the first IaC channel on air, a release whose PRE_ADS lookup turns slow during its rollout (P0-08's failure)
  rolls back unattended: the lookup times out, which no error alarm counts, and the pointer poll stops the
  deployment on the step's latency. A stub mint that reads the pointer with ``Entity-Id`` sees the ramp's shares,
  and the candidate's viewers leave the slow tree at their next break;
- the smoke test stops a release whose lookup is slow from the start, before any viewer meets it.
"""

import hashlib
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from _stack import Module, Stage
from boto3.dynamodb.conditions import Key
from harness import Channel, Tools, until, wait_for
from harness_release import (
    SLOW_MS,
    Rig,
    Viewer,
    breaks,
    entered,
    execution_of,
    insights,
    lab_check,
    next_break,
    pointer_ids,
    publish,
    request_latency,
    task_window,
    vod_session,
    with_lookup,
)

from fast_channels.core.demand import ORIGIN_SECRET_SLOTS, Parameter, origin_rule_group, origin_secret, parameter_path
from fast_channels.core.ledger import entry_from_item
from fast_channels.core.mediatailor_logs import EXCLUDED_ADS_LOG_FIELDS, EXCLUDED_MANIFEST_LOG_FIELDS
from fast_channels.core.monetization import names
from fast_channels.core.monetization.functions import release_tag
from fast_channels.core.monetization.playback import (
    PERSONALIZATION_THRESHOLD_SECONDS,
    RELEASE_OWNER,
    ROLE_LETTERS,
    SiblingRole,
)
from fast_channels.core.monetization.release import ChannelState
from fast_channels.core.pbs_config import POD_MIN_SECONDS, POD_SECONDS
from fast_channels.core.pds.names import sidecar_log_group

pytestmark = pytest.mark.e2e

PAIR_SUFFIXES: Final = tuple(f"-p-{letter}" for letter in ROLE_LETTERS[SiblingRole.GENERAL])
"""The pair siblings, which take the ad-server route with ``modules.demand.ad_server``."""
DELIVERIES_PER_SIBLING: Final = 5
"""Ads and manifest logs to the module's log group and the lake, transcode logs to the log group."""


@pytest.fixture(scope="module")
def release(stage: Stage) -> Stage:
    if stage.status(Module.RELEASE) is None:
        pytest.skip(f"stage {stage.prefix} has no Release stack (modules.release is off, or deploy-demand not run)")
    return stage


@pytest.fixture(scope="module")
def siblings(release: Stage) -> dict[str, dict[str, Any]]:
    """GetPlaybackConfiguration of every sibling the stack names."""
    mediatailor = release.session.client("mediatailor", region_name=release.region)
    return {
        name: dict(mediatailor.get_playback_configuration(Name=name))
        for name in release.json_output(Module.RELEASE, "Siblings")
    }


@pytest.fixture(scope="module")
def ad_server_url(release: Stage) -> str | None:
    """The mock ad server's URL with ``modules.demand.ad_server: mock`` (M8-09), else None."""
    ssm = release.session.client("ssm", region_name=release.region)
    try:
        parameter = ssm.get_parameter(Name=parameter_path(release.prefix, Parameter.AD_SERVER_URL))
    except ssm.exceptions.ParameterNotFound:
        return None
    return str(parameter["Parameter"]["Value"])


def test_every_sibling_calls_its_ad_server_with_the_monetized_settings(
    siblings: dict[str, Any], ad_server_url: str | None
) -> None:
    """Every sibling POSTs to Prebid Server, but with the mock ad server (M8-09) the pair siblings GET the mock's
    ``/ads`` instead, with the origin secret; the probe stays on Prebid Server. The siblings of each channel's newest
    release have the avail suppression the code renders (ADR-0021). A sibling an older release configured (the idle
    one, the probe) keeps that release's, which the drift check compares."""
    newest: dict[str, str] = {}
    for name, configuration in siblings.items():
        channel = name.rsplit("-p-", 1)[0]
        newest[channel] = max(newest.get(channel, ""), configuration["Tags"]["fast:release"])
    for name, configuration in siblings.items():
        server = configuration["AdDecisionServerConfiguration"]
        request = server["HttpRequest"]
        if ad_server_url and name.endswith(PAIR_SUFFIXES):
            assert configuration["AdDecisionServerUrl"].startswith(f"{ad_server_url.rstrip('/')}/ads?"), name
            assert request.get("Method", "GET") == "GET" and not request.get("Body"), name
        else:
            assert request["Method"] == "POST" and request["Body"], name
        assert "X-Fast-Origin" in request["Headers"], name  # the value is never read here
        assert server["VastResponse"]["AdSequencingMode"] == "FOLLOW_AD_SEQUENCE", name
        assert configuration["PersonalizationThresholdSeconds"] == PERSONALIZATION_THRESHOLD_SECONDS, name
        if configuration["Tags"]["fast:release"] == newest[name.rsplit("-p-", 1)[0]]:
            assert configuration["AvailSuppression"] == {
                "Mode": "AFTER_LIVE_EDGE",
                "Value": "00:00:00",
                "FillPolicy": "PARTIAL_AVAIL",
            }, name
        assert configuration["Tags"]["fast:owner"] == RELEASE_OWNER, name
        roots = configuration["FunctionMapping"]  # one root per hook (PRE_SESSION, and PRE_ADS with the crawl)
        assert roots and all(r.endswith(configuration["Tags"]["fast:release"]) for r in roots.values()), (name, roots)


def test_the_mapped_functions_exist(release: Stage, siblings: dict[str, Any]) -> None:
    mediatailor = release.session.client("mediatailor", region_name=release.region)
    for configuration in siblings.values():
        for function_id in configuration["FunctionMapping"].values():
            found = mediatailor.get_function(FunctionId=function_id)
            assert found["Tags"]["fast:owner"] == RELEASE_OWNER
            for step in found.get("SequentialExecutorConfiguration", {}).get("FunctionList", []):
                mediatailor.get_function(FunctionId=step["FunctionId"])


def test_the_siblings_logs_leave_out_the_fields_that_may_hold_a_secret(release: Stage) -> None:
    logs = release.session.client("logs", region_name=release.region)
    names_ = release.json_output(Module.RELEASE, "Siblings")
    deliveries = [
        d
        for page in logs.get_paginator("describe_deliveries").paginate()
        for d in page["deliveries"]
        if d["deliverySourceName"].rsplit("-", 1)[0] in names_
    ]
    assert len(deliveries) == DELIVERIES_PER_SIBLING * len(names_)
    for delivery in deliveries:
        fields = set(delivery.get("recordFields", []))
        if delivery["deliverySourceName"].endswith("-ads"):
            assert fields and not fields & EXCLUDED_ADS_LOG_FIELDS, delivery["deliverySourceName"]
        if delivery["deliverySourceName"].endswith("-manifest"):
            assert fields and not fields & EXCLUDED_MANIFEST_LOG_FIELDS, delivery["deliverySourceName"]
    group = release.output(Module.RELEASE, "SiblingLogGroup")
    [described] = logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]
    assert described.get("kmsKeyId"), "the siblings' log group is encrypted with the module's key"


def test_the_plan_names_each_channels_stable_sibling(release: Stage, siblings: dict[str, Any]) -> None:
    ssm = release.session.client("ssm", region_name=release.region)
    cdn = ssm.get_parameter(Name=names.parameter_name(release.prefix, names.Parameter.CDN_BASE_URL))["Parameter"]
    for channel in sorted({s.rsplit("-p-", 1)[0] for s in siblings}):
        response = httpx.get(f"{cdn['Value'].rstrip('/')}/{names.plan_key(channel)}", timeout=10)
        response.raise_for_status()
        plan = response.json()
        assert plan["configuration"] in siblings, plan
        assert response.headers.get("Cache-Control") == names.PLAN_CACHE_CONTROL
        assert all(url.startswith(cdn["Value"].rstrip("/")) for url in plan["playback"].values()), plan


def test_nothing_drifted(release: Stage, siblings: dict[str, Any]) -> None:
    client = release.session.client("lambda", region_name=release.region)
    response = client.invoke(
        FunctionName=release.output(Module.RELEASE, "DriftFunction"), Payload=b'{"change_id": "e2e"}'
    )
    result = json.loads(response["Payload"].read())
    assert result == {"siblings": len(siblings), "drift": []}, result


def test_the_log_reconciler_counts_every_siblings_requests(release: Stage, siblings: dict[str, Any]) -> None:
    """One run of the hourly reconciliation, over the hour that ended 20 minutes ago. A shortfall is a MediaTailor
    log gap, which the run reports and ``<prefix>-release-log-loss`` alarms on: the test prints it and does not fail
    on it."""
    client = release.session.client("lambda", region_name=release.region)
    response = client.invoke(FunctionName=release.output(Module.RELEASE, "LogReconcilerFunction"), Payload=b"{}")
    assert "FunctionError" not in response, response["Payload"].read()
    result = json.loads(response["Payload"].read())
    if "skipped" in result:  # a stage younger than the hour reconciled (its log group is newer)
        print(f"\n{result['start']} to {result['end']}: skipped, {result['skipped']}")
        assert result["unlogged_filled_s"] == 0, result
        return
    counts = {
        name: (s["counted"]["filled_ms"] / 1000, s["logged"]["filled_ms"] / 1000)
        for name, s in result["siblings"].items()
    }
    print(f"\n{result['start']} to {result['end']}: (counted, logged) filled seconds {counts}")
    assert set(result["siblings"]) == set(siblings), result
    assert result["unlogged_filled_s"] == round(sum(s["unlogged_filled_s"] for s in result["siblings"].values()), 3)


def test_the_ledger_records_the_release(release: Stage, siblings: dict[str, Any]) -> None:
    table = release.session.resource("dynamodb", region_name=release.region).Table(f"{release.prefix}-ledger")

    def actions(chain: str) -> set[str]:
        items = table.query(KeyConditionExpression=Key("pk").eq(chain))["Items"]
        return {str(item["action"]) for item in items if "action" in item}

    assert {"release.accept"} <= actions(names.BUNDLE_CHAIN)
    for channel in {s.rsplit("-p-", 1)[0] for s in siblings}:
        assert "release.apply" in actions(names.channel_chain(channel))


# --- releases (--releases) -----------------------------------------------------------------------------------------

VIEWERS: Final = [f"e2e-viewer-{n:03d}" for n in range(200)]
"""Entity ids the stub mint reads the pointer for."""
CANDIDATE_VIEWERS: Final = 3
"""Sessions opened on the candidate when the mint first names it, as M4's players will."""
DETECTOR_SECONDS: Final = 300
ON_AIR_CONTENT_MS: Final = 80 * 60_000
"""The programs' running time (breaks come every 4 to 7 minutes, at the content's cue points)."""
BREAK_A_MINUTE: Final = {"cadence_minutes": 1, "min_gap_minutes": 0.5, "slate_gops": 30}
ROLLOUT_TIMEOUT_S: Final = 40 * 60
"""Apply, the smoke test and the soak (the pointer deploys after them)."""
ROLLBACK_TIMEOUT_S: Final = 20 * 60
"""From the lookup turning slow to the execution's end: two minutes of timed-out breaks, the poll, the rollback."""
REMAP_S: Final = 3.0
"""A remap reaches a live session at its next break in about 3 s (P0-08)."""
LOG_DELAY_S: Final = 180
JOIN_AFTER_S: Final = 20
"""How far into a break the mid-break joiner joins."""


@pytest.fixture(scope="module")
def releases(request: pytest.FixtureRequest, release: Stage) -> Stage:
    if not request.config.getoption("releases"):
        pytest.skip("makes releases on the stage: pass --releases (about 50 minutes, the first IaC channel on air)")
    return release


@pytest.fixture(scope="module")
def rig(releases: Stage) -> Iterator[Rig]:
    rig = Rig.deploy(releases)
    try:
        yield rig
    finally:
        rig.delete()


@pytest.fixture(scope="module")
def on_air(releases: Stage, tools: Tools, channel: Channel) -> Iterator[dict[str, Any]]:
    """The stage's first IaC channel (a monetized one) on air with a break a minute; stopped at the end."""
    found = tools("channels___get_channel", channel=channel.name)
    assets = tools(
        "library___find_assets", kind="content", status="READY", ladder_version=found["ladder_version"], limit=10
    )["assets"]
    assert assets, f"{channel.name}: no READY content of ladder {found['ladder_version']}"
    picks: list[str] = []
    while sum(a.get("duration_ms") or 600_000 for a in assets for p in picks if p == a["asset_id"]) < ON_AIR_CONTENT_MS:
        picks.append(assets[len(picks) % len(assets)]["asset_id"])
    start = clear_schedule(tools, channel.name)
    plan = tools(
        "schedule___plan", channel=channel.name, picks=picks, start=start.isoformat(), break_policy=BREAK_A_MINUTE
    )
    assert plan["publishable"], f"plan {plan['plan_id']} has errors: {plan['violations']}"
    published = tools("schedule___publish", plan_id=plan["plan_id"])
    try:
        tools("channels___start_channel", channel=channel.name)
        until(datetime.fromisoformat(published["programs"][0]["start"]) + timedelta(seconds=30))
        wait_for(lambda: prebid_up(releases) or None, timeout=600, interval=15, what="Prebid Server's /status")
        yield published
    finally:
        tools("channels___stop_channel", channel=channel.name)


def clear_schedule(tools: Tools, channel: str) -> datetime:
    """Take the programs that have not started off ``channel`` (an earlier run's) and return when a new block can
    start: in 3 minutes, or when the program in progress ends."""
    now = datetime.now(UTC)
    programs = tools("schedule___get_schedule", channel=channel)["programs"]
    future = [p["name"] for p in programs if datetime.fromisoformat(p["start"]) > now]
    if future:
        tools("schedule___remove_programs", channel=channel, programs=future)
    ends = [datetime.fromisoformat(p["end"]) for p in programs if datetime.fromisoformat(p["start"]) <= now]
    return max([now + timedelta(minutes=3), *(e + timedelta(seconds=5) for e in ends)]).replace(microsecond=0)


def prebid_up(stage: Stage) -> bool:
    """Prebid Server answers ``/status`` through the guidance's CloudFront (the census starts it with a channel)."""
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    [sibling, *_] = stage.json_output(Module.RELEASE, "Siblings")
    ads = urlsplit(mediatailor.get_playback_configuration(Name=sibling)["AdDecisionServerUrl"])
    return httpx.get(f"{ads.scheme}://{ads.netloc}/status", timeout=10).status_code == 200


def channel_state(stage: Stage, channel: str) -> ChannelState:
    table = stage.session.resource("dynamodb", region_name=stage.region).Table(
        stage.output(Module.RELEASE, "StateTableName")
    )
    item = table.get_item(Key={"pk": f"CHANNEL#{channel}"}, ConsistentRead=True)["Item"]
    return ChannelState.model_validate_json(str(item["doc"]))


def ledgered(stage: Stage, chain: str, since: float) -> list[Any]:
    table = stage.session.resource("dynamodb", region_name=stage.region).Table(f"{stage.prefix}-ledger")
    items = table.query(KeyConditionExpression=Key("pk").eq(chain))["Items"]
    entries = [entry_from_item(i) for i in items if i.get("type") == "entry"]  # type: ignore[arg-type]
    return [e for e in entries if e.at.timestamp() >= since]


def outcome(stage: Stage, execution: str) -> dict[str, Any]:
    sfn = stage.session.client("stepfunctions", region_name=stage.region)
    described = sfn.describe_execution(executionArn=execution)
    return {k: described.get(k) for k in ("status", "error", "cause")}


def plan_names(stage: Stage, channel: str, configuration: str) -> bool:
    ssm = stage.session.client("ssm", region_name=stage.region)
    cdn = ssm.get_parameter(Name=names.parameter_name(stage.prefix, names.Parameter.CDN_BASE_URL))["Parameter"]["Value"]
    response = httpx.get(
        f"{cdn.rstrip('/')}/{names.plan_key(channel)}", timeout=10, headers={"Cache-Control": "no-cache"}
    )
    return bool(response.status_code == 200 and response.json()["configuration"] == configuration)


def alarm_history(stage: Stage, since: float) -> list[tuple[str, str]]:
    """The candidate alarm's and its children's state changes since ``since`` (time, alarm: summary)."""
    cloudwatch = stage.session.client("cloudwatch", region_name=stage.region)
    items = cloudwatch.describe_alarm_history(
        HistoryItemType="StateUpdate", StartDate=datetime.fromtimestamp(since, UTC), MaxRecords=100
    )["AlarmHistoryItems"]
    return sorted(
        (f"{i['Timestamp']:%H:%M:%S}", f"{i['AlarmName']}: {i['HistorySummary']}")
        for i in items
        if i["AlarmName"].startswith(stage.prefix)
    )


def test_the_detector_alarms_on_markers_without_an_ad_request(releases: Stage, rig: Rig) -> None:
    cloudwatch = releases.session.client("cloudwatch", region_name=releases.region)
    with lab_check(releases, rig) as name, httpx.Client(timeout=15, follow_redirects=True) as http:
        alarm = f"{name}-markers-without-ads-request"
        started, state = time.time(), ""
        while time.time() - started < DETECTOR_SECONDS:
            vod_session(releases, name, http)
            state = cloudwatch.describe_alarms(AlarmNames=[alarm])["MetricAlarms"][0]["StateValue"]
            if state == "ALARM":
                break
            time.sleep(10)  # 2 markers a session: a judged minute needs 5
        elapsed = time.time() - started
    assert state == "ALARM", f"{alarm} is {state} {elapsed:.0f} s after the first session"
    query = f"filter originId = '{name}' | stats count(*) as n by eventType"
    counts = {
        r["eventType"]: int(r["n"])
        for r in insights(releases, names.log_group(releases.prefix), query, started, time.time())
    }
    assert counts.get("AD_MARKER_FOUND", 0) > 0 and "MAKING_ADS_REQUEST" not in counts, counts
    errors = cloudwatch.get_metric_statistics(
        Namespace="AWS/MediaTailor",
        MetricName="AdDecisionServer.Errors",
        Dimensions=[{"Name": "ConfigurationName", "Value": name}],
        StartTime=datetime.fromtimestamp(started - 60, UTC),
        EndTime=datetime.now(UTC),
        Period=60,
        Statistics=["Sum"],
    )["Datapoints"]
    assert sum(point["Sum"] for point in errors) == 0, errors


def test_a_release_that_breaks_during_its_rollout_rolls_back_unattended(
    releases: Stage, rig: Rig, on_air: dict[str, Any], channel: Channel, run_id: str
) -> None:
    stage, began = releases, time.time()
    before = channel_state(stage, channel.name)
    stable, candidate = before.stable, before.idle
    assert stable and candidate, before
    good = before.siblings[candidate].release
    rig.set_delay(0)
    httpx.get(f"{rig.origin}lookup/0", headers=rig.headers, timeout=10).raise_for_status()  # warm
    correlation = f"{run_id}-ramp"
    publish(stage, with_lookup(stage, f"{rig.origin}lookup", rig.headers), correlation_id=correlation)
    execution = execution_of(stage, correlation)
    appconfig = stage.session.client("appconfig", region_name=stage.region)
    application, environment, _ = pointer_ids(stage)

    def pointing() -> int | None:
        states = entered(stage, execution)
        if "" in states:
            raise AssertionError(f"{execution} ended before its pointer deployment: {outcome(stage, execution)}")
        if "PointerWait" not in states:
            return None
        items = appconfig.list_deployments(ApplicationId=application, EnvironmentId=environment)["Items"]
        return max(int(i["DeploymentNumber"]) for i in items)

    deployment = wait_for(pointing, timeout=ROLLOUT_TIMEOUT_S, interval=30, what=f"{execution}'s pointer deployment")
    shares: list[tuple[float, float, float]] = []
    viewers: list[Viewer] = []
    flipped = 0.0
    try:
        while "" not in entered(stage, execution):
            percent = appconfig.get_deployment(
                ApplicationId=application, EnvironmentId=environment, DeploymentNumber=deployment
            ).get("PercentageComplete", 0)
            named = rig.mint(channel.name, VIEWERS)
            shares.append((time.time(), float(percent), sum(n == candidate for n in named.values()) / len(VIEWERS)))
            if not viewers and shares[-1][2] > 0:
                viewers = [Viewer(candidate, stage).start() for _ in range(CANDIDATE_VIEWERS)]
            stepped = {round(p) for _, p, share in shares if 0 < p < 100 and share > 0}
            if not flipped and viewers and len(stepped) >= 2:
                rig.set_delay(SLOW_MS)
                flipped = time.time()
            assert not flipped or time.time() - flipped < ROLLBACK_TIMEOUT_S, f"no rollback: {shares[-3:]}"
            assert time.time() - began < ROLLOUT_TIMEOUT_S + ROLLBACK_TIMEOUT_S, f"no ramp seen: {shares[-3:]}"
            time.sleep(20)

        def converged() -> dict[str, str | None] | None:
            named = rig.mint(channel.name, VIEWERS)
            return named if set(named.values()) == {stable} else None

        wait_for(converged, timeout=120, interval=10, what=f"the stub mint to name {stable} for every viewer")
        until(datetime.fromtimestamp(next_break(stage, channel.name, time.time()) + 30, UTC))  # one more break
    finally:
        for viewer in viewers:
            viewer.stop()
    print(f"\nthe lookup turned slow {flipped - began:.0f} s in; alarms: {alarm_history(stage, began)}")
    assert outcome(stage, execution)["error"] == "RolledBack", outcome(stage, execution)
    described = appconfig.get_deployment(
        ApplicationId=application, EnvironmentId=environment, DeploymentNumber=deployment
    )
    assert described["State"] == "ROLLED_BACK", described["State"]
    print(f"deployment events: {[(e.get('EventType'), e.get('TriggeredBy')) for e in described['EventLog']]}")
    print(f"\nmint shares (percent, share): {[(p, s) for _, p, s in shares]}")
    assert any(0 < p < 100 and abs(share * 100 - p) <= 10 for _, p, share in shares), shares
    after = channel_state(stage, channel.name)
    assert (after.stable, after.siblings[candidate].release) == (stable, good), after
    mapping = stage.session.client("mediatailor", region_name=stage.region).get_playback_configuration(Name=candidate)
    assert all(f.endswith(release_tag(good)) for f in mapping["FunctionMapping"].values()), mapping["FunctionMapping"]
    wait_for(
        lambda: plan_names(stage, channel.name, stable) or None,
        timeout=90,
        interval=10,
        what=f"the plan naming {stable}",
    )
    [accepted] = [
        e
        for e in ledgered(stage, names.BUNDLE_CHAIN, began)
        if e.action == "release.accept" and e.detail["request"] == correlation
    ]
    actions = {
        e.action for e in ledgered(stage, names.BUNDLE_CHAIN, began) if e.correlation_id == accepted.correlation_id
    }
    assert {"release.accept", "release.point", "release.rollback"} <= actions, actions
    [rolled] = [
        e
        for e in ledgered(stage, names.BUNDLE_CHAIN, began)
        if e.action == "release.rollback" and e.correlation_id == accepted.correlation_id
    ]
    late = rolled.detail["reason"].get("timed_out", {}).get(candidate, {})
    [lookup] = [f for f in late if "_pa_e2e_lookup_" in f] or [None]
    assert lookup, f"the rollback names no timed-out lookup: {rolled.detail['reason']}"
    applied = {
        e.action
        for e in ledgered(stage, names.channel_chain(channel.name), began)
        if e.correlation_id == accepted.correlation_id
    }
    assert "release.apply" in applied, applied
    remap_started, remapped = task_window(stage, execution, "Rollback")
    time.sleep(LOG_DELAY_S)
    seen = breaks(stage, [v.session for v in viewers], flipped - 600, time.time())
    print(f"rollback task {remap_started - flipped:.0f}-{remapped - flipped:.0f} s after the lookup turned slow")
    minutes = request_latency(stage, candidate, lookup, flipped - 60, time.time())
    print(f"{lookup} per minute (s from the remap, max ms): {[(round(t - remapped), m) for t, m in minutes]}")
    assert any(m >= late[lookup]["timeout_ms"] for t, m in minutes if t <= remap_started), minutes
    assert not [t for t, _ in minutes if t >= remapped + REMAP_S], f"the slow tree ran after the remap: {minutes}"
    for session, requests in seen.items():
        print(f"viewer {session}: {[(round(t - remapped, 1), failed) for t, failed in requests]}")
        assert any(flipped <= t <= remap_started for t, _ in requests), f"{session}: no break met the slow lookup"
        later = [failed for t, failed in requests if t > remapped + REMAP_S]
        assert later and not any(later), f"{session}: breaks after the remap {later}"


def test_the_smoke_test_stops_a_slow_release_before_any_viewer_meets_it(
    releases: Stage, rig: Rig, on_air: dict[str, Any], channel: Channel, run_id: str
) -> None:
    stage, began = releases, time.time()
    before = channel_state(stage, channel.name)
    rig.set_delay(SLOW_MS)
    correlation = f"{run_id}-smoke"
    publish(stage, with_lookup(stage, f"{rig.origin}lookup", rig.headers), correlation_id=correlation)
    execution = execution_of(stage, correlation)
    wait_for(
        lambda: "" in entered(stage, execution) or None,
        timeout=ROLLOUT_TIMEOUT_S,
        interval=30,
        what=f"{execution} to end",
    )
    states = entered(stage, execution)
    assert outcome(stage, execution)["error"] == "RolledBack" and "Point" not in states, (
        outcome(stage, execution),
        sorted(states),
    )
    after = channel_state(stage, channel.name)
    assert (
        after.stable == before.stable
        and after.siblings[before.idle or ""].release == before.siblings[before.idle or ""].release
    )
    [rollback] = [e for e in ledgered(stage, names.BUNDLE_CHAIN, began) if e.action == "release.rollback"]
    print(f"\nsmoke rollback reason: {json.dumps(rollback.detail['reason'])[:600]}")
    cause = rollback.detail["reason"].get("Cause", "")
    assert "_pa_e2e_lookup_" in cause and "timeout" in cause, rollback.detail


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def origin_slot(stage: Stage) -> str:
    """The slot of the origin secret the siblings send (the Release Controller's state)."""
    table = stage.session.resource("dynamodb", region_name=stage.region).Table(
        stage.output(Module.RELEASE, "StateTableName")
    )
    return str(table.get_item(Key={"pk": "ORIGIN"}, ConsistentRead=True)["Item"]["slot"])


def admitted(stage: Stage) -> set[str]:
    """Digests of the values ``fast-origin-required`` admits (never the values)."""
    waf = stage.session.client("wafv2", region_name="us-east-1")
    name = origin_rule_group(stage.prefix)
    [group] = [g for g in waf.list_rule_groups(Scope="CLOUDFRONT")["RuleGroups"] if g["Name"] == name]
    rules = waf.get_rule_group(Name=name, Scope="CLOUDFRONT", Id=group["Id"])["RuleGroup"]["Rules"]
    [rule] = [r for r in rules if r["Name"] == "fast-origin-required"]
    matches = rule["Statement"]["AndStatement"]["Statements"][1]["NotStatement"]["Statement"]["OrStatement"][
        "Statements"
    ]
    return {_digest(m["ByteMatchStatement"]["SearchString"].decode()) for m in matches}


def secrets(stage: Stage) -> dict[str, str]:
    """Digests of origin secrets A and B."""
    client = stage.session.client("secretsmanager", region_name="us-east-1")
    return {
        slot: _digest(client.get_secret_value(SecretId=origin_secret(stage.prefix, slot))["SecretString"])
        for slot in ORIGIN_SECRET_SLOTS
    }


def test_a_rotation_during_live_sessions_blocks_no_ad_request(
    releases: Stage, on_air: dict[str, Any], channel: Channel, run_id: str
) -> None:
    stage, began = releases, time.time()
    stable = channel_state(stage, channel.name).stable
    assert stable
    before = secrets(stage)
    assert admitted(stage) == set(before.values()), "the WAF admits both values before"
    viewers = [Viewer(stable, stage).start() for _ in range(CANDIDATE_VIEWERS)]
    sfn = stage.session.client("stepfunctions", region_name=stage.region)
    try:
        execution = sfn.start_execution(
            stateMachineArn=stage.output(Module.RELEASE, "RotationStateMachineArn"),
            name=f"{run_id}-rotation",
            input=json.dumps({"reason": "e2e"}),
        )["executionArn"]
        wait_for(lambda: "" in entered(stage, execution) or None, timeout=1800, interval=30, what=f"{execution}")
        time.sleep(270)  # two more breaks
    finally:
        for viewer in viewers:
            viewer.stop()
    assert outcome(stage, execution)["status"] == "SUCCEEDED", outcome(stage, execution)
    after = secrets(stage)
    slot = origin_slot(stage)
    assert all(after[s] != before[s] for s in ORIGIN_SECRET_SLOTS), "both values are new: one admitted, one retired"
    assert admitted(stage) == set(after.values()), "the WAF admits both current values after"
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    for name in stage.json_output(Module.RELEASE, "Siblings"):
        headers = mediatailor.get_playback_configuration(Name=name)["AdDecisionServerConfiguration"]["HttpRequest"][
            "Headers"
        ]
        assert _digest(headers["X-Fast-Origin"]) == after[slot], name
    time.sleep(LOG_DELAY_S)
    rows = insights(
        stage,
        names.log_group(stage.prefix),
        "filter sessionId in [{}] | fields strcontains(additionalInfo, 'Code:403') as is_refused "
        "| stats count(*) as n, sum(is_refused) as refused by eventType".format(
            ", ".join(f"'{v.session}'" for v in viewers)
        ),
        began,
        time.time(),
    )
    counts = {r["eventType"]: int(r["n"]) for r in rows}
    refused = sum(int(float(r.get("refused") or 0)) for r in rows)
    print(f"\nviewers' events during the rotation: {counts}; refused by the origin gate: {refused}")
    assert refused == 0, rows  # another ADS failure (a Prebid Server 500) is no rotation's
    # A prefetched break is requested minutes before it airs: the last request may have no fill in the window.
    assert counts.get("MAKING_ADS_REQUEST", 0) > 0 and counts.get("FILLED_AVAIL", 0) > 0, counts
    blocked = stage.session.client("cloudwatch", region_name="us-east-1").get_metric_statistics(
        Namespace="AWS/WAFV2",
        MetricName="BlockedRequests",
        Dimensions=[
            {"Name": "Rule", "Value": "fast-origin-required"},
            {"Name": "RuleGroup", "Value": origin_rule_group(stage.prefix)},
        ],
        StartTime=datetime.fromtimestamp(began, UTC),
        EndTime=datetime.now(UTC),
        Period=60,
        Statistics=["Sum"],
    )["Datapoints"]
    assert sum(p["Sum"] for p in blocked) == 0, blocked


def logged_at(timestamp: str) -> float:
    """A Logs Insights ``@timestamp`` (``2026-10-30 14:12:21.123``, UTC) in epoch seconds."""
    return datetime.fromisoformat(timestamp.replace(" ", "T")).replace(tzinfo=UTC).timestamp()


def upcoming_break(stage: Stage, channel: str, after: float, *, longer_than: float = 0) -> tuple[float, float]:
    """The start (epoch seconds) and length (s) of ``channel``'s first ad break that starts after ``after`` and lasts
    more than ``longer_than`` seconds."""
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    starts = sorted(
        (b["ApproximateStartTime"].timestamp(), float(b.get("ApproximateDurationSeconds", 0)))
        for page in mediatailor.get_paginator("get_channel_schedule").paginate(
            ChannelName=channel, DurationMinutes="60"
        )
        for item in page["Items"]
        for b in item.get("ScheduleAdBreaks", [])
    )
    later = [s for s in starts if s[0] > after and s[1] > longer_than]
    assert later, f"{channel} airs no break over {longer_than} s after {datetime.fromtimestamp(after, UTC):%H:%M:%S}"
    return later[0]


def test_a_mid_break_joiner_is_filled_for_what_is_left_of_the_break(
    releases: Stage, on_air: dict[str, Any], channel: Channel
) -> None:
    """ADR-0021 (M3-07): with AFTER_LIVE_EDGE 00:00:00 and PARTIAL_AVAIL, a viewer who joins 20 s into a 60.06 s break
    asks for what is left of it (about 42 s, at segment granularity); with BEHIND_LIVE_EDGE it asked for nothing. The
    remainder is not a grid duration, and the one stored request serves it: the module shapes a pod of one slot per
    whole 30 s of it, so the remainder is filled and never past its length. The sibling is the pair sibling of the
    newest release (the one under test)."""
    stage = releases
    state = channel_state(stage, channel.name)
    sibling = max((s for s in (state.stable, state.idle) if s), key=lambda s: state.siblings[s].release)
    # A remainder of a slot at least: the join at +20 s leaves 30 s or more of the break.
    start, length = upcoming_break(
        stage, channel.name, time.time() + 15, longer_than=JOIN_AFTER_S + POD_MIN_SECONDS + 5
    )
    until(datetime.fromtimestamp(start + JOIN_AFTER_S, UTC))
    viewer = Viewer(sibling, stage).start()
    try:
        until(datetime.fromtimestamp(start + length + 30, UTC))
    finally:
        viewer.stop()
    time.sleep(LOG_DELAY_S)
    rows = insights(
        stage,
        names.log_group(stage.prefix),
        f"filter sessionId = '{viewer.session}' and eventType in ['MAKING_ADS_REQUEST', 'FILLED_AVAIL']\n"
        "| fields @timestamp, eventType, adsRequestUrl, avail.numAds, avail.filledDuration, avail.originAvailDuration",
        start - 60,
        time.time(),
    )
    requests = [
        r for r in rows if r["eventType"] == "MAKING_ADS_REQUEST" and logged_at(r["@timestamp"]) < start + length
    ]
    filled = [r for r in rows if r["eventType"] == "FILLED_AVAIL" and logged_at(r["@timestamp"]) < start + length + 5]
    print(f"\nmid-break join on {sibling} at +20 s: {requests} {filled}")
    assert requests, f"no ad request for the joined break: {rows}"
    dur_ms = int(parse_qs(urlsplit(requests[0]["adsRequestUrl"]).query)["dur"][0])
    seconds = (dur_ms + 500) // 1000
    assert 0 < dur_ms < length * 1000 and seconds not in POD_SECONDS, f"not an off-grid remainder: {dur_ms} ms"
    assert seconds >= POD_MIN_SECONDS, f"a {seconds} s remainder is too short for a slot: join earlier in the break"
    assert filled, f"no FILLED_AVAIL for the {seconds} s remainder: {rows}"
    assert int(filled[0].get("avail.numAds") or 0) >= 1, f"the {seconds} s remainder was not filled: {filled}"
    assert float(filled[0]["avail.filledDuration"]) <= dur_ms / 1000, f"filled past the remainder: {filled}"


def test_the_probe_sibling_runs_the_stable_release_and_fills_house_ads(
    releases: Stage, on_air: dict[str, Any], channel: Channel
) -> None:
    """M3-x4: the probe ``-p-z`` follows the stable sibling's release on every promotion and rollback (this run's
    rollbacks, or the promote function's ``probes`` step on a stage upgraded since). It maps the same session-control
    tree (the crawl lookup is the pair's only), sends the same ADS body and carries the same avail suppression; its own
    name and ``X-Fast-Probe`` differ. A canary session on it through a break is filled, with house ads only, and its
    decision records are flagged."""
    stage = releases
    state = channel_state(stage, channel.name)
    assert state.stable is not None
    probe_name = f"{channel.name}-p-{ROLE_LETTERS[SiblingRole.PROBE][0]}"
    release = state.siblings[state.stable].release
    assert state.siblings[probe_name].release == release, (probe_name, state.siblings[probe_name], release)
    assert state.history[probe_name] == (release,), "the probe keeps no rollback target of its own"
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    stable: dict[str, Any] = dict(mediatailor.get_playback_configuration(Name=state.stable))
    probe: dict[str, Any] = dict(mediatailor.get_playback_configuration(Name=probe_name))
    assert probe["Tags"]["fast:release"] == stable["Tags"]["fast:release"] == release_tag(release)
    # the same trees for the hooks it maps: the session controls; the crawl lookup (PRE_ADS) is the pair's only
    assert (
        probe["FunctionMapping"]["PRE_SESSION_INITIALIZATION"]
        == stable["FunctionMapping"]["PRE_SESSION_INITIALIZATION"]
    )
    assert probe["FunctionMapping"].items() <= stable["FunctionMapping"].items(), (probe["FunctionMapping"], stable)
    assert probe["AvailSuppression"] == stable["AvailSuppression"]
    probe_request = probe["AdDecisionServerConfiguration"]["HttpRequest"]
    stable_request = stable["AdDecisionServerConfiguration"]["HttpRequest"]
    assert probe_request["Body"] == stable_request["Body"]
    assert probe_request["Headers"].get("X-Fast-Probe") == "1" and "X-Fast-Probe" not in stable_request["Headers"]
    start, length = upcoming_break(stage, channel.name, time.time() + 45)
    until(datetime.fromtimestamp(start - 30, UTC))
    viewer = Viewer(probe_name, stage).start()
    try:
        until(datetime.fromtimestamp(start + length + 30, UTC))
    finally:
        viewer.stop()
    time.sleep(LOG_DELAY_S)
    filled = [
        r
        for r in insights(
            stage,
            names.log_group(stage.prefix),
            f"filter sessionId = '{viewer.session}' and eventType = 'FILLED_AVAIL'\n"
            "| fields @timestamp, avail.numAds, avail.filledDuration",
            start - 60,
            time.time(),
        )
        if logged_at(r["@timestamp"]) < start + length + 5
    ]
    print(f"\nprobe {probe_name} on r{release:04d}: {filled}")
    assert filled and int(filled[0].get("avail.numAds") or 0) >= 1, f"the probe's break was not filled: {filled}"
    records = [
        json.loads(r["@message"])
        for r in insights(
            stage,
            sidecar_log_group(stage.prefix),
            f"fields @message | filter sid = '{viewer.session}' and record in ['pds.policy', 'pds.select']",
            start - 60,
            time.time(),
        )
    ]
    policies = [r for r in records if r["record"] == "pds.policy"]
    selects = [r for r in records if r["record"] == "pds.select" and r["pod"]]
    assert policies and all(r["probe"] for r in policies), f"the probe's decisions are not flagged: {policies}"
    assert selects, f"no pod selected for the probe session: {records}"
    for select in selects:
        chosen = [b for b in select["bids"] if b["ref"] in select["pod"]]
        assert chosen and all(b["house"] for b in chosen), f"a probe pod with a paid ad: {select}"
