from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from aws_cdk import Duration
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn

from fast_channels.infra import nag
from fast_channels.infra.constructs.functions import FastFunction

if TYPE_CHECKING:
    from aws_cdk.assertions import Template

type Factory = Callable[..., Any]

HANDLER = "fast_channels.functions.tools.catalog.handler"


def fast_function(scope: Any, id: str, **kwargs: Any) -> FastFunction:
    fn = FastFunction(
        scope, id, handler=HANDLER, description="Catalog tools", log_retention=logs.RetentionDays.ONE_WEEK, **kwargs
    )
    nag.acknowledge(fn, {nag.LAMBDA_DLQ: "Invoked synchronously"})
    return fn


@dataclass(frozen=True)
class Templates:
    media: Template
    operator: Template
    deployment: Any


@pytest.fixture
def templates(fresh_deployment: Factory) -> Templates:
    """FAST functions, plus plain functions and state machines as other constructs would create them."""
    d = fresh_deployment()
    media = d.stage.media
    fast_function(media, "Catalog", environment={"CATALOG_TABLE": "t"}, timeout=Duration.seconds(10), memory_size=256)
    fast_function(media, "Library")
    fast_function(d.stage.operator, "Tools")
    plain = {
        "runtime": lambda_.Runtime.PYTHON_3_14,
        "handler": "index.handler",
        "code": lambda_.Code.from_inline("def handler(event, context): pass"),
    }
    lambda_.Function(media, "Plain", **plain)
    lambda_.Function(
        media, "Explicit", tracing=lambda_.Tracing.PASS_THROUGH, logging_format=lambda_.LoggingFormat.TEXT, **plain
    )
    sfn.StateMachine(media, "Workflow", definition_body=sfn.DefinitionBody.from_chainable(sfn.Pass(media, "Step")))
    sfn.StateMachine(
        media,
        "Logged",
        definition_body=sfn.DefinitionBody.from_chainable(sfn.Pass(media, "Other")),
        logs=sfn.LogOptions(destination=logs.LogGroup(media, "OwnLogs"), level=sfn.LogLevel.ERROR),
    )
    return Templates(d.template(media), d.template(d.stage.operator), d)


def resource(template: Template, type_: str, logical_prefix: str) -> Mapping[str, Any]:
    [found] = [v for k, v in template.find_resources(type_).items() if k.startswith(logical_prefix)]
    return found


def function(template: Template, logical_prefix: str) -> Mapping[str, Any]:
    props: Mapping[str, Any] = resource(template, "AWS::Lambda::Function", logical_prefix)["Properties"]
    return props


def test_fast_function_runtime_and_observability(templates: Templates) -> None:
    props = function(templates.media, "Catalog")

    assert (props["Runtime"], props["Architectures"], props["Handler"]) == ("python3.14", ["arm64"], HANDLER)
    assert (props["Timeout"], props["MemorySize"], props["Description"]) == (10, 256, "Catalog tools")
    assert props["TracingConfig"] == {"Mode": "Active"}
    assert props["RecursiveLoop"] == "Terminate"
    assert props["LoggingConfig"]["LogGroup"]["Ref"].startswith("CatalogLogs")
    assert {k: v for k, v in props["LoggingConfig"].items() if k != "LogGroup"} == {
        "LogFormat": "JSON",
        "ApplicationLogLevel": "INFO",
        "SystemLogLevel": "INFO",
    }
    assert props["Environment"]["Variables"] == {
        "POWERTOOLS_SERVICE_NAME": "tools.catalog",
        "POWERTOOLS_METRICS_NAMESPACE": "FastChannels",
        "CATALOG_TABLE": "t",
    }


def test_fast_function_defaults(templates: Templates) -> None:
    props = function(templates.media, "Library")

    assert (props["Timeout"], props["MemorySize"]) == (30, 512)


def test_fast_function_log_group_and_least_privilege_role(templates: Templates) -> None:
    log_group = resource(templates.media, "AWS::Logs::LogGroup", "CatalogLogs")
    role = resource(templates.media, "AWS::IAM::Role", "CatalogRole")
    policy = resource(templates.media, "AWS::IAM::Policy", "CatalogRole")

    assert (log_group["Properties"]["RetentionInDays"], log_group["DeletionPolicy"]) == (7, "Delete")
    assert "ManagedPolicyArns" not in role["Properties"]
    [logs_statement, xray_statement] = policy["Properties"]["PolicyDocument"]["Statement"]
    assert logs_statement["Action"] == ["logs:CreateLogStream", "logs:PutLogEvents"]
    assert logs_statement["Resource"]["Fn::GetAtt"][0].startswith("CatalogLogs")
    assert (sorted(xray_statement["Action"]), xray_statement["Resource"]) == (
        ["xray:PutTelemetryRecords", "xray:PutTraceSegments"],
        "*",
    )


