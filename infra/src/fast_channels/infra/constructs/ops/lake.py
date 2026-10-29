"""The per-stage lake: an S3 bucket, a Glue database of log tables and views, and an Athena
workgroup with a bytes-scanned limit.

- :class:`LakeBucket` lives in the Data stack with the deployment's other buckets, so that its access-log and
  log-delivery grants stay in single bucket policies owned by that stack.
- :class:`Lake` lives in the Ops stack: the Glue database ``<prefix>_lake``, its tables and views, the Athena
  workgroup ``<prefix>-lake``, and the vended-log deliveries that copy each IaC playback configuration's MediaTailor
  ad decision server and manifest logs into the bucket.

Privacy: the ADS log copies drop ``requestHeaders`` (which holds the viewer's address and, on
monetized configurations, header secrets and the ticket) and ``rawAdsRequest``; the manifest log copies drop
``mediaTailorPath``, ``input`` and ``http``. The CloudFront table reads the v3.0 Parquet logs and leaves out
``c-ip``, ``cs-uri-query`` (the original viewer query, before any edge rewrite) and ``x-forwarded-for``, so the
lake never returns them.
"""

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from aws_cdk import Duration, Fn, RemovalPolicy, Stack
from aws_cdk import aws_athena as athena
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.delivery import ACCESS_LOG_FIELDS
from fast_channels.infra.constructs.logs import DELIVERY_PRINCIPAL, deliver

__all__ = [
    "ADS_LOG_FIELDS",
    "EXCLUDED_ACCESS_LOG_FIELDS",
    "EXCLUDED_ADS_LOG_FIELDS",
    "EXCLUDED_MANIFEST_LOG_FIELDS",
    "MANIFEST_LOG_FIELDS",
    "QUERY_SCAN_LIMIT_BYTES",
    "Lake",
    "LakeBucket",
    "lake_database_name",
]

# Default record fields of MediaTailor's S3 deliveries, in the service's order (``aws logs
# describe-configuration-templates --service mediatailor``, us-east-1, 2026-10-29).
_ADS_DEFAULT_FIELDS: Final = (
    "eventTimestamp",
    "awsAccountId",
    "originId",
    "customerId",
    "eventType",
    "sessionType",
    "requestId",
    "eventDescription",
    "sessionId",
    "vodVastResponseTimeOffset",
    "adsRequestUrl",
    "adMarkers",
    "segmentationUpid",
    "segmentationTypeId",
    "originalTargetUrl",
    "updatedTargetUrl",
    "rawAdsRequest",
    "rawAdsRequestIndex",
    "rawAdsResponse",
    "rawAdsResponseIndex",
    "avail",
    "vastResponse",
    "vastAd",
    "vodCreativeOffsets",
    "requestHeaders",
    "beaconInfo",
    "adBreakCorrelationId",
    "prefetchScheduleName",
    "eventId",
    "functionId",
    "executionTimeMs",
    "functionType",
    "status",
    "errorType",
    "error",
    "additionalInfo",
    "cause",
    "input",
    "output",
    "http",
    "streamId",
)
_MANIFEST_DEFAULT_FIELDS: Final = (
    "eventTimestamp",
    "awsAccountId",
    "originId",
    "customerId",
    "eventType",
    "sessionType",
    "requestId",
    "eventDescription",
    "sessionId",
    "originRequestUrl",
    "mediaTailorPath",
    "responseBody",
    "requestNextToken",
    "assetPath",
    "originFullUrl",
    "originPrefixUrl",
    "additionalInfo",
    "cause",
    "response",
    "httpCode",
    "errorMessage",
    "adAdsResponse",
    "adAdsRawResponse",
    "adAdsRequest",
    "adNumNewAvails",
    "generatedMediaPlaylist",
    "requestStartTime",
    "requestEndTime",
    "requestStartTimeEpochMillis",
    "requestEndTimeEpochMillis",
    "eventId",
    "functionId",
    "executionTimeMs",
    "functionType",
    "status",
    "errorType",
    "input",
    "output",
    "http",
    "streamId",
)
EXCLUDED_ADS_LOG_FIELDS: Final = frozenset({"requestHeaders", "rawAdsRequest"})
"""Never in the lake: the headers MediaTailor sent the ADS (viewer address and user agent; on monetized
configurations the origin secret and the ticket) and the opt-in raw request body (the ticket)."""
EXCLUDED_MANIFEST_LOG_FIELDS: Final = frozenset({"mediaTailorPath", "input", "http"})
"""Never in the lake: the viewer's request path with its query, and the session-initialization hook's input and HTTP
exchange."""
EXCLUDED_ACCESS_LOG_FIELDS: Final = frozenset({"c-ip", "cs-uri-query", "x-forwarded-for"})
"""CloudFront fields the lake's table leaves out: the viewer's address and the original viewer query."""
ADS_LOG_FIELDS: Final = tuple(f for f in _ADS_DEFAULT_FIELDS if f not in EXCLUDED_ADS_LOG_FIELDS)
"""Record fields of the lake's copy of the ad decision server logs."""
MANIFEST_LOG_FIELDS: Final = tuple(f for f in _MANIFEST_DEFAULT_FIELDS if f not in EXCLUDED_MANIFEST_LOG_FIELDS)
"""Record fields of the lake's copy of the manifest logs."""

