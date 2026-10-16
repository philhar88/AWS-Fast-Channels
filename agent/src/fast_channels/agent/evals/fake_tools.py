"""A local stand-in for the Gateway: the scheduling tools over MCP, backed by the eval catalog.

The server advertises the real Gateway tool definitions (``gateway_tools.json``: description and input schema,
generated from the tool registry by ``functions/tests/test_agent_contract.py``), so the model reads exactly what it
reads in production. The fake tools take the same arguments and answer in the registry's output shapes: the plan
tool computes start times, ad breaks and violations, and publishing takes only a ``plan_id``. Running it as a real
MCP server keeps the agent's Gateway client path (streamable HTTP, token forwarding, tool caching) in the loop.
"""

import functools
import json
import socket
import threading
import time
import zlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import uvicorn
from mcp.server.mcpserver import MCPServer

from fast_channels.agent.evals.catalog import FAMILY_RATINGS, LADDER_VERSION, Title, by_asset, titles

if TYPE_CHECKING:
    from mcp.types import Tool as MCPTool

CHANNEL: Final = "fastv3-family-east"
GOP_MS: Final = 2_002  # 2 s GOPs at 29.97 fps
LEAD_TIME_MS: Final = 10 * 60_000
PLAN_TTL_MS: Final = 3_600_000
DEFINITIONS: Final = Path(__file__).with_name("gateway_tools.json")
TOOLS: Final = {
    "catalog___search_content": "search_content",
    "library___find_assets": "find_assets",
    "channels___get_channel": "get_channel",
    "schedule___get_brief": "get_brief",
    "schedule___get_schedule": "get_schedule",
    "schedule___plan": "plan",
    "schedule___publish": "publish",
}
"""Gateway tool name -> :class:`FakeScheduleTools` method with the tool's input fields as parameters."""
type Payload = dict[str, Any]


def iso(ms: int) -> str:
    """UTC ISO-8601 to the second, as people write it."""
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def wire_time(ms: int) -> str:
    """UTC ISO-8601 with milliseconds, as the tools return times."""
    return f"{iso(ms).removesuffix('Z')}.{ms % 1000:03d}Z"


