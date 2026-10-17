"""Offline tests of the e2e helpers (not marked e2e): playlist readers against the samples in ``data/`` (synthetic TS
playlists in the MediaTailor URL shapes of the research run, e2e.md, and its asset list), the watcher against a fake
CDN, tool calls against a fake Lambda."""

import base64
import io
import json
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import agui
import harness
import httpx
import playback
import pytest
from harness import ASSET_ID, BREAK_POLICY, CUE_POINT_MS, LEAD, PROGRAMS, Channel, Observation, Player, ToolError, Tools
from playback import Segment, Window

from fast_channels.core.ladder import Ladder
from fast_channels.core.manifests import (
    expected_signature,
    ladder_signature,
    media_segment_durations,
    segment_boundaries_ms,
)
from fast_channels.core.specs import Creative, vast_pod
from fast_channels.core.timeline import BreakPolicy, PlanInputAsset, ProgrammingBrief, plan_block
from fast_channels.functions.tools.library_store import iso
from fast_channels.infra.constructs.operator.identity import AGENT_SCOPE

DATA = Path(__file__).parent / "data"
TS_ENCODE = Path(__file__).parents[2] / "core" / "tests" / "data" / "hls-ts"
"""A real MediaConvert TS encode of the standard ladder (content and slate), which the synthetic samples follow."""
STANDARD = Ladder.builtin("standard")
CDN = "https://d3pzfwsg0kjqjs.cloudfront.net"
HASH = "1bab6b4238868b752a9e9daae3e2a4911f2a2fc9"
"""MediaTailor's customer hash in the playback URLs of the research run."""
MANIFEST = f"{CDN}/v1/manifest/1bab/fastv3-exp-e2e-cdn/c14ebd60/0.m3u8"
CONTENT = f"{CDN}/assets/e2e-pattern-20261016165300/standard_843ed1d05a/"
"""Where the samples' program segments live (``assets/<asset id>/<ladder version>/`` on the CDN)."""
AD_BREAK = Window(datetime(2026, 10, 16, 16, 55, 54, 364000, UTC), datetime(2026, 10, 16, 16, 56, 24, 394000, UTC))
"""The 30.03 s break of every sample; synthetic-ssai-ad-break fills it with a 15-GOP house ad (5 x 6.006 s)."""
MEDIA_SAMPLES = ("synthetic-ssai-ad-break.m3u8", "synthetic-ssai-slate-break.m3u8", "synthetic-sgai-media.m3u8")


def sample(name: str) -> str:
    return (DATA / name).read_text()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)


# --- playback ------------------------------------------------------------------------------------------------------


def test_renditions_are_every_variant_by_resolution() -> None:
    base = f"{CDN}/v1/master/1bab/fastv3-exp-e2e-ssai/hls.m3u8"
    picked = playback.renditions(sample("synthetic-ssai-multivariant.m3u8"), base)
    assert list(picked) == ["1280x720", "1920x1080", "640x360"]
    session = f"{CDN}/v1/manifest/{HASH}/fastv3-exp-e2e-ssai/49f0c250-9e3c-4997-a723-3cd95eff9d42"
    assert list(picked.values()) == [f"{session}/{n}.m3u8" for n in range(3)]


def test_renditions_keep_variants_that_share_a_resolution() -> None:
    text = "#EXTM3U\n" + "".join(
        f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},CODECS="avc1.640028,mp4a.40.2",RESOLUTION=1920x1080\n{n}.m3u8\n'
        for n, bandwidth in enumerate((6728000, 4528000))
    )
    assert playback.renditions(text, f"{CDN}/x/") == {
        "1920x1080": f"{CDN}/x/0.m3u8",
        "1920x1080@4528000": f"{CDN}/x/1.m3u8",
    }


