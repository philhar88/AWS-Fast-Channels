"""Runtime configuration for Lambda functions, read once from environment variables set by the CDK constructs."""

import json
import os
from dataclasses import dataclass, field, fields
from functools import cache


def _env(name: str) -> str:
    return os.environ.get(name, "")


def _csv(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in _env(name).split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class CdnEndpoint:
    """A CDN that chains to the deployment's CloudFront distribution (config ``delivery.cdns``)."""

    name: str
    """Short name used in playback-URL listings, e.g. ``cdn-b``."""
    manifest_base: str
    """Base URL under which it serves the MediaTailor manifest paths, no trailing slash."""
    content_base: str
    """Base URL under which it serves content segments."""


def _cdn_endpoints() -> tuple[CdnEndpoint, ...]:
    """``CDN_ENDPOINTS``: a JSON list of ``{name, manifest_base, content_base}`` objects (absent: none)."""
    return tuple(CdnEndpoint(**entry) for entry in json.loads(_env("CDN_ENDPOINTS") or "[]"))


@dataclass(frozen=True, slots=True)
class Settings:
    """Environment contract (CONTRACTS §C). A function only receives the variables it needs; :meth:`require`
    raises a clear error when a missing one is used."""

    prefix: str = field(default_factory=lambda: _env("FAST_PREFIX"))
    media_bucket: str = field(default_factory=lambda: _env("MEDIA_BUCKET"))
    ingest_bucket: str = field(default_factory=lambda: _env("INGEST_BUCKET"))
    library_table: str = field(default_factory=lambda: _env("LIBRARY_TABLE"))
    catalog_table: str = field(default_factory=lambda: _env("CATALOG_TABLE"))
    event_bus: str = field(default_factory=lambda: _env("EVENT_BUS"))
    ingest_state_machine_arn: str = field(default_factory=lambda: _env("INGEST_STATE_MACHINE_ARN"))
    provision_state_machine_arn: str = field(default_factory=lambda: _env("PROVISION_STATE_MACHINE_ARN"))
    mediaconvert_role_arn: str = field(default_factory=lambda: _env("MEDIACONVERT_ROLE_ARN"))
    mediaconvert_queue_arn: str = field(default_factory=lambda: _env("MEDIACONVERT_QUEUE_ARN"))
    source_location: str = field(default_factory=lambda: _env("SOURCE_LOCATION"))
    content_base_url: str = field(default_factory=lambda: _env("CONTENT_BASE_URL"))
    cdn_domain: str = field(default_factory=lambda: _env("CDN_DOMAIN"))
    cdn_endpoints: tuple[CdnEndpoint, ...] = field(default_factory=_cdn_endpoints)
    package_profiles: tuple[str, ...] = field(default_factory=lambda: _csv("PACKAGE_PROFILES"))
    default_ladder_version: str = field(default_factory=lambda: _env("DEFAULT_LADDER_VERSION"))
    playback_config_template: str = field(default_factory=lambda: _env("PLAYBACK_CONFIG_TEMPLATE"))
    auto_stop_hours: float = field(default_factory=lambda: float(_env("AUTO_STOP_HOURS") or 0))
    max_agent_channels: int = field(default_factory=lambda: int(_env("MAX_AGENT_CHANNELS") or 0))
    scheduler_group: str = field(default_factory=lambda: _env("SCHEDULER_GROUP"))
    scheduler_role_arn: str = field(default_factory=lambda: _env("SCHEDULER_ROLE_ARN"))
    region: str = field(default_factory=lambda: _env("AWS_REGION"))

    @property
    def app(self) -> str:
        """Value of the ``fast:app`` tag on every runtime resource."""
        return self.prefix

    @property
    def agent_prefix(self) -> str:
        """Name prefix reserved for agent-owned MediaTailor resources."""
        return f"{self.prefix}-agent-"

    def require(self, *names: str) -> None:
        """Fail fast with the environment variable names that are missing."""
        known = {f.name for f in fields(self)}
        missing = [name.upper() for name in names if name in known and not getattr(self, name)]
        if missing:
            raise RuntimeError(f"missing environment configuration: {', '.join(missing)}")


@cache
def settings() -> Settings:
    """Process-wide settings (environment variables do not change during a Lambda execution environment's life)."""
    return Settings()
