"""Delivery and the release sweeper of the own book's reservations (ADR-0022).

- ``DeliveryCounter``: a subscription filter on the stage's MediaTailor log group (``/aws/vendedlogs/mediatailor/
  <prefix>``, the Media stack's) sends the impression beacons (``BEACON_FIRED`` with ``trackingEvent`` impression) to
  ``fast_channels.functions.pds.delivery``, which keeps our tracker's and credits them to their reservations. Every 5
  minutes a schedule runs it on the CDN's logs too (``CdnAccessTable``, with Athena), which counts the firings the
  ADS log left out (W-2 1.32).
- ``ReleaseSweeper``: every 5 minutes ``fast_channels.functions.pds.sweeper`` releases the reservations that saw no
  impression in their hold (``core.pds.delivery.release_after``: 10 minutes with prefetch's default lead).

Both write money events to the ledger (chains ``pds/*`` only) and dead-letter what fails after its retries.
"""

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from aws_cdk import Duration, Fn, RemovalPolicy, Stack
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_logs_destinations as logs_destinations
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from constructs import Construct

from fast_channels.core.ledger import ledger_table
from fast_channels.core.naming import mediatailor_log_group
from fast_channels.core.pds.delivery import CDN_LOG_EVERY, RELEASE_AFTER, SWEEP_EVERY, Source
from fast_channels.core.pds.names import (
    CDN_ACCESS_TABLE,
    DECISIONS_PREFIX,
    PENDING_INDEX,
    database_name,
    dead_letter_alarm,
    decision_table,
    table_name,
)
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs import alerts
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.ops.lake import (
    cloudfront_log_location,
    cloudfront_partitions,
    lake_database_name,
    lake_workgroup_name,
    parquet_column,
)
from fast_channels.infra.constructs.queues import dead_letter_queue

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aws_cdk import aws_s3 as s3
    from aws_cdk import aws_sns as sns
    from aws_cdk import aws_sqs as sqs

DELIVERY_HANDLER: Final = "fast_channels.functions.pds.delivery.handler"
SWEEPER_HANDLER: Final = "fast_channels.functions.pds.sweeper.handler"
SWEEP_SCHEDULE: Final = f"rate({SWEEP_EVERY.seconds // 60} minutes)"
RETRY_ATTEMPTS: Final = 2
MAX_EVENT_AGE: Final = Duration.hours(6)
"""How long Lambda keeps retrying a failed batch of beacons before it is dead-lettered."""
IMPRESSIONS: Final = logs.FilterPattern.all(
    logs.FilterPattern.string_value("$.eventType", "=", "BEACON_FIRED"),
    logs.FilterPattern.string_value("$.beaconInfo.trackingEvent", "=", "impression"),
)
"""The ADS log events the delivery function reads: impression beacons (ours among them)."""
CDN_LOG_SCHEDULE: Final = f"rate({CDN_LOG_EVERY.seconds // 60} minutes)"
CDN_LOG_FIELDS: Final = ("timestamp(ms)", "cs-uri-stem", "cs-uri-query", "sc-status")
"""The standard-logs fields of ``CdnAccessTable``: what the CDN-log query reads (``core.pds.delivery``)."""
SELECT_TABLE: Final = decision_table("select")


def allow_table(function: FastFunction, prefix: str, actions: Sequence[str], keys: Sequence[str]) -> None:
    """Allow ``actions`` on the PDS table's items whose partition keys match ``keys`` (``BUDGET#*``, ...)."""
    function.allow(
        [f"dynamodb:{action}" for action in actions],
        [arn(function, "dynamodb", "table", table_name(prefix))],
        conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": list(keys)}},
    )


def allow_ledger(function: FastFunction, prefix: str) -> None:
    """Allow appending to the ledger's ``pds/*`` chains (a put of a new entry, never an update)."""
    function.allow(
        ["dynamodb:PutItem"],
        [arn(function, "dynamodb", "table", ledger_table(prefix))],
        conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["pds/*"]}},
    )


