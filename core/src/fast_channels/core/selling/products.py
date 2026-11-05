"""The stage's inventory as AdCP 3.1.1 products: one non-guaranteed mid-roll product per monetized channel.

Each product names the channel's break inventory (the default ladder's break lengths, sold in whole 30 s slots) and
the genres of the catalog its programs come from. The seller stores what its database can hold: a product row and
one fixed CPM pricing option (``vendor/agentic-selling/seller/fast/bridge.py``). Collections and installments do not
round-trip through the Prebid Sales Agent, and a channel cannot be a 3.1.1 collection (W-8 A1, A3), so the genres
and breaks travel in the description and the placement.
"""

from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.timeline import whole_seconds

SLOT_SECONDS: Final = 30
"""What one ad fills: the 30.03 s slot of the 29.97 fps grid, an ad of 30 s (``rqddurs``)."""
FORMAT: Final = {"agent_url": "https://creative.adcontextprotocol.org", "id": "video_ctv_midroll_30s"}
"""The AdCP reference format of a 30 s CTV mid-roll (a federation identity: the seller in testing mode reads the
format from its checked-in reference catalog and dials nothing for it)."""
CURRENCY: Final = "USD"
REPORTING: Final = {
    "available_reporting_frequencies": ["hourly", "daily"],
    "expected_delay_minutes": 5,
    "timezone": "UTC",
    "supports_webhooks": False,
    "available_metrics": ["impressions", "spend"],
    "date_range_support": "date_range",
}
"""What delivery reporting the product offers: our tracker's impressions and their spend, a seller-side server count
(not an accredited measurement), refreshed every minute."""


class ProductSpec(BaseModel):
    """One product as the seller stores it: its row's columns and its fixed CPM."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    channel: str = Field(description="The monetized channel it sells (its booked lines are sold on it only).")
    name: str = Field(max_length=200)
    description: str
    format_ids: tuple[dict[str, str], ...] = (FORMAT,)
    delivery_type: str = "non_guaranteed"
    channels: tuple[str, ...] = ("ctv",)
    placements: tuple[dict[str, Any], ...]
    reporting_capabilities: dict[str, Any] = Field(default_factory=lambda: dict(REPORTING))
    cpm: Decimal = Field(gt=0)
    currency: str = CURRENCY


class Terms(BaseModel):
    """What the deployment sells (``modules.selling``): the sync function's ``SELLING_PRODUCTS``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channels: tuple[str, ...] = Field(min_length=1, description="The monetized channels (``release.channels``).")
    breaks_ms: tuple[int, ...] = Field(min_length=1, description="The default ladder's break lengths.")
    cpm: Decimal = Field(gt=0)
    publisher_domain: str
    creatives: tuple[str, ...] = Field(min_length=1, description="The direct demo creatives booked lines serve.")

    def products(self, genres: Iterable[str]) -> tuple[ProductSpec, ...]:
        """The products, with the catalog's ``genres``."""
        return products(
            self.channels, genres=genres, breaks_ms=self.breaks_ms, cpm=self.cpm, publisher_domain=self.publisher_domain
        )


def product_id(channel: str) -> str:
    """The product of a monetized channel."""
    return f"{channel}-midroll"


def break_seconds(breaks_ms: Iterable[int]) -> list[int]:
    """The break lengths in whole seconds, shortest first."""
    return sorted({whole_seconds(ms) for ms in breaks_ms})


def _listed(items: Sequence[str]) -> str:
    return " or ".join([", ".join(items[:-1]), items[-1]] if len(items) > 1 else items)


def products(
    channels: Sequence[str],
    *,
    genres: Iterable[str],
    breaks_ms: Iterable[int],
    cpm: Decimal,
    publisher_domain: str,
) -> tuple[ProductSpec, ...]:
    """The products of ``channels``, each at ``cpm``, describing the breaks (``breaks_ms``) and the catalog's
    ``genres``."""
    lengths = [f"{seconds} s" for seconds in break_seconds(breaks_ms)]
    named = sorted({genre.strip().lower() for genre in genres if genre.strip()})
    programming = (
        f"Programs from the catalog's genres: {', '.join(named)}." if named else "The catalog lists no genres yet."
    )
    return tuple(
        ProductSpec(
            product_id=product_id(channel),
            channel=channel,
            name=f"{channel}: 30 s mid-roll",
            description=(
                f"Server-side stitched {SLOT_SECONDS} s mid-roll ads on the FAST channel {channel} of "
                f"{publisher_domain}. Its breaks last {_listed(lengths)}, sold in whole {SLOT_SECONDS} s slots; a slot "
                f"left unsold plays the break slate. {programming} Delivery is counted from the publisher's own "
                "server-side impression tracker (not an accredited measurement)."
            ),
            placements=(
                {
                    "kind": "seller_inline",
                    "placement_id": f"{channel}-midroll",
                    "name": f"Mid-roll pod ({SLOT_SECONDS} s slots)",
                    "mode": "included",
                    "video_placement_types": ["instream"],
                },
            ),
            cpm=cpm,
        )
        for channel in channels
    )
