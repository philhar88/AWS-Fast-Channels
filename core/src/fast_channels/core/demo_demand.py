"""Demo demand: the creatives the bidder simulator bids and the own book's synthetic direct lines serve.

In ``modules.demand.mode: demo`` the ladder kit also encodes one ``DEMO_AD_GOPS`` (30.03 s) ad per demo creative from
a brand still (``assets/demo``, never through the ingest bucket):

- ``sim1`` to ``sim8`` for the bidder simulator, each with its own creative id and advertiser domain except ``sim3``
  and ``sim4``, which share ``brand-c.example`` so the pod's advertiser separation shows;
- ``direct1`` and ``direct2`` for the own book's synthetic direct lines (``modules.pds.demo_lines``).

The kit writes their index, ``ads/<ladder>/demo/creatives.json`` (:class:`DemoIndex`), from which the deploy script
renders the simulator's creative list (overlay O5, :func:`simulator_creatives`). Every advertiser, domain and price is
simulated.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.naming import ladder_path
from fast_channels.core.timeline import DEMO_AD_GOPS, whole_seconds

__all__ = [
    "DEMO_AD_GOPS",
    "DEMO_CREATIVES",
    "DemoCreative",
    "DemoIndex",
    "DemoRole",
    "IndexedCreative",
    "InlineCreative",
    "demo_index",
    "demo_index_key",
    "simulator_creatives",
]


class DemoRole(StrEnum):
    """Who serves a demo creative."""

    SIMULATOR = "simulator"
    """The guidance's bidder simulator (``amt``)."""
    DIRECT = "direct"
    """The own book, as a synthetic direct line."""


class DemoCreative(BaseModel):
    """One demo creative: who advertises it and what the simulator bids for it."""

    model_config = ConfigDict(frozen=True)

    crid: str = Field(pattern=r"^[a-z0-9]+$")
    role: DemoRole
    advertiser: str
    adomain: str
    price: Decimal = Field(gt=0)
    """Simulated CPM (US dollars): the simulator's bid, or the direct line's reference price."""


def _simulator(n: int, letter: str, price: str) -> DemoCreative:
    return DemoCreative(
        crid=f"sim{n}",
        role=DemoRole.SIMULATOR,
        advertiser=f"Brand {letter}",
        adomain=f"brand-{letter.lower()}.example",
        price=Decimal(price),
    )


def _direct(n: int, price: str) -> DemoCreative:
    return DemoCreative(
        crid=f"direct{n}",
        role=DemoRole.DIRECT,
        advertiser=f"Direct Advertiser {n}",
        adomain=f"direct{n}.example",
        price=Decimal(price),
    )


DEMO_CREATIVES: Final[tuple[DemoCreative, ...]] = (
    _simulator(1, "A", "12.50"),
    _simulator(2, "B", "22.50"),
    _simulator(3, "C", "35.00"),
    _simulator(4, "C", "12.50"),  # sim3's advertiser: the pod separates them
    _simulator(5, "E", "22.50"),
    _simulator(6, "F", "35.00"),
    _simulator(7, "G", "12.50"),
    _simulator(8, "H", "22.50"),
    _direct(1, "8.00"),
    _direct(2, "6.00"),
)
"""The demo creatives, in the order the kit encodes them (the prices are the Phase-0 break table's)."""


class IndexedCreative(DemoCreative):
    """A demo creative as the kit encoded it on one ladder."""

    creative_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    """The immutable creative id MediaTailor caches the preconditioned ad by (``<asset>-<ladder>-<hosthash>``)."""
    hls_uri: str = Field(pattern=r"^https://")
    """The ad's multivariant copy under ``ads/<ladder>/<creative id>/`` on the CDN."""
    duration_ms: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class DemoIndex(BaseModel):
    """``ads/<ladder>/demo/creatives.json``: the demo creatives of one ladder version."""

    model_config = ConfigDict(frozen=True)

    v: int = 1
    ladder_version: str
    creatives: tuple[IndexedCreative, ...]


