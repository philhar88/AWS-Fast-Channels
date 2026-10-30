"""Promotion and rollback (DESIGN §8.3-8.5, runbook R1): the steps after a release's siblings are configured.

- ``candidates``: the channels whose idle sibling the release configured, while another sibling is stable (a
  channel's first release has none: it is promoted by being applied).
- ``point``: a new version of the release pointer (AppConfig profile ``release``) naming each candidate, deployed
  with the stage's strategy. The environment's monitor rolls it back when a pair sibling alarms.
- ``pointer``: the deployment's state, polled by the state machine.
- ``promote``: at 100 %, each candidate becomes stable (the old stable sibling idle), its plan is rewritten and the
  promotion ledgered; the lock is given back.
- ``rollback`` (R1): each candidate is remapped to its last good tree (a remap reaches live sessions at their next
  break in about 3 s), the pointer goes back to the stable siblings at once, the plans name them, and the rollback is
  ledgered; the lock is given back.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from fast_channels.core.monetization import names
from fast_channels.core.monetization.functions import RELEASE_SUFFIX, FunctionType, Hook, released_id
from fast_channels.core.monetization.playback import render
from fast_channels.core.monetization.release import (
    SiblingRecord,
    kept_releases,
    last_good,
    pointer,
    promoted,
    rolled_back,
)
from fast_channels.core.monetization.smoke import request_timeouts, timed_out
from fast_channels.core.monetization.validators import check_rendered, require_valid
from fast_channels.functions import aws
from fast_channels.functions.channel_ops import ignore_missing, is_missing
from fast_channels.functions.release import apply, reaper, store
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

    from fast_channels.core.monetization.bundle import MonetizedChannel

logger = Logger()


class PointerBusyError(RuntimeError):
    """Another deployment of the environment is in progress (AppConfig allows one at a time); the step is retried."""


TERMINAL: Final = frozenset({"COMPLETE", "ROLLED_BACK", "REVERTED"})
TREE_ALARMS: Final = (
    "presessioninithook-errors",
    "preadsrequesthook-errors",
    "function-errors",
    "markers-without-ads-request",
)
"""The alarms of a sibling that a remap to its last good tree can clear. ``GetManifest.Errors`` stops a pointer
deployment but remaps nothing: a stopped channel or its origin raises it, not the sibling's Functions (fastm,
2026-10-29: a channel stopped under a probe session)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _channel(bundle_key: str, name: str) -> MonetizedChannel:
    return next(c for c in store.bundle(bundle_key).channels if c.name == name)


def candidates(bundle_key: str, release: int, siblings: list[str]) -> list[dict[str, str]]:
    """The channels whose idle sibling ``release`` configured, with a stable sibling of another release."""
    found: list[dict[str, str]] = []
    for channel in store.bundle(bundle_key).channels:
        state, _ = store.channel(channel.name)
        if state is None or state.stable is None or state.idle not in siblings:
            continue
        if state.siblings[state.stable].release != release:
            found.append({"channel": channel.name, "sibling": state.idle, "stable": state.stable})
    return found


def on_air(channel: str) -> bool:
    """Whether ``channel`` runs (a channel that does not exist does not)."""
    try:
        state = aws.client("mediatailor").describe_channel(ChannelName=channel)["ChannelState"]
    except ClientError as error:
        if is_missing(error):
            return False
        raise
    return bool(state == "RUNNING")


def smoke_plan(bundle_key: str, chosen: list[dict[str, str]]) -> dict[str, Any]:
    """Where the candidates are smoke-tested: on their own channels if any of them runs, else on the lab channel
    (started for it, through the agent tools), else nowhere (the release is applied but not promoted)."""
    lab = next((c.name for c in store.bundle(bundle_key).channels if c.lab), None)
    running = [c for c in chosen if c["channel"] != lab and on_air(c["channel"])]
    if running:
        return {"smoke": running, "lab": None}
    lab_candidates = [c for c in chosen if c["channel"] == lab]
    if lab_candidates and settings().tool_functions:
        return {"smoke": lab_candidates, "lab": lab}
    return {"smoke": [], "lab": None}


