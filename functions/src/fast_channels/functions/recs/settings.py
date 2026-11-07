"""The recommender functions' environment, set by the Recs stack (``infra/constructs/recs``)."""

import os
from dataclasses import dataclass, field
from functools import cache
from typing import Final

from fast_channels.core.recs.router import DEFAULT_HOLDOUT_PERCENT

DEFAULT_REFRESH_HOURS: Final = 20
"""A viewer's model rail older than this is rebuilt on their next request (DESIGN §15: stale-while-revalidate)."""
DEFAULT_RAIL_HOURS: Final = 36
"""How long a built rail is kept (``RAIL#vid``, 36 h)."""


def _env(name: str) -> str:
    return os.environ.get(name, "")


def _csv(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in _env(name).split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class RecsSettings:
    """The environment of the rail API and the rail builder."""

    prefix: str = field(default_factory=lambda: _env("FAST_PREFIX"))
    region: str = field(default_factory=lambda: _env("AWS_REGION"))
    table: str = field(default_factory=lambda: _env("RECS_TABLE"))
    """``<prefix>-recs``: viewers' rail state and the built rails."""
    media_bucket: str = field(default_factory=lambda: _env("MEDIA_BUCKET"))
    """The guides (``epg/<channel>.json``) and the cohort rails (``plan/rail/<cohort>.json``)."""
    catalog_table: str = field(default_factory=lambda: _env("CATALOG_TABLE"))
    channels: tuple[str, ...] = field(default_factory=lambda: _csv("RECS_CHANNELS"))
    """The channels a rail recommends, in the viewer's order."""
    child_directed: tuple[str, ...] = field(default_factory=lambda: _csv("RECS_CHILD_DIRECTED"))
    """Channels whose titles only a kids lineup shows."""
    cohorts: tuple[str, ...] = field(default_factory=lambda: _csv("RECS_COHORTS") or ("web-en",))
    """``<device>-<language>`` of each cohort rail; the hourly run builds each for the current daypart."""
    holdout_percent: int = field(default_factory=lambda: int(_env("RECS_HOLDOUT_PERCENT") or DEFAULT_HOLDOUT_PERCENT))
    refresh_hours: float = field(default_factory=lambda: float(_env("RECS_REFRESH_HOURS") or DEFAULT_REFRESH_HOURS))
    rail_hours: float = field(default_factory=lambda: float(_env("RECS_RAIL_HOURS") or DEFAULT_RAIL_HOURS))
    builder_function: str = field(default_factory=lambda: _env("RECS_BUILDER_FUNCTION"))
    """The rail builder, which the rail API invokes asynchronously."""
    identity_table: str = field(default_factory=lambda: _env("IDENTITY_TABLE"))
    """The identity module's table, where the consent rows ``C#<vid>`` are."""
    vid_key: str = field(default_factory=lambda: _env("VID_KEY"))
    model_task_parameter: str = field(default_factory=lambda: _env("MODEL_TASK_PARAMETER"))
    model_task_default: str = field(default_factory=lambda: _env("MODEL_TASK_DEFAULT"))
    """The ``recommender`` entry as deployed: used when the SSM entry cannot be read or is invalid."""
    model_counter_table: str = field(default_factory=lambda: _env("MODEL_COUNTER_TABLE"))
    alert_topic_arn: str | None = field(default_factory=lambda: _env("ALERT_TOPIC_ARN") or None)
    appconfig_application: str | None = field(default_factory=lambda: _env("APPCONFIG_APPLICATION") or None)
    """The ops module's AppConfig application, whose ``flags`` profile holds ``recs.strategy``; unset without ops."""
    appconfig_environment: str | None = field(default_factory=lambda: _env("APPCONFIG_ENVIRONMENT") or None)


@cache
def recs_settings() -> RecsSettings:
    """Process-wide settings."""
    return RecsSettings()
