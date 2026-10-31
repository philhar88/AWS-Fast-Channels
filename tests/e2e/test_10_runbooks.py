"""The ops module's runbooks and probe canary, on stages that enable it (skipped otherwise).

- Every runbook's dry-run path runs and appends its ledger record (chain ``runbook/<id>``).
- Runbook R13 starts from a ``Channel Alert``: a ``ListAlerts`` item as the poller publishes it, put on the bus.
- The probe canary, which the census started and stopped with the channel, passed every run; started while no
  channel runs, it passes too (skipped).

Runs after the channel tests, which ran and stopped the channel.
"""

import json
import time
from datetime import UTC, datetime
from typing import Any, Final

import pytest
from _stack import Module, Plane, Stage
from harness import wait_for

pytestmark = pytest.mark.e2e

EXECUTION_TIMEOUT_S: Final = 180
CANARY_TIMEOUT_S: Final = 420
RUNBOOKS: Final = ("r7", "r9", "r12", "r13", "r14", "r15", "r16", "r17")


@pytest.fixture(scope="module")
def ops(stage: Stage) -> Stage:
    if stage.status(Module.OPS) is None:
        pytest.skip(f"stage {stage.prefix} has no Ops stack (modules.ops.enabled is false)")
    return stage


def _alarm(name: str) -> dict[str, Any]:
    return {
        "source": "aws.cloudwatch",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {"alarmName": name, "state": {"value": "ALARM"}},
    }


def _channel_alert(prefix: str, modified: str, correlation: str) -> dict[str, Any]:
    """A Channel Assembly ``ListAlerts`` item, as ``ops.list_alerts`` publishes it."""
    return {
        "channel": f"{prefix}-demo",
        "code": "PROGRAM_ALERT",
        "category": "SCHEDULING_ERROR",
        "message": "The program e2e-program contained in this channel has the following alert: MISSING_SOURCE",
        "resource_arn": f"arn:aws:mediatailor:us-east-1:123456789012:channel/{prefix}-demo",
        "last_modified": modified,
        "correlation_id": correlation,
    }


def _triggers(ops: Stage, run: str) -> dict[str, dict[str, Any]]:
    now = datetime.now(UTC)
    millis = int(now.timestamp() * 1000)
    prefix = ops.prefix
    return {
        "r7": _alarm(f"{prefix}-account-ad-insertion-requests"),
        "r9": _alarm(f"{prefix}-demo-getmanifest-errors"),
        "r12": _alarm(f"{prefix}-probe-off-air"),
        "r13": {"detail-type": "Channel Alert", "detail": _channel_alert(prefix, now.isoformat(), run)},
        "r14": {
            "detail-type": "Asset Failed",
            "detail": {
                "asset_id": f"e2e-{run}",
                "kind": "content",
                "ladder_version": "standard@843ed1d05a",
                "error": "MediaConvertError",
                "cause": "e2e: a synthetic ingest failure",
                "correlation_id": run,
            },
        },
        "r15": {
            "detail-type": "Schedule Published",
            "detail": {
                "channel": f"{prefix}-demo",
                "plan_id": f"e2e-{run}",
                "programs": 0,
                "start_ms": millis,
                "end_ms": millis + 3_600_000,
                "correlation_id": run,
            },
        },
        "r16": {"scheduled_time": now.isoformat()},
        "r17": _alarm(f"{prefix}-cdn-5xx"),
    }


def _ledger_entry(ops: Stage, chain: str, record_id: str) -> dict[str, Any] | None:
    table = ops.session.resource("dynamodb", region_name=ops.region).Table(ops.output(Plane.DATA, "LedgerTableName"))
    item: dict[str, Any] | None = table.get_item(Key={"pk": chain, "sk": f"E#{record_id}"}).get("Item")
    return item