def test_one_code_asset_per_stack(templates: Templates) -> None:
    keys = {function(templates.media, name)["Code"]["S3Key"] for name in ("Catalog", "Library")}

    assert len(keys) == 1
    assert function(templates.operator, "Tools")["Code"]["S3Key"] in keys
    for stack in (templates.deployment.stage.media, templates.deployment.stage.operator):
        assert stack.node.try_find_child("FastFunctionsCode") is not None


def test_fast_function_requires_a_functions_package_handler(fresh_deployment: Factory) -> None:
    with pytest.raises(ValueError, match=r"must live in the fast_channels\.functions package"):
        FastFunction(
            fresh_deployment().stage.media,
            "Bad",
            handler="index.handler",
            description="x",
            log_retention=logs.RetentionDays.ONE_DAY,
        )


def test_other_functions_get_observability_defaults(templates: Templates) -> None:
    props = function(templates.media, "Plain")
    log_group = resource(templates.media, "AWS::Logs::LogGroup", "PlainLogs")

    assert props["TracingConfig"] == {"Mode": "Active"}
    assert props["RecursiveLoop"] == "Terminate"
    assert props["LoggingConfig"]["LogGroup"]["Ref"].startswith("PlainLogs")
    assert {k: v for k, v in props["LoggingConfig"].items() if k != "LogGroup"} == {
        "LogFormat": "JSON",
        "ApplicationLogLevel": "INFO",
        "SystemLogLevel": "INFO",
    }
    assert (log_group["Properties"]["RetentionInDays"], log_group["DeletionPolicy"]) == (7, "Delete")


def test_explicit_function_props_win(templates: Templates) -> None:
    props = function(templates.media, "Explicit")

    assert props["TracingConfig"] == {"Mode": "PassThrough"}
    assert props["LoggingConfig"]["LogFormat"] == "Text"
    assert "ApplicationLogLevel" not in props["LoggingConfig"]


def test_state_machines_get_tracing_and_vended_logs(templates: Templates) -> None:
    workflow = resource(templates.media, "AWS::StepFunctions::StateMachine", "Workflow")["Properties"]
    log_group = resource(templates.media, "AWS::Logs::LogGroup", "WorkflowLogs")["Properties"]

    assert workflow["TracingConfiguration"] == {"Enabled": True}
    assert (workflow["LoggingConfiguration"]["Level"], workflow["LoggingConfiguration"]["IncludeExecutionData"]) == (
        "ALL",
        True,
    )
    assert log_group["LogGroupName"].startswith("/aws/vendedlogs/states/")
    assert log_group["RetentionInDays"] == 7


def test_explicit_state_machine_logging_wins(templates: Templates) -> None:
    logged = resource(templates.media, "AWS::StepFunctions::StateMachine", "Logged")["Properties"]

    assert logged["LoggingConfiguration"]["Level"] == "ERROR"
    assert logged["TracingConfiguration"] == {"Enabled": True}


def test_core_custom_resource_providers_log_with_retention(default: Any) -> None:
    resources = default.template(default.stage.data).to_json()["Resources"]
    [handler] = [
        r
        for k, r in resources.items()
        if k.startswith("CustomS3AutoDeleteObjects") and r["Type"] == "AWS::Lambda::Function"
    ]
    log_group_id = handler["Properties"]["LoggingConfig"]["LogGroup"]["Ref"]
    [role] = [r for k, r in resources.items() if k.startswith("CustomS3AutoDeleteObjectsCustomResourceProviderRole")]

    assert resources[log_group_id]["Properties"]["RetentionInDays"] == 7
    assert resources[log_group_id]["DeletionPolicy"] == "Delete"
    assert log_group_id in role["DependsOn"]  # the role is deleted first


def test_roles_are_deleted_before_the_log_groups_of_their_functions(templates: Templates) -> None:
    resources = templates.media.to_json()["Resources"]
    [log_group_id] = [k for k in resources if k.startswith("PlainLogs")]

    for prefix in ("PlainServiceRole", "PlainServiceRoleDefaultPolicy"):
        [role] = [r for k, r in resources.items() if k.startswith(prefix) and "DefaultPolicy" not in k[len(prefix) :]]
        assert log_group_id in role["DependsOn"], prefix


def test_fast_functions_are_nag_clean(fresh_deployment: Factory, tmp_path: Path) -> None:
    d = fresh_deployment(outdir=tmp_path)
    fast_function(d.stage.media, "Catalog")

    d.app.synth()  # raises ValidationFailed on any unacknowledged cdk-nag finding
