"""Prebid Server follows the stage's channels: the census gate and the daypart scheduled actions.

* :class:`CensusGate`: a JSONata state machine that the channel census's ``Census Changed`` events start. When the
  first channel starts it raises the guidance's ECS service to ``tasks.min`` (the tier minimum); when the last one
  stops it waits 15 minutes, reads the census's last count again, and scales the service to zero if no channel runs.
  In the ad-server mode (M8-09) the Prebid Cache function's alias follows the same way: its provisioned concurrency
  is set with the service, and removed with it. It uses only Application Auto Scaling, ECS, Lambda and SSM SDK
  calls.
* :class:`Dayparts`: one Application Auto Scaling scheduled action per ``modules.demand.dayparts`` entry, raising the
  minimum ahead of a busy daypart; target tracking (the guidance's) covers the time between them.

The guidance's scalable target is the guidance stack's: the gate and the dayparts change its minimum, so a later
guidance deploy resets it to ``ECSTaskMinCapacity`` until the next census change.
"""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from aws_cdk import ArnFormat, Duration, RemovalPolicy, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import custom_resources as cr
from constructs import Construct

from fast_channels.core.demand import SCALE_IN_AFTER_SECONDS, census_gate, daypart_action
from fast_channels.core.events import DETAIL_TYPES, CensusChanged, event_source
from fast_channels.infra import nag
from fast_channels.infra.grants import allow

if TYPE_CHECKING:
    from fast_channels.infra.config import DemandDaypart, DemandTasks

DIMENSION: Final = "ecs:service:DesiredCount"
WHY_SCHEDULED: Final = "Application Auto Scaling's scheduled-action calls take no resource ARN"


def scalable_resource(cluster: str, service: str) -> str:
    """Application Auto Scaling's resource id of an ECS service."""
    return f"service/{cluster}/{service}"


@dataclass(frozen=True)
class PodCacheConcurrency:
    """The ad-server mode's Prebid Cache function: its alias's provisioned concurrency while a channel is on air."""

    function: str
    alias: str
    instances: int


NOT_PROVISIONED: Final = ("Lambda.ProvisionedConcurrencyConfigNotFoundException", "Lambda.ResourceNotFoundException")
"""Nothing to remove: the gate removed it before, or no channel ran since the deploy."""


def census_gate_definition(
    *, cluster: str, service: str, parameter: str, tasks: DemandTasks, pod_cache: PodCacheConcurrency | None = None
) -> dict[str, Any]:
    """The census gate's JSONata state machine for one ``Census Changed`` event's detail; with ``pod_cache``, the
    Prebid Cache function's provisioned concurrency follows the service."""
    target = {
        "ServiceNamespace": "ecs",
        "ResourceId": scalable_resource(cluster, service),
        "ScalableDimension": DIMENSION,
    }
    retry = [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 5, "MaxAttempts": 3, "BackoffRate": 2}]
    return {
        "Comment": "Scale Prebid Server with the stage's running channels",
        "QueryLanguage": "JSONata",
        "StartAt": "AnyChannel",
        "States": {
            "AnyChannel": {
                "Type": "Choice",
                "Choices": [{"Condition": "{% $states.input.detail.running > 0 %}", "Next": "CurrentMinimum"}],
                "Default": "WaitBeforeScaleIn",
            },
            "CurrentMinimum": {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:applicationautoscaling:describeScalableTargets",
                "Arguments": {
                    "ServiceNamespace": "ecs",
                    "ResourceIds": [scalable_resource(cluster, service)],
                    "ScalableDimension": DIMENSION,
                },
                # A daypart may have raised the minimum above the tier's: keep the higher one.
                "Assign": {"floor": f"{{% $max([$states.result.ScalableTargets[0].MinCapacity, {tasks.min}]) %}}"},
                "Retry": retry,
                "Next": "RaiseMinimum",
            },
            "RaiseMinimum": {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:applicationautoscaling:registerScalableTarget",
                "Arguments": {**target, "MinCapacity": "{% $floor %}", "MaxCapacity": tasks.max},
                "Retry": retry,
                "Next": "CurrentCount",
            },
            "CurrentCount": {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:ecs:describeServices",
                "Arguments": {"Cluster": cluster, "Services": [service]},
                "Assign": {"desired": "{% $states.result.Services[0].DesiredCount %}"},
                "Retry": retry,
                "Next": "ScaleOut",
            },
            "ScaleOut": {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:ecs:updateService",
                "Arguments": {
                    "Cluster": cluster,
                    "Service": service,
                    "DesiredCount": "{% $max([$desired, $floor]) %}",
                },
                "Retry": retry,
                **({"Next": "ProvisionPodCache"} if pod_cache else {"End": True}),
            },
            "WaitBeforeScaleIn": {"Type": "Wait", "Seconds": SCALE_IN_AFTER_SECONDS, "Next": "StillNoChannel"},
            "StillNoChannel": {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:ssm:getParameter",
                "Arguments": {"Name": parameter},
                "Assign": {"running": "{% $number($states.result.Parameter.Value) %}"},
                "Retry": retry,
                "Next": "NoneRunning",
            },
            "NoneRunning": {
                "Type": "Choice",
                "Choices": [{"Condition": "{% $running = 0 %}", "Next": "LowerMinimum"}],
                "Default": "ChannelsBack",
            },
            "ChannelsBack": {"Type": "Succeed"},
            "LowerMinimum": {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:applicationautoscaling:registerScalableTarget",
                "Arguments": {**target, "MinCapacity": 0, "MaxCapacity": tasks.max},
                "Retry": retry,
                "Next": "ScaleIn",
            },
            "ScaleIn": {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:ecs:updateService",
                "Arguments": {"Cluster": cluster, "Service": service, "DesiredCount": 0},
                "Retry": retry,
                **({"Next": "ReleasePodCache"} if pod_cache else {"End": True}),
            },
            **(_pod_cache_states(pod_cache, retry) if pod_cache else {}),
        },
    }


