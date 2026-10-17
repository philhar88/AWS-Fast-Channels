"""Encoding constructs in an isolated test stack of a FAST Channels stage (media-only topology): the stage's
runtime defaults, weak cross-stack references to the Data stack and cdk-nag apply as they will in MediaStack."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk.assertions import Template
from PIL import Image

from fast_channels.core.ladder import Ladder
from fast_channels.core.mediaconvert import compile_job_settings
from fast_channels.core.naming import ladder_path
from fast_channels.core.timeline import BREAK_SLATE_GOPS
from fast_channels.infra.app import build as app_build
from fast_channels.infra.bundling import REPO_ROOT
from fast_channels.infra.constructs.encoding import (
    BRAND_DIR,
    VIDEO_EXTENSIONS,
    EncodingLadder,
    InferenceCaptioner,
    IngestPipeline,
    LadderKit,
    LadderProvisioner,
    brand_digest,
    cfn_settings,
)

STANDARD = Ladder.builtin("standard")
PROFILES = ("hls", "hls-avc", "hls-hevc", "hls-av1", "dash-avc")
STACK = "EncodingUnderTest"
HANDLERS = "fast_channels.functions.ingest"


@dataclass(frozen=True)
class Encoding:
    app: App
    stack: Stack
    pipeline: IngestPipeline
    ladder: EncodingLadder
    provisioner: LadderProvisioner
    kit: LadderKit

    @property
    def template(self) -> Template:
        return Template.from_stack(self.stack)


@dataclass(frozen=True)
class ImportedCaptioner:
    """A captioner the pipeline only knows by its state machine (any implementation of the contract)."""

    engine: str
    state_machine: sfn.IStateMachine


CAPTIONER_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:fast-captioner"


def build(outdir: Path | None = None, *, captioner: bool = False) -> Encoding:
    """A media-only stage (cdk.json context, bundling off) with the encoding constructs in their own stack."""
    context = json.loads((REPO_ROOT / "cdk.json").read_text())["context"]
    app = App(
        context={**context, "aws:cdk:bundling-stacks": [], "fast-channels.agent.enabled": False},
        outdir=str(outdir) if outdir else None,
    )
    stage = app_build(app, account="123456789012", region="us-east-1")
    data = stage.data.exports
    stack = Stack(stage, STACK)
    imported = sfn.StateMachine.from_state_machine_arn(stack, "Captioner", CAPTIONER_ARN)
    pipeline = IngestPipeline(
        stack,
        "Ingest",
        prefix="fast",
        ingest_bucket=data.ingest_bucket,
        media_bucket=data.media_bucket,
        library=data.library_table,
        event_bus=events.EventBus(stack, "Bus", event_bus_name="fast-channels"),
        source_location_name="fast-origin",
        content_base_url="https://media.example.com",
        package_profiles=PROFILES,
        default_ladder_version=STANDARD.version,
        log_retention=logs.RetentionDays.ONE_WEEK,
        captioner=ImportedCaptioner("elemental-inference", imported) if captioner else None,
        captions_required=captioner,
    )
    ladder = EncodingLadder(stack, "Standard", ladder=STANDARD, prefix="fast", queue_arn=pipeline.queue_arn)
    provisioner = LadderProvisioner(stack, "Kits", pipeline=pipeline)
    kit = LadderKit(stack, "StandardKit", ladder=ladder, provisioner=provisioner)
    return Encoding(app, stack, pipeline, ladder, provisioner, kit)


@pytest.fixture(scope="module")
def encoding() -> Encoding:
    return build()


@pytest.fixture(scope="module")
def template(encoding: Encoding) -> Template:
    return encoding.template


def resource(template: Template, type_: str, logical_prefix: str) -> Mapping[str, Any]:
    [found] = [v for k, v in template.find_resources(type_).items() if k.startswith(logical_prefix)]
    return found


def definition(template: Template, logical_prefix: str) -> dict[str, Any]:
    """The state machine's definition with every intrinsic replaced by ``<ref>``."""
    body = resource(template, "AWS::StepFunctions::StateMachine", logical_prefix)["Properties"]["DefinitionString"]
    parts = body["Fn::Join"][1] if isinstance(body, dict) else [body]
    loaded: dict[str, Any] = json.loads("".join(p if isinstance(p, str) else "<ref>" for p in parts))
    return loaded


