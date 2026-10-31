"""The Release Controller's apply path: the steps of the state machine ``<prefix>-release`` (DESIGN §8.3).

``Desired State Rendered`` starts an execution with the bundle's key, its digest and the event's ``correlation_id``
(the deployment's CloudFormation request). The steps, each one invocation of this handler (``step``):

1. ``prepare``: load the bundle and run every validator (R-F1 … R-S1, R-A1) and the secret check; a bundle that breaks
   any is refused and ledgered. Otherwise allocate the event's release: a redelivered event gets the same one (and is
   skipped once applied), a deployment that brings an earlier bundle back gets a new one.
2. ``accept``: take the controller's lock, so releases apply one at a time (another one holding it makes the step
   retry); then record the release as applying, choose the siblings it configures (a channel's idle sibling, all
   of a channel never released, any sibling not configured yet) and ledger the acceptance.
3. ``functions``: create the Function trees those siblings map, children first. An id is never updated: an existing
   id with the same definition is left alone (a retried step), one with another definition stops the release.
4. ``configure`` (one per chosen sibling): ``PutPlaybackConfiguration`` with the origin secret read now from Secrets
   Manager, its logging, and its vended-log deliveries without the fields that may hold the secret or the ticket,
   into the module's KMS-encrypted log group and into the lake.
5. ``publish``: record each channel's siblings, rewrite ``/plan/playback/<channel>.json`` for its stable sibling,
   ledger the release and give the lock back. A channel this release already recorded is not published again (a
   retried step).
6. ``fail`` (on any error after a bundle is accepted as valid): mark the release failed, give the lock back and ledger
   why.

Nothing here calls ``PutChannelPolicy``: the channel policy is the static pattern set with the channel (P0-08).
"""

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from fast_channels.core import specs
from fast_channels.core.demand import ORIGIN_SECRET_SLOTS
from fast_channels.core.ledger import LedgerKind, new_record_id
from fast_channels.core.mediatailor_logs import RECORD_FIELDS, VENDED_LOG_TYPES
from fast_channels.core.monetization import names
from fast_channels.core.monetization.functions import release_tag
from fast_channels.core.monetization.playback import release_tags, render, render_function
from fast_channels.core.monetization.release import (
    DONE,
    ReleaseStatus,
    applied,
    differences,
    mapped_functions,
    playback_plan,
    targets,
)
from fast_channels.core.monetization.validators import (
    Rule,
    Violation,
    check_rendered,
    check_rendered_function,
    check_secret_free,
    require_valid,
)
from fast_channels.core.tags import tag_key
from fast_channels.functions import aws
from fast_channels.functions.channel_ops import is_missing
from fast_channels.functions.ops.ledger import append, entry
from fast_channels.functions.release import store
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from pydantic import SecretStr

    from fast_channels.core.monetization.bundle import Bundle, MonetizedChannel, Sibling
    from fast_channels.core.monetization.functions import Function
    from fast_channels.core.monetization.release import ChannelState

logger = Logger()
DETAIL_LIMIT: Final = 20
"""Violations or errors kept in one ledger record."""


class ImmutableFunctionError(RuntimeError):
    """A FunctionId of the release exists with another definition; ids are never updated (P0-08)."""


def _now() -> datetime:
    return datetime.now(UTC)


def ledger(chain: str, action: str, subject: str, *, correlation_id: str, detail: dict[str, Any]) -> None:
    """One Release Controller record (principal ``release-controller``) on ``chain``."""
    append(
        entry(
            chain,
            LedgerKind.RELEASE,
            principal=names.PRINCIPAL,
            action=action,
            subject=subject,
            correlation_id=correlation_id,
            detail=detail,
        )
    )


# --- prepare and accept ---------------------------------------------------------------------------------------------


