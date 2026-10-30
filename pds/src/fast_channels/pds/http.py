"""What the sidecar's endpoints share: their answer (status and JSON body), and a refused request's field names."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pydantic import ValidationError


@dataclass(frozen=True, slots=True)
class Answer:
    """An endpoint's HTTP answer: its status and JSON body (empty for a 204)."""

    status: int
    body: str = ""


REFUSED: Final = Answer(400, '{"error":"invalid request"}')
"""A request that is not the contract's: the module then leaves the auction alone (``/policy``) or renders house ads
(``/select``); Prebid Server logs it as the own book's error (``/ownbook/openrtb``)."""
INTERNAL: Final = Answer(500, '{"error":"internal"}')
NO_SNAPSHOT: Final = Answer(503, '{"error":"no snapshot"}')
"""The sidecar has not loaded a snapshot yet (a first deploy before the publisher's first one, or S3 unreachable):
``/policy`` and ``/select`` answer this at once, so the module serves the break SAFE and, with the own book silent
too, renders the kit's house ads from its configuration (``HOUSE_CONFIG``)."""
NO_BID: Final = Answer(204)
SHED: Final = Answer(503, '{"error":"overloaded"}')
"""A request that waited for a worker until its caller stopped waiting (``server.deadlines``): computing it would
only lengthen the queue, so it is answered at once and counted (``Shed``)."""


def refused_fields(error: ValidationError) -> list[str]:
    """The locations of a validation error's problems, never their values (a value may be a ticket)."""
    return sorted({".".join(str(part) for part in problem["loc"]) for problem in error.errors()})
