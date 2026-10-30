"""The ad-server route (``modules.demand.ad_server: mock``, M8-09) driven by MediaTailor on a deployed stage.

Runs only when the stage has the mock ad server (``/<prefix>/demand/ad-server-url``); skipped otherwise. Viewers play
the monetized channel's stable sibling, which takes the ad-server route, through the breaks the session's schedule
airs. For each break the test then joins:

- the fast-ctv module's ``fast-ctv-pod`` lines in Prebid Server's log;
- the mock ad server's decisions;
- MediaTailor's ADS log: the request, the Wrapper it followed, and what it filled.

It also reads the cached pods back through the edge, and checks that the edge lets only ``GET /cache?uuid=<uuid4>``
through. It changes nothing.

The stage ``fastciads`` (``make deploy STAGE=fastciads PREFIX=<throwaway>``, then ``make deploy-demand`` with the
same) has the mock, and sets ``modules.demand.pod_cache_seconds`` long enough (900 s) that every pod of the run can
still be read once the ADS log has been delivered, a few minutes after the break.
"""

import time
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
from _stack import Stage, stage_id
from harness import BREAK_POLICY, LEAD, PROGRAMS, Channel, Player, Tools, until, wait_for
from harness_ad_server import (
    SMART_TV_USER_AGENT,
    Auction,
    Break,
    Decision,
    Logs,
    Viewer,
    auctions,
    breaks,
    decisions,
    per_break,
    pod_media,
    running_tasks,
    warmups,
    watch,
)
from playback import break_windows

from fast_channels.core.demand import Parameter, origin_secret, parameter_path, warmup_project
from fast_channels.core.monetization import names

pytestmark = pytest.mark.e2e

VIEWERS: Final = 3
MAX_REQUESTS_PER_BREAK: Final = 2
"""With prefetch, the retrieval and, when MediaTailor could not use what it retrieved, one request in real time."""
READY_TIMEOUT_S: Final = 900
"""Census lag, Prebid Server's scale-out from zero and its warm-up, with room."""
TAIL: Final = timedelta(seconds=90)
"""How long the viewers keep playing after the last break's scheduled end: the live playlist lags the wall clock."""
LOG_WAIT_S: Final = 480
"""How long the ADS log may take to show every viewer's last break (vended logs arrive in batches)."""
EDGE_REGION: Final = "us-east-1"


@dataclass(frozen=True)
class AdServerStage:
    stage: Stage
    mock_url: str
    cache_url: str
    secret: str
    mock_log_group: str
    container_log_group: str
    ads_log_group: str
    cluster: str
    service: str


@dataclass(frozen=True)
class Run:
    viewers: tuple[Viewer, ...]
    auctions: tuple[Auction, ...]
    decisions: tuple[Decision, ...]
    breaks: tuple[Break, ...]
    pods: dict[str, str]
    """Cached pod documents by uuid, read back through the edge."""


def _parameters(stage: Stage) -> dict[str, str]:
    ssm = stage.session.client("ssm", region_name=stage.region)
    pages = ssm.get_paginator("get_parameters_by_path").paginate(Path=parameter_path(stage.prefix))
    return {p["Name"].rsplit("/", 1)[1]: p["Value"] for page in pages for p in page["Parameters"]}


@pytest.fixture(scope="module")
def ad_server(stage: Stage) -> AdServerStage:
    parameters = _parameters(stage)
    if Parameter.AD_SERVER_URL.value not in parameters:
        pytest.skip(f"stage {stage.prefix} has no mock ad server (modules.demand.ad_server is not mock)")
    demand = _outputs(stage, f"{stage_id(stage.prefix)}-Demand")
    secrets = stage.session.client("secretsmanager", region_name=EDGE_REGION)
    secret = str(secrets.get_secret_value(SecretId=origin_secret(stage.prefix, "a"))["SecretString"])
    return AdServerStage(
        stage=stage,
        mock_url=parameters[Parameter.AD_SERVER_URL.value],
        cache_url=f"https://{parameters[Parameter.CLOUDFRONT_DOMAIN.value]}/cache",
        secret=secret,
        mock_log_group=demand["AdServerLogGroup"],
        container_log_group=demand["ContainerLogGroup"],
        ads_log_group=names.log_group(stage.prefix),
        cluster=parameters[Parameter.CLUSTER_ARN.value],
        service=parameters[Parameter.SERVICE_ARN.value],
    )


