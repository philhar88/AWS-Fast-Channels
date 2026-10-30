"""The PDS's private config bucket and the snapshot publisher.

The bucket is never served by the CDN and never the ingest bucket: only the PDS's functions read it and only the
publishers write it. The snapshot publisher (``fast_channels.functions.pds.publisher``) runs when the stack deploys
(a CDK trigger, again whenever the function or its configuration changes) and every hour. It reads the default
ladder's kit from the media bucket (only its VAST pod and demo index) and sets the own book's budget shards.
"""

from typing import TYPE_CHECKING, Final

from aws_cdk import Duration, RemovalPolicy, Stack, triggers
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from constructs import Construct

from fast_channels.core.ledger import ledger_table
from fast_channels.core.pds.names import Parameter, parameter_name, publisher_alarm, table_name
from fast_channels.core.pds.snapshot import HISTORY_PREFIX, SNAPSHOT_KEY, SnapshotBase
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.alerts import dead_letter_alarm
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.pds.delivery import allow_ledger, allow_table
from fast_channels.infra.constructs.queues import dead_letter_queue

if TYPE_CHECKING:
    from aws_cdk import aws_logs as logs
    from aws_cdk import aws_sns as sns

HANDLER: Final = "fast_channels.functions.pds.publisher.handler"
SCHEDULE: Final = "rate(1 hour)"
HISTORY_DAYS: Final = 30
"""Published snapshots are kept for audit this long; the bucket's old object versions for a week."""
RETRY_ATTEMPTS: Final = 2
MAX_EVENT_AGE: Final = Duration.hours(1)
"""An older run is superseded by the next scheduled one."""


class ConfigBucket(Construct):
    """The private bucket (SSE-S3, TLS only, versioned, access-logged to the logs bucket).

    Args:
        logs_bucket: the deployment's access-log bucket.
        removal_policy: what happens to the bucket and its objects when the stack is deleted.

    Attributes:
        bucket: the bucket.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        logs_bucket: s3.IBucket,
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
            server_access_logs_prefix="s3/pds-config/",
            target_object_key_format=s3.TargetObjectKeyFormat.partitioned_prefix(s3.PartitionDateSource.EVENT_TIME),
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-old-versions",
                    noncurrent_version_expiration=Duration.days(7),
                    expired_object_delete_marker=True,
                ),
                s3.LifecycleRule(id="expire-history", prefix=HISTORY_PREFIX, expiration=Duration.days(HISTORY_DAYS)),
                s3.LifecycleRule(id="abort-uploads", abort_incomplete_multipart_upload_after=Duration.days(1)),
            ],
        )


class SnapshotPublisher(Construct):
    """The snapshot publisher function, its deploy-time trigger and its hourly schedule.

    Args:
        prefix: the stage's prefix.
        bucket: the config bucket.
        media_bucket: the media bucket (the ladder kits).
        base: what the deployment configures in the snapshot.
        alert_topic: topic of the dead-letter alarm.
        log_retention, removal_policy: of the function's log group and the dead-letter queue.

    Attributes:
        function: the publisher. group: the PDS's schedule group. schedule: the hourly run. dead_letters: failed
            scheduled runs.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        bucket: s3.IBucket,
        media_bucket: s3.IBucket,
        base: SnapshotBase,
        alert_topic: sns.ITopic,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.dead_letters = dead_letter_queue(self, "DeadLetters", removal_policy=removal_policy)
        self.function = FastFunction(
            self,
            "Function",
            compiled=True,  # the Pds stack's one code asset: precompiled, for the endpoints' cold starts
            handler=HANDLER,
            description="PDS: publish the snapshot (demand, floor, CloudFront origin-facing networks, own book) "
            "and the lines' budgets",
            environment={
                "FAST_PREFIX": prefix,
                "PDS_CONFIG_BUCKET": bucket.bucket_name,
                "PDS_SNAPSHOT_BASE": base.model_dump_json(),
                "MEDIA_BUCKET": media_bucket.bucket_name,
                "PDS_TABLE": table_name(prefix),
                "LEDGER_TABLE": ledger_table(prefix),
            },
            timeout=Duration.minutes(1),
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        fn = self.function.function
        fn.configure_async_invoke(
            on_failure=destinations.SqsDestination(self.dead_letters),
            retry_attempts=RETRY_ATTEMPTS,
            max_event_age=MAX_EVENT_AGE,
        )
        nag.acknowledge(
            self.function,
            {
                nag.LAMBDA_DLQ: "Failed scheduled runs go to the DeadLetters queue; the deploy-time run fails the "
                "deployment",
            },
        )
        self.function.allow(["s3:GetObject"], [bucket.arn_for_objects(SNAPSHOT_KEY)])
        self.function.allow(
            ["s3:PutObject"],
            [bucket.arn_for_objects(SNAPSHOT_KEY), bucket.arn_for_objects(f"{HISTORY_PREFIX}*")],
            why=f"Each published version is kept under {HISTORY_PREFIX}<version>.json",
        )
        # A GetObject of a missing key is a 404 only for a caller with ListBucket on the bucket (S3 evaluates no
        # s3:prefix for it), so the first publish can tell "none yet" from "denied". The bucket holds only snapshots.
        self.function.allow(["s3:ListBucket"], [bucket.bucket_arn])
        self.function.allow(
            ["s3:GetObject"],
            [media_bucket.arn_for_objects("ads/*/vast.xml"), media_bucket.arn_for_objects("ads/*/demo/creatives.json")],
            why="The own book's creatives: each ladder kit's VAST pod and demo index (ads/<ladder>/...)",
        )
        self.function.allow(
            ["ssm:GetParameter"],
            [arn(self, "ssm", "parameter", parameter_name(prefix, Parameter.LADDER_VERSION).lstrip("/"))],
        )
        allow_table(self.function, prefix, ["GetItem", "PutItem", "UpdateItem"], ["BUDGET#*"])
        allow_ledger(self.function, prefix)
        self.trigger = triggers.Trigger(
            self,
            "OnDeploy",
            handler=fn,
            invocation_type=triggers.InvocationType.REQUEST_RESPONSE,
            timeout=Duration.minutes(1),
            execute_on_handler_change=True,
        )
        self.group = group = scheduler.ScheduleGroup(
            self, "Group", schedule_group_name=f"{prefix}-pds", removal_policy=RemovalPolicy.DESTROY
        )
        role = iam.Role(
            self,
            "ScheduleRole",
            description="EventBridge Scheduler: run the PDS snapshot publisher and dead-letter failed runs",
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
            "Hourly",
            schedule=scheduler.ScheduleExpression.expression(SCHEDULE),
            schedule_group=group,
            description="PDS: republish the snapshot (CloudFront's origin-facing networks change without notice)",
            target=scheduler_targets.LambdaInvoke(
                fn,
                retry_attempts=RETRY_ATTEMPTS,
                max_event_age=MAX_EVENT_AGE,
                dead_letter_queue=self.dead_letters,
                role=role,
            ),
        )
        nag.acknowledge_references(
            role,
            {nag.wildcard_resource(f"{fn.function_arn}:*"): "Scheduler L2 grant: invoke the function and its versions"},
        )
        self.alarm = dead_letter_alarm(
            self,
            "DeadLettersAlarm",
            queue=self.dead_letters,
            name=publisher_alarm(prefix),
            description="A scheduled PDS snapshot publish failed after its retries: the snapshot may be stale",
            topic=alert_topic,
        )
