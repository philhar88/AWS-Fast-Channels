#!/usr/bin/env -S uv run python
"""Follow one identifier through every CloudWatch log of a deployment, as a single time-ordered timeline.

    uv run python scripts/trace.py big-buck-bunny-1a2b3c                   # asset id
    uv run python scripts/trace.py seed-sintel-1a2b3c4d --since 6h         # ingest correlation id (execution name)
    uv run python scripts/trace.py 49f0c250-9e3c-4997-a723-3cd95eff9d42    # MediaTailor playback session id
    uv run python scripts/trace.py 20261016T120000-sintel-1a2b3c --full    # program name
    uv run python scripts/trace.py 49f0c250-9e3c-4997-a723-3cd95eff9d42.820461826 --lake   # decision id
    uv run python scripts/trace.py 01JBC8Z9X4Q7M2N5P8R3T6V9W2 --lake         # change id (a release)
    uv run python scripts/trace.py r0007                                   # release tag (fv)

The deployment's log groups are those tagged ``fast:app=<prefix>`` plus those AWS services name for it: Lambda and
state machine logs, the bus and MediaConvert job events, MediaTailor ad-insertion logs (sessions, ADS requests; the
release module's siblings too), the Channel Assembly as-run log, the agent's Runtime, Gateway and Memory logs, and
with the modules the Pod Decision Service's decision records and Prebid Server's container logs. Logs Insights
matches the identifier and what is linked to it (``docs/observability.md``, "Correlation chains"):

- the media library: the ingest correlation ids of an asset, and the SCTE-35 event ids of a program's breaks, which
  reach the ad decision server as ``evid=<id>``;
- a decision id ``<sid>.<evid>`` (one session's decision on one break): its MediaTailor session id;
- the Release Controller: a change id's release tag ``rNNNN`` (the ``fv`` of every decision record), and back.

Events become searchable a minute or two after they are written. With ``--lake`` the same terms are also looked up
in the ops module's lake with Athena: the PDS decision records, the MediaTailor log copies and the archived ledger.
CloudFront access logs are not searched: their lake table has no session or query fields.
"""

import argparse
import json
import re
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import batched
from typing import TYPE_CHECKING, Any, Final

from _stack import Module, Plane, StackError, Stage, add_prefix_argument, run
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from fast_channels.core.monetization.functions import release_tag
from fast_channels.core.monetization.release import ReleaseStatus
from fast_channels.core.naming import agentcore_name
from fast_channels.core.pds.names import sidecar_log_group
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
ATHENA_POLL_SECONDS: Final = 1
IDENTIFIER: Final = re.compile(r'[^"\\\s]{3,256}')
SINCE: Final = re.compile(r"(\d+)([smhd])")
DECISION_ID: Final = re.compile(
    r"(?P<sid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(?P<evid>[0-9]{1,10})"
)
"""``<sid>.<evid>``: one MediaTailor session's decision on one break (``core.pds.models.decision_id``)."""
CHANGE_ID: Final = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
"""A ULID (``core.ledger.new_record_id``): the Release Controller's change ids."""
RELEASE_TAG: Final = re.compile(r"r[0-9]{4}")
"""``rNNNN`` (``core.monetization.functions.release_tag``): the ``fv`` of decision records, ``X-Fast-Fv``."""
RELEASE_ITEM: Final = "RELEASE#"
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
    "record",  # PDS decision records (pds.policy, pds.select, pds.delivery)
    "decision_id",
    "evt",  # the fast-ctv module's lines in Prebid Server's container log
    "action",  # ledger entries in the lake
)
"""Fields that say what a log record is about, in display order (missing ones are skipped)."""


@dataclass(frozen=True, slots=True)
class LakeTable:
    """A lake table that ``--lake`` searches: the columns compared with the terms, and how its rows are dated."""

    name: str
    columns: tuple[str, ...]
    time: str
    """Column with the record's time."""
    partition: str
    partition_format: str
    """``strftime`` format of the partition values (the tables' partition projection)."""
    record: str | None = None
    """``record`` value of the rows (the decision tables' objects hold an empty line between records)."""
    starts: tuple[str, ...] = ()
    """Columns whose value may start with a term (the ledger's ``subject``: ``<decision id>/<crid>``)."""


