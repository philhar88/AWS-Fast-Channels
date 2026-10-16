"""Building blocks every plane shares: grants, ARNs, log plumbing, providers, dead-letter queues, exports."""

import json
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App, Environment, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk.assertions import Template

from fast_channels.infra import exports
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs import _provider, channel_assembly, encoding, observability
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.logs import deliver, vended_log_group
from fast_channels.infra.constructs.queues import dead_letter_queue
from fast_channels.infra.grants import allow
from fast_channels.infra.stacks import data, media

ACCOUNT = "123456789012"
CDK_CONTEXT: dict[str, Any] = {
    k: v
    for k, v in json.loads((Path(__file__).resolve().parents[2] / "cdk.json").read_text())["context"].items()
    if k != "fast-channels"
}


def new_stack() -> Stack:
    app = App(context={**CDK_CONTEXT, "aws:cdk:bundling-stacks": []})
    return Stack(app, "S", env=Environment(account=ACCOUNT, region="us-east-1"))


def function(stack: Stack, id: str = "Fn") -> FastFunction:
    return FastFunction(
        stack,
        id,
        handler="fast_channels.functions.tools.catalog.handler",
        description="test",
        log_retention=logs.RetentionDays.ONE_DAY,
    )


def policy_statements(template: Template) -> list[dict[str, Any]]:
    return [
        s
        for p in template.find_resources("AWS::IAM::Policy").values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]


# --- ARNs and grants -----------------------------------------------------------------------------------------------


def test_arns_are_literal_in_a_concrete_environment() -> None:
    stack = new_stack()

    assert arn(stack, "mediatailor", "channel", "fast-*") == f"arn:aws:mediatailor:us-east-1:{ACCOUNT}:channel/fast-*"
    assert arn(stack, "logs", "log-group", "g", colon=True) == f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:g"


def test_allow_adds_one_statement_with_its_conditions() -> None:
    stack = new_stack()
    fn = function(stack)
    fn.allow(["s3:GetObject"], ["arn:aws:s3:::b/k"], conditions={"Bool": {"aws:SecureTransport": "true"}})

    assert {
        "Action": "s3:GetObject",
        "Effect": "Allow",
        "Resource": "arn:aws:s3:::b/k",
        "Condition": {"Bool": {"aws:SecureTransport": "true"}},
    } in policy_statements(Template.from_stack(stack))


def test_allow_insists_on_a_reason_for_wildcards() -> None:
    stack = new_stack()
    role = iam.Role(stack, "R", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))

    with pytest.raises(ValueError, match="explain why"):
        allow(role, ["s3:GetObject"], ["arn:aws:s3:::b/*"])
    allow(role, ["xray:PutTraceSegments"], ["*"])  # actions without resource-level permissions
    allow(role, ["s3:GetObject"], ["arn:aws:s3:::b/*"], why="any key")


