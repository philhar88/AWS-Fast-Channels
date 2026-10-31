"""The probe canary's script (``functions/canary/python/fast_probe.py``), with playlists served from memory."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "canary" / "python" / "fast_probe.py"
MULTIVARIANT = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
0.m3u8?aws.sessionId=s1
#EXT-X-STREAM-INF:BANDWIDTH=800000
1.m3u8?aws.sessionId=s1
"""


def media(sequence: int, *, daterange: str = "") -> str:
    return (
        f"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:{sequence}\n{daterange}"
        "#EXTINF:6.0,\nseg-1.ts\n#EXTINF:6.0,\nhttps://ads.example.com/ad-1.ts\n"
    )


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("fast_probe", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    return module


def serve(probe: ModuleType, monkeypatch: pytest.MonkeyPatch, pages: dict[str, list[str]]) -> list[str]:
    """Answer ``fetch`` from ``pages`` (each URL's bodies in turn); returns the fetched URLs."""
    fetched: list[str] = []

    def fetch(url: str, *, first_bytes: int | None = None) -> tuple[str, bytes]:
        fetched.append(url)
        bodies = pages[url.split("?", maxsplit=1)[0]]
        return url, (bodies.pop(0) if len(bodies) > 1 else bodies[0]).encode()

    monkeypatch.setattr(probe, "fetch", fetch)
    return fetched


BASE = "https://cdn.example.com/v1/master/h/fastm-demo/out/v1/fastm-demo/hls"


def test_a_channel_on_air_passes(probe: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    daterange = '#EXT-X-DATERANGE:ID="1793-1-ABC",START-DATE="2026-10-29T12:00:00.000Z",DURATION=60.06\n'
    fetched = serve(
        probe,
        monkeypatch,
        {
            f"{BASE}/index.m3u8": [MULTIVARIANT],
            f"{BASE}/0.m3u8": [media(10), media(11, daterange=daterange)],
            "https://ads.example.com/ad-1.ts": ["ts bytes"],
        },
    )

    result = probe.probe("fastm-demo", f"{BASE}/index.m3u8")

    assert result == {"channel": "fastm-demo", "media_sequence": [10, 11], "dateranges": 1}
    assert fetched[-1] == "https://ads.example.com/ad-1.ts"  # the newest segment resolves


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ({"index": ["#EXTM3U\n"]}, "lists no variant"),
        ({"0": [media(10), media(10)]}, "media sequence stayed at 10"),
        ({"0": ["#EXTM3U\n"]}, "no EXT-X-MEDIA-SEQUENCE"),
        ({"0": [media(10), "#EXT-X-MEDIA-SEQUENCE:11\n"]}, "lists no segment"),
        ({"0": [media(10), media(11, daterange='#EXT-X-DATERANGE:START-DATE="x"\n')]}, "has no ID"),
        ({"0": [media(10), media(11, daterange='#EXT-X-DATERANGE:ID="a",START-DATE="soon"\n')]}, "no valid START"),
        ({"ad": [""]}, "the newest segment is empty"),
    ],
)
def test_a_failing_check_raises(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch, pages: dict[str, list[str]], message: str
) -> None:
    served = {
        f"{BASE}/index.m3u8": pages.get("index", [MULTIVARIANT]),
        f"{BASE}/0.m3u8": pages.get("0", [media(10), media(11)]),
        "https://ads.example.com/ad-1.ts": pages.get("ad", ["ts"]),
    }
    serve(probe, monkeypatch, served)

    with pytest.raises(probe.ProbeError, match=message):
        probe.probe("fastm-demo", f"{BASE}/index.m3u8")


class Clients:
    def __init__(self, running: str, states: dict[str, str]) -> None:
        self.running, self.states = running, states

    def __call__(self, service: str) -> Clients:
        return self

    def get_parameter(self, *, Name: str) -> dict[str, Any]:  # noqa: N803
        return {"Parameter": {"Value": self.running}}

    def describe_channel(self, *, ChannelName: str) -> dict[str, str]:  # noqa: N803
        return {"ChannelState": self.states[ChannelName]}


def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CENSUS_PARAMETER", "/fastm/ops/running-channels")
    monkeypatch.setenv("CHANNELS", json.dumps({"fastm-demo": f"{BASE}/index.m3u8"}))


def test_with_no_running_channel_the_run_passes_skipped(probe: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    environment(monkeypatch)
    monkeypatch.setattr(probe.boto3, "client", Clients("0", {}))
    assert probe.handler({}, None) == {"skipped": True}

    monkeypatch.setattr(probe.boto3, "client", Clients("1", {"fastm-demo": "STOPPED"}))
    assert probe.handler({}, None) == {"skipped": True}


def test_the_handler_probes_running_channels(probe: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    environment(monkeypatch)
    monkeypatch.setattr(probe.boto3, "client", Clients("1", {"fastm-demo": "RUNNING"}))
    monkeypatch.setattr(probe, "probe", lambda name, url: {"channel": name})

    assert probe.handler({}, None) == {"skipped": False, "probed": [{"channel": "fastm-demo"}]}


def test_a_failed_channel_fails_the_run(probe: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    environment(monkeypatch)
    monkeypatch.setattr(probe.boto3, "client", Clients("1", {"fastm-demo": "RUNNING"}))

    def stalled(name: str, url: str) -> dict[str, Any]:
        raise probe.ProbeError("stalled")

    monkeypatch.setattr(probe, "probe", stalled)

    with pytest.raises(probe.ProbeError, match="fastm-demo: stalled"):
        probe.handler({}, None)


def test_fetch_asks_for_the_first_bytes(probe: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def geturl(self) -> str:
            return "https://final.example.com/x"

        def read(self) -> bytes:
            return b"body"

    def urlopen(request: Any, timeout: float) -> Response:
        seen.append((request.full_url, dict(request.header_items()), timeout))
        return Response()

    monkeypatch.setattr(probe.urllib.request, "urlopen", urlopen)

    assert probe.fetch("https://cdn.example.com/a.ts", first_bytes=1024) == ("https://final.example.com/x", b"body")
    assert seen[0][1]["Range"] == "bytes=0-1023"
    assert seen[0][2] == probe.TIMEOUT_S
