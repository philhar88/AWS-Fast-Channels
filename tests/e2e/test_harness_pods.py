"""Offline tests of ``harness_pods.py``: the readers and checks of the demand smoke (``test_14_demand_pods.py``)."""

import json
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Final

import harness_pods as pods
import httpx
import pytest
from harness import Player
from harness_pods import Ad, Creatives, reconcile

from fast_channels.core.demo_demand import demo_index
from fast_channels.core.pds.book import Creative

DATA = Path(__file__).parent / "data"
CDN = "https://d3pzfwsg0kjqjs.cloudfront.net"
SID = "49f0c250-9e3c-4997-a723-3cd95eff9d42"
POD = ["direct1", "sim2", "sim3", "direct2"]
STANDALONES = ["sim1", "house1", "house2"]


def sample(name: str) -> str:
    return (DATA / name).read_text()


# --- the viewer's address ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "sent"),
    [
        ("198.51.100.77", "198.51.100.0"),
        ("203.0.113.0", "203.0.113.0"),
        ("2001:db8:1234:56ff:aaaa:bbbb:cccc:dddd", "2001:db8:1234:5600::"),
        ("2001:db8::1", "2001:db8::"),
    ],
)
def test_masked_is_what_prebid_server_sends_a_bidder_denied_precise_geo(address: str, sent: str) -> None:
    assert pods.masked(ip_address(address)) == ip_address(sent)


def test_the_egress_address_is_read_over_the_given_client() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="198.51.100.7\n")))
    assert pods.egress_address(client) == ip_address("198.51.100.7")


def test_the_ipv4_client_binds_its_connections_to_ipv4() -> None:
    with pods.ipv4_client(timeout=1.0) as client:
        pool = client._transport._pool  # type: ignore[attr-defined]  # httpx's HTTPTransport keeps it here
        assert pool._local_address == "0.0.0.0"


# --- viewers -------------------------------------------------------------------------------------------------------


def test_session_id_is_the_uuid_in_the_playlist_path() -> None:
    assert pods.session_id(f"{CDN}/v1/manifest/1bab/cfg-p-a/{SID}/2.m3u8") == SID
    assert pods.session_id(f"{CDN}/v1/master/1bab/cfg-p-a/hls.m3u8?{SID}") is None


def fake_sibling(requests: list[httpx.Request]) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/hls.m3u8"):
            return httpx.Response(200, text=sample("synthetic-ssai-multivariant.m3u8"))
        if request.url.path.startswith("/v1/manifest/"):
            return httpx.Response(200, text=sample("synthetic-ssai-ad-break.m3u8"))
        if "/segment/" in request.url.path:
            return httpx.Response(302, headers={"Location": f"{CDN}/ads/house1/main_00001.ts"})
        return httpx.Response(404)

    return httpx.MockTransport(answer)


def test_a_viewer_plays_the_lowest_variant_and_requests_each_ad_segment_once() -> None:
    requests: list[httpx.Request] = []
    player = Player(httpx.Client(transport=fake_sibling(requests), follow_redirects=True))
    viewer = pods.Viewer.join(player, f"{CDN}/v1/master/1bab/cfg-p-a/hls.m3u8")

    viewer.poll()
    viewer.poll()

    assert (viewer.sid, viewer.variant.rsplit("/", 1)[1]) == (SID, "2.m3u8")  # 640x360, the lowest bandwidth
    segments = [str(r.url) for r in requests if "/segment/" in r.url.path]
    assert len(segments) == len(set(segments)) == 5
    assert not [r for r in requests if r.url.path.startswith("/ads/")], "redirects are not followed"
    assert list(viewer.splices) == ["1792169754364-34-1F41"] and not viewer.errors
    starts = datetime(2026, 10, 16, 16, 55, 54, 364000, UTC)
    ads, slate = viewer.break_seconds(starts, 30.03)
    assert (round(ads, 3), slate) == (30.03, 0)


