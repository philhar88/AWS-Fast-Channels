"""What ``test_12_release`` needs besides the stage to prove promotion, rollback and the silent-loss detector
(BUILD-PLAN M3-04). Everything here is throwaway: the run creates it, tags it ``Project=fast-channels`` with the
stage's prefix, and deletes it.

- :class:`Rig`: a CloudFormation stack ``<prefix>-e2e-release-rig`` with
  - the **origin**, a Lambda function behind an API Gateway HTTP API (its permission scoped to the API; low
    throttling): a VOD source with ad markers (``/vod/<key>/``), an empty VAST (``/vast``) and a lookup whose latency
    the test sets (``/lookup/all``, the function's ``DELAY_MS``). Each run has its own key: the lookup and the VAST
    need it in ``x-fast-e2e-key`` (the Function step and the ADS request send it), the VOD source in its path (an
    origin request carries no header); anything else is refused (403). An account guardrail removes a Lambda
    permission open to everyone (it did to a function URL's, fastm 2026-10-29), so the rig opens none;
  - the **stub mint**, a Lambda function with the AppConfig Agent extension that reads the release pointer with
    ``Entity-Id: <viewer>``, as M4's playback-ticket mint will.
- :func:`with_lookup`: the stage's bundle plus a PRE_ADS step that calls the origin's lookup on the pair siblings
  (after any other module's PRE_ADS steps), and :func:`publish`, which stores and announces it as
  ``Custom::DesiredState`` does.
- :func:`lab_check`: the VOD playback configuration ``<prefix>-lab-x`` (no Channel Assembly, outside the siblings'
  pattern and so outside the ownership guard) whose body template holds an unquoted variable no session sets (the
  P0-20 repro), logging to the release module's log group.
- :class:`Viewer`: a session on a sibling, polled like a player in a thread.
"""

import json
import secrets
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

import httpx
from _stack import Module, Stage
from botocore.exceptions import ClientError

from fast_channels.core.events import DesiredStateRendered, put_events_entry
from fast_channels.core.manifests import parse_multivariant
from fast_channels.core.monetization import names
from fast_channels.core.monetization.bundle import Bundle, bundle_document, root_stem
from fast_channels.core.monetization.functions import (
    Cache,
    CacheScope,
    Function,
    FunctionRef,
    FunctionType,
    Hook,
    HttpRequestConfiguration,
    function_stem,
    reexport,
    sequential_executor,
)
from fast_channels.core.monetization.playback import SiblingRole
from fast_channels.core.monetization.validators import HOOK_BUDGET_MS, validate_bundle

TAGS: Final = {"Project": "fast-channels"}
LOOKUP_BUDGET_MS: Final = 1200
"""The lookup's share of the PRE_ADS tree's timeout (less when other modules' steps use more of the 2,000 ms)."""
LOOKUP_MARGIN_MS: Final = 200
"""The lookup's request timeout is its share less this (R-F7: a request never outlasts its executor)."""
SLOW_MS: Final = 2500
"""The lookup's latency that breaks a release (P0-08's failure): the step times out, which no error metric counts
(the controller reads the step's latency against its timeout)."""
THROTTLE_RATE: Final = 20
THROTTLE_BURST: Final = 40
"""Requests per second the rig's API takes: a probe session's breaks and a VOD session every 10 s need far less."""
KEY_HEADER: Final = "x-fast-e2e-key"
INVALID_BODY: Final = '{"id":"{{session.uuid}}","len":{{player_params.content_len}}}'
"""P0-20 §4.14: unquoted and unresolved, the rendered body is not JSON and MediaTailor sends no request."""
APPCONFIG_LAYER: Final = "arn:aws:lambda:{region}:027255383542:layer:AWS-AppConfig-Extension-Arm64"
APPCONFIG_LAYER_FLOOR: Final = 291
"""The newest arm64 extension version seen in us-east-1 (2026-10-28); :func:`appconfig_layer` probes newer ones."""