def prepare(bundle_key: str, digest: str, request: str, execution: str) -> dict[str, Any]:
    """Validate the bundle and allocate the release of event ``request``.

    Raises:
        InvalidBundleError: the bundle breaks a rule (ledgered as ``release.reject``; nothing is allocated).
    """
    bundle, violations, text = store.load(bundle_key)
    violations += check_secret_free(
        text, secrets=[store.secret(slot) for slot in ORIGIN_SECRET_SLOTS], where=f"bundle {bundle_key}"
    )
    if bundle is not None and bundle.digest != digest:
        violations.append(
            Violation(rule=Rule.F3, where=f"bundle {bundle_key}", message="the event's digest is not the bundle's")
        )
    if violations:
        ledger(
            names.BUNDLE_CHAIN,
            "release.reject",
            bundle_key,
            correlation_id=execution,
            detail={
                "digest": digest,
                "violations": [str(v) for v in violations[:DETAIL_LIMIT]],
                "count": len(violations),
            },
        )
        require_valid(violations)
    release = store.allocate(request)
    previous = store.status(release)
    if previous is not None and previous.status in DONE:
        logger.info("bundle already released", extra={"release": release, "digest": digest})
        return {"skip": True, "release": release}
    change_id = previous.change_id if previous else new_record_id(_now(), os.urandom(10))
    logger.info("release allocated", extra={"release": release, "change_id": change_id, "request": request})
    return {
        "skip": False,
        "release": release,
        "change_id": change_id,
        "bundle_key": bundle_key,
        "digest": digest,
        "request": request,
    }


def accept(
    bundle_key: str, digest: str, release: int, change_id: str, *, request: str, execution: str
) -> dict[str, Any]:
    """Take the lock, record the release as applying and choose the siblings it configures.

    Raises:
        ReleaseBusyError: another release holds the lock (the state machine retries the step).
    """
    store.lock(release)
    store.put_status(
        ReleaseStatus(
            release=release, digest=digest, bundle_key=bundle_key, change_id=change_id, status="APPLYING", at=_now()
        )
    )
    bundle = store.bundle(bundle_key)
    chosen = [s.name for s in targets(bundle, store.channels([c.name for c in bundle.channels]))]
    ledger(
        names.BUNDLE_CHAIN,
        "release.accept",
        bundle_key,
        correlation_id=change_id,
        detail={"digest": digest, "release": release, "siblings": chosen, "request": request, "execution": execution},
    )
    logger.info("release accepted", extra={"release": release, "siblings": chosen, "change_id": change_id})
    return {"siblings": chosen}


# --- functions ------------------------------------------------------------------------------------------------------


def _existing(function_id: str) -> dict[str, Any] | None:
    try:
        found: dict[str, Any] = aws.client("mediatailor").get_function(FunctionId=function_id)
    except ClientError as error:
        if is_missing(error):
            return None
        raise
    return found


def put_function(function: Function) -> bool:
    """Create ``function`` with its tags; True if it was created, False if it already existed unchanged.

    Raises:
        ImmutableFunctionError: the id exists with another definition.
    """
    existing = _existing(function.function_id)
    if existing is None:
        aws.client("mediatailor").put_function(**function.put_request())
        return True
    if changed := differences(function.put_request(tags=False), existing):
        raise ImmutableFunctionError(f"{function.function_id} exists with another definition ({', '.join(changed)})")
    return False


ORIGIN_SLOT_TAG: Final = tag_key("origin-slot")
"""``fast:origin-slot``: the origin secret slot a demand-origin Function carries (a rotation re-creates those trees
before it retires the slot)."""


def put_functions(
    bundle_key: str, release: int, change_id: str, siblings: list[str], *, slot: str | None = None
) -> dict[str, Any]:
    """The Function trees ``siblings`` map, with the ids of ``release``, children before the executors that run
    them. A demand-origin request (the ad-server route's auction) gets the origin secret of ``slot`` (default: the
    current one) and the release in its headers here, and a ``fast:origin-slot`` tag."""
    config = settings()
    config.require("prefix")
    tags = release_tags(config.prefix, release=release, change_id=change_id)
    bundle = store.bundle(bundle_key)
    created: list[str] = []
    existing: list[str] = []
    secret: SecretStr | None = None
    for function in mapped_functions(bundle, [_sibling(bundle, name) for name in siblings]):
        http = function.http_request_configuration
        origin = http is not None and bool(http.demand_origin)
        if origin and secret is None:
            slot = slot or store.origin_slot()
            secret = store.secret(slot)
        released = function.released(release, tags={**tags, **({ORIGIN_SLOT_TAG: slot} if origin and slot else {})})
        if origin and secret is not None:
            released = render_function(released, release=release, origin_secret=secret)
        if secret is not None:
            require_valid(
                check_rendered_function(released.put_request(), release=release, secrets=[secret], demand_origin=origin)
            )
        (created if put_function(released) else existing).append(released.function_id)
    logger.info("functions in place", extra={"release": release, "new": created, "existing": existing})
    return {"created": created, "existing": existing}


# --- configure ------------------------------------------------------------------------------------------------------


