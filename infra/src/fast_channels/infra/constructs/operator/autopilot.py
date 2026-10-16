"""Autopilot: EventBridge Scheduler keeps each channel's schedule filled by asking the agent, unattended (SPEC §6)."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as targets
from constructs import Construct

from fast_channels.infra import nag
from fast_channels.infra.constructs.alerts import PERIOD, alarm, dead_letter_alarm
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.operator.identity import AGENT_SCOPE, OperatorIdentity
from fast_channels.infra.constructs.queues import dead_letter_queue

if TYPE_CHECKING:
    from aws_cdk import aws_sns as sns

HANDLER: Final = "fast_channels.functions.autopilot.handler"
TIMEOUT: Final = Duration.minutes(15)
"""A run plans and publishes several blocks; the handler stops resuming 90 s before the timeout."""
RETRY_ATTEMPTS: Final = 1
MAX_EVENT_AGE: Final = Duration.hours(1)
"""An older tick is superseded by the next one; the session id changes every UTC hour anyway."""


class Autopilot(Construct):
    """One EventBridge Scheduler schedule per channel invoking the autopilot Lambda with ``{"channel": <name>}``.

    The Lambda authenticates as the autopilot Cognito client (client secret read with ``DescribeUserPoolClient``)
    and holds one AG-UI conversation with the Runtime; Cedar limits that client to READ tools plus schedule
    planning and publishing on ``channels``. Delivery by Scheduler and the Lambda's own asynchronous invocation are
    both retried once within an hour, then dead-lettered to :attr:`dead_letters`. The scheduler role trusts only
    schedules of the autopilot's own schedule group.

    With ``alert_topic``, two alarms notify it: function errors (a run that crashed, including before it could
    publish a failed ``Autopilot Run`` event) and messages in the dead-letter queue (a run that never succeeded).

    Args:
        channels: schedule expression (``cron(...)``/``rate(...)``, UTC) by channel name.
        identity: Cognito user pool and its autopilot client (``OperatorIdentity(autopilot=True)``).
        runtime_invoke_url: HTTPS invocation URL of the scheduler agent Runtime.
        event_bus: bus for ``AutopilotRun`` events.
        prefix: deployment prefix (``fast:app``, event source, alarm names).
        log_retention, removal_policy: of the function's log group and the dead-letter queue.
        alert_topic: topic the alarms notify; ``None`` creates no alarms.

    Attributes:
        function: the autopilot FastFunction; schedules: by channel; dead_letters: the DLQ; alarms: by name.

    Raises:
        ValueError: ``identity`` has no autopilot client.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        channels: Mapping[str, str],
        identity: OperatorIdentity,
        runtime_invoke_url: str,
        event_bus: events.IEventBus,
        prefix: str,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
        alert_topic: sns.ITopic | None = None,
    ) -> None:
        super().__init__(scope, id)
        client = identity.autopilot_client
        if client is None:
            raise ValueError("the autopilot needs OperatorIdentity(autopilot=True)")
        self.dead_letters = dead_letter_queue(self, "DeadLetters", removal_policy=removal_policy)
        self.function = FastFunction(
            self,
            "Function",
            handler=HANDLER,
            description="Autopilot: asks the scheduling agent to extend a channel's schedule to its horizon",
            environment={
                "FAST_PREFIX": prefix,
                "EVENT_BUS": event_bus.event_bus_name,
                "RUNTIME_INVOKE_URL": runtime_invoke_url,
                "USER_POOL_ID": identity.user_pool.user_pool_id,
                "AUTOPILOT_CLIENT_ID": client.user_pool_client_id,
                "TOKEN_ENDPOINT": identity.token_endpoint,
                "SCOPE": AGENT_SCOPE,
            },
            timeout=TIMEOUT,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        fn = self.function.function
        fn.configure_async_invoke(
            retry_attempts=RETRY_ATTEMPTS,
            max_event_age=MAX_EVENT_AGE,
            on_failure=destinations.SqsDestination(self.dead_letters),
        )
        identity.user_pool.grant(fn, "cognito-idp:DescribeUserPoolClient")
        event_bus.grant_put_events_to(fn)
        group = scheduler.ScheduleGroup(
            self, "Group", schedule_group_name=f"{prefix}-autopilot", removal_policy=RemovalPolicy.DESTROY
        )
        schedule_role = iam.Role(
            self,
            "ScheduleRole",
            description="EventBridge Scheduler: invoke the autopilot and dead-letter failed deliveries",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Stack.of(self).account},
                    "ArnEquals": {"aws:SourceArn": group.schedule_group_arn},
                },
            ),
        )
        self.schedules = {
            channel: scheduler.Schedule(
                self,
                f"Schedule-{channel}",
                schedule=scheduler.ScheduleExpression.expression(expression),
                schedule_group=group,
                description=f"Autopilot: keep {channel}'s schedule filled",
                target=targets.LambdaInvoke(
                    fn,
                    input=scheduler.ScheduleTargetInput.from_object({"channel": channel}),
                    retry_attempts=RETRY_ATTEMPTS,
                    max_event_age=MAX_EVENT_AGE,
                    dead_letter_queue=self.dead_letters,
                    role=schedule_role,
                ),
            )
            for channel, expression in channels.items()
        }
        nag.acknowledge_references(
            schedule_role,
            {nag.wildcard_resource(f"{fn.function_arn}:*"): "Scheduler L2 grant: invoke the function and its versions"},
        )
        nag.acknowledge(
            self.function,
            {nag.LAMBDA_DLQ: "Asynchronous failures go to the dead-letter queue as the on-failure destination"},
        )
        self.alarms: dict[str, cw.Alarm] = {}
        if alert_topic is not None:
            self._alarm_on(prefix, alert_topic)

    def _alarm_on(self, prefix: str, topic: sns.ITopic) -> None:
        name = f"{prefix}-autopilot-errors"
        self.alarms[name] = alarm(
            self,
            "ErrorsAlarm",
            name=name,
            metric=self.function.function.metric_errors(period=PERIOD, statistic=cw.Stats.SUM),
            threshold=1,
            description="An autopilot run failed (it is retried once); see the function's log group",
            topic=topic,
        )
        name = f"{prefix}-autopilot-dead-letters"
        self.alarms[name] = dead_letter_alarm(
            self,
            "DeadLettersAlarm",
            queue=self.dead_letters,
            name=name,
            description="Autopilot runs that failed after their retry: the channel's schedule was not extended",
            topic=topic,
        )
