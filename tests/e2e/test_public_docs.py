"""The public docs name only the stages that ship with the repository (``stages/*.json``), never a throwaway test
stage: a measurement says "a test stage", not its prefix."""

import re
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[2]
STAGE_NAME: Final = re.compile(r"\bfastc[a-z0-9]*\b")
"""A ``fastci``-style prefix (the shipped CI stages and every throwaway one are named so)."""
EXAMPLES: Final = frozenset({"fastcipr7"})
"""Prefixes the docs use as examples (docs/operations.md: the same stage under another prefix)."""


def test_the_docs_name_only_shipped_stages() -> None:
    shipped = {path.stem for path in (REPO / "stages").glob("*.json")}
    pages = [REPO / "README.md", REPO / "CHANGELOG.md", *sorted((REPO / "docs").rglob("*.md"))]
    unknown = {
        f"{page.relative_to(REPO)}: {name}"
        for page in pages
        for name in STAGE_NAME.findall(page.read_text())
        if name not in shipped | EXAMPLES
    }
    assert shipped, "no stage files found"
    assert sorted(unknown) == [], "internal stage names in the public docs: say 'a test stage' instead"
