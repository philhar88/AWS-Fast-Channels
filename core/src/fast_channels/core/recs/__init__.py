"""The rail recommender's deterministic parts (M10a, DESIGN §15, ADR-0027): everything but the model call.

- :mod:`.rail`: guide airings, content ratings, dayparts and the rule-based rail, a port of the viewer's
  ``console/src/viewer/rail.ts`` ``programRail`` (the fallback, kept identical by a shared fixture test).
- :mod:`.cards`: the catalog card of a title, from the CMS item and, when it exists, the content understanding item.
- :mod:`.router`: which rail a request gets, decided before any model call.
- :mod:`.candidates`: the titles a model may choose among, already filtered by policy and scored.
- :mod:`.prompt`: the stable rubric and output schema, the per-day catalog block and the request block.
- :mod:`.validate`: the validator and the deterministic faithfulness check that every model rail passes before it
  is served.

The model registry task is ``recommender`` (:data:`TASK`). The default entry is Claude Haiku 5.5 with thinking
disabled: with adaptive thinking and a JSON output schema, 29% of P0-24's calls spent the whole output budget and
returned no rail.
"""

from typing import Any, Final

TASK: Final = "recommender"
"""The model registry task of every model rail, per viewer and per cohort."""
REGISTRY_DEFAULT: Final[dict[str, Any]] = {
    "model": "{geo}.anthropic.claude-haiku-5-5",
    "effort": "low",
    "thinking": "disabled",
    "max_tokens": 4000,
    "data_class": "viewer",
    "daily_cap_usd": 2,
    "eval": "haiku-5-5-disabled-4k",
}
"""The ``recommender`` registry entry a deployment gets with ``modules.recs`` unless it configures its own. The cap is
about ten times a busy demo day: one rail per signed-in adult viewer-day plus 24 cohort rails per cohort, at about a
tenth of a cent each."""
