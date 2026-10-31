"""The ops module's runbooks R7, R9 and R12-R17 (``core.runbooks``): gather the facts, decide, append the ledger record.

Each runbook's state machine (``<prefix>-runbook-<id>``) invokes :func:`handler` with
``{"runbook": "r13", "trigger": <what started it>, "execution": <execution name>}``. The trigger is the EventBridge
event (an alarm state change, ``Channel Alert``, ``Asset Failed``, ``Schedule Published``) or, for R16, the
schedule's ``{"scheduled_time": ...}``. The function reads the kill switches from the AppConfig profile ``flags``,
gathers what the runbook needs, decides (``core.runbooks``) and appends one ledger entry to the chain
``runbook/<id>`` with the decision as its detail (the execution name is its correlation id). Until ADR-0023 is
accepted every runbook only records what it would do (``mode`` ``dry_run``).
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities import parameters
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from fast_channels.core.events import AssetFailed, ChannelAlert, SchedulePublished
from fast_channels.core.flags import PROFILE, Flags
from fast_channels.core.ledger import Json, LedgerKind, entry_from_item
from fast_channels.core.runbooks import (
    REPEAT_WINDOW,
    RIGHTS_LOOKAHEAD,
    Action,
    Decision,
    Runbook,
    ca_alert_actions,
    cdn_actions,
    decide,
    ingest_failure_actions,
    missing_source_actions,
    off_air_actions,
    qoe_actions,
    quota_actions,
    rights_actions,
)
from fast_channels.functions import aws
from fast_channels.functions.ops.ledger import append, entry
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import library_store

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from aws_lambda_powertools.utilities.typing import LambdaContext

    from fast_channels.core.flags import OpsSwitches

PRINCIPAL: Final = "fast-channels/runbooks"
FLAGS_MAX_AGE_S: Final = 30
LEDGER_LOOKBACK: Final = 50
"""Entries of a runbook's chain read back to find repeats (R13) and earlier attempts (R14)."""

logger = Logger()
metrics = Metrics()


def switches() -> OpsSwitches:
    """The ``ops`` kill switches from the AppConfig profile ``flags`` (defaults if it cannot be read)."""
    config = settings()
    try:
        data = parameters.get_app_config(
            name=PROFILE,
            environment=config.appconfig_environment,
            application=config.appconfig_application,
            transform="json",
            max_age=FLAGS_MAX_AGE_S,
        )
    except parameters.exceptions.GetParameterError:
        logger.exception("flags unreadable; using the defaults (every runbook in dry run)")
        return Flags().ops
    return Flags.from_simplified(data if isinstance(data, dict) else {}).ops


def recent(runbook: Runbook) -> list[dict[str, Any]]:
    """Details and times of the runbook's latest ledger entries, newest first."""
    config = settings()
    config.require("ledger_table")
    items = (
        aws.table(config.ledger_table)
        .query(
            KeyConditionExpression=Key("pk").eq(runbook.chain_id) & Key("sk").begins_with("E#"),
            ScanIndexForward=False,
            Limit=LEDGER_LOOKBACK,
        )
        .get("Items", [])
    )
    found = []
    for item in items:
        try:
            record = entry_from_item({key: str(value) for key, value in item.items()})
        except ValueError:  # pydantic's ValidationError is a ValueError
            continue
        found.append({"at": record.at, "detail": record.detail})
    return found


def _alarm(trigger: Mapping[str, Any]) -> tuple[str, str]:
    detail = trigger.get("detail") or {}
    return str(detail.get("alarmName", "unknown alarm")), str((detail.get("state") or {}).get("value", "UNKNOWN"))


def _now() -> datetime:
    return datetime.now(UTC)


# --- one gatherer per runbook ----------------------------------------------------------------------------------------


def r7(trigger: Mapping[str, Any], ops: OpsSwitches) -> Decision:
    alarm, state = _alarm(trigger)
    actions, reason = quota_actions(state)
    return decide(Runbook.R7, ops, actions, subject=alarm, reason=reason, facts={"alarm": alarm, "state": state})


