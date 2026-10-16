#!/usr/bin/env -S uv run python
"""Follow one identifier through every CloudWatch log of a deployment, as a single time-ordered timeline.

    uv run python scripts/trace.py big-buck-bunny-1a2b3c                   # asset id
    uv run python scripts/trace.py seed-sintel-1a2b3c4d --since 6h         # ingest correlation id (execution name)
    uv run python scripts/trace.py 49f0c250-9e3c-4997-a723-3cd95eff9d42    # MediaTailor playback session id
    uv run python scripts/trace.py 20261016T120000-sintel-1a2b3c --full    # program name

The deployment's log groups are those tagged ``fast:app=<prefix>`` plus those AWS services name for it: Lambda and
state machine logs, the bus and MediaConvert job events, MediaTailor ad-insertion logs (sessions, ADS requests),
the Channel Assembly as-run log and the agent's Runtime, Gateway and Memory logs. Logs Insights matches the
identifier and what the media library links to it: the ingest correlation ids of an asset, and the SCTE-35 event
ids of a program's breaks, which reach the ad decision server as ``evid=<id>`` (SPEC §9). Events become searchable
a minute or two after they are written. CloudFront access logs are Parquet objects in the logs bucket and are not
searched.
"""

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import batched
from typing import TYPE_CHECKING, Any, Final

from _stack import Plane, StackError, Stage, add_prefix_argument, run
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from fast_channels.core.naming import agentcore_name
from fast_channels.core.tags import TAG_APP
from fast_channels.functions.tools.library_store import GSI1, Asset, ProgramRecord

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table
    from mypy_boto3_logs import CloudWatchLogsClient

APP_TAG: Final = TAG_APP
AS_RUN_LOG_GROUP: Final = "MediaTailor/Channel/AsRunLog"
"""Channel Assembly's account-wide as-run log (``infra.constructs.channel_assembly.AS_RUN_LOG_GROUP``)."""
LOG_GROUPS_PER_QUERY: Final = 50
RESULT_LIMIT: Final = 10_000
IDENTIFIER: Final = re.compile(r'[^"\\\s]{3,256}')
SINCE: Final = re.compile(r"(\d+)([smhd])")
UNIT_SECONDS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400}
SUMMARY_WIDTH: Final = 160
SUMMARY_FIELDS: Final = (
    "level",  # Powertools JSON logs of the Lambda functions
    "message",
    "type",  # Step Functions execution events
    "details.name",
    "details.error",
    "detail-type",  # EventBridge events: bus logs, MediaConvert job state changes
    "detail.status",
    "message_type",
    "eventType",  # MediaTailor ad-insertion and Channel Assembly as-run logs
    "programName",
    "adsRequestUrl",
    "body",  # OpenTelemetry log records of the agent runtime
)
"""Fields that say what a log record is about, in display order (missing ones are skipped)."""


@dataclass(frozen=True, slots=True, order=True)
class Record:
    """One matching log event."""

    timestamp: datetime
    log_group: str
    message: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("id", help="asset id, correlation id, MediaTailor session id or program name")
    parser.add_argument("--since", type=since, default=timedelta(hours=24), help="how far back, e.g. 30m, 6h, 2d")
    parser.add_argument("--full", action="store_true", help="print whole log messages instead of a summary")
    add_prefix_argument(parser)
    args = parser.parse_args()
    if not IDENTIFIER.fullmatch(args.id):
        parser.error("the id must be 3-256 characters without spaces, quotes or backslashes")

    stage = Stage.of(args.prefix)
    logs = stage.session.client("logs", region_name=stage.region)
    tagging = stage.session.client("resourcegroupstaggingapi", region_name=stage.region)
    terms = [args.id, *related(stage, args.id)]
    groups = log_groups(stage, logs, tagging)
    if not groups:
        sys.exit(f"no log groups of deployment {stage.prefix} in {stage.region}")
    end = datetime.now(UTC)
    start = end - args.since
    print(
        f"Searching {len(groups)} log groups of {stage.prefix} ({stage.region}) from {start:%Y-%m-%d %H:%M}Z "
        f"for {', '.join(terms)}",
        file=sys.stderr,
    )
    records = search(logs, groups, terms, start=start, end=end)
    for record in records:
        print(line(record, full=args.full))
    print(f"{len(records)} log events", file=sys.stderr)
    return 0 if records else 1


