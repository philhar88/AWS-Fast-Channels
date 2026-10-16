"""Building blocks of the deployed end-to-end suite: patient waiting, the tool Lambdas, player-like HTTP, the agent
and the synthetic source. Every wait has a deadline and fails with what it saw last."""

import base64
import json
import random
import shutil
import subprocess
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import urljoin

import agui
import httpx
from playback import (
    Mpd,
    Segment,
    Window,
    ad_seconds,
    asset_list,
    defines,
    interstitials,
    media_segments,
    parse_mpd,
    renditions,
    substitute,
)

from fast_channels.functions.tools.registry import CORRELATION_FIELD, SEPARATOR

# The scenario: a 70 s test pattern with a cue point, aired three times with one break each.
CONTENT_ID: Final = "e2e-pattern"
ASSET_ID: Final = f"{CONTENT_ID}-{datetime.now(UTC):%Y%m%d%H%M%S}"
"""A new asset per run: the pipeline refuses to re-encode a READY asset in place (its VOD source serves that media)."""
PATTERN_SECONDS: Final = 70
CUE_POINT_MS: Final = 30_000
PROGRAMS: Final = 3
LEAD: Final = timedelta(minutes=2, seconds=30)
"""From planning to the first program: publishing and starting the channel fit in it."""
BREAK_POLICY: Final = {"cadence_minutes": 0.5, "min_gap_minutes": 0.25, "max_breaks": 1, "slate_gops": 15}
"""One 30 s break per program, three in five minutes. The first break of new creatives airs its slate while
MediaTailor imports them (NEW_CREATIVE), so ad fill is asserted from the second break on."""

USER_AGENT: Final = "fast-channels-e2e/3.0"
POLL_SECONDS: Final = 4.0
"""Media playlists are re-read this often, well within their 60 s window."""
AGENT_SCOPE: Final = "fast-channels/agent.invoke"
"""OAuth scope of the runtime and Gateway (``infra.constructs.operator.identity.AGENT_SCOPE``)."""
TRANSIENT_STATUS: Final = frozenset({429, 500, 502, 503, 504})
"""MediaTailor answers 504 while a session's origin is still warming up (e2e.md §6.1)."""


def wait_for[T](
    probe: Callable[[], T | None],
    *,
    timeout: float,
    interval: float,
    what: str,
    state: Callable[[], object] | None = None,
) -> T:
    """``probe()`` once it returns something other than None; fails after ``timeout`` seconds, naming ``what`` and
    the last ``state()``."""
    deadline = time.monotonic() + timeout
    while (result := probe()) is None:
        if time.monotonic() >= deadline:
            last = f"; last seen: {state()}" if state else ""
            raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}{last}")
        time.sleep(interval)
    return result


def retrying[T](
    call: Callable[[], T], *, retry: Callable[[Exception], bool], attempts: int = 6, delay: float = 2.0
) -> T:
    """``call()``, repeated with jittered exponential backoff (``delay``, x2, at most 60 s) while ``retry(error)``,
    at most ``attempts`` times."""
    for attempt in range(1, attempts):
        try:
            return call()
        except Exception as error:
            if not retry(error):
                raise
            time.sleep(min(delay * 2 ** (attempt - 1), 60.0) * random.uniform(0.8, 1.2))
    return call()


def until(moment: datetime) -> None:
    """Sleep until ``moment`` (aware)."""
    if (seconds := (moment - datetime.now(UTC)).total_seconds()) > 0:
        time.sleep(seconds)


@dataclass(frozen=True, slots=True)
class Channel:
    """An IaC channel of the stage: an entry of the Media output ``Channels``."""

    name: str
    ladder_version: str
    outputs: tuple[str, ...]
    playback: Mapping[str, Mapping[str, str]]
    """Viewer URLs per output: ``ssai`` and ``sgai`` for HLS outputs, ``dash`` for DASH outputs."""

    @classmethod
    def of(cls, entry: Mapping[str, Any]) -> Channel:
        return cls(entry["name"], entry["ladderVersion"], tuple(entry["outputs"]), entry["playback"])

    def url(self, mode: str, output: str = "hls") -> str:
        """The ``mode`` (ssai, sgai, dash) URL of ``output``, else of the first output that has one."""
        candidates = [self.playback.get(output, {}), *self.playback.values()]
        if urls := [urls[mode] for urls in candidates if mode in urls]:
            return urls[0]
        raise AssertionError(f"channel {self.name} has no {mode} playback URL: {dict(self.playback)}")


# --- tools ---------------------------------------------------------------------------------------------------------


class ToolError(AssertionError):
    """A tool answered with the ``{"ok": false, "error": ...}`` envelope."""

    def __init__(self, tool: str, error: Mapping[str, Any]) -> None:
        self.code = str(error.get("code", ""))
        self.retryable = bool(error.get("retryable"))
        hint = f" (hint: {error['hint']})" if error.get("hint") else ""
        super().__init__(f"{tool} failed with {self.code}: {error.get('message', '')}{hint}")


