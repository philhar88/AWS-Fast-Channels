"""A local stand-in for the Gateway: the v3 scheduling tool surface over MCP, backed by the eval catalog.

The tools mirror the registry's names and semantics (SPEC §6): the plan tool computes start times, ad breaks
and violations, and publishing takes only a ``plan_id``. Running it as a real MCP server keeps the agent's
Gateway client path (streamable HTTP, token forwarding, tool caching) in the loop.
"""

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import uvicorn
from mcp.server.mcpserver import MCPServer

from fast_channels.agent.evals.catalog import FAMILY_RATINGS, LADDER_VERSION, Title, by_asset, titles

CHANNEL: Final = "fastv3-family-east"
SLATE_MS: Final = 60_060  # 30-GOP break slate at 29.97 fps
LEAD_TIME_MS: Final = 10 * 60_000
type Payload = dict[str, Any]


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


@dataclass
class FakeScheduleTools:
    """In-memory channel, brief, plans and published schedule for one eval run."""

    channel: str = CHANNEL
    plans: dict[str, Payload] = field(default_factory=dict)
    published: list[Payload] = field(default_factory=list)

    def brief(self) -> Payload:
        return {
            "ratings": sorted(FAMILY_RATINGS),
            "genres_allow": [],
            "genres_deny": [],
            "dayparts": [],
            "break_policy": {"cadence_minutes": 12, "min_gap_minutes": 8, "max_breaks": None, "slate_gops": 30},
            "notes": "Family channel: every program must carry a family rating.",
        }

    def search_content(
        self,
        text: str | None = None,
        genres: list[str] | None = None,
        ratings: list[str] | None = None,
        limit: int = 20,
    ) -> Payload:
        wanted = {genre.lower() for genre in genres or ()}
        items = [
            _content(t)
            for t in titles()
            if (not text or text.lower() in t.title.lower())
            and (not wanted or t.genre.lower() in wanted)
            and (not ratings or t.rating in ratings)
        ]
        return {"ok": True, "items": items[:limit]}

    def find_assets(self, content_id: str | None = None, ladder_version: str | None = None) -> Payload:
        assets = [
            _asset(t)
            for t in titles()
            if (content_id in (None, t.content_id)) and ladder_version in (None, LADDER_VERSION)
        ]
        return {"ok": True, "assets": assets}

    def get_channel(self, channel: str) -> Payload:
        if channel != self.channel:
            return _error("NOT_FOUND", f"channel {channel} does not exist")
        return {
            "ok": True,
            "name": channel,
            "state": "RUNNING",
            "tier": "BASIC",
            "ladder_version": LADDER_VERSION,
            "outputs": ["hls", "hls-avc", "dash-avc"],
        }

    def get_schedule(self, channel: str) -> Payload:
        end = self.published[-1]["start_ms"] + self.published[-1]["airtime_ms"] if self.published else None
        return {"ok": True, "channel": channel, "programs": self.published, "end_ms": end, "gaps": []}

    def plan(
        self, channel: str, picks: list[str], start: str | None = None, break_policy: Payload | None = None
    ) -> Payload:
        if channel != self.channel:
            return _error("NOT_FOUND", f"channel {channel} does not exist")
        policy = {**self.brief()["break_policy"], **(break_policy or {})}
        now = int(time.time() * 1000)
        start_ms = parse_iso(start) if start else self.get_schedule(channel)["end_ms"] or now + LEAD_TIME_MS
        assets = by_asset()
        programs: list[Payload] = []
        violations: list[Payload] = []
        cursor = start_ms
        for asset_id in picks:
            if (title := assets.get(asset_id)) is None:
                violations.append(
                    {
                        "code": "UNKNOWN_ASSET",
                        "severity": "error",
                        "program": asset_id,
                        "message": f"asset {asset_id} is not in the library on {LADDER_VERSION}",
                    }
                )
                continue
            if title.rating not in self.brief()["ratings"]:
                violations.append(
                    {
                        "code": "RATING",
                        "severity": "error",
                        "program": asset_id,
                        "message": f"{title.title} is rated {title.rating}; the brief allows "
                        f"{', '.join(self.brief()['ratings'])}",
                    }
                )
            program = _program(title, cursor, policy)
            programs.append(program)
            cursor += program["airtime_ms"]
        if len(picks) != len(set(picks)):
            violations.append(
                {
                    "code": "REPEAT",
                    "severity": "warning",
                    "program": None,
                    "message": "a title repeats within the block",
                }
            )
        if start_ms < now + LEAD_TIME_MS:
            violations.append(
                {
                    "code": "LEAD_TIME",
                    "severity": "warning",
                    "program": None,
                    "message": "the block starts less than 10 minutes from now",
                }
            )
        plan_id = f"plan-{len(self.plans) + 1:04d}"
        self.plans[plan_id] = {
            "ok": True,
            "plan_id": plan_id,
            "channel": channel,
            "ladder_version": LADDER_VERSION,
            "start_ms": start_ms,
            "start": iso(start_ms),
            "end_ms": cursor,
            "end": iso(cursor),
            "programs": programs,
            "violations": violations,
        }
        return self.plans[plan_id]

    def publish(self, plan_id: str) -> Payload:
        plan = self.plans.get(plan_id)
        if plan is None:
            return _error("NOT_FOUND", f"plan {plan_id} does not exist", hint="Call schedule___plan first.")
        if any(v["severity"] == "error" for v in plan["violations"]):
            return _error("VALIDATION", "the plan has error violations", hint="Fix the picks and plan again.")
        self.published.extend(plan["programs"])
        return {
            "ok": True,
            "plan_id": plan_id,
            "channel": plan["channel"],
            "programs": len(plan["programs"]),
            "start_ms": plan["start_ms"],
            "end_ms": plan["end_ms"],
        }

    def server(self) -> MCPServer:
        """An MCP server exposing these tools under their Gateway names.

        Every tool accepts ``correlation_id`` and returns compact JSON text, like Gateway Lambda targets.
        """
        server = MCPServer("fast-channels-eval-tools", log_level="WARNING")
        tools = self

        def search_content(
            text: str | None = None,
            genres: list[str] | None = None,
            ratings: list[str] | None = None,
            limit: int = 20,
            correlation_id: str | None = None,
        ) -> str:
            """Search the content catalog by text, genres and ratings. Returns content items with asset ids."""
            return _json(tools.search_content(text, genres, ratings, limit))

        def find_assets(
            content_id: str | None = None, ladder_version: str | None = None, correlation_id: str | None = None
        ) -> str:
            """Find encoded assets in the media library (READY assets per ladder version, with break offsets)."""
            return _json(tools.find_assets(content_id, ladder_version))

        def get_channel(channel: str, correlation_id: str | None = None) -> str:
            """Get a channel: state, tier, ladder version and outputs."""
            return _json(tools.get_channel(channel))

        def get_brief(channel: str, correlation_id: str | None = None) -> str:
            """Get the channel's programming brief: allowed ratings and genres, dayparts and break policy."""
            return _json({"ok": True, "channel": channel, "brief": tools.brief()})

        def get_schedule(channel: str, correlation_id: str | None = None) -> str:
            """Get the published schedule of a channel, its end time and gaps."""
            return _json(tools.get_schedule(channel))

        def plan(
            channel: str,
            picks: list[str],
            start: str | None = None,
            break_policy: Payload | None = None,
            correlation_id: str | None = None,
        ) -> str:
            """Plan a block: ordered asset ids (picks) from start (ISO-8601 UTC) or after the published schedule.

            Computes every start time, duration and ad break and returns a plan_id, the programs and violations.
            """
            return _json(tools.plan(channel, picks, start, break_policy))

        def publish(plan_id: str, correlation_id: str | None = None) -> str:
            """Publish a validated plan to the channel. Changes the live schedule."""
            return _json(tools.publish(plan_id))

        for name, fn in (
            ("catalog___search_content", search_content),
            ("library___find_assets", find_assets),
            ("channels___get_channel", get_channel),
            ("schedule___get_brief", get_brief),
            ("schedule___get_schedule", get_schedule),
            ("schedule___plan", plan),
            ("schedule___publish", publish),
        ):
            server.add_tool(fn, name=name)
        return server


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


