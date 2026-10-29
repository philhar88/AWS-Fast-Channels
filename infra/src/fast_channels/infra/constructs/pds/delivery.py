"""Delivery and the release sweeper of the own book's reservations (ADR-0022).

- ``DeliveryCounter``: a subscription filter on the stage's MediaTailor log group (``/aws/vendedlogs/mediatailor/
  <prefix>``, the Media stack's) sends the impression beacons (``BEACON_FIRED`` with ``trackingEvent`` impression) to
  ``fast_channels.functions.pds.delivery``, which keeps our tracker's and credits them to their reservations.
- ``ReleaseSweeper``: every 15 minutes ``fast_channels.functions.pds.sweeper`` releases the reservations that saw no
  impression in two hours.

Both write money events to the ledger (chains ``pds/*`` only) and dead-letter what fails after its retries.
"""

from typing import TYPE_CHECKING, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_logs_destinations as logs_destinations
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from constructs import Construct

from fast_channels.core.ledger import ledger_table
from fast_channels.core.naming import mediatailor_log_group
from fast_channels.core.pds.names import PENDING_INDEX, dead_letter_alarm, table_name
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs import alerts
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.queues import dead_letter_queue

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aws_cdk import aws_sns as sns
    from aws_cdk import aws_sqs as sqs

DELIVERY_HANDLER: Final = "fast_channels.functions.pds.delivery.handler"
SWEEPER_HANDLER: Final = "fast_channels.functions.pds.sweeper.handler"
SWEEP_SCHEDULE: Final = "rate(15 minutes)"
RETRY_ATTEMPTS: Final = 2
MAX_EVENT_AGE: Final = Duration.hours(6)
"""How long Lambda keeps retrying a failed batch of beacons before it is dead-lettered."""
IMPRESSIONS: Final = logs.FilterPattern.all(
    logs.FilterPattern.string_value("$.eventType", "=", "BEACON_FIRED"),
    logs.FilterPattern.string_value("$.beaconInfo.trackingEvent", "=", "impression"),
)
"""The ADS log events the delivery function reads: impression beacons (ours among them)."""


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


class DeliveryCounter(Construct):
    """Our tracker's impressions from the MediaTailor log group (module docstring).

    Args:
        prefix: the stage's prefix (its log group ``/aws/vendedlogs/mediatailor/<prefix>``, its table, its ledger).
        content_base_url: the CDN's content base URL, whose host our tracker is on.
        alert_topic: topic of the dead-letter alarm.
        log_retention, removal_policy: of the function's log group and the dead-letter queue.

    Attributes:
        function: the delivery function. subscription: its filter on the log group. dead_letters, alarm.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        content_base_url: str,
        alert_topic: sns.ITopic,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.function = FastFunction(
            self,
            "Function",
            handler=DELIVERY_HANDLER,
            description="PDS: count our tracker's impressions (BEACON_FIRED) and deliver their reservations",
            environment={
                "FAST_PREFIX": prefix,
                "PDS_TABLE": table_name(prefix),
                "LEDGER_TABLE": ledger_table(prefix),
                "CONTENT_BASE_URL": content_base_url,
            },
            timeout=Duration.minutes(1),
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        self.dead_letters = _dead_lettered(self.function, self, removal_policy=removal_policy)
        nag.acknowledge(
            self.function,
            {nag.LAMBDA_DLQ: "Invoked asynchronously by CloudWatch Logs; failed batches go to the DeadLetters queue"},
        )
        allow_table(self.function, prefix, ["PutItem"], ["IMP#*", "FILL#*"])
        allow_table(self.function, prefix, ["GetItem", "UpdateItem"], ["RES#*", "BUDGET#*"])
        allow_ledger(self.function, prefix)
        log_group = logs.LogGroup.from_log_group_name(self, "AdsLogs", mediatailor_log_group(prefix))
        self.subscription = logs.SubscriptionFilter(
            self,
            "Impressions",
            log_group=log_group,
            destination=logs_destinations.LambdaDestination(self.function.function),
            filter_pattern=IMPRESSIONS,
            filter_name=f"{prefix}-pds-delivery",
        )
        self.alarm = alerts.dead_letter_alarm(
            self,
            "DeadLettersAlarm",
            queue=self.dead_letters,
            name=dead_letter_alarm(prefix, "delivery"),
            description="Beacons from the MediaTailor log could not be counted after their retries: impressions and "
            "deliveries are missing (redrive the queue)",
            topic=alert_topic,
        )


class ReleaseSweeper(Construct):
    """Releases the reservations that saw no impression in two hours, every 15 minutes (module docstring).

    Args:
        prefix: the stage's prefix (its table and ledger).
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
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.function = FastFunction(
            self,
            "Function",
            handler=SWEEPER_HANDLER,
            description="PDS: release reservations that saw no impression in two hours",
            environment={
                "FAST_PREFIX": prefix,
                "PDS_TABLE": table_name(prefix),
                "LEDGER_TABLE": ledger_table(prefix),
            },
            timeout=Duration.minutes(5),
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
        allow_table(self.function, prefix, ["GetItem", "UpdateItem"], ["RES#*", "BUDGET#*"])
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
            "Every15Minutes",
            schedule=scheduler.ScheduleExpression.expression(SWEEP_SCHEDULE),
            schedule_group=group,
            description="PDS: release reservations with no impression after two hours",
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