def test_a_viewer_records_errors_and_asks_again_after_a_server_error() -> None:
    statuses = iter([503, 302])

    def answer(request: httpx.Request) -> httpx.Response:
        if "/segment/" in request.url.path:
            return httpx.Response(next(statuses, 302))
        return httpx.Response(200, text=sample("synthetic-ssai-ad-break.m3u8"))

    viewer = pods.Viewer(
        Player(httpx.Client(transport=httpx.MockTransport(answer))), f"{CDN}/v1/manifest/x/0.m3u8", SID
    )
    viewer.poll()
    assert len(viewer.requested) == 4 and any("-> 503" in error for error in viewer.errors)
    viewer.poll()
    assert len(viewer.requested) == 5


def test_watch_polls_every_viewer_until_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    polled: list[int] = []

    class Counter:
        def __init__(self, n: int) -> None:
            self.n = n

        def poll(self) -> None:
            polled.append(self.n)

    monkeypatch.setattr(pods, "POLL_SECONDS", 0.001)
    pods.watch([Counter(1), Counter(2)], ends=datetime.now(UTC) + timedelta(milliseconds=50))  # type: ignore[list-item]
    assert polled[:2] == [1, 2] and len(polled) % 2 == 0
    pods.watch([Counter(3)], ends=datetime.now(UTC))  # type: ignore[list-item]
    assert 3 not in polled


# --- logs ----------------------------------------------------------------------------------------------------------


class FakeLogs:
    """``filter_log_events`` of a CloudWatch Logs client over fixed pages."""

    class exceptions:  # noqa: N801 - botocore's attribute name
        class ResourceNotFoundException(Exception):  # noqa: N818 - botocore's name
            pass

    def __init__(self, groups: dict[str, list[dict[str, Any]]]) -> None:
        self.groups = groups
        self.calls: list[dict[str, Any]] = []

    def get_paginator(self, name: str) -> FakeLogs:
        assert name == "filter_log_events"
        return self

    def paginate(self, **arguments: Any) -> list[dict[str, Any]]:
        self.calls.append(arguments)
        if arguments["logGroupName"] not in self.groups:
            raise self.exceptions.ResourceNotFoundException
        return [{"events": self.groups[arguments["logGroupName"]]}]


def test_logs_read_a_window_oldest_first_and_nothing_from_a_missing_group() -> None:
    client = FakeLogs({"g": [{"timestamp": 2, "message": "b"}, {"timestamp": 1, "message": "a"}]})
    logs = pods.Logs(client, start_ms=10, end_ms=20)
    assert [e["message"] for e in logs.events("g", '"x"')] == ["a", "b"]
    assert logs.events("missing", '"x"') == []
    assert client.calls[0] == {"logGroupName": "g", "filterPattern": '"x"', "startTime": 10, "endTime": 20}
    pods.Logs(client, start_ms=10).events("g", '"x"')
    assert client.calls[-1] == {"logGroupName": "g", "filterPattern": '"x"', "startTime": 10}


def test_filter_patterns() -> None:
    assert pods.any_of("sessionId", ["b", "a"]) == '($.sessionId = "a" || $.sessionId = "b")'
    assert pods.record_pattern("pds.select", ["s"]) == '{ ($.record = "pds.select") && ($.sid = "s") }'


def test_json_object_reads_json_lines_and_lambda_text_lines() -> None:
    line = {"evt": "sim-bid-request", "imps": 4}
    assert pods.json_object(json.dumps(line)) == line
    assert pods.json_object(f"[INFO]\t2026-10-29T10:00:00.000Z\treq-1\t{json.dumps(line)}\n") == line
    assert pods.json_object("START RequestId: req-1 Version: $LATEST") is None
    assert pods.json_object("[1, 2]") is None


