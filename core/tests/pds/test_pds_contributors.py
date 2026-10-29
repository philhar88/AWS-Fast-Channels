from dataclasses import dataclass, field
from typing import Any

from fast_channels.core.pds.contributors import Contribution, PatchContributor, collect
from fast_channels.core.pds.models import Mode, PolicyRequest

REQUEST = PolicyRequest(v=1, sid="s", pc="fast-demo-p-a", dur_ms=60060, upid="tears-of-steel")


@dataclass
class _Fixed:
    name: str
    app_content: dict[str, Any] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)
    seen: list[Mode] = field(default_factory=list)

    def contribute(self, request: PolicyRequest, mode: Mode) -> Contribution:
        self.seen.append(mode)
        return Contribution(app_content=self.app_content, record=self.record)


class _Broken:
    name = "broken"

    def contribute(self, request: PolicyRequest, mode: Mode) -> Contribution:
        raise TimeoutError("catalog index not loaded")


def test_contributions_merge_in_order() -> None:
    first = _Fixed("crawl", {"id": "tears-of-steel", "title": "Tears of Steel"}, {"enr": 1})
    second = _Fixed("ratings", {"contentrating": "TV-14"})
    contributors: list[PatchContributor] = [first, second]
    merged = collect(contributors, REQUEST, Mode.CONTEXTUAL)
    assert merged.app_content == {"id": "tears-of-steel", "title": "Tears of Steel", "contentrating": "TV-14"}
    assert merged.record == {"enr": 1}
    assert merged.applied == ("crawl", "ratings") and merged.skipped == ()
    assert first.seen == [Mode.CONTEXTUAL]


def test_failing_and_colliding_contributors_are_skipped() -> None:
    contributors: list[PatchContributor] = [
        _Fixed("crawl", {"id": "a"}),
        _Broken(),
        _Fixed("dup-content", {"id": "b"}),
        _Fixed("dup-record", record={"enr": 0}),
        _Fixed("late", record={"enr": 1}),
    ]
    merged = collect(contributors, REQUEST, Mode.FULL)
    assert merged.app_content == {"id": "a"}
    assert merged.record == {"enr": 0}
    assert merged.applied == ("crawl", "dup-record")
    assert merged.skipped == ("broken", "dup-content", "late")


def test_no_contributors() -> None:
    merged = collect([], REQUEST, Mode.SAFE)
    assert (merged.app_content, merged.record, merged.applied, merged.skipped) == ({}, {}, (), ())
