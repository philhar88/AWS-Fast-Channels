"""The public docs name only the stages that ship with the repository (``stages/*.json``), never a throwaway test
stage: a measurement says "a test stage", not its prefix. They also carry no notes on what was or was not reported
to AWS or upstream: docs/limitations.md records the behaviour, and reporting is kept out of the repository."""

import re
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[2]
STAGE_NAME: Final = re.compile(r"\bfastc[a-z0-9]*\b")
"""A ``fastci``-style prefix (the shipped CI stages and every throwaway one are named so)."""
EXAMPLES: Final = frozenset({"fastcipr7"})
"""Prefixes the docs use as examples (docs/operations.md: the same stage under another prefix)."""
REPORTING_NOTE: Final = re.compile(r"reported (?:to AWS|upstream)", re.IGNORECASE)


def pages() -> list[Path]:
    return [REPO / "README.md", REPO / "CHANGELOG.md", *sorted((REPO / "docs").rglob("*.md"))]


def test_the_docs_name_only_shipped_stages() -> None:
    shipped = {path.stem for path in (REPO / "stages").glob("*.json")}
    unknown = {
        f"{page.relative_to(REPO)}: {name}"
        for page in pages()
        for name in STAGE_NAME.findall(page.read_text())
        if name not in shipped | EXAMPLES
    }
    assert shipped, "no stage files found"
    assert sorted(unknown) == [], "internal stage names in the public docs: say 'a test stage' instead"


def test_the_docs_carry_no_reporting_notes() -> None:
    notes = [
        f"{page.relative_to(REPO)}:{number}"
        for page in pages()
        for number, line in enumerate(page.read_text().splitlines(), 1)
        if REPORTING_NOTE.search(line)
    ]
    assert notes == [], "notes on reporting to AWS or upstream do not belong in the public docs"
