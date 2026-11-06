"""The viewer's tier from ``/policy`` to ``/select``: the hand-off, the reads off the request path (ADR-0037)."""

import threading
from dataclasses import dataclass, field

import pytest

from fast_channels.core.pds.contributors import Contributions
from fast_channels.core.pds.models import Mode, PolicyResponse, Reason
from fast_channels.core.pds.policy import ANONYMOUS, PolicyDecision, Viewer
from fast_channels.core.pds.xff import IpRule
from fast_channels.pds import viewers
from fast_channels.pds.telemetry import counters
from fast_channels.pds.viewers import Handoff, Tiers, Viewers

DANA = "0123456789abcdef0123456789abcdef"
DECISION = "6f1c2a9e-4b7d-4e2a-9c1e-2d3f4a5b6c7d.820461826"


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Reader:
    """Answers from ``tiers``, counting reads; blocks while ``gate`` is clear, raises for ``broken``."""

    tiers: dict[str, str | None] = field(default_factory=lambda: {DANA: "light"})
    reads: list[str] = field(default_factory=list)
    gate: threading.Event = field(default_factory=threading.Event)
    broken: bool = False

    def __post_init__(self) -> None:
        self.gate.set()

    def __call__(self, vid: str) -> str | None:
        self.reads.append(vid)
        self.gate.wait(5)
        if self.broken:
            raise RuntimeError("throttled")
        return self.tiers.get(vid)


BOUND = Viewer(vid=DANA)


def decision(mode: Mode, viewer: Viewer = BOUND) -> PolicyDecision:
    response = PolicyResponse(decision_id=DECISION, mode=mode, reason=Reason.BOUND)
    return PolicyDecision(response, IpRule.OK, Contributions(), viewer)


def test_the_handoff_keeps_a_breaks_viewer_until_it_expires() -> None:
    clock = Clock()
    handoff = Handoff(ttl_s=120, clock=clock)
    handoff.put(DECISION, DANA)
    clock.now = 119
    assert handoff.get(DECISION) == DANA
    clock.now = 121
    assert handoff.get(DECISION) is None


def test_the_handoff_drops_the_oldest_breaks_first() -> None:
    handoff = Handoff(max_entries=2)
    for n in range(3):
        handoff.put(f"s.{n}", DANA)
    assert [handoff.get(f"s.{n}") for n in range(3)] == [None, DANA, DANA]


def test_a_tier_is_read_once_and_kept() -> None:
    clock, reader = Clock(), Reader()
    tiers = Tiers(reader, ttl_s=300, clock=clock)
    tiers.prefetch(DANA)
    assert tiers.get(DANA, wait_s=1) == "light"
    assert tiers.get(DANA, wait_s=1) == "light"
    assert reader.reads == [DANA]
    clock.now = 301
    assert tiers.get(DANA, wait_s=1) == "light"
    assert reader.reads == [DANA, DANA]


def test_a_late_read_gives_no_tier_and_is_used_once_done() -> None:
    reader = Reader()
    reader.gate.clear()
    tiers = Tiers(reader)
    counters.take()
    assert tiers.get(DANA, wait_s=0.01) is None
    assert any(name == "TierReadsLate" for name, _, _ in counters.take())
    reader.gate.set()
    assert tiers.get(DANA, wait_s=1) == "light"
    assert reader.reads == [DANA]


def test_a_failed_read_gives_no_tier_and_is_tried_again() -> None:
    reader = Reader(broken=True)
    tiers = Tiers(reader)
    counters.take()
    assert tiers.get(DANA, wait_s=1) is None
    assert any(name == "TierReadErrors" for name, _, _ in counters.take())
    reader.broken = False
    assert tiers.get(DANA, wait_s=1) == "light"
    assert reader.reads == [DANA, DANA]


@pytest.mark.parametrize("mode", [Mode.FULL, Mode.PSEUDONYMOUS])
def test_a_personalized_break_hands_its_viewers_tier_to_select(mode: Mode) -> None:
    reader = Reader()
    known = Viewers(Handoff(), Tiers(reader))
    known.note(DECISION, decision(mode))
    assert known.tier(DECISION, mode, probe=False) == "light"
    assert known.tier("other.1", mode, probe=False) is None


@pytest.mark.parametrize(
    "noted",
    [
        decision(Mode.CONTEXTUAL),
        decision(Mode.SAFE),
        decision(Mode.KIDS),
        decision(Mode.FULL, ANONYMOUS),
        decision(Mode.FULL, Viewer(vid="not-a-viewer-id")),
    ],
)
def test_nothing_else_is_handed_over_or_read(noted: PolicyDecision) -> None:
    reader = Reader()
    known = Viewers(Handoff(), Tiers(reader))
    known.note(DECISION, noted)
    assert known.tier(DECISION, Mode.FULL, probe=False) is None
    assert reader.reads == []


@pytest.mark.parametrize(("mode", "probe"), [(Mode.CONTEXTUAL, False), (None, False), (Mode.FULL, True)])
def test_select_asks_for_no_tier_where_none_applies(mode: Mode | None, probe: bool) -> None:
    known = Viewers(Handoff(), Tiers(Reader()))
    known.note(DECISION, decision(Mode.FULL))
    assert known.tier(DECISION, mode, probe=probe) is None


def test_the_sidecars_tiers_read_the_pds_table(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[object, str, str]] = []
    monkeypatch.setenv("PDS_TABLE", "fastm-pds")
    viewers.settings.cache_clear()
    monkeypatch.setattr(viewers.state, "dynamodb", lambda: "client")
    monkeypatch.setattr(viewers, "read_tier", lambda client, table, vid: asked.append((client, table, vid)) or "light")
    try:
        assert viewers._read(DANA) == "light"
    finally:
        viewers.settings.cache_clear()
    assert asked == [("client", "fastm-pds", DANA)]
    viewers.viewers.cache_clear()
    assert viewers.viewers() is viewers.viewers()
