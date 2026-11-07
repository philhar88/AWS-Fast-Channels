"""From buys booked at the seller to direct lines of the PDS own book, and from the lines' delivery back to the buys.

The seller's bridge (``vendor/agentic-selling/seller/fast/bridge.py``) reads the tenant's media buys (:class:`Buy`)
and sends them to the sync function, which publishes their lines with :func:`booked_lines` and answers with each
buy's delivery (:func:`delivery`). One package is one line:

- **id** ``adcp-<media buy>-<n>`` (:func:`line_id`);
- **advertiser and brand**: the buy's advertiser and its brand's domain (``adomain``);
- **budget, CPM and flight**: the package's budget and fixed CPM, the buy's start and end;
- **channel**: the channel of the package's product, the only one the line airs on;
- **creative**: with the creatives module, a READY creative registered for the buy's brand (``core.creatives``),
  held while the buy runs; else one of the stage's configured direct demo creatives (``modules.selling.creatives``).
  ``OPTIMIZATION(aws): W-10 item 6``: the AWS reference buyer has no creative tasks, so a buy carries no creative of
  its own and stays ``pending_creatives`` at the seller; the brand's creatives are registered by an operator
  (``scripts/creatives.py``), and the demo creative is the fallback.

A buy is served while it is in a serving state and not paused, before its end. A package without a fixed USD CPM, a
budget, a known product or a free creative is not served; :class:`Booking` says why.
"""

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from fast_channels.core.catalog import DOMAIN_PATTERN
from fast_channels.core.pds.book import LINE_ID_PATTERN, Line
from fast_channels.core.selling.products import CURRENCY, ProductSpec

LINE_PREFIX: Final = "adcp-"
"""Booked lines' ids start with it, so they never meet a demo line's (``modules.pds.demo_lines``)."""
SERVED: Final = frozenset(
    {"pending_creatives", "pending_start", "pending_activation", "scheduled", "approved", "ready", "active"}
)
"""The seller's persisted media-buy states whose packages are served (its ``PersistedMediaBuyStatus``). Not served:
``draft``, ``pending`` and ``pending_approval`` (not committed), ``paused``, ``completed``, ``canceled``, ``rejected``
and ``failed``."""
_LINE_ID: Final = re.compile(LINE_ID_PATTERN)


class Package(BaseModel):
    """A package of a buy, as the seller stored it (``media_packages``)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    package_id: str = Field(min_length=1, max_length=100)
    product_id: str = Field(min_length=1, max_length=100)
    budget: Decimal | None = Field(default=None, description="The package's budget in the buy's currency.")
    cpm: Decimal | None = Field(default=None, description="Its fixed CPM (``pricing_info.rate``), when it has one.")
    pricing_model: str | None = None


class Buy(BaseModel):
    """A media buy of the seller's tenant (``media_buys``), as the bridge reads it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    media_buy_id: str = Field(min_length=1, max_length=100)
    status: str
    paused: bool = False
    created_at: AwareDatetime
    starts: AwareDatetime
    ends: AwareDatetime
    currency: str = CURRENCY
    advertiser: str = Field(min_length=1, max_length=255)
    brand: str | None = Field(default=None, description="The brand's domain (the request's ``brand.domain``).")
    packages: tuple[Package, ...] = ()

    def served(self, now: datetime) -> bool:
        """Whether the buy's packages air: committed, not paused and not ended."""
        return self.status in SERVED and not self.paused and now < self.ends


class Unserved(StrEnum):
    """Why a package has no line."""

    PRODUCT = "unknown_product"
    PRICING = "no_fixed_usd_cpm"
    BUDGET = "no_budget"
    BRAND = "no_brand_domain"
    CREATIVE = "no_free_creative"


class Booking(BaseModel):
    """The lines of the served buys' packages, and the packages left without one."""

    model_config = ConfigDict(frozen=True)

    lines: tuple[Line, ...] = ()
    packages: dict[str, tuple[str, str]] = Field(
        default_factory=dict, description="Line id -> (media buy id, package id)."
    )
    unserved: dict[str, Unserved] = Field(default_factory=dict, description="'<media buy>/<package>' -> why.")


def line_id(media_buy_id: str, index: int) -> str:
    """The line of a buy's ``index``-th package (from 1): ``adcp-<media buy>-<n>``, or ``adcp-<digest>`` when the
    buy's id does not fit a line id."""
    slug = re.sub(r"[^a-z0-9]+", "-", media_buy_id.lower()).strip("-")
    candidate = f"{LINE_PREFIX}{slug}-{index}"
    if _LINE_ID.fullmatch(candidate):
        return candidate
    digest = hashlib.sha256(f"{media_buy_id}/{index}".encode()).hexdigest()[:16]
    return f"{LINE_PREFIX}{digest}"


def _is_domain(value: str | None) -> bool:
    return bool(value) and re.fullmatch(DOMAIN_PATTERN, value or "") is not None