def test_every_runbook_dry_run_writes_a_ledger_record(ops: Stage, run_id: str) -> None:
    sfn = ops.session.client("stepfunctions", region_name=ops.region)
    account = ops.session.client("sts", region_name=ops.region).get_caller_identity()["Account"]
    run = f"{run_id}-{int(time.time())}"[-40:]
    executions = {
        runbook: sfn.start_execution(
            stateMachineArn=f"arn:aws:states:{ops.region}:{account}:stateMachine:{ops.prefix}-runbook-{runbook}",
            name=f"e2e-{runbook}-{run}"[:80],
            input=json.dumps(trigger),
        )["executionArn"]
        for runbook, trigger in _triggers(ops, run).items()
    }

    def finished() -> dict[str, dict[str, Any]] | None:
        described: dict[str, dict[str, Any]] = {
            r: dict(sfn.describe_execution(executionArn=arn)) for r, arn in executions.items()
        }
        return described if all(d["status"] != "RUNNING" for d in described.values()) else None

    described = wait_for(finished, timeout=EXECUTION_TIMEOUT_S, interval=10, what="the runbook executions")

    assert {r: d["status"] for r, d in described.items()} == dict.fromkeys(RUNBOOKS, "SUCCEEDED")
    for runbook, execution in described.items():
        output = json.loads(execution["output"])
        assert output["mode"] == "dry_run", (runbook, output)
        item = _ledger_entry(ops, f"runbook/{runbook}", output["record_id"])
        assert item is not None, (runbook, output)
        assert (item["action"], item["correlation_id"]) == ("runbook.dry_run", execution["name"])
    assert json.loads(described["r13"]["output"])["actions"] == ["page"]  # no evergreen block yet
    assert json.loads(described["r14"]["output"])["actions"] == ["ingest.retry"]


def test_r13_starts_from_a_list_alerts_fixture(ops: Stage, run_id: str) -> None:
    events = ops.session.client("events", region_name=ops.region)
    table = ops.session.resource("dynamodb", region_name=ops.region).Table(ops.output(Plane.DATA, "LedgerTableName"))
    modified = datetime.now(UTC).isoformat()
    detail = _channel_alert(ops.prefix, modified, f"e2e-{run_id}")
    response = events.put_events(
        Entries=[
            {
                "Source": f"fast-channels.{ops.prefix}",
                "DetailType": "Channel Alert",
                "Detail": json.dumps(detail),
                "EventBusName": ops.output(Plane.MEDIA, "EventBusName"),
            }
        ]
    )
    assert response["FailedEntryCount"] == 0

    def recorded() -> dict[str, Any] | None:
        items = table.query(
            KeyConditionExpression="pk = :chain AND begins_with(sk, :entries)",
            ExpressionAttributeValues={":chain": "runbook/r13", ":entries": "E#"},
            ScanIndexForward=False,
            Limit=20,
        )["Items"]
        return next((i for i in items if json.loads(str(i["detail"]))["facts"].get("last_modified") == modified), None)

    item = wait_for(recorded, timeout=EXECUTION_TIMEOUT_S, interval=10, what="runbook R13's record for the alert")

    assert item["subject"] == f"{ops.prefix}-demo"
    assert json.loads(item["detail"])["actions"] == ["page"]


def test_the_probe_passed_while_the_channel_ran_and_passes_skipped_after(
    ops: Stage, observation: object, published: dict[str, Any]
) -> None:
    synthetics = ops.session.client("synthetics", region_name=ops.region)
    name = f"{ops.prefix}-probe"
    since = datetime.fromisoformat(published["programs"][0]["start"])

    def runs() -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = synthetics.get_canary_runs(Name=name, MaxResults=100)["CanaryRuns"]
        return [r for r in found if r["Timeline"]["Started"] >= since]

    during = runs()
    assert all(r["Status"]["State"] == "PASSED" for r in during), [r["Status"] for r in during]

    after = datetime.now(UTC)
    if synthetics.get_canary(Name=name)["Canary"]["Status"]["State"] != "RUNNING":
        synthetics.start_canary(Name=name)
    try:

        def skipped_run() -> dict[str, Any] | None:
            done = [r for r in runs() if r["Timeline"]["Started"] >= after and r["Status"]["State"] != "RUNNING"]
            return done[0] if done else None

        run = wait_for(skipped_run, timeout=CANARY_TIMEOUT_S, interval=20, what=f"a run of {name} with no channel")
    finally:
        synthetics.stop_canary(Name=name)

    assert run["Status"]["State"] == "PASSED", run["Status"]