def since(text: str) -> timedelta:
    """``--since`` value: a whole number of seconds, minutes, hours or days (``90s``, ``30m``, ``6h``, ``2d``)."""
    if not (match := SINCE.fullmatch(text)):
        raise argparse.ArgumentTypeError(f"{text!r} is not a duration such as 30m, 6h or 2d")
    return timedelta(seconds=int(match[1]) * UNIT_SECONDS[match[2]])


def related(stage: Stage, identifier: str) -> list[str]:
    """Identifiers the media library links to ``identifier`` (none when the Data stack is gone)."""
    try:
        name = stage.output(Plane.DATA, "LibraryTableName")
    except StackError:
        return []
    return library_links(stage.session.resource("dynamodb", region_name=stage.region).Table(name), identifier)


def library_links(table: Table, identifier: str) -> list[str]:
    """The ingest correlation ids of an asset's encodes, or ``evid=<event id>`` for each break of a program.

    Lambda and Step Functions logs of an ingest carry its correlation id; ad decision requests carry the break's
    SCTE-35 event id, not the program name (keys: CONTRACTS §D).
    """
    encodes = table.query(KeyConditionExpression=Key("pk").eq(f"ASSET#{identifier}"))["Items"]
    programs = table.query(IndexName=GSI1, KeyConditionExpression=Key("gsi1pk").eq(f"PROGRAM#{identifier}"))["Items"]
    links = {Asset.from_item(item).correlation_id for item in encodes}
    links |= {f"evid={b.event_id}" for item in programs for b in ProgramRecord.from_item(item).breaks}
    return sorted(link for link in links if link and link != identifier)


def log_groups(stage: Stage, logs: CloudWatchLogsClient, tagging: Any) -> list[str]:
    """Names of the deployment's log groups in the session's region."""
    named = {group for prefix in name_prefixes(stage) for group in groups_named(logs, prefix)}
    return sorted(named | set(tagged_log_groups(tagging, stage.prefix)))


def name_prefixes(stage: Stage) -> tuple[str, ...]:
    """Log group name prefixes of a deployment: CloudFormation names (``<stack>-<logical id>-<suffix>``, Lambda
    functions) and the names services or the constructs give vended logs."""
    prefix = stage.prefix
    return (
        *(f"{stage.stack_name(plane)}-" for plane in Plane),
        f"/aws/vendedlogs/states/{prefix}-",
        f"/aws/vendedlogs/events/{prefix}-",
        f"/aws/vendedlogs/mediatailor/{prefix}",
        f"/aws/vendedlogs/bedrock-agentcore/{prefix}/",
        f"/aws/vendedlogs/cloudfront/{prefix}",
        f"/aws/bedrock-agentcore/runtimes/{agentcore_name(prefix, 'scheduler')}-",
        AS_RUN_LOG_GROUP,
    )


def groups_named(logs: CloudWatchLogsClient, prefix: str) -> list[str]:
    pages = logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix)
    return [group["logGroupName"] for page in pages for group in page["logGroups"]]


def tagged_log_groups(tagging: Any, prefix: str) -> list[str]:
    """Log groups tagged ``fast:app=<prefix>`` (Resource Groups Tagging API client ``tagging``); empty, with a
    warning, without the tag:GetResources permission."""
    pages = tagging.get_paginator("get_resources").paginate(
        TagFilters=[{"Key": APP_TAG, "Values": [prefix]}], ResourceTypeFilters=["logs:log-group"]
    )
    try:
        arns = [resource["ResourceARN"] for page in pages for resource in page["ResourceTagMappingList"]]
    except ClientError as error:
        print(f"warning: tagged log groups not searched ({error})", file=sys.stderr)
        return []
    return [arn.partition(":log-group:")[2].removesuffix(":*") for arn in arns]


