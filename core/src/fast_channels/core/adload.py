"""Ad load by tier: how many ads a break may hold for a viewer, and the price λ an ad must beat (ADR-0037).

``/select`` runs a sold ad in slot *j* only if its accepted net value beats λ(j), and fills a slot with a house ad
only where λ(j) is 0 (:mod:`fast_channels.core.pds.pod`). The PDS and anything that replays its decisions import this
module, so both price ad load the same way.

This is the simple rule of DESIGN §16, not its learned model. Each viewer's profile names an ad-load tier
(:mod:`fast_channels.core.pds.viewers`), and each tier has an :class:`AdLoadPolicy`:

- ``max_ads``: at most this many ads in one break, sold or house. The rest of the break is Channel Assembly's slate.
- ``bar_cpm``: a sold ad runs only if it is worth more than this accepted CPM (price * accept ratio). λ is
  ``bar_cpm / 1000 - c_ins`` for every slot, so ``price * accept / 1000 - c_ins > λ`` holds exactly when
  ``price * accept > bar_cpm``. The bar applies to tier-0 lines too: λ is what one more ad costs the viewer, whoever
  pays for it. A guaranteed line delivers on the other tiers.

Guardrails (DESIGN §16.1, "Guardrails"):

- only FULL and PSEUDONYMOUS breaks follow a tier. KIDS, SAFE and CONTEXTUAL breaks, probes, and breaks served
  without the ``/policy`` patch keep the control plan (:data:`CONTROL`: every slot, at the market floor);
- the tiers ``standard`` and ``holdout`` are always the control plan;
- λ never exceeds λ_cap, $20 CPM-equivalent (:data:`LAMBDA_CAP`);
- a tier never empties a pod: when its rules leave nothing, the pod holds the one ad the control plan ranks first
  (``pod.select_pod``, :attr:`AdLoadReason.MIN_LOAD`);
- the bar is applied here only, never on the wire, so bid requests are the same for every tier.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fast_channels.core.pds.models import Mode

C_INS: Final = Decimal("0.0005")
"""``c_ins``: the cost of inserting one ad, US dollars."""
LAMBDA_CAP: Final = Decimal("0.020")
"""λ_cap: the most λ may be, US dollars per insertion ($20 CPM-equivalent)."""
TIER_PATTERN: Final = r"^[a-z][a-z0-9-]{0,15}$"
"""An ad-load tier's name."""
CONTROL_TIER: Final = "standard"
"""The tier of the control plan: every slot, at the market floor."""
HOLDOUT_TIER: Final = "holdout"
"""The holdout's tier: the control plan, named apart so that its breaks can be told from the rest."""
CONTROL_TIERS: Final = frozenset({CONTROL_TIER, HOLDOUT_TIER})
"""Tiers that are always the control plan; no configuration may give them a policy."""
PERSONALIZED: Final = frozenset({Mode.FULL, Mode.PSEUDONYMOUS})
"""The modes whose breaks follow the viewer's tier."""


class AdLoadReason(StrEnum):
    """Which ad load a break got, and why (the ``pds.select`` record's ``ad_load.reason``)."""

    CONTROL = "control"
    """The viewer has no tier, a control tier (``standard``, ``holdout``) or a tier with no policy."""
    EXEMPT = "exempt"
    """The break's mode is not FULL or PSEUDONYMOUS, it is a probe, or it was served without the ``/policy`` patch."""
    TIER = "tier"
    """The tier's policy applied."""
    MIN_LOAD = "min_load"
    """The tier's policy left the pod empty, so it holds the one ad the control plan ranks first."""


class AdLoadPolicy(BaseModel):
    """The ad load of one tier."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_attribute_docstrings=True)

    max_ads: int | None = Field(default=None, ge=1, le=16)
    """The most ads one break may hold, sold or house; ``null``: as many as the pod has slots."""
    bar_cpm: Decimal = Field(default=Decimal(0), ge=0)
    """A sold ad runs only if its accepted CPM (price * accept ratio) is above this, US dollars; 0: the market floor
    alone. At most λ_cap + c_ins, $20.50."""

    @model_validator(mode="after")
    def _capped(self) -> Self:
        if self.lambda_usd > LAMBDA_CAP:
            raise ValueError(f"bar_cpm {self.bar_cpm} puts λ above λ_cap (${LAMBDA_CAP} per insertion)")
        return self

    @property
    def lambda_usd(self) -> Decimal:
        """λ for every slot: ``bar_cpm / 1000 - c_ins``, at least 0."""
        return max(self.bar_cpm / 1000 - C_INS, Decimal(0))

    @property
    def is_control(self) -> bool:
        """No cap and no bar: the control plan."""
        return self.max_ads is None and self.lambda_usd == 0


CONTROL: Final = AdLoadPolicy()
"""The control plan."""


@dataclass(frozen=True, slots=True)
class AdLoad:
    """The ad load of one break: the tier whose policy applies, why, and that policy."""

    policy: str
    reason: AdLoadReason
    rules: AdLoadPolicy

    def guarded(self) -> AdLoad:
        """This ad load after the minimum-load guardrail replaced it: the control plan's first ad."""
        return AdLoad(self.policy, AdLoadReason.MIN_LOAD, self.rules)


def ad_load(tier: str | None, mode: Mode | None, *, probe: bool, tiers: Mapping[str, AdLoadPolicy]) -> AdLoad:
    """The ad load of a break whose viewer has ``tier``, in ``mode`` (``None``: served without the policy patch).

    Args:
        tier: the viewer's ad-load tier, from their profile; ``None`` without a profile.
        mode: the mode of the ``/policy`` patch the module applied.
        probe: a probe session.
        tiers: the policy of each tier (the snapshot's ``ad_load``).
    """
    if probe or mode not in PERSONALIZED:
        return AdLoad(CONTROL_TIER, AdLoadReason.EXEMPT, CONTROL)
    if tier is None or tier in CONTROL_TIERS or (rules := tiers.get(tier)) is None or rules.is_control:
        return AdLoad(CONTROL_TIER, AdLoadReason.CONTROL, CONTROL)
    return AdLoad(tier, AdLoadReason.TIER, rules)


def slot_lambdas(slots: int, policy: AdLoadPolicy = CONTROL) -> tuple[Decimal, ...]:
    """λ for each of ``slots`` slots of one pod under ``policy``: the same for every slot, 0 in the control plan."""
    if slots < 0:
        raise ValueError(f"slots must not be negative, got {slots}")
    return (policy.lambda_usd,) * slots
