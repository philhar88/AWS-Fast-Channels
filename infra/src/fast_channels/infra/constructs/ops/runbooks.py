"""The ops module's runbooks R7, R9 and R12-R17 (``core.runbooks``): one allow-listed state machine each, started by
its trigger, each execution ledgered.

Every state machine (``<prefix>-runbook-<id>``) runs one step, the function
``fast_channels.functions.ops.runbooks``, which reads the kill switches, gathers the facts, decides and appends the
decision to the ledger chain ``runbook/<id>``. Until ADR-0023 (L2 autonomy) is accepted that is the whole runbook:
its dry-run path. The acting steps join these state machines when it is.

| Runbook | Trigger |
|---|---|
| R7 | An account quota alarm goes to ALARM (default bus) |
| R9 | A configuration's ``GetManifest.Errors`` alarm goes to ALARM |
| R12 | The probe canary's alarm goes to ALARM (two failed runs in a row) |
| R13 | ``Channel Alert`` on the stage's bus |
| R14 | ``Asset Failed`` on the stage's bus |
| R15 | ``Schedule Published`` on the stage's bus |
| R16 | Every hour (EventBridge Scheduler): the programs that start within 6 hours |
| R17 | The CDN 5xx alarm goes to ALARM (in us-east-1, where CloudFront's metrics are) |
"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from fast_channels.core.events import DETAIL_TYPES, AssetFailed, ChannelAlert, SchedulePublished, event_source
from fast_channels.core.runbooks import Runbook
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.functions import FastFunction

if TYPE_CHECKING:
    from fast_channels.infra.constructs.ops.flags import OpsAppConfig

RIGHTS_CHECK_INTERVAL: Final = Duration.hours(1)
STAGE_EVENTS: Final[dict[Runbook, str]] = {
    Runbook.R13: DETAIL_TYPES[ChannelAlert],
    Runbook.R14: DETAIL_TYPES[AssetFailed],
    Runbook.R15: DETAIL_TYPES[SchedulePublished],
}
"""Runbooks started by an event of the stage's bus (detail-type)."""


