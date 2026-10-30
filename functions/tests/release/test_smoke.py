"""Probe sessions against a candidate sibling: opening one, watching it for breaks, and the verdict from its logs."""

import io
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from fast_channels.functions.release import smoke

type Stub = Callable[[str], Stubber]
PREFIX = "https://abc123.mediatailor.us-east-1.amazonaws.com/v1/master/0123abcd/fastm-demo-p-b/"
SESSION = "11111111-2222-3333-4444-555555555555"
MASTER = (
    "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360\n"
    f"../../../manifest/0123abcd/fastm-demo-p-b/{SESSION}/0.m3u8\n"
)
MARKER = '#EXT-X-DATERANGE:ID="{id}",START-DATE="2026-10-29T12:00:00Z",SCTE35-OUT=0xFC\n'


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Media playlists the session is served, one per poll (the last one repeats)."""
    playlists: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("hls.m3u8"):
            return httpx.Response(200, text=MASTER)
        text = playlists.pop(0) if len(playlists) > 1 else playlists[0]
        return httpx.Response(200, text=text)

    original = httpx.Client
    monkeypatch.setattr(smoke.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(answer), **kw))
    monkeypatch.setattr(smoke.time, "sleep", lambda seconds: None)
    return playlists


def test_open_finds_the_session_of_the_first_variant(stub: Stub, web: list[str]) -> None:
    stub("mediatailor").add_response(
        "get_playback_configuration",
        {"HlsConfiguration": {"ManifestEndpointPrefix": PREFIX}},
        {"Name": "fastm-demo-p-b"},
    )
    web.append(MARKER.format(id="in-progress"))  # the break the session joined during
    session = smoke.open_session("fastm-demo-p-b")
    assert session["session"] == SESSION and session["markers"] == [] and session["joined"] == ["in-progress"]
    assert session["playlist"].endswith(f"/v1/manifest/0123abcd/fastm-demo-p-b/{SESSION}/0.m3u8")


def test_watch_counts_distinct_markers_until_enough(web: list[str]) -> None:
    web += [
        "#EXTM3U\n",
        MARKER.format(id="e1"),
        MARKER.format(id="e1"),
        MARKER.format(id="e1") + MARKER.format(id="e2"),
    ]
    session = {"sibling": "s", "playlist": "https://x/v1/manifest/h/s/id/0.m3u8", "session": "id", "markers": []}
    watched = smoke.watch(session, breaks=2)
    assert watched["markers"] == ["e1", "e2"] and watched["done"]
    web[:] = [MARKER.format(id="e0") + MARKER.format(id="e1") + MARKER.format(id="e2")]
    joined = smoke.watch(session | {"joined": ["e0"]}, breaks=2)
    assert joined["markers"] == ["e1", "e2"], "a break joined during is not counted"
    web[:] = ["#EXTM3U\n"]
    assert not smoke.watch(session, breaks=2, seconds=0)["done"]


def test_judge_reads_the_sessions_events(stub: Stub) -> None:
    logs = stub("logs")
    logs.add_response(
        "start_query",
        {"queryId": "q1"},
        {
            "logGroupNames": ["/aws/vendedlogs/mediatailor/fastm-release"],
            "startTime": ANY,
            "endTime": ANY,
            "queryString": ANY,
        },
    )

    def row(**cells: str) -> list[dict[str, str]]:
        return [{"field": k, "value": v} for k, v in cells.items()]

    first, second = "1793303950308-34-633AB926", "1793304462386-34-466E9F57"
    rows = [
        row(eventType="AD_MARKER_FOUND", marker=first, breakId="b1"),
        row(eventType="MAKING_ADS_REQUEST", breakId="b1"),
        row(eventType="FILLED_AVAIL", breakId="b1"),
        row(eventType="AD_MARKER_FOUND", marker=second, breakId="b2"),
        row(eventType="MAKING_ADS_REQUEST", breakId="b2"),
        row(eventType="AD_MARKER_FOUND", marker=second, breakId="b2-done", completed=",DURATION="),
        row(eventType="AD_MARKER_FOUND", marker="joined", breakId="b0"),  # the break the session joined during
    ]
    logs.add_response("get_query_results", {"status": "Running"}, {"queryId": "q1"})
    logs.add_response("get_query_results", {"status": "Complete", "results": rows}, {"queryId": "q1"})
    logs.add_response(
        "start_query",
        {"queryId": "q2"},
        {"logGroupNames": ["/aws/lambda/pds-policy"], "startTime": ANY, "endTime": ANY, "queryString": ANY},
    )
    logs.add_response(
        "get_query_results", {"status": "Complete", "results": [row(evid="1664792870")]}, {"queryId": "q2"}
    )
    session = {
        "sibling": "fastm-demo-p-b",
        "session": SESSION,
        "started": 1_700_000_000.0,
        "joined": ["joined"],
        "markers": [first, second],
    }
    result: dict[str, Any] = smoke.judge(session, breaks=2, probe=False)
    assert result["failures"] == ("1 breaks were not filled", "1 breaks without a decision record")
    assert not result["passed"] and result["decisions"] == 1 and result["breaks"] == 2


def test_no_decision_rows_count_zero(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = stub("logs")
    logs.add_response("start_query", {"queryId": "q3"}, None)
    logs.add_response("get_query_results", {"status": "Complete", "results": []}, {"queryId": "q3"})
    assert smoke.decisions(SESSION, 0, 1) == set()


def test_a_failed_query_raises(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke.time, "sleep", lambda seconds: None)
    logs = stub("logs")
    logs.add_response("start_query", {"queryId": "q2"}, None)
    logs.add_response("get_query_results", {"status": "Failed"}, {"queryId": "q2"})
    with pytest.raises(RuntimeError, match="ended Failed"):
        smoke.events(SESSION, 0, 1)


def tool_answer(body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode()
    return {"StatusCode": 200, "Payload": StreamingBody(io.BytesIO(data), len(data))}


def test_the_lab_channel_is_scheduled_started_and_stopped_through_the_agent_tools(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "TOOL_FUNCTIONS", json.dumps({"channels": "f-channels", "library": "f-library", "schedule": "f-schedule"})
    )
    smoke.settings.cache_clear()
    lam = stub("lambda")
    assets = [{"asset_id": a} for a in ("sintel", "tears", "bbb", "spring")]

    def planned(plan_id: str, *breaks: int) -> dict[str, Any]:
        return {"ok": True, "plan_id": plan_id, "programs": [{"breaks": [{}] * n} for n in breaks]}

    calls = [
        ("f-channels", {"ok": True, "ladder_version": "standard@0123456789"}),
        ("f-library", {"ok": True, "assets": assets}),
        ("f-schedule", planned("fastm-lab.1", 3)),
        ("f-schedule", planned("fastm-lab.2", 3, 2)),  # 5: fewer than the smoke test and the soak need
        ("f-schedule", planned("fastm-lab.3", 3, 2, 4)),
        ("f-schedule", {"ok": True, "published": 3}),
        ("f-channels", {"ok": True, "state": "RUNNING"}),
    ]
    for function, body in calls:
        lam.add_response("invoke", tool_answer(body), {"FunctionName": function, "Payload": ANY, "ClientContext": ANY})
    started = smoke.lab_start("fastm-lab", "c9")
    assert started["lab"] == "fastm-lab" and started["plan_id"] == "fastm-lab.3" and started["breaks"] == 9
    lam.assert_no_pending_responses()
    lam.add_response("invoke", tool_answer({"ok": True, "state": "STOPPED"}), None)
    assert smoke.lab_stop("fastm-lab", "c9") == {"lab": "fastm-lab", "stopped": True}
    lam.add_response("invoke", tool_answer({"ok": False, "error": {"code": "NOT_FOUND"}}), None)
    with pytest.raises(smoke.LabError, match="NOT_FOUND"):
        smoke.lab_stop("fastm-lab", "c9")
    lam.add_response("invoke", tool_answer({"ok": True, "ladder_version": "standard@0123456789"}), None)
    lam.add_response("invoke", tool_answer({"ok": True, "assets": []}), None)
    with pytest.raises(smoke.LabError, match="no READY content"):
        smoke.lab_start("fastm-lab", "c9")


def test_the_handler_dispatches(monkeypatch: pytest.MonkeyPatch, context: Any) -> None:
    calls: list[str] = []

    def recorder(name: str) -> Callable[..., dict[str, str]]:
        def record(*_: Any, **__: Any) -> dict[str, str]:
            calls.append(name)
            return {"step": name}

        return record

    for name in ("open_session", "watch", "judge", "lab_start", "lab_stop"):
        monkeypatch.setattr(smoke, name, recorder(name))
    for step in ("open", "watch", "judge", "lab-start", "lab-stop"):
        event = {"step": step, "sibling": "s", "session": {}, "breaks": 2, "lab": "fastm-lab", "change_id": "c"}
        smoke.handler(event, context)
    assert calls == ["open_session", "watch", "judge", "lab_start", "lab_stop"]
    with pytest.raises(ValueError, match="unknown step 'x'"):
        smoke.handler({"step": "x"}, context)


@pytest.mark.parametrize(
    ("master", "message"),
    [("#EXTM3U\n", "has no variant"), ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nplain.m3u8\n", "no session id")],
)
def test_open_refuses_a_playlist_it_cannot_follow(
    stub: Stub, monkeypatch: pytest.MonkeyPatch, master: str, message: str
) -> None:
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=master))
    monkeypatch.setattr(smoke.httpx, "Client", lambda **kw: original(transport=transport, **kw))
    stub("mediatailor").add_response(
        "get_playback_configuration", {"HlsConfiguration": {"ManifestEndpointPrefix": PREFIX}}, None
    )
    with pytest.raises(ValueError, match=message):
        smoke.open_session("fastm-demo-p-b")


def test_a_request_that_reached_its_timeout_fails_the_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = "1793303950308-34-633AB926"
    healthy = [
        {"eventType": "AD_MARKER_FOUND", "marker": marker, "breakId": "b1"},
        {"eventType": "MAKING_ADS_REQUEST", "breakId": "b1"},
        {"eventType": "FILLED_AVAIL", "breakId": "b1"},
    ]
    asked: list[tuple[str, int, str]] = []

    def timed_out_steps(bundle_key: str, release: int, sibling: str, *_: Any) -> dict[str, dict[str, int]]:
        asked.append((bundle_key, release, sibling))
        return {"fastm_pa_lookup_r0004": {"timeout_ms": 1000, "minutes": 2}}

    monkeypatch.setattr(smoke, "events", lambda *_: healthy)
    monkeypatch.setattr(smoke, "decisions", lambda *_: {"1664792870"})
    monkeypatch.setattr(smoke.promote, "timed_out_steps", timed_out_steps)
    session = {"sibling": "fastm-demo-p-b", "session": SESSION, "started": 1_700_000_000.0, "markers": [marker]}
    assert smoke.judge(session, breaks=1, probe=False)["passed"]  # without the release, latency is not read
    result = smoke.judge(session, breaks=1, probe=False, bundle_key="release/d/bundle.json", release=4)
    assert result["failures"] == ("PRE_ADS request fastm_pa_lookup_r0004 reached its 1000 ms timeout in 2 minutes",)
    assert not result["passed"] and asked == [("release/d/bundle.json", 4, "fastm-demo-p-b")]