QUERY_SCAN_LIMIT_BYTES: Final = 10 * 1024**3
"""Athena cancels a lake query that would scan more than this (10 GiB, about 5 US cents)."""
MEDIATAILOR_PREFIX: Final = "mediatailor"
"""Key prefix of the MediaTailor log copies: ``mediatailor/<ads|manifest>/``."""
_SUFFIX_PATH: Final = "{yyyy}/{MM}/{dd}/{HH}"
_DATEHOUR_FORMAT: Final = "yyyy/MM/dd/HH"


def lake_database_name(prefix: str) -> str:
    """Glue database of the deployment's lake: ``<prefix>_lake`` with hyphens as underscores (``fastci-pr7`` ->
    ``fastci_pr7_lake``), which Athena needs no quotes for."""
    return f"{prefix.replace('-', '_')}_lake"


class LakeBucket(Construct):
    """The lake bucket: versioned, SSE-S3, bucket-owner enforced, private, TLS 1.2+ only, access-logged to the logs
    bucket (``s3/lake/``); objects expire after ``expiration`` and noncurrent versions a day later.

    Vended-log deliveries of this account whose delivery source is named ``<prefix>-*`` may write under
    ``mediatailor/`` (declared here so that CreateDelivery adds no statement out of band).

    Args:
        prefix: deployment prefix.
        logs_bucket: receives the access logs.
        expiration: how long lake objects are kept (``modules.ops.lake_retention_days``).
        removal_policy: ``DESTROY`` also empties the bucket on deletion.

    Attributes:
        bucket: the bucket.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        logs_bucket: s3.IBucket,
        expiration: Duration,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.bucket = s3.Bucket(
            self,
            "Bucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            versioned=True,
            removal_policy=removal_policy,
            auto_delete_objects=removal_policy == RemovalPolicy.DESTROY,
            server_access_logs_bucket=logs_bucket,
            server_access_logs_prefix="s3/lake/",
            target_object_key_format=s3.TargetObjectKeyFormat.partitioned_prefix(s3.PartitionDateSource.EVENT_TIME),
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="abort-incomplete-uploads", abort_incomplete_multipart_upload_after=Duration.days(7)
                ),
                s3.LifecycleRule(
                    id="expire-lake-objects",
                    expiration=expiration,
                    noncurrent_version_expiration=Duration.days(1),
                    expired_object_delete_marker=False,
                ),
            ],
        )
        stack = Stack.of(self)
        delivery = iam.ServicePrincipal(DELIVERY_PRINCIPAL)
        sources = {"ArnLike": {"aws:SourceArn": arn(self, "logs", "delivery-source", f"{prefix}-*", colon=True)}}
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="LogDeliveryAclCheck",
                principals=[delivery],
                actions=["s3:GetBucketAcl"],
                resources=[self.bucket.bucket_arn],
                conditions={"StringEquals": {"aws:SourceAccount": stack.account}, **sources},
            )
        )
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="LogDeliveryWrite",
                principals=[delivery],
                actions=["s3:PutObject"],
                resources=[self.bucket.arn_for_objects(f"{MEDIATAILOR_PREFIX}/*")],
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account, "s3:x-amz-acl": "bucket-owner-full-control"},
                    **sources,
                },
            )
        )


@dataclass(frozen=True, slots=True)
class LakeLog:
    """A MediaTailor vended-log type the lake copies (one table, one delivery destination)."""

    log_type: str
    """CloudWatch Logs delivery source log type."""
    suffix: str
    """Delivery source name suffix (``<playback configuration>-<suffix>``) and key prefix ``mediatailor/<suffix>/``."""
    service_dir: str
    """The directory S3 deliveries of this log type write under ``AWSLogs/<account>/`` (observed on fastm,
    2026-10-29: ``mediatailor/ads/AWSLogs/<account>/mediatailoraddecisionserverinteractionlogs/<yyyy>/<MM>/<dd>/
    <HH>/<account>_mediatailoraddecisionserverinteractionlogs_<region>_<configuration>_<yyyyMMddTHH>Z_<id>.log.gz``)."""
    table: str
    fields: tuple[str, ...]
    description: str


LAKE_LOGS: Final = (
    LakeLog(
        "AD_DECISION_SERVER_LOGS",
        "ads",
        "mediatailoraddecisionserverinteractionlogs",
        "mediatailor_ads",
        ADS_LOG_FIELDS,
        "MediaTailor ad decision server logs of the IaC playback configurations (no requestHeaders, rawAdsRequest)",
    ),
    LakeLog(
        "MANIFEST_SERVICE_LOGS",
        "manifest",
        "mediatailormanifestservicelogs",
        "mediatailor_manifest",
        MANIFEST_LOG_FIELDS,
        "MediaTailor manifest service logs of the IaC playback configurations (no mediaTailorPath, input, http)",
    ),
)


@dataclass(frozen=True, slots=True)
class View:
    """An Athena view of the lake, stored in the Glue catalog like Athena's own ``CREATE VIEW``."""

    name: str
    description: str
    columns: tuple[tuple[str, str], ...]
    """Output columns: name and Trino type (``varchar``, ``bigint``, ``double``, ``timestamp(3)``)."""
    sql: str
    """The ``SELECT``; ``{db}`` stands for the lake database."""