def policy_statements(template: Template, role_prefix: str) -> list[dict[str, Any]]:
    policies = [p for k, p in template.find_resources("AWS::IAM::Policy").items() if k.startswith(f"{role_prefix}")]
    return [s for p in policies for s in p["Properties"]["PolicyDocument"]["Statement"]]


def actions(statements: list[dict[str, Any]]) -> set[str]:
    return {a for s in statements for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}


# --- EncodingLadder ------------------------------------------------------------------------------------------------


def test_job_template_is_the_compiled_ladder(template: Template) -> None:
    props = resource(template, "AWS::MediaConvert::JobTemplate", "Standard")["Properties"]

    assert props["Name"] == f"fast-{ladder_path(STANDARD.version)}"
    assert props["Category"] == "fast"
    assert props["SettingsJson"] == cfn_settings(compile_job_settings(STANDARD))
    cmfc = props["SettingsJson"]["OutputGroups"][0]["Outputs"][0]["ContainerSettings"]["CmfcSettings"]
    assert cmfc["IframeOnlyManifest"] == "EXCLUDE" and "IFrameOnlyManifest" not in cmfc
    assert props["Queue"]["Fn::GetAtt"][0].startswith("IngestQueue")
    assert props["AccelerationSettings"] == {"Mode": "PREFERRED"}
    assert props["StatusUpdateInterval"] == "SECONDS_60"
    assert props["Tags"] == {"fast:app": "fast", "fast:ladder": STANDARD.version, "fast:owner": "iac"}


# --- IngestPipeline ------------------------------------------------------------------------------------------------


def test_one_on_demand_queue(template: Template) -> None:
    template.resource_count_is("AWS::MediaConvert::Queue", 1)
    template.has_resource_properties(
        "AWS::MediaConvert::Queue",
        {"Name": "fast-ingest", "PricingPlan": "ON_DEMAND", "ConcurrentJobs": 20, "Status": "ACTIVE"},
    )


def test_mediaconvert_role_is_confused_deputy_safe_and_least_privilege(template: Template) -> None:
    role = resource(template, "AWS::IAM::Role", "IngestMediaConvertRole")["Properties"]
    [trust] = role["AssumeRolePolicyDocument"]["Statement"]

    assert trust["Principal"] == {"Service": "mediaconvert.amazonaws.com"}
    assert trust["Condition"]["StringEquals"] == {"aws:SourceAccount": "123456789012"}
    assert trust["Condition"]["ArnEquals"]["aws:SourceArn"]["Fn::GetAtt"][0].startswith("IngestQueue")
    statements = policy_statements(template, "IngestMediaConvertRole")
    resources = (s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]] for s in statements)
    suffixes = [sorted(r["Fn::Join"][1][-1] for r in rs) for rs in resources]
    assert list(zip((s["Action"] for s in statements), suffixes, strict=True)) == [
        ("s3:GetObject", ["/*"]),  # mezzanines and caption files of the ingest bucket
        ("s3:PutObject", ["/assets/*", "/work/*"]),  # encodes; caption scans and audio proxies
    ]
    assert "ManagedPolicyArns" not in role


def test_state_machine_logs_everything_and_traces(template: Template) -> None:
    props = resource(template, "AWS::StepFunctions::StateMachine", "IngestStateMachine")["Properties"]
    log_group = resource(template, "AWS::Logs::LogGroup", "IngestLogs")["Properties"]

    assert props["StateMachineName"] == "fast-ingest"
    assert props["LoggingConfiguration"]["Level"] == "ALL"
    assert props["LoggingConfiguration"]["IncludeExecutionData"] is True
    assert props["TracingConfiguration"] == {"Enabled": True}
    assert log_group == {**log_group, "LogGroupName": "/aws/vendedlogs/states/fast-ingest", "RetentionInDays": 7}