def test_event_line_reads_prebid_servers_and_the_lambda_formats() -> None:
    pod = '{"evt":"fast-ctv-pod","sid":"s","evid":"7","winners":["ownbook:direct1"]}'
    unescaped = (
        '{"timestamp":"2026-10-28T18:00:34.545+0000", "level":"INFO", "logger":"org.prebid...FastCtvVastHook", '
        f'"thread":"vert.x-eventloop-thread-0", "message":"{pod}", "containerId":"77b0be93"}}'
    )
    assert pods.event_line(unescaped) == json.loads(pod)
    escaped = json.dumps({"level": "INFO", "message": pod})
    assert pods.event_line(escaped) == json.loads(pod)
    simulator = {"evt": "sim-bid-request", "imps": 4, "device_ip": "35.95.64.0"}
    lambda_json = {"timestamp": "2026-10-29T20:21:32Z", "level": "INFO", "message": json.dumps(simulator)}
    assert pods.event_line(json.dumps(lambda_json) + "\n") == simulator
    assert pods.event_line(f"[INFO]\t2026-10-29T20:21:32Z\treq-1\t{json.dumps(simulator)}\n") == simulator
    assert pods.event_line(json.dumps({"level": "INFO", "message": "Started"})) is None
    assert pods.event_line('{"message":"{\\"no\\": \\"evt\\"}"}') is None


def test_dur_ms_is_the_request_urls_dur() -> None:
    assert pods.dur_ms("https://pbs.example/openrtb2/auction?evid=7&dur=48054") == 48054
    assert (pods.dur_ms("https://pbs.example/openrtb2/auction?evid=7"), pods.dur_ms(None)) == (None, None)
    assert pods.dur_ms("https://pbs.example/openrtb2/auction?dur=") is None


def test_evid_and_timestamp() -> None:
    assert pods.evid("https://pbs.example/openrtb2/auction?evid=1793&upid=FST3e2e&dur=120120") == "1793"
    assert pods.evid("https://pbs.example/openrtb2/auction") is None
    assert pods.evid(None) is None
    assert pods.timestamp({"eventTimestamp": "2026-10-28T18:01:01.420434896Z"}) == datetime(
        2026, 10, 28, 18, 1, 1, 420434, UTC
    )


# --- creatives -----------------------------------------------------------------------------------------------------


def creatives() -> Creatives:
    encoded = {
        crid: (f"{crid}-std-1234", f"{CDN}/ads/standard/{crid}-std-1234/main.m3u8", 30030)
        for crid in ("sim1", "sim2", "sim3", "direct1", "direct2")
    }
    house = [
        Creative(
            crid=f"house{n}",
            creative_id=f"h{n}-std-1234",
            hls_uri=f"{CDN}/ads/standard/h{n}/main.m3u8",
            duration_ms=30030,
        )
        for n in (1, 2)
    ]
    return Creatives.of(demo_index("standard", encoded, width=1920, height=1080), house)


def test_creatives_by_mediatailors_id_or_mezzanine_url() -> None:
    known = creatives()
    assert known.crid({"creativeUniqueId": "sim2-std-1234"}) == "sim2"
    assert known.crid({"creativeUniqueId": "?", "adMezzanineUrl": f"{CDN}/ads/standard/h1/main.m3u8"}) == "house1"
    assert known.crid({"creativeUniqueId": "other"}) is None


def ad(crid: str, sequence: int | None = None, reason: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "creativeUniqueId": f"{crid}-std-1234" if crid.startswith(("sim", "direct")) else f"h{crid[-1]}-std-1234"
    }
    if sequence is not None:
        entry["sequence"] = sequence
    if reason is not None:
        entry["skippedReason"] = reason
    return entry


def test_avail_ads() -> None:
    avail = {"creativeAds": [ad("direct1", 1), ad("sim1")], "skippedAds": [ad("sim2", 2, "NEW_CREATIVE")]}
    stitched, skipped = pods.avail_ads(avail, creatives())
    assert stitched == [Ad("direct1", 1), Ad("sim1", None)]
    assert skipped == [Ad("sim2", 2, "NEW_CREATIVE")]
    assert pods.avail_ads({}, creatives()) == ([], [])


# --- reconcile -----------------------------------------------------------------------------------------------------


def test_reconcile_a_whole_pod_in_sequence() -> None:
    stitched = [Ad(crid, n) for n, crid in enumerate(POD, start=1)]
    skipped = [Ad(crid, None, "STANDALONE_AD_NOT_NEEDED") for crid in STANDALONES]
    assert reconcile(POD, STANDALONES, stitched, skipped) == pods.Fill()