def _outputs(stage: Stage, stack: str) -> dict[str, str]:
    cloudformation = stage.session.client("cloudformation", region_name=stage.region)
    [described] = cloudformation.describe_stacks(StackName=stack)["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in described.get("Outputs", [])}


@pytest.fixture(scope="module")
def tv() -> Iterator[Player]:
    """A Connected TV to the Pod Decision Service's device-type rule (the auction forwards the viewer's agent)."""
    client = httpx.Client(follow_redirects=True, timeout=20.0, headers={"User-Agent": SMART_TV_USER_AGENT})
    player = Player(client=client)
    try:
        yield player
    finally:
        player.close()


@pytest.fixture(scope="module")
def run(ad_server: AdServerStage, tv: Player, channel: Channel, tools: Tools, request: pytest.FixtureRequest) -> Run:
    """Start the channel, ingest the pattern while Prebid Server scales out, wait until it is warm, schedule the
    pattern three times with a break each, and play viewers on the stable sibling through the breaks."""
    stage = ad_server.stage
    try:
        tools("channels___start_channel", channel=channel.name)
        asset = request.getfixturevalue("ingested")  # while the census gate scales Prebid Server out
        wait_until_warm(ad_server)
        start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
        plan = tools(
            "schedule___plan",
            channel=channel.name,
            picks=[asset.asset_id] * PROGRAMS,
            start=start.isoformat(),
            break_policy=BREAK_POLICY,
        )
        assert plan["publishable"], plan["violations"]
        published = tools("schedule___publish", plan_id=plan["plan_id"])
        windows = break_windows(published["programs"])
        ssm = stage.session.client("ssm", region_name=stage.region)
        cdn = ssm.get_parameter(Name=names.parameter_name(stage.prefix, names.Parameter.CDN_BASE_URL))["Parameter"]
        playback = httpx.get(f"{cdn['Value'].rstrip('/')}/{names.plan_key(channel.name)}", timeout=10).json()
        until(datetime.fromisoformat(published["programs"][0]["start"]) - timedelta(seconds=20))
        start_ms = int(time.time() * 1000) - 60_000
        viewers = tuple(Viewer.join(tv, playback["playback"]["hls"]) for _ in range(VIEWERS))
        watch(viewers, ends=windows[-1].ends + TAIL)
    finally:
        tools("channels___stop_channel", channel=channel.name)
    logs = Logs(stage.session.client("logs", region_name=stage.region), start_ms)
    sids = {v.sid for v in viewers}
    deadline = time.monotonic() + LOG_WAIT_S
    while True:
        found = {
            k: b
            for k, b in breaks(logs.records(ad_server.ads_log_group, '{ $.eventType = "*" }')).items()
            if b.sid in sids
        }
        filled = {b.sid for b in found.values() if "FILLED_AVAIL" in b.events}
        if (filled == sids and len(found) >= len(sids) * len(windows)) or time.monotonic() > deadline:
            break
        time.sleep(30)
    pods: dict[str, str] = {}
    for entry in found.values():
        if entry.uuid and entry.uuid not in pods:
            response = httpx.get(ad_server.cache_url, params={"uuid": entry.uuid}, timeout=10)
            if response.status_code == 200:
                pods[entry.uuid] = response.text
    result = Run(
        viewers=viewers,
        auctions=tuple(
            a for a in auctions(logs.messages(ad_server.container_log_group, '"fast-ctv-pod"')) if a.sid in sids
        ),
        decisions=tuple(d for d in decisions(logs.messages(ad_server.mock_log_group)) if d.sid in sids),
        breaks=tuple(found.values()),
        pods=pods,
    )
    print(report(result))
    return result


def wait_until_warm(ad_server: AdServerStage) -> None:
    """Until Prebid Server runs and every running task's warm-up succeeded. The Pod Decision Service is a sidecar of
    each task (M2-x3): Prebid Server starts only once it is healthy, so a running task has it."""
    stage = ad_server.stage
    ecs, codebuild = stage.session.client("ecs"), stage.session.client("codebuild")
    last: dict[str, Any] = {}

    def ready() -> bool | None:
        tasks = running_tasks(ecs, cluster=ad_server.cluster, service=ad_server.service)
        warmed = warmups(codebuild, warmup_project(stage.prefix), tasks)
        last.update(tasks=len(tasks), warmups=sorted(warmed.values()))
        if failed := [status for status in warmed.values() if status not in {"SUCCEEDED", "IN_PROGRESS"}]:
            raise AssertionError(f"a Prebid Server warm-up did not succeed: {failed}")
        warm = bool(tasks) and len(warmed) == len(tasks) and set(warmed.values()) == {"SUCCEEDED"}
        return True if warm else None

    wait_for(ready, timeout=READY_TIMEOUT_S, interval=20, what="a warm Prebid Server", state=lambda: last)


def report(run: Run) -> str:
    lines = {(d.sid, d.evid): d.line for d in run.decisions}
    rows = [
        f"{entry.sid[:8]} evid {entry.evid}: line {lines.get((entry.sid, entry.evid))}, wrapper "
        f"{entry.uuid[:8] or entry.redirect[-40:]}, filled {entry.filled_seconds} s with {len(entry.creatives)} "
        f"ads, skipped {sorted(set(entry.skipped))}"
        for entry in sorted(run.breaks, key=lambda b: (b.sid, b.evid))
    ]
    head = f"{len(run.viewers)} viewers, {len(run.breaks)} ad requests, {len(run.auctions)} auctions"
    return "\n".join([head, *rows])


def test_each_ad_request_ran_one_auction(run: Run) -> None:
    """The auction runs once for each request to the ad server, never twice, for every break the viewers met.

    With prefetch (M3-06) a break is usually asked for once, at retrieval. MediaTailor asks again in real time when
    it could not use what it retrieved: a first-sight creative, as on a stage's first break. The 30 s
    ``session_break`` cache (the shortest break, validator R-F8) does not span the retrieval's lead, so that second
    request runs a second auction (fastcih, 2026-10-30)."""
    met = {(b.sid, b.evid) for b in run.breaks if "MAKING_ADS_REQUEST" in b.events and b.evid}
    assert met, "no ad request logged"
    asked = Counter((d.sid, d.evid) for d in run.decisions)
    counts = per_break(run.auctions)
    assert {pair: counts.get(pair, 0) for pair in asked} == dict(asked), "one auction per ad server request"
    assert met <= set(asked), "every break the viewers met went to the ad server"
    assert all(n <= MAX_REQUESTS_PER_BREAK for n in asked.values()), asked


def test_the_ad_server_redirected_to_each_cached_pod(run: Run) -> None:
    """A Prebid line answered every auction with a cached pod, with a Wrapper to that pod's cache entry."""
    by_break = {(d.sid, d.evid): d for d in run.decisions}
    cached = [a for a in run.auctions if a.cached]
    assert cached, f"no auction cached a pod: {run.auctions}"
    for auction in cached:
        decision = by_break[(auction.sid, auction.evid)]
        assert decision.line == f"prebid-{auction.hb_pb}", (auction, decision)
        assert decision.uuid8 == auction.uuid8, (auction, decision)
    redirected = [b for b in run.breaks if b.uuid]
    assert redirected and all(b.uuid[:8] in {a.uuid8 for a in cached} for b in redirected)


def test_a_break_without_a_cached_pod_got_the_house_line(run: Run) -> None:
    """A break whose auction cached no pod (no bid cleared, or the auction failed) reached the ad server without
    Prebid's keys and got the house line. A run often has none, so
    ``test_without_a_bid_the_ad_server_serves_its_house_pod`` checks the mock directly."""
    cached = {(a.sid, a.evid) for a in run.auctions if a.cached}
    uncached = [d for d in run.decisions if (d.sid, d.evid) not in cached]
    assert all(d.line == "house" and not d.uuid8 for d in uncached), uncached


def test_the_stitched_ads_are_the_cached_pods(run: Run) -> None:
    """What MediaTailor filled from a Wrapper is the cached pod's ads (the first break of a new creative plays its
    slate while MediaTailor imports it)."""
    filled = [b for b in run.breaks if b.uuid and b.filled_seconds > 0]
    assert filled, f"no break was filled through a Wrapper: {run.breaks}"
    for entry in filled:
        assert entry.uuid in run.pods, f"the pod {entry.uuid[:8]} could not be read back"
        assert set(entry.creatives) <= set(pod_media(run.pods[entry.uuid])), entry


def test_the_cache_is_only_read_from_the_internet(ad_server: AdServerStage, run: Run) -> None:
    """The newest pod still in the cache (the first ones expire while the ADS log arrives) reads back through the edge;
    nothing else gets through."""
    readable = [
        uuid_
        for uuid_ in reversed(run.pods)
        if httpx.get(ad_server.cache_url, params={"uuid": uuid_}, timeout=10).status_code == 200
    ]
    assert readable, "no pod of the run is still in the cache"
    known = readable[0]
    assert httpx.head(ad_server.cache_url, params={"uuid": known}, timeout=10).status_code == 200
    blocked = [
        httpx.post(ad_server.cache_url, json={"puts": [{"type": "xml", "value": "<VAST/>"}]}, timeout=10),
        httpx.put(ad_server.cache_url, params={"uuid": known}, content=b"<VAST/>", timeout=10),
        httpx.get(ad_server.cache_url, params={"uuid": "not-a-uuid"}, timeout=10),
        httpx.get(ad_server.cache_url, params={"uuid": known.upper()}, timeout=10),
        httpx.get(f"{ad_server.cache_url}/health", timeout=10),
        httpx.get(ad_server.cache_url, timeout=10),
    ]
    assert [r.status_code for r in blocked] == [403] * len(blocked)
    assert httpx.get(ad_server.cache_url, params={"uuid": str(uuid.uuid4())}, timeout=10).status_code == 404


def test_without_a_bid_the_ad_server_serves_its_house_pod(ad_server: AdServerStage) -> None:
    query = {"pc": "e2e", "sid": "e2e", "evid": "1", "hb_pb": "", "hb_uuid": "", "hb_bidder": ""}
    answer = httpx.get(
        f"{ad_server.mock_url}/ads", params=query, headers={"x-fast-origin": ad_server.secret}, timeout=10
    )
    assert answer.status_code == 200
    assert "/vast.xml]]></VASTAdTagURI>" in answer.text and "/cache?uuid=" not in answer.text
    assert httpx.get(f"{ad_server.mock_url}/ads", params=query, timeout=10).status_code == 403
    assert (
        httpx.get(f"{ad_server.mock_url}/ads", params=query, headers={"x-fast-origin": "x"}, timeout=10).status_code
        == 403
    )


def test_the_viewers_played_without_errors(run: Run) -> None:
    errors = [error for viewer in run.viewers for error in viewer.errors]
    assert not errors, errors[:5]