def test_ingest_definition(template: Template) -> None:
    body = definition(template, "IngestStateMachine")
    states = body["States"]

    assert body["QueryLanguage"] == "JSONata"
    assert body["StartAt"] == "Prepare"
    assert [states[s].get("Next") for s in ("Prepare", "Transcode", "Finalize", "Register")] == [
        "AwaitsCaptionScan",
        "Finalize",
        "Register",
        "Publish",
    ]
    # Without a captioner: the scan may find embedded captions, and nothing is generated.
    assert states["AwaitsCaptionScan"]["Choices"] == [
        {"Condition": "{% $asset.captions.awaiting = 'scan' %}", "Next": "ScanCaptions"}
    ]
    assert states["AwaitsCaptionScan"]["Default"] == "Transcode"
    scan = states["ScanCaptions"]
    assert scan["Resource"].endswith(":states:::mediaconvert:createJob.sync")
    assert scan["Arguments"]["Settings"] == "{% $asset.caption_scan.Settings %}"
    assert scan["Arguments"]["Role"] == "<ref>" and "JobTemplate" not in scan["Arguments"]
    assert scan["Next"] == "CaptionsScanned" and states["CaptionsScanned"]["Next"] == "Transcode"
    assert states["CaptionsScanned"]["Arguments"]["Payload"] == {"step": "scanned", "asset": "{% $asset %}"}
    assert states["CaptionsScanned"]["Assign"] == {"asset": "{% $states.result.Payload %}"}
    assert "GenerateCaptions" not in states
    transcode = states["Transcode"]
    assert transcode["Resource"].endswith(":states:::mediaconvert:createJob.sync")
    assert transcode["Arguments"]["Role"] == "<ref>" and transcode["Arguments"]["Queue"] == "<ref>"
    assert transcode["Arguments"]["JobTemplate"] == "{% $asset.job.JobTemplate %}"
    assert transcode["Arguments"]["Settings"] == "{% $asset.job.Settings %}"
    assert "Tags" not in transcode["Arguments"]  # Tags would need mediaconvert:TagResource (mediaconvert.md §7.4)
    assert transcode["Retry"][0]["ErrorEquals"] == [
        "MediaConvert.TooManyRequestsException",
        "MediaConvert.InternalServerErrorException",
    ]
    register = states["Register"]
    assert register["Type"] == "Map" and register["MaxConcurrency"] == 1
    assert register["Items"] == "{% $encoded.registrations %}"
    assert register["ItemProcessor"]["States"]["RegisterVodSource"]["Retry"][1]["ErrorEquals"] == ["VodSourceBusyError"]
    assert states["Publish"]["Arguments"]["Payload"]["status"] == "READY"
    for step in ("Prepare", "ScanCaptions", "CaptionsScanned", "Transcode", "Finalize", "Register", "Publish"):
        assert states[step]["Catch"][-1] == {
            "ErrorEquals": ["States.ALL"],
            "Next": "PublishFailure",
            "Output": "{% $states.errorOutput %}",
        }
    assert states["Prepare"]["Catch"][0]["Next"] == "AlreadyIngesting"
    assert states["PublishFailure"]["Next"] == "IngestFailed"
    assert states["IngestFailed"]["Type"] == "Fail"


