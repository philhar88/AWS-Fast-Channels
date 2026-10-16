"""Channel Assembly constructs in an isolated stack, inside a stage with ``FastChannelsStage``'s Lambda and state
machine defaults, aspects, tags and cdk-nag packs, next to a real ``DataStack``."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App, Aspects, Environment, RemovalPolicy, Stack, Stage, Tags, Validations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Match, Template
from cdk_nag import AwsSolutionsChecks, ServerlessChecks

from fast_channels.core import specs
from fast_channels.core.profiles import profile
from fast_channels.core.timeline import ProgrammingBrief
from fast_channels.infra.config import ChannelTier, FastChannelsConfig
from fast_channels.infra.constructs.channel_assembly import (
    AS_RUN_LOG_GROUP,
    AsRunLogging,
    ChannelAssemblyOrigin,
    LinearChannel,
    MediaTailorLogs,
    RuntimeResourceReaper,
    playback_configuration_template,
)
from fast_channels.infra.constructs.functions import (
    FunctionLogGroups,
    LambdaDefaults,
    LogGroupDefaults,
    StateMachineDefaults,
)
from fast_channels.infra.stacks.data import DataStack
from fast_channels.infra.stacks.media import ChannelExports

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXT = json.loads((REPO_ROOT / "cdk.json").read_text())["context"] | {"aws:cdk:bundling-stacks": []}
CONTENT = "https://d111111abcdef8.cloudfront.net"
LADDER = "standard@86bf8088e0"
EXTERNAL_ADS = "https://ads.example.com/vast?sid=[session.uuid]"
OUTPUTS = ("hls", "hls-avc", "dash-avc")
RETENTION = logs.RetentionDays.ONE_WEEK


@dataclass(frozen=True)
class Built:
    app: App
    stack: Stack
    template: Template
    demo: LinearChannel
    news: LinearChannel
    vended_logs: MediaTailorLogs


def _stage(app: App) -> Stage:
    """A stage with the defaults ``FastChannelsStage`` registers (and the packs ``app.build`` adds)."""
    defaults = LogGroupDefaults(RETENTION, RemovalPolicy.DESTROY)
    stage = Stage(
        app,
        "FastChannels",
        env=Environment(account="123456789012", region="us-east-1"),
        property_injectors=[LambdaDefaults(defaults), StateMachineDefaults(defaults)],  # type: ignore[list-item]
    )
    Aspects.of(stage).add(FunctionLogGroups(defaults))
    for key, value in {"fast:app": "fast", "fast:owner": "iac"}.items():
        Tags.of(stage).add(key, value)
    Validations.of(app).add_plugins(AwsSolutionsChecks(app), ServerlessChecks(app))
    return stage


def _build(outdir: Path | None = None, removal: RemovalPolicy = RemovalPolicy.DESTROY) -> Built:
    app = App(context=CONTEXT, outdir=str(outdir) if outdir else None)
    stage = _stage(app)
    config = FastChannelsConfig.model_validate({"agent": {"enabled": False}, "channels": {"iac": []}})
    data_stack = DataStack(stage, "Data", config=config)
    stack = Stack(stage, "ChannelAssemblyTest")
    stack.add_dependency(data_stack)
    data = data_stack.exports
    origin = ChannelAssemblyOrigin(
        stack, "Origin", name="fast-media", bucket=data.media_bucket, content_base_url=CONTENT
    )
    vended_logs = MediaTailorLogs(stack, "MediaTailorLogs", prefix="fast", log_retention=RETENTION)
    reaper = RuntimeResourceReaper(
        stack,
        "Reaper",
        prefix="fast",
        origin=origin,
        library=data.library_table,
        log_retention=RETENTION,
        removal_policy=removal,
    )
    common: dict[str, Any] = {
        "ladder_version": LADDER,
        "filler_slate": "fast-filler",
        "origin": origin,
        "vended_logs": vended_logs,
        "library": data.library_table,
        "content_base_url": CONTENT,
        "log_retention": RETENTION,
    }
    demo = LinearChannel(stack, "Demo", name="fast-demo", outputs=OUTPUTS, brief=ProgrammingBrief(), **common)
    news = LinearChannel(
        stack,
        "News",
        name="fast-news",
        outputs=("hls-avc",),
        ads_url=EXTERNAL_ADS,
        tier=ChannelTier.STANDARD,
        restart_on_update=True,
        **common,
    )
    reaper.run_before_deleting(vended_logs)
    reaper.run_after_deleting(demo, news)
    AsRunLogging(
        stack, "AsRun", prefix="fast", library=data.library_table, as_run_retention_days=30, log_retention=RETENTION
    )
    return Built(app, stack, Template.from_stack(stack), demo, news, vended_logs)


@pytest.fixture(scope="module")
def built() -> Built:
    return _build()


@pytest.fixture(scope="module")
def template(built: Built) -> Template:
    return built.template


def one(template: Template, type_: str, props: Mapping[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """The logical id and definition of the single ``type_`` resource with ``props``."""
    [(logical_id, resource)] = template.find_resources(type_, {"Properties": props} if props else None).items()
    return logical_id, dict(resource)


def logical_id(built: Built, construct: Any) -> str:
    return built.stack.get_logical_id(construct.node.default_child if hasattr(construct, "node") else construct)


# --- origin --------------------------------------------------------------------------------------------------------


def test_source_location_is_s3_sigv4_on_the_regional_virtual_hosted_url(template: Template) -> None:
    _, location = one(template, "AWS::MediaTailor::SourceLocation")

    props = location["Properties"]
    assert props["SourceLocationName"] == "fast-media"
    assert props["AccessConfiguration"] == {"AccessType": "S3_SIGV4"}
    assert props["DefaultSegmentDeliveryConfiguration"] == {"BaseUrl": CONTENT}
    base = props["HttpConfiguration"]["BaseUrl"]["Fn::Join"][1]
    assert (base[0], base[2], base[3]) == ("https://", ".s3.us-east-1.", {"Ref": "AWS::URLSuffix"})
    assert {"Key": "fast:owner", "Value": "iac"} in props["Tags"]


def test_vod_source_registration_grant_covers_the_callers_manifest_read() -> None:
    stack = Stack(_stage(App(context=CONTEXT)), "Grant")
    origin = ChannelAssemblyOrigin(
        stack, "Origin", name="fast-media", bucket=s3.Bucket(stack, "Bucket"), content_base_url=CONTENT
    )
    role = iam.Role(stack, "Register", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))

    origin.grant_vod_sources(role)

    _, policy = one(Template.from_stack(stack), "AWS::IAM::Policy")
    statements = json.dumps(policy["Properties"]["PolicyDocument"]["Statement"])
    assert "mediatailor:CreateVodSource" in statements and ":vodSource/fast-media/*" in statements
    assert "s3:GetObject" in statements and "/assets/*" in statements


# --- channels ------------------------------------------------------------------------------------------------------


def test_channel_is_linear_with_filler_outputs_and_as_run_logs(built: Built, template: Template) -> None:
    _, channel = one(template, "AWS::MediaTailor::Channel", {"ChannelName": "fast-demo"})

    assert channel["Properties"] | {"Tags": None} == {
        "ChannelName": "fast-demo",
        "PlaybackMode": "LINEAR",
        "Tier": "BASIC",
        "FillerSlate": {"SourceLocationName": "fast-media", "VodSourceName": "fast-filler"},
        "Outputs": specs.channel_outputs([profile(o) for o in OUTPUTS]),
        "LogConfiguration": {"LogTypes": ["AS_RUN"]},
        "Tags": None,
    }
    guard, _ = one(template, "Custom::ChannelGuard", {"ChannelName": "fast-demo"})
    assert {guard, logical_id(built, built.stack.node.find_child("Origin"))} <= set(channel["DependsOn"])


def test_playback_configuration_is_the_core_spec(template: Template) -> None:
    endpoint, _ = one(template, "Custom::ChannelEndpoint", {"ChannelName": "fast-demo"})
    _, config = one(template, "AWS::MediaTailor::PlaybackConfiguration", {"Name": "fast-demo"})

    expected = specs.to_cfn_playback_configuration(
        specs.playback_configuration(
            name="fast-demo",
            video_content_source_url="<endpoint>",
            ads_url=specs.ads_url(content_base_url=CONTENT, ladder_version=LADDER, channel="fast-demo"),
            content_base_url=CONTENT,
            log_percent=100,
            tags={},
        )
    )
    props = config["Properties"]
    assert props["VideoContentSourceUrl"] == {"Fn::GetAtt": [endpoint, "ChannelUrl"]}
    assert props | {"VideoContentSourceUrl": "<endpoint>", "Tags": []} == expected


def test_token_content_hosts_survive_the_spec_builders() -> None:
    stack = Stack(_stage(App(context=CONTEXT)), "Tokens")
    bucket = s3.Bucket(stack, "Bucket")
    origin = ChannelAssemblyOrigin(stack, "Origin", name="fast-media", bucket=bucket, content_base_url=CONTENT)
    table = dynamodb.TableV2(
        stack, "Table", partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING)
    )
    LinearChannel(
        stack,
        "Demo",
        name="fast-demo",
        ladder_version=LADDER,
        outputs=("hls",),
        filler_slate="f",
        origin=origin,
        vended_logs=MediaTailorLogs(stack, "Logs", prefix="fast", log_retention=RETENTION),
        library=table,
        content_base_url=f"https://{bucket.bucket_regional_domain_name}",
        log_retention=RETENTION,
    )

    _, config = one(Template.from_stack(stack), "AWS::MediaTailor::PlaybackConfiguration")
    ads = json.dumps(config["Properties"]["AdDecisionServerUrl"])
    assert "RegionalDomainName" in ads and "/ads/standard_86bf8088e0/vast.xml?ch=fast-demo" in ads
    assert "cdk-token" not in json.dumps(config)


def test_external_ads_url_and_standard_tier(template: Template) -> None:
    _, config = one(template, "AWS::MediaTailor::PlaybackConfiguration", {"Name": "fast-news"})
    _, channel = one(template, "AWS::MediaTailor::Channel", {"ChannelName": "fast-news"})

    assert config["Properties"]["AdDecisionServerUrl"] == EXTERNAL_ADS
    assert channel["Properties"]["Tier"] == "STANDARD"


def test_channel_policy_admits_only_its_playback_configuration(template: Template) -> None:
    channel, _ = one(template, "AWS::MediaTailor::Channel", {"ChannelName": "fast-demo"})
    config, _ = one(template, "AWS::MediaTailor::PlaybackConfiguration", {"Name": "fast-demo"})
    _, policy = one(template, "AWS::MediaTailor::ChannelPolicy", {"ChannelName": "fast-demo"})

    assert policy["Properties"]["Policy"] == specs.channel_policy(
        channel_arn={"Fn::GetAtt": [channel, "Arn"]},  # type: ignore[arg-type]
        playback_configuration_arn={"Fn::GetAtt": [config, "PlaybackConfigurationArn"]},  # type: ignore[arg-type]
        account={"Ref": "AWS::AccountId"},  # type: ignore[arg-type]
    )


def test_lifecycle_resources_bracket_the_channel(template: Template) -> None:
    channel_id, channel = one(template, "AWS::MediaTailor::Channel", {"ChannelName": "fast-demo"})
    policy, _ = one(template, "AWS::MediaTailor::ChannelPolicy", {"ChannelName": "fast-demo"})
    guard_id, guard = one(template, "Custom::ChannelGuard", {"ChannelName": "fast-demo"})
    endpoint_id, endpoint = one(template, "Custom::ChannelEndpoint", {"ChannelName": "fast-demo"})
    _, lifecycle = one(template, "Custom::ChannelLifecycle", {"ChannelName": "fast-demo"})

    # The guard sees exactly what an update would change on the channel.
    assert guard["Properties"]["Spec"] == {
        k: v for k, v in channel["Properties"].items() if k not in {"ChannelName", "Tags"}
    }
    assert guard["Properties"]["RestartOnUpdate"] is False
    assert channel_id in endpoint["DependsOn"]
    assert policy in lifecycle["DependsOn"]
    props = lifecycle["Properties"]
    assert props["RestartToken"] == {"Fn::GetAtt": [guard_id, "StoppedForUpdate"]}
    assert json.loads(props["Brief"]) == ProgrammingBrief().model_dump(mode="json")
    # Attribute names of the tools' ChannelRecord, plus the URLs and ARNs the tools would otherwise look up.
    assert set(props["Record"]) == {
        "name",
        "arn",
        "tier",
        "ladder_version",
        "outputs",
        "playback_configuration_name",
        "playback_configuration_arn",
        "hls_prefix",
        "dash_prefix",
        "channel_url",
        "source_location",
        "filler_slate",
    }
    assert props["Record"]["channel_url"] == {"Fn::GetAtt": [endpoint_id, "ChannelUrl"]}
    assert props["Record"]["outputs"] == list(OUTPUTS)


def test_channels_share_one_provider_scoped_to_their_channels(template: Template) -> None:
    providers = {
        r["Properties"]["ServiceToken"]["Fn::GetAtt"][0]
        for r in template.find_resources("Custom::ChannelLifecycle").values()
    }
    assert len(providers) == 1
    [policy] = [
        v["Properties"]["PolicyDocument"]["Statement"]
        for v in template.find_resources("AWS::IAM::Policy").values()
        if any("mediatailor:DeleteProgram" in json.dumps(s) for s in v["Properties"]["PolicyDocument"]["Statement"])
        and "LinearChannelProvider" in json.dumps(v["Properties"]["Roles"])
    ]
    resources = json.dumps([s["Resource"] for s in policy])
    assert ":channel/fast-demo" in resources and ":channel/fast-news" in resources and ":channel/*" not in resources
    # The guard checks for a rollback of its own stack only.
    assert {"Action": "cloudformation:DescribeStacks", "Effect": "Allow", "Resource": {"Ref": "AWS::StackId"}} in policy
    leading = [s["Condition"] for s in policy if "dynamodb:UpdateItem" in json.dumps(s["Action"])]
    assert {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["CHANNEL#fast-demo"]}} in leading


def test_exports_fill_channel_exports(built: Built) -> None:
    exports = built.demo.exports

    assert isinstance(exports, ChannelExports)
    assert (exports.name, exports.ladder_version, exports.outputs, exports.playback_configuration_name) == (
        "fast-demo",
        LADDER,
        OUTPUTS,
        "fast-demo",
    )
    resolved = built.stack.resolve([exports.arn, exports.hls_prefix, exports.dash_prefix])
    assert [r["Fn::GetAtt"][1] for r in resolved] == [
        "Arn",
        "HlsConfiguration.ManifestEndpointPrefix",
        "DashConfiguration.ManifestEndpointPrefix",
    ]


# --- vended logs ---------------------------------------------------------------------------------------------------


def test_one_log_group_scoped_destination_for_every_playback_configuration(template: Template) -> None:
    _, log_group = one(template, "AWS::Logs::LogGroup", {"LogGroupName": "/aws/vendedlogs/mediatailor/fast"})
    destination, _ = one(
        template, "AWS::Logs::DeliveryDestination", {"Name": "fast-mediatailor", "OutputFormat": "json"}
    )

    assert log_group["Properties"]["RetentionInDays"] == 7
    [statement] = log_group["Properties"]["ResourcePolicyDocument"]["Statement"]
    assert statement["Principal"] == {"Service": "delivery.logs.amazonaws.com"}
    logs_arn = "arn:aws:logs:us-east-1:123456789012"
    assert statement["Resource"] == f"{logs_arn}:log-group:/aws/vendedlogs/mediatailor/fast:log-stream:*"
    assert statement["Condition"] == {
        "StringEquals": {"aws:SourceAccount": "123456789012"},
        "ArnLike": {"aws:SourceArn": f"{logs_arn}:delivery-source:fast-*"},
    }
    template.resource_count_is("AWS::Logs::ResourcePolicy", 0)  # never the account-wide policy (_review.md)
    sources = template.find_resources("AWS::Logs::DeliverySource")
    assert sorted((s["Properties"]["Name"], s["Properties"]["LogType"]) for s in sources.values()) == sorted(
        (f"{channel}-{suffix}", log_type)
        for channel in ("fast-demo", "fast-news")
        for log_type, suffix in (
            ("AD_DECISION_SERVER_LOGS", "ads"),
            ("MANIFEST_SERVICE_LOGS", "manifest"),
            ("TRANSCODE_LOGS", "transcode"),
        )
    )
    deliveries = template.find_resources("AWS::Logs::Delivery")
    assert {json.dumps(d["Properties"]["DeliveryDestinationArn"]) for d in deliveries.values()} == {
        json.dumps({"Fn::GetAtt": [destination, "Arn"]})
    }
    chained = [d for d in deliveries.values() if any(dep in deliveries for dep in d.get("DependsOn", []))]
    assert len(chained) == len(deliveries) - 1  # created one after another


def test_playback_configuration_template_lists_what_agent_channels_share(built: Built) -> None:
    preconditioned = json.loads(
        built.stack.resolve(
            playback_configuration_template(
                built.stack, content_base_url=CONTENT, log_destination_arn="arn:dest", log_percent=10
            )
        )
    )
    external = json.loads(
        built.stack.resolve(
            playback_configuration_template(
                built.stack,
                content_base_url=CONTENT,
                log_destination_arn="arn:dest",
                log_percent=10,
                ads_url=EXTERNAL_ADS,
            )
        )
    )

    assert preconditioned == {
        "content_base_url": CONTENT,
        "log_percent": 10,
        "log_destination_arn": "arn:dest",
        "log_types": {
            "AD_DECISION_SERVER_LOGS": "ads",
            "MANIFEST_SERVICE_LOGS": "manifest",
            "TRANSCODE_LOGS": "transcode",
        },
    }
    assert external == preconditioned | {"ads_url": EXTERNAL_ADS}


# --- as-run --------------------------------------------------------------------------------------------------------


def test_as_run_group_gets_retention_and_a_reconciler(template: Template) -> None:
    ensure, _ = one(template, "Custom::AsRunLogGroup", {"LogGroupName": AS_RUN_LOG_GROUP, "RetentionInDays": 30})
    _, subscription = one(template, "AWS::Logs::SubscriptionFilter")
    _, reconciler = one(
        template, "AWS::Lambda::Function", {"Handler": "fast_channels.functions.as_run_reconciler.handler"}
    )

    assert subscription["Properties"]["LogGroupName"] == AS_RUN_LOG_GROUP
    assert subscription["Properties"]["FilterName"] == "fast-as-run"
    assert subscription["Properties"]["FilterPattern"] == (
        '{ ($.eventType = "VodProgramStart") && ($.channelName = "fast-*") }'
    )
    assert ensure in subscription["DependsOn"]
    assert "LIBRARY_TABLE" in reconciler["Properties"]["Environment"]["Variables"]
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "Principal": "logs.amazonaws.com",
            "SourceArn": Match.string_like_regexp(f"log-group:{AS_RUN_LOG_GROUP}:\\*$"),
        },
    )


# --- reaper --------------------------------------------------------------------------------------------------------


def test_reaper_runs_after_channels_and_before_the_containers(built: Built, template: Template) -> None:
    reaper_id, reaper = one(template, "Custom::RuntimeResourceReaper")

    assert reaper["Properties"] | {"TableName": None, "ServiceToken": None} == {
        "Prefix": "fast",
        "SourceLocation": "fast-media",
        "JobTemplateCategory": "fast",
        "TableName": None,
        "Enabled": True,
        "ServiceToken": None,
    }
    origin = logical_id(built, built.stack.node.find_child("Origin"))
    destination, _ = one(template, "AWS::Logs::DeliveryDestination")
    assert {origin, destination} <= set(reaper["DependsOn"])
    for type_ in ("AWS::MediaTailor::Channel", "Custom::ChannelLifecycle", "AWS::Logs::Delivery"):
        assert all(reaper_id in r.get("DependsOn", []) for r in template.find_resources(type_).values()), type_


def test_reaper_waiter_logs_everything_and_is_traced(template: Template) -> None:
    _, waiter = one(template, "AWS::StepFunctions::StateMachine")

    assert waiter["Properties"]["TracingConfiguration"] == {"Enabled": True}
    logging = waiter["Properties"]["LoggingConfiguration"]
    assert (logging["Level"], logging["IncludeExecutionData"]) == ("ALL", True)
    template.has_resource_properties(
        "AWS::Logs::LogGroup", {"LogGroupName": "/aws/vendedlogs/states/fast-reaper", "RetentionInDays": 7}
    )


def test_reaper_is_disabled_when_data_is_retained() -> None:
    _, reaper = one(_build(removal=RemovalPolicy.RETAIN).template, "Custom::RuntimeResourceReaper")

    assert reaper["Properties"]["Enabled"] is False


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def test_reaper_permissions_are_scoped_to_runtime_names(template: Template) -> None:
    [statements] = [
        v["Properties"]["PolicyDocument"]["Statement"]
        for v in template.find_resources("AWS::IAM::Policy").values()
        if "mediatailor:DeleteVodSource" in json.dumps(v)
    ]
    by_action = {
        a: json.dumps(s["Resource"])
        for s in statements
        for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    }

    by_condition = {a: s.get("Condition") for s in statements for a in as_list(s["Action"])}

    # "fast-" with its hyphen: deployment fast must never reach fastci-* resources.
    assert by_action["mediatailor:DeleteChannel"].endswith(':channel/fast-agent-*"')
    assert by_action["mediatailor:DeleteVodSource"].endswith(':vodSource/fast-*/*"')
    # ListVodSources is authorized on vodSource/<location>/*, not on the source location (observed live).
    assert by_action["mediatailor:ListVodSources"] == by_action["mediatailor:DeleteVodSource"]
    assert by_action["mediatailor:DeleteSourceLocation"].endswith(':sourceLocation/fast-*"')
    assert by_action["mediaconvert:DeleteJobTemplate"].endswith(':jobTemplates/fast-*"')
    assert by_action["logs:DeleteDeliverySource"].endswith(':delivery-source:fast-agent-*"')
    assert by_condition["logs:DeleteDelivery"] == {"StringEquals": {"aws:ResourceTag/fast:app": "fast"}}
    assert by_action["cloudformation:DescribeStacks"] == json.dumps({"Ref": "AWS::StackId"})
    assert by_action["mediatailor:ListChannels"] == '"*"'


# --- stage-wide properties -----------------------------------------------------------------------------------------


def test_every_log_group_has_retention(template: Template) -> None:
    groups = template.find_resources("AWS::Logs::LogGroup")

    assert groups and all(g["Properties"].get("RetentionInDays") == 7 for g in groups.values())
    functions = template.find_resources("AWS::Lambda::Function")
    assert all("LogGroup" in f["Properties"]["LoggingConfig"] for f in functions.values())
    assert all(f["Properties"]["TracingConfig"] == {"Mode": "Active"} for f in functions.values())


def test_cdk_nag_has_no_unacknowledged_findings(tmp_path: Path) -> None:
    built = _build(outdir=tmp_path)

    built.app.synth()  # raises ValidationFailed listing every unacknowledged finding

    reports = json.loads((tmp_path / "validation-report.json").read_text())["pluginReports"]
    assert [v for r in reports for v in r.get("violations", [])] == []
