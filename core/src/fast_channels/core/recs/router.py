"""Which rail a request gets, decided before any model call (DESIGN §15, P0-24).

| Who | Rail |
|---|---|
| ``recs.strategy=popularity`` (the kill switch) | the rule-based rail |
| a kids profile | the kids lineup |
| an opted-out viewer, or one without a current consent record | the cohort rail (no viewer data) |
| the rail holdout (``SHA-256("rail-v1" ‖ vid) mod 100 < holdout_percent``) | the rule-based rail |
| an account's first session | the cohort rail, re-ranked by the declared genres |
| everyone else | the viewer's model rail |

A model rail that is missing, being built, failed or invalid is served as the rule-based rail with that reason
(:class:`Reason`), so every answer says why it is what it is.
"""

import hashlib
from enum import StrEnum
from typing import Final, NamedTuple

HOLDOUT_SEED: Final = "rail-v1"
"""Salt of the rail holdout's bucket; a new salt draws a new holdout."""
DEFAULT_HOLDOUT_PERCENT: Final = 10


class Strategy(StrEnum):
    """How a rail was made."""

    LLM = "llm"
    """Picked, ordered and explained by the ``recommender`` task, then validated."""
    COHORT = "cohort"
    """The hourly cohort rail (device x language x daypart): no viewer data."""
    BASELINE = "baseline"
    """The rule-based rail (the viewer's genre baseline)."""
    KIDS = "kids"
    """The kids lineup: titles rated for children, soonest first."""


class Reason(StrEnum):
    """Why a request got its strategy."""

    MODEL = "model"
    """The viewer's model rail."""
    KILL_SWITCH = "kill_switch"
    """``recs.strategy`` is ``popularity``."""
    KIDS = "kids"
    """A kids profile."""
    OPTED_OUT = "opted_out"
    """The viewer opted out of personalization."""
    NO_CONSENT = "no_consent"
    """No current consent record (SAFE): no personalization."""
    HOLDOUT = "holdout"
    """The viewer is in the rail holdout, the baseline the model rail is measured against."""
    FIRST_SESSION = "first_session"
    """An account's first session: no model call is bought by a sign-up."""
    BUILDING = "building"
    """The viewer's model rail is being built; the next request may get it."""
    MODEL_ERROR = "model_error"
    """The model call failed."""
    TIMEOUT = "timeout"
    """The model call timed out."""
    CAP = "cap"
    """The task's daily spend cap is reached."""
    INVALID = "invalid"
    """The model's rail did not pass the validator (too few tiles survived)."""
    EXPIRED = "expired"
    """Too few of the model rail's titles still air; a new one is being built."""
    COHORT = "cohort"
    """A cohort rail (guests read it from the CDN)."""


class Personalization(StrEnum):
    """What the viewer's consent record allows (the identity module's consent store)."""

    ALLOWED = "allowed"
    """FULL or PSEUDONYMOUS: internal personalization."""
    OPTED_OUT = "opted_out"
    UNAVAILABLE = "unavailable"
    """Missing or stale: SAFE."""


class Route(NamedTuple):
    strategy: Strategy
    reason: Reason


def holdout_bucket(vid: str, seed: str = HOLDOUT_SEED) -> int:
    """The viewer's bucket, 0 to 99, of the rail holdout."""
    return int(hashlib.sha256(f"{seed}{vid}".encode()).hexdigest(), 16) % 100


def route(
    *,
    vid: str,
    kill_switch: bool,
    kids: bool,
    personalization: Personalization,
    first_session: bool,
    holdout_percent: int = DEFAULT_HOLDOUT_PERCENT,
) -> Route:
    """The rail of one request: the first rule that applies, in the module's table order. :data:`Strategy.LLM` means
    the viewer's model rail if a valid one is cached."""
    rules = (
        (kill_switch, Route(Strategy.BASELINE, Reason.KILL_SWITCH)),
        (kids, Route(Strategy.KIDS, Reason.KIDS)),
        (personalization is Personalization.OPTED_OUT, Route(Strategy.COHORT, Reason.OPTED_OUT)),
        (personalization is Personalization.UNAVAILABLE, Route(Strategy.COHORT, Reason.NO_CONSENT)),
        (holdout_bucket(vid) < holdout_percent, Route(Strategy.BASELINE, Reason.HOLDOUT)),
        (first_session, Route(Strategy.COHORT, Reason.FIRST_SESSION)),
    )
    return next((chosen for applies, chosen in rules if applies), Route(Strategy.LLM, Reason.MODEL))
