"""The smoke test's verdict (DESIGN §8.3 step 3): what a probe session against the candidate sibling must show before a
release is exposed, judged break by break from its MediaTailor log events and the Pod Decision Service's records.

For each break the session saw (its ``EXT-X-DATERANGE`` id; breaks it joined during are left out by the caller):

- its ad marker led to an ad request (``MAKING_ADS_REQUEST``): an invalid rendered body or an exhausted PRE_ADS
  budget makes none and logs nothing else (P0-20 §4.14, P0-03);
- the break was filled (``FILLED_AVAIL``);
- the Pod Decision Service recorded a decision for it (the session's ``sid`` and the break's ``evid``);

and no Function failed (``*_FUNCTION_ERROR`` and ``*_HOOK_ERROR`` events, which MediaTailor logs by default, or a hook
summary other than ``SUCCESS`` where a sibling publishes them).

A break is judged on the events of its marker's first appearance. MediaTailor finds a Channel Assembly marker again
when it gains its ``DURATION`` at the break's end (a new ``adBreakCorrelationId``, no request); that event is not a
break of its own. MediaTailor can also leave a break out of its vended logs altogether while it requested and filled
it (fastm, 2026-10-29: the ad request reached the Pod Decision Service and ``Avail.FilledDuration`` counted the
break). Such an *unlogged* break passes when the Pod Decision Service recorded its request, and is counted in
:attr:`SmokeVerdict.unlogged`; at least one break must be logged.

The verdict names what failed; it never holds a log value.
"""

import re
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.monetization.functions import Hook

if TYPE_CHECKING:
    from fast_channels.core.monetization.bundle import Bundle

MARKER = "AD_MARKER_FOUND"
REQUEST = "MAKING_ADS_REQUEST"
FILLED = "FILLED_AVAIL"
TIMEOUT_PERIODS: Final = 2
"""A request that reaches its timeout in this many one-minute periods fails a release: a dependency down or slow,
not one cold call (fastm, 2026-10-30: the crawl request reached its 250 ms once, on a healthy smoke's first
break)."""
NO_ADS = "WARNING_NO_ADVERTISEMENTS"
ERROR_SUFFIXES = ("_FUNCTION_ERROR", "_HOOK_ERROR")
HOOK_SUMMARY_SUFFIX = "_HOOK_SUMMARY"
CHANNEL_ASSEMBLY_ID: Final = re.compile(r"^\d+-\d+-(?P<event>[0-9A-Fa-f]{1,8})$")
"""Channel Assembly's ``EXT-X-DATERANGE`` id: ``<epoch ms>-<n>-<splice event id in hex>``."""


class SmokeVerdict(BaseModel):
    """The smoke test of one sibling."""

    model_config = ConfigDict(frozen=True)

    sibling: str
    session: str
    breaks: int = Field(ge=0, description="Distinct breaks the session saw.")
    requests: int = Field(ge=0, description="Breaks with an ad request (logged, or recorded by the PDS).")
    filled: int = Field(ge=0, description="Logged breaks that were filled.")
    function_errors: int = Field(ge=0)
    decisions: int = Field(ge=0, description="Breaks the Pod Decision Service recorded a decision for.")
    unlogged: int = Field(default=0, ge=0, description="Breaks MediaTailor left out of its logs.")
    failures: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures


def splice_event(marker: str) -> str | None:
    """The SCTE-35 event id (``evid``, decimal) in a Channel Assembly marker id, or None for another origin's."""
    match = CHANNEL_ASSEMBLY_ID.match(marker)
    return str(int(match["event"], 16)) if match else None


class _Scan:
    """A session's events by break: each marker's first-appearance break ids, the event types of each break id, the
    logged markers in order, and the Function errors."""

    def __init__(self, events: Iterable[Mapping[str, str]]) -> None:
        self.first: dict[str, set[str]] = defaultdict(set)
        self.kinds: dict[str, set[str]] = defaultdict(set)
        self.markers: dict[str, None] = {}
        self.errors = 0
        for event in events:
            kind, break_id = event.get("eventType", ""), event.get("breakId")
            if break_id is not None:
                self.kinds[break_id].add(kind)
            if kind == MARKER:
                marker = event.get("marker") or break_id or ""
                self.markers.setdefault(marker)
                if not event.get("completed") and break_id:
                    self.first[marker].add(break_id)
            failed_hook = kind.endswith(HOOK_SUMMARY_SUFFIX) and event.get("hookStatus", "SUCCESS") != "SUCCESS"
            self.errors += failed_hook or kind.endswith(ERROR_SUFFIXES)

    def of(self, marker: str) -> set[str] | None:
        """The event types of ``marker``'s first appearance; None when it was not logged."""
        ids = self.first.get(marker)
        return set().union(*(self.kinds[b] for b in ids)) if ids else None


