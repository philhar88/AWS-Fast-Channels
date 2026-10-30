"""Origin-secret rotation (DESIGN §8.6): the two-value overlap, one step per invocation of the rotation state machine.

The siblings send one of the two origin secrets (``store.origin_slot()``) in ``X-Fast-Origin``, and the WAF rule
group ``<prefix>-demand-origin`` (rule ``fast-origin-required``) admits either. A daily check rotates
when ``modules.release.secret_rotation_days`` days have passed; a rotation also runs by hand on a suspected exposure:

1. ``begin``: hold the Release Controller's lock (no release applies meanwhile) and choose the other slot;
2. ``admit``: give the other slot a new value and admit it at the WAF, beside the current one;
3. ``probe``: once the WAF change reached CloudFront, a probe POST with the new value is not blocked;
4. ``switch``: a Function tree that carries the origin secret itself (the ad-server route's auction, M8-09) is
   immutable, so each one a sibling keeps is created again under a new release with the new value, and the
   sibling's record and history name it; then every configured sibling sends the new value
   (``PutPlaybackConfiguration`` of its own release, the call class of a remap: live sessions use it at their next
   break), and the slot is recorded;
5. ``verify``: while a monetized channel is on air, its siblings' ad requests since the switch were answered and a
   break was filled;
6. ``retire``: unless a Function a sibling keeps still carries the old value (``fast:origin-slot``), the old slot
   gets a value nobody has seen, which the WAF admits instead of the old one.

The lock is then given back. A failure at any step gives the lock back and leaves admitted every value a sibling
may send: the rotation can be run again. No secret value is ever logged or ledgered.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

import httpx
from aws_lambda_powertools import Logger

from fast_channels.core.demand import ORIGIN_HEADER, ORIGIN_SECRET_SLOTS, origin_rule_group, origin_secret
from fast_channels.core.monetization import names
from fast_channels.core.monetization.functions import RELEASE_SUFFIX, release_tag, released_id
from fast_channels.core.monetization.playback import render
from fast_channels.core.monetization.release import ChannelState, mapped_functions
from fast_channels.core.monetization.validators import check_rendered, require_valid
from fast_channels.functions import aws
from fast_channels.functions.release import apply, promote, reaper, smoke, store
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from pydantic import SecretStr

    from fast_channels.core.monetization.bundle import Bundle

logger = Logger()
ROTATION: Final = store.ROTATION
SCOPE: Final = "CLOUDFRONT"
RULE: Final = "fast-origin-required"
SECRET_LENGTH: Final = 48
DUE_SLACK: Final = timedelta(hours=12)
"""A daily check a few minutes short of the period still rotates on its day (a rotation takes minutes)."""
VERIFY_QUERY: Final = (
    "filter originId in [{siblings}] and eventType in ['MAKING_ADS_REQUEST', 'FILLED_AVAIL', "
    "'ERROR_ADS_INVALID_RESPONSE'] and toMillis(@timestamp) >= {since_ms}\n"
    "| fields strcontains(additionalInfo, 'Code:403') as is_refused\n"
    "| stats count(*) as n, sum(is_refused) as refused by eventType"
)
"""Since the switch only (the query helper widens its window by a minute), and the origin gate's refusals (403)
apart from the ADS's other failures: a Prebid Server 500 is no rotation's fault (fastm, 2026-10-30)."""


class NotAdmittedError(RuntimeError):
    """The WAF still refuses the new value (its change has not reached CloudFront yet); the step is retried."""


class NotVerifiedError(RuntimeError):
    """No break was answered since the switch yet; the step is retried."""


class RefusedError(RuntimeError):
    """The siblings' ad requests were refused since the switch: the rotation stops before it retires the old value."""


def _now() -> datetime:
    return datetime.now(UTC)


def _other(slot: str) -> str:
    return next(s for s in ORIGIN_SECRET_SLOTS if s != slot)


def last_rotated() -> datetime:
    """When the siblings' origin secret last changed: the last switch, else the current secret's last change."""
    if (at := store.origin_rotated_at()) is not None:
        return at
    config = settings()
    client = aws.regional_client("secretsmanager", config.origin_secret_region)
    found = client.describe_secret(SecretId=origin_secret(config.prefix, store.origin_slot()))
    return cast("datetime", found.get("LastChangedDate") or found["CreatedDate"])


def due() -> bool:
    """Whether ``secret_rotation_days`` (less :data:`DUE_SLACK`) have passed since the last rotation."""
    config = settings()
    config.require("secret_rotation_days")
    return _now() - last_rotated() >= timedelta(days=config.secret_rotation_days) - DUE_SLACK


