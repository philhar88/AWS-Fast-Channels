"""The ops module's kill switches: AWS AppConfig feature flags in the profile ``flags`` of the stage's application
``<prefix>-fast`` (D§19.5).

The stack deploys :func:`flags_document` (the ``AWS.AppConfig.FeatureFlags`` format: flag definitions and values) as
the profile's default; operators change a flag in AppConfig, which deploys a new version. Readers call
``GetLatestConfiguration`` and receive the simplified form, ``{"<flag>": {"enabled": ..., "<attribute>": ...}}``, in
which a disabled flag has no attributes: :meth:`Flags.from_simplified` reads it. Flag and attribute keys match
``^[a-z][a-zA-Z\\d_-]{0,63}$``, so the design's ``agents.enabled`` is the flag ``agents`` and ``ops.freeze`` the
attribute ``freeze`` of the flag ``ops``.

| Design switch | Flag | Who reads it |
|---|---|---|
| ``agents.enabled`` | ``agents`` | agents (M9) |
| ``commercial`` | ``commercial`` | selling (M7) |
| ``demand.mode`` | ``demand`` (``mode``) | the PDS (M2) |
| ``crawl.enabled`` | ``crawl`` | crawl (M17) |
| ``recs.strategy`` | ``recs`` (``strategy``) | the recommender (M10a) |
| ``release.promote`` | ``release`` (``promote``) | the Release Controller (M3) |
| ``ops.*`` | ``ops`` (``act``, ``freeze``, ``restart``, ``ca_alerts``, ``shed``) | the runbooks (M1) |
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

PROFILE: Final = "flags"
"""Name of the configuration profile."""


class DemandMode(StrEnum):
    """Where ads come from (``demand.mode``)."""

    OFF = "off"
    OWN_BOOK = "own_book"
    DEMO = "demo"
    LIVE = "live"


class RecsStrategy(StrEnum):
    """How rails are ranked (``recs.strategy``); ``popularity`` is the kill switch."""

    POPULARITY = "popularity"
    RECOMMENDER = "recommender"


class OpsSwitches(BaseModel):
    """Attributes of the ``ops`` flag. A runbook whose switch is off records that it was disabled and does nothing.

    ``act`` is off until ADR-0023 (L2 autonomy) is accepted: until then every runbook runs its dry-run path, which
    decides and records what it would do.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    act: bool = False
    freeze: bool = True
    """R9: freeze experiments and releases."""
    restart: bool = True
    """R12: start an always-on channel found stopped."""
    ca_alerts: bool = True
    """R13: Channel Assembly alerts."""
    shed: bool = True
    """R17: shed optional work."""


class Flags(BaseModel):
    """The flag values, as the stack deploys them and as readers see them."""

    model_config = ConfigDict(frozen=True)

    agents: bool = True
    commercial: bool = False
    demand: DemandMode = DemandMode.OFF
    crawl: bool = False
    recs: RecsStrategy = RecsStrategy.POPULARITY
    release_promote: bool = False
    ops: OpsSwitches = OpsSwitches()

    @classmethod
    def from_simplified(cls, data: Mapping[str, Any]) -> Flags:
        """Read ``GetLatestConfiguration``'s simplified form; missing flags keep their defaults."""

        def flag(name: str) -> Mapping[str, Any]:
            value = data.get(name)
            return value if isinstance(value, Mapping) else {}

        def enabled(name: str, default: bool) -> bool:
            return bool(flag(name).get("enabled", default))

        defaults = cls()
        demand = flag("demand")
        release = flag("release")
        ops = flag("ops")
        return cls(
            agents=enabled("agents", defaults.agents),
            commercial=enabled("commercial", defaults.commercial),
            demand=DemandMode(demand.get("mode", DemandMode.OFF)) if demand.get("enabled") else DemandMode.OFF,
            crawl=enabled("crawl", defaults.crawl),
            recs=RecsStrategy(flag("recs").get("strategy", RecsStrategy.POPULARITY)),
            release_promote=bool(release.get("enabled") and release.get("promote", False)),
            ops=OpsSwitches.model_validate(ops) if ops.get("enabled", True) else OpsSwitches(),
        )


def _bool(description: str) -> dict[str, Any]:
    return {"description": description, "constraints": {"type": "boolean", "required": True}}


def _enum(description: str, values: type[StrEnum]) -> dict[str, Any]:
    return {"description": description, "constraints": {"type": "string", "enum": [v.value for v in values]}}


def flags_document(flags: Flags) -> dict[str, Any]:
    """The profile's ``AWS.AppConfig.FeatureFlags`` content holding ``flags``."""
    definitions: dict[str, dict[str, Any]] = {
        "agents": {"name": "agents", "description": "Agents run (agents.enabled); off stops every agent."},
        "commercial": {"name": "commercial", "description": "Agentic selling and deal desk (commercial)."},
        "demand": {
            "name": "demand",
            "description": "Programmatic demand; off means demand.mode off.",
            "attributes": {"mode": _enum("demand.mode", DemandMode)},
        },
        "crawl": {"name": "crawl", "description": "Show-level metadata enrichment (crawl.enabled)."},
        "recs": {
            "name": "recs",
            "description": "Rail ranking (recs.strategy); popularity is the kill switch.",
            "attributes": {"strategy": _enum("recs.strategy", RecsStrategy)},
        },
        "release": {
            "name": "release",
            "description": "Release Controller; promote off stops promotion (rollback is never off).",
            "attributes": {"promote": _bool("release.promote")},
        },
        "ops": {
            "name": "ops",
            "description": "Runbook kill switches (ops.*); act stays off until ADR-0023 is accepted.",
            "attributes": {
                "act": _bool("Runbooks act (L2); off: every runbook runs its dry-run path."),
                "freeze": _bool("R9 may freeze experiments and releases."),
                "restart": _bool("R12 may start an always-on channel."),
                "ca_alerts": _bool("R13 handles Channel Assembly alerts."),
                "shed": _bool("R17 may shed optional work."),
            },
        },
    }
    values: dict[str, dict[str, Any]] = {
        "agents": {"enabled": flags.agents},
        "commercial": {"enabled": flags.commercial},
        "demand": {"enabled": flags.demand is not DemandMode.OFF, "mode": flags.demand.value},
        "crawl": {"enabled": flags.crawl},
        "recs": {"enabled": True, "strategy": flags.recs.value},
        "release": {"enabled": flags.release_promote, "promote": flags.release_promote},
        "ops": {"enabled": True, **flags.ops.model_dump()},
    }
    return {"version": "1", "flags": definitions, "values": values}