def _content(title: Title) -> Payload:
    return {
        "content_id": title.content_id,
        "title": title.title,
        "rating": title.rating,
        "genres": [title.genre],
        "duration_ms": title.duration_ms,
        "asset_ids": [title.asset_id],
    }


def _asset(title: Title) -> Payload:
    return {
        "asset_id": title.asset_id,
        "content_id": title.content_id,
        "kind": "content",
        "status": "READY",
        "ladder_version": LADDER_VERSION,
        "duration_ms": title.duration_ms,
        "break_offsets_ms": list(title.break_offsets_ms),
    }


def _program(title: Title, start_ms: int, policy: Payload) -> Payload:
    breaks = _breaks(
        title, cadence_ms=int(policy["cadence_minutes"] * 60_000), min_gap_ms=int(policy["min_gap_minutes"] * 60_000)
    )
    return {
        "name": f"{iso(start_ms).replace('-', '').replace(':', '')[:15]}-{title.asset_id}",
        "asset_id": title.asset_id,
        "content_id": title.content_id,
        "title": title.title,
        "rating": title.rating,
        "genres": [title.genre],
        "start_ms": start_ms,
        "start": iso(start_ms),
        "duration_ms": title.duration_ms,
        "airtime_ms": title.duration_ms + len(breaks) * SLATE_MS,
        "breaks": [{"offset_ms": offset, "duration_ms": SLATE_MS} for offset in breaks],
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


def _json(payload: Payload) -> str:
    return json.dumps(payload, separators=(",", ":"))  # compact, like Gateway Lambda results


def _error(code: str, message: str, *, hint: str = "") -> Payload:
    return {"ok": False, "error": {"code": code, "message": message, "retryable": False, "hint": hint}}