def _assign(
    wanted: Sequence[tuple[str, str]],
    held: Mapping[str, str],
    pool: Sequence[str],
    brands: Mapping[str, Sequence[str]],
) -> dict[str, str]:
    """A creative for each wanted (line id, brand), in booking order, none twice: a line keeps its brand's
    registered creative; else it takes a free one of its brand; else it keeps its pool creative; else it takes a free
    one of the pool. A line left out gets none."""
    assigned: dict[str, str] = {}
    taken: set[str] = set()

    def kept(line: str, among: Sequence[str]) -> str | None:
        return crid if (crid := held.get(line)) in among else None

    def free(among: Sequence[str]) -> str | None:
        return next((crid for crid in among if crid not in taken), None)

    rounds: tuple[Callable[[str, str], str | None], ...] = (
        lambda line, brand: kept(line, brands.get(brand, ())),
        lambda _, brand: free(brands.get(brand, ())),
        lambda line, _: kept(line, pool),
        lambda _line, _brand: free(pool),
    )
    for pick in rounds:
        for line, brand in wanted:
            if line not in assigned and (crid := pick(line, brand)) is not None and crid not in taken:
                assigned[line] = crid
                taken.add(crid)
    return assigned


def booked_lines(
    buys: Iterable[Buy],
    *,
    products: Sequence[ProductSpec],
    pool: Sequence[str],
    registered: Mapping[str, Sequence[str]] | None = None,
    current: Iterable[Line] = (),
    now: datetime,
) -> Booking:
    """The lines of the served packages of ``buys``.

    Each line serves one creative, which no other line serves: its brand's registered creative if one is free, else
    one of the ``pool``. A line keeps its creative while its buy runs, but moves from the pool to its brand's own
    creative as soon as one is free.

    Args:
        buys: the tenant's media buys.
        products: the seller's products (a package's product gives its line's channel).
        pool: the demo creatives booked lines may serve.
        registered: the READY registered creatives by brand domain (the creatives module's; none without it).
        current: the lines published before.
        now: the time of the sync.
    """
    channel = {product.product_id: product.channel for product in products}
    brands = registered or {}
    held = {line.id: line.creatives[0] for line in current if line.creatives}
    wanted: list[tuple[str, Buy, Package]] = []
    unserved: dict[str, Unserved] = {}
    for buy in sorted(buys, key=lambda b: (b.created_at, b.media_buy_id)):
        if not buy.served(now):
            continue
        for index, package in enumerate(buy.packages, start=1):
            key = f"{buy.media_buy_id}/{package.package_id}"
            if package.product_id not in channel:
                unserved[key] = Unserved.PRODUCT
            elif package.cpm is None or package.cpm <= 0 or buy.currency != CURRENCY:
                unserved[key] = Unserved.PRICING
            elif package.budget is None or package.budget <= 0:
                unserved[key] = Unserved.BUDGET
            elif not _is_domain(buy.brand):
                unserved[key] = Unserved.BRAND
            else:
                wanted.append((line_id(buy.media_buy_id, index), buy, package))
    assigned = _assign([(id_, str(buy.brand)) for id_, buy, _ in wanted], held, pool, brands)
    lines: list[Line] = []
    packages: dict[str, tuple[str, str]] = {}
    for id_, buy, package in wanted:
        if (crid := assigned.get(id_)) is None:
            unserved[f"{buy.media_buy_id}/{package.package_id}"] = Unserved.CREATIVE
            continue
        assert package.cpm is not None and package.budget is not None  # checked above
        lines.append(
            Line(
                id=id_,
                advertiser=buy.advertiser[:100],
                adomain=str(buy.brand),
                creatives=(crid,),
                cpm=package.cpm,
                budget=package.budget,
                starts=buy.starts,
                ends=buy.ends,
                channels=(channel[package.product_id],),
            )
        )
        packages[id_] = (buy.media_buy_id, package.package_id)
    return Booking(lines=tuple(lines), packages=packages, unserved=unserved)


class PackageDelivery(BaseModel):
    """What one package delivered: our tracker's impressions of its line and what they cost."""

    model_config = ConfigDict(frozen=True)

    package_id: str
    impressions: int = Field(ge=0)
    spend: Decimal = Field(ge=0)


class BuyDelivery(BaseModel):
    """What one buy delivered, for the seller's ``get_media_buy_delivery``."""

    model_config = ConfigDict(frozen=True)

    media_buy_id: str
    currency: str = CURRENCY
    impressions: int = Field(ge=0)
    spend: Decimal = Field(ge=0)
    packages: tuple[PackageDelivery, ...]
    source: Literal["seller server count, not MRC"] = "seller server count, not MRC"
    """Our own tracker's count: what bills is the counterparty's (docs/pds.md#reservations-and-delivery)."""


def impressions(delivered: Decimal, line: Line) -> int:
    """The impressions a line's ``delivered`` amount stands for (each delivers exactly its price, ``cpm / 1000``)."""
    return int((delivered / line.insertion).to_integral_value(rounding=ROUND_HALF_UP))


def delivery(booking: Booking, delivered: Mapping[str, Decimal]) -> list[BuyDelivery]:
    """Each booked buy's delivery from its lines' ``delivered`` amounts (line id -> the sum over its budget shards;
    a line not read delivered nothing)."""
    by_buy: dict[str, list[PackageDelivery]] = {}
    for line in booking.lines:
        media_buy_id, package_id = booking.packages[line.id]
        amount = delivered.get(line.id, Decimal(0))
        by_buy.setdefault(media_buy_id, []).append(
            PackageDelivery(package_id=package_id, impressions=impressions(amount, line), spend=amount)
        )
    return [
        BuyDelivery(
            media_buy_id=media_buy_id,
            impressions=sum(p.impressions for p in packages),
            spend=sum((p.spend for p in packages), Decimal(0)),
            packages=tuple(packages),
        )
        for media_buy_id, packages in by_buy.items()
    ]
