"""The release bundle: the whole desired monetization state of a deployment, rendered by CDK, applied by the Release
Controller (DESIGN §8.1, OD-01).

A bundle holds every Function document (with ``Cache``, which CloudFormation lacks), every sibling playback
configuration with its Function mapping, the expected channel-policy pattern of each monetized channel, the stored
requests Prebid Server serves and the rollout strategy. It holds no secret, and no release number: FunctionIds are
stems until the Release Controller applies the bundle as release ``rNNNN`` to an idle sibling. Its :attr:`digest` is
the bundle's version; ``cdk diff`` shows a change as a new digest.

Other modules add per-hook function trees through :class:`BundleContribution` (M17 adds the PRE_ADS content lookup,
M4 the per-sibling ticket redeem). Each hook of each sibling is one root ``SEQUENTIAL_EXECUTOR`` that runs every
contribution's steps in order and commits exactly their ``outputs``: ``<p>_<hook>_root`` when every step is
deployment-wide, ``<p>_<sibling>_<hook>_root`` when one embeds the sibling's name (FunctionMapping holds one id per
hook, and a FunctionMapping remap reaches live sessions at their next break in about 3 s, fleet-wide: it is the
rollback primitive, never a canary, P0-08).
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Any, Final, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fast_channels.core.monetization.functions import (
    Function,
    FunctionRef,
    Hook,
    function_stem,
    ps_ctl,
    reexport,
    sequential_executor,
    sibling_scope,
)
from fast_channels.core.monetization.playback import (
    ROLE_LETTERS,
    AdsRoute,
    PlaybackConfiguration,
    SiblingRole,
    sibling_name,
    sibling_playback_configuration,
)
from fast_channels.core.specs import SIBLING_PATTERN

BUNDLE_SCHEMA_VERSION: Final = 1
SESSION_CONTROLS_BUDGET_MS: Final = 400
"""The session controls' share of the PRE_SESSION root timeout: one CUSTOM_OUTPUT of literals. With M4's 800 ms ticket
redeem before it the root allows 1,200 ms (DESIGN §5.3; the hook measured p50 117 ms, p99 217 ms with the redeem,
P0-05)."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_attribute_docstrings=True)


# --- contributions -------------------------------------------------------------------------------------------------


class Step(_Model):
    """One step of a hook's root executor: a non-executor function (nesting depth is 2) and when it runs."""

    function: Function
    run_condition: str | None = None
    """JSONata expression evaluated just before the step; the step is skipped when it is false."""
    alias: str | None = None


class HookTree(_Model):
    """What a contribution adds to one hook of one sibling.

    ``steps`` run in order inside the hook's root executor; ``outputs`` are the root ``Output`` entries that commit
    their results (an executor discards every binding its ``Output`` does not reference); ``budget_ms`` is the share
    of the root executor's timeout the steps need, margin included.
    """

    hook: Hook
    steps: tuple[Step, ...] = Field(min_length=1)
    outputs: dict[str, str] = Field(default_factory=dict)
    budget_ms: int = Field(gt=0)


class SiblingContext(_Model):
    """What a contribution may read about the sibling it builds for."""

    prefix: str
    channel: str
    name: str
    role: SiblingRole
    route: AdsRoute
    content_base_url: str


@runtime_checkable
class BundleContribution(Protocol):
    """A module's function trees. Contributions run in the order given; the session controls run last in
    PRE_SESSION, so nothing after them can change the constants."""

    @property
    def name(self) -> str:
        """Short name of the contributing module, for errors (``crawl``)."""
        ...

    def hook_trees(self, sibling: SiblingContext) -> Iterable[HookTree]:
        """The trees this module adds to ``sibling`` (none for a sibling it does not serve)."""
        ...


class SessionControls:
    """The core contribution: ``<p>_ps_ctl`` on every sibling's PRE_SESSION, committed by re-export."""

    name = "session-controls"

    def hook_trees(self, sibling: SiblingContext) -> Iterable[HookTree]:
        ctl = ps_ctl(sibling.prefix)
        yield HookTree(
            hook=Hook.PRE_SESSION,
            steps=(Step(function=ctl),),
            outputs={key: reexport(key) for key in ctl.output},
            budget_ms=SESSION_CONTROLS_BUDGET_MS,
        )


# --- rollout -------------------------------------------------------------------------------------------------------


class RolloutTier(StrEnum):
    """Audience size the release pointer's AppConfig strategy is chosen for (``modules.release.strategy``)."""

    DEMO = "demo"
    STANDARD = "standard"
    LARGE = "large"


class Rollout(_Model):
    """An AppConfig deployment strategy for the release pointer (``CreateDeploymentStrategy``)."""

    name: str
    growth_type: Literal["LINEAR", "EXPONENTIAL"]
    growth_factor: float = Field(gt=0, le=100)
    deployment_duration_in_minutes: int = Field(ge=0)
    final_bake_time_in_minutes: int = Field(ge=0)
    description: str = ""