def begin(change_id: str, *, scheduled: bool = False) -> dict[str, Any]:
    """Hold the lock for this execution (``store.ReleaseBusyError`` while a release or another rotation holds it)
    and choose the slot to rotate to. A scheduled run that is not :func:`due` does nothing."""
    if scheduled and not due():
        return {"due": False}
    store.lock(ROTATION, owner=change_id)
    current = store.origin_slot()
    target = _other(current)
    apply.ledger(
        names.ROTATION_CHAIN,
        "rotation.begin",
        f"slot-{target}",
        correlation_id=change_id,
        detail={"from": current, "to": target},
    )
    return {"due": True, "current": current, "target": target}


def _new_value(slot: str) -> None:
    config = settings()
    client = aws.regional_client("secretsmanager", config.origin_secret_region)
    value = client.get_random_password(PasswordLength=SECRET_LENGTH, ExcludePunctuation=True, IncludeSpace=False)
    client.put_secret_value(SecretId=origin_secret(config.prefix, slot), SecretString=value["RandomPassword"])


def _waf() -> Any:
    return aws.regional_client("wafv2", settings().origin_secret_region)


def _rule_group() -> tuple[str, str]:
    """The id and name of the stage's origin rule group."""
    name = origin_rule_group(settings().prefix)
    client, marker = _waf(), None
    while True:
        page = client.list_rule_groups(Scope=SCOPE, **({"NextMarker": marker} if marker else {}))
        for group in page.get("RuleGroups", []):
            if group["Name"] == name:
                return str(group["Id"]), name
        if not (marker := page.get("NextMarker")) or not page.get("RuleGroups"):
            raise LookupError(f"no WAF rule group {name}")


def admit_values(values: Mapping[str, SecretStr]) -> None:
    """Make ``fast-origin-required`` admit exactly ``values`` (by slot, in :data:`ORIGIN_SECRET_SLOTS` order)."""
    group_id, name = _rule_group()
    client = _waf()
    found = client.get_rule_group(Name=name, Scope=SCOPE, Id=group_id)
    group = found["RuleGroup"]
    [rule] = [r for r in group["Rules"] if r["Name"] == RULE]
    matches = rule["Statement"]["AndStatement"]["Statements"][1]["NotStatement"]["Statement"]["OrStatement"][
        "Statements"
    ]
    if len(matches) != len(ORIGIN_SECRET_SLOTS):
        raise ValueError(f"{name}/{RULE} matches {len(matches)} values, not {len(ORIGIN_SECRET_SLOTS)}")
    for match, slot in zip(matches, ORIGIN_SECRET_SLOTS, strict=True):
        header = match["ByteMatchStatement"]["FieldToMatch"]["SingleHeader"]["Name"]
        if header.lower() != ORIGIN_HEADER.lower():
            raise ValueError(f"{name}/{RULE} matches {header}, not {ORIGIN_HEADER}")
        match["ByteMatchStatement"]["SearchString"] = values[slot].get_secret_value().encode()
    client.update_rule_group(
        Name=name,
        Scope=SCOPE,
        Id=group_id,
        Description=group.get("Description", ""),
        Rules=group["Rules"],
        VisibilityConfig=group["VisibilityConfig"],
        LockToken=found["LockToken"],
    )


def admit(target: str, change_id: str) -> dict[str, str]:
    """A new value for ``target``, admitted at the WAF beside the current one."""
    _new_value(target)
    admit_values({slot: store.secret(slot) for slot in ORIGIN_SECRET_SLOTS})
    apply.ledger(
        names.ROTATION_CHAIN, "rotation.admit", f"slot-{target}", correlation_id=change_id, detail={"slot": target}
    )
    return {"admitted": target}


def probe(target: str) -> dict[str, Any]:
    """A probe POST with ``target``'s value through the demand CloudFront is not blocked (403)."""
    config = settings()
    config.require("pbs_origin")
    response = httpx.post(
        f"{config.pbs_origin.rstrip('/')}/openrtb2/auction?evid=0&upid=rotation&dur=30030",
        json={"ext": {"prebid": {"storedrequest": {"id": "fast-pod-30s"}}}},
        headers={
            "Content-Type": "application/json",
            ORIGIN_HEADER: store.secret(target).get_secret_value(),
            "X-Fast-Probe": "1",
            "X-Fast-Sid": "rotation-probe",
        },
        timeout=10,
    )
    if response.status_code == 403:
        raise NotAdmittedError(f"slot {target} is not admitted yet")
    return {"status": response.status_code}


