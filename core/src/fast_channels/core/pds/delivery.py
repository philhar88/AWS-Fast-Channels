"""Reservations and delivery states of the own book's lines (ADR-0022): the keys, the budget shards and our tracker's
beacons, as MediaTailor logs them.

An own-book line ad that ``/select`` places moves through these states:

``RESERVED``
    One ``TransactWriteItems`` takes the ad's price (``cpm / 1000``) from a budget shard of its line, counts it
    against the line's per-session cap, writes the reservation and appends a money event to the ledger. Either all
    of it happens or none: a line's liability never exceeds its budget.
``DELIVERED``
    MediaTailor fired our tracker for the ad (a ``BEACON_FIRED`` event in the ad decision server log).
``RELEASED``
    No impression :data:`RELEASE_AFTER` the reservation (MediaTailor retries a beacon for up to an hour): the price
    goes back to the shard. An impression that still arrives later is delivered anyway and recorded as late.
``RECONCILED``
    Later, against the counterparty's count, which is what bills; ours is a seller-side server count.

Fills are counted once per break, ``(sid, evid)``, and impressions once per ad, ``(sid, evid, crid)``: a pod holds
several ads, and MediaTailor may fire a beacon more than once.
"""

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.pbs_config import TRACKER_PATH
from fast_channels.core.pds.buckets import bucket
from fast_channels.core.pds.models import EVID_PATTERN, NAME_PATTERN, SID_PATTERN, decision_id
from fast_channels.core.pds.pod import TRACKER_EVENT

RELEASE_AFTER: Final = timedelta(hours=2)
"""How long a reservation waits for its impression before its price is released."""
BUDGET_SHARDS: Final = 4
"""Budget shards per line: reservations spread over them, so no single item takes every write."""
SHARD_SEED: Final = "budget-v1"
PENDING: Final = "RESERVED"
"""The ``pending`` attribute of an open reservation: the table's sparse ``pending`` index lists exactly those."""
PRINCIPAL: Final = "pds"
"""The principal of the PDS's money events in the ledger."""
CURRENCY: Final = "USD"
MONEY_PLACES: Final = Decimal("0.000001")


class State(StrEnum):
    """A reservation's delivery state."""

    RESERVED = "RESERVED"
    DELIVERED = "DELIVERED"
    RELEASED = "RELEASED"
    RECONCILED = "RECONCILED"


class Credit(StrEnum):
    """What an impression did to its ad's reservation."""

    DELIVERED = "delivered"
    """The reservation went from RESERVED to DELIVERED."""
    LATE = "late"
    """It had been RELEASED: the price is taken from the shard again, even past the budget."""
    UNRESERVED = "unreserved"
    """No reservation: a house ad or a third party's."""
    REPEAT = "repeat"
    """It was already DELIVERED (a repeated beacon)."""


class Action(StrEnum):
    """The ledger action of a money event."""

    BUDGET = "money.budget"
    """A budget shard got its share of the line's budget (new, or changed)."""
    RESERVE = "money.reserve"
    DELIVER = "money.deliver"
    RELEASE = "money.release"


type Key = dict[str, str]


def budget_key(line: str, shard: int) -> Key:
    return {"pk": f"BUDGET#{line}", "sk": f"S#{shard}"}


def cap_key(sid: str, line: str) -> Key:
    return {"pk": f"CAP#{sid}", "sk": line}


def reservation_key(decision: str, crid: str) -> Key:
    """A pod never repeats a creative, so ``(decision, crid)`` names one ad of one break."""
    return {"pk": f"RES#{decision}", "sk": crid}


def impression_key(decision: str, crid: str) -> Key:
    return {"pk": f"IMP#{decision}", "sk": crid}


def fill_key(decision: str) -> Key:
    return {"pk": f"FILL#{decision}", "sk": "-"}


def chain_id(line: str) -> str:
    """The ledger chain of a line's money events."""
    return f"pds/line/{line}"


def home_shard(decision: str) -> int:
    """The budget shard a break reserves from first."""
    return bucket(SHARD_SEED, decision) % BUDGET_SHARDS