def test_reconcile_a_replacement_in_place_and_a_partial_pod() -> None:
    """The first 120 s break of the smoke: three first-sight winners, one known standalone."""
    stitched = [Ad("direct1", 1), Ad("sim1", None)]
    skipped = [Ad(c, n, "NEW_CREATIVE") for n, c in ((2, "sim2"), (3, "sim3"), (4, "direct2"))]
    skipped += [Ad("house1", None, "NEW_CREATIVE"), Ad("house2", None, "NEW_CREATIVE")]

    fill = reconcile(POD, STANDALONES, stitched, skipped)

    assert fill.replaced == (("sim2", "NEW_CREATIVE", "sim1"),)
    assert fill.unfilled == (("sim3", "NEW_CREATIVE"), ("direct2", "NEW_CREATIVE"))
    assert not fill.problems


def test_reconcile_takes_a_replacement_that_inherits_the_winners_sequence() -> None:
    fill = reconcile(POD[:2], ["house1"], [Ad("direct1", 1), Ad("house1", 2)], [Ad("sim2", 2, "NEW_CREATIVE")])
    assert fill == pods.Fill(replaced=(("sim2", "NEW_CREATIVE", "house1"),))


def test_reconcile_a_skip_followed_by_the_next_winner() -> None:
    fill = reconcile(POD[:3], [], [Ad("direct1", 1), Ad("sim3", 3)], [Ad("sim2", 2, "NEW_CREATIVE")])
    assert fill == pods.Fill(unfilled=(("sim2", "NEW_CREATIVE"),))


NEW = "NEW_CREATIVE"


@pytest.mark.parametrize(
    ("pod", "stitched", "skipped", "problem"),
    [
        (POD[:2], [Ad("sim2", 1), Ad("direct1", 2)], [], "direct1 (sequence 1) is neither stitched"),
        (POD[:2], [Ad("direct1", 2)], [Ad("sim2", 2, NEW)], "direct1 stitched with sequence 2, not 1"),
        (POD[:2], [Ad("direct1", 1), Ad("sim2", 2), Ad("sim9", None)], [], "beyond it: [('sim9', None)]"),
        (
            POD[:3],
            [Ad("direct1", 1), Ad("sim1", None), Ad("sim1", None)],
            [Ad("sim2", 2, NEW), Ad("sim3", 3, NEW)],
            "('sim1', None)",
        ),
        (POD[:2], [Ad("direct1", 1), Ad("sim1", 3)], [Ad("sim2", 2, NEW)], "beyond it: [('sim1', 3)]"),
        (POD[:2], [Ad("direct1", 1), Ad(None, None)], [Ad("sim2", 2, NEW)], "beyond it: [(None, None)]"),
    ],
)
def test_reconcile_finds_what_breaks_the_sequence(
    pod: list[str], stitched: list[Ad], skipped: list[Ad], problem: str
) -> None:
    fill = reconcile(pod, STANDALONES, stitched, skipped)
    assert any(problem in found for found in fill.problems), fill


# --- breaks --------------------------------------------------------------------------------------------------------


def ads_event(kind: str, request: str, at: str, **fields: Any) -> dict[str, Any]:
    return {"eventType": kind, "sessionId": SID, "requestId": request, "eventTimestamp": at, **fields}


def logged_at(at: str) -> dict[str, int]:
    """A reader's :data:`harness_pods.LOGGED` for ``at``."""
    return {pods.LOGGED: int(datetime.fromisoformat(at).timestamp() * 1000)}


def select_record(evid: str) -> dict[str, Any]:
    bids = [
        {"ref": f"r{n}", "crid": crid, "seat": "amt" if crid.startswith("sim") else "ownbook"}
        for n, crid in enumerate(POD + STANDALONES)
    ]
    return {
        "record": "pds.select",
        "sid": SID,
        "evid": evid,
        "pod": ["r0", "r1", "r2", "r3"],
        "standalone": ["r4", "r5", "r6"],
        "bids": bids,
    }


