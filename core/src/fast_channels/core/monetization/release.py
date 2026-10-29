"""The Release Controller's state and decisions, as pure functions (DESIGN §8.2-8.3).

Each monetized channel has the sibling pair ``a``/``b`` (and the probe ``z``). One of the pair is **stable**: viewers
play it (``/plan/playback/<channel>.json``). The other is **idle**: a release is applied to it only, so no
configuration with live sessions is remapped. A FunctionMapping remap reaches live sessions at their next break in
about 3 s, fleet-wide, which makes it the rollback primitive and never a canary (P0-08). The first release of a
channel configures every sibling (``a`` becomes stable, ``b`` idle). A sibling added later is configured by the next
release. Promotion (M3-04) swaps the pair and re-points the probe.

:func:`targets` picks the siblings a release configures, :func:`mapped_functions` the Functions it creates for
them, :func:`applied` records it, :func:`differences` compares what a configuration should be with what the service
returns (drift), and :class:`PlaybackPlan` is the document players without a ticket read.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.monetization.functions import release_tag
from fast_channels.core.monetization.names import HISTORY
from fast_channels.core.monetization.playback import ROLE_LETTERS, SiblingRole

if TYPE_CHECKING:
    from fast_channels.core.monetization.bundle import Bundle, Sibling
    from fast_channels.core.monetization.functions import Function

STABLE_LETTER, IDLE_LETTER = ROLE_LETTERS[SiblingRole.GENERAL]
PLAN_VERSION: Final = 1


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SiblingRecord(_Model):
    """What a release did to one sibling."""

    release: int = Field(ge=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    change_id: str
    at: datetime


class ChannelState(_Model):
    """A monetized channel's siblings, as the Release Controller last configured them."""

    channel: str
    stable: str | None = None
    """The sibling viewers play; None before the channel's first release."""
    idle: str | None = None
    """The sibling the next release configures."""
    siblings: dict[str, SiblingRecord] = Field(default_factory=dict)
    """By configuration name."""
    history: dict[str, tuple[int, ...]] = Field(default_factory=dict)
    """Releases each sibling had, newest first, at most :data:`~fast_channels.core.monetization.names.HISTORY`."""


class ReleaseStatus(_Model):
    """One release: the bundle it applies and how far it got."""

    release: int = Field(ge=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_key: str
    change_id: str
    status: Literal["APPLYING", "APPLIED", "FAILED"]
    at: datetime


def targets(bundle: Bundle, state: Mapping[str, ChannelState]) -> list[Sibling]:
    """The siblings a release of ``bundle`` configures: of a channel never released, all of them; afterwards its
    idle sibling, and any sibling not configured yet (a channel's new probe)."""
    chosen: list[Sibling] = []
    for sibling in bundle.siblings:
        channel = state.get(sibling.channel)
        idle = channel.idle if channel else None  # None: the channel was never released
        configured = channel.siblings if channel else {}
        if idle is None or sibling.name == idle or sibling.name not in configured:
            chosen.append(sibling)
    return chosen


def mapped_functions(bundle: Bundle, siblings: Sequence[Sibling]) -> list[Function]:
    """The functions the ``siblings`` run: each mapped root and the steps under it, in the bundle's order (children
    before the executors that run them). A release creates only these: the other siblings keep the ids they have."""
    wanted: set[str] = set()
    pending = [stem for sibling in siblings for stem in (sibling.playback.function_mapping or {}).values()]
    while pending:
        stem = pending.pop()
        if stem in wanted or (function := bundle.function(stem)) is None:
            continue
        wanted.add(stem)
        pending += [ref.function_id for ref in function.steps]
    return [f for f in bundle.functions if f.function_id in wanted]


def applied(
    state: ChannelState | None,
    *,
    channel: str,
    siblings: Sequence[str],
    release: int,
    digest: str,
    change_id: str,
    at: datetime,
) -> ChannelState:
    """``state`` after ``release`` configured ``siblings`` of ``channel``; the first release makes ``<channel>-p-a``
    stable and ``<channel>-p-b`` idle."""
    current = state or ChannelState(channel=channel)
    record = SiblingRecord(release=release, digest=digest, change_id=change_id, at=at)
    history = dict(current.history)
    for name in siblings:
        history[name] = (release, *(r for r in history.get(name, ()) if r != release))[:HISTORY]
    stable, idle = current.stable, current.idle
    if stable is None:
        stable, idle = f"{channel}-p-{STABLE_LETTER}", f"{channel}-p-{IDLE_LETTER}"
    return current.model_copy(
        update={
            "stable": stable,
            "idle": idle,
            "siblings": {**current.siblings, **dict.fromkeys(siblings, record)},
            "history": history,
        }
    )


class PlaybackPlan(_Model):
    """``/plan/playback/<channel>.json``: where a player without a ticket plays the channel (its stable sibling).

    Rewritten on every release that changes the stable sibling, every promotion and every rollback; served by the
    CDN with ``max-age=30``.
    """

    v: Literal[1] = PLAN_VERSION
    channel: str
    configuration: str
    release: str = Field(pattern=r"^r[0-9]{4}$")
    playback: dict[str, str] = Field(description="Playback URL by channel output, through the CDN.")
    updated: datetime


def playback_plan(
    *, channel: str, configuration: str, release: int, playback: Mapping[str, str], at: datetime
) -> PlaybackPlan:
    """The plan of ``channel`` playing ``configuration`` at ``release``."""
    return PlaybackPlan(
        channel=channel, configuration=configuration, release=release_tag(release), playback=dict(playback), updated=at
    )


def differences(desired: Any, actual: Any, path: str = "") -> list[str]:
    """Where ``actual`` (what the service returns) departs from ``desired`` (what was put), as dotted paths; a key
    the service adds is not a difference. Values are never in the result: a header may hold the origin secret."""
    match desired:
        case Mapping():
            if not isinstance(actual, Mapping):
                return [path or "."]
            found: list[str] = []
            for key, value in desired.items():
                where = f"{path}.{key}" if path else str(key)
                found += [where] if key not in actual else differences(value, actual[key], where)
            return found
        case list() | tuple():
            if not isinstance(actual, list | tuple) or len(actual) != len(desired):
                return [path]
            if all(not isinstance(item, Mapping) for item in desired):  # a set of names: the order does not matter
                return [] if sorted(map(repr, desired)) == sorted(map(repr, actual)) else [path]
            return [d for n, item in enumerate(desired) for d in differences(item, actual[n], f"{path}[{n}]")]
    return [] if desired == actual else [path]