def _sibling(bundle: Bundle, name: str) -> Sibling:
    return next(s for s in bundle.siblings if s.name == name)


def _deliveries() -> dict[tuple[str, str], str]:
    """Every delivery of the account and region: (source name, destination ARN) -> id."""
    logs = aws.client("logs")
    return {
        (d["deliverySourceName"], d["deliveryDestinationArn"]): d["id"]
        for page in logs.get_paginator("describe_deliveries").paginate()
        for d in page["deliveries"]
    }


def _sources() -> dict[str, list[str]]:
    """Every delivery source of the account and region: name -> resource ARNs."""
    logs = aws.client("logs")
    return {
        s["name"]: list(s.get("resourceArns", []))
        for page in logs.get_paginator("describe_delivery_sources").paginate()
        for s in page["deliverySources"]
    }


def deliver(configuration: str, arn: str, tags: dict[str, str]) -> list[str]:
    """The configuration's vended logs: one delivery source per log type, delivered to the module's encrypted log
    group and (ads and manifest) to the lake, every delivery without the fields that are never stored. Returns the
    deliveries it created."""
    config = settings()
    config.require("release_log_destination", "lake_deliveries")
    lake = json.loads(config.lake_deliveries)
    logs = aws.client("logs")
    existing, sources = _deliveries(), _sources()
    created: list[str] = []
    for log_type, suffix in VENDED_LOG_TYPES.items():
        source = f"{configuration}-{suffix}"
        if sources.get(source) != [arn]:  # tags can only be given when a source is created ("ConflictException")
            extra = {} if source in sources else {"tags": tags}
            logs.put_delivery_source(name=source, resourceArn=arn, logType=log_type, **extra)
        fields = {"recordFields": list(RECORD_FIELDS[log_type])} if log_type in RECORD_FIELDS else {}
        destinations: list[tuple[str, dict[str, Any]]] = [(config.release_log_destination, {})]
        if log_type in lake:
            destinations.append((lake[log_type]["destination"], {"s3DeliveryConfiguration": lake[log_type]["s3"]}))
        for destination, options in destinations:
            if (source, destination) in existing:
                continue
            logs.create_delivery(
                deliverySourceName=source, deliveryDestinationArn=destination, tags=tags, **fields, **options
            )
            created.append(f"{source} -> {destination.rpartition(':')[2]}")
    return created


def put_sibling(bundle_key: str, sibling_name: str, release: int, change_id: str) -> tuple[dict[str, Any], Any]:
    """One sibling's configuration of ``release`` (with the current origin secret) and its logging; returns the
    ``PutPlaybackConfiguration`` request and response. Its vended-log deliveries are :func:`deliver`'s."""
    config = settings()
    config.require("prefix")
    sibling = _sibling(store.bundle(bundle_key), sibling_name)
    secret = store.secret(store.origin_slot())
    put, logging = render(
        sibling.playback, prefix=config.prefix, release=release, change_id=change_id, origin_secret=secret
    )
    require_valid(check_rendered(put, release=release, secrets=[secret], sends_origin=sibling.playback.sends_origin))
    mediatailor = aws.client("mediatailor")
    response = mediatailor.put_playback_configuration(**put)
    mediatailor.configure_logs_for_playback_configuration(**logging)
    return put, response


def configure(bundle_key: str, sibling_name: str, release: int, change_id: str) -> dict[str, Any]:
    """Apply the release to one sibling: its configuration (with the current origin secret), logging and logs."""
    sibling = _sibling(store.bundle(bundle_key), sibling_name)
    put, response = put_sibling(bundle_key, sibling_name, release, change_id)
    created = deliver(sibling.name, response["PlaybackConfigurationArn"], put["Tags"])
    logger.info("sibling configured", extra={"sibling": sibling.name, "release": release, "deliveries": created})
    return {
        "sibling": sibling.name,
        "channel": sibling.channel,
        "hls_prefix": response["HlsConfiguration"]["ManifestEndpointPrefix"],
    }


# --- publish --------------------------------------------------------------------------------------------------------


def _hls_prefix(configuration: str, configured: Mapping[str, str]) -> str:
    if configuration in configured:
        return configured[configuration]
    response = aws.client("mediatailor").get_playback_configuration(Name=configuration)
    return str(response["HlsConfiguration"]["ManifestEndpointPrefix"])