def test_a_captioner_generates_the_captions_nothing_else_provides() -> None:
    template = build(captioner=True).template
    states = definition(template, "IngestStateMachine")["States"]

    assert states["CaptionsScanned"]["Next"] == "AwaitsCaptionGeneration"
    assert states["AwaitsCaptionGeneration"]["Choices"] == [
        {"Condition": "{% $asset.captions.awaiting = 'generation' %}", "Next": "GenerateCaptions"}
    ]
    assert states["AwaitsCaptionGeneration"]["Default"] == "Transcode"
    generate = states["GenerateCaptions"]
    assert generate["Resource"].endswith(":states:::states:startExecution.sync:2")
    assert generate["Arguments"]["StateMachineArn"] == CAPTIONER_ARN
    assert generate["Arguments"]["Input"] == "{% $asset.captioner_input %}"
    assert generate["Catch"] == [
        {"ErrorEquals": ["States.ALL"], "Next": "CaptionGenerationFailed", "Output": "{% $states.errorOutput %}"}
    ]
    assert generate["Next"] == "CaptionsGenerated" and states["CaptionsGenerated"]["Next"] == "Transcode"
    assert states["CaptionsGenerated"]["Arguments"]["Payload"]["result"] == "{% $states.input.Output %}"
    assert states["CaptionGenerationFailed"]["Arguments"]["Payload"]["step"] == "failed"
    assert states["CaptionGenerationFailed"]["Next"] == "Transcode"
    prepare = function(template, "IngestPrepare")["Environment"]["Variables"]
    assert (prepare["CAPTIONER"], prepare["CAPTIONS_REQUIRED"]) == ("elemental-inference", "true")
    machine = [
        s for s in policy_statements(template, "IngestStateMachineRole") if "states:StartExecution" in actions([s])
    ]
    assert machine and machine[0]["Resource"] == CAPTIONER_ARN


def function(template: Template, logical_prefix: str) -> Mapping[str, Any]:
    props: Mapping[str, Any] = resource(template, "AWS::Lambda::Function", logical_prefix)["Properties"]
    return props


@pytest.mark.parametrize(
    ("logical", "module", "variables"),
    [
        (
            "IngestPrepare",
            "prepare",
            {"FAST_PREFIX", "LIBRARY_TABLE", "MEDIA_BUCKET", "INGEST_BUCKET", "CAPTIONS_REQUIRED", "CAPTIONER"},
        ),
        ("IngestCaptions", "captions", {"INGEST_BUCKET", "CAPTIONS_REQUIRED", "CAPTIONER"}),
        ("IngestFinalize", "finalize", {"LIBRARY_TABLE", "MEDIA_BUCKET", "CONTENT_BASE_URL", "PACKAGE_PROFILES"}),
        ("IngestRegister", "register", {"FAST_PREFIX", "SOURCE_LOCATION"}),
        ("IngestPublish", "publish", {"FAST_PREFIX", "LIBRARY_TABLE", "EVENT_BUS"}),
        (
            "IngestTrigger",
            "trigger",
            {"FAST_PREFIX", "INGEST_STATE_MACHINE_ARN", "DEFAULT_LADDER_VERSION", "EVENT_BUS"},
        ),
        ("KitsKit", "kit", {"FAST_PREFIX", "LIBRARY_TABLE", "MEDIA_BUCKET", "CONTENT_BASE_URL", "EVENT_BUS"}),
    ],
)
def test_step_functions(template: Template, logical: str, module: str, variables: set[str]) -> None:
    props = [p for k, p in template.find_resources("AWS::Lambda::Function").items() if k.startswith(f"{logical}")]
    [fn] = [p["Properties"] for p in props if p["Properties"]["Handler"] == f"{HANDLERS}.{module}.handler"]

    assert set(fn["Environment"]["Variables"]) == variables | {
        "POWERTOOLS_SERVICE_NAME",
        "POWERTOOLS_METRICS_NAMESPACE",
    }
    assert fn["Environment"]["Variables"]["POWERTOOLS_SERVICE_NAME"] == f"ingest.{module}"
    assert (fn["Runtime"], fn["Architectures"], fn["TracingConfig"]) == ("python3.14", ["arm64"], {"Mode": "Active"})


def test_finalize_and_register_settings(template: Template) -> None:
    finalize = function(template, "IngestFinalize")["Environment"]["Variables"]
    register = function(template, "IngestRegister")["Environment"]["Variables"]

    assert finalize["PACKAGE_PROFILES"] == ",".join(PROFILES)
    assert finalize["CONTENT_BASE_URL"] == "https://media.example.com"
    assert register["SOURCE_LOCATION"] == "fast-origin"


