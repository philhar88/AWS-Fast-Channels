"""The catalog context publisher: one JSON object per catalog title in the media bucket, served by the CDN."""

from typing import TYPE_CHECKING, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from constructs import Construct

from fast_channels.core.crawl.catalog_context import CONTEXT_PREFIX
from fast_channels.core.events import DETAIL_TYPES, CatalogItemChanged, SchedulePublished, event_source
from fast_channels.infra import nag
from fast_channels.infra.constructs.alerts import dead_letter_alarm
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.queues import dead_letter_queue

if TYPE_CHECKING:
    from aws_cdk import aws_cloudwatch as cw
    from aws_cdk import aws_logs as logs
    from aws_cdk import aws_s3 as s3
    from aws_cdk import aws_sns as sns

    from fast_channels.infra.exports import DataExports

HANDLER: Final = "fast_channels.functions.crawl.publisher.handler"
TIMEOUT: Final = Duration.minutes(5)
"""The daily sweep reads the whole catalog with one HEAD per title."""
SWEEP_SCHEDULE: Final = "rate(1 day)"
RETRY_ATTEMPTS: Final = 2
MAX_EVENT_AGE: Final = Duration.hours(6)
"""An older trigger is superseded by the next sweep."""
_TABLE_READ: Final = ("dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan")


class ContextPublisher(Construct):
    """A Lambda that keeps ``catalog/ctx/v1/<content_id>.json`` in the catalog-context bucket in step with the
    reference catalog (``fast_channels.functions.crawl.publisher``).

    Three triggers: ``Schedule Published`` and ``Catalog Item Changed`` from the deployment's bus, and a daily sweep
    from EventBridge Scheduler. Each delivery is retried, then dead-lettered to :attr:`dead_letters`, whose alarm
    notifies ``alert_topic``.

    The function reads the catalog and media library tables (items only), and reads, writes, deletes and lists objects
    under the context prefix of the catalog-context bucket and nowhere else: never the media or the ingest bucket.
    The CDN's ``/catalog/ctx/*`` route serves the objects, with the ``Cache-Control`` the function sets.

    Args:
        prefix: deployment prefix (event source, alarm and schedule names).
        data: Data stack references (the tables).
        bucket: the catalog-context bucket (``MediaExports.catalog_context_bucket``).
        event_bus: the deployment's bus.
        alert_topic: topic of the dead-letter alarm.
        publisher_domain: ``modules.crawl.publisher_domain``; None leaves it out of the objects.
        log_retention, removal_policy: of the function's log group and the dead-letter queue.

    Attributes:
        function: the publisher; dead_letters: its dead-letter queue; rule: the event rule; sweep: the schedule;
        alarm: the dead-letter alarm.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        data: DataExports,
        bucket: s3.IBucket,
        event_bus: events.IEventBus,
        alert_topic: sns.ITopic,
        publisher_domain: str | None,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.dead_letters = dead_letter_queue(self, "DeadLetters", removal_policy=removal_policy)
        self.function = FastFunction(
            self,
            "Function",
            handler=HANDLER,
            description="Crawl: publish catalog context objects (catalog/ctx/v1/) for the PRE_ADS lookup and the PDS",
            environment={
                "FAST_PREFIX": prefix,
                "CATALOG_TABLE": data.catalog_table.table_name,
                "LIBRARY_TABLE": data.library_table.table_name,
                "CATALOG_CONTEXT_BUCKET": bucket.bucket_name,
                **({"PUBLISHER_DOMAIN": publisher_domain} if publisher_domain else {}),
            },
            timeout=TIMEOUT,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        fn = self.function.function
        fn.configure_async_invoke(
            on_failure=destinations.SqsDestination(self.dead_letters),
            retry_attempts=RETRY_ATTEMPTS,
            max_event_age=MAX_EVENT_AGE,
        )
        nag.acknowledge(self.function, {nag.LAMBDA_DLQ: "Failed asynchronous invocations go to the DeadLetters queue"})
        self.function.allow(list(_TABLE_READ), [data.catalog_table.table_arn, data.library_table.table_arn])
        self.function.allow(
            ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            [bucket.arn_for_objects(f"{CONTEXT_PREFIX}*")],
            why=f"One catalog context object per title under {CONTEXT_PREFIX} (content ids are open-ended)",
        )
        # Listing finds orphans in the sweep, and turns the HEAD of a missing object into a 404 instead of a 403.
        self.function.allow(
            ["s3:ListBucket"],
            [bucket.bucket_arn],
            conditions={"StringLike": {"s3:prefix": [f"{CONTEXT_PREFIX}*"]}},
        )

        self.rule = events.Rule(
            self,
            "Triggers",
            description=f"FAST Channels ({prefix}): republish catalog context objects after a publish or a change",
            event_bus=event_bus,
            event_pattern=events.EventPattern(
                source=[event_source(prefix)],
                detail_type=[DETAIL_TYPES[SchedulePublished], DETAIL_TYPES[CatalogItemChanged]],
            ),
            targets=[
                events_targets.LambdaFunction(
                    fn,
                    dead_letter_queue=self.dead_letters,
                    retry_attempts=8,
                    max_event_age=MAX_EVENT_AGE,
                )
            ],
        )
        group = scheduler.ScheduleGroup(
            self, "Group", schedule_group_name=f"{prefix}-crawl", removal_policy=RemovalPolicy.DESTROY
        )
        role = iam.Role(
            self,
            "ScheduleRole",
            description="EventBridge Scheduler: invoke the catalog context sweep and dead-letter failed deliveries",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Stack.of(self).account},
                    "ArnEquals": {"aws:SourceArn": group.schedule_group_arn},
                },
            ),
        )
        self.sweep = scheduler.Schedule(
            self,
            "Sweep",
            schedule=scheduler.ScheduleExpression.expression(SWEEP_SCHEDULE),
            schedule_group=group,
            description="Crawl: publish every catalog title and remove the objects of titles that left the catalog",
            target=scheduler_targets.LambdaInvoke(
                fn,
                input=scheduler.ScheduleTargetInput.from_object({"sweep": True}),
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
        self.alarm: cw.Alarm = dead_letter_alarm(
            self,
            "DeadLettersAlarm",
            queue=self.dead_letters,
            name=f"{prefix}-crawl-publisher-dead-letters",
            description="A catalog context trigger failed after its retries; the objects may be stale until the sweep",
            topic=alert_topic,
        )