def test_the_samples_follow_the_ts_encode() -> None:
    """The synthetic multivariants list the real encode's variants, muxed (what test_04 asserts on the air), and
    the media samples keep its segment grid: no EXT-X-MAP, 6.006 s segments, every one ``.ts`` or a beacon."""
    expected = sorted(expected_signature(STANDARD))
    assert sorted(ladder_signature((TS_ENCODE / "content" / "main.m3u8").read_text())) == expected
    for name in ("synthetic-ssai-multivariant.m3u8", "synthetic-sgai-multivariant.m3u8"):
        assert sorted(ladder_signature(sample(name))) == expected, name
        assert "#EXT-X-MEDIA:" not in sample(name), name
    for name in MEDIA_SAMPLES:
        segments = playback.media_segments(sample(name), MANIFEST)
        assert "#EXT-X-MAP" not in sample(name), name
        assert {s.duration for s in segments} == {STANDARD.grid.segment_ms / 1000}, name
        assert all(s.is_ad or s.uri.endswith(".ts") for s in segments), name


def test_stitched_ads_are_told_from_content_by_their_uris() -> None:
    segments = playback.media_segments(sample("synthetic-ssai-ad-break.m3u8"), MANIFEST)
    ads = [s for s in segments if s.is_ad]
    assert [s.starts for s in ads] == [AD_BREAK.starts + timedelta(milliseconds=6006 * n) for n in range(5)]
    assert ads[0].uri == f"{CDN}/v1/segment/{HASH}/fastv3-exp-e2e-cdn/c14ebd60-0c10-4a48-b428-5fb03cfd8d63/0/151"
    content = [s for s in segments if not s.is_ad]
    assert content and all(s.uri.startswith(CONTENT) for s in content)
    assert {s.asset_id for s in content} == {"e2e-pattern-20261016165300"}
    assert playback.ad_seconds(segments, AD_BREAK) == pytest.approx(30.03)


def test_a_break_filled_with_the_slate_has_no_ad_seconds() -> None:
    segments = playback.media_segments(sample("synthetic-ssai-slate-break.m3u8"), MANIFEST)
    slate = [s for s in segments if s.asset_id == "break-15-3c9d2e"]
    assert len(slate) == 5 and slate[0].starts == AD_BREAK.starts
    assert playback.ad_seconds(segments, AD_BREAK, house_ads={"house-ad-1-0a1b2c"}) == 0


def test_house_ad_segments_count_by_their_asset() -> None:
    """A preconditioned ad's segment served straight from its encode on the CDN (no beacon redirect) is an ad only
    by the kit's house-ad asset ids."""
    uri = f"{CDN}/assets/house-ad-1-0a1b2c/standard_843ed1d05a/main_avc-720p_00001.ts"
    direct = Segment(uri, 6.006, AD_BREAK.starts)
    assert direct.asset_id == "house-ad-1-0a1b2c" and not direct.is_ad
    assert direct.is_ad_of({"house-ad-1-0a1b2c"})
    assert playback.ad_seconds([direct], AD_BREAK) == 0
    assert playback.ad_seconds([direct], AD_BREAK, house_ads={"house-ad-1-0a1b2c"}) == 6.006


def test_program_date_time_carries_forward_by_duration() -> None:
    text = "#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:2026-10-16T12:00:00.000Z\n#EXTINF:6.006,\na.ts\n#EXTINF:2.002,\nb.ts\n"
    [first, second] = playback.media_segments(text, f"{CDN}/x/index.m3u8")
    assert second.starts == first.ends == datetime(2026, 10, 16, 12, 0, 6, 6000, tzinfo=UTC)
    assert second.uri == f"{CDN}/x/b.ts"


def test_interstitial_asset_list_url_with_session_variables() -> None:
    [(daterange, attributes)] = playback.interstitials(sample("synthetic-sgai-media.m3u8")).items()
    assert daterange == "1792169754364-34-1F41"
    assert datetime.fromisoformat(attributes["START-DATE"]) == AD_BREAK.starts
    variables = playback.defines(sample("synthetic-sgai-multivariant.m3u8"))
    assert variables["awsSessionId"] == "d8caf024-ff63-4a86-87fa-776d3b05da9d"
    url = playback.substitute(attributes["X-ASSET-LIST"], variables)
    assert "aws.sessionData=<blob>&" in url and "{$" not in url
    assert playback.substitute("{$unknown}", variables) == "{$unknown}"