def verdict(
    *,
    sibling: str,
    session: str,
    events: Iterable[Mapping[str, str]],
    decisions: Collection[str],
    breaks: int,
    seen: Sequence[str] = (),
    decisions_required: bool = True,
) -> SmokeVerdict:
    """The verdict on a session's log ``events`` (``eventType``; ``breakId``, the ``adBreakCorrelationId``, or a
    prefetched break's ``prefetchScheduleName``, which its retrieval, marker and fill carry instead; for a
    marker its ``EXT-X-DATERANGE`` id ``marker`` and ``completed`` when it carries its duration; ``hookStatus``).

    Args:
        sibling: the candidate sibling.
        session: the probe session's id.
        events: its MediaTailor ADS and manifest log events.
        decisions: the ``evid`` of every Pod Decision Service record of the session.
        breaks: breaks the session must have seen.
        seen: the markers the session's playlists showed (breaks joined during left out).
        decisions_required: whether the sibling's ad requests go through the Pod Decision Service (a probe's do,
            flagged).
    """
    scan = _Scan(events)
    markers = list(dict.fromkeys([*seen, *scan.markers]))
    count: dict[str, int] = defaultdict(int)
    for marker in markers:
        evid = splice_event(marker)
        decided = evid in decisions if evid is not None else len(decisions) >= len(markers)
        kinds = scan.of(marker)
        requested = decided if kinds is None else REQUEST in kinds
        count["decided"] += decided
        count["requested"] += requested
        count["unlogged"] += kinds is None
        count["filled"] += kinds is not None and FILLED in kinds
        count["unfilled"] += kinds is not None and requested and FILLED not in kinds
        count["undecided"] += decisions_required and not decided
    unrequested = len(markers) - count["requested"]
    failures = [
        message
        for failed, message in (
            (len(markers) < breaks, f"saw {len(markers)} of {breaks} breaks"),
            (unrequested > 0, f"{unrequested} breaks made no ad request"),
            (count["unfilled"] > 0, f"{count['unfilled']} breaks were not filled"),
            (bool(markers) and count["unlogged"] == len(markers), "no break was logged"),
            (scan.errors > 0, f"{scan.errors} Function errors"),
            (count["undecided"] > 0, f"{count['undecided']} breaks without a decision record"),
        )
        if failed
    ]
    return SmokeVerdict(
        sibling=sibling,
        session=session,
        breaks=len(markers),
        requests=count["requested"],
        filled=count["filled"],
        function_errors=scan.errors,
        decisions=count["decided"],
        unlogged=count["unlogged"],
        failures=tuple(failures),
    )


def request_timeouts(bundle: Bundle, sibling: str, hook: Hook = Hook.PRE_ADS) -> dict[str, int]:
    """The request timeout (ms) of each ``HTTP_REQUEST`` function in ``sibling``'s ``hook`` tree, by stem."""
    found = next((s for s in bundle.siblings if s.name == sibling), None)
    root = bundle.function((found.playback.function_mapping or {}).get(hook, "")) if found else None
    if root is None:
        return {}
    functions = [bundle.function(ref.function_id) for ref in root.steps] or [root]
    return {
        f.function_id: f.http_request_configuration.request_timeout_milliseconds
        for f in functions
        if f is not None and f.http_request_configuration is not None
    }


def timed_out(
    maxima: Mapping[str, Iterable[float]], timeouts: Mapping[str, int], *, periods: int = TIMEOUT_PERIODS
) -> dict[str, int]:
    """The functions whose latency reached their request timeout in ``periods`` one-minute periods or more, with the
    number of those periods.

    ``maxima`` holds each function's per-minute maximum ``Function.Latency``. A request that times out inside an
    executor is neither a Function error nor a hook error (its response is null): only its latency, at its timeout,
    shows it (fastm, 2026-10-30: 1,007 and 1,008 ms for a 1,000 ms request to a 2,500 ms endpoint).
    """
    counts = {f: sum(1 for m in maxima.get(f, ()) if m >= timeout) for f, timeout in timeouts.items()}
    return {f: n for f, n in counts.items() if n >= periods}
