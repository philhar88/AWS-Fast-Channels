"""The PDS snapshot: everything a ``/policy``, ``/select`` or own-book answer reads besides the request and the
per-viewer and budget state.

The snapshot publisher writes it to the PDS's private config bucket (never the CDN, never the ingest bucket) as
``snapshot/current.json``, with a copy under ``snapshot/history/<version>.json``. Each function loads it at cold start
and looks for a newer one every :data:`REFRESH_SECONDS`, off the request path. A function that has never loaded one
serves :data:`EMPTY`: no third-party bidder and no viewer address, the fail-safe answer. It holds no per-viewer data.
"""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from functools import cached_property
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Any, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
    field_serializer,
    field_validator,
    model_validator,
)

from fast_channels.core.pds.book import Book, Creative, Line
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
    lines: tuple[Line, ...] = Field(default=(), description="The own book's direct lines.")
    breaks_ms: tuple[int, ...] = Field(
        default=(), description="The break lengths of the schedule of record (the default ladder's 30.03 s grid)."
    )

    @field_serializer("shed")
    def _sorted(self, shed: frozenset[str]) -> list[str]:
        # Sorted, so the same content always serializes, and hashes, the same.
        return sorted(shed)


class Snapshot(SnapshotBase):
    """One published snapshot."""

    v: Literal[1] = 1
    version: str = Field(pattern=r"^\d{8}T\d{6}Z-[0-9a-f]{12}$", description="``<UTC time>-<content digest>``.")
    generated_at: datetime
    origin_facing: tuple[str, ...] = Field(
        default=(), description="CloudFront's origin-facing networks (ip-ranges.json CLOUDFRONT_ORIGIN_FACING)."
    )
    ip_ranges: str | None = Field(default=None, description="``syncToken`` of the ip-ranges.json they came from.")
    ladder_version: str | None = Field(default=None, description="The ladder of the own book's creatives.")
    creatives: tuple[Creative, ...] = Field(default=(), description="The lines' creatives that the kit encoded.")
    house: tuple[Creative, ...] = Field(default=(), description="The house ads of the ladder's kit.")
    _book: Book = PrivateAttr(default_factory=Book)

    @model_validator(mode="after")
    def _own_book(self) -> Self:
        self._book = Book(
            ladder_version=self.ladder_version, lines=self.lines, creatives=self.creatives, house=self.house
        )
        return self

    @property
    def book(self) -> Book:
        """The own book: the lines with their creatives, and the house ads."""
        return self._book

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


def from_ip_ranges(document: dict[str, Any] | None) -> tuple[tuple[str, ...], str | None]:
    """CloudFront's origin-facing networks and the ``syncToken`` of an ip-ranges.json document (none for ``None``)."""
    if not document:
        return (), None
    networks = tuple(str(network) for network in origin_facing_networks(document))
    sync = document.get("syncToken")
    return networks, str(sync) if sync is not None else None


def build(
    base: SnapshotBase,
    *,
    origin_facing: tuple[str, ...] = (),
    ip_ranges: str | None = None,
    own: Book | None = None,
    now: datetime,
) -> Snapshot:
    """A snapshot of ``base`` with CloudFront's origin-facing networks (none: no viewer address until a later
    publish), the ``syncToken`` of the ip-ranges.json they came from, and the own book's creatives and house ads
    (``own``; its lines are ``base``'s).

    Its version ends with a digest of the content the functions act on: the sync token is left out, because AWS
    republishes ip-ranges.json for any service's change.
    """
    book = own or Book()
    content = {
        **base.model_dump(mode="json"),
        "origin_facing": sorted(str(ip_network(n)) for n in origin_facing),
        "ladder_version": book.ladder_version,
        "creatives": [c.model_dump(mode="json") for c in book.creatives],
        "house": [c.model_dump(mode="json") for c in book.house],
    }
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    return Snapshot.model_validate(
        {
            **content,
            "ip_ranges": ip_ranges,
            "version": f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{digest}",
            "generated_at": now,
        }
    )


def digest(snapshot: Snapshot) -> str:
    """The content digest a snapshot's version ends with: equal digests, equal content."""
    return snapshot.version.rsplit("-", 1)[1]


def history_key(snapshot: Snapshot) -> str:
    """Where the publisher keeps a copy of ``snapshot``."""
    return f"{HISTORY_PREFIX}{snapshot.version}.json"