def shard_caps(budget: Decimal) -> tuple[Decimal, ...]:
    """``budget`` split over :data:`BUDGET_SHARDS` shards (the last takes the rounding), summing to it exactly."""
    share = (budget / BUDGET_SHARDS).quantize(MONEY_PLACES, rounding=ROUND_DOWN)
    return (*(share,) * (BUDGET_SHARDS - 1), budget - share * (BUDGET_SHARDS - 1))


def money(amount: Decimal) -> str:
    """A ledger amount: a decimal string (the ledger holds no floats)."""
    return str(amount.quantize(MONEY_PLACES))


class Ignored(StrEnum):
    """Why a ``BEACON_FIRED`` event is not one of our impressions."""

    NOT_BEACON = "not_beacon"
    """Another event type, or no beacon URI."""
    OTHER_TRACKER = "other_tracker"
    """Not our tracker: another host or path (a third party's), or the CDN's beacon without a decision (the kit's
    own VAST pod, a billing notice)."""
    MALFORMED = "malformed"
    """Our tracker, with parameters that do not parse."""
    OTHER_EVENT = "other_event"
    """Our tracker, for an event other than the impression."""
    SESSION_MISMATCH = "session_mismatch"
    """The tracker names another session than the one that fired it."""


class Beacon(BaseModel):
    """One firing of our impression tracker."""

    model_config = ConfigDict(frozen=True)

    sid: str = Field(pattern=SID_PATTERN)
    evid: str = Field(pattern=EVID_PATTERN)
    crid: str = Field(pattern=NAME_PATTERN)
    enr: int = Field(ge=0, le=1)
    at: datetime
    configuration: str = Field(default="", description="The playback configuration (``originId``).")
    status: int | None = Field(default=None, description="The tracker's HTTP status as MediaTailor got it.")

    @property
    def decision_id(self) -> str:
        return decision_id(self.sid, self.evid)


_DECISION = re.compile(r"^(?P<sid>[A-Za-z0-9-]{1,64})\.(?P<evid>[0-9]{1,10})$")


def _one(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values is not None and len(values) == 1 else None


def _tracker(uri: str, host: str) -> dict[str, str] | Ignored:
    """The parameters of our impression tracker in ``uri``, or why it is not one.

    The CDN's beacon serves other trackers too (the kit's VAST pod, ``?evt=…&cr=…``; the simulator's billing notice,
    ``?seat=…``): only one with a decision (``d``) is ours.
    """
    parts = urlsplit(uri)
    if parts.scheme != "https" or parts.hostname != host.lower() or parts.path != TRACKER_PATH:
        return Ignored.OTHER_TRACKER
    query = parse_qs(parts.query, keep_blank_values=True)
    if "d" not in query:
        return Ignored.OTHER_TRACKER
    d, crid, kind, enr = (_one(query, name) for name in ("d", "c", "e", "enr"))
    match = _DECISION.match(d or "")
    if match is None or crid is None or kind is None or enr not in {"0", "1"}:
        return Ignored.MALFORMED
    if kind != TRACKER_EVENT:
        return Ignored.OTHER_EVENT
    return {"sid": match["sid"], "evid": match["evid"], "crid": crid, "enr": enr or "0"}


def beacon(event: Mapping[str, Any], *, host: str) -> Beacon | Ignored:
    """Our impression in one ADS log event, or why it is not one.

    Our tracker is ``https://<host>/ads/b.gif?d=<sid>.<evid>&c=<crid>&e=impression&enr=<0|1>``, read from
    ``beaconInfo.beaconUri`` (MediaTailor's copy of the URL it fired), never from the CDN's logs.
    """
    info = event.get("beaconInfo")
    if event.get("eventType") != "BEACON_FIRED" or not isinstance(info, Mapping):
        return Ignored.NOT_BEACON
    uri = info.get("beaconUri")
    found = _tracker(uri, host) if isinstance(uri, str) else Ignored.NOT_BEACON
    if isinstance(found, Ignored):
        return found
    if event.get("sessionId") != found["sid"]:
        return Ignored.SESSION_MISMATCH
    status = info.get("beaconHttpResponseCode")
    try:
        return Beacon(
            **found,
            at=datetime.fromisoformat(str(event.get("eventTimestamp"))),
            configuration=str(event.get("originId") or ""),
            status=status if isinstance(status, int) and not isinstance(status, bool) else None,
        )
    except ValueError:  # pydantic's ValidationError is a ValueError; so is a bad timestamp
        return Ignored.MALFORMED