ROLLOUTS: Final = {
    RolloutTier.DEMO: Rollout(
        name="demo",
        growth_type="LINEAR",
        growth_factor=10,
        deployment_duration_in_minutes=30,
        final_bake_time_in_minutes=10,
        description="Simulated gate: a simulated audience, 10% steps over 30 minutes.",
    ),
    RolloutTier.STANDARD: Rollout(
        name="standard",
        growth_type="LINEAR",
        growth_factor=10,
        deployment_duration_in_minutes=180,
        final_bake_time_in_minutes=60,
        description="Up to 20,000 concurrent viewers: 10% steps over 3 hours.",
    ),
    RolloutTier.LARGE: Rollout(
        name="large",
        growth_type="EXPONENTIAL",
        growth_factor=1,
        deployment_duration_in_minutes=240,
        final_bake_time_in_minutes=120,
        description="Above 20,000 concurrent viewers: 1%, 2%, 4% ... over 4 hours.",
    ),
}
"""DESIGN §8.4. Every step lasts longer than 2 minutes, because an alarm needs 90-118 s from first exposure, and no
strategy deploys all at once, because 0-minute deployments ignore the environment's monitors (P0-08 §3.5)."""


# --- the bundle ----------------------------------------------------------------------------------------------------


class MonetizedChannel(_Model):
    """A channel the release module monetizes, with the channel policy it must have (validator R-C1).

    The policy is created with the channel and never released (the Release Controller never calls
    ``PutChannelPolicy``: a policy change on a running channel is not session-safe, P0-08 §3.2).
    """

    name: str
    child_directed: bool = False
    lab: bool = False
    """The stage's lab channel (``<prefix>-lab``): smoke tests run on it while the monetized channels are stopped."""
    outputs: tuple[str, ...] = ("hls",)
    """The channel's outputs (rendition profiles): one playback URL each in ``/plan/playback/<channel>.json``."""
    policy_source_arns: tuple[str, ...]
    """The ``ArnLike aws:SourceArn`` values as playback-configuration names: the v3.0.0 configuration (the channel
    name) and the sibling pattern ``<channel>-p-?``."""


class Sibling(_Model):
    """One sibling playback configuration, release-agnostic."""

    name: str
    channel: str
    letter: str
    role: SiblingRole
    route: AdsRoute = AdsRoute.PBS
    playback: PlaybackConfiguration


class Bundle(_Model):
    """The desired state of one deployment (``release/<digest>/bundle.json``)."""

    v: Literal[1] = BUNDLE_SCHEMA_VERSION
    prefix: str
    functions: tuple[Function, ...]
    """Every Function document, stems for ids, children before the executors that run them."""
    siblings: tuple[Sibling, ...]
    channels: tuple[MonetizedChannel, ...]
    stored_requests: tuple[str, ...]
    """Stored-request ids the Prebid Server configuration serves (validator R-P5)."""
    grid_seconds: tuple[int, ...]
    """``session.avail_duration_secs`` of every break length the planner airs (30, 60, 90, 120)."""
    rollout: Rollout

    @model_validator(mode="after")
    def _unique(self) -> Self:
        for kind, names in (
            ("function", [f.function_id for f in self.functions]),
            ("sibling", [s.name for s in self.siblings]),
            ("channel", [c.name for c in self.channels]),
        ):
            if duplicates := sorted({n for n in names if names.count(n) > 1}):
                raise ValueError(f"duplicate {kind} names {duplicates}")
        return self

    @property
    def digest(self) -> str:
        """SHA-256 of the canonical JSON: the bundle's version."""
        return hashlib.sha256(canonical_json(self).encode()).hexdigest()

    def function(self, stem: str) -> Function | None:
        """The function document with id ``stem``."""
        return next((f for f in self.functions if f.function_id == stem), None)

    def released_functions(self, release: int, *, tags: dict[str, str]) -> list[Function]:
        """Every function with the ids of ``release``, in creation order (children first)."""
        return [f.released(release, tags=tags) for f in self.functions]


def canonical_json(model: BaseModel) -> str:
    """Sorted-key, whitespace-free JSON of ``model``, the input of its digest."""
    return json.dumps(
        model.model_dump(mode="json", by_alias=True, exclude_none=True), sort_keys=True, separators=(",", ":")
    )


class ChannelInput(_Model):
    """A monetized channel as the bundle builder needs it."""

    name: str
    video_content_source_url: str
    """Channel Assembly base URL of the channel, as the v3.0.0 configuration reads it."""
    child_directed: bool = False
    lab: bool = False
    """The stage's lab channel: its siblings publish the PRE_SESSION hook events for the smoke test."""
    outputs: tuple[str, ...] = ("hls",)
    """The channel's outputs (``channels.iac[].outputs``)."""


def root_stem(prefix: str, hook: Hook, *, sibling: str | None = None) -> str:
    """The root executor of ``hook``: ``<p>_<hook>_root`` or, when a step embeds the sibling, per sibling."""
    return function_stem(prefix, f"{hook.abbreviation}_root", sibling=sibling)


