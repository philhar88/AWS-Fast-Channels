"""Offline tests of the e2e helpers (not marked e2e): manifest readers against wire samples of the research run
(``data/``, trimmed captures from e2e.md), the watcher against a fake CDN, tool calls against a fake Lambda."""

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
from fast_channels.core.specs import Creative, vast_pod
from fast_channels.core.timeline import BreakPolicy, PlanInputAsset, ProgrammingBrief, plan_block
from fast_channels.functions.tools._library_store import iso
from fast_channels.infra.constructs.operator.identity import AGENT_SCOPE

DATA = Path(__file__).parent / "data"
CDN = "https://d3pzfwsg0kjqjs.cloudfront.net"
HASH = "1bab6b4238868b752a9e9daae3e2a4911f2a2fc9"
"""MediaTailor's customer hash in the playback URLs of the research run."""
MANIFEST = f"{CDN}/v1/manifest/1bab/fastv3-exp-e2e-cdn/c14ebd60/0.m3u8"
AD_BREAK = Window(datetime(2026, 10, 16, 0, 44, 45, 977000, UTC), datetime(2026, 10, 16, 0, 44, 55, 977000, UTC))
"""The break the cdn-avc-ad sample fills with two preconditioned ads (6 s + 4 s)."""


def sample(name: str) -> str:
    return (DATA / name).read_text()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)


# --- playback ------------------------------------------------------------------------------------------------------


def test_renditions_pick_one_variant_per_codec_family_and_audio_group() -> None:
    base = f"{CDN}/v1/master/1bab/fastv3-exp-e2e-ssai/hls.m3u8"
    picked = playback.renditions(sample("ssai-main-multivariant.m3u8"), base)
    assert sorted(picked) == ["audio:aac-lc-128k", "audio:he-aac-64k", "av1", "avc", "hevc"]
    session = f"{CDN}/v1/manifest/{HASH}/fastv3-exp-e2e-ssai/49f0c250-9e3c-4997-a723-3cd95eff9d42"
    assert picked["av1"] == f"{session}/0.m3u8"
    assert picked["avc"].endswith("/10.m3u8") and picked["audio:he-aac-64k"].endswith("/17.m3u8")


def test_codec_families_and_tags() -> None:
    multivariant = sample("ssai-main-multivariant.m3u8")
    assert playback.video_codecs(multivariant) == {"avc", "hevc", "av1"}
    assert playback.codec_tags(multivariant) == {"avc1", "hvc1", "av01"}


def test_stitched_ads_are_told_from_content_by_their_uris() -> None:
    segments = playback.media_segments(sample("cdn-avc-ad.m3u8"), MANIFEST)
    ads = [s for s in segments if s.is_ad]
    assert [(s.duration, s.starts) for s in ads] == [
        (6.0, AD_BREAK.starts),
        (4.0, AD_BREAK.starts + timedelta(seconds=6)),
    ]
    assert ads[0].uri == f"{CDN}/v1/segment/{HASH}/fastv3-exp-e2e-cdn/c14ebd60-0c10-4a48-b428-5fb03cfd8d63/0/151"
    assert ads[0].map_uri == f"{CDN}/ads/e2e/pre/crA6/cmaf/index_avc_1080pinit.cmfv"
    assert all(s.uri.startswith(f"{CDN}/vod/") for s in segments if not s.is_ad)
    assert playback.ad_seconds(segments, AD_BREAK) == 10.0


def test_a_break_filled_with_the_slate_has_no_ad_seconds() -> None:
    segments = playback.media_segments(sample("ssai-avc-ad.m3u8"), MANIFEST)
    slate = Window(datetime(2026, 10, 16, 0, 34, 55, 977000, UTC), datetime(2026, 10, 16, 0, 35, 5, 977000, UTC))
    assert any("/vod/slate/" in s.uri for s in segments)
    assert playback.ad_seconds(segments, slate) == 0


def test_program_date_time_carries_forward_by_duration() -> None:
    text = "#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:2026-10-16T12:00:00.000Z\n#EXTINF:6.0,\na.cmfv\n#EXTINF:2.0,\nb.cmfv\n"
    [first, second] = playback.media_segments(text, f"{CDN}/x/index.m3u8")
    assert second.starts == first.ends == datetime(2026, 10, 16, 12, 0, 6, tzinfo=UTC)
    assert second.uri == f"{CDN}/x/b.cmfv"