LAKE_TABLES: Final = (
    LakeTable("pds_policy", ("decision_id", "sid", "evid", "upid", "fv", "correlation_id"), "timestamp", "datehour",
              "%Y/%m/%d/%H", "pds.policy"),
    LakeTable("pds_select", ("decision_id", "sid", "evid", "correlation_id"), "timestamp", "datehour", "%Y/%m/%d/%H",
              "pds.select"),
    LakeTable("pds_delivery", ("decision_id", "sid", "evid", "correlation_id"), "timestamp", "datehour",
              "%Y/%m/%d/%H", "pds.delivery"),
    LakeTable("mediatailor_ads", ("sessionid", "eventid"), "eventtimestamp", "datehour", "%Y/%m/%d/%H"),
    LakeTable("mediatailor_manifest", ("sessionid",), "eventtimestamp", "datehour", "%Y/%m/%d/%H"),
    LakeTable("ledger", ("correlation_id", "subject", "record_id"), "at", "day", "%Y/%m/%d", starts=("subject",)),
)  # fmt: skip
"""The lake tables of the correlation chains (``infra.constructs.ops.lake``, ``infra.constructs.pds.decisions``)."""
LAKE_ROW_LIMIT: Final = 1000


@dataclass(frozen=True, slots=True, order=True)
class Record:
    """One matching log event."""

    timestamp: datetime
    log_group: str
    message: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "id",
        help="asset id, correlation id, MediaTailor session id, program name, decision id (<sid>.<evid>), change id "
        "or release tag (rNNNN)",
    )
    parser.add_argument("--since", type=since, default=timedelta(hours=24), help="how far back, e.g. 30m, 6h, 2d")
    parser.add_argument("--full", action="store_true", help="print whole log messages instead of a summary")
    parser.add_argument(
        "--lake", action="store_true", help="also search the ops module's lake with Athena (decision records, ledger)"
    )
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
    if args.lake:
        records = sorted([*records, *lake_search(stage, terms, start=start)])
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
    """Identifiers linked to ``identifier``: by its shape (a decision id's session), by the media library and by the
    Release Controller's releases (each skipped when its stack is gone)."""
    links = set(decision_links(identifier))
    dynamodb = stage.session.resource("dynamodb", region_name=stage.region)
    with suppress(StackError):
        links |= set(library_links(dynamodb.Table(stage.output(Plane.DATA, "LibraryTableName")), identifier))
    if CHANGE_ID.fullmatch(identifier) or RELEASE_TAG.fullmatch(identifier):
        with suppress(StackError):
            links |= set(release_links(dynamodb.Table(stage.output(Module.RELEASE, "StateTableName")), identifier))
    return sorted(links - {identifier})


def decision_links(identifier: str) -> list[str]:
    """A decision id's MediaTailor session id: the session's own logs belong to the viewer chain."""
    match = DECISION_ID.fullmatch(identifier)
    return [match["sid"]] if match else []


def release_links(table: Table, identifier: str) -> list[str]:
    """The release tag of a change id, or the change id of a release tag, from the Release Controller's state table
    (``RELEASE#rNNNN`` items; a stage has a few hundred at most)."""
    links: list[str] = []
    request: dict[str, Any] = {"FilterExpression": Attr("pk").begins_with(RELEASE_ITEM)}
    while True:
        page = table.scan(**request)
        for item in page.get("Items", []):
            status = ReleaseStatus.model_validate_json(str(item["doc"]))
            tag = release_tag(status.release)
            if identifier == status.change_id:
                links.append(tag)
            elif identifier == tag:
                links.append(status.change_id)
        if "LastEvaluatedKey" not in page:
            return sorted(set(links))
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


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
    named = {group for prefix in name_prefixes(stage) for group in groups_named(logs, prefix) if owned(group, prefix)}
    return sorted(named | set(tagged_log_groups(tagging, stage.prefix)))


def owned(group: str, prefix: str) -> bool:
    """Whether ``group`` is the deployment's. A name prefix that ends in the deployment prefix
    (``…/mediatailor/fastci``) also matches another deployment's (``…/mediatailor/fastciads``), so such a group must
    be the prefix itself or continue with ``-``."""
    if prefix.endswith(("-", "/", "_")) or group == prefix:
        return True
    return group.startswith(f"{prefix}-")


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
        sidecar_log_group(prefix),  # the PDS sidecar: decision records, own-book lines, stats
        f"{prefix}-pbs-",  # the Prebid Server guidance's stacks: the container's log group
        *(f"{stage.stack_name(module)}-" for module in Module),
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