_HIVE_TYPES: Final = {"varchar": "string", "bigint": "bigint", "double": "double", "timestamp(3)": "timestamp"}

# Real-time ADS requests carry the break's SCTE-35 event id (evid=) and the content's UPID (upid=); the ad marker's
# DATERANGE ID is "<start epoch ms>-<segmentation type>-<event id in hex>" (seen live, 2026-10). FILLED_AVAIL joins its
# MAKING_ADS_REQUEST by requestId within one personalization.
_BREAK_EVENTS: Final = """
WITH requests AS (
  SELECT originid AS configuration, sessionid AS session_id, requestid AS request_id,
    CAST(from_iso8601_timestamp(eventtimestamp) AS timestamp(3)) AS at,
    TRY_CAST(url_extract_parameter(adsrequesturl, 'evid') AS bigint) AS evid,
    url_extract_parameter(adsrequesturl, 'upid') AS upid,
    url_extract_parameter(adsrequesturl, 'ch') AS channel
  FROM "{db}"."mediatailor_ads" WHERE eventtype = 'MAKING_ADS_REQUEST'
),
markers AS (
  SELECT originid AS configuration, sessionid AS session_id,
    from_base(regexp_extract(admarkers, 'ID="[0-9]+-[0-9]+-([0-9A-Fa-f]+)"', 1), 16) AS evid,
    segmentationupid AS upid,
    CAST(from_iso8601_timestamp(regexp_extract(admarkers, 'START-DATE="([^"]+)"', 1)) AS timestamp(3)) AS started_at
  FROM "{db}"."mediatailor_ads" WHERE eventtype = 'AD_MARKER_FOUND'
),
fills AS (
  SELECT requestid AS request_id,
    TRY_CAST(json_extract_scalar(avail, '$.originavailduration') AS double) AS avail_seconds,
    TRY_CAST(json_extract_scalar(avail, '$.filledduration') AS double) AS filled_seconds
  FROM "{db}"."mediatailor_ads" WHERE eventtype = 'FILLED_AVAIL'
)
"""

