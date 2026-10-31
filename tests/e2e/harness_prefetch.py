"""Readers for the prefetch e2e (``test_16_prefetch.py``): the siblings' vended ADS log events (``MAKING_ADS_REQUEST``,
``FILLED_AVAIL``) into per-break request arrival spreads and first-sight skips.

Pure functions over the decoded log events (unit-tested in ``test_harness_prefetch.py``). A prefetch retrieval and
the avail it fills name their schedule (``prefetchScheduleName``, ``pf-<event id>``); a real-time request carries
the break's event id in its URL (``evid=``). ``FILLED_AVAIL.adsRequestUrl`` is stale on a prefetched avail (P0-11), so
a prefetched avail's break is read from its schedule's name.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from statistics import quantiles
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

FIRST_SIGHT: Final = frozenset({"NEW_CREATIVE", "IMPORT_IN_PROGRESS", "TRANSCODE_IN_PROGRESS"})
"""``skippedReason`` values of a creative MediaTailor had not prepared yet: the first-sight loss prefetch removes."""
_TIMESTAMP: Final = re.compile(r"(?P<seconds>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(?P<fraction>\d+))?Z")


def _event_id(event: Mapping[str, Any]) -> tuple[int | None, bool]:
    """The break's event id and whether the event is a prefetch's."""
    schedule = event.get("prefetchScheduleName") or ""
    if schedule.startswith("pf-") and schedule[3:].isdigit():
        return int(schedule[3:]), True
    values = parse_qs(urlsplit(event.get("adsRequestUrl") or "").query).get("evid") or [""]
    return (int(values[0]) if values[0].isdigit() else None), bool(schedule)


@dataclass(frozen=True, slots=True)
class Request:
    """One ``MAKING_ADS_REQUEST``."""

    configuration: str
    session: str
    event_id: int | None
    prefetched: bool
    at: datetime


@dataclass(frozen=True, slots=True)
class Avail:
    """One ``FILLED_AVAIL``."""

    configuration: str
    session: str
    event_id: int | None
    prefetched: bool
    filled_s: float
    first_sight: tuple[str, ...]
    """The skipped creatives' reasons that are first-sight ones."""


def requests(events: Iterable[Mapping[str, Any]]) -> list[Request]:
    """The ``MAKING_ADS_REQUEST`` events."""
    found = []
    for event in events:
        if event.get("eventType") != "MAKING_ADS_REQUEST":
            continue
        event_id, prefetched = _event_id(event)
        at = timestamp(str(event["eventTimestamp"]))
        found.append(Request(event["originId"], event.get("sessionId", ""), event_id, prefetched, at))
    return found


def timestamp(text: str) -> datetime:
    """A vended log ``eventTimestamp`` (UTC, nanoseconds: ``2026-10-27T21:10:11.717529762Z``) to microseconds."""
    match = _TIMESTAMP.fullmatch(text)
    if match is None:
        raise ValueError(f"not an event timestamp: {text!r}")
    fraction = (match["fraction"] or "").ljust(6, "0")[:6]
    return datetime.fromisoformat(f"{match['seconds']}.{fraction}+00:00")


def avails(events: Iterable[Mapping[str, Any]]) -> list[Avail]:
    """The ``FILLED_AVAIL`` events."""
    found = []
    for event in events:
        if event.get("eventType") != "FILLED_AVAIL":
            continue
        event_id, prefetched = _event_id(event)
        avail = event.get("avail") or {}
        reasons = tuple(
            str(skipped.get("skippedReason"))
            for skipped in avail.get("skippedAds") or []
            if skipped.get("skippedReason") in FIRST_SIGHT
        )
        found.append(
            Avail(
                event["originId"],
                event.get("sessionId", ""),
                event_id,
                prefetched,
                float(avail.get("filledDuration") or 0),
                reasons,
            )
        )
    return found


@dataclass(frozen=True, slots=True)
class Spread:
    """When a break's ADS requests arrived, in seconds relative to the break's start (negative: before it)."""

    count: int
    first: float
    p05: float
    p50: float
    p95: float
    last: float

    @property
    def width(self) -> float:
        """``p95 - p05``: how spread out the herd is."""
        return self.p95 - self.p05


def spread(at: Iterable[datetime], *, start: datetime) -> Spread | None:
    """The arrival spread of requests ``at`` for a break that starts at ``start``; None without requests."""
    offsets = sorted((moment - start).total_seconds() for moment in at)
    if not offsets:
        return None
    if len(offsets) == 1:
        only = offsets[0]
        return Spread(1, only, only, only, only, only)
    cuts = quantiles(offsets, n=20, method="inclusive")
    return Spread(len(offsets), offsets[0], cuts[0], cuts[9], cuts[18], offsets[-1])