def test_breaks_join_mediatailor_the_pds_and_the_module_per_session_and_evid() -> None:
    url = "https://pbs.example/openrtb2/auction?evid=7&dur=120120"
    avail = {"creativeAds": [ad(c, n) for n, c in enumerate(POD, start=1)], "skippedAds": []}
    events = [
        ads_event("FILLED_AVAIL", "q1", "2026-10-29T10:00:01.5Z", adsRequestUrl=url, avail=avail),
        ads_event("MAKING_ADS_REQUEST", "q1", "2026-10-29T10:00:01.0Z", adsRequestUrl=url),
        ads_event("VAST_RESPONSE", "q1", "2026-10-29T10:00:01.2Z"),
        ads_event("BEACON_FIRED", "q1", "2026-10-29T10:00:09.0Z", beaconInfo={}),
        ads_event("VAST_RESPONSE", "other", "2026-10-29T10:00:01.2Z"),
    ]
    at = logged_at("2026-10-29T10:00:01.1Z")
    records = [
        {"record": "pds.policy", "sid": SID, "evid": "7"} | at,
        select_record("7") | at,
        {"record": "other", "sid": SID},
    ]
    winners = ["ownbook:direct1", "amt:sim2", "amt:sim3", "ownbook:direct2"]
    pod_line: dict[str, Any] = {
        "evt": "fast-ctv-pod",
        "sid": SID,
        "evid": "7",
        "winners": winners,
        "standalones": ["amt:sim1"],
    } | at
    lines = [pod_line, {"evt": "fast-ctv-request", "sid": SID, "evid": "7"}]

    found = pods.breaks(events, records, lines)

    assert list(found) == [(SID, "7")]
    joined = found[SID, "7"]
    assert joined.event_types == ["MAKING_ADS_REQUEST", "VAST_RESPONSE", "FILLED_AVAIL"]
    assert joined.requested_at == datetime(2026, 10, 29, 10, 0, 1, tzinfo=UTC)
    assert (len(joined.policy), len(joined.select), len(joined.pod_lines)) == (1, 1, 1)
    assert joined.placed() == (POD, STANDALONES)
    assert joined.rendered() == (POD, ["sim1"])
    assert joined.fill(creatives()) == pods.Fill()
    assert joined.avail == avail
    [auction] = joined.auctions()
    assert auction.request == pods.Request("q1", datetime(2026, 10, 29, 10, 0, 1, tzinfo=UTC), None, 120120)
    assert (auction.policy, auction.select, auction.lines) == (joined.policy, joined.select, joined.pod_lines)
    assert (joined.aired, joined.filled_from) == (auction, None)


def test_a_break_without_records_or_events() -> None:
    lone = pods.Break(SID, "9")
    assert (lone.placed(), lone.rendered(), lone.avail, lone.requested_at) == (([], []), ([], []), None, None)
    assert (lone.auctions(), lone.aired, lone.filled, lone.filled_from) == ([], None, None, None)


RETRIEVAL_URL: Final = "https://pbs.example/openrtb2/auction?evid=7&dur=30000"
REAL_TIME_URL: Final = "https://pbs.example/openrtb2/auction?evid=7&dur=30030"


def auction_records(at: str, crids: list[str]) -> list[dict[str, Any]]:
    """A ``/policy`` and a ``/select`` record of a one-ad pod of ``crids[0]`` with ``crids[1:]`` as standalones."""
    bids = [{"ref": f"r{n}", "crid": crid, "seat": "ownbook"} for n, crid in enumerate(crids)]
    select = {"record": "pds.select", "sid": SID, "evid": "7", "bids": bids, "pod": ["r0"], "standalone": ["r1"]}
    return [{"record": "pds.policy", "sid": SID, "evid": "7"} | logged_at(at), select | logged_at(at)]


def pod_line(at: str, crids: list[str]) -> dict[str, Any]:
    return {
        "evt": "fast-ctv-pod",
        "sid": SID,
        "evid": "7",
        "winners": [f"ownbook:{crids[0]}"],
        "standalones": [f"ownbook:{crids[1]}"],
    } | logged_at(at)


