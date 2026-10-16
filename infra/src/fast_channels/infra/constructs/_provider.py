"""Custom-resource ``Provider`` frameworks, built one way: INFO-level framework logs, a traced and logged waiter,
and their cdk-nag findings acknowledged."""

from typing import TYPE_CHECKING, Final, cast

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import custom_resources as cr

from fast_channels.infra import nag

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.constructs.functions import FastFunction

XRAY_ACTIONS: Final = (
    "xray:PutTraceSegments",
    "xray:PutTelemetryRecords",
    "xray:GetSamplingRules",
    "xray:GetSamplingTargets",
)
FRAMEWORK_DLQ: Final = "Provider framework function, invoked by CloudFormation, which reports failures back to it"
PROVIDER_INVOKED: Final = "Invoked synchronously by the custom-resource Provider framework, which reports failures"
QUERY_INTERVAL: Final = Duration.seconds(30)
TOTAL_TIMEOUT: Final = Duration.minutes(30)


def provider(
    scope: Construct,
    id: str,
    *,
    on_event: FastFunction,
    is_complete: FastFunction | None = None,
    waiter_log_group_name: str | None = None,
    waiter_logs_id: str = "WaiterLogs",
    query_interval: Duration = QUERY_INTERVAL,
    total_timeout: Duration = TOTAL_TIMEOUT,
    log_retention: logs.RetentionDays,
    removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
) -> cr.Provider:
    """A ``cr.Provider`` for ``on_event`` (and ``is_complete``, which makes it asynchronous).

    Its framework functions log at INFO (the L2 default is FATAL, which leaves no trace of a failed or timed-out
    resource). An asynchronous provider polls every ``query_interval`` for up to ``total_timeout``; its waiter state
    machine logs ALL events with execution data to ``waiter_log_group_name`` (construct ``waiter_logs_id`` in
    ``scope``) and is traced with X-Ray.

    Raises:
        ValueError: ``is_complete`` without ``waiter_log_group_name``.
    """
    handlers = [on_event, *([is_complete] if is_complete else [])]
    for handler in handlers:
        nag.acknowledge(handler, {nag.LAMBDA_DLQ: PROVIDER_INVOKED})
    if is_complete is None:
        created = cr.Provider(
            scope,
            id,
            on_event_handler=on_event.function,
            framework_lambda_logging_level=lambda_.ApplicationLogLevel.INFO,
        )
    else:
        if waiter_log_group_name is None:
            raise ValueError("an asynchronous provider needs waiter_log_group_name")
        waiter_logs = logs.LogGroup(
            scope,
            waiter_logs_id,
            log_group_name=waiter_log_group_name,
            retention=log_retention,
            removal_policy=removal_policy,
        )
        created = cr.Provider(
            scope,
            id,
            on_event_handler=on_event.function,
            is_complete_handler=is_complete.function,
            query_interval=query_interval,
            total_timeout=total_timeout,
            framework_lambda_logging_level=lambda_.ApplicationLogLevel.INFO,
            disable_waiter_state_machine_logging=False,
            waiter_state_machine_log_options=cr.LogOptions(
                destination=waiter_logs, level=sfn.LogLevel.ALL, include_execution_data=True
            ),
        )
        trace_waiter(created)
    acknowledge_framework(created, *(handler.function for handler in handlers))
    return created


def trace_waiter(provider: cr.Provider) -> None:
    """X-Ray tracing for the waiter state machine of an asynchronous ``provider`` (``is_complete_handler``); the
    L2 offers logging options only."""
    waiter = provider.node.find_child("waiter-state-machine")
    cast(
        "sfn.CfnStateMachine", waiter.node.find_child("Resource")
    ).tracing_configuration = sfn.CfnStateMachine.TracingConfigurationProperty(enabled=True)
    role = cast("iam.Role", waiter.node.find_child("Role"))
    role.add_to_principal_policy(iam.PolicyStatement(actions=list(XRAY_ACTIONS), resources=["*"]))


def acknowledge_framework(provider: cr.Provider, *handlers: lambda_.IFunction) -> None:
    """Acknowledge the cdk-nag findings of ``provider``'s framework functions, roles and waiter, which aws-cdk-lib
    shapes, not this project.

    The framework (and its waiter) invoke ``handlers`` and the framework functions as ``<function ARN>:*``, i.e.
    any version or alias.
    """
    invoked = [*handlers, *(c for c in provider.node.find_all() if isinstance(c, lambda_.Function))]
    nag.acknowledge_references(
        provider,
        {
            nag.LAMBDA_DLQ: FRAMEWORK_DLQ,
            nag.managed_policy("service-role/AWSLambdaBasicExecutionRole"): "Provider framework function role",
            nag.wildcard_resource(): "X-Ray tracing, and the waiter's CloudWatch Logs delivery actions, have no "
            "resource-level permissions",
            "Serverless-LambdaDefaultMemorySize": "Provider framework functions only relay requests",
            **{
                nag.wildcard_resource(f"{function.function_arn}:*"): "Provider framework: invoke the function and "
                "its versions"
                for function in invoked
            },
        },
    )
