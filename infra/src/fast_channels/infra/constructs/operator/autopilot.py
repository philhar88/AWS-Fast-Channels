"""Autopilot: EventBridge Scheduler keeps each channel's schedule filled by asking the agent, unattended (SPEC §6)."""

from collections.abc import Mapping
from typing import Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as targets
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from fast_channels.infra import nag
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.operator.identity import AGENT_SCOPE, OperatorIdentity
from fast_channels.infra.nag import acknowledge_references

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
    planning and publishing. Delivery by Scheduler and the Lambda's own asynchronous invocation are both retried
    once within an hour, then dead-lettered to :attr:`dead_letters`.

    Args:
        channels: schedule expression (``cron(...)``/``rate(...)``, UTC) by channel name.
        identity: Cognito user pool and its autopilot client.
        runtime_invoke_url: HTTPS invocation URL of the scheduler agent Runtime.
        event_bus: bus for ``AutopilotRun`` events.
        prefix: deployment prefix (``fast:app``, event source).
        log_retention, removal_policy: of the function's log group.

    Attributes:
        function: the autopilot FastFunction; schedules: by channel; dead_letters: the DLQ.
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
    ) -> None:
        super().__init__(scope, id)
        self.dead_letters = sqs.Queue(
            self,
            "DeadLetters",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
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
                "AUTOPILOT_CLIENT_ID": identity.autopilot_client.user_pool_client_id,
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
        schedule_role = iam.Role(
            self,
            "ScheduleRole",
            description="EventBridge Scheduler: invoke the autopilot and dead-letter failed deliveries",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": Stack.of(self).account}}
            ),
        )
        group = scheduler.ScheduleGroup(
            self, "Group", schedule_group_name=f"{prefix}-autopilot", removal_policy=RemovalPolicy.DESTROY
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
        acknowledge_references(
            schedule_role,
            {nag.wildcard_resource(f"{fn.function_arn}:*"): "Scheduler L2 grant: invoke the function and its versions"},
        )
        nag.acknowledge(
            self.dead_letters,
            {
                nag.SQS_DLQ: "This queue is the dead-letter queue",
                "Serverless-SQSRedrivePolicy": "This queue is the dead-letter queue",
            },
        )
        nag.acknowledge(
            self.function,
            {nag.LAMBDA_DLQ: "Asynchronous failures go to the dead-letter queue as the on-failure destination"},
        )