def _origin_functions(bundle: Bundle, sibling: str) -> list[str]:
    """The stems of the Functions ``sibling`` runs in ``bundle`` that carry the origin secret (``DemandOrigin``)."""
    found = next((s for s in bundle.siblings if s.name == sibling), None)
    return [
        f.function_id
        for f in (mapped_functions(bundle, [found]) if found else [])
        if f.http_request_configuration is not None and f.http_request_configuration.demand_origin
    ]


def origin_trees(states: Mapping[str, ChannelState], change_id: str) -> dict[int, list[str]]:
    """Each release a sibling keeps (its current tree and its history) whose tree for that sibling carries the origin
    secret, with those siblings. A release this rotation created (its ``change_id``) carries the new value already."""
    found: dict[int, set[str]] = {}
    for state in states.values():
        for sibling, history in sorted(state.history.items()):
            for release in history:
                status = store.status(release)
                if status is None or status.change_id == change_id:
                    continue
                if _origin_functions(store.bundle(status.bundle_key), sibling):
                    found.setdefault(release, set()).add(sibling)
    return {release: sorted(siblings) for release, siblings in sorted(found.items())}


def _renumbered(
    state: ChannelState, moved: Mapping[tuple[str, int], int], change_id: str, at: datetime
) -> ChannelState:
    """``state`` with each (sibling, release) of ``moved`` on its new release, in its record and its history."""
    siblings = {
        name: record.model_copy(update={"release": moved[(name, record.release)], "change_id": change_id, "at": at})
        if (name, record.release) in moved
        else record
        for name, record in state.siblings.items()
    }
    history = {name: tuple(moved.get((name, r), r) for r in kept) for name, kept in state.history.items()}
    return state.model_copy(update={"siblings": siblings, "history": history})


def recreate(target: str, change_id: str) -> dict[str, str]:
    """Each tree a sibling keeps that carries the origin secret, created again under a new release with ``target``'s
    value (Functions are immutable), and the sibling's record and history moved to it; the plan of a channel whose
    stable sibling moved is rewritten. Run again, it allocates and creates nothing twice. Returns the moves, as
    ``<sibling>@<old release>`` -> new release."""
    config = settings()
    config.require("release_channels")
    channels = list(config.release_channels)
    moved: dict[tuple[str, int], int] = {}
    for release, siblings in origin_trees(store.channels(channels), change_id).items():
        status = store.status(release)
        assert status is not None  # origin_trees read it
        new = store.allocate(f"rotation#{change_id}#{release_tag(release)}")
        store.put_status(
            status.model_copy(update={"release": new, "change_id": change_id, "at": _now(), "recreated_from": release})
        )
        apply.put_functions(status.bundle_key, new, change_id, siblings, slot=target)
        moved |= {(sibling, release): new for sibling in siblings}
    for channel in channels:
        state, version = store.channel(channel)
        if state is None or (after := _renumbered(state, moved, change_id, _now())) == state:
            continue
        store.put_channel(after, version=version)
        if state.stable is not None and after.siblings[state.stable] != state.siblings[state.stable]:
            status = store.status(after.siblings[state.stable].release)
            assert status is not None
            bundle = store.bundle(status.bundle_key)
            apply.write_plan(next(c for c in bundle.channels if c.name == channel), after, {}, _now())
    return {f"{sibling}@{release_tag(old)}": release_tag(new) for (sibling, old), new in sorted(moved.items())}


def kept_origin_functions(slot: str) -> list[str]:
    """The Functions a sibling keeps (its current tree and its history) that carry ``slot``'s value
    (``fast:origin-slot``)."""
    config = settings()
    config.require("prefix", "release_channels")
    kept: set[str] = set()
    for state in store.channels(list(config.release_channels)).values():
        for sibling, history in state.history.items():
            for release in history:
                if (status := store.status(release)) is not None:
                    bundle = store.bundle(status.bundle_key)
                    kept |= {released_id(stem, release) for stem in _origin_functions(bundle, sibling)}
    if not kept:  # no kept tree carries the secret itself (no ad-server route): nothing to list
        return []
    return sorted(
        f["FunctionId"]
        for f in reaper.functions(config.prefix)
        if (f.get("Tags") or {}).get(apply.ORIGIN_SLOT_TAG) == slot
        and RELEASE_SUFFIX.search(f["FunctionId"])
        and f["FunctionId"] in kept
    )


