"""Shared shaping of CDK custom-resource ``Provider`` frameworks: waiter tracing and their cdk-nag findings."""

from typing import Final, cast

from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import custom_resources as cr

from fast_channels.infra import nag

XRAY_ACTIONS: Final = (
    "xray:PutTraceSegments",
    "xray:PutTelemetryRecords",
    "xray:GetSamplingRules",
    "xray:GetSamplingTargets",
)
FRAMEWORK_DLQ: Final = "Provider framework function, invoked by CloudFormation, which reports failures back to it"


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