def _running_deployments() -> list[dict[str, Any]]:
    """The environment's deployments in progress (AppConfig allows one at a time)."""
    config = settings()
    config.require("appconfig_application", "appconfig_environment")
    items = aws.client("appconfig").list_deployments(
        ApplicationId=config.appconfig_application, EnvironmentId=config.appconfig_environment, MaxResults=5
    )["Items"]
    return [d for d in items if d["State"] not in TERMINAL]


def stop_pointer() -> list[int]:
    """Stop the release pointer's deployment in progress, if any (a rollback must not wait out a rollout); another
    profile's deployment (the flags) is left to finish."""
    config = settings()
    config.require("appconfig_application", "appconfig_environment", "release_profile")
    client = aws.client("appconfig")
    stopped: list[int] = []
    for deployment in _running_deployments():
        number = int(deployment["DeploymentNumber"])
        found = client.get_deployment(
            ApplicationId=config.appconfig_application,
            EnvironmentId=config.appconfig_environment,
            DeploymentNumber=number,
        )
        if found.get("ConfigurationProfileId") == config.release_profile:
            client.stop_deployment(
                ApplicationId=config.appconfig_application,
                EnvironmentId=config.appconfig_environment,
                DeploymentNumber=number,
            )
            stopped.append(number)
    if stopped:
        logger.warning("pointer deployment stopped for the rollback", extra={"deployments": stopped})
    return stopped


def _deploy(content: dict[str, str], *, strategy: str, description: str, tags: dict[str, str]) -> dict[str, Any]:
    config = settings()
    config.require("appconfig_application", "appconfig_environment", "release_profile")
    client = aws.client("appconfig")
    if _running_deployments():
        raise PointerBusyError("another deployment of the environment is in progress")
    version = client.create_hosted_configuration_version(
        ApplicationId=config.appconfig_application,
        ConfigurationProfileId=config.release_profile,
        Content=json.dumps(content, sort_keys=True).encode(),
        ContentType="application/json",
        Description=description,
    )["VersionNumber"]
    deployment = client.start_deployment(
        ApplicationId=config.appconfig_application,
        EnvironmentId=config.appconfig_environment,
        DeploymentStrategyId=strategy,
        ConfigurationProfileId=config.release_profile,
        ConfigurationVersion=str(version),
        Description=description,
        Tags=tags,
    )
    return {"deployment": deployment["DeploymentNumber"], "version": version, "pointer": content}


def _at_once() -> str:
    """A rollback (or a pointer that moves no viewer) goes at once; only a promotion is gradual and monitored."""
    config = settings()
    config.require("release_at_once_strategy")
    return config.release_at_once_strategy


def point(release: int, change_id: str, chosen: list[dict[str, str]]) -> dict[str, Any]:
    """Deploy the pointer that names each candidate (and every other channel's stable sibling)."""
    config = settings()
    config.require("prefix", "release_strategy", "release_channels")
    states = store.channels(list(config.release_channels))
    moving = {c["channel"] for c in chosen}
    content = pointer({name: promoted(state) if name in moving else state for name, state in states.items()})
    tags = {"fast:release": f"r{release:04d}", "fast:change": change_id}
    deployed = _deploy(content, strategy=config.release_strategy, description=f"release r{release:04d}", tags=tags)
    apply.ledger(
        names.BUNDLE_CHAIN,
        "release.point",
        f"r{release:04d}",
        correlation_id=change_id,
        detail={"release": release, "pointer": content, "deployment": deployed["deployment"]},
    )
    logger.info("pointer deploying", extra=deployed)
    return deployed


def step_maxima(sibling: str, function_id: str, start: datetime, end: datetime) -> list[float]:
    """The per-minute maxima (ms) of a PRE_ADS ``HTTP_REQUEST`` function's ``Function.Latency`` on ``sibling``."""
    dimensions = {
        "ConfigurationName": sibling,
        "FunctionId": function_id,
        "HookType": Hook.PRE_ADS.value,
        "FunctionType": FunctionType.HTTP_REQUEST.value,
    }
    points = aws.client("cloudwatch").get_metric_statistics(
        Namespace="AWS/MediaTailor",
        MetricName="Function.Latency",
        Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
        StartTime=start,
        EndTime=end,
        Period=60,
        Statistics=["Maximum"],
    )["Datapoints"]
    return [float(p["Maximum"]) for p in points]


