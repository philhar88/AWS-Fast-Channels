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

A firing is read from two sources (:class:`Source`), deduplicated by those keys: MediaTailor's ADS log
(``BEACON_FIRED``, :func:`beacon`), and the CDN's logs of the tracker's requests (:func:`cdn_log_query`,
:func:`cdn_beacon`), because the ADS log can leave out a whole break that MediaTailor filled (W-2 1.32).
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any, Final
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.pbs_config import TRACKER_PATH, TRACKER_PREFIX
from fast_channels.core.pds.buckets import bucket
from fast_channels.core.pds.models import EVID_PATTERN, NAME_PATTERN, SID_PATTERN, decision_id
from fast_channels.core.pds.pod import TRACKER_EVENT, Outcome

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
    """Not our tracker: another host or path (a third party's), or the CDN's beacon object without a decision (the
    kit's own VAST pod, a billing notice)."""
    MALFORMED = "malformed"
    """Our tracker, with parameters that do not parse."""
    OTHER_EVENT = "other_event"
    """Our tracker, for an event other than the impression."""
    SESSION_MISMATCH = "session_mismatch"
    """The tracker names another session than the one that fired it."""


class Source(StrEnum):
    """Where a firing of our tracker was read."""

    ADS_LOG = "ads_log"
    """MediaTailor's ADS log: a ``BEACON_FIRED`` event, by subscription, within seconds."""
    CDN_LOG = "cdn_log"
    """The CDN's log of the tracker's request, every :data:`CDN_LOG_EVERY`, :data:`CDN_LOG_LAG` after its file
    arrived. An impression it counts is one the ADS log left out (W-2 1.32)."""


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
    source: Source = Field(default=Source.ADS_LOG, description="Where the firing was read.")

    @property
    def decision_id(self) -> str:
        return decision_id(self.sid, self.evid)


_DECISION = re.compile(r"^(?P<sid>[A-Za-z0-9-]{1,64})\.(?P<evid>[0-9]{1,10})$")
_PATH = re.compile(rf"^{re.escape(TRACKER_PREFIX)}(?P<d>[^/]+)/(?P<c>[^/]+)/(?P<enr>[^/]+)\.gif$")
"""Our tracker's path: ``/ads/b/<sid>.<evid>/<crid>/<enr>.gif`` (``pbs_config.TRACKER_PREFIX``)."""