def switch(target: str, change_id: str) -> dict[str, Any]:
    """Every tree that carries the origin secret is created again with ``target``'s value (:func:`recreate`), then
    every configured sibling sends ``target``'s value; the slot is recorded."""
    config = settings()
    config.require("prefix", "release_channels")
    secret = store.secret(target)
    recreated = recreate(target, change_id)
    switched: list[str] = []
    for state in store.channels(list(config.release_channels)).values():
        for name, record in sorted(state.siblings.items()):
            status = store.status(record.release)
            if status is None:
                raise ValueError(f"{name}: no record of release {record.release}")
            sibling = next(s for s in store.bundle(status.bundle_key).siblings if s.name == name)
            put, _ = render(
                sibling.playback,
                prefix=config.prefix,
                release=record.release,
                change_id=record.change_id,
                origin_secret=secret,
            )
            require_valid(check_rendered(put, release=record.release, secrets=[secret]))
            aws.client("mediatailor").put_playback_configuration(**put)
            switched.append(name)
    store.put_origin_slot(target)
    apply.ledger(
        names.ROTATION_CHAIN,
        "rotation.switch",
        f"slot-{target}",
        correlation_id=change_id,
        detail={"slot": target, "siblings": switched, **({"recreated": recreated} if recreated else {})},
    )
    return {"switched": switched, "at": _now().timestamp()}


def verify(siblings: list[str], since: float) -> dict[str, Any]:
    """Since the switch, the siblings' ad requests were answered and a break was filled, when a channel is on air."""
    config = settings()
    config.require("prefix", "release_channels")
    running = [c for c in config.release_channels if promote.on_air(c)]
    if not running:
        return {"verified": False, "reason": "no monetized channel on air: the probe proved the value admitted"}
    quoted = ", ".join(f"'{s}'" for s in siblings)
    query = VERIFY_QUERY.format(siblings=quoted, since_ms=int(since * 1000))
    rows = smoke.insights(names.log_group(config.prefix), query, since, _now().timestamp())
    counts = {row["eventType"]: int(row["n"]) for row in rows}
    if refused := sum(int(float(row.get("refused") or 0)) for row in rows):
        raise RefusedError(f"{refused} ad requests refused by the origin gate (403) since the switch")
    if not counts.get("MAKING_ADS_REQUEST") or not counts.get("FILLED_AVAIL"):
        raise NotVerifiedError(f"no filled break since the switch yet ({counts})")
    return {"verified": True, "on_air": running, "counts": counts}


def retire(current: str, change_id: str) -> dict[str, str]:
    """``current`` (the old value) gets a value nobody has seen, admitted instead of it; the lock is given back.

    Raises:
        RefusedError: a Function a sibling keeps still carries ``current``'s value; retiring it would refuse that
            tree's requests at the origin. Both values stay admitted.
    """
    if carrying := kept_origin_functions(current):
        raise RefusedError(f"{len(carrying)} Functions a sibling keeps still carry slot {current}: {carrying[:5]}")
    _new_value(current)
    admit_values({slot: store.secret(slot) for slot in ORIGIN_SECRET_SLOTS})
    apply.ledger(
        names.ROTATION_CHAIN, "rotation.retire", f"slot-{current}", correlation_id=change_id, detail={"slot": current}
    )
    store.unlock(ROTATION, owner=change_id)
    return {"retired": current}


def fail(change_id: str | None, error: Mapping[str, Any]) -> dict[str, bool]:
    """Ledger the failure and give the lock back (both values the siblings may send stay admitted)."""
    apply.ledger(
        names.ROTATION_CHAIN,
        "rotation.fail",
        "origin",
        correlation_id=change_id or "rotation",
        detail={"error": str(error.get("Error", ""))[:200], "cause": str(error.get("Cause", ""))[:500]},
    )
    if change_id:
        store.unlock(ROTATION, owner=change_id)  # only its own: another rotation's lock stays
    return {"failed": True}


STEPS: Final[dict[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "begin": lambda e: begin(e["change_id"], scheduled=e.get("reason") == "scheduled"),
    "admit": lambda e: admit(e["target"], e["change_id"]),
    "probe": lambda e: probe(e["target"]),
    "switch": lambda e: switch(e["target"], e["change_id"]),
    "verify": lambda e: verify(e["siblings"], e["since"]),
    "retire": lambda e: retire(e["current"], e["change_id"]),
    "fail": lambda e: fail(e.get("change_id"), e.get("error") or {}),
}


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """One rotation step (``event["step"]``)."""
    logger.set_correlation_id(event.get("change_id"))
    if (step := STEPS.get(event["step"])) is None:
        raise ValueError(f"unknown step {event['step']!r}")
    return step(event)