ORIGIN_CODE: Final = """
import json, os, time

MASTER = "#EXTM3U\\n#EXT-X-VERSION:3\\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=640x360\\nv.m3u8\\n"
MEDIA = "\\n".join(["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6", "#EXT-X-MEDIA-SEQUENCE:1",
    "#EXT-X-PLAYLIST-TYPE:VOD", "#EXTINF:6.0,", "s1.ts", "#EXTINF:6.0,", "s2.ts", "#EXT-X-CUE-OUT:30",
    "#EXT-X-CUE-IN", "#EXTINF:6.0,", "s3.ts", "#EXTINF:6.0,", "s4.ts", "#EXT-X-CUE-OUT:30", "#EXT-X-CUE-IN",
    "#EXTINF:6.0,", "s5.ts", "#EXT-X-ENDLIST", ""])


def handler(event, context):
    path, key = event.get("rawPath", "/"), os.environ["KEY"]
    keyed = (event.get("headers") or {}).get("x-fast-e2e-key") == key
    if path.startswith("/lookup/") and keyed:
        time.sleep(int(os.environ.get("DELAY_MS", "0")) / 1000)
        return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": json.dumps({"ok": 1})}
    if path == "/vast" and keyed:
        return {"statusCode": 200, "headers": {"content-type": "application/xml"}, "body": '<VAST version="4.2"/>'}
    if path in (f"/vod/{key}/main.m3u8", f"/vod/{key}/v.m3u8"):
        body = MASTER if path.endswith("/main.m3u8") else MEDIA
        return {"statusCode": 200, "headers": {"content-type": "application/vnd.apple.mpegurl"}, "body": body}
    return {"statusCode": 403, "body": ""}
"""
MINT_CODE: Final = """
import json, os, urllib.request

URL = (f"http://localhost:2772/applications/{os.environ['APPLICATION']}/environments/{os.environ['ENVIRONMENT']}"
       f"/configurations/{os.environ['PROFILE']}")


def handler(event, context):
    found = {}
    for viewer in event["viewers"]:
        request = urllib.request.Request(URL, headers={"Entity-Id": viewer})
        with urllib.request.urlopen(request, timeout=3) as response:
            found[viewer] = json.loads(response.read() or b"{}").get(event["channel"])
    return found
"""


# --- the rig -------------------------------------------------------------------------------------------------------


def appconfig_layer(lambda_client: Any, region: str) -> str:
    """The newest AppConfig Agent extension for arm64 from :data:`APPCONFIG_LAYER_FLOOR` on (a version that does not
    exist yet is AccessDenied)."""
    base, version = APPCONFIG_LAYER.format(region=region), APPCONFIG_LAYER_FLOOR
    while True:
        try:
            lambda_client.get_layer_version_by_arn(Arn=f"{base}:{version + 1}")
        except ClientError:
            return f"{base}:{version}"
        version += 1


def _function(role: str, code: str, environment: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "Type": "AWS::Lambda::Function",
        "Properties": {
            "Runtime": "python3.14",
            "Architectures": ["arm64"],
            "Handler": "index.handler",
            "MemorySize": 256,
            "Timeout": 10,
            "Role": {"Fn::GetAtt": [role, "Arn"]},
            "Code": {"ZipFile": code},
            "Environment": {"Variables": dict(environment)},
            **extra,
        },
    }