def test_interstitial_asset_list_url_with_session_variables() -> None:
    [(daterange, attributes)] = playback.interstitials(sample("sgai-main-avc1080-imedia.m3u8")).items()
    assert daterange == "1792110895977-101-B"
    variables = playback.defines(sample("sgai-main-multivariant.m3u8"))
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


def test_mpd_ad_periods_and_codecs() -> None:
    mpd = playback.parse_mpd(sample("pre-dash.mpd"))
    assert mpd.type == "dynamic"
    assert mpd.ad_periods == ("1792110925977_1_1",)
    assert set(mpd.ad_periods) < set(mpd.periods)
    assert {"avc1.640028", "hvc1.2.4.L120.B0", "av01.0.08M.10"} <= mpd.codecs


def test_vast_pod_media_files_and_creatives() -> None:
    creatives = [
        Creative(
            creative_id=f"house-{n}",
            duration_ms=14014,
            hls_uri=f"{CDN}/ads/{n}/main.m3u8",
            dash_uri=f"{CDN}/ads/{n}/main_avc.mpd",
        )
        for n in (1, 2)
    ]
    pod = vast_pod(creatives=creatives, content_base_url=CDN)
    assert playback.vast_ads(pod) == ["house-1", "house-2"]
    files = playback.vast_media_files(pod)
    assert [(f.mime_type, f.uri) for f in files[:2]] == [
        ("application/x-mpegURL", f"{CDN}/ads/1/main.m3u8"),
        ("application/dash+xml", f"{CDN}/ads/1/main_avc.mpd"),
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


def test_the_scenario_plans_one_break_per_program() -> None:
    """The suite's break policy over the 70 s pattern (break offsets on the 6.006 s segment grid): three breaks,
    the first on the cue point, the second more than a minute later (once MediaTailor imported the creatives)."""
    ladder = Ladder.builtin("standard")
    offsets = [ladder.grid.segment_ms * n for n in range(1, 12)]
    pattern = PlanInputAsset(
        asset_id=ASSET_ID, vod_source="v", duration_ms=70_070, break_offsets_ms=offsets, ladder_version=ladder.version
    )
    start = 1_792_120_000_000
    plan = plan_block(
        channel="fast-demo",
        ladder_version=ladder.version,
        grid=ladder.grid,
        start_ms=start,
        assets=[pattern] * PROGRAMS,
        brief=ProgrammingBrief(break_policy=BreakPolicy.model_validate(BREAK_POLICY)),
        now_ms=start - int(LEAD.total_seconds() * 1000),
    )
    assert plan.ok, plan.violations
    epg = [{"start": iso(p.start_ms), "breaks": [b.model_dump() for b in p.breaks]} for p in plan.programs]
    windows = playback.break_windows(epg)
    offsets_aired = [b.offset_ms for p in plan.programs for b in p.breaks]
    assert offsets_aired == [ladder.grid.snap_to_gop(CUE_POINT_MS), offsets[0], offsets[0]]
    assert windows[1].starts - windows[0].starts > timedelta(minutes=1)


# --- observation and watch -----------------------------------------------------------------------------------------


def ad(starts: datetime) -> Segment:
    return Segment(f"{CDN}/v1/segment/x/{starts:%H%M%S}", 6.0, starts, f"{CDN}/ads/a/init.cmfv")


def test_ads_count_only_from_the_second_break() -> None:
    first = Window(datetime(2026, 10, 16, 12, 0, 30, tzinfo=UTC), datetime(2026, 10, 16, 12, 1, tzinfo=UTC))
    second = Window(datetime(2026, 10, 16, 12, 2, tzinfo=UTC), datetime(2026, 10, 16, 12, 2, 30, tzinfo=UTC))
    seen = Observation([first, second])
    seen.ssai["avc"] = {s.uri: s for s in [ad(first.starts)]}
    seen.ssai["hevc"] = {s.uri: s for s in [ad(second.starts)]}
    assert seen.ssai_ad_seconds("avc") == [6.0, 0]
    assert not seen.ssai_filled("avc")
    assert seen.ssai_filled("hevc")
    assert not seen.complete
    assert "avc [6.0, 0]" in seen.report()


def fake_cdn(requests: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    """The stage's CDN answering with the research samples (each media playlist once filled with ads)."""

    def answer(request: httpx.Request) -> httpx.Response:
        url, path = str(request.url), request.url.path
        requests.append(url)
        if path.endswith("/v1/master/1bab/cfg/hls.m3u8"):
            name = "sgai-main-multivariant.m3u8" if "GUIDED" in url else "ssai-main-multivariant.m3u8"
            return httpx.Response(200, text=sample(name))
        if path.startswith("/v1/manifest/"):
            return httpx.Response(200, text=sample("cdn-avc-ad.m3u8"))
        if path.startswith("/v1/i-media/"):
            return httpx.Response(200, text=sample("sgai-main-avc1080-imedia.m3u8"))
        if path.startswith("/v1/interstitials/"):
            return httpx.Response(200, text=sample("pab-assetlist.json"))
        if path.startswith("/preconditioned-ads/"):
            return httpx.Response(200, text="#EXTM3U\n")
        if path.endswith("dash-avc.mpd"):
            if "aws.sessionId" not in url:
                return httpx.Response(302, headers={"Location": f"{url}?aws.sessionId=s1"})
            return httpx.Response(200, text=sample("pre-dash.mpd"))
        return httpx.Response(404)

    return answer


def test_watch_stops_once_every_session_saw_its_ads() -> None:
    requests: list[str] = []
    player = Player(httpx.Client(transport=httpx.MockTransport(fake_cdn(requests)), follow_redirects=True))
    seen = harness.watch(
        player,
        ssai_url=f"{CDN}/v1/master/1bab/cfg/hls.m3u8",
        sgai_url=f"{CDN}/v1/master/1bab/cfg/hls.m3u8?aws.insertionMode=GUIDED",
        dash_url=f"{CDN}/v1/dash/1bab/cfg/dash-avc.mpd",
        breaks=[Window(AD_BREAK.starts - timedelta(minutes=5), AD_BREAK.starts - timedelta(minutes=4)), AD_BREAK],
        ends=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert seen.complete, seen.report()
    assert sorted(seen.ssai) == ["audio:aac-lc-128k", "audio:he-aac-64k", "av1", "avc", "hevc"]
    assert list(seen.interstitials) == ["1792110895977-101-B"]
    assert set(seen.ad_playlists.values()) == {200}
    assert seen.dash_ad_periods == {"1792110925977_1_1"}
    assert not seen.errors
    [asset_list] = [url for url in requests if "/v1/interstitials/" in url]
    assert "aws.sessionData=%3Cblob%3E" in asset_list  # {$awsSessionData} from the multivariant's EXT-X-DEFINE


def test_watch_records_errors_and_gives_up_at_the_end() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("hls.m3u8"):
            return httpx.Response(200, text=sample("ssai-main-multivariant.m3u8"))
        if request.url.path.endswith(".mpd"):
            return httpx.Response(200, text=sample("pre-dash.mpd"))
        return httpx.Response(403, text="AccessDenied")

    player = Player(httpx.Client(transport=httpx.MockTransport(answer)))
    seen = harness.watch(
        player,
        ssai_url=f"{CDN}/v1/master/1bab/cfg/hls.m3u8",
        sgai_url=f"{CDN}/v1/master/1bab/cfg/hls.m3u8?aws.insertionMode=GUIDED",
        dash_url=f"{CDN}/v1/dash/1bab/cfg/dash-avc.mpd",
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


def test_channel_urls_prefer_the_combined_output() -> None:
    channel = Channel.of(
        {
            "name": "fast-demo",
            "ladderVersion": "standard@86bf8088e0",
            "outputs": ["hls-avc", "hls", "dash-avc"],
            "playback": {
                "hls-avc": {"ssai": "u/hls-avc", "sgai": "g/hls-avc"},
                "hls": {"ssai": "u/hls", "sgai": "g/hls"},
                "dash-avc": {"dash": "d/dash-avc"},
            },
        }
    )
    assert (channel.url("ssai"), channel.url("sgai"), channel.url("dash")) == ("u/hls", "g/hls", "d/dash-avc")
    with pytest.raises(AssertionError, match="no mss playback URL"):
        channel.url("mss")


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
