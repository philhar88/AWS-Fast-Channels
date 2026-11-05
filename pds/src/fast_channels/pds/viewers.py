"""The viewer's ad-load tier, from ``/policy`` to ``/select`` (ADR-0037).

``/select`` carries no ticket: only ``/policy`` resolves the binding, and with it the viewer id. Both hooks of one
auction reach the same task's sidecar (ADR-0036), so:

1. ``/policy``, for a FULL or PSEUDONYMOUS break with a bound viewer, notes the viewer id under the break's
   ``decision_id`` (:class:`Handoff`) and starts reading the viewer's tier (:class:`Tiers`) on a thread of its own,
   off the request path, while the auction runs;
2. ``/select`` takes the viewer id back by ``decision_id`` and waits at most :data:`WAIT_S` for the tier.

A tier is kept :data:`TIER_TTL_S` per viewer (a profile changes at most daily), so only a viewer's first break in that
time reads it. A read that fails or is not done in time gives no tier: the break gets the control plan, and
``TierReadErrors`` or ``TierReadsLate`` counts it. Nothing here outlives the process.
"""

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Final

from aws_lambda_powertools import Logger

from fast_channels.core.adload import PERSONALIZED
from fast_channels.core.pds.viewers import VID_PATTERN
from fast_channels.functions.pds.profiles import read_tier
from fast_channels.functions.settings import settings
from fast_channels.pds import state
from fast_channels.pds.telemetry import counters

if TYPE_CHECKING:
    from fast_channels.core.pds.models import Mode
    from fast_channels.core.pds.policy import PolicyDecision

HANDOFF_TTL_S: Final = 120.0
"""How long a break's viewer id is kept for its ``/select``: an auction takes well under a second."""
HANDOFF_MAX: Final = 20_000
"""The most breaks kept: the oldest go first."""
TIER_TTL_S: Final = 300.0
"""How long a viewer's tier is kept."""
TIERS_MAX: Final = 50_000
"""The most viewers' tiers kept: the oldest go first."""
WAIT_S: Final = 0.02
"""The longest ``/select`` waits for a tier still being read."""
READERS: Final = 4
"""Threads that read tiers."""

logger = Logger(child=True)
_VID: Final = re.compile(VID_PATTERN)


class _Expiring[V]:
    """A bounded map whose entries expire, safe across threads."""

    def __init__(self, *, ttl_s: float, max_entries: int, clock: Callable[[], float]) -> None:
        self._ttl_s, self._max, self._clock = ttl_s, max_entries, clock
        self._items: OrderedDict[str, tuple[float, V]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> V | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if self._clock() - entry[0] > self._ttl_s:
                del self._items[key]
                return None
            return entry[1]

    def put(self, key: str, value: V) -> None:
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = (self._clock(), value)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def discard(self, key: str, value: V) -> None:
        """Remove ``key`` if it still holds ``value``."""
        with self._lock:
            entry = self._items.get(key)
            if entry is not None and entry[1] is value:
                del self._items[key]


class Handoff:
    """The bound viewer of each break, from its ``/policy`` to its ``/select``."""

    def __init__(
        self,
        *,
        ttl_s: float = HANDOFF_TTL_S,
        max_entries: int = HANDOFF_MAX,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._viewers = _Expiring[str](ttl_s=ttl_s, max_entries=max_entries, clock=clock)

    def put(self, decision_id: str, vid: str) -> None:
        self._viewers.put(decision_id, vid)

    def get(self, decision_id: str) -> str | None:
        return self._viewers.get(decision_id)


class Tiers:
    """Viewers' tiers, each read once per :data:`TIER_TTL_S` on a reader thread."""

    def __init__(
        self,
        read: Callable[[str], str | None],
        *,
        ttl_s: float = TIER_TTL_S,
        max_entries: int = TIERS_MAX,
        workers: int = READERS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._futures = _Expiring[Future[str | None]](ttl_s=ttl_s, max_entries=max_entries, clock=clock)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tiers")
        self._lock = threading.Lock()

    def prefetch(self, vid: str) -> Future[str | None]:
        """Start reading ``vid``'s tier unless it is known or being read; returns the read."""
        with self._lock:
            future = self._futures.get(vid)
            if future is None:
                future = self._pool.submit(self._read, vid)
                self._futures.put(vid, future)
        return future

    def get(self, vid: str, *, wait_s: float = WAIT_S) -> str | None:
        """``vid``'s tier, waiting at most ``wait_s`` for a read in progress; ``None`` on a failed or late read
        (a failed read is tried again on the next break)."""
        future = self.prefetch(vid)
        try:
            return future.result(timeout=wait_s)
        except FutureTimeout:
            counters.add("TierReadsLate")
            return None
        except Exception as error:
            counters.add("TierReadErrors")
            logger.warning("tier read failed", extra={"error": type(error).__name__, "detail": str(error)[:300]})
            self._futures.discard(vid, future)
            return None


@dataclass(frozen=True, slots=True)
class Viewers:
    """The hand-off and the tiers, as the endpoints use them."""

    handoff: Handoff
    tiers: Tiers

    def note(self, decision_id: str, decision: PolicyDecision) -> None:
        """After ``/policy``: keep a FULL or PSEUDONYMOUS break's bound viewer and start reading their tier."""
        vid = decision.viewer.vid
        if decision.response.mode in PERSONALIZED and vid is not None and _VID.fullmatch(vid):
            self.handoff.put(decision_id, vid)
            self.tiers.prefetch(vid)

    def tier(self, decision_id: str, mode: Mode | None, *, probe: bool) -> str | None:
        """For ``/select``: the tier of the break's viewer, if its mode follows tiers and ``/policy`` bound one."""
        if probe or mode not in PERSONALIZED or (vid := self.handoff.get(decision_id)) is None:
            return None
        return self.tiers.get(vid)


def _read(vid: str) -> str | None:
    settings().require("pds_table")
    return read_tier(state.dynamodb(), settings().pds_table, vid)


@cache
def viewers() -> Viewers:
    """The sidecar's hand-off and tiers, reading on the reservations' client."""
    return Viewers(handoff=Handoff(), tiers=Tiers(_read))