class Runbooks(Construct):
    """The runbooks' function, their state machines and their triggers.

    Args:
        prefix: deployment prefix.
        event_bus: the stage's bus.
        ledger_table: the ledger (entries of the chains ``runbook/*`` only).
        library: the media library (programs, R15 and R16).
        catalog: the reference catalog (rights windows, R16).
        appconfig: the stage's AppConfig application (the ``flags`` profile).
        channels: the IaC channels.
        source_location: the deployment's source location (R15).
        alarms: names of the alarms that start R7, R9, R12 and R17.
        log_retention: retention of the logs.
        removal_policy: what happens to them when the stack is deleted.

    Attributes:
        function: the runbooks' function.
        state_machines: by runbook.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        event_bus: events.IEventBus,
        ledger_table: dynamodb.ITableV2,
        library: dynamodb.ITableV2,
        catalog: dynamodb.ITableV2,
        appconfig: OpsAppConfig,
        channels: Sequence[str],
        source_location: str,
        alarms: Mapping[Runbook, Sequence[str]],
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.function = FastFunction(
            self,
            "Handler",
            handler="fast_channels.functions.ops.runbooks.handler",
            description="Runbooks R7, R9, R12-R17: decide from the facts and ledger the decision (dry run)",
            environment={
                "FAST_PREFIX": prefix,
                "LEDGER_TABLE": ledger_table.table_name,
                "LIBRARY_TABLE": library.table_name,
                "CATALOG_TABLE": catalog.table_name,
                "APPCONFIG_APPLICATION": f"{prefix}-fast",
                "APPCONFIG_ENVIRONMENT": prefix,
                "IAC_CHANNELS": ",".join(channels),
                "SOURCE_LOCATION": source_location,
            },
            timeout=Duration.minutes(2),
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        chains = {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["runbook/*"]}}
        self.function.allow(["dynamodb:PutItem", "dynamodb:Query"], [ledger_table.table_arn], conditions=chains)
        self.function.allow(
            ["dynamodb:Query"],
            [library.table_arn],
            conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"CHANNEL#{prefix}-*"]}},
        )
        self.function.allow(["dynamodb:GetItem"], [catalog.table_arn])
        self.function.allow(
            ["mediatailor:DescribeChannel"],
            [arn(self, "mediatailor", "channel", f"{prefix}-*")],
            why="States of this deployment's channels (named <prefix>-*)",
        )
        self.function.allow(
            ["mediatailor:DescribeVodSource"],
            [arn(self, "mediatailor", "vodSource", f"{prefix}-*/*")],
            why="Published programs' VOD sources, in this deployment's or a runtime source location (R15)",
        )
        appconfig.grant_read_flags(self.function.function)
        nag.acknowledge(self.function, {nag.LAMBDA_DLQ: "Invoked synchronously by its state machines"})

        self.state_machines = {runbook: self._state_machine(prefix, runbook) for runbook in Runbook}
        self._triggers(prefix, event_bus, alarms)

    def _state_machine(self, prefix: str, runbook: Runbook) -> sfn.StateMachine:
        scope = Construct(self, runbook.value.upper())
        decide = tasks.LambdaInvoke.jsonata(
            scope,
            "Decide",
            lambda_function=self.function.function,
            payload=sfn.TaskInput.from_object(
                {
                    "runbook": runbook.value,
                    "trigger": "{% $states.input %}",
                    "execution": "{% $states.context.Execution.Name %}",
                }
            ),
            outputs="{% $states.result.Payload %}",
            retry_on_service_exceptions=True,
        )
        state_machine = sfn.StateMachine(
            scope,
            "StateMachine",
            state_machine_name=f"{prefix}-runbook-{runbook.value}",
            comment=f"Runbook {runbook.value.upper()} (dry run until ADR-0023): decide and ledger the decision",
            query_language=sfn.QueryLanguage.JSONATA,
            definition_body=sfn.DefinitionBody.from_chainable(decide),
            timeout=Duration.minutes(5),
        )
        nag.acknowledge_references(
            state_machine.role,
            {
                nag.wildcard_resource(): "X-Ray has no resource-level permissions",
                nag.wildcard_resource(f"{self.function.function.function_arn}:*"): "Invoke the step and its versions",
            },
        )
        return state_machine

    def _triggers(self, prefix: str, event_bus: events.IEventBus, alarms: Mapping[Runbook, Sequence[str]]) -> None:
        for runbook, detail_type in STAGE_EVENTS.items():
            self._acknowledge(
                events.Rule(
                    self,
                    f"{runbook.value.upper()}Trigger",
                    description=f"Start runbook {runbook.value.upper()} on {detail_type}",
                    event_bus=event_bus,
                    event_pattern=events.EventPattern(source=[event_source(prefix)], detail_type=[detail_type]),
                    targets=[self._target(runbook)],
                )
            )
        for runbook, names in alarms.items():
            if not names:
                continue
            self._acknowledge(
                events.Rule(
                    self,
                    f"{runbook.value.upper()}Trigger",
                    description=f"Start runbook {runbook.value.upper()} when its alarms go to ALARM",
                    event_pattern=events.EventPattern(
                        source=["aws.cloudwatch"],
                        detail_type=["CloudWatch Alarm State Change"],
                        detail={"alarmName": list(names), "state": {"value": ["ALARM"]}},
                    ),
                    targets=[self._target(runbook)],
                )
            )
        role = iam.Role(
            self,
            "RightsCheckRole",
            description="EventBridge Scheduler: start runbook R16",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": Stack.of(self).account}}
            ),
        )
        scheduler.Schedule(
            self,
            "RightsCheck",
            description=f"Runbook R16: rights of {prefix}'s programs that start within 6 hours",
            schedule=scheduler.ScheduleExpression.rate(RIGHTS_CHECK_INTERVAL),
            target=scheduler_targets.StepFunctionsStartExecution(
                self.state_machines[Runbook.R16],
                input=scheduler.ScheduleTargetInput.from_object({"scheduled_time": "<aws.scheduler.scheduled-time>"}),
                retry_attempts=0,
                role=role,
            ),
        )

    @staticmethod
    def _acknowledge(rule: events.Rule) -> None:
        nag.acknowledge(
            rule, {"Serverless-EventBusDLQ": "EventBridge retries the runbook; its trigger (alarm, event) stays"}
        )

    def _target(self, runbook: Runbook) -> events_targets.SfnStateMachine:
        return events_targets.SfnStateMachine(self.state_machines[runbook], retry_attempts=4)