def _pod_cache_states(pod_cache: PodCacheConcurrency, retry: list[dict[str, Any]]) -> dict[str, Any]:
    """Set the Prebid Cache alias's provisioned concurrency with the service, and remove it with it."""
    qualified = {"FunctionName": pod_cache.function, "Qualifier": pod_cache.alias}
    return {
        "ProvisionPodCache": {
            "Type": "Task",
            "Resource": "arn:aws:states:::aws-sdk:lambda:putProvisionedConcurrencyConfig",
            "Arguments": {**qualified, "ProvisionedConcurrentExecutions": pod_cache.instances},
            "Retry": retry,
            "End": True,
        },
        "ReleasePodCache": {
            "Type": "Task",
            "Resource": "arn:aws:states:::aws-sdk:lambda:deleteProvisionedConcurrencyConfig",
            "Arguments": qualified,
            "Retry": [{**retry[0], "ErrorEquals": ["Lambda.TooManyRequestsException", "Lambda.ServiceException"]}],
            "Catch": [{"ErrorEquals": list(NOT_PROVISIONED), "Next": "PodCacheReleased"}],
            "End": True,
        },
        "PodCacheReleased": {"Type": "Succeed"},
    }


class CensusGate(Construct):
    """The census gate (module docstring) and the rule that runs it on every ``Census Changed`` event.

    Args:
        prefix: the stage's prefix (its event bus and census parameter).
        cluster_arn: the guidance's ECS cluster.
        service_arn: the guidance's Prebid Server service.
        census_parameter: the census's last count (``/<prefix>/ops/running-channels``).
        tasks: the tier minimum and the service maximum.
        pod_cache: the ad-server mode's Prebid Cache function, whose alias the gate provisions with the service.
        log_retention: the state machine's log retention.
        dead_letters: where undeliverable ``Census Changed`` events go.

    Attributes:
        state_machine: the gate.
        rule: the ``Census Changed`` rule on the stage's bus.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        cluster_arn: str,
        service_arn: str,
        census_parameter: str,
        tasks: DemandTasks,
        log_retention: logs.RetentionDays,
        dead_letters: sqs.IQueue,
        pod_cache: PodCacheConcurrency | None = None,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        cluster = cluster_arn.rsplit("/", 1)[1]
        service = service_arn.rsplit("/", 1)[1]
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/vendedlogs/states/{census_gate(prefix)}",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY,
        )
        definition = census_gate_definition(
            cluster=cluster, service=service, parameter=census_parameter, tasks=tasks, pod_cache=pod_cache
        )
        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_name=census_gate(prefix),
            definition_body=sfn.DefinitionBody.from_string(json.dumps(definition)),
            comment=f"FAST Channels ({prefix}): Prebid Server follows the running channels",
            timeout=Duration.minutes(30),
            tracing_enabled=True,
            logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ALL, include_execution_data=True),
        )
        allow(
            self.state_machine,
            ["application-autoscaling:RegisterScalableTarget", "application-autoscaling:DescribeScalableTargets"],
            ["*"],
            conditions={"StringEquals": {"application-autoscaling:service-namespace": "ecs"}},
        )
        allow(self.state_machine, ["ecs:DescribeServices", "ecs:UpdateService"], [service_arn])
        if pod_cache:
            function = stack.format_arn(
                service="lambda",
                resource="function",
                resource_name=pod_cache.function,
                arn_format=ArnFormat.COLON_RESOURCE_NAME,
            )
            allow(
                self.state_machine,
                ["lambda:PutProvisionedConcurrencyConfig", "lambda:DeleteProvisionedConcurrencyConfig"],
                [function, f"{function}:{pod_cache.alias}"],
            )
        allow(
            self.state_machine,
            ["ssm:GetParameter"],
            [stack.format_arn(service="ssm", resource="parameter", resource_name=census_parameter.lstrip("/"))],
        )
        nag.acknowledge_references(
            self.state_machine.role,
            {
                nag.wildcard_resource(): "Application Auto Scaling's RegisterScalableTarget takes no resource ARN "
                "(limited to the ECS namespace); X-Ray has no resource-level permissions",
            },
        )
        bus = events.EventBus.from_event_bus_name(self, "Bus", f"{prefix}-fast-channels")
        self.rule = events.Rule(
            self,
            "CensusChanged",
            event_bus=bus,
            description=f"FAST Channels ({prefix}): the census gate scales Prebid Server with the running channels",
            event_pattern=events.EventPattern(source=[event_source(prefix)], detail_type=[DETAIL_TYPES[CensusChanged]]),
            targets=[
                events_targets.SfnStateMachine(self.state_machine, retry_attempts=8, dead_letter_queue=dead_letters)
            ],
        )


class Dayparts(Construct):
    """One Application Auto Scaling scheduled action per daypart (created, updated and deleted with the stack).

    Args:
        prefix: the stage's prefix.
        cluster_arn: the guidance's ECS cluster.
        service_arn: the guidance's Prebid Server service.
        dayparts: the scheduled minimums.
        tasks: the service maximum.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        cluster_arn: str,
        service_arn: str,
        dayparts: tuple[DemandDaypart, ...],
        tasks: DemandTasks,
    ) -> None:
        super().__init__(scope, id)
        resource = scalable_resource(cluster_arn.rsplit("/", 1)[1], service_arn.rsplit("/", 1)[1])
        policy = cr.AwsCustomResourcePolicy.from_statements(
            [
                iam.PolicyStatement(
                    actions=[
                        "application-autoscaling:PutScheduledAction",
                        "application-autoscaling:DeleteScheduledAction",
                    ],
                    resources=["*"],
                    conditions={"StringEquals": {"application-autoscaling:service-namespace": "ecs"}},
                )
            ]
        )
        for index, daypart in enumerate(dayparts, start=1):
            name = daypart_action(prefix, index)
            target = {"ServiceNamespace": "ecs", "ResourceId": resource, "ScalableDimension": DIMENSION}
            put = cr.AwsSdkCall(
                service="ApplicationAutoScaling",
                action="putScheduledAction",
                parameters={
                    **target,
                    "ScheduledActionName": name,
                    "Schedule": daypart.cron,
                    "Timezone": "UTC",
                    "ScalableTargetAction": {"MinCapacity": daypart.min, "MaxCapacity": tasks.max},
                },
                physical_resource_id=cr.PhysicalResourceId.of(name),
            )
            action = cr.AwsCustomResource(
                self,
                f"Daypart{index}",
                on_create=put,
                on_update=put,
                on_delete=cr.AwsSdkCall(
                    service="ApplicationAutoScaling",
                    action="deleteScheduledAction",
                    parameters={**target, "ScheduledActionName": name},
                    ignore_error_codes_matching="ObjectNotFoundException",
                ),
                policy=policy,
                install_latest_aws_sdk=False,
            )
            nag.acknowledge_references(
                action,
                {nag.wildcard_resource(): f"{WHY_SCHEDULED}; limited to the ECS namespace"},
            )
        if dayparts:
            # The CDK-managed provider of AwsCustomResource is a stack-wide singleton that aws-cdk-lib shapes.
            provider = Stack.of(self).node.try_find_child(
                "AWS" + cr.AwsCustomResource.PROVIDER_FUNCTION_UUID.replace("-", "")
            )
            reason = "CDK AwsCustomResource provider (aws-cdk-lib shapes it), invoked synchronously by CloudFormation"
            if provider is not None:
                nag.acknowledge_references(
                    provider,
                    {
                        nag.LAMBDA_DLQ: reason,
                        "Serverless-LambdaDefaultMemorySize": reason,
                        nag.managed_policy("service-role/AWSLambdaBasicExecutionRole"): f"{reason}: its role",
                        nag.wildcard_resource(): f"{WHY_SCHEDULED}; {reason}",
                    },
                )
