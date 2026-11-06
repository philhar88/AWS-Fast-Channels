"""The ``OPTIMIZATION(aws|oss): <id>`` markers in the code and the rows of docs/workarounds.md name the same ids: a
workaround with no row could outlive the AWS fix, and a row with no marker points at nothing to delete."""

import re
import subprocess
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[2]
DOC: Final = REPO / "docs" / "workarounds.md"
ID: Final = r"W-2 (?:\d+\.\d+|FR-\d+)|W-\d+|FINDINGS [A-Za-z0-9-]+"
MARKER: Final = re.compile(rf"OPTIMIZATION\((?:aws|oss)\): (?P<id>{ID})(?=[ :,.)]|$)")
ROW_ID: Final = re.compile(rf"`(?P<id>{ID})`")


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