def _role(statements: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    trust = {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}
    logs = {
        "Effect": "Allow",
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": {"Fn::Sub": "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/*"},
    }
    return {
        "Type": "AWS::IAM::Role",
        "Properties": {
            "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [trust]},
            "Policies": [
                {"PolicyName": "rig", "PolicyDocument": {"Version": "2012-10-17", "Statement": [logs, *statements]}}
            ],
        },
    }


def rig_template(*, application: str, environment: str, profile: str, layer: str) -> dict[str, Any]:
    """The rig's CloudFormation template (see the module docstring)."""
    pointer = {
        "Fn::Sub": f"arn:${{AWS::Partition}}:appconfig:${{AWS::Region}}:${{AWS::AccountId}}:application/{application}"
        f"/environment/{environment}/configuration/{profile}"
    }
    appconfig = {
        "Effect": "Allow",
        "Action": ["appconfig:StartConfigurationSession", "appconfig:GetLatestConfiguration"],
        "Resource": pointer,
    }
    mint_environment = {
        "APPLICATION": application,
        "ENVIRONMENT": environment,
        "PROFILE": profile,
        "AWS_APPCONFIG_EXTENSION_POLL_INTERVAL_SECONDS": "15",
    }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "FAST Channels e2e (test_12_release): throwaway origin and stub mint; safe to delete",
        "Resources": {
            "OriginRole": _role([]),
            "Origin": _function("OriginRole", ORIGIN_CODE, {"DELAY_MS": "0", "KEY": {"Ref": "Key"}}),
            "OriginLogs": {
                "Type": "AWS::Logs::LogGroup",
                "Properties": {"LogGroupName": {"Fn::Sub": "/aws/lambda/${Origin}"}, "RetentionInDays": 1},
            },
            "Api": {
                "Type": "AWS::ApiGatewayV2::Api",
                "Properties": {
                    "Name": {"Ref": "AWS::StackName"},
                    "ProtocolType": "HTTP",
                    "Description": "FAST Channels e2e origin (throwaway)",
                    "Tags": dict(TAGS),
                },
            },
            "Integration": {
                "Type": "AWS::ApiGatewayV2::Integration",
                "Properties": {
                    "ApiId": {"Ref": "Api"},
                    "IntegrationType": "AWS_PROXY",
                    "IntegrationUri": {"Fn::GetAtt": ["Origin", "Arn"]},
                    "PayloadFormatVersion": "2.0",
                    "TimeoutInMillis": 10000,
                },
            },
            "Route": {
                "Type": "AWS::ApiGatewayV2::Route",
                "Properties": {
                    "ApiId": {"Ref": "Api"},
                    "RouteKey": "$default",
                    "Target": {"Fn::Sub": "integrations/${Integration}"},
                },
            },
            "ApiStage": {
                "Type": "AWS::ApiGatewayV2::Stage",
                "Properties": {
                    "ApiId": {"Ref": "Api"},
                    "StageName": "$default",
                    "AutoDeploy": True,
                    "DefaultRouteSettings": {
                        "ThrottlingBurstLimit": THROTTLE_BURST,
                        "ThrottlingRateLimit": THROTTLE_RATE,
                    },
                    "Tags": dict(TAGS),
                },
            },
            "OriginPermission": {
                "Type": "AWS::Lambda::Permission",
                "Properties": {
                    "Action": "lambda:InvokeFunction",
                    "FunctionName": {"Ref": "Origin"},
                    "Principal": "apigateway.amazonaws.com",
                    "SourceArn": {
                        "Fn::Sub": "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${Api}/*"
                    },
                },
            },
            "MintRole": _role([appconfig]),
            "Mint": _function("MintRole", MINT_CODE, mint_environment, Layers=[layer]),
            "MintLogs": {
                "Type": "AWS::Logs::LogGroup",
                "Properties": {"LogGroupName": {"Fn::Sub": "/aws/lambda/${Mint}"}, "RetentionInDays": 1},
            },
        },
        "Parameters": {"Key": {"Type": "String", "NoEcho": True, "MinLength": 32}},
        "Outputs": {
            "OriginUrl": {"Value": {"Fn::Sub": "${Api.ApiEndpoint}/"}},
            "OriginFunction": {"Value": {"Ref": "Origin"}},
            "MintFunction": {"Value": {"Ref": "Mint"}},
        },
    }


def pointer_ids(stage: Stage) -> tuple[str, str, str]:
    """The release pointer's AppConfig application, environment and profile ids."""
    ssm = stage.session.client("ssm", region_name=stage.region)

    def parameter(name: names.Parameter) -> str:
        return str(ssm.get_parameter(Name=names.parameter_name(stage.prefix, name))["Parameter"]["Value"])

    application = parameter(names.Parameter.APPCONFIG_APPLICATION)
    environment = parameter(names.Parameter.APPCONFIG_ENVIRONMENT)
    appconfig = stage.session.client("appconfig", region_name=stage.region)
    profiles = appconfig.list_configuration_profiles(ApplicationId=application)["Items"]
    [profile] = [p["Id"] for p in profiles if p["Name"] == names.POINTER_PROFILE]
    return application, environment, profile


@dataclass
class Rig:
    """The deployed rig."""

    stage: Stage
    name: str
    outputs: dict[str, str]
    key: str = field(repr=False, default="")
    """This run's key (see the module docstring); it lives as long as the rig."""

    @classmethod
    def deploy(cls, stage: Stage) -> Rig:
        """Create the rig's stack (replacing one a failed run left)."""
        name = f"{stage.prefix}-e2e-release-rig"
        cfn = stage.session.client("cloudformation", region_name=stage.region)
        try:
            cfn.describe_stacks(StackName=name)
            cfn.delete_stack(StackName=name)
            cfn.get_waiter("stack_delete_complete").wait(StackName=name)
        except ClientError:
            pass  # no such stack
        application, environment, profile = pointer_ids(stage)
        layer = appconfig_layer(stage.session.client("lambda", region_name=stage.region), stage.region)
        template = rig_template(application=application, environment=environment, profile=profile, layer=layer)
        key = secrets.token_urlsafe(32)
        cfn.create_stack(
            StackName=name,
            TemplateBody=json.dumps(template),
            Parameters=[{"ParameterKey": "Key", "ParameterValue": key}],
            Capabilities=["CAPABILITY_IAM"],
            Tags=[{"Key": k, "Value": v} for k, v in {**TAGS, "Stage": stage.prefix}.items()],
            OnFailure="DELETE",
        )
        cfn.get_waiter("stack_create_complete").wait(StackName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
        [described] = cfn.describe_stacks(StackName=name)["Stacks"]
        return cls(stage, name, {o["OutputKey"]: o["OutputValue"] for o in described["Outputs"]}, key)

    def delete(self) -> None:
        cfn = self.stage.session.client("cloudformation", region_name=self.stage.region)
        cfn.delete_stack(StackName=self.name)
        cfn.get_waiter("stack_delete_complete").wait(StackName=self.name, WaiterConfig={"Delay": 10, "MaxAttempts": 60})

    @property
    def origin(self) -> str:
        """The origin's base URL (ends with ``/``)."""
        return self.outputs["OriginUrl"]

    def set_delay(self, milliseconds: int) -> None:
        """The lookup's latency, for every invocation that starts after this returns."""
        client = self.stage.session.client("lambda", region_name=self.stage.region)
        function = self.outputs["OriginFunction"]
        client.update_function_configuration(
            FunctionName=function, Environment={"Variables": {"DELAY_MS": str(milliseconds), "KEY": self.key}}
        )
        client.get_waiter("function_updated_v2").wait(FunctionName=function)

    @property
    def headers(self) -> dict[str, str]:
        """The header the lookup and the VAST need."""
        return {KEY_HEADER: self.key}

    def mint(self, channel: str, viewers: Sequence[str]) -> dict[str, str | None]:
        """The sibling the pointer names for ``channel``, per viewer (``Entity-Id``)."""
        client = self.stage.session.client("lambda", region_name=self.stage.region)
        response = client.invoke(
            FunctionName=self.outputs["MintFunction"],
            Payload=json.dumps({"channel": channel, "viewers": list(viewers)}).encode(),
        )
        body: dict[str, str | None] = json.loads(response["Payload"].read())
        if "FunctionError" in response:
            raise AssertionError(f"the stub mint failed: {body}")
        return body


# --- a release with a lookup ---------------------------------------------------------------------------------------


LOOKUP_KEY: Final = "{% 'e2e|v1|all' %}"
"""Scope ``global``: the lookup's answer is the same for every session and break, and the pair siblings of every
channel share it (a ``break`` key would name each one's channel, validator R-F8)."""
LOOKUP_CACHE_REASON: Final = (
    "e2e: the lookup answers the same for everyone, only its latency matters; 1 s, so that every break after the "
    "latency changes meets the new latency"
)


def add_lookup(bundle: Bundle, url: str, headers: Mapping[str, str] | None = None) -> Bundle:
    """``bundle`` with a PRE_ADS step that GETs ``url/all`` at the end of the pair
    siblings' PRE_ADS tree (a new tree where they have none), within the 2,000 ms the request-path hooks share; other
    modules' steps stay as they are.

    Its answer is cached for 1 s for everyone (scope ``global``): breaks are minutes apart, so each calls the
    lookup, and the time it turns slow is the time its breaks meet it.

    Raises:
        AssertionError: the pair siblings map different PRE_ADS trees, or too little of the budget is left.
    """
    pair = [s for s in bundle.siblings if s.role is SiblingRole.GENERAL]
    roots = {(s.playback.function_mapping or {}).get(Hook.PRE_ADS) for s in pair}
    assert len(roots) == 1, f"the pair siblings map different PRE_ADS trees: {roots}"
    [stem] = roots
    existing = bundle.function(stem) if stem else None
    executor = existing.sequential_executor_configuration if existing else None
    used = executor.timeout_milliseconds if executor else 0
    budget = min(LOOKUP_BUDGET_MS, HOOK_BUDGET_MS - used)
    assert budget - LOOKUP_MARGIN_MS >= 300, f"{used} ms of PRE_ADS leave too little for the lookup"
    lookup = Function(
        function_id=function_stem(bundle.prefix, "pa_e2e_lookup"),
        function_type=FunctionType.HTTP_REQUEST,
        description="e2e: a lookup whose latency the test sets (test_12_release)",
        http_request_configuration=HttpRequestConfiguration(
            method_type="GET",
            url=f"{url}/all",
            headers=dict(headers) if headers else None,
            request_timeout_milliseconds=budget - LOOKUP_MARGIN_MS,
            output={"temp.e2e": "{% $substring($string(response.statusCode), 0, 3) %}"},
            cache=Cache(ttl_minimum_seconds=1, ttl_maximum_seconds=1, key=LOOKUP_KEY),
            cache_scope=CacheScope.GLOBAL,
            cache_reason=LOOKUP_CACHE_REASON,
        ),
    )
    step, commit = FunctionRef(function_id=lookup.function_id), {"adsRequest.headers.x-fast-e2e": reexport("temp.e2e")}
    if existing and executor:
        stem = existing.function_id
        root = existing.model_copy(
            update={
                "sequential_executor_configuration": executor.model_copy(
                    update={
                        "function_list": (*executor.function_list, step),
                        "timeout_milliseconds": used + budget,
                        "output": {**(executor.output or {}), **commit},
                    }
                )
            }
        )
    else:
        stem = root_stem(bundle.prefix, Hook.PRE_ADS)
        root = sequential_executor(stem, steps=[step], output=commit, timeout_ms=budget, description="e2e lookup")
    functions = [f for f in bundle.functions if f.function_id != stem] + [lookup, root]
    ordered = sorted(functions, key=lambda f: (bool(f.steps), f.function_id))  # children before executors
    siblings = [
        s.model_copy(
            update={
                "playback": s.playback.model_copy(
                    update={"function_mapping": {**(s.playback.function_mapping or {}), Hook.PRE_ADS: stem}}
                )
            }
        )
        if s.role is SiblingRole.GENERAL
        else s
        for s in bundle.siblings
    ]
    changed = bundle.model_copy(update={"functions": tuple(ordered), "siblings": tuple(siblings)})
    return Bundle.model_validate(changed.model_dump(mode="json", by_alias=True, exclude_none=True))


def deployed_bundle(stage: Stage) -> Bundle:
    """The bundle the stage's Release stack rendered (output ``BundleKey``)."""
    s3 = stage.session.client("s3", region_name=stage.region)
    document = s3.get_object(
        Bucket=stage.output(Module.RELEASE, "BundleBucketName"), Key=stage.output(Module.RELEASE, "BundleKey")
    )
    return Bundle.model_validate(json.loads(document["Body"].read())["bundle"])


def with_lookup(stage: Stage, url: str, headers: Mapping[str, str] | None = None) -> Bundle:
    """The stage's bundle with :func:`add_lookup` of ``url``, valid."""
    bundle = add_lookup(deployed_bundle(stage), url, headers)
    assert validate_bundle(bundle) == [], validate_bundle(bundle)
    return bundle


def publish(stage: Stage, bundle: Bundle, *, correlation_id: str) -> str:
    """Store ``bundle`` under its digest and put ``Desired State Rendered``; the bundle key."""
    key = names.bundle_key(bundle.digest)
    s3 = stage.session.client("s3", region_name=stage.region)
    s3.put_object(
        Bucket=stage.output(Module.RELEASE, "BundleBucketName"),
        Key=key,
        Body=json.dumps(bundle_document(bundle), indent=2, sort_keys=True).encode(),
        ContentType="application/json",
    )
    detail = DesiredStateRendered(bundle_key=key, digest=bundle.digest, correlation_id=correlation_id)
    entry = put_events_entry(detail, prefix=stage.prefix, event_bus=f"{stage.prefix}-fast-channels")
    response = stage.session.client("events", region_name=stage.region).put_events(Entries=[entry])  # type: ignore[list-item]
    assert not response.get("FailedEntryCount"), response["Entries"]
    return key


def execution_of(stage: Stage, correlation_id: str, *, timeout: float = 180) -> str:
    """The ARN of the release execution the event ``correlation_id`` started."""
    sfn = stage.session.client("stepfunctions", region_name=stage.region)
    arn = stage.output(Module.RELEASE, "StateMachineArn")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for execution in sfn.list_executions(stateMachineArn=arn, maxResults=10)["executions"]:
            described = sfn.describe_execution(executionArn=execution["executionArn"])
            if json.loads(described.get("input") or "{}").get("correlation_id") == correlation_id:
                return str(execution["executionArn"])
        time.sleep(10)
    raise AssertionError(f"no release execution for {correlation_id} in {timeout:.0f} s")


def entered(stage: Stage, execution: str) -> dict[str, float]:
    """When the execution last entered each state (epoch seconds), and ``""`` -> when it ended, if it did."""
    sfn = stage.session.client("stepfunctions", region_name=stage.region)
    found: dict[str, float] = {}
    for page in sfn.get_paginator("get_execution_history").paginate(executionArn=execution):
        for event in page["events"]:
            if details := event.get("stateEnteredEventDetails"):
                found[details["name"]] = event["timestamp"].timestamp()
            if event["type"].startswith("Execution") and event["type"] != "ExecutionStarted":
                found[""] = event["timestamp"].timestamp()
    return found


def task_window(stage: Stage, execution: str, state: str) -> tuple[float, float]:
    """When the task state ``state`` was entered and exited (epoch seconds)."""
    sfn = stage.session.client("stepfunctions", region_name=stage.region)
    began = ended = 0.0
    for page in sfn.get_paginator("get_execution_history").paginate(executionArn=execution):
        for event in page["events"]:
            if (enter := event.get("stateEnteredEventDetails")) and enter["name"] == state:
                began = event["timestamp"].timestamp()
            if (exit_ := event.get("stateExitedEventDetails")) and exit_["name"] == state:
                ended = event["timestamp"].timestamp()
    assert began and ended, f"{execution} never ran {state}"
    return began, ended


def next_break(stage: Stage, channel: str, after: float) -> float:
    """When the first ad break of ``channel`` that starts after ``after`` (epoch seconds) ends."""
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    starts = [
        (b["ApproximateStartTime"].timestamp(), float(b.get("ApproximateDurationSeconds", 0)))
        for page in mediatailor.get_paginator("get_channel_schedule").paginate(
            ChannelName=channel, DurationMinutes="240"
        )
        for item in page["Items"]
        for b in item.get("ScheduleAdBreaks", [])
    ]
    later = sorted(s for s in starts if s[0] > after)
    assert later, f"{channel} airs no break after {datetime.fromtimestamp(after, UTC):%H:%M:%S}"
    return later[0][0] + later[0][1]


# --- the silent-loss repro -----------------------------------------------------------------------------------------


@contextmanager
def lab_check(stage: Stage, rig: Rig) -> Iterator[str]:
    """``<prefix>-lab-x`` on the rig's VOD origin, its ads log delivered to the module's log group; deleted after."""
    name = names.lab_check(stage.prefix)
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    logs = stage.session.client("logs", region_name=stage.region)
    tags = {**TAGS, "Stage": stage.prefix}
    mediatailor.put_playback_configuration(
        Name=name,
        VideoContentSourceUrl=f"{rig.origin}vod/{rig.key}",
        AdDecisionServerUrl=f"{rig.origin}vast",
        AdDecisionServerConfiguration={
            "HttpRequest": {
                "Method": "POST",
                "Body": INVALID_BODY,
                "Headers": {"Content-Type": "application/json", **rig.headers},
            }
        },
        Tags=tags,
    )
    source = f"{name}-ads"
    delivery: str | None = None
    try:
        mediatailor.configure_logs_for_playback_configuration(
            PlaybackConfigurationName=name, PercentEnabled=100, EnabledLoggingStrategies=["VENDED_LOGS"]
        )
        arn = mediatailor.get_playback_configuration(Name=name)["PlaybackConfigurationArn"]
        logs.put_delivery_source(name=source, resourceArn=arn, logType="AD_DECISION_SERVER_LOGS", tags=tags)
        destination = logs.get_delivery_destination(name=names.log_destination(stage.prefix))["deliveryDestination"]
        delivery = logs.create_delivery(
            deliverySourceName=source, deliveryDestinationArn=destination["arn"], tags=tags
        )["delivery"]["id"]
        yield name
    finally:
        if delivery:
            logs.delete_delivery(id=delivery)
        with suppress(logs.exceptions.ResourceNotFoundException):
            logs.delete_delivery_source(name=source)
        mediatailor.delete_playback_configuration(Name=name)


def vod_session(stage: Stage, configuration: str, http: httpx.Client) -> str:
    """One VOD session on ``configuration``: its multivariant and media playlist (MediaTailor finds the markers
    when it personalizes the media playlist). The media playlist's URL."""
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    prefix = mediatailor.get_playback_configuration(Name=configuration)["HlsConfiguration"]["ManifestEndpointPrefix"]
    master = http.get(urljoin(prefix, "main.m3u8")).raise_for_status()
    [variant] = parse_multivariant(master.text)
    media = urljoin(str(master.url), variant.uri)
    http.get(media).raise_for_status()
    return media


# --- viewers -------------------------------------------------------------------------------------------------------


@dataclass
class Viewer:
    """A session on ``sibling`` polled like a player (every 4 s) in a thread until :meth:`stop`."""

    sibling: str
    stage: Stage
    session: str = ""
    errors: list[str] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> Viewer:
        mediatailor = self.stage.session.client("mediatailor", region_name=self.stage.region)
        prefix = mediatailor.get_playback_configuration(Name=self.sibling)["HlsConfiguration"]["ManifestEndpointPrefix"]
        http = httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": "fast-channels-e2e/3.0"})
        master = http.get(urljoin(prefix, "hls.m3u8")).raise_for_status()
        playlist = urljoin(str(master.url), parse_multivariant(master.text)[0].uri)
        self.session = urlsplit(playlist).path.split("/")[5]  # /v1/manifest/<hash>/<configuration>/<session>/…

        def poll() -> None:
            while not self._stop.wait(4.0):
                try:
                    http.get(playlist).raise_for_status()
                except httpx.HTTPError as error:
                    self.errors.append(str(error)[:200])
            http.close()

        self._thread = threading.Thread(target=poll, name=f"viewer-{self.session}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)


# --- logs ----------------------------------------------------------------------------------------------------------


def insights(stage: Stage, group: str, query: str, start: float, end: float) -> list[dict[str, str]]:
    """The rows of a Logs Insights ``query`` over ``group`` between ``start`` and ``end`` (epoch seconds)."""
    logs = stage.session.client("logs", region_name=stage.region)
    started = logs.start_query(
        logGroupNames=[group], startTime=int(start) - 60, endTime=int(end) + 60, queryString=query
    )
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        result = logs.get_query_results(queryId=started["queryId"])
        if result["status"] == "Complete":
            return [{c["field"]: c["value"] for c in row if c["field"] != "@ptr"} for row in result["results"]]
        if result["status"] in ("Failed", "Cancelled", "Timeout", "Unknown"):
            raise AssertionError(f"Logs Insights query ended {result['status']}: {query}")
        time.sleep(2)
    raise AssertionError(f"Logs Insights query did not complete: {query}")


HOOK_WINDOW_S: Final = 5.0
"""A PRE_ADS hook runs within its 2,000 ms budget before the break's ad request."""
BREAKS_QUERY: Final = (
    "filter sessionId in [{sessions}] and eventType in ['MAKING_ADS_REQUEST', 'PRE_ADS_REQUEST_HOOK_ERROR', "
    "'PRE_ADS_REQUEST_FUNCTION_ERROR']\n"
    "| fields @timestamp, sessionId, eventType, coalesce(adBreakCorrelationId, prefetchScheduleName) as breakId\n"
    "| limit 10000"
)


def _epoch(timestamp: str) -> float:
    """A Logs Insights ``@timestamp`` (UTC) in epoch seconds."""
    return datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC).timestamp()


def breaks(stage: Stage, sessions: Sequence[str], start: float, end: float) -> dict[str, list[tuple[float, bool]]]:
    """Each session's ad requests in time order: (epoch seconds, whether its PRE_ADS hook failed: an error event of
    its break, or of the session in the :data:`HOOK_WINDOW_S` before it)."""
    quoted = ", ".join(f"'{s}'" for s in sessions)
    rows = insights(stage, names.log_group(stage.prefix), BREAKS_QUERY.format(sessions=quoted), start, end)
    errors = [(r["sessionId"], r.get("breakId"), _epoch(r["@timestamp"])) for r in rows]
    errors = [e for e, r in zip(errors, rows, strict=True) if r["eventType"] != "MAKING_ADS_REQUEST"]
    found: dict[str, list[tuple[float, bool]]] = {s: [] for s in sessions}
    for row in rows:
        if row["eventType"] != "MAKING_ADS_REQUEST":
            continue
        session, break_id, at = row["sessionId"], row.get("breakId"), _epoch(row["@timestamp"])
        failed = any(
            s == session and ((b is not None and b == break_id) or 0 <= at - t <= HOOK_WINDOW_S) for s, b, t in errors
        )
        found[session].append((at, failed))
    return {s: sorted(v) for s, v in found.items()}


def request_latency(
    stage: Stage, sibling: str, function_id: str, start: float, end: float
) -> list[tuple[float, float]]:
    """The per-minute maximum ``Function.Latency`` of a PRE_ADS ``HTTP_REQUEST`` function on ``sibling``: (minute
    start in epoch seconds, ms), in time order."""
    dimensions = {
        "ConfigurationName": sibling,
        "FunctionId": function_id,
        "HookType": "PRE_ADS_REQUEST",
        "FunctionType": "HTTP_REQUEST",
    }
    points = stage.session.client("cloudwatch", region_name=stage.region).get_metric_statistics(
        Namespace="AWS/MediaTailor",
        MetricName="Function.Latency",
        Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
        StartTime=datetime.fromtimestamp(start, UTC),
        EndTime=datetime.fromtimestamp(end, UTC),
        Period=60,
        Statistics=["Maximum"],
    )["Datapoints"]
    return sorted((p["Timestamp"].timestamp(), float(p["Maximum"])) for p in points)