def test_asset_list() -> None:
    assets = playback.asset_list(sample("pab-assetlist.json"))
    assert [a["DURATION"] for a in assets] == [6.0, 4.0]
    assert "/preconditioned-ads/" in assets[0]["URI"]
    with pytest.raises(ValueError, match="not an asset list"):
        playback.asset_list('{"message": "failed to generate interstitial manifest"}')


def test_vast_pod_has_one_hls_media_file_per_ad() -> None:
    creatives = [
        Creative(creative_id=f"house-{n}", duration_ms=30030, hls_uri=f"{CDN}/ads/{n}/main.m3u8") for n in (1, 2)
    ]
    pod = vast_pod(creatives=creatives, content_base_url=CDN)
    assert playback.vast_ads(pod) == ["house-1", "house-2"]
    assert playback.vast_durations(pod) == [30.03, 30.03]
    assert [(f.mime_type, f.uri) for f in playback.vast_media_files(pod)] == [
        ("application/x-mpegURL", f"{CDN}/ads/1/main.m3u8"),
        ("application/x-mpegURL", f"{CDN}/ads/2/main.m3u8"),
    ]


def test_break_windows_shift_later_breaks_by_earlier_ones() -> None:
    programs: list[dict[str, Any]] = [
        {
            "start": "2026-10-16T12:00:00.000Z",
            "breaks": [{"offset_ms": 30030, "duration_ms": 30030}, {"offset_ms": 60060, "duration_ms": 30030}],
        },
        {"start": "2026-10-16T11:58:00.000Z", "breaks": []},
    ]
    windows = playback.break_windows(programs)
    noon = datetime(2026, 10, 16, 12, tzinfo=UTC)
    assert windows == [
        Window(noon + timedelta(milliseconds=30030), noon + timedelta(milliseconds=60060)),
        Window(noon + timedelta(milliseconds=90090), noon + timedelta(milliseconds=120120)),
    ]


def test_the_cue_point_snaps_to_a_boundary_of_every_variant() -> None:
    """The scenario's cue point is off the segment grid, so the run exercises snapping: it moves to 30030 ms, a
    boundary that every variant of the real TS encode shares (all keep identical EXTINFs; there is no ESAM split)."""
    snapped = STANDARD.grid.snap_to_segment(CUE_POINT_MS)
    assert snapped != CUE_POINT_MS
    assert snapped == 30_030
    variants = [path.read_text() for path in sorted((TS_ENCODE / "content").glob("main_*.m3u8"))]
    assert len(variants) == len(STANDARD.video)
    assert len({tuple(media_segment_durations(variant)) for variant in variants}) == 1
    assert snapped in segment_boundaries_ms(*variants)


def test_the_scenario_plans_one_break_per_program() -> None:
    """The suite's break policy over the 70 s pattern as the tools hand it to the planner: its sidecar cue point is
    its only break offset and is editorial, so the policy's margins and minimum program length do not apply. Three
    breaks, all on the snapped cue point, the second more than a minute after the first (once MediaTailor imported
    the creatives)."""
    cue = STANDARD.grid.snap_to_segment(CUE_POINT_MS)
    pattern = PlanInputAsset(
        asset_id=ASSET_ID,
        vod_source="v",
        duration_ms=70_070,
        break_offsets_ms=[cue],
        editorial_breaks=True,
        ladder_version=STANDARD.version,
    )
    start = 1_792_120_000_000
    plan = plan_block(
        channel="fast-demo",
        ladder_version=STANDARD.version,
        grid=STANDARD.grid,
        start_ms=start,
        assets=[pattern] * PROGRAMS,
        brief=ProgrammingBrief(break_policy=BreakPolicy.model_validate(BREAK_POLICY)),
        now_ms=start - int(LEAD.total_seconds() * 1000),
    )
    assert plan.ok, plan.violations
    epg = [{"start": iso(p.start_ms), "breaks": [b.model_dump() for b in p.breaks]} for p in plan.programs]
    windows = playback.break_windows(epg)
    offsets_aired = [b.offset_ms for p in plan.programs for b in p.breaks]
    assert offsets_aired == [cue] * PROGRAMS
    assert windows[1].starts - windows[0].starts > timedelta(minutes=1)