def _one(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values is not None and len(values) == 1 else None


def _tracker(uri: str, host: str) -> dict[str, str] | Ignored:
    """The parameters of our impression tracker in ``uri``, or why it is not one.

    Our tracker is ``/ads/b/<sid>.<evid>/<crid>/<enr>.gif`` (an impression), each segment percent-encoded. Its former
    form, the CDN's beacon object with ``?d=<sid>.<evid>&c=<crid>&e=impression&enr=<0|1>``, is read too: a pod
    rendered before the change can still fire it. The beacon object serves other trackers as well (the kit's VAST
    pod, ``?evt=…&cr=…``; the simulator's billing notice, ``?seat=…``): only one with a decision (``d``) is ours.
    """
    parts = urlsplit(uri)
    if parts.scheme != "https" or parts.hostname != host.lower():
        return Ignored.OTHER_TRACKER
    values: tuple[str | None, ...]
    if (path := _PATH.match(parts.path)) is not None:
        values = (unquote(path["d"]), unquote(path["c"]), TRACKER_EVENT, path["enr"])
    elif parts.path != TRACKER_PATH or "d" not in (query := parse_qs(parts.query, keep_blank_values=True)):
        return Ignored.OTHER_TRACKER
    else:
        values = tuple(_one(query, name) for name in ("d", "c", "e", "enr"))
    d, crid, kind, enr = values
    match = _DECISION.match(d or "")
    if match is None or crid is None or kind is None or enr not in {"0", "1"}:
        return Ignored.MALFORMED
    if kind != TRACKER_EVENT:
        return Ignored.OTHER_EVENT
    return {"sid": match["sid"], "evid": match["evid"], "crid": crid, "enr": enr}


def beacon(event: Mapping[str, Any], *, host: str) -> Beacon | Ignored:
    """Our impression in one ADS log event, or why it is not one.

    Our tracker is ``https://<host>/ads/b/<sid>.<evid>/<crid>/<enr>.gif`` (or its former query form, :func:`_tracker`),
    read from ``beaconInfo.beaconUri`` (MediaTailor's copy of the URL it fired); :func:`cdn_beacon` reads the CDN's.
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


# OPTIMIZATION(aws): W-2 1.32 the ADS log can leave out a filled break: delete the CDN-log source when fixed
CDN_LOG_EVERY: Final = timedelta(minutes=5)
"""How often the delivery function reads the CDN's logs: each run reads the log files that arrived in one window."""
CDN_LOG_LAG: Final = timedelta(minutes=10)
"""How long after a log file arrived it is read. By then the ADS log has delivered its firings (its subscription takes
seconds), so an impression that only the CDN's log counts is one the ADS log left out."""
CDN_LOG_LOOKBACK: Final = timedelta(hours=2)
"""How far back from its window a run lists the log hours. CloudFront delivers a file about every 5 minutes with the
requests of the 5 before (seen live), so this only bounds a late file: one that arrives more than this after its
requests is not read. Listing costs nothing: Athena reads only the files of the window (``$file_modified_time``)."""
SELECT_LOOKBACK: Final = timedelta(minutes=90)
"""How much earlier than its oldest log hour a run reads the select records: a tracker fires after its break's
``/select``, prefetched minutes before the break, and MediaTailor retries a beacon for up to an hour. These JSON
records are what a run scans: about this plus :data:`CDN_LOG_LOOKBACK` of them."""
PLACED: Final = (Outcome.WON, Outcome.STANDALONE)
"""The outcomes of the bids that get our tracker (``core.pds.pod.select_response``)."""


def cdn_log_window(scheduled: datetime) -> tuple[datetime, datetime]:
    """The arrival window ``[start, end)`` of the CDN's log files that the run scheduled at ``scheduled`` reads.

    Runs every :data:`CDN_LOG_EVERY` read consecutive windows, so every file is read once; a run that is retried, or
    redriven from the dead-letter queue, reads its own window again, which counts nothing twice.
    """
    end = scheduled.astimezone(UTC) - CDN_LOG_LAG
    return end - CDN_LOG_EVERY, end


def _hours(start: datetime, end: datetime) -> list[datetime]:
    """The UTC hours from ``start``'s to ``end``'s, both included."""
    hour = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    hours = []
    while hour <= end:
        hours.append(hour)
        hour += timedelta(hours=1)
    return hours


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds")


def cdn_log_query(start: datetime, end: datetime, *, cdn_table: str, select_table: str) -> str:
    """The Athena query of our tracker's requests in the CDN's log files that arrived in ``[start, end)``.

    The tracker is a public URL, so a request counts only when the break's ``pds.select`` record placed the ad of that
    creative (a won or standalone bid, which got our tracker): a forged request names a real ad at most, which counts
    once. Each row has the request's ``timestamp_ms``, ``cs_uri_stem``, ``cs_uri_query`` and ``sc_status``, and the
    break's configuration ``pc``.

    The parameters come from the path (``/ads/b/<sid>.<evid>/<crid>/<enr>.gif``), or from the former form's query,
    which the logs leave out with the identity module (that form then counts from the ADS log only). They are matched
    with regular expressions and compared as logged, not decoded: ``url_decode`` would fail the whole query on a
    malformed request, and only crids of ``NAME_PATTERN`` can count (:class:`Beacon`), which percent-encoding leaves as
    they are. :func:`cdn_beacon` checks them as :func:`beacon` does.

    Args:
        start, end: the window (:func:`cdn_log_window`), on the files' S3 modification time.
        cdn_table: ``<database>.<table>`` of the CDN's logs (Parquet, partitioned by ``year``, ``month``, ``day``,
            ``hour``, with the query string).
        select_table: ``<database>.<table>`` of the ``pds.select`` records (partitioned by ``datehour``).
    """
    oldest = end - CDN_LOG_LOOKBACK
    logs = " OR ".join(
        f"(year = '{h:%Y}' AND month = '{h:%m}' AND day = '{h:%d}' AND hour = '{h:%H}')" for h in _hours(oldest, end)
    )
    selects = ", ".join(f"'{h:%Y/%m/%d/%H}'" for h in _hours(oldest - SELECT_LOOKBACK, end))
    placed = ", ".join(f"'{outcome.value}'" for outcome in PLACED)
    path = f"^{TRACKER_PREFIX}([^/]+)/([^/]+)/[^/]+\\.gif$"
    return f"""WITH tracker AS (
  SELECT timestamp_ms, cs_uri_stem, cs_uri_query, sc_status,
    coalesce(regexp_extract(cs_uri_stem, '{path}', 1), regexp_extract(cs_uri_query, '(?:^|&)d=([^&]*)', 1)) AS d,
    coalesce(regexp_extract(cs_uri_stem, '{path}', 2), regexp_extract(cs_uri_query, '(?:^|&)c=([^&]*)', 1)) AS c
  FROM {cdn_table}
  WHERE ({logs})
    AND (cs_uri_stem LIKE '{TRACKER_PREFIX}%' OR cs_uri_stem = '{TRACKER_PATH}')
    AND "$file_modified_time" >= from_iso8601_timestamp('{_iso(start)}')
    AND "$file_modified_time" < from_iso8601_timestamp('{_iso(end)}')
), placed AS (
  SELECT DISTINCT decision_id, pc, json_extract_scalar(bid, '$.crid') AS crid
  FROM {select_table} CROSS JOIN UNNEST(bids) AS b (bid)
  WHERE datehour IN ({selects}) AND record = 'pds.select' AND NOT probe
    AND json_extract_scalar(bid, '$.outcome') IN ({placed})
)
SELECT t.timestamp_ms, t.cs_uri_stem, t.cs_uri_query, t.sc_status, p.pc
FROM tracker t JOIN placed p ON p.decision_id = t.d AND p.crid = t.c
ORDER BY t.timestamp_ms"""  # noqa: S608 - datetimes, constants and the stage's own table names only


def cdn_beacon(row: Mapping[str, str | None], *, host: str) -> Beacon | Ignored:
    """Our impression in one row of :func:`cdn_log_query`, or why it is not one.

    The parameters are read from the request's path (or the former form's query) as :func:`beacon` reads them from
    MediaTailor's copy of the URL; the time is CloudFront's (``timestamp_ms``), the status the one CloudFront answered,
    and the configuration the break's select record's.
    """
    stem = row.get("cs_uri_stem") or TRACKER_PATH
    query = "" if stem.startswith(TRACKER_PREFIX) else f"?{row.get('cs_uri_query') or ''}"
    found = _tracker(f"https://{host}{stem}{query}", host)
    if isinstance(found, Ignored):
        return found
    status = row.get("sc_status") or ""
    try:
        return Beacon(
            **found,
            at=datetime.fromtimestamp(0, UTC) + timedelta(milliseconds=int(row.get("timestamp_ms") or "")),
            configuration=row.get("pc") or "",
            status=int(status) if status.isdigit() else None,
            source=Source.CDN_LOG,
        )
    except ValueError:  # an unreadable time; pydantic's ValidationError is a ValueError too
        return Ignored.MALFORMED
