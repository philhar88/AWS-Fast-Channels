"""The demo's eight viewer personas (DESIGN §16.4), as ``contracts/personas.json`` publishes them.

One list serves every demo part: the seed script creates the signed-in personas' users and their profiles, which name
their ad-load tier (:mod:`fast_channels.core.pds.viewers`); the viewer app reads the names, sign-ins and genres. The
tier and the value and tolerance labels are the demo's premise, not something a viewer sees: the app shows a tier
only from the PDS's decision records. Every persona is fictional.

Value and tolerance form a grid, because neither cuts ads alone (DESIGN §16.1): only Dana, high value and sensitive,
gets the ``light`` tier; Marcus (high value, tolerant) and Sam (sensitive, low value) keep full pods, and Ben, Dana's
twin, is the holdout.
"""

from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fast_channels.core.adload import CONTROL_TIER, HOLDOUT_TIER, TIER_PATTERN
from fast_channels.core.pds.models import Mode

PERSONA_PATTERN: Final = r"^[a-z]{2,16}$"
"""A persona id: the ``custom:persona`` of its demo user."""
EMAIL_DOMAIN: Final = "personas.example.com"
"""The demo users' e-mail domain (RFC 2606: never a real mailbox)."""
LIGHT_TIER: Final = "light"
"""The demo's reduced-load tier: one ad per break, above a bar (``modules.pds.ad_load``)."""


class Value(StrEnum):
    """How much the persona's breaks are worth to buyers (the premise of the demo)."""

    HIGH = "high"
    MID = "mid"
    LOW = "low"


class Tolerance(StrEnum):
    """How readily the persona tunes away from ads (the premise of the demo)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SignIn(StrEnum):
    """How the persona reaches the app."""

    USER = "user"
    """A user of the viewer pool."""
    KIDS_PROFILE = "kids_profile"
    """A kids profile on another persona's sign-in: KIDS breaks, no viewer id, no profile."""
    GUEST = "guest"
    """No sign-in: CONTEXTUAL breaks, nothing stored."""


