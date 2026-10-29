"""The own book: the publisher's house ads and direct lines, bid into every pod as an OpenRTB bidder.

Prebid Server reaches it as ``ownbook``, a ``generic`` alias on the PDS's ``/ownbook/openrtb`` (OpenRTB 2.6). For each
imp it bids every house ad, and every creative of each line with budget left, whose duration the slot accepts
(``rqddurs``):

- **house ads** (the ladder kit's) bid the sentinel price :data:`HOUSE_PRICE`, so any paid bid wins, and carry
  ``ext.fast.house: true``. They are the publisher's own fillers and are never sold, so they carry no ``adomain``;
- **lines** bid their CPM with ``ext.fast.line``. A tier-0 line (sold as guaranteed) bids with its deal id
  (:func:`deal_id`); the snapshot lists those among the tier-0 deals, so the pod rules place it first.

Each ``adm`` is an inline VAST 4.2 ad of a creative preconditioned on the stage's default ladder (``ads/<ladder>/…`` on
the CDN). It carries no impression, and the bid no ``burl``: Prebid Server sends bidders neither the session nor the
break, so the own book cannot build our tracker. The fast-ctv module puts the tracker on every ad it renders, and that
is what delivery counts. The reservation that ``/select`` makes enforces a line's budget; the own book only stops
bidding a line once its budget is reserved.
"""

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any, Final, Self
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fast_channels.core.demo_demand import DemoIndex, vast_ad
from fast_channels.core.pds.models import NAME_PATTERN, OWN_BOOK, Money

HOUSE_PRICE: Final = Decimal("0.01")
"""A house ad's bid: below any paid bid, so a filler never takes a slot that a sale could fill."""
AD_SYSTEM: Final = "fast-channels own book"
VAST_NS: Final = "{http://www.iab.com/VAST}"
"""The kit's VAST pod (``ads/<ladder>/vast.xml``) is in the VAST namespace."""
LINE_ID_PATTERN: Final = r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$"


def deal_id(line: str) -> str:
    """The deal id of a tier-0 line's bids."""
    return f"fast-{line}"


class Creative(BaseModel):
    """A preconditioned ad that the own book can bid."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    crid: str = Field(pattern=NAME_PATTERN, description="The creative id that bids carry and trackers count.")
    creative_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", description="MediaTailor's immutable creative id.")
    hls_uri: str = Field(pattern=r"^https://", description="Its multivariant copy on the CDN.")
    duration_ms: int = Field(gt=0)
    width: int = Field(default=1920, gt=0)
    height: int = Field(default=1080, gt=0)
    advertiser: str = Field(default="", max_length=100)

    @property
    def seconds(self) -> int:
        """Whole seconds, rounded half up (``bid.dur``)."""
        return (self.duration_ms + 500) // 1000


class Line(BaseModel):
    """A direct line (in demo mode, one of ``modules.pds.demo_lines``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=LINE_ID_PATTERN)
    advertiser: str = Field(min_length=1, max_length=100)
    adomain: str = Field(min_length=1, max_length=253)
    cat: tuple[str, ...] = ()
    creatives: tuple[str, ...] = Field(min_length=1, description="The crids it serves.")
    cpm: Money
    budget: Money
    per_session: int | None = Field(default=None, ge=1, description="Most impressions per playback session.")
    tier0: bool = False

    @property
    def insertion(self) -> Decimal:
        """What one impression costs the line: ``cpm / 1000``."""
        return self.cpm / 1000


