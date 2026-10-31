import json
from collections.abc import Callable, Sequence
from dataclasses import replace
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


@pytest.fixture
def capped_harness(settings: Settings, tmp_path: Path) -> HarnessFactory:
    """``capped_harness(counter, turn, ...)``: :func:`harness` deployed with the model registry, a $0.01 daily cap
    (``cap=None``: none) and ``counter`` as the spend counter."""

    def build(counter: object, *turns: Sequence[Block], cap: float | None = 0.01) -> Harness:
        profile = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
        entry = {"model": settings.default_model_id, "daily_cap_usd": cap, "profile_arn": profile}
        deployed = replace(
            settings,
            model_task_default=json.dumps(entry),
            model_profile_arn=profile,
            model_counter_table="fast-models",
            allowed_models=frozenset({settings.default_model_id}),
        )
        return Harness.build(deployed, tmp_path, turns, default_handlers(), SESSION_ID, counter=counter)  # type: ignore[arg-type]

    return build