def test_dynamodb_grants_are_item_level_on_the_table(template: Template) -> None:
    statements = [
        s
        for k, p in template.find_resources("AWS::IAM::Policy").items()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if any(str(a).startswith("dynamodb:") for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"]))
    ]

    assert actions(statements) == {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"}
    assert all("index" not in json.dumps(s["Resource"]) for s in statements)


def test_register_may_only_touch_vod_sources_of_the_origin(template: Template) -> None:
    statements = policy_statements(template, "IngestRegisterRole")
    [mediatailor] = [s for s in statements if "mediatailor:CreateVodSource" in s["Action"]]

    assert set(mediatailor["Action"]) == {
        "mediatailor:DescribeVodSource",
        "mediatailor:CreateVodSource",
        "mediatailor:UpdateVodSource",
        "mediatailor:TagResource",
    }
    assert mediatailor["Resource"] == "arn:aws:mediatailor:us-east-1:123456789012:vodSource/fast-origin/*"
    [alerts] = [s for s in statements if s["Action"] == "mediatailor:ListAlerts"]
    assert alerts["Resource"] == "arn:aws:mediatailor:us-east-1:123456789012:alerts/*"


def test_uploads_and_requests_trigger_ingests(template: Template) -> None:
    rules = template.find_resources("AWS::Events::Rule")
    uploads = resource(template, "AWS::Events::Rule", "IngestUploads")["Properties"]
    requests = resource(template, "AWS::Events::Rule", "IngestRequests")["Properties"]

    assert len(rules) == 2
    assert uploads["EventPattern"]["source"] == ["aws.s3"]
    assert uploads["EventPattern"]["detail-type"] == ["Object Created"]
    assert uploads["EventPattern"]["detail"]["object"]["key"] == [
        {"suffix": {"equals-ignore-case": extension}} for extension in VIDEO_EXTENSIONS
    ]
    assert "Fn::GetStackOutput" in json.dumps(uploads["EventPattern"]["detail"]["bucket"]["name"])
    assert "EventBusName" not in uploads  # S3 delivers to the default bus
    assert requests["EventPattern"] == {"source": ["fast-channels.fast"], "detail-type": ["Ingest Requested"]}
    for rule in (uploads, requests):
        [target] = rule["Targets"]
        assert target["Arn"]["Fn::GetAtt"][0].startswith("IngestTrigger")
        assert target["DeadLetterConfig"]["Arn"]["Fn::GetAtt"][0].startswith("IngestDeadLetters")
        assert target["RetryPolicy"] == {"MaximumEventAgeInSeconds": 21600, "MaximumRetryAttempts": 8}


def test_failed_trigger_invocations_are_dead_lettered(template: Template) -> None:
    config = resource(template, "AWS::Lambda::EventInvokeConfig", "IngestTrigger")["Properties"]

    assert config["MaximumRetryAttempts"] == 2
    assert config["DestinationConfig"]["OnFailure"]["Destination"]["Fn::GetAtt"][0].startswith("IngestDeadLetters")
    queue = resource(template, "AWS::SQS::Queue", "IngestDeadLetters")["Properties"]
    assert queue["MessageRetentionPeriod"] == 14 * 86400 and queue["SqsManagedSseEnabled"] is True


# --- LadderProvisioner and LadderKit -------------------------------------------------------------------------------


def test_brand_stills_are_deployed_under_their_digest(template: Template) -> None:
    deployment = resource(template, "Custom::CDKBucketDeployment", "KitsBrand")["Properties"]

    assert deployment["DestinationBucketKeyPrefix"] == f"brand/{brand_digest()}/"
    assert deployment["Prune"] is True and deployment["RetainOnDelete"] is False


def test_provisioner_definition(template: Template) -> None:
    props = resource(template, "AWS::StepFunctions::StateMachine", "KitsStateMachine")["Properties"]
    body = definition(template, "KitsStateMachine")
    states = body["States"]

    assert props["StateMachineName"] == "fast-ladder-provisioner"
    assert props["LoggingConfiguration"]["Level"] == "ALL" and props["TracingConfiguration"] == {"Enabled": True}
    assert [body["StartAt"], states["Begin"]["Next"], states["EncodeKit"]["Next"]] == ["Begin", "EncodeKit", "Complete"]
    images = states["Begin"]["Arguments"]["Payload"]["images"]
    assert images["filler"] == f"s3://<ref>/brand/{brand_digest()}/filler.png"
    assert len(images["house_ads"]) == 2
    encode = states["EncodeKit"]
    assert encode["Items"] == "{% $kit.requests %}" and encode["MaxConcurrency"] == 4
    child = encode["ItemProcessor"]["States"]["Ingest"]
    assert child["Resource"].endswith(":states:::states:startExecution.sync:2")
    assert child["Arguments"]["Name"] == "{% $states.input.correlation_id %}"
    assert "AWS_STEP_FUNCTIONS_STARTED_BY_EXECUTION_ID" in child["Arguments"]["Input"]
    assert states["RecordFailure"]["Next"] == "ProvisioningFailed"


def test_ladder_kit_custom_resource(encoding: Encoding, template: Template) -> None:
    kit = resource(template, "Custom::LadderKit", "StandardKit")
    props = kit["Properties"]

    assert props["LadderVersion"] == STANDARD.version
    assert Ladder.model_validate_json(props["Ladder"]) == STANDARD
    assert props["JobTemplate"] == encoding.ladder.template_name
    assert props["BrandDigest"] == brand_digest()
    assert props["PackageProfiles"] == ",".join(PROFILES)
    assert props["SourceLocation"] == "fast-origin"
    assert any(d.startswith("StandardC") or d.startswith("Standard") for d in kit["DependsOn"])
    assert any(d.startswith("KitsBrand") for d in kit["DependsOn"])
    assert set(encoding.kit.break_slate_vod_sources) == set(BREAK_SLATE_GOPS)


def test_kit_provider_waits_up_to_an_hour_and_its_waiter_logs_and_traces(template: Template) -> None:
    waiter = resource(template, "AWS::StepFunctions::StateMachine", "KitsKitProviderwaiterstatemachine")["Properties"]
    body = definition(template, "KitsKitProviderwaiterstatemachine")

    assert waiter["LoggingConfiguration"]["Level"] == "ALL"
    assert waiter["TracingConfiguration"] == {"Enabled": True}
    retry = body["States"]["framework-isComplete-task"]["Retry"][0]
    assert retry["IntervalSeconds"] * retry["MaxAttempts"] <= 3600


def test_every_log_group_has_retention(template: Template) -> None:
    groups = template.find_resources("AWS::Logs::LogGroup")

    assert groups and all(g["Properties"].get("RetentionInDays") == 7 for g in groups.values())
    names = {g["Properties"].get("LogGroupName") for g in groups.values()}
    assert {
        "/aws/vendedlogs/states/fast-ingest",
        "/aws/vendedlogs/states/fast-ladder-provisioner",
        "/aws/vendedlogs/states/fast-ladder-kit-waiter",
    } <= names


def test_no_unacknowledged_nag_findings(tmp_path: Path) -> None:
    encoding = build(tmp_path)
    # A canary finding proves cdk-nag evaluated the stack (reports without violations are left out).
    canary = iam.Role(encoding.stack, "Canary", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    canary.add_to_policy(iam.PolicyStatement(actions=["s3:GetObject"], resources=["*"]))

    with pytest.raises(RuntimeError, match="ValidationFailed"):
        encoding.app.synth()

    reports = json.loads((tmp_path / "validation-report.json").read_text())["pluginReports"]
    findings = {
        (v["ruleName"], c["constructPath"].removeprefix(f"FastChannels/{STACK}/"))
        for r in reports
        for v in r.get("violations", [])
        for c in v.get("violatingConstructs", [])
        if f"/{STACK}/" in c["constructPath"]
    }
    assert findings == {("AwsSolutions-IAM5[Resource::*]", "Canary/DefaultPolicy/Resource")}


# --- brand stills --------------------------------------------------------------------------------------------------


def test_brand_stills_are_full_hd_rgb_pngs() -> None:
    stills = sorted(p.name for p in BRAND_DIR.glob("*.png"))

    assert stills == ["break.png", "filler.png", "house-ad-1.png", "house-ad-2.png"]
    for name in stills:
        with Image.open(BRAND_DIR / name) as image:
            assert (image.format, image.size, image.mode) == ("PNG", (1920, 1080), "RGB")


def test_brand_digest_follows_the_files(tmp_path: Path) -> None:
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(name.encode())
    before = brand_digest(tmp_path)
    (tmp_path / "b.png").write_bytes(b"changed")

    assert len(before) == 10 and brand_digest(tmp_path) != before


def test_cfn_settings_spell_leading_acronyms_like_the_java_sdk() -> None:
    settings = {
        "IFrameOnlyManifest": "INCLUDE",
        "H264Settings": {"GopBReference": "ENABLED"},
        "Outputs": [{"S3Settings": 1}],
    }

    assert cfn_settings(settings) == {
        "IframeOnlyManifest": "INCLUDE",
        "H264Settings": {"GopBReference": "ENABLED"},
        "Outputs": [{"S3Settings": 1}],
    }


# --- InferenceCaptioner -------------------------------------------------------------------------------------------


def test_inference_captioner_relays_chunks_to_feeds_of_its_deployment() -> None:
    encoding = build()
    captioner = InferenceCaptioner(
        encoding.stack,
        "Inference",
        prefix="fast",
        ingest_bucket=encoding.pipeline.ingest_bucket,
        max_feeds=3,
        dictionary_id="dict-1",
        log_retention=logs.RetentionDays.ONE_WEEK,
    )
    template = Template.from_stack(encoding.stack)
    states = definition(template, "InferenceStateMachine")["States"]

    assert captioner.engine == "elemental-inference"
    assert [states[s].get("Next") for s in ("Proxy", "ProxyJob", "Plan", "Relay", "Stitch")] == [
        "ProxyJob",
        "Plan",
        "Relay",
        "Stitch",
        "Captioned",
    ]
    job = states["ProxyJob"]
    assert job["Resource"].endswith(":states:::mediaconvert:createJob.sync")
    assert job["Arguments"]["Settings"] == "{% $prepared.job.Settings %}" and job["Arguments"]["Queue"] == "<ref>"
    relay = states["Relay"]
    assert relay["MaxConcurrency"] == 3 and relay["Items"] == "{% $chunks %}"
    chunk = relay["ItemProcessor"]["States"]["RelayChunk"]
    assert chunk["Arguments"]["Payload"]["step"] == "relay"
    assert chunk["Retry"][-1]["ErrorEquals"] == ["FeedQuotaExceededError"]
    for step in ("Proxy", "ProxyJob", "Plan", "Relay", "Stitch"):
        assert states[step]["Catch"][-1]["Next"] == "Cleanup"
    assert states["Cleanup"]["Next"] == "CaptioningFailed" and states["CaptioningFailed"]["Type"] == "Fail"

    variables = function(template, "InferenceFunction")["Environment"]["Variables"]
    assert (variables["INFERENCE_DICTIONARY"], variables["FAST_PREFIX"]) == ("dict-1", "fast")
    assert function(template, "InferenceFunction")["Timeout"] == 900
    statements = policy_statements(template, "InferenceFunction")
    conditioned = {
        frozenset([s["Action"]] if isinstance(s["Action"], str) else s["Action"]): s.get("Condition")
        for s in statements
        if "elemental-inference" in str(s["Action"])
    }
    assert conditioned[frozenset({"elemental-inference:CreateFeed", "elemental-inference:TagResource"})] == {
        "StringEquals": {"aws:RequestTag/fast:app": "fast"}
    }
    data_plane = next(k for k in conditioned if "elemental-inference:PutMedia" in k)
    assert {"elemental-inference:GetMetadata", "elemental-inference:DeleteFeed"} <= data_plane
    assert conditioned[data_plane] == {"StringEquals": {"aws:ResourceTag/fast:app": "fast"}}
    queue = resource(template, "AWS::MediaConvert::Queue", "InferenceQueue")["Properties"]
    assert queue["Name"] == "fast-captioner"