def test_explained_wildcards_pass_cdk_nag(tmp_path: Path) -> None:
    from aws_cdk import Validations  # noqa: PLC0415 - only this test runs the nag packs
    from cdk_nag import AwsSolutionsChecks  # noqa: PLC0415

    app = App(context={**CDK_CONTEXT, "aws:cdk:bundling-stacks": []}, outdir=str(tmp_path))
    stack = Stack(app, "S", env=Environment(account=ACCOUNT, region="us-east-1"))
    role = iam.Role(stack, "R", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    allow(role, ["s3:GetObject"], ["arn:aws:s3:::b/*"], why="objects of any key")
    Validations.of(app).add_plugins(AwsSolutionsChecks(app))

    app.synth()  # raises on an unacknowledged AwsSolutions-IAM5


# --- logs ----------------------------------------------------------------------------------------------------------


def test_vended_log_group_admits_only_the_named_delivery_sources() -> None:
    stack = new_stack()
    vended_log_group(
        stack, "G", name="/aws/vendedlogs/x/fast", delivery_sources="fast-x-*", retention=logs.RetentionDays.ONE_DAY
    )
    [group] = Template.from_stack(stack).find_resources("AWS::Logs::LogGroup").values()
    [statement] = group["Properties"]["ResourcePolicyDocument"]["Statement"]

    assert statement == {
        "Sid": "VendedLogDelivery",
        "Effect": "Allow",
        "Principal": {"Service": "delivery.logs.amazonaws.com"},
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/vendedlogs/x/fast:log-stream:*",
        "Condition": {
            "StringEquals": {"aws:SourceAccount": ACCOUNT},
            "ArnLike": {"aws:SourceArn": f"arn:aws:logs:us-east-1:{ACCOUNT}:delivery-source:fast-x-*"},
        },
    }


def test_deliveries_can_be_serialized() -> None:
    stack = new_stack()
    source = logs.CfnDeliverySource(
        stack, "Source", name="s", log_type="INFO_LOGS", resource_arn=f"arn:aws:events:us-east-1:{ACCOUNT}:event-bus/b"
    )
    first = deliver(
        stack, "First", source=source, destination_arn=f"arn:aws:logs:us-east-1:{ACCOUNT}:delivery-destination:d1"
    )
    deliver(
        stack,
        "Second",
        source=source,
        destination_arn=f"arn:aws:logs:us-east-1:{ACCOUNT}:delivery-destination:d2",
        after=first,
    )
    deliveries = Template.from_stack(stack).find_resources("AWS::Logs::Delivery")

    [second] = [d for k, d in deliveries.items() if k.startswith("Second")]
    assert second["DependsOn"] == [stack.get_logical_id(first)]
    assert second["Properties"]["DeliverySourceName"] == {"Ref": stack.get_logical_id(source)}


# --- providers and queues ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("asynchronous", [False, True])
def test_provider_framework_functions_log_at_info(asynchronous: bool) -> None:
    stack = new_stack()
    on_event = function(stack, "OnEvent")
    options: dict[str, Any] = {
        "is_complete": function(stack, "IsComplete"),
        "waiter_log_group_name": "/aws/vendedlogs/states/w",
    }
    _provider.provider(
        stack,
        "Provider",
        on_event=on_event,
        log_retention=logs.RetentionDays.ONE_DAY,
        **(options if asynchronous else {}),
    )
    template = Template.from_stack(stack)
    framework = [
        f["Properties"]
        for k, f in template.find_resources("AWS::Lambda::Function").items()
        if k.startswith("Providerframework")
    ]

    assert len(framework) == (3 if asynchronous else 1)
    assert {f["LoggingConfig"]["ApplicationLogLevel"] for f in framework} == {"INFO"}  # the L2 default is FATAL
    machines = template.find_resources("AWS::StepFunctions::StateMachine").values()
    assert [m["Properties"]["TracingConfiguration"] for m in machines] == ([{"Enabled": True}] if asynchronous else [])


def test_asynchronous_provider_needs_its_waiter_log_group() -> None:
    stack = new_stack()

    with pytest.raises(ValueError, match="waiter_log_group_name"):
        _provider.provider(
            stack,
            "Provider",
            on_event=function(stack, "OnEvent"),
            is_complete=function(stack, "IsComplete"),
            log_retention=logs.RetentionDays.ONE_DAY,
        )


@pytest.mark.parametrize(("removal", "policy"), [(RemovalPolicy.DESTROY, "Delete"), (RemovalPolicy.RETAIN, "Retain")])
def test_dead_letter_queue_is_encrypted_tls_only_and_follows_the_removal_policy(
    removal: RemovalPolicy, policy: str
) -> None:
    stack = new_stack()
    dead_letter_queue(stack, "DeadLetters", removal_policy=removal)
    template = Template.from_stack(stack)
    [queue] = template.find_resources("AWS::SQS::Queue").values()

    assert queue["DeletionPolicy"] == policy
    assert queue["Properties"] == {"MessageRetentionPeriod": 1209600, "SqsManagedSseEnabled": True}
    template.resource_count_is("AWS::SQS::QueuePolicy", 1)  # enforce_ssl


# --- module layout ---------------------------------------------------------------------------------------------------


def test_exports_live_in_one_module_and_the_stacks_re_export_them() -> None:
    assert data.DataExports is exports.DataExports
    assert media.MediaExports is exports.MediaExports
    assert media.ChannelExports is exports.ChannelExports


def test_split_packages_keep_their_v3_0_names() -> None:
    for name in ("EncodingLadder", "IngestPipeline", "LadderProvisioner", "LadderKit", "cfn_settings", "brand_digest"):
        assert hasattr(encoding, name), name
    for name in (
        "ChannelAssemblyOrigin",
        "MediaTailorLogs",
        "AdInsertion",
        "LinearChannel",
        "AsRunLogging",
        "RuntimeResourceReaper",
        "playback_configuration_template",
        "AS_RUN_LOG_GROUP",
    ):
        assert hasattr(channel_assembly, name), name
    for name in (
        "FastEventBus",
        "alert_topic",
        "cdn_error_alarm",
        "vended_log_group",
        "log_group_arn",
        "CLOUDFRONT_REGION",
    ):
        assert hasattr(observability, name), name