def test_a_retrieved_break_asked_again_at_the_break_is_two_auctions_and_airs_the_second() -> None:
    events = [
        ads_event(
            "MAKING_ADS_REQUEST",
            "q1",
            "2026-10-30T13:29:37.6Z",
            adsRequestUrl=RETRIEVAL_URL,
            prefetchScheduleName="pf-7",
        ),
        ads_event("VAST_RESPONSE", "q1", "2026-10-30T13:29:37.8Z"),
        ads_event("MAKING_ADS_REQUEST", "q2", "2026-10-30T13:32:04.8Z", adsRequestUrl=REAL_TIME_URL),
        ads_event("VAST_RESPONSE", "q2", "2026-10-30T13:32:05.0Z"),
        ads_event("FILLED_AVAIL", "q2", "2026-10-30T13:32:05.1Z", adsRequestUrl=REAL_TIME_URL, avail={}),
    ]
    records = auction_records("2026-10-30T13:29:37.7Z", ["direct1", "sim1"])
    records += auction_records("2026-10-30T13:32:04.9Z", ["direct2", "house1"])
    lines = [
        pod_line("2026-10-30T13:29:37.8Z", ["direct1", "sim1"]),
        pod_line("2026-10-30T13:32:05Z", ["direct2", "house1"]),
    ]

    [joined] = pods.breaks(events, records, lines).values()

    retrieval, real_time = joined.auctions()
    assert (retrieval.request.prefetch, real_time.request.prefetch) == ("pf-7", None)
    assert (retrieval.request.dur_ms, real_time.request.dur_ms) == (30000, 30030)
    assert [len(a.policy) + len(a.select) + len(a.lines) for a in (retrieval, real_time)] == [3, 3]
    assert retrieval.lines[0]["winners"] == ["ownbook:direct1"]
    assert joined.requested_at == retrieval.request.at
    assert (joined.filled_from, joined.aired) == (None, real_time)
    assert (joined.placed(), joined.rendered()) == ((["direct2"], ["house1"]), (["direct2"], ["house1"]))


def test_a_break_filled_from_its_retrieval_airs_the_retrieval() -> None:
    events = [
        ads_event(
            "MAKING_ADS_REQUEST", "q1", "2026-10-30T13:31:08Z", adsRequestUrl=RETRIEVAL_URL, prefetchScheduleName="pf-7"
        ),
        ads_event(
            "FILLED_AVAIL",
            "q3",
            "2026-10-30T13:33:40Z",
            adsRequestUrl=RETRIEVAL_URL,
            avail={},
            prefetchScheduleName="pf-7",
        ),
    ]
    records = auction_records("2026-10-30T13:31:08.1Z", ["direct1", "sim1"])
    lines = [pod_line("2026-10-30T13:31:08.2Z", ["direct1", "sim1"])]

    [joined] = pods.breaks(events, records, lines).values()

    [retrieval] = joined.auctions()
    assert (joined.filled_from, joined.aired) == ("pf-7", retrieval)
    assert joined.filled is not None and joined.filled["requestId"] == "q3"
    assert joined.rendered() == (["direct1"], ["sim1"])


def test_an_auction_holds_what_was_logged_from_just_before_its_request_until_the_next() -> None:
    events = [
        ads_event(
            "MAKING_ADS_REQUEST", "q1", "2026-10-30T13:29:37Z", adsRequestUrl=RETRIEVAL_URL, prefetchScheduleName="pf-7"
        ),
        ads_event("MAKING_ADS_REQUEST", "q2", "2026-10-30T13:32:04Z", adsRequestUrl=REAL_TIME_URL),
    ]
    skewed = auction_records("2026-10-30T13:29:36.5Z", ["direct1", "sim1"])  # within CLOCK_SLACK before
    early = auction_records("2026-10-30T13:29:30Z", ["direct1", "sim1"])  # before any request
    undated = [{"record": "pds.policy", "sid": SID, "evid": "7"}]
    lines = [pod_line("2026-10-30T13:32:03.5Z", ["direct2", "house1"])]  # the second auction's, logged a bit early

    [joined] = pods.breaks(events, skewed + early + undated, lines).values()

    retrieval, real_time = joined.auctions()
    assert (len(retrieval.policy), len(retrieval.select), len(retrieval.lines)) == (1, 1, 0)
    assert (len(real_time.policy), len(real_time.select), len(real_time.lines)) == (0, 0, 1)
    assert (len(joined.policy), len(joined.select)) == (3, 2)  # the early and undated ones are in no auction


# --- readiness -----------------------------------------------------------------------------------------------------