def write_plan(channel: MonetizedChannel, state: ChannelState, configured: Mapping[str, str], at: datetime) -> None:
    """Rewrite ``/plan/playback/<channel>.json`` for the state's stable sibling (``configured``: HLS prefixes this
    step already knows, by sibling)."""
    config = settings()
    config.require("media_bucket", "cdn_base_url")
    if state.stable is None:
        raise ValueError(f"{channel.name} has no stable sibling")
    plan = playback_plan(
        channel=channel.name,
        configuration=state.stable,
        release=state.siblings[state.stable].release,
        playback=specs.playback_urls(
            cdn_base=config.cdn_base_url,
            playback_prefix_hls=_hls_prefix(state.stable, configured),
            outputs=channel.outputs,
        ),
        at=at,
    )
    aws.client("s3").put_object(
        Bucket=config.media_bucket,
        Key=names.plan_key(channel.name),
        Body=plan.model_dump_json(indent=2).encode(),
        ContentType="application/json",
        CacheControl=names.PLAN_CACHE_CONTROL,
    )


def publish(
    bundle_key: str, digest: str, release: int, change_id: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Record each channel's configured siblings, rewrite its playback plan, ledger the release and give the lock
    back."""
    config = settings()
    config.require("media_bucket", "cdn_base_url")
    bundle = store.bundle(bundle_key)
    configured = {r["sibling"]: r["hls_prefix"] for r in results}
    at = _now()
    plans: dict[str, str] = {}
    for channel in bundle.channels:
        siblings = sorted(r["sibling"] for r in results if r["channel"] == channel.name)
        if not siblings:
            continue
        state, version = store.channel(channel.name)
        if state is not None and all(
            (record := state.siblings.get(s)) is not None and (record.release, record.change_id) == (release, change_id)
            for s in siblings
        ):
            logger.info("channel already published", extra={"channel": channel.name, "release": release})
            continue  # a retried step: its state, plan and ledger record were written
        state = applied(
            state, channel=channel.name, siblings=siblings, release=release, digest=digest, change_id=change_id, at=at
        )
        store.put_channel(state, version=version)
        write_plan(channel, state, configured, at)
        assert state.stable  # set by the channel's first release
        plans[channel.name] = state.stable
        ledger(
            names.channel_chain(channel.name),
            "release.apply",
            channel.name,
            correlation_id=change_id,
            detail={
                "release": release,
                "digest": digest,
                "siblings": siblings,
                "stable": state.stable,
                "idle": state.idle,
            },
        )
    previous = store.status(release)
    store.put_status(
        ReleaseStatus(
            release=release,
            digest=digest,
            bundle_key=bundle_key,
            change_id=change_id,
            status="APPLIED",
            at=previous.at if previous else at,
        )
    )
    logger.info("release applied", extra={"release": release, "plans": plans})  # the lock is kept for promotion
    return {"release": release, "plans": plans}


def fail(release: int | None, change_id: str | None, error: dict[str, Any], execution: str) -> dict[str, Any]:
    """Mark the release failed, give its lock back and ledger the error (the execution then fails)."""
    message = f"{error.get('Error', 'Error')}: {str(error.get('Cause', ''))[:1000]}"
    if release is not None:
        if (previous := store.status(release)) is not None:
            store.put_status(previous.model_copy(update={"status": "FAILED", "at": _now()}))
        store.unlock(release)
    ledger(
        names.BUNDLE_CHAIN,
        "release.fail",
        release_tag(release) if release else "unallocated",
        correlation_id=change_id or execution,
        detail={"release": release, "error": message, "execution": execution},
    )
    logger.error("release failed", extra={"release": release, "error": message})
    return {"release": release, "error": message}


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """One step of the apply path (``event["step"]``)."""
    logger.set_correlation_id(event.get("change_id") or event.get("execution"))
    match event["step"]:
        case "prepare":
            return prepare(event["bundle_key"], event["digest"], event["request"], event["execution"])
        case "accept":
            return accept(
                event["bundle_key"],
                event["digest"],
                event["release"],
                event["change_id"],
                request=event["request"],
                execution=event["execution"],
            )
        case "functions":
            return put_functions(event["bundle_key"], event["release"], event["change_id"], event["siblings"])
        case "configure":
            return configure(event["bundle_key"], event["sibling"], event["release"], event["change_id"])
        case "publish":
            return publish(event["bundle_key"], event["digest"], event["release"], event["change_id"], event["results"])
        case "fail":
            return fail(event.get("release"), event.get("change_id"), event["error"], event["execution"])
    raise ValueError(f"unknown step {event['step']!r}")