class Book(BaseModel):
    """The own book in the snapshot: the lines, their creatives and the house ads, all on one ladder."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ladder_version: str | None = Field(default=None, description="The ladder its creatives are preconditioned on.")
    lines: tuple[Line, ...] = ()
    creatives: tuple[Creative, ...] = Field(default=(), description="The lines' creatives that the kit encoded.")
    house: tuple[Creative, ...] = Field(default=(), description="The house ads (the ladder kit's).")

    @model_validator(mode="after")
    def _one_line_per_creative(self) -> Self:
        ids = [line.id for line in self.lines]
        if len(ids) != len(set(ids)):
            raise ValueError("line ids must be unique")
        served = [crid for line in self.lines for crid in line.creatives]
        if len(served) != len(set(served)):
            raise ValueError("a creative serves at most one line")
        crids = [c.crid for c in (*self.creatives, *self.house)]
        if len(crids) != len(set(crids)):
            raise ValueError("creative ids must be unique")
        return self

    @property
    def tier0_deals(self) -> frozenset[str]:
        return frozenset(deal_id(line.id) for line in self.lines if line.tier0)

    def line_of(self, crid: str) -> Line | None:
        """The line that serves ``crid``, if any."""
        return next((line for line in self.lines if crid in line.creatives), None)


def _milliseconds(duration: str) -> int:
    hours, minutes, seconds = duration.strip().split(":")
    return round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)


def house_creatives(document: str) -> tuple[Creative, ...]:
    """The house ads of the kit's VAST pod (``ads/<ladder>/vast.xml``, which repeats them), in order, as
    ``house1``, ``house2``, …

    Raises:
        ValueError: the document is not a VAST pod of inline streaming ads.
    """
    try:
        # Our own kit's document from our private media bucket, not third-party input.
        root = ET.fromstring(document)  # noqa: S314
    except ET.ParseError as error:
        raise ValueError(f"not XML: {error}") from None
    seen: dict[str, Creative] = {}
    for creative in root.iterfind(f"{VAST_NS}Ad/{VAST_NS}InLine/{VAST_NS}Creatives/{VAST_NS}Creative"):
        creative_id = creative.get("id", "")
        if creative_id in seen:
            continue
        media = creative.find(f"{VAST_NS}Linear/{VAST_NS}MediaFiles/{VAST_NS}MediaFile")
        duration = creative.findtext(f"{VAST_NS}Linear/{VAST_NS}Duration")
        if media is None or duration is None or not (media.text or "").strip():
            raise ValueError(f"house ad {creative_id!r} has no duration or streaming media file")
        seen[creative_id] = Creative(
            crid=f"house{len(seen) + 1}",
            creative_id=creative_id,
            hls_uri=(media.text or "").strip(),
            duration_ms=_milliseconds(duration),
            width=int(media.get("width", "1920")),
            height=int(media.get("height", "1080")),
        )
    return tuple(seen.values())


def book(lines: Sequence[Line], *, house: Sequence[Creative], index: DemoIndex | None) -> Book:
    """The own book of ``lines`` on the ladder of ``index`` (the kit's demo creatives): a line creative that the kit
    did not encode is left out, and a line left with none bids nothing.

    Raises:
        pydantic.ValidationError: two lines serve one creative, or share an id.
    """
    encoded = {c.crid: c for c in index.creatives} if index is not None else {}
    creatives = tuple(
        Creative(
            crid=c.crid,
            creative_id=c.creative_id,
            hls_uri=c.hls_uri,
            duration_ms=c.duration_ms,
            width=c.width,
            height=c.height,
            advertiser=line.advertiser,
        )
        for line in lines
        for crid in line.creatives
        if (c := encoded.get(crid)) is not None
    )
    return Book(
        ladder_version=index.ladder_version if index is not None else None,
        lines=tuple(lines),
        creatives=creatives,
        house=tuple(house),
    )


class Imp(BaseModel):
    """What the own book reads of an OpenRTB imp (Prebid Server sends far more)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(pattern=NAME_PATTERN)
    video: dict[str, Any] | None = None

    @property
    def durations(self) -> frozenset[int]:
        """The ad durations the slot accepts: ``rqddurs``, else none (only structured pods are bid)."""
        durations = (self.video or {}).get("rqddurs")
        if not isinstance(durations, list):
            return frozenset()
        return frozenset(d for d in durations if isinstance(d, int) and not isinstance(d, bool) and d > 0)


class BidRequest(BaseModel):
    """What the own book reads of an OpenRTB request."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    imp: tuple[Imp, ...] = Field(default=(), max_length=16)


def _bid(imp: Imp, creative: Creative, price: Decimal, ext: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": f"{imp.id}-{creative.crid}",
        "impid": imp.id,
        "price": float(price),
        "adm": vast_ad(creative, ad_system=AD_SYSTEM),
        "crid": creative.crid,
        "dur": creative.seconds,
        "w": creative.width,
        "h": creative.height,
        "mtype": 2,
        "ext": {"fast": dict(ext)},
    }


def bids(request: BidRequest, own: Book, *, exhausted: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The own book's bids for every imp of ``request``: its house ads, then the creatives of each line with budget
    left, that fit the imp.

    Args:
        request: the OpenRTB request that Prebid Server sent the ``ownbook`` alias.
        own: the snapshot's own book.
        exhausted: lines whose budget is reserved (not bid).
    """
    spent = frozenset(exhausted)
    creatives = {creative.crid: creative for creative in own.creatives}
    placed: list[dict[str, Any]] = []
    for imp in request.imp:
        fits = imp.durations
        placed.extend(
            _bid(imp, creative, HOUSE_PRICE, {"house": True}) for creative in own.house if creative.seconds in fits
        )
        for line in own.lines:
            if line.id in spent:
                continue
            for crid in line.creatives:
                if (creative := creatives.get(crid)) is None or creative.seconds not in fits:
                    continue
                bid = _bid(imp, creative, line.cpm, {"line": line.id})
                bid["adomain"] = [line.adomain]
                if line.cat:
                    bid["cat"] = list(line.cat)
                if line.tier0:
                    bid["dealid"] = deal_id(line.id)
                placed.append(bid)
    return placed


def response(request: BidRequest, own: Book, *, exhausted: Iterable[str] = ()) -> dict[str, Any] | None:
    """The OpenRTB response, or None for no bid (HTTP 204)."""
    placed = bids(request, own, exhausted=exhausted)
    if not placed:
        return None
    return {"id": request.id, "cur": "USD", "seatbid": [{"seat": OWN_BOOK, "bid": placed}]}
