"""The ops module, on stages that enable it (skipped otherwise): its stack outputs, the lake's MediaTailor log copies
without the excluded fields, ``v_breaks`` returning the breaks this run aired, the as-run reconciler marking the
breaks of the as-run log ``AIRED``, and ``Census Changed`` events for the channel's start and stop.

Runs after the channel tests, which aired the breaks and stopped the channel. The log copies reach S3 a few minutes
after the events, and the census runs every five minutes, so these tests wait up to about ten minutes.
"""

import gzip
import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import pytest
from _stack import MODULE_OUTPUT_KEYS, Module, Plane, Stage
from boto3.dynamodb.conditions import Key
from harness import wait_for

from fast_channels.core.timeline import scte_event_id
from fast_channels.functions.tools.library_store import GSI1

if TYPE_CHECKING:
    from harness import Channel, Observation
    from mypy_boto3_dynamodb.service_resource import Table

pytestmark = pytest.mark.e2e

EXCLUDED: Final = {
    "ads": {"requestHeaders", "rawAdsRequest"},
    "manifest": {"mediaTailorPath", "input", "http"},
}
"""Fields the lake's copies never hold (``infra.constructs.ops.lake``)."""
DELIVERY_TIMEOUT_S: Final = 900
CENSUS_TIMEOUT_S: Final = 420
AS_RUN_TIMEOUT_S: Final = 300
AS_RUN_LOG_GROUP: Final = "MediaTailor/Channel/AsRunLog"


@pytest.fixture(scope="module")
def ops(stage: Stage) -> Stage:
    if stage.status(Module.OPS) is None:
        pytest.skip(f"stage {stage.prefix} has no Ops stack (modules.ops.enabled is false)")
    return stage


@pytest.fixture(scope="module")
def aired_breaks(published: dict[str, Any], observation: Observation) -> dict[int, str]:
    """SCTE-35 event id -> program of every scheduled break whose ad marker the SSAI session saw.

    Only those: the session stops watching once every variant has shown ads, and MediaTailor requests ads for a
    break only in a session that plays it, so a break that aired after the session left is in no ADS log. The
    marker's ``DATERANGE`` ID is ``<start epoch ms>-<segmentation type>-<event id in hex>``.
    """
    scheduled = {
        scte_event_id(program["name"], index): program["name"]
        for program in published["programs"]
        for index, _ in enumerate(program["breaks"])
    }
    seen = {
        int(hex_id, 16)
        for markers in observation.splices.values()
        for marker in markers
        if re.fullmatch(r"[0-9A-Fa-f]+", hex_id := marker.rsplit("-", 1)[-1])
    }
    aired = {evid: program for evid, program in scheduled.items() if evid in seen}
    assert aired, f"the session saw no scheduled break: markers {sorted(seen)}, scheduled {sorted(scheduled)}"
    return aired


def test_ops_stack_outputs(ops: Stage) -> None:
    outputs = ops.outputs(Module.OPS)

    assert not [key for key in MODULE_OUTPUT_KEYS[Module.OPS] if not outputs.get(key)], outputs
    assert ops.output(Plane.DATA, "LakeBucketName")
    assert outputs["LakeDatabase"] == f"{ops.prefix.replace('-', '_')}_lake"


def _copies(ops: Stage, kind: str, since: datetime) -> list[dict[str, Any]]:
    """Every record of the lake's ``kind`` (``ads``, ``manifest``) copies written since ``since``."""
    s3 = ops.session.client("s3", region_name=ops.region)
    bucket = ops.output(Plane.DATA, "LakeBucketName")
    records: list[dict[str, Any]] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"mediatailor/{kind}/"):
        for item in page.get("Contents", []):
            if item["LastModified"] < since:
                continue
            body = s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
            text = (gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body).decode()
            records += [json.loads(line) for line in text.splitlines() if line.strip()]
    return records


def test_log_copies_hold_no_excluded_field(ops: Stage, aired_breaks: dict[int, str]) -> None:
    since = datetime.now(UTC) - timedelta(hours=2)

    def copied() -> list[dict[str, Any]] | None:
        records = _copies(ops, "ads", since)
        requests = [r for r in records if r.get("eventType") == "MAKING_ADS_REQUEST"]
        return (
            records
            if any(f"evid={evid}" in r.get("adsRequestUrl", "") for r in requests for evid in aired_breaks)
            else None
        )

    ads = wait_for(copied, timeout=DELIVERY_TIMEOUT_S, interval=30, what="the run's ADS requests in the lake")
    manifest = _copies(ops, "manifest", since)

    assert not [r for r in ads if EXCLUDED["ads"] & r.keys()]
    assert not [r for r in manifest if EXCLUDED["manifest"] & r.keys()]
    assert {"eventTimestamp", "eventType", "sessionId", "originId"} <= ads[0].keys()


