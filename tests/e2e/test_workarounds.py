"""The ``OPTIMIZATION(aws|oss): <id>`` markers in the code and the rows of docs/workarounds.md name the same ids: a
workaround with no row could outlive the AWS fix, and a row with no marker points at nothing to delete. Every row's
id is also an item of docs/limitations.md, which says what the behaviour means for a deployment."""

import re
import subprocess
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[2]
DOC: Final = REPO / "docs" / "workarounds.md"
LIMITATIONS: Final = REPO / "docs" / "limitations.md"
ID: Final = r"W-2 (?:\d+\.\d+|FR-\d+)|W-\d+|FINDINGS [A-Za-z0-9-]+|EI-VOD"
"""A report item (``W-2 1.31``), a whole report (``W-11``), a build finding, or a missing AWS feature (``EI-VOD``)."""
MARKER: Final = re.compile(rf"OPTIMIZATION\((?:aws|oss)\): (?P<id>{ID})(?=[ :,.)]|$)")
ROW_ID: Final = re.compile(rf"`(?P<id>{ID})`")
ITEM_ID: Final = re.compile(r"`(?P<id>(?:W-\d+|FINDINGS|EI-VOD)(?: [^`]+)?)`")
"""An item id of docs/limitations.md: a report and, for the reports with several items, the item (``W-2 1.31``,
``W-11 item 9``, ``W-8 A10 cancel``)."""


def marked() -> dict[str, list[str]]:
    """Every marker's id, with the files that carry it (tracked code files; Markdown only describes markers)."""
    listed = subprocess.run(
        ["git", "grep", "-n", "OPTIMIZATION("], cwd=REPO, capture_output=True, text=True, check=False
    ).stdout
    found: dict[str, list[str]] = {}
    for line in listed.splitlines():
        path, _, text = line.split(":", 2)
        if path.endswith((".md", "test_workarounds.py")):
            continue
        for match in MARKER.finditer(text):
            found.setdefault(match["id"], []).append(path)
    return found


def documented() -> set[str]:
    """The ids in the first column of the page's tables."""
    ids: set[str] = set()
    for line in DOC.read_text().splitlines():
        if line.startswith("| `"):
            first = line.split("|")[1]
            ids |= {match["id"] for match in ROW_ID.finditer(first)}
    return ids


def limitations() -> list[str]:
    """The ids in the first column of docs/limitations.md's tables, in page order (``—`` rows have none)."""
    ids: list[str] = []
    for line in LIMITATIONS.read_text().splitlines():
        if line.startswith("| `"):
            ids += [match["id"] for match in ITEM_ID.finditer(line.split("|")[1])]
    return ids


def test_every_workaround_has_a_limitation() -> None:
    """A row of workarounds.md names one item (``W-2 1.31``) or a whole report (``W-11``), which then needs at least
    one of its items (``W-11 item 9``) on the limitations page."""
    items = limitations()
    missing = sorted(
        workaround
        for workaround in documented()
        if not any(item == workaround or item.startswith(f"{workaround} ") for item in items)
    )
    assert missing == [], "ids of docs/workarounds.md without a row in docs/limitations.md"


def test_each_limitation_has_one_row() -> None:
    items = limitations()
    assert items, "no rows found in docs/limitations.md"
    assert sorted({item for item in items if items.count(item) > 1}) == []


def test_every_marker_has_a_row_and_every_row_a_marker() -> None:
    in_code = marked()
    assert in_code, "no OPTIMIZATION markers found (git grep)"
    rows = documented()
    assert sorted(set(in_code) - rows) == [], "markers without a row in docs/workarounds.md"
    assert sorted(rows - set(in_code)) == [], "rows of docs/workarounds.md without a marker in the code"


def test_a_marker_names_its_id_first() -> None:
    """A marker reads ``OPTIMIZATION(aws): <id> <why>``: an id the pattern does not read would escape the check."""
    listed = subprocess.run(
        ["git", "grep", "-n", "OPTIMIZATION("], cwd=REPO, capture_output=True, text=True, check=False
    ).stdout
    unreadable = [
        line
        for line in listed.splitlines()
        if not line.split(":", 1)[0].endswith((".md", "test_workarounds.py"))
        and "OPTIMIZATION(" in line
        and not MARKER.search(line)
    ]
    assert unreadable == []