def parse_iso(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


@cache
def gateway_definitions() -> dict[str, Payload]:
    """The Gateway tool definitions (``name``, ``description``, ``inputSchema``) the fake serves, by name."""
    return {definition["name"]: definition for definition in json.loads(DEFINITIONS.read_text())["tools"]}


@dataclass
class FakeScheduleTools:
    """In-memory channel, brief, plans and published schedule for one eval run."""

    channel: str = CHANNEL
    plans: dict[str, Payload] = field(default_factory=dict)
    published: list[Payload] = field(default_factory=list)
    """Published programs as guide entries, in air order."""
    _schedule_at_planning: dict[str, int] = field(default_factory=dict, repr=False)
    """Published program count when each plan was made: publishing after a change is PLAN_STALE."""

    def brief(self) -> Payload:
        return {
            "ratings": sorted(FAMILY_RATINGS),
            "genres_allow": [],
            "genres_deny": [],
            "dayparts": [],
            "break_policy": {"cadence_minutes": 12, "min_gap_minutes": 8, "slate_gops": 30},
            "notes": "Family channel: every program must carry a family rating.",
        }

    # --- the tools: one method per Gateway tool, parameters named like the tool's input fields ---------------------

    def search_content(
        self,
        *,
        text: str | None = None,
        genres: list[str] | None = None,
        ratings: list[str] | None = None,
        max_duration_minutes: float | None = None,
        limit: int = 20,
        next_token: str | None = None,
    ) -> Payload:
        words = (text or "").lower().split()
        wanted = {genre.lower() for genre in genres or ()}
        items = [
            _content(t)
            for t in titles()
            if all(word in t.title.lower() for word in words)
            and (not wanted or t.genre.lower() in wanted)
            and (not ratings or t.rating in ratings)
            and (max_duration_minutes is None or t.duration_ms <= max_duration_minutes * 60_000)
        ]
        return {"ok": True, "items": items[:limit]}

    def find_assets(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        ladder_version: str | None = None,
        content_id: str | None = None,
        limit: int = 50,
        next_token: str | None = None,
    ) -> Payload:
        matching = kind in (None, "content") and status in (None, "READY") and ladder_version in (None, LADDER_VERSION)
        assets = [_asset(t) for t in titles() if matching and content_id in (None, t.content_id)]
        return {"ok": True, "assets": assets[:limit]}

    def get_channel(self, channel: str) -> Payload:
        if channel != self.channel:
            return _error("NOT_FOUND", f"channel {channel} does not exist", hint="Call channels___list_channels.")
        playback = f"https://d111111abcdef8.cloudfront.net/v1/master/1bab6b42/{channel}"
        return {
            "ok": True,
            "name": channel,
            "state": "RUNNING",
            "owner": "iac",
            "ladder_version": LADDER_VERSION,
            "outputs": ["hls", "hls-avc", "dash-avc"],
            "tier": "BASIC",
            "playback_configuration": channel,
            "playback": {
                "hls": {"ssai": f"{playback}/hls.m3u8", "sgai": f"{playback}/hls.m3u8?aws.insertionMode=GUIDED"},
                "hls-avc": {
                    "ssai": f"{playback}/hls-avc.m3u8",
                    "sgai": f"{playback}/hls-avc.m3u8?aws.insertionMode=GUIDED",
                },
                "dash-avc": {"dash": playback.replace("/master/", "/dash/") + "/dash-avc.mpd"},
            },
        }

    def get_brief(self, channel: str) -> Payload:
        if channel != self.channel:
            return _error("NOT_FOUND", f"channel {channel} does not exist", hint="Call channels___list_channels.")
        return {"ok": True, "channel": channel, "brief": self.brief(), "default": False}

    def get_schedule(self, channel: str) -> Payload:
        if channel != self.channel:
            return _error("NOT_FOUND", f"channel {channel} does not exist", hint="Call channels___list_channels.")
        now = _now_ms()
        upcoming = [p for p in self.published if parse_iso(p["end"]) > now]
        end = {"end_ms": parse_iso(upcoming[-1]["end"]), "end": upcoming[-1]["end"]} if upcoming else {}
        return {**self._guide(upcoming, now), **end, "gaps": []}

    def plan(
        self,
        *,
        channel: str,
        picks: list[str],
        start: str | None = None,
        after: str | None = None,
        break_policy: Payload | None = None,
    ) -> Payload:
        if channel != self.channel:
            return _error("NOT_FOUND", f"channel {channel} does not exist", hint="Call channels___list_channels.")
        if start is not None and after is not None:
            return _error("VALIDATION", "input: give start or after, not both", hint="Fix the arguments and retry.")
        now = _now_ms()
        start_ms = self._block_start(start, after, now)
        if start_ms is None:
            return _error(
                "NOT_FOUND",
                f"{after} is not an upcoming program of {channel}",
                hint="Use a program name from schedule___get_schedule.",
            )
        policy = {**self.brief()["break_policy"], **(break_policy or {})}
        assets = by_asset()
        programs: list[Payload] = []
        violations: list[Payload] = []
        cursor = start_ms
        for asset_id in picks:
            if (title := assets.get(asset_id)) is None:
                violations.append(_violation("UNKNOWN_ASSET", "no such asset in the media library", asset_id))
                continue
            program = _program(title, cursor, policy)
            if title.rating not in FAMILY_RATINGS:
                message = f"rating {title.rating!r} is not allowed on this channel"
                violations.append(_violation("RATING", message, program["name"]))
            programs.append(program)
            cursor += program["duration_ms"]
        if not programs:
            violations.append(_violation("EMPTY", "the plan has no programs"))
        if start_ms < now:
            violations.append(_violation("START_IN_PAST", "the block starts in the past"))
        elif (lead := start_ms - now) < LEAD_TIME_MS:
            violations.append(
                _violation("LEAD_TIME", f"starts in {lead // 1000} s; best effort below 10 min", severity="warning")
            )
        plan_id = f"{channel}.{len(self.plans) + 1:08x}"
        self.plans[plan_id] = {
            "ok": True,
            "plan_id": plan_id,
            "channel": channel,
            "ladder_version": LADDER_VERSION,
            "start_ms": start_ms,
            "start": wire_time(start_ms),
            "end_ms": cursor,
            "end": wire_time(cursor),
            "programs": programs,
            "violations": violations,
            "publishable": not any(v["severity"] == "error" for v in violations),
            "expires_at": iso(now + PLAN_TTL_MS),
        }
        self._schedule_at_planning[plan_id] = len(self.published)
        return self.plans[plan_id]

    def publish(self, plan_id: str) -> Payload:
        plan = self.plans.get(plan_id)
        if plan is None:
            return _error("NOT_FOUND", f"plan {plan_id} does not exist or expired", hint="Call schedule___plan again.")
        if not plan["publishable"]:
            return _error("VALIDATION", "the plan has error violations", hint="Fix the picks and plan again.")
        if self._schedule_at_planning[plan_id] != len(self.published):
            # Like the real tool: the schedule changed since planning (for example another plan was published).
            return _error(
                "PLAN_STALE",
                f"the schedule of {plan['channel']} changed since the plan was made",
                hint="Call schedule___get_schedule and plan again.",
            )
        entries = [_guide_entry(p) for p in plan["programs"]]
        self.published.extend(entries)
        return {
            **self._guide(entries, _now_ms()),
            "plan_id": plan_id,
            "published": len(entries),
            **{key: plan[key] for key in ("start_ms", "start", "end_ms", "end")},
            "alerts": [],
        }

    # --- helpers -------------------------------------------------------------------------------------------------

    def _block_start(self, start: str | None, after: str | None, now: int) -> int | None:
        if start is not None:
            return parse_iso(start)
        if after is not None:
            previous = next((p for p in self.published if p["name"] == after and parse_iso(p["end"]) > now), None)
            return parse_iso(previous["end"]) if previous else None
        upcoming = [p for p in self.published if parse_iso(p["end"]) > now]
        return parse_iso(upcoming[-1]["end"]) if upcoming else -(-(now + LEAD_TIME_MS) // 1000) * 1000

    def _guide(self, programs: list[Payload], now: int) -> Payload:
        return {
            "ok": True,
            "channel": self.channel,
            "generated_at": wire_time(now),
            "ladder_version": LADDER_VERSION,
            "programs": programs,
        }

    def server(self) -> MCPServer:
        """An MCP server exposing these tools under their Gateway names and definitions.

        Every tool ignores ``correlation_id`` (the runtime injects it) and returns compact JSON text, like Gateway
        Lambda targets.
        """
        server = _GatewayDefinedServer("fast-channels-eval-tools", log_level="WARNING")
        for name, method in TOOLS.items():
            server.add_tool(_json_result(getattr(self, method)), name=name, structured_output=False)
        return server


class _GatewayDefinedServer(MCPServer):
    """Advertises the Gateway's own description and input schema for every tool it serves."""

    async def list_tools(self) -> list[MCPTool]:
        definitions = gateway_definitions()
        return [
            tool.model_copy(
                update={
                    "description": definitions[tool.name]["description"],
                    "input_schema": definitions[tool.name]["inputSchema"],
                }
            )
            for tool in await super().list_tools()
        ]


def _json_result(method: Callable[..., Payload]) -> Callable[..., str]:
    """``method`` as an MCP tool function: same parameters, compact JSON text result."""

    @functools.wraps(method)
    def call(**arguments: Any) -> str:
        return json.dumps(method(**arguments), separators=(",", ":"))

    return call


@contextmanager
def serve(tools: FakeScheduleTools) -> Iterator[str]:
    """Serve ``tools`` over streamable HTTP on localhost; yields the MCP endpoint URL."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = tools.server().streamable_http_app(stateless_http=True, json_response=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _content(title: Title) -> Payload:
    """``core.catalog.ContentItem`` as search_content returns it."""
    return {
        "content_id": title.content_id,
        "title": title.title,
        "synopsis": "",
        "rating": title.rating,
        "genres": [title.genre],
        "duration_ms": title.duration_ms,
        "cue_points_ms": [],
        "asset_ids": [title.asset_id],
    }


def _asset(title: Title) -> Payload:
    """A ``library.AssetSummary`` row."""
    return {
        "asset_id": title.asset_id,
        "kind": "content",
        "ladder_version": LADDER_VERSION,
        "status": "READY",
        "owner": "pipeline",
        "title": title.title,
        "content_id": title.content_id,
        "duration_ms": title.duration_ms,
        "break_offset_count": len(title.break_offsets_ms),
        "vod_source": f"{title.asset_id}-{LADDER_VERSION.replace('@', '_')}",
        "updated_at": "2026-10-15T21:14:03.000Z",
    }


def _program(title: Title, start_ms: int, policy: Mapping[str, Any]) -> Payload:
    """A ``schedule.PlannedRow``: ``duration_ms`` is the on-air duration, content plus breaks."""
    slate_ms = int(policy["slate_gops"]) * GOP_MS
    offsets = _breaks(
        title, cadence_ms=int(policy["cadence_minutes"] * 60_000), min_gap_ms=int(policy["min_gap_minutes"] * 60_000)
    )
    if (max_breaks := policy.get("max_breaks")) is not None:
        offsets = offsets[:max_breaks]
    on_air = title.duration_ms + len(offsets) * slate_ms
    name = f"{iso(start_ms).replace('-', '').replace(':', '')[:15]}-{title.asset_id}"
    return {
        "name": name,
        "asset_id": title.asset_id,
        "vod_source": f"{title.asset_id}-{LADDER_VERSION.replace('@', '_')}",
        "content_id": title.content_id,
        "start_ms": start_ms,
        "duration_ms": on_air,
        "breaks": [
            {
                "offset_ms": offset,
                "duration_ms": slate_ms,
                "slate_gops": policy["slate_gops"],
                "event_id": zlib.crc32(f"{name}:{index}".encode()) & 0x7FFFFFFF,
                "upid": f"FST3{title.content_id}",
            }
            for index, offset in enumerate(offsets)
        ],
        "ladder_version": LADDER_VERSION,
        "start": wire_time(start_ms),
        "end": wire_time(start_ms + on_air),
        "title": title.title,
        "rating": title.rating,
        "genres": [title.genre],
    }


def _guide_entry(program: Mapping[str, Any]) -> Payload:
    """An ``epg.EpgProgram`` for a planned program."""
    return {
        "name": program["name"],
        "title": program["title"],
        "content_id": program["content_id"],
        "asset_id": program["asset_id"],
        "start": program["start"],
        "end": program["end"],
        "rating": program["rating"],
        "genres": program["genres"],
        "breaks": [{"offset_ms": b["offset_ms"], "duration_ms": b["duration_ms"]} for b in program["breaks"]],
        "status": "PUBLISHED",
    }


def _breaks(title: Title, *, cadence_ms: int, min_gap_ms: int) -> list[int]:
    """Natural break points about every ``cadence_ms``, never closer than ``min_gap_ms``, never at 0."""
    chosen, last, max_stretch = [], 0, cadence_ms * 5 // 4
    while title.duration_ms - last > max_stretch:
        window = [b for b in title.break_offsets_ms if last + min_gap_ms <= b <= last + max_stretch]
        if not window:
            break
        last = min(window, key=lambda b: abs(b - (last + cadence_ms)))
        chosen.append(last)
    return chosen


def _violation(code: str, message: str, program: str | None = None, *, severity: str = "error") -> Payload:
    return {"code": code, "severity": severity, "message": message, **({"program": program} if program else {})}


def _error(code: str, message: str, *, hint: str = "") -> Payload:
    return {"ok": False, "error": {"code": code, "message": message, "retryable": False, "hint": hint}}
