"""The creative registry: its states, the readiness state machine's inputs, and what it publishes to the own book.

States (DESIGN-v2.2 §11; docs/creatives.md#states), per creative on the stage's default ladder:

- ``RECEIVED``: registered, its source copied to the quarantine bucket;
- ``REVIEW``: moderation (Amazon Rekognition) found labels at or above the stage's confidence: a person approves or
  rejects it (``scripts/creatives.py approve|reject``);
- ``APPROVED``: no such label, or a person approved it;
- ``CONDITIONING``: the ingest pipeline encodes it to the ladder;
- ``CONDITIONED``: the encode passed the ladder-conformance check (:mod:`~fast_channels.core.creatives.conformance`);
- ``READY``: published to the own book (:data:`~fast_channels.core.creatives.names.READY_KEY`);
- ``SUSPENDED``: withdrawn from the own book by an operator (``resume`` publishes it again);
- ``REJECTED``: refused by a person or by the conformance check, with the reason; ``FAILED``: a step failed (register
  it again to retry).

``OBSERVED`` and ``WARM`` (seen airing, then quiet) are not tracked yet: every registered creative counts as not yet
WARM, so the own book places it only where a standalone backs its slot (ad buffet; ``core.pds.pod``).
"""

from collections import defaultdict
from collections.abc import Mapping
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.catalog import DOMAIN_PATTERN
from fast_channels.core.creatives.names import CRID_PATTERN


class State(StrEnum):
    """A registered creative's state (module docstring)."""

    RECEIVED = "RECEIVED"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    CONDITIONING = "CONDITIONING"
    CONDITIONED = "CONDITIONED"
    READY = "READY"
    SUSPENDED = "SUSPENDED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


TRANSITIONS: Final[Mapping[State | None, frozenset[State]]] = {
    None: frozenset({State.RECEIVED}),
    State.RECEIVED: frozenset({State.APPROVED, State.REVIEW, State.FAILED}),
    State.REVIEW: frozenset({State.APPROVED, State.REJECTED}),
    State.APPROVED: frozenset({State.CONDITIONING, State.FAILED}),
    State.CONDITIONING: frozenset({State.CONDITIONED, State.REJECTED, State.FAILED}),
    State.CONDITIONED: frozenset({State.READY, State.FAILED}),
    State.READY: frozenset({State.SUSPENDED}),
    State.SUSPENDED: frozenset({State.READY}),
    State.REJECTED: frozenset(),
    State.FAILED: frozenset({State.RECEIVED}),
}
"""Every state a creative may move to from each (``None``: not registered yet)."""


class TransitionError(ValueError):
    """A creative may not move from its state to the one asked for."""


def check_transition(current: State | None, wanted: State) -> None:
    """Raise :class:`TransitionError` unless a creative in ``current`` may move to ``wanted``."""
    if wanted not in TRANSITIONS[current]:
        raise TransitionError(f"a creative {current.value if current else 'not registered'} cannot become {wanted}")


class Action(StrEnum):
    """What a readiness execution does."""

    REGISTER = "register"
    """Register a creative (or retry a failed one) and run it to READY."""
    APPROVE = "approve"
    """A person approves a creative in REVIEW; it is then conditioned and published."""
    REJECT = "reject"
    """A person rejects a creative in REVIEW."""
    SUSPEND = "suspend"
    """Withdraw a READY creative from the own book."""
    RESUME = "resume"
    """Publish a SUSPENDED creative again."""


DECIDED: Final[Mapping[Action, tuple[State, State]]] = {
    Action.APPROVE: (State.REVIEW, State.APPROVED),
    Action.REJECT: (State.REVIEW, State.REJECTED),
    Action.SUSPEND: (State.READY, State.SUSPENDED),
    Action.RESUME: (State.SUSPENDED, State.READY),
}
"""A person's decision: the state it applies to and the state it leads to."""


class Registration(BaseModel):
    """The readiness state machine's input that registers a creative."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal[Action.REGISTER] = Action.REGISTER
    crid: str = Field(pattern=CRID_PATTERN)
    advertiser: str = Field(min_length=1, max_length=100)
    adomain: str = Field(pattern=DOMAIN_PATTERN, max_length=253, description="The brand's domain (OpenRTB adomain).")
    source: str = Field(
        pattern=r"^(s3|https)://\S+$",
        max_length=2048,
        description="An MP4 or a VAST document (https://), or the quarantine bucket's copy of an uploaded file (s3://).",
    )
    by: str = Field(default="operator", min_length=1, max_length=100, description="Who registered it.")


class Decision(BaseModel):
    """The readiness state machine's input of a person's decision on a creative (:data:`DECIDED`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal[Action.APPROVE, Action.REJECT, Action.SUSPEND, Action.RESUME]
    crid: str = Field(pattern=CRID_PATTERN)
    by: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)


class RegisteredCreative(BaseModel):
    """A READY creative as the own book bids it: preconditioned on the ladder, with its advertiser and brand."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    crid: str = Field(pattern=CRID_PATTERN)
    creative_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", description="MediaTailor's immutable creative id.")
    hls_uri: str = Field(pattern=r"^https://", description="Its multivariant copy on the CDN.")
    duration_ms: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    advertiser: str = Field(min_length=1, max_length=100)
    adomain: str = Field(pattern=DOMAIN_PATTERN, max_length=253)


class ReadyCreatives(BaseModel):
    """:data:`~fast_channels.core.creatives.names.READY_KEY`: the READY creatives on one ladder, oldest first."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[1] = 1
    ladder_version: str
    creatives: tuple[RegisteredCreative, ...] = ()

    def by_brand(self) -> dict[str, tuple[str, ...]]:
        """The creatives' ids by brand domain, in publication order."""
        brands: defaultdict[str, list[str]] = defaultdict(list)
        for creative in self.creatives:
            brands[creative.adomain].append(creative.crid)
        return {brand: tuple(crids) for brand, crids in brands.items()}