def _dead_lettered(function: FastFunction, queue_scope: Construct, *, removal_policy: RemovalPolicy) -> sqs.Queue:
    """A dead-letter queue for ``function``'s failed asynchronous invocations."""
    queue = dead_letter_queue(queue_scope, "DeadLetters", removal_policy=removal_policy)
    function.function.configure_async_invoke(
        on_failure=destinations.SqsDestination(queue),
        retry_attempts=RETRY_ATTEMPTS,
        max_event_age=MAX_EVENT_AGE,
    )
    return queue


class CdnAccessTable(Construct):
    """The PDS's Glue database ``<prefix>_pds`` and its one table, ``cdn_access``: the media distribution's CloudFront
    logs (``CdnAccessLogs``: Parquet in the logs bucket, partitioned by ``year``, ``month``, ``day``, ``hour``) with
    each request's time, path, query string and status (:data:`CDN_LOG_FIELDS`), for the delivery function's CDN-log
    runs. The lake's ``cloudfront_access`` leaves the query string out (it carries session parameters), so this table
    is not in the lake.

    Args:
        prefix: the stage's prefix.
        logs_bucket: the logs bucket. distribution_id: the media distribution.
        removal_policy: of the database.

    Attributes:
        database, table. location: the logs' key prefix in the bucket (``AWSLogs/.../DistributionId=<id>``).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        logs_bucket: s3.IBucket,
        distribution_id: str,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.location = cloudfront_log_location(Stack.of(self).account, distribution_id)
        self.database = glue.Database(
            self,
            "Database",
            database_name=database_name(prefix),
            description=f"FAST Channels ({prefix}) Pod Decision Service: the delivery function's tables (not the lake)",
            removal_policy=removal_policy,
        )
        self.table = glue.S3Table(
            self,
            "Table",
            database=self.database,
            table_name=CDN_ACCESS_TABLE,
            description="CloudFront standard logs v2 of the media distribution: time, path, query string and status "
            "(the delivery function reads our tracker's requests)",
            storage=glue.S3TableStorage.from_bucket(logs_bucket),
            s3_prefix=f"{self.location}/",
            data_format=glue.DataFormat.PARQUET,
            columns=[glue.Column(name=parquet_column(f), type=glue.Schema.STRING) for f in CDN_LOG_FIELDS],
            partition_keys=[glue.Column(name=key, type=glue.Schema.STRING) for key in ("year", "month", "day", "hour")],
            partition_projection=cloudfront_partitions(),
            parameters={
                "storage.location.template": Fn.join(
                    "",
                    [
                        f"s3://{logs_bucket.bucket_name}/{self.location}/",
                        "year=${year}/month=${month}/day=${day}/hour=${hour}/",
                    ],
                )
            },
        )


class DeliveryCounter(Construct):
    """Our tracker's impressions from the MediaTailor log group and, every 5 minutes, the CDN's logs (module
    docstring).

    Args:
        prefix: the stage's prefix (its log group ``/aws/vendedlogs/mediatailor/<prefix>``, its table, its ledger,
            the lake's database and workgroup).
        content_base_url: the CDN's content base URL, whose host our tracker is on.
        group: the PDS's schedule group (the CDN-log runs).
        cdn_access: the table of the CDN's logs; logs_bucket: its bucket.
        lake_bucket: the lake bucket, which holds the ``pds.select`` records the CDN-log query joins.
        alert_topic: topic of the dead-letter alarm.
        log_retention, removal_policy: of the function's log group and the dead-letter queue.

    Attributes:
        function: the delivery function. subscription: its filter on the log group. schedule: its CDN-log runs.
        dead_letters, alarm.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        content_base_url: str,
        group: scheduler.IScheduleGroup,
        cdn_access: CdnAccessTable,
        logs_bucket: s3.IBucket,
        lake_bucket: s3.IBucket,
        alert_topic: sns.ITopic,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.function = FastFunction(
            self,
            "Function",
            compiled=True,  # the Pds stack's one code asset: precompiled, for the endpoints' cold starts
            handler=DELIVERY_HANDLER,
            description="PDS: count our tracker's impressions (BEACON_FIRED, the CDN's logs) and deliver reservations",
            environment={
                "FAST_PREFIX": prefix,
                "PDS_TABLE": table_name(prefix),
                "LEDGER_TABLE": ledger_table(prefix),
                "CONTENT_BASE_URL": content_base_url,
                "LAKE_WORKGROUP": lake_workgroup_name(prefix),
                "CDN_LOG_TABLE": f"{database_name(prefix)}.{CDN_ACCESS_TABLE}",
                "SELECT_TABLE": f"{lake_database_name(prefix)}.{SELECT_TABLE}",
            },
            timeout=Duration.minutes(3),  # a CDN-log run waits for its Athena query
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        self.dead_letters = _dead_lettered(self.function, self, removal_policy=removal_policy)
        nag.acknowledge(
            self.function,
            {
                nag.LAMBDA_DLQ: "Invoked asynchronously by CloudWatch Logs and EventBridge Scheduler; failed batches "
                "and runs go to the DeadLetters queue"
            },
        )
        allow_table(self.function, prefix, ["PutItem"], ["IMP#*", "FILL#*"])
        allow_table(self.function, prefix, ["GetItem", "UpdateItem"], ["RES#*", "BUDGET#*", "CAP#*"])
        allow_ledger(self.function, prefix)
        self._allow_cdn_log_query(prefix, cdn_access, logs_bucket=logs_bucket, lake_bucket=lake_bucket)
        log_group = logs.LogGroup.from_log_group_name(self, "AdsLogs", mediatailor_log_group(prefix))
        self.subscription = logs.SubscriptionFilter(
            self,
            "Impressions",
            log_group=log_group,
            destination=logs_destinations.LambdaDestination(self.function.function),
            filter_pattern=IMPRESSIONS,
            filter_name=f"{prefix}-pds-delivery",
        )
        self.schedule = self._cdn_log_runs(group)
        self.alarm = alerts.dead_letter_alarm(
            self,
            "DeadLettersAlarm",
            queue=self.dead_letters,
            name=dead_letter_alarm(prefix, "delivery"),
            description="Beacons from the MediaTailor log, or a window of the CDN's logs, could not be counted after "
            "their retries: impressions and deliveries are missing (redrive the queue)",
            topic=alert_topic,
        )

    def _allow_cdn_log_query(
        self, prefix: str, cdn_access: CdnAccessTable, *, logs_bucket: s3.IBucket, lake_bucket: s3.IBucket
    ) -> None:
        """The CDN-log query: in the lake's workgroup, on the CDN's logs and the ``pds.select`` records only."""
        self.function.allow(
            ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults"],
            [arn(self, "athena", "workgroup", lake_workgroup_name(prefix))],
        )
        cdn_database, lake = database_name(prefix), lake_database_name(prefix)
        self.function.allow(
            ["glue:GetDatabase", "glue:GetTable"],
            [
                Stack.of(self).format_arn(service="glue", resource="catalog"),
                arn(self, "glue", "database", cdn_database),
                arn(self, "glue", "database", lake),
                arn(self, "glue", "table", f"{cdn_database}/{CDN_ACCESS_TABLE}"),
                arn(self, "glue", "table", f"{lake}/{SELECT_TABLE}"),
            ],
        )
        logs_keys, select_keys = f"{cdn_access.location}/*", f"{DECISIONS_PREFIX}select/*"
        self.function.allow(
            ["s3:GetObject"],
            [logs_bucket.arn_for_objects(logs_keys), lake_bucket.arn_for_objects(select_keys)],
            why="Athena reads the CDN's log objects and the select records under their generated keys",
        )
        for bucket, keys in ((logs_bucket, logs_keys), (lake_bucket, select_keys)):
            self.function.allow(
                ["s3:ListBucket", "s3:GetBucketLocation"],
                [bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": [keys]}},
            )

    # OPTIMIZATION(aws): W-2 1.32 the ADS log can leave out a filled break: delete this schedule when fixed
    def _cdn_log_runs(self, group: scheduler.IScheduleGroup) -> scheduler.Schedule:
        """Every 5 minutes, the function's CDN-log run with its scheduled time, from which it takes its window."""
        fn = self.function.function
        role = iam.Role(
            self,
            "ScheduleRole",
            description="EventBridge Scheduler: run the PDS delivery function on the CDN's logs",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Stack.of(self).account},
                    "ArnEquals": {"aws:SourceArn": group.schedule_group_arn},
                },
            ),
        )
        schedule = scheduler.Schedule(
            self,
            "CdnLogRuns",
            schedule=scheduler.ScheduleExpression.expression(CDN_LOG_SCHEDULE),
            schedule_group=group,
            description="PDS: count our tracker's impressions in the CDN's logs (W-2 1.32)",
            target=scheduler_targets.LambdaInvoke(
                fn,
                input=scheduler.ScheduleTargetInput.from_object(
                    {"source": Source.CDN_LOG.value, "scheduled": scheduler.ContextAttribute.scheduled_time}
                ),
                retry_attempts=RETRY_ATTEMPTS,
                max_event_age=Duration.minutes(15),
                dead_letter_queue=self.dead_letters,
                role=role,
            ),
        )
        nag.acknowledge_references(
            role,
            {nag.wildcard_resource(f"{fn.function_arn}:*"): "Scheduler L2 grant: invoke the function and its versions"},
        )
        return schedule


