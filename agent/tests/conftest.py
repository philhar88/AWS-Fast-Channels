from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from fast_channels.agent.settings import Settings
from fast_channels.agent.testing import Block, Handler, Harness

SESSION_ID = "test-session-0123456789abcdef0123456789"

type HarnessFactory = Callable[..., Harness]


def default_handlers() -> dict[str, Handler]:
    return {
        "catalog___search_content": lambda _: {"ok": True, "items": [{"content_id": "bbb", "title": "Big Buck Bunny"}]},
        "schedule___plan": lambda args: {
            "ok": True,
            "plan_id": "plan-1",
            "channel": args.get("channel"),
            "programs": [],
            "violations": [],
        },
        "schedule___publish": lambda args: {
            "ok": True,
            "plan_id": args.get("plan_id"),
            "channel": "fast-demo",
            "programs": [],
        },
        "schedule___get_schedule": lambda args: {"ok": True, "channel": args.get("channel"), "programs": []},
    }


@pytest.fixture
def settings() -> Settings:
    return Settings(
        region="us-east-1", gateway_url="https://gateway.invalid/mcp", confirm_tools=frozenset({"schedule___publish"})
    )


@pytest.fixture
def harness(settings: Settings, tmp_path: Path) -> HarnessFactory:
    """``harness(turn, turn, ...)``: a scheduler whose model replays one scripted turn per model call."""

    def build(*turns: Sequence[Block], handlers: dict[str, Handler] | None = None) -> Harness:
        return Harness.build(settings, tmp_path, turns, handlers or default_handlers(), SESSION_ID)

    return build
