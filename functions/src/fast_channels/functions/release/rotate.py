"""Origin-secret rotation (DESIGN §8.6): the two-value overlap, one step per invocation of the rotation state machine.

The siblings send one of the two origin secrets (``store.origin_slot()``) in ``X-Fast-Origin``, and the WAF rule
group ``<prefix>-demand-origin`` (rule ``fast-origin-required``) admits either. A rotation runs every
``modules.release.secret_rotation_days`` days, or by hand on a suspected exposure:

1. ``begin``: hold the Release Controller's lock (no release applies meanwhile) and choose the other slot;
2. ``admit``: give the other slot a new value and admit it at the WAF, beside the current one;
3. ``probe``: once the WAF change reached CloudFront, a probe POST with the new value is not blocked;
4. ``switch``: every configured sibling sends the new value (``PutPlaybackConfiguration`` of its own release, the
   call class of a remap: live sessions use it at their next break), and the slot is recorded;
5. ``verify``: while a monetized channel is on air, its siblings' ad requests since the switch were answered and a
   break was filled;
6. ``retire``: the old slot gets a value nobody has seen, which the WAF admits instead of the old one.

The lock is then given back. A failure at any step gives the lock back and leaves admitted every value a sibling
may send: the rotation can be run again. No secret value is ever logged or ledgered.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
from aws_lambda_powertools import Logger

from fast_channels.core.demand import ORIGIN_HEADER, ORIGIN_SECRET_SLOTS, origin_rule_group, origin_secret
from fast_channels.core.monetization import names
from fast_channels.core.monetization.playback import render
from fast_channels.core.monetization.validators import check_rendered, require_valid
from fast_channels.functions import aws
from fast_channels.functions.release import apply, promote, smoke, store
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from pydantic import SecretStr

logger = Logger()
ROTATION: Final = store.ROTATION
SCOPE: Final = "CLOUDFRONT"
RULE: Final = "fast-origin-required"
SECRET_LENGTH: Final = 48
VERIFY_QUERY: Final = (
    "filter originId in [{siblings}] and eventType in ['MAKING_ADS_REQUEST', 'FILLED_AVAIL', "
    "'ERROR_ADS_INVALID_RESPONSE']\n| stats count(*) as n by eventType"
)


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


def begin(change_id: str) -> dict[str, str]:
    """Hold the lock (``store.ReleaseBusyError`` while a release holds it) and choose the slot to rotate to."""
    store.lock(ROTATION)
    current = store.origin_slot()
    target = _other(current)
    apply.ledger(
        names.ROTATION_CHAIN,
        "rotation.begin",
        f"slot-{target}",
        correlation_id=change_id,
        detail={"from": current, "to": target},
    )
    return {"current": current, "target": target}


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


def switch(target: str, change_id: str) -> dict[str, Any]:
    """Every configured sibling sends ``target``'s value; the slot is recorded."""
    config = settings()
    config.require("prefix", "release_channels")
    secret = store.secret(target)
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
        detail={"slot": target, "siblings": switched},
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
    rows = smoke.insights(
        names.log_group(config.prefix), VERIFY_QUERY.format(siblings=quoted), since, _now().timestamp()
    )
    counts = {row["eventType"]: int(row["n"]) for row in rows}
    if counts.get("ERROR_ADS_INVALID_RESPONSE"):
        raise RefusedError(f"{counts['ERROR_ADS_INVALID_RESPONSE']} ad responses refused since the switch")
    if not counts.get("MAKING_ADS_REQUEST") or not counts.get("FILLED_AVAIL"):
        raise NotVerifiedError(f"no filled break since the switch yet ({counts})")
    return {"verified": True, "on_air": running, "counts": counts}


def retire(current: str, change_id: str) -> dict[str, str]:
    """``current`` (the old value) gets a value nobody has seen, admitted instead of it; the lock is given back."""
    _new_value(current)
    admit_values({slot: store.secret(slot) for slot in ORIGIN_SECRET_SLOTS})
    apply.ledger(
        names.ROTATION_CHAIN, "rotation.retire", f"slot-{current}", correlation_id=change_id, detail={"slot": current}
    )
    store.unlock(ROTATION)
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
    store.unlock(ROTATION)
    return {"failed": True}


STEPS: Final[dict[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "begin": lambda e: begin(e["change_id"]),
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