# --- observation and watch -----------------------------------------------------------------------------------------


def ad(starts: datetime) -> Segment:
    return Segment(f"{CDN}/v1/segment/x/{starts:%H%M%S}", 6.006, starts)


def test_ads_count_only_from_the_second_break() -> None:
    first = Window(datetime(2026, 10, 16, 12, 0, 30, tzinfo=UTC), datetime(2026, 10, 16, 12, 1, tzinfo=UTC))
    second = Window(datetime(2026, 10, 16, 12, 2, tzinfo=UTC), datetime(2026, 10, 16, 12, 2, 30, tzinfo=UTC))
    seen = Observation([first, second])
    seen.ssai["1280x720"] = {s.uri: s for s in [ad(first.starts)]}
    seen.ssai["640x360"] = {s.uri: s for s in [ad(second.starts)]}
    assert seen.ssai_ad_seconds("1280x720") == [6.006, 0]
    assert not seen.ssai_filled("1280x720")
    assert seen.ssai_filled("640x360")
    assert not seen.complete
    assert "1280x720 [6.006, 0]" in seen.report()


def fake_cdn(requests: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    """The stage's CDN answering with the samples (every SSAI media playlist filled with ads)."""

    def answer(request: httpx.Request) -> httpx.Response:
        url, path = str(request.url), request.url.path
        requests.append(url)
        if path.endswith("/v1/master/1bab/cfg/hls.m3u8"):
            name = "synthetic-sgai-multivariant.m3u8" if "GUIDED" in url else "synthetic-ssai-multivariant.m3u8"
            return httpx.Response(200, text=sample(name))
        if path.startswith("/v1/manifest/"):
            return httpx.Response(200, text=sample("synthetic-ssai-ad-break.m3u8"))
        if path.startswith("/v1/i-media/"):
            return httpx.Response(200, text=sample("synthetic-sgai-media.m3u8"))
        if path.startswith("/v1/interstitials/"):
            return httpx.Response(200, text=sample("pab-assetlist.json"))
        if path.startswith("/preconditioned-ads/"):
            return httpx.Response(200, text="#EXTM3U\n")
        return httpx.Response(404)

    return answer


def test_watch_stops_once_every_session_saw_its_ads() -> None:
    requests: list[str] = []
    player = Player(httpx.Client(transport=httpx.MockTransport(fake_cdn(requests)), follow_redirects=True))
    seen = harness.watch(
        player,
        ssai_url=f"{CDN}/v1/master/1bab/cfg/hls.m3u8",
        sgai_url=f"{CDN}/v1/master/1bab/cfg/hls.m3u8?aws.insertionMode=GUIDED",
        breaks=[Window(AD_BREAK.starts - timedelta(minutes=5), AD_BREAK.starts - timedelta(minutes=4)), AD_BREAK],
        ends=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert seen.complete, seen.report()
    assert sorted(seen.ssai) == ["1280x720", "1920x1080", "640x360"]
    assert list(seen.interstitials) == ["1792169754364-34-1F41"]
    assert set(seen.ad_playlists.values()) == {200}
    assert not seen.errors
    [asset_list] = [url for url in requests if "/v1/interstitials/" in url]
    assert "aws.sessionData=%3Cblob%3E" in asset_list  # {$awsSessionData} from the multivariant's EXT-X-DEFINE


def test_watch_records_errors_and_gives_up_at_the_end() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("hls.m3u8"):
            return httpx.Response(200, text=sample("synthetic-ssai-multivariant.m3u8"))
        return httpx.Response(403, text="AccessDenied")

    player = Player(httpx.Client(transport=httpx.MockTransport(answer)))
    seen = harness.watch(
        player,
        ssai_url=f"{CDN}/v1/master/1bab/cfg/hls.m3u8",
        sgai_url=f"{CDN}/v1/master/1bab/cfg/hls.m3u8?aws.insertionMode=GUIDED",
        breaks=[AD_BREAK],
        ends=datetime.now(UTC),
    )
    assert not seen.complete
    assert seen.errors and all("-> 403: AccessDenied" in error for error in seen.errors)


# --- HTTP and tools ------------------------------------------------------------------------------------------------


def test_player_retries_transient_responses_only() -> None:
    statuses = iter([503, 504, 200])
    player = Player(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(next(statuses), text="ok"))))
    assert player.get(f"{CDN}/a").text == "ok"
    player = Player(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404, text="nope"))))
    with pytest.raises(AssertionError, match="404: nope"):
        player.get(f"{CDN}/a")