VIEWS: Final = (
    View(
        "v_breaks",
        "One row per aired break (SCTE-35 event id) that any session of an IaC playback configuration saw",
        (
            ("evid", "bigint"),
            ("upid", "varchar"),
            ("channel", "varchar"),
            ("started_at", "timestamp(3)"),
            ("first_request_at", "timestamp(3)"),
            ("configurations", "bigint"),
            ("sessions", "bigint"),
            ("ads_requests", "bigint"),
            ("filled_avails", "bigint"),
            ("avail_seconds", "double"),
            ("filled_seconds", "double"),
        ),
        _BREAK_EVENTS  # noqa: S608 - constant SQL, nothing from input
        + """
,
seen AS (
  SELECT evid, count(DISTINCT configuration) AS configurations, count(DISTINCT session_id) AS sessions
  FROM (
    SELECT evid, configuration, session_id FROM markers
    UNION ALL
    SELECT evid, configuration, session_id FROM requests
  )
  WHERE evid IS NOT NULL GROUP BY evid
),
marked AS (
  SELECT evid, max(upid) AS upid, min(started_at) AS started_at FROM markers WHERE evid IS NOT NULL GROUP BY evid
),
requested AS (
  SELECT r.evid, max(r.upid) AS upid, max(r.channel) AS channel, min(r.at) AS first_request_at,
    count(DISTINCT r.request_id) AS ads_requests, count(f.request_id) AS filled_avails,
    max(f.avail_seconds) AS avail_seconds, sum(f.filled_seconds) AS filled_seconds
  FROM requests r LEFT JOIN fills f ON f.request_id = r.request_id
  WHERE r.evid IS NOT NULL GROUP BY r.evid
)
SELECT s.evid, coalesce(m.upid, q.upid) AS upid, q.channel, m.started_at, q.first_request_at, s.configurations,
  s.sessions, coalesce(q.ads_requests, 0) AS ads_requests, coalesce(q.filled_avails, 0) AS filled_avails,
  q.avail_seconds, q.filled_seconds
FROM seen s LEFT JOIN marked m ON m.evid = s.evid LEFT JOIN requested q ON q.evid = s.evid
""",
    ),
    View(
        "v_fill_by_pc",
        "Ad requests, filled avails and fill rate per playback configuration and hour",
        (
            ("configuration", "varchar"),
            ("hour", "timestamp(3)"),
            ("breaks", "bigint"),
            ("sessions", "bigint"),
            ("ads_requests", "bigint"),
            ("filled_avails", "bigint"),
            ("avail_seconds", "double"),
            ("filled_seconds", "double"),
            ("fill_rate", "double"),
        ),
        _BREAK_EVENTS
        + """
SELECT r.configuration, date_trunc('hour', r.at) AS hour, count(DISTINCT r.evid) AS breaks,
  count(DISTINCT r.session_id) AS sessions, count(DISTINCT r.request_id) AS ads_requests,
  count(f.request_id) AS filled_avails, sum(f.avail_seconds) AS avail_seconds,
  sum(f.filled_seconds) AS filled_seconds,
  sum(f.filled_seconds) / nullif(sum(f.avail_seconds), 0) AS fill_rate
FROM requests r LEFT JOIN fills f ON f.request_id = r.request_id
GROUP BY r.configuration, date_trunc('hour', r.at)
""",
    ),
)