def demo_index_key(ladder_version: str) -> str:
    """Media-bucket key of a ladder's demo creative index."""
    return f"ads/{ladder_path(ladder_version)}/demo/creatives.json"


def demo_index(
    ladder_version: str, encoded: Mapping[str, tuple[str, str, int]], *, width: int, height: int
) -> DemoIndex:
    """The index of the demo creatives ``encoded`` holds: ``crid`` to (creative id, HLS URI, duration in ms)."""
    if unknown := sorted(encoded.keys() - {c.crid for c in DEMO_CREATIVES}):
        raise ValueError(f"not demo creatives: {unknown}")
    return DemoIndex(
        ladder_version=ladder_version,
        creatives=tuple(
            IndexedCreative(
                **c.model_dump(),
                creative_id=encoded[c.crid][0],
                hls_uri=encoded[c.crid][1],
                duration_ms=encoded[c.crid][2],
                width=width,
                height=height,
            )
            for c in DEMO_CREATIVES
            if c.crid in encoded
        ),
    )


def _duration(ms: int) -> str:
    seconds, millis = divmod(ms, 1000)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}.{millis:03d}"


class InlineCreative(Protocol):
    """What an inline ad of a preconditioned creative is made of (:class:`IndexedCreative`, the own book's)."""

    @property
    def crid(self) -> str: ...
    @property
    def advertiser(self) -> str: ...
    @property
    def creative_id(self) -> str: ...
    @property
    def hls_uri(self) -> str: ...
    @property
    def duration_ms(self) -> int: ...
    @property
    def width(self) -> int: ...
    @property
    def height(self) -> int: ...


def vast_ad(creative: InlineCreative, *, ad_system: str = "fast-channels demo") -> str:
    """One inline VAST 4.2 ad of a preconditioned creative (the simulator's and the own book's ``adm``): no impression
    of its own, so the fast-ctv module's notices and tracker are its impressions. No ``<Advertiser>`` when it has
    none (a house ad)."""
    c = creative
    advertiser = f"<Advertiser>{escape(c.advertiser)}</Advertiser>" if c.advertiser else ""
    return (
        f'<VAST version="4.2"><Ad id={quoteattr(c.crid)}><InLine><AdSystem>{escape(ad_system)}</AdSystem>'
        f"<AdTitle>{escape(c.crid)}</AdTitle>{advertiser}"
        f"<Creatives><Creative id={quoteattr(c.creative_id)} adId={quoteattr(c.crid)}>"
        f'<UniversalAdId idRegistry="fast-channels">{escape(c.creative_id)}</UniversalAdId>'
        f"<Linear><Duration>{_duration(c.duration_ms)}</Duration><MediaFiles>"
        f'<MediaFile delivery="streaming" type="application/x-mpegURL" width="{c.width}" height="{c.height}">'
        f"<![CDATA[{c.hls_uri}]]></MediaFile></MediaFiles></Linear></Creative></Creatives></InLine></Ad></VAST>"
    )


def simulator_creatives(index: DemoIndex, *, beacon_url: str) -> list[dict[str, Any]]:
    """The bidder simulator's ``video_creatives.json`` (overlay O5): its demo creatives with their ``adm`` and a
    billing notice on the CDN's beacon (``${AUCTION_PRICE}`` left for the module to fill)."""
    return [
        {
            "crid": c.crid,
            "adomain": [c.adomain],
            "price": float(c.price),
            "dur": whole_seconds(c.duration_ms),
            "w": c.width,
            "h": c.height,
            "adm": vast_ad(c),
            "burl": f"{beacon_url}?seat=amt&cr={quote(c.crid)}&p=${{AUCTION_PRICE}}&e=burl",
        }
        for c in index.creatives
        if c.role is DemoRole.SIMULATOR
    ]


def crids(role: DemoRole | None = None) -> Sequence[str]:
    """The demo creative ids, of one role or all."""
    return [c.crid for c in DEMO_CREATIVES if role is None or c.role is role]