class FakeLambda:
    """``lambda.invoke`` answering with queued tool envelopes."""

    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        response = dict(self.responses.pop(0))
        response["Payload"] = io.BytesIO(json.dumps(response["Payload"]).encode())
        return response


def test_tools_are_invoked_like_the_gateway_invokes_them() -> None:
    client = FakeLambda({"Payload": {"ok": True, "plan_id": "p"}})
    tools = Tools(client, {"schedule": "fn-schedule"}, "e2e-run")
    assert tools("schedule___plan", channel="fast-demo") == {"ok": True, "plan_id": "p"}
    [call] = client.calls
    assert call["FunctionName"] == "fn-schedule"
    assert json.loads(call["Payload"]) == {"channel": "fast-demo", "correlation_id": "e2e-run"}
    assert json.loads(base64.b64decode(call["ClientContext"])) == {
        "custom": {"bedrockAgentCoreToolName": "schedule___plan"}
    }


def test_tools_retry_retryable_errors_and_raise_the_others() -> None:
    conflict = {"Payload": {"ok": False, "error": {"code": "CONFLICT", "message": "lease held", "retryable": True}}}
    invalid = {
        "Payload": {"ok": False, "error": {"code": "VALIDATION", "message": "bad", "retryable": False, "hint": "fix"}}
    }
    client = FakeLambda(conflict, {"Payload": {"ok": True}}, invalid)
    tools = Tools(client, {"schedule": "fn"}, "e2e-run")
    assert tools("schedule___publish", plan_id="p") == {"ok": True}
    with pytest.raises(ToolError, match="schedule___publish failed with VALIDATION: bad \\(hint: fix\\)") as error:
        tools("schedule___publish", plan_id="p")
    assert (error.value.code, error.value.retryable) == ("VALIDATION", False)
    crash = FakeLambda({"Payload": {"errorMessage": "boom"}, "FunctionError": "Unhandled"})
    with pytest.raises(AssertionError, match="the Lambda function failed"):
        Tools(crash, {"channels": "fn"}, "e2e-run")("channels___start_channel", channel="x")


def test_wait_for_names_what_and_the_last_state() -> None:
    states = iter([None, None, "READY"])
    assert harness.wait_for(lambda: next(states), timeout=10, interval=0, what="ready") == "READY"
    with pytest.raises(AssertionError, match=r"waiting for the asset; last seen: \{'status': 'INGESTING'\}"):
        harness.wait_for(lambda: None, timeout=0, interval=0, what="the asset", state=lambda: {"status": "INGESTING"})


def test_retrying_gives_up_after_its_attempts() -> None:
    calls: list[int] = []

    def flaky() -> int:
        calls.append(1)
        raise TimeoutError

    with pytest.raises(TimeoutError):
        harness.retrying(flaky, retry=lambda e: isinstance(e, TimeoutError), attempts=3)
    assert len(calls) == 3

    def missing() -> int:
        calls.append(1)
        raise KeyError("x")

    calls.clear()
    with pytest.raises(KeyError):
        harness.retrying(missing, retry=lambda e: isinstance(e, TimeoutError))
    assert len(calls) == 1