class ReleaseSweeper(Construct):
    """Releases the reservations that saw no impression in their hold, every 5 minutes (module docstring).

    Args:
        prefix: the stage's prefix (its table and ledger).
        hold: how long a reservation waits for its impression (``core.pds.delivery.release_after``).
        group: the PDS's schedule group.
        alert_topic: topic of the dead-letter alarm.
        log_retention, removal_policy: of the function's log group and the dead-letter queue.

    Attributes:
        function: the sweeper. schedule: its runs. dead_letters, alarm.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        group: scheduler.IScheduleGroup,
        alert_topic: sns.ITopic,
        log_retention: logs.RetentionDays,
        hold: timedelta = RELEASE_AFTER,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        minutes = int(hold.total_seconds() // 60)
        self.function = FastFunction(
            self,
            "Function",
            compiled=True,  # the Pds stack's one code asset: precompiled, for the endpoints' cold starts
            handler=SWEEPER_HANDLER,
            description=f"PDS: release reservations that saw no impression in {minutes} minutes",
            environment={
                "FAST_PREFIX": prefix,
                "PDS_TABLE": table_name(prefix),
                "LEDGER_TABLE": ledger_table(prefix),
                "PDS_RELEASE_AFTER_SECONDS": str(int(hold.total_seconds())),
            },
            timeout=Duration.minutes(4),  # under SWEEP_EVERY: runs never overlap; the next carries on
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        fn = self.function.function
        self.dead_letters = _dead_lettered(self.function, self, removal_policy=removal_policy)
        nag.acknowledge(
            self.function,
            {nag.LAMBDA_DLQ: "Invoked by EventBridge Scheduler; failed runs go to the DeadLetters queue"},
        )
        self.function.allow(
            ["dynamodb:Query"],
            [f"{arn(self, 'dynamodb', 'table', table_name(prefix))}/index/{PENDING_INDEX}"],
        )
        allow_table(self.function, prefix, ["GetItem", "UpdateItem"], ["RES#*", "BUDGET#*", "CAP#*"])
        allow_ledger(self.function, prefix)
        role = iam.Role(
            self,
            "ScheduleRole",
            description="EventBridge Scheduler: run the PDS release sweeper and dead-letter failed runs",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Stack.of(self).account},
                    "ArnEquals": {"aws:SourceArn": group.schedule_group_arn},
                },
            ),
        )
        self.schedule = scheduler.Schedule(
            self,
            "Runs",
            schedule=scheduler.ScheduleExpression.expression(SWEEP_SCHEDULE),
            schedule_group=group,
            description=f"PDS: release reservations with no impression after {minutes} minutes",
            target=scheduler_targets.LambdaInvoke(
                fn,
                retry_attempts=RETRY_ATTEMPTS,
                max_event_age=Duration.minutes(15),
                dead_letter_queue=self.dead_letters,
                role=role,
            ),
        )
        nag.acknowledge_references(
            role,
            {nag.wildcard_resource(f"{fn.function_arn}:*"): "Scheduler L2 grant: invoke the function and its versions"},
        )
        self.alarm = alerts.dead_letter_alarm(
            self,
            "DeadLettersAlarm",
            queue=self.dead_letters,
            name=dead_letter_alarm(prefix, "sweeper"),
            description="A PDS release sweep failed after its retries: unreserved budget stays held until a later run",
            topic=alert_topic,
        )