def r9(trigger: Mapping[str, Any], ops: OpsSwitches) -> Decision:
    alarm, state = _alarm(trigger)
    actions, reason = qoe_actions(state)
    return decide(Runbook.R9, ops, actions, subject=alarm, reason=reason, facts={"alarm": alarm, "state": state})


def r12(trigger: Mapping[str, Any], ops: OpsSwitches) -> Decision:
    config = settings()
    alarm, state = _alarm(trigger)
    client = aws.client("mediatailor")
    channels: dict[str, str] = {}
    for name in config.iac_channels:
        try:
            channels[name] = client.describe_channel(ChannelName=name)["ChannelState"]
        except ClientError as error:
            channels[name] = error.response.get("Error", {}).get("Code", "ERROR")
    actions, reason = off_air_actions(channels, config.always_on) if state == "ALARM" else ([Action.NONE], state)
    facts: dict[str, Json] = {"alarm": alarm, "state": state, "channels": dict(channels)}
    return decide(Runbook.R12, ops, actions, subject=",".join(sorted(channels)) or alarm, reason=reason, facts=facts)


def r13(trigger: Mapping[str, Any], ops: OpsSwitches) -> Decision:
    alert = ChannelAlert.model_validate(trigger["detail"])
    since = _now() - REPEAT_WINDOW
    repeated = any(
        entry_["at"] >= since
        and entry_["detail"].get("facts", {}).get("code") == alert.code
        and entry_["detail"].get("facts", {}).get("resource_arn") == alert.resource_arn
        and entry_["detail"].get("facts", {}).get("last_modified") != alert.last_modified
        for entry_ in recent(Runbook.R13)
    )
    actions, reason = ca_alert_actions(
        alert.category, alert.code, repeated=repeated, evergreen=settings().evergreen_block or None
    )
    facts: dict[str, Json] = {
        "code": alert.code,
        "category": alert.category,
        "message": alert.message,
        "resource_arn": alert.resource_arn,
        "last_modified": alert.last_modified,
        "repeated": repeated,
    }
    return decide(Runbook.R13, ops, actions, subject=alert.channel, reason=reason, facts=facts)


def r14(trigger: Mapping[str, Any], ops: OpsSwitches) -> Decision:
    failed = AssetFailed.model_validate(trigger["detail"])
    earlier = sum(1 for e in recent(Runbook.R14) if e["detail"].get("facts", {}).get("asset_id") == failed.asset_id)
    actions, reason = ingest_failure_actions(earlier + 1)
    facts: dict[str, Json] = {
        "asset_id": failed.asset_id,
        "error": failed.error,
        "cause": failed.cause[:500],
        "attempts": earlier + 1,
        "ingest_correlation_id": failed.correlation_id,
    }
    return decide(Runbook.R14, ops, actions, subject=failed.asset_id, reason=reason, facts=facts)


def _vod_source_exists(program: library_store.ProgramRecord) -> bool:
    location = program.source_location or settings().source_location
    try:
        aws.client("mediatailor").describe_vod_source(SourceLocationName=location, VodSourceName=program.vod_source)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NotFoundException", "BadRequestException"}:
            return False
        raise
    return True


def r15(trigger: Mapping[str, Any], ops: OpsSwitches) -> Decision:
    published = SchedulePublished.model_validate(trigger["detail"])
    now = _now()
    findings: list[Json] = []
    actions: list[Action] = []
    for program in library_store.programs(published.channel, start_ms=published.start_ms, end_ms=published.end_ms):
        if _vod_source_exists(program):
            continue
        starts_at = datetime.fromtimestamp(program.start_ms / 1000, UTC)
        would, reason = missing_source_actions(starts_at, now, evergreen=settings().evergreen_block or None)
        findings.append(
            {
                "program": program.name,
                "vod_source": program.vod_source,
                "actions": [a.value for a in would],
                "reason": reason,
            }
        )
        actions += [a for a in would if a not in actions]
    reason = f"{len(findings)} program(s) with a missing VOD source" if findings else "every VOD source is there"
    facts: dict[str, Json] = {"plan_id": published.plan_id, "programs": findings}
    return decide(Runbook.R15, ops, actions or [Action.NONE], subject=published.channel, reason=reason, facts=facts)