def test_channel_urls_are_those_of_the_hls_output() -> None:
    channel = Channel.of(
        {
            "name": "fast-demo",
            "ladderVersion": "standard@843ed1d05a",
            "outputs": ["hls"],
            "playback": {"hls": {"ssai": "u/hls", "sgai": "g/hls"}},
        }
    )
    assert (channel.url("ssai"), channel.url("sgai")) == ("u/hls", "g/hls")
    with pytest.raises(AssertionError, match="no dash playback URL for hls"):
        channel.url("dash")


# --- agent ---------------------------------------------------------------------------------------------------------


SSE = [
    'data: {"type":"RUN_STARTED"}',
    "",
    ": keep-alive",
    "event: message",
    'data: {"type":"TOOL_CALL_START",',
    'data: "toolCallName":"channels___list_channels"}',
    "",
    'data: {"type":"RUN_FINISHED"}',
    "",
    'data: {"type":"UNTERMINATED"}',
]


def test_sse_events_and_tool_calls() -> None:
    events = list(agui.events(SSE))
    assert [e["type"] for e in events] == ["RUN_STARTED", "TOOL_CALL_START", "RUN_FINISHED"]
    assert agui.tool_calls(events) == ["channels___list_channels"]


def test_run_input_and_session_id() -> None:
    session = agui.session_id()
    assert len(session) >= 33
    body = agui.run_input("List the channels.", thread_id=session)
    assert body["threadId"] == session
    assert body["messages"][0] | {"id": ""} == {"id": "", "role": "user", "content": "List the channels."}
    assert (body["tools"], body["context"], body["state"]) == ([], [], {})


def test_ask_agent_reads_the_stream_also_on_platform_errors() -> None:
    seen: list[httpx.Request] = []

    def runtime(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers["Authorization"] == "Bearer bad":
            return httpx.Response(403, text='data: {"type":"RUN_ERROR","code":"ACCESS_DENIED"}\n\n')
        return httpx.Response(200, text="\n".join(SSE) + "\n")

    http = httpx.Client(transport=httpx.MockTransport(runtime))
    events = harness.ask_agent(http, invoke_url="https://agentcore/invocations", token="t", prompt="List the channels.")
    assert agui.tool_calls(events) == ["channels___list_channels"]
    request = seen[0]
    assert request.headers[agui.SESSION_HEADER] == json.loads(request.content)["threadId"]
    assert request.headers["Accept"] == "text/event-stream"
    assert harness.ask_agent(http, invoke_url="https://agentcore/invocations", token="bad", prompt="x") == [
        {"type": "RUN_ERROR", "code": "ACCESS_DENIED"}
    ]


def test_machine_token_uses_the_client_credentials_grant() -> None:
    def cognito(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://fast-1.auth.us-east-1.amazoncognito.com/oauth2/token"
        assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"client:secret").decode()
        assert request.content == b"grant_type=client_credentials&scope=fast-channels%2Fagent.invoke"
        return httpx.Response(200, json={"access_token": "token", "token_type": "Bearer"})

    http = httpx.Client(transport=httpx.MockTransport(cognito))
    token = harness.machine_token(
        http,
        domain="fast-1.auth.us-east-1.amazoncognito.com",
        client_id="client",
        client_secret="secret",
        scope=AGENT_SCOPE,
    )
    assert token == "token"


def test_agent_scope_matches_the_identity_construct() -> None:
    assert harness.AGENT_SCOPE == AGENT_SCOPE


# --- synthetic source ----------------------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="needs ffmpeg and ffprobe")
def test_render_test_pattern(tmp_path: Path) -> None:
    path = harness.render_test_pattern(tmp_path / "pattern.mp4", seconds=2)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height,r_frame_rate,channels,sample_rate",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    video, audio = json.loads(probe.stdout)["streams"]
    assert (video["codec_name"], video["width"], video["height"], video["r_frame_rate"]) == ("h264", 1920, 1080, "30/1")
    assert (audio["codec_name"], audio["channels"], audio["sample_rate"]) == ("aac", 2, "48000")