class Fake:
    """A client whose calls answer from a table."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers

    def __getattr__(self, name: str) -> Any:
        answer = self.answers[name]
        return answer if callable(answer) else lambda **_: answer


def test_prebid_tasks_are_the_running_ones_with_their_image() -> None:
    tasks = [
        {"taskArn": "t1", "lastStatus": "RUNNING", "containers": [{"name": "Prebid-Container", "image": "img:1"}]},
        {"taskArn": "t2", "lastStatus": "PROVISIONING", "containers": []},
    ]
    ecs = Fake(list_tasks={"taskArns": ["t1", "t2"]}, describe_tasks={"tasks": tasks})
    assert pods.prebid_tasks(ecs, cluster="c", service="s") == {"t1": "img:1"}
    assert pods.prebid_tasks(Fake(list_tasks={"taskArns": []}), cluster="c", service="s") == {}


def test_warmups_take_each_tasks_newest_build() -> None:
    def build(task: str | None, status: str) -> dict[str, Any]:
        variables = [{"name": "TASK_ARN", "value": task}] if task else [{"name": "RATE", "value": "100"}]
        return {"buildStatus": status, "environment": {"environmentVariables": variables}}

    builds = [build("t1", "IN_PROGRESS"), build("t1", "FAILED"), build("t2", "SUCCEEDED"), build(None, "SUCCEEDED")]
    codebuild = Fake(list_builds_for_project={"ids": ["b1", "b2", "b3", "b4"]}, batch_get_builds={"builds": builds})
    assert pods.warmups(codebuild, "p", {"t1", "t2", "t3"}) == {"t1": "IN_PROGRESS", "t2": "SUCCEEDED"}
    assert pods.warmups(Fake(list_builds_for_project={"ids": []}), "p", {"t1"}) == {}


def test_a_viewer_records_a_failed_read_and_a_failed_segment_request() -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="AccessDenied")

    variant = f"{CDN}/v1/manifest/x/0.m3u8"
    viewer = pods.Viewer(Player(httpx.Client(transport=httpx.MockTransport(refuse))), variant, SID)
    viewer.poll()
    assert viewer.errors == [f"{SID}: GET {variant} -> 403: AccessDenied"]

    def unreachable_ads(request: httpx.Request) -> httpx.Response:
        if "/segment/" in request.url.path:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, text=sample("synthetic-ssai-ad-break.m3u8"))

    viewer = pods.Viewer(Player(httpx.Client(transport=httpx.MockTransport(unreachable_ads))), variant, SID)
    viewer.poll()
    assert not viewer.requested and len(viewer.errors) == 5 and "refused" in viewer.errors[0]


def test_event_line_ignores_other_lines() -> None:
    assert pods.event_line("INFO  Prebid Server started") is None


class Body:
    def __init__(self, text: str) -> None:
        self.text = text

    def read(self) -> bytes:
        return self.text.encode()


def test_creatives_are_read_from_the_media_bucket() -> None:
    encoded = {"sim1": ("sim1-std-1234", f"{CDN}/ads/s/sim1/main.m3u8", 30030)}
    index = demo_index("standard", encoded, width=1920, height=1080)
    vast = (
        '<VAST xmlns="http://www.iab.com/VAST" version="4.2"><Ad sequence="1"><InLine><Creatives>'
        '<Creative id="h1-std-1234"><Linear><Duration>00:00:30.030</Duration><MediaFiles>'
        f'<MediaFile width="1920" height="1080">{CDN}/ads/s/h1/main.m3u8</MediaFile>'
        "</MediaFiles></Linear></Creative></Creatives></InLine></Ad></VAST>"
    )
    objects = {"ads/s/demo/creatives.json": index.model_dump_json(), "ads/s/vast.xml": vast}
    s3 = Fake(get_object=lambda Bucket, Key: {"Body": Body(objects[Key])})  # noqa: N803 - boto3's argument names
    known = Creatives.read(s3, "media", index_key="ads/s/demo/creatives.json", vast_key="ads/s/vast.xml")
    assert known.by_id == {"sim1-std-1234": "sim1", "h1-std-1234": "house1"}