def _available_until(content_id: str | None) -> datetime | None:
    if not content_id:
        return None
    config = settings()
    config.require("catalog_table")
    item = aws.table(config.catalog_table).get_item(Key={"pk": f"CONTENT#{content_id}", "sk": "META"}).get("Item")
    value = (item or {}).get("available_until")
    return datetime.fromisoformat(str(value)) if value else None


def r16(trigger: Mapping[str, Any], ops: OpsSwitches) -> Decision:
    now = datetime.fromisoformat(str(trigger.get("scheduled_time") or _now().isoformat())).astimezone(UTC)
    start_ms, end_ms = int(now.timestamp() * 1000), int((now + RIGHTS_LOOKAHEAD).timestamp() * 1000)
    findings: list[Json] = []
    checked = 0
    for channel in settings().iac_channels:
        for program in library_store.programs(channel, start_ms=start_ms, end_ms=end_ms):
            checked += 1
            until = _available_until(program.content_id)
            would, reason = rights_actions(datetime.fromtimestamp(program.end_ms / 1000, UTC), until)
            if would != [Action.NONE]:
                findings.append(
                    {"channel": channel, "program": program.name, "actions": [a.value for a in would], "reason": reason}
                )
    actions = [Action.REPLACE_BY_AGENT] if findings else [Action.NONE]
    reason = (
        f"{len(findings)} of {checked} program(s) end outside their rights"
        if findings
        else (f"{checked} program(s) in the next {RIGHTS_LOOKAHEAD} are inside their rights")
    )
    facts: dict[str, Json] = {"window_start": now.isoformat(), "programs": findings}
    return decide(Runbook.R16, ops, actions, subject=settings().prefix or "stage", reason=reason, facts=facts)


def r17(trigger: Mapping[str, Any], ops: OpsSwitches) -> Decision:
    alarm, state = _alarm(trigger)
    actions, reason = cdn_actions(state)
    return decide(Runbook.R17, ops, actions, subject=alarm, reason=reason, facts={"alarm": alarm, "state": state})


GATHERERS: Final[dict[Runbook, Callable[[Mapping[str, Any], OpsSwitches], Decision]]] = {
    Runbook.R7: r7,
    Runbook.R9: r9,
    Runbook.R12: r12,
    Runbook.R13: r13,
    Runbook.R14: r14,
    Runbook.R15: r15,
    Runbook.R16: r16,
    Runbook.R17: r17,
}


@metrics.log_metrics
@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Decide what the runbook does about its trigger and append the decision to its ledger chain."""
    runbook = Runbook(event["runbook"])
    execution = str(event.get("execution") or context.aws_request_id)
    logger.set_correlation_id(execution)
    logger.append_keys(runbook=runbook.value)
    decision = GATHERERS[runbook](event.get("trigger") or {}, switches())
    record = append(
        entry(
            runbook.chain_id,
            LedgerKind.RUNBOOK,
            principal=PRINCIPAL,
            action=f"runbook.{decision.mode.value}",
            subject=decision.subject,
            correlation_id=execution,
            detail=decision.detail(),
        )
    )
    logger.info("runbook decided", extra={"decision": decision.detail(), "record_id": record.record_id})
    metrics.add_dimension(name="runbook", value=runbook.value)
    metrics.add_metric(name="RunbookDecisions", unit=MetricUnit.Count, value=1)
    return {**decision.detail(), "record_id": record.record_id, "chain_id": runbook.chain_id}