class Persona(BaseModel):
    """One demo persona."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=PERSONA_PATTERN)
    name: str = Field(min_length=1, max_length=64)
    bio: str = Field(min_length=1, max_length=200)
    mode: Mode = Field(description="The privacy mode of the persona's breaks.")
    value: Value | None = Field(description="Null for a kids profile and a guest.")
    tolerance: Tolerance | None = Field(description="Null for a kids profile and a guest.")
    tier: str | None = Field(
        pattern=TIER_PATTERN, description="The ad-load tier of the persona's profile; null without a profile."
    )
    sign_in: SignIn
    email: str | None = Field(description="The demo user's sign-in name; null for a kids profile and a guest.")
    account: str | None = Field(
        default=None, pattern=PERSONA_PATTERN, description="The persona whose sign-in a kids profile uses."
    )
    genres: tuple[str, ...] = Field(default=(), description="The rail's seed, in the catalog's genre vocabulary.")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        user = self.sign_in is SignIn.USER
        if user != (self.email == f"{self.id}@{EMAIL_DOMAIN}"):
            raise ValueError(f"{self.id}: a user's email is <id>@{EMAIL_DOMAIN}; others have none")
        if user != (self.tier is not None):
            raise ValueError(f"{self.id}: a user has a tier; a kids profile and a guest have none")
        if (self.sign_in is SignIn.KIDS_PROFILE) != (self.account is not None):
            raise ValueError(f"{self.id}: a kids profile, and only one, names the account it uses")
        if (self.sign_in is SignIn.KIDS_PROFILE) != (self.mode is Mode.KIDS):
            raise ValueError(f"{self.id}: KIDS breaks are a kids profile's")
        return self


def _user(
    id: str,
    name: str,
    *,
    bio: str,
    mode: Mode,
    value: Value,
    tolerance: Tolerance,
    tier: str,
    genres: tuple[str, ...],
) -> Persona:
    return Persona(
        id=id,
        name=name,
        bio=bio,
        mode=mode,
        value=value,
        tolerance=tolerance,
        tier=tier,
        sign_in=SignIn.USER,
        email=f"{id}@{EMAIL_DOMAIN}",
        genres=genres,
    )


PERSONAS: Final[tuple[Persona, ...]] = (
    _user(
        "dana",
        "Dana Reyes",
        bio="Designer in Austin who binges sci-fi, then fantasy and documentaries, five nights a week.",
        mode=Mode.FULL,
        value=Value.HIGH,
        tolerance=Tolerance.LOW,
        tier=LIGHT_TIER,
        genres=("sci-fi", "action", "fantasy"),
    ),
    _user(
        "ben",
        "Ben Okafor",
        bio="Dana's twin: the same habits, pinned to the holdout so the demo shows the full pod beside hers.",
        mode=Mode.FULL,
        value=Value.HIGH,
        tolerance=Tolerance.LOW,
        tier=HOLDOUT_TIER,
        genres=("sci-fi", "action", "fantasy"),
    ),
    _user(
        "marcus",
        "Marcus Hale",
        bio="Retired engineer who watches documentaries and classic comedy every weekday evening.",
        mode=Mode.FULL,
        value=Value.HIGH,
        tolerance=Tolerance.HIGH,
        tier=CONTROL_TIER,
        genres=("live-action", "action"),
    ),
    _user(
        "luis",
        "Luis Moreno",
        bio="Freelancer who keeps animation and comedy on in the background through the working day.",
        mode=Mode.PSEUDONYMOUS,
        value=Value.LOW,
        tolerance=Tolerance.HIGH,
        tier=CONTROL_TIER,
        genres=("animation", "surreal"),
    ),
    _user(
        "priya",
        "Priya Shah",
        bio="Nurse in San Jose who watches an hour of drama in the evening; she opts out of sale and sharing live.",
        mode=Mode.PSEUDONYMOUS,
        value=Value.MID,
        tolerance=Tolerance.MEDIUM,
        tier=CONTROL_TIER,
        genres=("fantasy", "adventure"),
    ),
    _user(
        "sam",
        "Sam Lee",
        bio="Student in his first week, on a phone in the daytime: sensitive to ads, but his breaks are worth little.",
        mode=Mode.FULL,
        value=Value.LOW,
        tolerance=Tolerance.LOW,
        tier=CONTROL_TIER,
        genres=("animation", "sci-fi"),
    ),
    Persona(
        id="mia",
        name="Mia",
        bio="Eight, on the kids profile of Marcus's account: kids animation on weekend mornings.",
        mode=Mode.KIDS,
        value=None,
        tolerance=None,
        tier=None,
        sign_in=SignIn.KIDS_PROFILE,
        email=None,
        account="marcus",
        genres=("animation",),
    ),
    Persona(
        id="guest",
        name="Guest",
        bio="A drive-by viewer who never signs in (the Jonas preset: in the EU, with no consent given).",
        mode=Mode.CONTEXTUAL,
        value=None,
        tolerance=None,
        tier=None,
        sign_in=SignIn.GUEST,
        email=None,
    ),
)
"""The eight personas, in DESIGN §16.4's order."""

_BY_ID: Final = {persona.id: persona for persona in PERSONAS}


def persona(id: str) -> Persona:
    """The persona ``id``.

    Raises:
        ValueError: no persona has that id.
    """
    try:
        return _BY_ID[id]
    except KeyError:
        raise ValueError(f"unknown persona {id!r}; known: {', '.join(_BY_ID)}") from None


class PersonaList(BaseModel):
    """``contracts/personas.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: int = 1
    personas: tuple[Persona, ...]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [p.id for p in self.personas]
        if len(set(ids)) != len(ids):
            raise ValueError("persona ids must be unique")
        if strays := sorted({p.account for p in self.personas if p.account} - set(ids)):
            raise ValueError(f"kids profiles on unknown accounts: {strays}")
        return self


PERSONA_LIST: Final = PersonaList(personas=PERSONAS)
"""What ``contracts/personas.json`` holds."""
