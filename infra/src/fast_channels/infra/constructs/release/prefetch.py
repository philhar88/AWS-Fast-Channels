"""The Release Controller's prefetcher (M3-06): SINGLE prefetch schedules for the monetized channels' next breaks."""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from constructs import Construct

from fast_channels.core.events import DETAIL_TYPES, CensusChanged, SchedulePublished, event_source
from fast_channels.core.monetization import names
from fast_channels.infra import nag
from fast_channels.infra.constructs.alerts import alarm
from fast_channels.infra.constructs.functions import METRICS_NAMESPACE, FastFunction

if TYPE_CHECKING:
    from aws_cdk import aws_logs as logs
    from aws_cdk import aws_sns as sns

    from fast_channels.core.monetization.prefetch import PrefetchPolicy

HANDLER: Final = "fast_channels.functions.release.prefetch.handler"
SERVICE: Final = "release.prefetch"
INTERVAL: Final = Duration.minutes(5)
"""How often every running channel's schedules are brought in line; a re-plan or a channel start also triggers it."""
TIMEOUT: Final = Duration.minutes(2)
_READS: Final = ("mediatailor:DescribeChannel", "mediatailor:GetChannelSchedule")


class Prefetcher(Construct):
    """A Lambda that keeps the prefetching siblings' SINGLE prefetch schedules in step with their channels' schedules
    of record (``fast_channels.functions.release.prefetch``).

    It runs every :data:`INTERVAL` (EventBridge Scheduler) and on ``Schedule Published`` and ``Census Changed`` from
    the stage's bus. Runs are idempotent: a failed one is made good by the next, so there is no dead-letter queue; the
    alarm ``<prefix>-release-prefetch`` fires on any failure or refusal (a RECURRING schedule on a sibling).

    It may read the monetized channels, their schedules and programs, and the controller's state table, and create,
    list and delete prefetch schedules of the channels' siblings (``<channel>-p-*``): nothing else. The release guard
    exempts it from the stage's Deny of prefetch writes.

    Args:
        prefix: deployment prefix.
        channels: ``modules.release.channels``.
        letters: the sibling letters that prefetch.
        policy: ``modules.release.prefetch``'s timing.
        break_lengths_ms: the stage's break lengths (the default ladder's slate grid).
        state_table: the controller's state table (its channels' recorded siblings).
        event_bus: the stage's bus.
        alert_topic: the alarm's topic.
        log_retention, removal_policy: of the function's log group.

    Attributes:
        function: the prefetcher; rule: its event rule; schedule: its timer; alarm: its alarm.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        channels: Sequence[str],
        letters: Sequence[str],
        policy: PrefetchPolicy,
        break_lengths_ms: Sequence[int],
        state_table: str,
        event_bus: events.IEventBus,
        alert_topic: sns.ITopic,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.function = FastFunction(
            self,
            "Function",
            handler=HANDLER,
            description="Release Controller: SINGLE prefetch schedules for the monetized channels' next breaks",
            environment={
                "FAST_PREFIX": prefix,
                "RELEASE_TABLE": state_table,
                "RELEASE_CHANNELS": ",".join(channels),
                "PREFETCH_SIBLINGS": ",".join(letters),
                "PREFETCH_POLICY": policy.model_dump_json(),
                "BREAK_LENGTHS_MS": ",".join(str(ms) for ms in break_lengths_ms),
            },
            timeout=TIMEOUT,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        base = f"arn:{stack.partition}:mediatailor:{stack.region}:{stack.account}"
        self.function.allow(list(_READS), [f"{base}:channel/{c}" for c in channels])
        self.function.allow(
            ["mediatailor:DescribeProgram"],
            [f"{base}:program/{c}/*" for c in channels],
            why="The channels' programs, which their schedules name",
        )
        self.function.allow(
            ["mediatailor:ListPrefetchSchedules"],
            [f"{base}:prefetchSchedule/*"],
            why="ListPrefetchSchedules authorizes on prefetchSchedule/* whatever configuration it names (seen live)",
        )
        self.function.allow(
            ["mediatailor:CreatePrefetchSchedule", "mediatailor:DeletePrefetchSchedule", "mediatailor:TagResource"],
            [f"{base}:prefetchSchedule/{names.sibling_pattern(c)}/*" for c in channels],
            why="One schedule per upcoming break (pf-<event id>) on each sibling",
        )
        self.function.allow(
            ["dynamodb:Scan"],
            [stack.format_arn(service="dynamodb", resource="table", resource_name=state_table)],
        )
        fn = self.function.function
        nag.acknowledge(
            self.function, {nag.LAMBDA_DLQ: "Idempotent: the next run (every 5 minutes) redoes a failed one"}
        )

        self.rule = events.Rule(
            self,
            "Triggers",
            description=f"FAST Channels ({prefix}): prefetch schedules after a re-plan or a channel start",
            event_bus=event_bus,
            event_pattern=events.EventPattern(
                source=[event_source(prefix)],
                detail_type=[DETAIL_TYPES[SchedulePublished], DETAIL_TYPES[CensusChanged]],
            ),
            targets=[events_targets.LambdaFunction(fn, retry_attempts=2, max_event_age=Duration.minutes(10))],
        )
        nag.acknowledge(
            self.rule, {"Serverless-EventBusDLQ": "EventBridge retries it; the 5-minute run redoes a lost one"}
        )
        role = iam.Role(
            self,
            "ScheduleRole",
            description="EventBridge Scheduler: run the Release Controller's prefetcher",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": stack.account}}
            ),
        )
        nag.acknowledge_references(
            role,
            {nag.wildcard_resource(f"{fn.function_arn}:*"): "Scheduler L2 grant: the function's versions"},
        )
        self.schedule = scheduler.Schedule(
            self,
            "Schedule",
            description=f"Release prefetch schedules of {prefix} every 5 minutes",
            schedule=scheduler.ScheduleExpression.rate(INTERVAL),
            target=scheduler_targets.LambdaInvoke(
                fn,
                input=scheduler.ScheduleTargetInput.from_object({"scheduled_time": "<aws.scheduler.scheduled-time>"}),
                retry_attempts=0,
                role=role,
            ),
        )
        self.alarm = alarm(
            self,
            "Alarm",
            name=f"{prefix}-release-prefetch",
            metric=cw.MathExpression(
                expression="FILL(errors, 0) + FILL(refused, 0)",
                using_metrics={
                    key: cw.Metric(
                        namespace=METRICS_NAMESPACE,
                        metric_name=metric,
                        dimensions_map={"service": SERVICE},
                        statistic=cw.Stats.SUM,
                        period=Duration.minutes(15),
                    )
                    for key, metric in (("errors", "PrefetchErrors"), ("refused", "PrefetchRefused"))
                },
                period=Duration.minutes(15),
                label="prefetch failures and refusals",
            ),
            threshold=1,
            description="The Release Controller could not create or delete prefetch schedules, or found a RECURRING "
            "one on a sibling (see the prefetcher's log)",
            topic=alert_topic,
        )
