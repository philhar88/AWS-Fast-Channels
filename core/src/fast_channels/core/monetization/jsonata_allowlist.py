"""The JSONata functions MediaTailor Functions allow, as documented on :data:`ALLOWLIST_DATE`.

The User Guide says any other function "is blocked and causes a validation error when you create or update a
function". In fact ``PutFunction`` accepted ``$pad`` and the hook failed at run time with ``RESTRICTION_ERROR``
(P0-20 §4.5), so validator R-F2 checks every expression against this list before anything is put. Re-read the page
and update the date when AWS changes it.
"""

from typing import Final

ALLOWLIST_DATE: Final = "2026-10-27"
"""When the list below was transcribed from :data:`ALLOWLIST_SOURCE` (44 functions)."""
ALLOWLIST_SOURCE: Final = "https://docs.aws.amazon.com/mediatailor/latest/ug/monetization-functions-jsonata.html"

ALLOWED_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        # Type conversion (3)
        "string",
        "number",
        "boolean",
        # Introspection (6)
        "length",
        "count",
        "exists",
        "keys",
        "lookup",
        "each",
        # Numeric (7)
        "sum",
        "max",
        "min",
        "average",
        "abs",
        "floor",
        "round",
        # String (9)
        "uppercase",
        "lowercase",
        "trim",
        "substring",
        "contains",
        "match",
        "replace",
        "join",
        "split",
        # Array (7)
        "append",
        "reverse",
        "sort",
        "distinct",
        "map",
        "filter",
        "reduce",
        # Boolean (1)
        "not",
        # Random (1)
        "random",
        # Date/time (4)
        "now",
        "millis",
        "toMillis",
        "fromMillis",
        # Encoding (6)
        "encodeUrl",
        "encodeUrlComponent",
        "decodeUrl",
        "decodeUrlComponent",
        "base64encode",
        "base64decode",
    }
)
"""Function names without the ``$``."""