class _Functions:
    """Function documents by stem; a deployment-wide stem built for several siblings must be identical."""

    def __init__(self) -> None:
        self.documents: dict[str, Function] = {}

    def add(self, function: Function) -> str:
        existing = self.documents.setdefault(function.function_id, function)
        if existing != function:
            raise ValueError(f"function {function.function_id!r} is built differently for two siblings")
        return function.function_id


def _root(prefix: str, sibling: SiblingContext, hook: Hook, trees: Sequence[HookTree], functions: _Functions) -> str:
    steps = [step for tree in trees for step in tree.steps]
    scope = f"{prefix}_{sibling_scope(prefix, sibling.name)}_"
    scoped = any(step.function.function_id.startswith(scope) for step in steps)
    outputs: dict[str, str] = {}
    for tree in trees:
        if clash := sorted(set(outputs) & set(tree.outputs)):
            raise ValueError(f"two contributions commit {clash} at {hook} of {sibling.name}")
        outputs |= tree.outputs
    refs = [
        FunctionRef(function_id=functions.add(s.function), run_condition=s.run_condition, alias=s.alias) for s in steps
    ]
    root = sequential_executor(
        root_stem(prefix, hook, sibling=sibling.name if scoped else None),
        steps=refs,
        output=outputs,
        timeout_ms=sum(tree.budget_ms for tree in trees),
        description=f"{hook.value} root: runs {len(refs)} step(s) in order and commits only its Output.",
    )
    return functions.add(root)


def _letters(probe_sibling: bool) -> list[tuple[SiblingRole, str]]:
    roles = [SiblingRole.GENERAL, *([SiblingRole.PROBE] if probe_sibling else [])]
    return [(role, letter) for role in roles for letter in ROLE_LETTERS[role]]


def build_bundle(
    *,
    prefix: str,
    channels: Sequence[ChannelInput],
    content_base_url: str,
    pbs_origin: str,
    log_percent: int,
    stored_requests: Iterable[str],
    grid_seconds: Iterable[int],
    tier: RolloutTier = RolloutTier.DEMO,
    probe_sibling: bool = True,
    contributions: Sequence[BundleContribution] = (),
) -> Bundle:
    """The bundle of ``channels``: siblings ``a``/``b`` (and the probe ``z``) per channel, each hook's root executor
    over ``contributions`` and then the session controls. The probe, which never forwards a ticket, publishes the
    PRE_SESSION hook events as lab siblings do, so the committed session controls can be read from its logs.

    Phase 1 has no kids pairs and no ticket (M4); sessions are CONTEXTUAL and PRE_SESSION runs the controls alone.
    Validate the result with :func:`fast_channels.core.monetization.validators.validate_bundle`.
    """
    functions = _Functions()
    siblings: list[Sibling] = []
    every = [*contributions, SessionControls()]
    for channel in channels:
        for role, letter in _letters(probe_sibling):
            name = sibling_name(channel.name, letter)
            context = SiblingContext(
                prefix=prefix,
                channel=channel.name,
                name=name,
                role=role,
                route=AdsRoute.PBS,
                content_base_url=content_base_url,
            )
            by_hook: dict[Hook, list[HookTree]] = {}
            for contribution in every:
                for tree in contribution.hook_trees(context):
                    by_hook.setdefault(tree.hook, []).append(tree)
            mapping = {hook: _root(prefix, context, hook, trees, functions) for hook, trees in by_hook.items()}
            playback = sibling_playback_configuration(
                name=name,
                video_content_source_url=channel.video_content_source_url,
                content_base_url=content_base_url,
                pbs_origin=pbs_origin,
                log_percent=log_percent,
                function_mapping=mapping,
                kids=channel.child_directed or role is SiblingRole.KIDS,
                probe=role is SiblingRole.PROBE,
                lab=channel.lab or role is SiblingRole.PROBE,  # never ticketed: it may log the hook's output
            )
            siblings.append(Sibling(name=name, channel=channel.name, letter=letter, role=role, playback=playback))
    return Bundle(
        prefix=prefix,
        functions=tuple(_creation_order(functions.documents)),
        siblings=tuple(siblings),
        channels=tuple(
            MonetizedChannel(
                name=c.name,
                child_directed=c.child_directed,
                lab=c.lab,
                outputs=c.outputs,
                policy_source_arns=(c.name, f"{c.name}{SIBLING_PATTERN}"),
            )
            for c in channels
        ),
        stored_requests=tuple(sorted(set(stored_requests))),
        grid_seconds=tuple(sorted(set(grid_seconds))),
        rollout=ROLLOUTS[tier],
    )


def _creation_order(documents: dict[str, Function]) -> list[Function]:
    """Children before executors (a function must exist before an executor references it); stable by stem."""
    leaves = sorted((f for f in documents.values() if not f.steps), key=lambda f: f.function_id)
    roots = sorted((f for f in documents.values() if f.steps), key=lambda f: f.function_id)
    return [*leaves, *roots]


def bundle_document(bundle: Bundle) -> dict[str, Any]:
    """The bundle as stored: its JSON with the digest alongside (``{"digest": …, "bundle": …}``)."""
    return {"digest": bundle.digest, "bundle": bundle.model_dump(mode="json", by_alias=True, exclude_none=True)}