def client_context(tool: str) -> str:
    """The Lambda ClientContext AgentCore Gateway sends: the qualified tool name in ``custom``."""
    return base64.b64encode(json.dumps({"custom": {"bedrockAgentCoreToolName": tool}}).encode()).decode()


@dataclass(frozen=True, slots=True)
class Tools:
    """Agent tools invoked as AgentCore Gateway invokes them: the target's Lambda function, the arguments as the
    event, the tool in the client context. Every call carries the suite's correlation id (``scripts/trace.py``)."""

    lambda_client: Any
    functions: Mapping[str, str]
    """Function name per target (Operator output ``ToolFunctions``)."""
    correlation_id: str

    def __call__(self, tool: str, **arguments: Any) -> dict[str, Any]:
        """The tool's result; retryable errors (throttling, a held schedule lease) are retried with backoff."""
        return retrying(lambda: self._invoke(tool, arguments), retry=lambda e: isinstance(e, ToolError) and e.retryable)

    def _invoke(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        response = self.lambda_client.invoke(
            FunctionName=self.functions[tool.partition(SEPARATOR)[0]],
            Payload=json.dumps({**arguments, CORRELATION_FIELD: self.correlation_id}).encode(),
            ClientContext=client_context(tool),
        )
        body: dict[str, Any] = json.loads(response["Payload"].read())
        if "FunctionError" in response:
            raise AssertionError(f"{tool}: the Lambda function failed ({response['FunctionError']}): {body}")
        if body.get("ok") is not True:
            raise ToolError(tool, body.get("error", body))
        return body


# --- HTTP ----------------------------------------------------------------------------------------------------------


class TransientResponseError(Exception):
    """A response worth retrying (429, 5xx)."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"{response.status_code} from {response.url}")


@dataclass
class Player:
    """GET requests as a player makes them through the CDN (redirects followed); transport errors, 429 and 5xx
    are retried with backoff, other errors fail with the response."""

    client: httpx.Client = field(
        default_factory=lambda: httpx.Client(
            follow_redirects=True, timeout=httpx.Timeout(20.0), headers={"User-Agent": USER_AGENT}
        )
    )

    def get(self, url: str) -> httpx.Response:
        def fetch() -> httpx.Response:
            response = self.client.get(url)
            if response.status_code in TRANSIENT_STATUS:
                raise TransientResponseError(response)
            return response

        response = retrying(fetch, retry=lambda e: isinstance(e, httpx.TransportError | TransientResponseError))
        if response.is_error:
            raise AssertionError(f"GET {url} -> {response.status_code}: {response.text[:300]}")
        return response

    def close(self) -> None:
        self.client.close()


# --- playback ------------------------------------------------------------------------------------------------------


@dataclass
class Observation:
    """What one SSAI, one SGAI (HLS Interstitials) and one DASH session saw while the scheduled breaks aired."""

    breaks: list[Window]
    house_ads: frozenset[str] = frozenset()
    """Asset ids of the ladder kit's house ads (their segments are ad segments, see ``Segment.is_ad_of``)."""
    ssai_multivariant: str = ""
    ssai: dict[str, dict[str, Segment]] = field(default_factory=dict)
    """Every distinct segment per rendition (``avc``, ``hevc``, ``av1``, ``audio:<group>``), by URI."""
    interstitials: dict[str, dict[str, str]] = field(default_factory=dict)
    assets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """Asset list per interstitial id (fetched once, when the DATERANGE first appears)."""
    ad_playlists: dict[str, int] = field(default_factory=dict)
    """HTTP status of the first asset of each filled asset list (the SGAI ad media playlist on the CDN)."""
    mpds: list[Mpd] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def ssai_ad_seconds(self, rendition: str) -> list[float]:
        """Seconds of ad segments a rendition showed in each break."""
        segments = self.ssai.get(rendition, {}).values()
        return [ad_seconds(segments, window, house_ads=self.house_ads) for window in self.breaks]

    def ssai_filled(self, rendition: str) -> bool:
        """Ads in a break after the first: the first break of a new creative airs its slate (NEW_CREATIVE)."""
        return any(seconds > 0 for seconds in self.ssai_ad_seconds(rendition)[1:])

    @property
    def sgai_filled(self) -> bool:
        return any(self.assets.values())

    @property
    def dash_ad_periods(self) -> set[str]:
        return {period for mpd in self.mpds for period in mpd.ad_periods}

    @property
    def complete(self) -> bool:
        return (
            bool(self.ssai)
            and all(self.ssai_filled(rendition) for rendition in self.ssai)
            and self.sgai_filled
            and bool(self.dash_ad_periods)
        )

    def report(self) -> str:
        """Per-break ad seconds of every SSAI rendition, SGAI asset counts, DASH ad periods and errors."""
        ssai = "; ".join(f"{r} {self.ssai_ad_seconds(r)}" for r in self.ssai)
        sgai = {interstitial: len(assets) for interstitial, assets in self.assets.items()}
        return (
            f"breaks at {[f'{w.starts:%H:%M:%S}' for w in self.breaks]}; SSAI ad seconds per break: {ssai}; "
            f"SGAI interstitials {sorted(self.interstitials)} with assets {sgai}; "
            f"DASH ad periods {sorted(self.dash_ad_periods)}; errors {self.errors[:5]}"
        )


def watch(
    player: Player,
    *,
    ssai_url: str,
    sgai_url: str,
    dash_url: str,
    breaks: list[Window],
    ends: datetime,
    house_ads: Collection[str] = (),
) -> Observation:
    """Play one session of each kind until every break was seen filled, or until ``ends``.

    Errors are recorded, not raised, so one bad response does not hide what the other sessions saw.
    """
    seen = Observation(breaks, frozenset(house_ads))
    ssai = player.get(ssai_url)
    seen.ssai_multivariant = ssai.text
    ssai_media = renditions(ssai.text, str(ssai.url))
    sgai = player.get(sgai_url)
    sgai_media = next(iter(renditions(sgai.text, str(sgai.url)).values()))
    variables = defines(sgai.text)
    dash = str(player.get(dash_url).url)  # the session's MPD URL, after MediaTailor's redirect
    while True:
        for rendition, url in ssai_media.items():
            if (text := _text(player, url, seen)) is not None:
                seen.ssai.setdefault(rendition, {}).update((s.uri, s) for s in media_segments(text, url))
        if (text := _text(player, sgai_media, seen)) is not None:
            for daterange, attributes in interstitials(text).items():
                if daterange not in seen.interstitials:
                    seen.interstitials[daterange] = attributes
                    assets = urljoin(sgai_media, substitute(attributes.get("X-ASSET-LIST", ""), variables))
                    _fetch_assets(player, seen, daterange, assets)
        if (text := _text(player, dash, seen)) is not None:
            seen.mpds.append(parse_mpd(text))
        if seen.complete or datetime.now(UTC) >= ends:
            return seen
        time.sleep(POLL_SECONDS)


def _text(player: Player, url: str, seen: Observation) -> str | None:
    try:
        return player.get(url).text
    except (AssertionError, httpx.HTTPError, TransientResponseError) as error:
        seen.errors.append(str(error))
        return None


def _fetch_assets(player: Player, seen: Observation, daterange: str, url: str) -> None:
    """Read an interstitial's asset list, and the first ad's media playlist through the CDN."""
    if (text := _text(player, url, seen)) is None:
        return
    seen.assets[daterange] = assets = asset_list(text)
    if assets:
        try:
            seen.ad_playlists[assets[0]["URI"]] = player.client.get(assets[0]["URI"]).status_code
        except httpx.HTTPError as error:
            seen.errors.append(f"GET {assets[0]['URI']}: {error!r}")


# --- agent ---------------------------------------------------------------------------------------------------------


def machine_token(http: httpx.Client, *, domain: str, client_id: str, client_secret: str, scope: str) -> str:
    """An access token for an app client with the client-credentials grant (Cognito ``/oauth2/token``)."""
    base = domain if domain.startswith("https://") else f"https://{domain}"
    response = http.post(
        f"{base.rstrip('/')}/oauth2/token",
        data={"grant_type": "client_credentials", "scope": scope},
        auth=(client_id, client_secret),
    )
    if response.is_error:
        raise AssertionError(f"token request -> {response.status_code}: {response.text[:300]}")
    return str(response.json()["access_token"])


def ask_agent(http: httpx.Client, *, invoke_url: str, token: str, prompt: str) -> list[dict[str, Any]]:
    """One AG-UI run in a new conversation; its events. Platform failures arrive as an SSE RUN_ERROR too, with
    HTTP 401/403, so the body is read whatever the status."""
    session = agui.session_id()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
        agui.SESSION_HEADER: session,
    }
    timeout = httpx.Timeout(30.0, read=300.0)
    body = agui.run_input(prompt, thread_id=session)
    with http.stream("POST", invoke_url, headers=headers, json=body, timeout=timeout) as response:
        events = list(agui.events(response.iter_lines()))
    if response.is_error and not events:
        raise AssertionError(f"POST {invoke_url} -> {response.status_code}")
    return events


# --- synthetic source ----------------------------------------------------------------------------------------------


def render_test_pattern(path: Path, *, seconds: int, fps: int = 30) -> Path:
    """``seconds`` of a 1080p ffmpeg ``testsrc2`` pattern with a 1 kHz stereo tone: H.264 High (2 s GOP) and AAC
    in MP4, a stand-in for a broadcaster's mezzanine."""
    if (ffmpeg := shutil.which("ffmpeg")) is None:
        raise AssertionError("ffmpeg is required to render the e2e test pattern (e.g. apt-get install ffmpeg)")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate={fps}:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=1000:sample_rate=48000:duration={seconds}",
        "-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-g", str(2 * fps), "-keyint_min", str(2 * fps), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-shortest", "-movflags", "+faststart", str(path),
    ]  # fmt: skip
    subprocess.run(command, check=True)
    return path