def lake_search(stage: Stage, terms: list[str], *, start: datetime) -> list[Record]:
    """The rows of the lake's correlation tables that name any of ``terms`` since ``start`` (Athena, the ops module's
    workgroup and its 10 GiB scan limit); none, with a warning, without the ops module."""
    try:
        database = stage.output(Module.OPS, "LakeDatabase")
        workgroup = stage.output(Module.OPS, "LakeWorkGroup")
    except StackError as error:
        print(f"warning: the lake is not searched ({error})", file=sys.stderr)
        return []
    glue = stage.session.client("glue", region_name=stage.region)
    athena = stage.session.client("athena", region_name=stage.region)
    present = {
        t["Name"]
        for page in glue.get_paginator("get_tables").paginate(DatabaseName=database)
        for t in page["TableList"]
    }
    records: list[Record] = []
    for table in LAKE_TABLES:
        if table.name in present:
            query = lake_query(table, terms, database=database, start=start)
            records += [lake_record(table, row) for row in athena_rows(athena, query, workgroup=workgroup)]
    return [record for record in records if record.timestamp >= start]  # day partitions reach before ``start``


def lake_query(table: LakeTable, terms: list[str], *, database: str, start: datetime) -> str:
    """Athena SQL for ``table``'s rows since ``start`` whose id columns hold one of ``terms`` (an ``evid=<id>`` term
    also as the bare id); values are quoted SQL strings."""
    values = sorted({*terms, *(term.removeprefix("evid=") for term in terms if term.startswith("evid="))})
    literals = ["'" + value.replace("'", "''") + "'" for value in values]
    quoted = ", ".join(literals)
    match = " OR ".join(
        [
            *(f"{column} IN ({quoted})" for column in table.columns),
            *(f"starts_with({column}, {literal})" for column in table.starts for literal in literals),
        ]
    )
    conditions = [f"{table.partition} >= '{start:{table.partition_format}}'", f"({match})"]
    if table.record:
        conditions.append(f"record = '{table.record}'")
    # The terms passed IDENTIFIER (no double quote, backslash or space) and are quoted with '' escaping; the database
    # is the Ops stack's output and the table a LAKE_TABLES constant.
    return (
        f'SELECT CAST({table.time} AS varchar) AS lake_time, * FROM "{database}"."{table.name}"\n'  # noqa: S608
        f"WHERE {' AND '.join(conditions)}\nLIMIT {LAKE_ROW_LIMIT}"
    )


def athena_rows(athena: Any, query: str, *, workgroup: str) -> list[dict[str, str]]:
    """Rows of a finished Athena query as ``{column: value}`` (empty values left out)."""
    execution = athena.start_query_execution(QueryString=query, WorkGroup=workgroup)["QueryExecutionId"]
    while True:
        status = athena.get_query_execution(QueryExecutionId=execution)["QueryExecution"]["Status"]
        match status["State"]:
            case "SUCCEEDED":
                break
            case "QUEUED" | "RUNNING":
                time.sleep(ATHENA_POLL_SECONDS)
            case state:
                sys.exit(f"Athena query {execution} ended {state}: {status.get('StateChangeReason', '')}")
    header: list[str] = []
    rows: list[dict[str, str]] = []
    for page in athena.get_paginator("get_query_results").paginate(QueryExecutionId=execution):
        for row in page["ResultSet"]["Rows"]:
            values = [cell.get("VarCharValue") for cell in row["Data"]]
            if not header:
                header = [str(value) for value in values]
                continue
            rows.append({name: value for name, value in zip(header, values, strict=True) if value not in (None, "")})
    return rows


def lake_record(table: LakeTable, row: dict[str, str]) -> Record:
    """A lake row as a timeline record: its time column, the source ``lake/<table>`` and the row as JSON."""
    fields = {name: value for name, value in row.items() if name != "lake_time"}
    return Record(lake_time(row.get("lake_time", "")), f"lake/{table.name}", json.dumps(fields, sort_keys=True))


def lake_time(text: str) -> datetime:
    """A lake row's time: ISO 8601 (MediaTailor, the ledger) or Powertools' ``2026-10-30 08:08:31,577+0000``;
    the epoch when it has none (:func:`lake_search` leaves such rows out)."""
    normalised = text.strip().replace(",", ".").replace("Z", "+00:00")
    normalised = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", normalised)
    try:
        when = datetime.fromisoformat(normalised)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=UTC)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


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
