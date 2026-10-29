"""Runtime configuration for Lambda functions, read once from environment variables set by the CDK constructs."""

import json
import os
from dataclasses import dataclass, field, fields
from functools import cache

from fast_channels.core.naming import agent_namespace


def _env(name: str) -> str:
    return os.environ.get(name, "")


def _cdn_base_url() -> str:
    """``CDN_BASE_URL`` (``https://<host>``, no trailing slash); ``CDN_DOMAIN`` is its former name, which deployments
    also set to the URL."""
    return (_env("CDN_BASE_URL") or _env("CDN_DOMAIN")).rstrip("/")


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
    mediaconvert_queue_arn: str = field(default_factory=lambda: _env("MEDIACONVERT_QUEUE_ARN"))
    source_location: str = field(default_factory=lambda: _env("SOURCE_LOCATION"))
    content_base_url: str = field(default_factory=lambda: _env("CONTENT_BASE_URL"))
    cdn_base_url: str = field(default_factory=_cdn_base_url)
    """Base URL of the deployment's own CloudFront distribution, e.g. ``https://d111111abcdef8.cloudfront.net``."""
    cdn_endpoints: tuple[CdnEndpoint, ...] = field(default_factory=_cdn_endpoints)
    package_profiles: tuple[str, ...] = field(default_factory=lambda: _csv("PACKAGE_PROFILES"))
    default_ladder_version: str = field(default_factory=lambda: _env("DEFAULT_LADDER_VERSION"))
    playback_config_template: str = field(default_factory=lambda: _env("PLAYBACK_CONFIG_TEMPLATE"))
    auto_stop_hours: float = field(default_factory=lambda: float(_env("AUTO_STOP_HOURS") or 0))
    max_agent_channels: int = field(default_factory=lambda: int(_env("MAX_AGENT_CHANNELS") or 0))
    scheduler_group: str = field(default_factory=lambda: _env("SCHEDULER_GROUP"))
    scheduler_role_arn: str = field(default_factory=lambda: _env("SCHEDULER_ROLE_ARN"))
    publisher_domain: str = field(default_factory=lambda: _env("PUBLISHER_DOMAIN"))
    """The publisher's domain, the registry of catalog content ids in context objects (empty: not configured)."""
    region: str = field(default_factory=lambda: _env("AWS_REGION"))
    ledger_table: str = field(default_factory=lambda: _env("LEDGER_TABLE"))
    """The ops module's ledger table (``<prefix>-ledger``)."""
    ledger_stream: str = field(default_factory=lambda: _env("LEDGER_STREAM"))
    """The Firehose stream that archives linked ledger records."""
    as_run_breaks: bool = field(default_factory=lambda: _env("AS_RUN_BREAKS") == "true")
    """The as-run reconciler marks breaks aired (ops module)."""
    epg_break_ids: bool = field(default_factory=lambda: _env("EPG_BREAK_IDS") == "true")
    """EPG feeds carry each break's SCTE-35 event id and UPID (ops module)."""

    @property
    def app(self) -> str:
        """Value of the ``fast:app`` tag on every runtime resource."""
        return self.prefix

    @property
    def agent_prefix(self) -> str:
        """Name prefix reserved for agent-owned MediaTailor resources."""
        return agent_namespace(self.prefix)

    def require(self, *names: str) -> None:
        """Fail fast with the environment variable names that are missing.

        Raises:
            ValueError: a name is not a setting (a typo in the caller).
            RuntimeError: a setting is empty because its variable is not set on this function.
        """
        known = {f.name for f in fields(self)}
        if unknown := [name for name in names if name not in known]:
            raise ValueError(f"unknown settings {unknown}; known: {sorted(known)}")
        if missing := [name.upper() for name in names if not getattr(self, name)]:
            raise RuntimeError(f"missing environment configuration: {', '.join(missing)}")


@cache
def settings() -> Settings:
    """Process-wide settings (environment variables do not change during a Lambda execution environment's life)."""
    return Settings()
