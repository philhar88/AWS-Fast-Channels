"""Channel Assembly's as-run log: its retention, and the reconciler that marks EPG programs ``AIRED``."""

from typing import TYPE_CHECKING, Final

from aws_cdk import CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_logs_destinations as log_destinations
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from constructs import Construct

from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs._provider import provider
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.storage import GSI1

if TYPE_CHECKING:
    from aws_cdk import aws_dynamodb as dynamodb

AS_RUN_LOG_GROUP: Final = "MediaTailor/Channel/AsRunLog"
"""Channel Assembly's as-run log group, shared by every channel of the account and region (channel-assembly.md §7.2)."""
SWEEP_INTERVAL: Final = Duration.minutes(15)
"""How often the break sweep runs (ops module): a break is marked unaired at most this long after it is decided."""


class AsRunLogging(Construct):
    """Retention on Channel Assembly's as-run log, and the reconciler that marks EPG programs ``AIRED``.

    The account-wide group ``MediaTailor/Channel/AsRunLog`` is created by MediaTailor on first use, without
    retention; an idempotent custom resource creates it if needed and sets its retention, and leaves it on deletion
    (other deployments may log there). A subscription filter sends the deployment's ``VodProgramStart`` events to
    ``fast_channels.functions.as_run_reconciler``. A log group has at most five subscription filters (not adjustable),
    so at most five deployments can exist per account and region.

    With ``breaks`` (the ops module) the filter also passes ``AdBreakStart`` events, the reconciler marks each break
    of a ``PROGRAM`` item aired (``AS_RUN_BREAKS``), and a sweep every :data:`SWEEP_INTERVAL` marks the breaks that
    never aired.

    Args:
        prefix: deployment prefix; channels named ``<prefix>-*`` are reconciled.
        library: media library table (``PROGRAM`` items).
        as_run_retention_days: retention of the shared as-run log group, in days.
        log_retention: retention of the functions' own logs.
        removal_policy: what happens to the functions' logs when the stack is deleted.
        breaks: reconcile breaks too (ops module).

    Attributes:
        log_group_name: the as-run log group. reconciler: the reconciler :class:`FastFunction`.
        sweeper: the break sweep (``breaks`` only).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        library: dynamodb.ITableV2,
        as_run_retention_days: int,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
        breaks: bool = False,
    ) -> None:
        super().__init__(scope, id)
        self.log_group_name = AS_RUN_LOG_GROUP
        handler = FastFunction(
            self,
            "Handler",
            handler="fast_channels.functions.custom_resources.as_run.handler",
            description=f"Create {AS_RUN_LOG_GROUP} if missing and set its retention",
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        handler.allow(
            ["logs:CreateLogGroup", "logs:PutRetentionPolicy"],
            [arn(self, "logs", "log-group", AS_RUN_LOG_GROUP, colon=True)],
        )
        log_group = CustomResource(
            self,
            "LogGroup",
            service_token=provider(
                self, "Provider", on_event=handler, log_retention=log_retention, removal_policy=removal_policy
            ).service_token,
            resource_type="Custom::AsRunLogGroup",
            properties={"LogGroupName": AS_RUN_LOG_GROUP, "RetentionInDays": as_run_retention_days},
        )

        self.reconciler = FastFunction(
            self,
            "Reconciler",
            handler="fast_channels.functions.as_run_reconciler.handler",
            description="Mark scheduled programs AIRED from Channel Assembly as-run events",
            environment={"LIBRARY_TABLE": library.table_name, **({"AS_RUN_BREAKS": "true"} if breaks else {})},
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        self.reconciler.allow(["dynamodb:Query"], [f"{library.table_arn}/index/{GSI1}"])
        self.reconciler.allow(
            ["dynamodb:UpdateItem"],
            [library.table_arn],
            conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"CHANNEL#{prefix}-*"]}},
        )
        self.sweeper: FastFunction | None = None
        if breaks:
            self.sweeper = self._sweeper(prefix, library, log_retention, removal_policy)
        nag.acknowledge(
            self.reconciler,
            {
                nag.LAMBDA_DLQ: "Asynchronous invocations by CloudWatch Logs are retried twice; the AIRED mark is "
                "informational (the as-run log keeps the record)"
            },
        )
        events = '$.eventType = "VodProgramStart"'
        if breaks:
            events += ' || $.eventType = "AdBreakStart"'
        subscription = logs.SubscriptionFilter(
            self,
            "ProgramStarts",
            log_group=logs.LogGroup.from_log_group_name(self, "AsRunLog", AS_RUN_LOG_GROUP),
            destination=log_destinations.LambdaDestination(self.reconciler.function),
            filter_name=f"{prefix}-as-run",
            filter_pattern=logs.FilterPattern.literal(f'{{ ({events}) && ($.channelName = "{prefix}-*") }}'),
        )
        subscription.node.add_dependency(log_group)

    def _sweeper(
        self, prefix: str, library: dynamodb.ITableV2, log_retention: logs.RetentionDays, removal_policy: RemovalPolicy
    ) -> FastFunction:
        sweeper = FastFunction(
            self,
            "Sweeper",
            handler="fast_channels.functions.as_run_reconciler.sweep",
            description="Mark scheduled ad breaks that never aired UNAIRED",
            environment={"LIBRARY_TABLE": library.table_name, "AS_RUN_BREAKS": "true"},
            timeout=Duration.minutes(2),
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        sweeper.allow(["dynamodb:Query"], [f"{library.table_arn}/index/{GSI1}"])
        sweeper.allow(
            ["dynamodb:Query", "dynamodb:UpdateItem"],
            [library.table_arn],
            conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"CHANNEL#{prefix}-*"]}},
        )
        nag.acknowledge(
            sweeper,
            {nag.LAMBDA_DLQ: "Invoked by a schedule every 15 minutes; a failed sweep is repeated by the next one"},
        )
        role = iam.Role(
            self,
            "SweepScheduleRole",
            description="EventBridge Scheduler: run the break sweep",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": Stack.of(self).account}}
            ),
        )
        scheduler.Schedule(
            self,
            "SweepSchedule",
            description=f"Mark the breaks of {prefix} channels that never aired",
            schedule=scheduler.ScheduleExpression.rate(SWEEP_INTERVAL),
            target=scheduler_targets.LambdaInvoke(sweeper.function, retry_attempts=0, role=role),
        )
        nag.acknowledge_references(
            role,
            {
                nag.wildcard_resource(f"{sweeper.function.function_arn}:*"): "Scheduler L2 grant: invoke the function "
                "and its versions"
            },
        )
        return sweeper