def query_string(terms: list[str]) -> str:
    """Logs Insights QL for events mentioning any of ``terms`` (validated: no quotes or backslashes)."""
    match = " or ".join(f'@message like "{term}"' for term in terms)
    return f"fields @timestamp, @log, @message\n| filter {match}\n| sort @timestamp asc\n| limit {RESULT_LIMIT}"


def search(
    logs: CloudWatchLogsClient, groups: list[str], terms: list[str], *, start: datetime, end: datetime
) -> list[Record]:
    """Run the query over every log group (at most 50 per query, run concurrently) and merge the results."""
    query = query_string(terms)
    queries = [
        logs.start_query(
            logGroupNames=list(batch),
            startTime=int(start.timestamp()),
            endTime=int(end.timestamp()),
            queryString=query,
            limit=RESULT_LIMIT,
        )["queryId"]
        for batch in batched(groups, LOG_GROUPS_PER_QUERY, strict=False)
    ]
    records: list[Record] = []
    for query_id in queries:
        rows = results(logs, query_id)
        if len(rows) >= RESULT_LIMIT:
            print(f"warning: only the first {RESULT_LIMIT} events of a query; use a shorter --since", file=sys.stderr)
        records += [record(row) for row in rows]
    return sorted(records)


def results(logs: CloudWatchLogsClient, query_id: str) -> list[dict[str, str]]:
    """Rows of a finished query as ``{field: value}``."""
    while True:
        response = logs.get_query_results(queryId=query_id)
        match response["status"]:
            case "Complete":
                return [
                    {f["field"]: f["value"] for f in row if "field" in f and "value" in f}
                    for row in response["results"]
                ]
            case "Scheduled" | "Running":
                time.sleep(1)
            case status:
                sys.exit(f"Logs Insights query {query_id} ended {status}")


def record(row: dict[str, str]) -> Record:
    """A result row; ``@log`` is ``<account>:<log group>`` and ``@timestamp`` UTC with milliseconds."""
    when = datetime.strptime(row["@timestamp"], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
    return Record(when, row.get("@log", "").partition(":")[2], row.get("@message", ""))


def line(record: Record, *, full: bool) -> str:
    """``<UTC time>  <source>  <summary or message>``."""
    fields = json_fields(record.message)
    text = record.message.rstrip() if full else summary(fields, record.message)
    when = f"{record.timestamp:%Y-%m-%dT%H:%M:%S.%f}"[:-3] + "Z"
    return f"{when}  {source(record.log_group, fields):<34.34}  {text}"


def json_fields(message: str) -> dict[str, Any]:
    try:
        fields = json.loads(message)
    except ValueError:
        return {}
    return fields if isinstance(fields, dict) else {}


def source(log_group: str, fields: dict[str, Any]) -> str:
    """The Powertools service of a function's log (e.g. ``ingest.prepare``), else the shortened log group."""
    if service := fields.get("service"):
        return str(service)
    return log_group.removeprefix("/aws/vendedlogs/").removeprefix("/aws/")


def summary(fields: dict[str, Any], message: str) -> str:
    """The :data:`SUMMARY_FIELDS` of a JSON record (else the message on one line), cut to one terminal line."""
    values = [str(value) for path in SUMMARY_FIELDS if (value := lookup(fields, path)) not in (None, "", [], {})]
    text = " | ".join(dict.fromkeys(values)) or " ".join(message.split())
    return text if len(text) <= SUMMARY_WIDTH else f"{text[: SUMMARY_WIDTH - 1]}…"


def lookup(fields: dict[str, Any], path: str) -> Any:
    """Value at a dotted ``path`` of nested objects, or None."""
    value: Any = fields
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


if __name__ == "__main__":
    run(main)