def timed_out_steps(
    bundle_key: str, release: int, sibling: str, start: datetime, end: datetime
) -> dict[str, dict[str, int]]:
    """The PRE_ADS requests of ``release`` on ``sibling`` that reached their timeout in
    :data:`~fast_channels.core.monetization.smoke.TIMEOUT_PERIODS` minutes or more between ``start`` and ``end``: by
    FunctionId, the timeout (from the release's bundle) and the minutes. No error metric counts them."""
    timeouts = {
        released_id(stem, release): ms for stem, ms in request_timeouts(store.bundle(bundle_key), sibling).items()
    }
    maxima = {function_id: step_maxima(sibling, function_id, start, end) for function_id in timeouts}
    return {f: {"timeout_ms": timeouts[f], "minutes": n} for f, n in timed_out(maxima, timeouts).items()}


def pointer_state(
    deployment: int,
    bundle_key: str | None = None,
    release: int | None = None,
    chosen: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The pointer deployment's state and progress.

    A candidate whose PRE_ADS request reached its timeout in the deployment (:func:`timed_out_steps`) stops it: the
    state is ``ROLLED_BACK`` and ``timed_out`` names the requests. The environment's monitors count errors, and a
    timed-out request is none.
    """
    config = settings()
    config.require("appconfig_application", "appconfig_environment")
    client = aws.client("appconfig")
    found = client.get_deployment(
        ApplicationId=config.appconfig_application,
        EnvironmentId=config.appconfig_environment,
        DeploymentNumber=deployment,
    )
    late: dict[str, dict[str, dict[str, int]]] = {}
    if bundle_key and release and chosen and found["State"] not in TERMINAL:
        started, now = found.get("StartedAt") or _now(), _now()
        for candidate in chosen:
            if steps := timed_out_steps(bundle_key, release, candidate["sibling"], started, now):
                late[candidate["sibling"]] = steps
        if late:
            client.stop_deployment(
                ApplicationId=config.appconfig_application,
                EnvironmentId=config.appconfig_environment,
                DeploymentNumber=deployment,
            )
            logger.warning("pointer deployment stopped: a PRE_ADS request timed out", extra={"timed_out": late})
            found = found | {"State": "ROLLED_BACK"}
    events = [e.get("Description", "") for e in found.get("EventLog", [])][:5]
    return {
        "deployment": deployment,
        "state": found["State"],
        "percent": found.get("PercentageComplete", 0),
        "done": found["State"] in TERMINAL,
        "events": events,
        **({"timed_out": late} if late else {}),
    }


def promote(bundle_key: str, release: int, change_id: str, chosen: list[dict[str, str]]) -> dict[str, Any]:
    """Make each candidate stable, rewrite its plan, ledger the promotion and give the lock back."""
    promoted_to: dict[str, str] = {}
    for candidate in chosen:
        state, version = store.channel(candidate["channel"])
        assert state is not None
        if state.stable != candidate["sibling"]:  # not done yet (a retried step finds it done)
            state = promoted(state)
            store.put_channel(state, version=version)
        apply.write_plan(_channel(bundle_key, candidate["channel"]), state, {}, _now())
        promoted_to[candidate["channel"]] = candidate["sibling"]
        apply.ledger(
            names.channel_chain(candidate["channel"]),
            "release.promote",
            candidate["channel"],
            correlation_id=change_id,
            detail={"release": release, "stable": state.stable, "idle": state.idle},
        )
    config = settings()
    config.require("release_channels")
    store.put_pointer(pointer(store.channels(list(config.release_channels))))
    if (previous := store.status(release)) is not None:
        store.put_status(previous.model_copy(update={"status": "PROMOTED", "at": _now()}))
    store.unlock(release)
    logger.info("release promoted", extra={"release": release, "stable": promoted_to})
    return {"release": release, "stable": promoted_to}


def remap(sibling_name: str, record: SiblingRecord) -> None:
    """Put ``sibling_name`` back on the configuration of ``record``'s release (its Functions still exist)."""
    config = settings()
    config.require("prefix")
    status = store.status(record.release)
    if status is None:
        raise ValueError(f"{sibling_name}: no record of release {record.release}")
    sibling = next(s for s in store.bundle(status.bundle_key).siblings if s.name == sibling_name)
    secret = store.secret(store.origin_slot())
    put, _ = render(
        sibling.playback, prefix=config.prefix, release=record.release, change_id=record.change_id, origin_secret=secret
    )
    require_valid(check_rendered(put, release=record.release, secrets=[secret]))
    aws.client("mediatailor").put_playback_configuration(**put)


def rollback(
    bundle_key: str, release: int, change_id: str, chosen: list[dict[str, str]], reason: dict[str, Any]
) -> dict[str, Any]:
    """R1: remap each candidate to its last good tree, point back at the stable siblings, rewrite the plans, ledger
    the rollback and give the lock back."""
    config = settings()
    config.require("release_channels")
    remapped: dict[str, int | None] = {}
    for candidate in chosen:
        state, version = store.channel(candidate["channel"])
        assert state is not None
        sibling = candidate["sibling"]
        target = last_good(state, sibling)
        if state.siblings[sibling].release == release and target is not None:
            good = store.status(target)
            assert good is not None
            record = SiblingRecord(release=target, digest=good.digest, change_id=good.change_id, at=_now())
            remap(sibling, record)
            state = rolled_back(state, sibling, record=record)
            store.put_channel(state, version=version)
        remapped[sibling] = state.siblings[sibling].release
        apply.write_plan(_channel(bundle_key, candidate["channel"]), state, {}, _now())
    content = pointer(store.channels(list(config.release_channels)))
    stop_pointer()  # a rollout still in progress (the pointer step failed) goes no further
    deployed = _deploy(
        content,
        strategy=_at_once(),
        description=f"rollback of r{release:04d}",
        tags={"fast:release": f"r{release:04d}", "fast:change": change_id},
    )
    apply.ledger(
        names.BUNDLE_CHAIN,
        "release.rollback",
        f"r{release:04d}",
        correlation_id=change_id,
        detail={"release": release, "remapped": remapped, "pointer": content, "reason": reason},
    )
    store.put_pointer(content)
    if (previous := store.status(release)) is not None:
        store.put_status(previous.model_copy(update={"status": "ROLLED_BACK", "at": _now()}))
    store.unlock(release)
    logger.warning("release rolled back", extra={"release": release, "remapped": remapped})
    return {"release": release, "remapped": remapped, "deployment": deployed["deployment"]}


def collect() -> list[str]:
    """Delete the Functions of every release no sibling keeps (each keeps three trees: the current one, the last good
    one and the one before), executors before the functions they run. Returns the ids deleted."""
    config = settings()
    config.require("prefix", "release_channels")
    kept = kept_releases(store.channels(list(config.release_channels)))
    stale = [
        f
        for f in reaper.functions(config.prefix)
        if (suffix := RELEASE_SUFFIX.search(f["FunctionId"])) and int(suffix.group()[2:]) not in kept
    ]
    ordered = sorted(stale, key=lambda f: (f["FunctionType"] != FunctionType.SEQUENTIAL_EXECUTOR, f["FunctionId"]))
    mediatailor = aws.client("mediatailor")
    for function in ordered:
        ignore_missing(mediatailor.delete_function, FunctionId=function["FunctionId"])
    deleted = [f["FunctionId"] for f in ordered]
    logger.info("old Function trees deleted", extra={"kept": sorted(kept), "deleted": deleted})
    return deleted


def alarming_siblings(names_: list[str]) -> set[str]:
    """The siblings among ``names_`` with an alarm of their Functions in ALARM (:data:`TREE_ALARMS`; the candidate
    composite alarm's children are named ``<sibling>-<metric>``)."""
    found: set[str] = set()
    client = aws.client("cloudwatch")
    for page in client.get_paginator("describe_alarms").paginate(StateValue="ALARM", AlarmTypes=["MetricAlarm"]):
        for alarm in page["MetricAlarms"]:
            found |= {n for n in names_ if alarm["AlarmName"] in {f"{n}-{suffix}" for suffix in TREE_ALARMS}}
    return found


def runbook(alarm: str, correlation_id: str) -> dict[str, Any]:
    """R1 outside a promotion: each pair sibling in ALARM goes back to its last good tree (a remap, fleet-wide in
    about 3 s) and its channel's plan is rewritten. While a release holds the lock, its own steps roll back (the
    pointer's monitor stops the deployment), so nothing is done here."""
    config = settings()
    config.require("release_channels")
    states = store.channels(list(config.release_channels))
    pair = [n for state in states.values() for n in (state.stable, state.idle) if n]
    if (holder := store.holder()) is not None:
        logger.info(
            "a release or rotation holds the lock; R1 leaves the siblings to it",
            extra={"holder": holder, "alarm": alarm},
        )
        return {"remapped": {}, "deferred_to": holder}
    remapped: dict[str, int] = {}
    for sibling in sorted(alarming_siblings(pair)):
        channel = next(name for name, state in states.items() if sibling in (state.stable, state.idle))
        state, version = store.channel(channel)
        assert state is not None
        target = last_good(state, sibling)
        if target is None or (good := store.status(target)) is None:
            logger.warning("no last good tree to go back to", extra={"sibling": sibling})
            continue
        record = SiblingRecord(release=target, digest=good.digest, change_id=good.change_id, at=_now())
        remap(sibling, record)
        state = rolled_back(state, sibling, record=record)
        store.put_channel(state, version=version)
        apply.write_plan(_channel(good.bundle_key, channel), state, {}, _now())
        remapped[sibling] = target
        apply.ledger(
            names.channel_chain(channel),
            "release.rollback",
            channel,
            correlation_id=correlation_id,
            detail={"runbook": "R1", "alarm": alarm, "sibling": sibling, "release": target},
        )
    logger.warning("runbook R1", extra={"alarm": alarm, "remapped": remapped})
    return {"remapped": remapped}


def finish(release: int, change_id: str) -> dict[str, Any]:
    """A release without candidates (channels' first releases, a new probe): publish the pointer at once if it
    changed (it moves no viewer), delete the trees no sibling keeps and give the lock back."""
    config = settings()
    config.require("release_channels")
    content = pointer(store.channels(list(config.release_channels)))
    deployed: dict[str, Any] | None = None
    if content and content != store.pointer():
        deployed = _deploy(
            content,
            strategy=_at_once(),
            description=f"pointer of r{release:04d}",
            tags={"fast:release": f"r{release:04d}", "fast:change": change_id},
        )
        store.put_pointer(content)
    deleted = collect()
    store.unlock(release)
    return {
        "release": release,
        "pointer": content,
        "deployment": deployed and deployed["deployment"],
        "deleted": deleted,
    }


def _candidates(event: dict[str, Any]) -> dict[str, Any]:
    chosen = candidates(event["bundle_key"], event["release"], event["siblings"])
    plan = smoke_plan(event["bundle_key"], chosen) if chosen else {"smoke": [], "lab": None}
    if chosen and not plan["smoke"]:
        apply.ledger(
            names.BUNDLE_CHAIN,
            "release.hold",
            f"r{event['release']:04d}",
            correlation_id=event["change_id"],
            detail={"release": event["release"], "reason": "no running channel or lab channel to smoke-test on"},
        )
    return {"candidates": chosen, **plan}


def _promote_and_collect(event: dict[str, Any]) -> dict[str, Any]:
    result = promote(event["bundle_key"], event["release"], event["change_id"], event["candidates"])
    return result | {"deleted": collect()}


STEPS: Final[dict[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "candidates": _candidates,
    "finish": lambda e: finish(e["release"], e["change_id"]),
    "point": lambda e: point(e["release"], e["change_id"], e["candidates"]),
    "pointer": lambda e: pointer_state(e["deployment"], e.get("bundle_key"), e.get("release"), e.get("candidates")),
    "promote": _promote_and_collect,
    "rollback": lambda e: rollback(e["bundle_key"], e["release"], e["change_id"], e["candidates"], e["reason"]),
}


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """One promotion or rollback step (``event["step"]``), or runbook R1 (an alarm state change)."""
    if event.get("detail-type") == "CloudWatch Alarm State Change":  # the candidate alarm's rule
        logger.set_correlation_id(event["id"])
        return runbook(event["detail"]["alarmName"], event["id"])
    logger.set_correlation_id(event.get("change_id") or event.get("execution"))
    if (step := STEPS.get(event["step"])) is None:
        raise ValueError(f"unknown step {event['step']!r}")
    return step(event)