def _athena(ops: Stage, sql: str) -> list[dict[str, str | None]]:
    athena = ops.session.client("athena", region_name=ops.region)
    started = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=ops.output(Module.OPS, "LakeWorkGroup"),
        QueryExecutionContext={"Database": ops.output(Module.OPS, "LakeDatabase")},
    )
    query_id = started["QueryExecutionId"]
    while (state := athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]["Status"])["State"] in {
        "QUEUED",
        "RUNNING",
    }:
        time.sleep(2)
    assert state["State"] == "SUCCEEDED", state
    rows = athena.get_query_results(QueryExecutionId=query_id)["ResultSet"]["Rows"]
    header = [cell.get("VarCharValue") or "" for cell in rows[0]["Data"]]
    return [dict(zip(header, (cell.get("VarCharValue") for cell in row["Data"]), strict=True)) for row in rows[1:]]


def test_v_breaks_returns_every_aired_break_with_its_event_id(
    ops: Stage, aired_breaks: dict[int, str], observation: Observation
) -> None:
    evids = ", ".join(str(evid) for evid in aired_breaks)
    sql = f"SELECT evid, upid, started_at, sessions, ads_requests FROM v_breaks WHERE evid IN ({evids})"

    def found() -> list[dict[str, str | None]] | None:
        rows = _athena(ops, sql)
        return rows if len(rows) == len(aired_breaks) else None

    rows = wait_for(found, timeout=DELIVERY_TIMEOUT_S, interval=60, what=f"v_breaks rows for {sorted(aired_breaks)}")

    assert {int(row["evid"] or 0) for row in rows} == set(aired_breaks)
    assert all(int(row["sessions"] or 0) >= 1 for row in rows), rows


def _break_starts(ops: Stage, channel: str, since: datetime) -> set[tuple[str, str]]:
    """``(programName, timeStamp)`` of every ``AdBreakStart`` of ``channel`` since ``since`` (any output), from the
    as-run log."""
    logs = ops.session.client("logs", region_name=ops.region)
    pages = logs.get_paginator("filter_log_events").paginate(
        logGroupName=AS_RUN_LOG_GROUP,
        logStreamNamePrefix=f"{channel}-",  # one stream per output: <channel>-<source group>
        startTime=int(since.timestamp() * 1000),
        filterPattern=f'{{ $.channelName = "{channel}" && $.eventType = "AdBreakStart" }}',
    )
    entries = [json.loads(event["message"]) for page in pages for event in page.get("events", [])]
    return {(entry["programName"], entry["timeStamp"]) for entry in entries}


def test_the_as_run_reconciler_marks_every_logged_break_aired(
    ops: Stage, library: Table, channel: Channel, published: dict[str, Any], observation: Observation
) -> None:
    """Every ``AdBreakStart`` that Channel Assembly logged for the channel during the run marks its break ``AIRED``
    on the ``PROGRAM`` item, with the event's time as ``aired_at``: from the stage's own as-run subscription or, with
    ``observability.as_run`` ``shared``, through the account's as-run router. The expectation is the as-run log's:
    a break that starts less than about a minute before the channel stops can be missing from it."""
    since = datetime.fromisoformat(published["programs"][0]["start"]) - timedelta(minutes=5)
    starts = wait_for(
        lambda: _break_starts(ops, channel.name, since) or None,
        timeout=AS_RUN_TIMEOUT_S,
        interval=15,
        what=f"AdBreakStart events of {channel.name} in the as-run log",
    )

    def marked() -> set[tuple[str, str]]:
        aired: set[tuple[str, str]] = set()
        for program in {name for name, _ in starts}:
            key = Key("gsi1pk").eq(f"PROGRAM#{program}")
            for item in library.query(IndexName=GSI1, KeyConditionExpression=key)["Items"]:
                breaks = item.get("breaks")
                for brk in breaks if isinstance(breaks, list) else []:
                    if isinstance(brk, dict) and brk.get("status") == "AIRED":
                        aired.add((program, str(brk.get("aired_at"))))
        return aired

    wait_for(
        lambda: starts if starts <= marked() else None,
        timeout=AS_RUN_TIMEOUT_S,
        interval=15,
        what=f"AIRED breaks for the as-run starts {sorted(starts)}",
        state=marked,
    )


def _census_changes(ops: Stage, since: datetime) -> list[dict[str, Any]]:
    logs = ops.session.client("logs", region_name=ops.region)
    events = []
    for page in logs.get_paginator("filter_log_events").paginate(
        logGroupName=f"/aws/vendedlogs/events/{ops.prefix}-fast-channels",
        startTime=int(since.timestamp() * 1000),
        filterPattern='{ $.message_type = "EVENT_INGEST_SUCCESS" && $.details.detail_type = "Census Changed" }',
    ):
        events += [json.loads(json.loads(e["message"])["details"]["event_detail"]) for e in page["events"]]
    return events


def test_census_changed_on_channel_start_and_stop(
    ops: Stage, published: dict[str, Any], observation: Observation
) -> None:
    since = datetime.fromisoformat(published["programs"][0]["start"]) - timedelta(minutes=15)

    def both() -> list[dict[str, Any]] | None:
        changes = _census_changes(ops, since)
        started = any(c["previous"] == 0 and c["running"] >= 1 for c in changes)
        stopped = any(c["previous"] >= 1 and c["running"] == 0 for c in changes)
        return changes if started and stopped else None

    changes = wait_for(both, timeout=CENSUS_TIMEOUT_S, interval=30, what="Census Changed for the start and the stop")

    assert all(c["correlation_id"] for c in changes)
