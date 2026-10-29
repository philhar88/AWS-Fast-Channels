"""``DecisionLog``: one kind of PDS decision record, from its function's log group into the lake.

The function logs each record as a Powertools JSON line (``"record": "pds.<kind>"``). A subscription filter on that
field sends the lines to a Firehose stream, which decompresses the CloudWatch Logs batches, keeps each message (a
function's log message ends with its newline, so no delimiter is added: that would make every other line empty) and
writes gzip JSON lines under ``pds/<kind>/<yyyy>/<MM>/<dd>/<HH>/`` in the lake bucket (within a minute or 5 MiB).
The table ``pds_<kind>`` in the lake's Glue database reads them, partitioned by ``datehour`` (partition projection
over the lake's retention, as the other lake tables are).
"""

import types
from typing import TYPE_CHECKING, Any, Final, Literal, Union, cast, get_args, get_origin

from aws_cdk import Duration, RemovalPolicy, Size, Stack
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kinesisfirehose as firehose
from aws_cdk import aws_logs as logs
from aws_cdk import aws_logs_destinations as logs_destinations
from constructs import Construct

from fast_channels.core.pds.names import DECISIONS_PREFIX, decision_stream, decision_table
from fast_channels.infra import nag
from fast_channels.infra.constructs.ops.lake import lake_database_name

if TYPE_CHECKING:
    from aws_cdk import aws_s3 as s3
    from pydantic import BaseModel

DATEHOUR_FORMAT: Final = "yyyy/MM/dd/HH"
LOG_FIELDS: Final = (
    ("timestamp", glue.Schema.STRING),
    ("correlation_id", glue.Schema.STRING),
    ("xray_trace_id", glue.Schema.STRING),
    ("snapshot", glue.Schema.STRING),
)
"""What Powertools and the handler add to each record line."""


_SCALARS: Final[tuple[tuple[type, glue.Type], ...]] = (
    (bool, glue.Schema.BOOLEAN),  # before int: bool is an int
    (int, glue.Schema.BIG_INT),  # IntEnum too
    (float, glue.Schema.DOUBLE),
)


def _column_type(annotation: Any) -> glue.Type:
    """Glue type of a record field (JSON SerDe): optional fields take their type, literals their value's, lists
    become arrays, and objects (and strings, enums) strings: the SerDe reads an object as its JSON text."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        (inner,) = [arg for arg in args if arg is not type(None)]
        return _column_type(inner)
    if origin is Literal:
        return _column_type(type(args[0]))
    if origin is list:
        return glue.Schema.array(_column_type(args[0]))
    scalar = isinstance(annotation, type)
    kind = next((kind for base, kind in _SCALARS if scalar and issubclass(annotation, base)), None)
    return kind or cast("glue.Type", glue.Schema.STRING)


def record_columns(model: type[BaseModel]) -> list[glue.Column]:
    """The lake columns of a decision record model, then :data:`LOG_FIELDS`."""
    fields = [glue.Column(name=name, type=_column_type(info.annotation)) for name, info in model.model_fields.items()]
    return fields + [glue.Column(name=name, type=kind) for name, kind in LOG_FIELDS]


class DecisionLog(Construct):
    """One kind of decision record into the lake (module docstring).

    Args:
        prefix: the stage's prefix.
        kind: the record kind (``policy``, ``select``).
        model: its pydantic model (``core.pds.records``), whose fields are the table's columns.
        log_group: the log group of the function that writes it.
        lake_bucket: the ops module's lake bucket.
        retention_days: the lake's retention (``modules.ops.lake_retention_days``), which bounds the partitions.
        log_retention, removal_policy: of the stream's error log group.

    Attributes:
        stream: the Firehose stream. subscription: the log subscription. table: the Glue table.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        kind: str,
        model: type[BaseModel],
        log_group: logs.ILogGroup,
        lake_bucket: s3.IBucket,
        retention_days: int,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        path = f"{DECISIONS_PREFIX}{kind}/"
        stream_logs = logs.LogGroup(
            self,
            "StreamLogs",
            log_group_name=f"/aws/vendedlogs/firehose/{decision_stream(prefix, kind)}",
            retention=log_retention,
            removal_policy=removal_policy,
        )
        role = iam.Role(
            self,
            "StreamRole",
            assumed_by=iam.ServicePrincipal(
                "firehose.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": stack.account}}
            ),
            description=f"FAST Channels ({prefix}): deliver pds.{kind} decision records to the lake",
        )
        self.stream = firehose.DeliveryStream(
            self,
            "Stream",
            delivery_stream_name=decision_stream(prefix, kind),
            encryption=firehose.StreamEncryption.aws_owned_key(),
            destination=firehose.S3Bucket(
                lake_bucket,
                role=role,
                data_output_prefix=f"{path}!{{timestamp:yyyy/MM/dd/HH}}/",
                error_output_prefix=f"{DECISIONS_PREFIX}errors/{kind}/!{{firehose:error-output-type}}/"
                "!{timestamp:yyyy/MM/dd}/",
                buffering_interval=Duration.seconds(60),
                buffering_size=Size.mebibytes(5),
                compression=firehose.Compression.GZIP,
                processors=[
                    firehose.DecompressionProcessor(),
                    firehose.CloudWatchLogProcessor(data_message_extraction=True),
                ],
                logging_config=firehose.EnableLogging(stream_logs),
            ),
        )
        nag.acknowledge_references(
            role,
            {
                nag.wildcard_action(action): "Firehose's S3 destination grants read-write on its bucket"
                for action in ("s3:Abort*", "s3:DeleteObject*", "s3:GetBucket*", "s3:GetObject*", "s3:List*")
            }
            | {
                nag.wildcard_resource(lake_bucket.arn_for_objects("*")): "Firehose writes objects under generated keys",
                nag.wildcard_resource(f"{stream_logs.log_group_arn}:*"): "Firehose writes its error log streams",
            },
        )
        self.subscription = logs.SubscriptionFilter(
            self,
            "Records",
            log_group=log_group,
            destination=logs_destinations.FirehoseDestination(self.stream),
            filter_pattern=logs.FilterPattern.string_value("$.record", "=", f"pds.{kind}"),
            filter_name=f"{prefix}-pds-{kind}-records",
        )
        location = f"s3://{lake_bucket.bucket_name}/{path}"
        database = glue.Database.from_database_arn(
            self,
            "Lake",
            stack.format_arn(service="glue", resource="database", resource_name=lake_database_name(prefix)),
        )
        self.table = glue.S3Table(
            self,
            "Table",
            database=database,
            table_name=decision_table(kind),
            description=f"The Pod Decision Service's pds.{kind} decision records (core.pds.records)",
            storage=glue.S3TableStorage.from_bucket(lake_bucket),
            s3_prefix=path,
            data_format=glue.DataFormat.JSON,
            columns=record_columns(model),
            partition_keys=[glue.Column(name="datehour", type=glue.Schema.STRING)],
            partition_projection={
                "datehour": glue.PartitionProjectionConfiguration.date(
                    format=DATEHOUR_FORMAT,
                    min=f"NOW-{retention_days}DAYS",
                    max="NOW+1HOURS",
                    step=glue.DateProjectionStep(interval=1, interval_unit=glue.DateIntervalUnit.HOURS),
                )
            },
            parameters={"storage.location.template": f"{location}${{datehour}}/"},
        )