class Lake(Construct):
    """The Glue database, tables and views, the Athena workgroup and the MediaTailor log copies of the lake.

    Tables (partition projection, so no crawler and no partition maintenance):

    - ``cloudfront_access``: the distribution's standard logs v2 (Parquet in the logs bucket, every field but
      :data:`EXCLUDED_ACCESS_LOG_FIELDS`), partitioned by ``year``, ``month``, ``day``, ``hour``;
    - ``mediatailor_ads``, ``mediatailor_manifest``: JSON copies of each IaC playback configuration's vended logs
      (:data:`ADS_LOG_FIELDS`, :data:`MANIFEST_LOG_FIELDS`), partitioned by ``datehour`` (``yyyy/MM/dd/HH``) over
      the lake's retention. Nested objects (``avail``, ``vastResponse``, ``beaconInfo``) are JSON text with keys in
      lower case (the OpenX SerDe lowercases them): ``json_extract_scalar(avail, '$.filledduration')``.

    Views: ``v_breaks`` (one row per aired break by SCTE-35 event id) and ``v_fill_by_pc`` (per playback
    configuration and hour). The workgroup enforces its settings, keeps results as Athena managed query results and
    cancels queries above :data:`QUERY_SCAN_LIMIT_BYTES`.

    Args:
        prefix: deployment prefix.
        lake_bucket: the :class:`LakeBucket`.
        logs_bucket: holds the CloudFront Parquet logs (``CdnAccessLogs``).
        distribution_id: the media distribution, whose logs the CloudFront table reads.
        playback_configurations: names of the IaC playback configurations whose logs are copied (their delivery
            sources ``<name>-ads`` and ``<name>-manifest`` exist in the Media stack).
        retention_days: lake retention, which bounds the projected partitions.
        removal_policy: what happens to the database and tables when the stack is deleted.

    Attributes:
        database: the Glue database. workgroup: the Athena workgroup. tables: the tables by name.
        deliveries: the log copies' deliveries.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        lake_bucket: s3.IBucket,
        logs_bucket: s3.IBucket,
        distribution_id: str,
        playback_configurations: Sequence[str],
        retention_days: int,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.database_name = lake_database_name(prefix)
        self.database = glue.Database(
            self,
            "Database",
            database_name=self.database_name,
            description=f"FAST Channels ({prefix}) lake: CDN and MediaTailor logs",
            removal_policy=removal_policy,
        )
        self.tables: dict[str, glue.S3Table] = {}
        access = f"AWSLogs/aws-account-id={stack.account}/CloudFront/DistributionId={distribution_id}"
        self.tables["cloudfront_access"] = glue.S3Table(
            self,
            "CloudFrontAccess",
            database=self.database,
            table_name="cloudfront_access",
            description="CloudFront standard logs v2 of the media distribution (no c-ip, cs-uri-query, "
            "x-forwarded-for)",
            storage=glue.S3TableStorage.from_bucket(logs_bucket),
            s3_prefix=f"{access}/",
            data_format=glue.DataFormat.PARQUET,
            columns=[
                glue.Column(name=_parquet_column(f), type=glue.Schema.STRING)
                for f in ACCESS_LOG_FIELDS
                if f not in EXCLUDED_ACCESS_LOG_FIELDS
            ],
            partition_keys=[glue.Column(name=key, type=glue.Schema.STRING) for key in ("year", "month", "day", "hour")],
            partition_projection={
                # This year and last: the logs bucket keeps CloudFront logs for days, not years.
                "year": glue.PartitionProjectionConfiguration.date(
                    format="yyyy",
                    min="NOW-1YEARS",
                    max="NOW",
                    step=glue.DateProjectionStep(interval=1, interval_unit=glue.DateIntervalUnit.YEARS),
                ),
                "month": glue.PartitionProjectionConfiguration.integer(min=1, max=12, digits=2),
                "day": glue.PartitionProjectionConfiguration.integer(min=1, max=31, digits=2),
                "hour": glue.PartitionProjectionConfiguration.integer(min=0, max=23, digits=2),
            },
            parameters={
                "storage.location.template": Fn.join(
                    "",
                    [
                        f"s3://{logs_bucket.bucket_name}/{access}/",
                        "year=${year}/month=${month}/day=${day}/hour=${hour}/",
                    ],
                )
            },
        )
        self.deliveries: list[logs.CfnDelivery] = []
        previous: logs.CfnDelivery | None = None
        for log in LAKE_LOGS:
            path = f"{MEDIATAILOR_PREFIX}/{log.suffix}"
            destination = logs.CfnDeliveryDestination(
                self,
                f"{log.suffix.title()}Destination",
                name=f"{prefix}-lake-{log.suffix}",
                destination_resource_arn=f"{lake_bucket.bucket_arn}/{path}",
                output_format="json",
            )
            for configuration in playback_configurations:
                previous = deliver(
                    self,
                    f"{log.suffix.title()}Delivery-{configuration}",
                    source=f"{configuration}-{log.suffix}",
                    destination_arn=destination.attr_arn,
                    after=previous,
                    record_fields=list(log.fields),
                    s3_suffix_path=_SUFFIX_PATH,
                    s3_enable_hive_compatible_path=False,
                )
                self.deliveries.append(previous)
            location = f"s3://{lake_bucket.bucket_name}/{path}/AWSLogs/{stack.account}/{log.service_dir}/"
            self.tables[log.table] = glue.S3Table(
                self,
                log.table.title().replace("_", ""),
                database=self.database,
                table_name=log.table,
                description=log.description,
                storage=glue.S3TableStorage.from_bucket(lake_bucket),
                s3_prefix=f"{path}/",
                data_format=glue.DataFormat.JSON,
                columns=[glue.Column(name=field.lower(), type=glue.Schema.STRING) for field in log.fields],
                partition_keys=[glue.Column(name="datehour", type=glue.Schema.STRING)],
                partition_projection={
                    "datehour": glue.PartitionProjectionConfiguration.date(
                        format=_DATEHOUR_FORMAT,
                        min=f"NOW-{retention_days}DAYS",
                        max="NOW+1HOURS",
                        step=glue.DateProjectionStep(interval=1, interval_unit=glue.DateIntervalUnit.HOURS),
                    )
                },
                parameters={"storage.location.template": f"{location}${{datehour}}/"},
            )
        self.workgroup = athena.CfnWorkGroup(
            self,
            "WorkGroup",
            name=f"{prefix}-lake",
            description=f"FAST Channels ({prefix}) lake queries",
            recursive_delete_option=True,
            state="ENABLED",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                bytes_scanned_cutoff_per_query=QUERY_SCAN_LIMIT_BYTES,
                publish_cloud_watch_metrics_enabled=True,
                engine_version=athena.CfnWorkGroup.EngineVersionProperty(
                    selected_engine_version="Athena engine version 3"
                ),
                managed_query_results_configuration=athena.CfnWorkGroup.ManagedQueryResultsConfigurationProperty(
                    enabled=True
                ),
            ),
        )
        self.views = [self._view(view) for view in VIEWS]

    def _view(self, view: View) -> glue.CfnTable:
        """``view`` as Athena stores a ``CREATE VIEW``: a ``VIRTUAL_VIEW`` table whose original text is the
        base64 Presto view definition."""
        sql = view.sql.format(db=self.database_name).strip()
        definition = {
            "originalSql": sql,
            "catalog": "awsdatacatalog",
            "schema": self.database_name,
            "columns": [{"name": name, "type": kind} for name, kind in view.columns],
            "runAsInvoker": False,
            "properties": {},
        }
        encoded = base64.b64encode(json.dumps(definition, separators=(",", ":")).encode()).decode()
        table = glue.CfnTable(
            self,
            view.name.title().replace("_", ""),
            catalog_id=Stack.of(self).account,
            database_name=self.database_name,
            table_input=glue.CfnTable.TableInputProperty(
                name=view.name,
                description=view.description,
                table_type="VIRTUAL_VIEW",
                parameters={"presto_view": "true", "comment": "Presto View"},
                view_original_text=f"/* Presto View: {encoded} */",
                view_expanded_text="/* Presto View */",
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    columns=[
                        glue.CfnTable.ColumnProperty(name=name, type=_HIVE_TYPES[kind]) for name, kind in view.columns
                    ],
                    serde_info=glue.CfnTable.SerdeInfoProperty(),
                ),
            ),
        )
        table.add_resource_dependency(self.database.node.default_child)  # type: ignore[arg-type]
        for source in self.tables.values():
            table.node.add_dependency(source)
        return table


def _parquet_column(field: str) -> str:
    """CloudFront's Parquet column name of a standard-logs field: ``cs(User-Agent)`` -> ``cs_User_Agent``,
    ``timestamp(ms)`` -> ``timestamp_ms``, lowercased for Glue."""
    return field.replace("(", "_").replace(")", "").replace("-", "_").lower()
