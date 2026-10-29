"""The PDS snapshot: everything a ``/policy`` or ``/select`` decision reads besides the request and per-viewer state.

The snapshot publisher writes it to the PDS's private config bucket (never the CDN, never the ingest bucket) as
``snapshot/current.json``, with a copy under ``snapshot/<version>.json``. Each function loads it at cold start and
looks for a newer one every :data:`REFRESH_SECONDS`. A function that has never loaded one serves :data:`EMPTY`: no
third-party bidder and no viewer address, the fail-safe answer. It holds no per-viewer data.
"""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from functools import cached_property
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from fast_channels.core.pds.policy import Demand
from fast_channels.core.pds.xff import IpNetwork, origin_facing_networks

SNAPSHOT_KEY: Final = "snapshot/current.json"
"""The snapshot every function reads."""
HISTORY_PREFIX: Final = "snapshot/history/"
"""Each published version is also kept as ``snapshot/history/<version>.json`` (expired by the bucket's lifecycle)."""
REFRESH_SECONDS: Final = 30
"""How often a function looks for a newer snapshot (a conditional GET)."""
SIMULATOR: Final = "amt"
"""Prebid Server bidder code of the guidance's bidder simulator."""


def demo_demand(prefix: str) -> dict[str, dict[str, JsonValue]]:
    """The demand allow-list of a stage in demo mode: the guidance's bidder simulator, whose adapter requires a
    ``placementId``."""
    return {SIMULATOR: {"placementId": f"{prefix}-demo"}}


class SnapshotBase(BaseModel):
    """What the deployment configures (the publisher's input from the stack)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    demand: dict[str, dict[str, JsonValue]] = Field(
        default_factory=dict, description="Third-party bidders /policy may add, with their Prebid Server parameters."
    )
    shed: frozenset[str] = Field(default=frozenset(), description="Bidders a runbook took out of the allow-list.")
    floor_cpm: Decimal = Field(default=Decimal(0), ge=0, description="Price floor for paid bids, CPM.")


class Snapshot(SnapshotBase):
    """One published snapshot."""

    v: Literal[1] = 1
    version: str = Field(pattern=r"^\d{8}T\d{6}Z-[0-9a-f]{12}$", description="``<UTC time>-<content digest>``.")
    generated_at: datetime
    origin_facing: tuple[str, ...] = Field(
        default=(), description="CloudFront's origin-facing networks (ip-ranges.json CLOUDFRONT_ORIGIN_FACING)."
    )
    ip_ranges: str | None = Field(default=None, description="``syncToken`` of the ip-ranges.json they came from.")

    @field_validator("origin_facing")
    @classmethod
    def _networks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(str(ip_network(network)) for network in value)

    @cached_property
    def networks(self) -> tuple[IpNetwork, ...]:
        """The origin-facing networks, parsed once."""
        parsed: list[IPv4Network | IPv6Network] = [ip_network(network) for network in self.origin_facing]
        return tuple(parsed)

    @cached_property
    def allow_list(self) -> Demand:
        """The demand allow-list as :func:`~fast_channels.core.pds.policy.decide` takes it."""
        return Demand(self.demand, self.shed)


EMPTY: Final = Snapshot(version="19700101T000000Z-000000000000", generated_at=datetime(1970, 1, 1, tzinfo=UTC))
"""What a function serves before it has loaded any snapshot."""


def _content(base: SnapshotBase, origin_facing: tuple[str, ...], ip_ranges: str | None) -> dict[str, Any]:
    return {**base.model_dump(mode="json"), "origin_facing": sorted(origin_facing), "ip_ranges": ip_ranges}


def build(base: SnapshotBase, ip_ranges: dict[str, Any] | None, *, now: datetime) -> Snapshot:
    """A snapshot of ``base`` with CloudFront's origin-facing networks from an ip-ranges.json document (``None``
    when it could not be read: no network, so no viewer address until a later publish)."""
    networks = tuple(str(network) for network in origin_facing_networks(ip_ranges)) if ip_ranges else ()
    sync = str(ip_ranges.get("syncToken")) if ip_ranges and ip_ranges.get("syncToken") is not None else None
    content = _content(base, networks, sync)
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    return Snapshot.model_validate(
        {**content, "version": f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{digest}", "generated_at": now}
    )


def digest(snapshot: Snapshot) -> str:
    """The content digest a snapshot's version ends with: equal digests, equal content."""
    return snapshot.version.rsplit("-", 1)[1]


def history_key(snapshot: Snapshot) -> str:
    """Where the publisher keeps a copy of ``snapshot``."""
    return f"{HISTORY_PREFIX}{snapshot.version}.json"
