"""The release module's stack (M3-03): beside the guidance, reading the Media stack by parameter; the only roles that
change monetization; the siblings' encrypted logs; the drift check and the per-sibling alarms."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aws_cdk import Stack

from fast_channels.core.demand import NETWORK_CONTEXT
from fast_channels.core.monetization.validators import validate_bundle
from fast_channels.infra.constructs.pds.delivery import IMPRESSIONS
from fast_channels.infra.constructs.release.guards import DENIED_ACTIONS, LOG_READ_ACTIONS

if TYPE_CHECKING:
    from aws_cdk.assertions import Template
    from syrupy.assertion import SnapshotAssertion

type GetDeployment = Callable[..., Any]

NETWORK: dict[str, Any] = {
    "vpc_id": "vpc-0123456789abcdef0",
    "private_subnets": [
        {"id": "subnet-0aaaaaaaaaaaaaaa1", "az": "us-east-1a", "route_table": "rtb-0aaaaaaaaaaaaaaa1"},
        {"id": "subnet-0bbbbbbbbbbbbbbb2", "az": "us-east-1b", "route_table": "rtb-0bbbbbbbbbbbbbbb2"},
    ],
    "task_security_group_id": "sg-0123456789abcdef0",
    "cluster_arn": "arn:aws:ecs:us-east-1:123456789012:cluster/fastm-pbs-PrebidCluster",
    "service_arn": "arn:aws:ecs:us-east-1:123456789012:service/fastm-pbs-PrebidCluster/fastm-pbs-Service",
    "cloudfront_domain": "d111111abcdef8.cloudfront.net",
    "target_group_arn": "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/fastm-pbs/0123456789abcd",
    "simulator_code_key": "simulator/0123456789abcdef.zip",
}
EXTRA = {NETWORK_CONTEXT: NETWORK}
RELEASE = {
    "prefix": "fastm",
    "modules.ops.enabled": True,
    "modules.demand.enabled": True,
    "modules.demand.simulator": "lambda",
    "modules.pds.enabled": True,
    "modules.release.enabled": True,
    "modules.release.channels": ["{prefix}-demo"],
}
BASE = "arn:aws:mediatailor:us-east-1:123456789012"
WRITES = {a for a in DENIED_ACTIONS if a != "mediatailor:PutChannelPolicy"}


def resources(template: Template, kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template.find_resources(kind).items()}


def statements(template: Template) -> list[tuple[str, dict[str, Any]]]:
    """(policy's role, statement) of every identity policy in ``template``."""
    found = []
    for properties in resources(template, "AWS::IAM::Policy").values():
        for role in properties.get("Roles", []):
            found += [(role["Ref"], s) for s in properties["PolicyDocument"]["Statement"]]
    return found


def actions(statement: dict[str, Any]) -> set[str]:
    action = statement["Action"]
    return {action} if isinstance(action, str) else set(action)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def stacks(deployment: Any) -> list[Stack]:
    return [c for c in deployment.stage.node.children if isinstance(c, Stack)]


def test_the_stack_exists_only_with_the_module_and_the_guidances_network(deployment: GetDeployment) -> None:
    assert deployment(RELEASE).stage.release is None
    stage = deployment(RELEASE, extra=EXTRA).stage

    assert stage.release.stack_name == "FastChannels-fastm-Release"
    assert {s.stack_name for s in stage.release.dependencies} == {
        "FastChannels-fastm-Data",
        "FastChannels-fastm-Media",
        "FastChannels-fastm-Ops",
        "FastChannels-fastm-Demand",
        "FastChannels-fastm-Pds",
    }
    assert deployment({**RELEASE, "modules.release.enabled": False}, extra=EXTRA).stage.release is None


def test_it_reads_the_media_stack_by_parameter_not_by_weak_reference(deployment: GetDeployment) -> None:
    """The Media stack also deploys without the guidance's network (``make deploy``): it must owe this stack nothing."""
    d = deployment(RELEASE, extra=EXTRA)
    template = d.template(d.stage.release).to_json()

    text = json.dumps(template)
    assert "Fn::ImportValue" not in text, "no cross-stack reference"
    ssm = {p["Default"] for p in template["Parameters"].values() if p["Type"].startswith("AWS::SSM::Parameter")}
    assert ssm - {"/cdk-bootstrap/hnb659fds/version"} == {
        "/fastm/release/content-base-url",
        "/fastm/release/cdn-base-url",
        "/fastm/release/media-bucket",
        "/fastm/release/channels/fastm-demo",
        "/fastm/release/channels/fastm-lab",
        "/fastm/release/appconfig-application",  # the Ops stack's
        "/fastm/release/appconfig-environment",
        "/fastm/pds/delivery-function",  # the Pds stack's
        "/fastm/pds/policy-log-group",
        "/fastm/release/tool-functions",  # the Operator stack's
    }
    plain = deployment(RELEASE)
    for stack in ("data", "media", "ops"):
        assert plain.template(getattr(plain.stage, stack)).to_json() == d.template(getattr(d.stage, stack)).to_json()
    written = {
        p["Name"]
        for stack in (d.stage.media, d.stage.ops, d.stage.pds, d.stage.operator)
        for p in resources(d.template(stack), "AWS::SSM::Parameter").values()
    }
    assert ssm - {"/cdk-bootstrap/hnb659fds/version"} <= written


def test_the_bundle_is_rendered_at_synth_and_published_at_deploy(deployment: GetDeployment) -> None:
    d = deployment(RELEASE, extra=EXTRA)
    release = d.stage.release
    bundle = release.rendered.bundle

    assert validate_bundle(bundle) == []
    assert [s.name for s in bundle.siblings] == [
        *("fastm-demo-p-a", "fastm-demo-p-b", "fastm-demo-p-z"),
        *("fastm-lab-p-a", "fastm-lab-p-b", "fastm-lab-p-z"),  # the lab channel's, which publish the hook events
    ]
    hooks = [s.playback.log_configuration.manifest_service_interaction_log for s in bundle.siblings]
    assert [bool(h and h.publish_opt_in_event_types) for h in hooks] == [False, False, True, True, True, True]
    assert [f.function_id for f in bundle.functions] == ["fastm_ps_ctl", "fastm_ps_root"]  # M3-03: ps_ctl only
    [properties] = resources(d.template(release), "Custom::DesiredState").values()
    assert {"Prefix": "fastm", "EventBus": "fastm-fast-channels"}.items() <= properties.items()
    parts = properties["Bundle"]["Fn::Join"][1]
    text = "".join(p for p in parts if isinstance(p, str))
    assert "https://d111111abcdef8.cloudfront.net" in text, "the guidance's CloudFront is the PBS origin"
    assert "X-Fast-Origin" not in text and "origin-a" not in text, "no secret, no secret name in the bundle"
    references = [p["Ref"] for p in parts if isinstance(p, dict)]
    assert any("fastmreleasechannelsfastmdemo" in r for r in references), "the channel's URL resolves at deploy"


def test_only_the_controller_changes_monetization(deployment: GetDeployment) -> None:
    d = deployment(RELEASE, extra=EXTRA)
    template = d.template(d.stage.release)
    roles = resources(template, "AWS::IAM::Role")
    apply_role = next(name for name in roles if name.startswith("ControllerApplyRole"))
    promote_role = next(name for name in roles if name.startswith("ControllerPromoteRole"))
    reaper_role = next(name for name in roles if name.startswith("ControllerReaperRole"))

    prefetch_role = next(name for name in roles if name.startswith("PrefetcherFunctionRole"))
    writers = {role for role, s in statements(template) if s["Effect"] == "Allow" and actions(s) & WRITES}
    assert writers == {apply_role, promote_role, reaper_role, prefetch_role}  # promote remaps (R1)
    prefetch_writes = {a for r, s in statements(template) if r == prefetch_role for a in actions(s) & WRITES}
    assert prefetch_writes == {"mediatailor:CreatePrefetchSchedule", "mediatailor:DeletePrefetchSchedule"}
    [policy] = [
        (role, s)
        for role, s in statements(template)
        if role == apply_role and "mediatailor:PutPlaybackConfiguration" in actions(s)
    ]
    assert policy[1]["Resource"] == [f"{BASE}:playbackConfiguration/fastm-{c}-p-*" for c in ("demo", "lab")]
    assert not any("mediatailor:PutChannelPolicy" in actions(s) for _, s in statements(template))
    assert not any("mediatailor:DeleteFunction" in actions(s) for r, s in statements(template) if r == apply_role)


def test_every_other_role_of_the_stage_is_denied_the_controllers_writes(deployment: GetDeployment) -> None:
    d = deployment(RELEASE, extra=EXTRA)
    unguarded: list[str] = []
    for stack in stacks(d):
        template = d.template(stack).to_json()["Resources"]
        guards = [k for k, r in template.items() if r["Type"] == "AWS::IAM::ManagedPolicy" and "ReleaseGuard" in k]
        for name, role in template.items():
            if role["Type"] != "AWS::IAM::Role":
                continue
            if {"Ref": guards[0] if guards else ""} not in role["Properties"].get("ManagedPolicyArns", []):
                unguarded.append(f"{stack.stack_name}/{name}")
        for guard in guards:
            deny, logs_deny = template[guard]["Properties"]["PolicyDocument"]["Statement"]
            assert logs_deny["Effect"] == "Deny" and set(logs_deny["Action"]) == set(LOG_READ_ACTIONS)
            group = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/vendedlogs/mediatailor/fastm-release"
            assert logs_deny["Resource"] == [group, f"{group}:*"]
            assert deny["Effect"] == "Deny" and set(deny["Action"]) == set(DENIED_ACTIONS)
            assert set(deny["Resource"]) == {
                *(f"{BASE}:playbackConfiguration/fastm-{c}-p-*" for c in ("demo", "lab")),
                *(f"{BASE}:prefetchSchedule/fastm-{c}-p-*/*" for c in ("demo", "lab")),
                f"{BASE}:channel/fastm-demo",
                f"{BASE}:channel/fastm-lab",
                f"{BASE}:function/fastm_*",
            }
    exempt = (
        "/ControllerApplyRole",
        "/ControllerPromoteRole",
        "/ControllerReaperRole",
        "/PrefetcherFunctionRole",
        "/ZeroAdsDetectorRole",
    )
    controller = {n for n in unguarded if any(r in n for r in (*exempt, "/ControllerSmokeRole"))}
    assert len(controller) == 6  # the controller's writers, and the readers of the siblings' logs
    # The roles of CDK's core custom-resource providers are CfnResources, not iam.Role (the guard's Aspect cannot
    # attach to them): they empty buckets or run a trigger, and none of them may call MediaTailor.
    providers = set(unguarded) - controller
    assert providers and all("CustomResourceProviderRole" in n for n in providers), providers
    for stack in stacks(d):
        template = d.template(stack).to_json()["Resources"]
        for name in (n.partition("/")[2] for n in providers if n.startswith(f"{stack.stack_name}/")):
            inline = [
                s for p in template[name]["Properties"].get("Policies", []) for s in p["PolicyDocument"]["Statement"]
            ]
            assert not any(a.startswith("mediatailor:") for s in inline for a in actions(s)), name
        for role, statement in statements(d.template(stack)):
            assert f"{stack.stack_name}/{role}" not in providers or not any(
                a.startswith("mediatailor:") for a in actions(statement)
            )


def test_the_guard_is_off_with_the_module(deployment: GetDeployment) -> None:
    d = deployment({**RELEASE, "modules.release.enabled": False}, extra=EXTRA)
    for stack in stacks(d):
        assert not [k for k in d.template(stack).to_json()["Resources"] if k.startswith("ReleaseGuard")]


def test_the_siblings_logs_are_encrypted_with_a_key_that_delegates_nothing(deployment: GetDeployment) -> None:
    d = deployment(RELEASE, extra=EXTRA)
    template = d.template(d.stage.release)
    [key] = resources(template, "AWS::KMS::Key").values()
    policy = key["KeyPolicy"]["Statement"]

    assert key["EnableKeyRotation"] is True
    root = [s for s in policy if s["Principal"].get("AWS") == "arn:aws:iam::123456789012:root"]
    assert len(root) == 1 and not {"kms:*", "kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*"} & actions(root[0])
    [service] = [s for s in policy if s["Principal"].get("Service") == "logs.us-east-1.amazonaws.com"]
    assert service["Condition"] == {
        "ArnEquals": {
            "kms:EncryptionContext:aws:logs:arn": "arn:aws:logs:us-east-1:123456789012:log-group:"
            "/aws/vendedlogs/mediatailor/fastm-release"
        }
    }
    readers = [s for s in policy if "Fn::GetAtt" in json.dumps(s["Principal"])]
    principals = [p for r in readers for p in _list(r["Principal"]["AWS"])]
    roles = resources(template, "AWS::IAM::Role")
    assert sorted(p["Fn::GetAtt"][0] for p in principals) == sorted(
        next(n for n in roles if n.startswith(start)) for start in ("ZeroAdsDetectorRole", "ControllerSmokeRole")
    )
    assert all(r["Condition"] == {"StringEquals": {"kms:ViaService": "logs.us-east-1.amazonaws.com"}} for r in readers)
    groups = resources(template, "AWS::Logs::LogGroup")
    [group] = [g for g in groups.values() if g.get("LogGroupName") == "/aws/vendedlogs/mediatailor/fastm-release"]
    assert "KmsKeyId" in group
    [destination] = resources(template, "AWS::Logs::DeliveryDestination").values()
    assert destination["Name"] == "fastm-release-mediatailor"


def test_drift_runs_every_hour_and_alarms_on_any(deployment: GetDeployment) -> None:
    d = deployment(RELEASE, extra=EXTRA)
    template = d.template(d.stage.release)
    alarms = {a["AlarmName"]: a for a in resources(template, "AWS::CloudWatch::Alarm").values()}

    drift = alarms["fastm-release-drift"]
    assert (drift["MetricName"], drift["Threshold"], drift["TreatMissingData"]) == ("ReleaseDrift", 1, "notBreaching")
    assert drift["Dimensions"] == [{"Name": "service", "Value": "release.drift"}]
    functions = resources(template, "AWS::Lambda::Function")
    [service] = [
        f["Environment"]["Variables"]["POWERTOOLS_SERVICE_NAME"]
        for f in functions.values()
        if f.get("Handler") == "fast_channels.functions.release.drift.handler"
    ]
    assert service == "release.drift"
    [schedule] = [s for s in resources(template, "AWS::Scheduler::Schedule").values() if "drift" in s["Description"]]
    assert schedule["ScheduleExpression"] == "rate(1 hour)"
    assert all(a["TreatMissingData"] == "notBreaching" for a in alarms.values())


def test_every_sibling_has_the_hook_alarms_and_the_zero_ad_sessions_detector(deployment: GetDeployment) -> None:
    d = deployment(RELEASE, extra=EXTRA)
    template = d.template(d.stage.release)
    alarms = {a["AlarmName"] for a in resources(template, "AWS::CloudWatch::Alarm").values()}

    for sibling in ("fastm-demo-p-a", "fastm-demo-p-b", "fastm-demo-p-z"):
        assert {f"{sibling}-presessioninithook-errors", f"{sibling}-function-errors"} <= alarms
    assert "fastm-release-zero-ads-sessions" in alarms
    [detector] = [
        f
        for f in resources(template, "AWS::Lambda::Function").values()
        if f.get("Handler") == "fast_channels.functions.ops.zero_ads_sessions.handler"
    ]
    variables = detector["Environment"]["Variables"]
    assert variables["POWERTOOLS_SERVICE_NAME"] == "ops.release_zero_ads_sessions"
    assert "/aws/vendedlogs/mediatailor/fastm-release" in json.dumps(variables)


def test_the_siblings_impressions_reach_the_pds_delivery_function(deployment: GetDeployment) -> None:
    """The siblings log to the module's group, not the stage's: the delivery function must read it too, or the
    sweeper would release delivered impressions as undelivered."""
    d = deployment(RELEASE, extra=EXTRA)
    template = d.template(d.stage.release).to_json()
    resources_ = template["Resources"]
    [log_group] = [
        k
        for k, r in resources_.items()
        if r["Type"] == "AWS::Logs::LogGroup"
        and r["Properties"].get("LogGroupName") == "/aws/vendedlogs/mediatailor/fastm-release"
    ]
    [subscription] = [r["Properties"] for r in resources_.values() if r["Type"] == "AWS::Logs::SubscriptionFilter"]
    assert subscription["FilterName"] == "fastm-pds-delivery-release"
    assert subscription["LogGroupName"] == {"Ref": log_group}
    assert subscription["FilterPattern"] == IMPRESSIONS.log_pattern_string
    [permission] = [
        r["Properties"]
        for r in resources_.values()
        if r["Type"] == "AWS::Lambda::Permission" and r["Properties"]["Principal"] == "logs.amazonaws.com"
    ]
    assert permission["Principal"] == "logs.amazonaws.com"
    assert permission["SourceArn"] == {"Fn::GetAtt": [log_group, "Arn"]}
    assert permission["FunctionName"] == subscription["DestinationArn"]
    parameter = "/fastm/pds/delivery-function"
    assert parameter in {p["Default"] for p in template["Parameters"].values()}
    pds = resources(d.template(d.stage.pds), "AWS::SSM::Parameter")
    assert sorted(p["Name"] for p in pds.values()) == [parameter, "/fastm/pds/policy-log-group"]
    off = deployment({**RELEASE, "modules.release.enabled": False}, extra=EXTRA)
    assert not resources(off.template(off.stage.pds), "AWS::SSM::Parameter")


def _definition(template: Template) -> dict[str, Any]:
    [machine] = resources(template, "AWS::StepFunctions::StateMachine").values()
    parts = machine["DefinitionString"]["Fn::Join"][1]
    return dict(json.loads("".join(p if isinstance(p, str) else "TOKEN" for p in parts)))


def test_the_apply_path_applies_one_release_at_a_time(deployment: GetDeployment) -> None:
    d = deployment(RELEASE, extra=EXTRA)
    template = d.template(d.stage.release)
    states = _definition(template)["States"]

    assert states["Prepare"]["Next"] == "AlreadyReleased?"
    assert [c["ErrorEquals"] for c in states["Prepare"]["Catch"]] == [["InvalidBundleError"], ["States.ALL"]]
    assert states["Prepare"]["Catch"][0]["Next"] == "BundleRejected"
    assert states["AlreadyReleased?"]["Default"] == "Accept"
    for step in ("Point", "Rollback", "Finish"):
        assert ["PointerBusyError"] in [r["ErrorEquals"] for r in states[step]["Retry"]], step
    [busy] = [r for r in states["Accept"]["Retry"] if r["ErrorEquals"] == ["ReleaseBusyError"]]
    assert (busy["IntervalSeconds"], busy["MaxAttempts"], busy["BackoffRate"]) == (60, 160, 1)  # demo: 40 + 120 min
    for step in ("Accept", "Functions", "Configure", "Publish"):
        assert states[step]["Catch"][-1] == {
            "ErrorEquals": ["States.ALL"],
            "Next": "RecordFailure",
            "Output": "{% $states.errorOutput %}",
        }
    assert states["Configure"]["MaxConcurrency"] == 1
    assert states["Publish"]["Next"] == "CheckDrift" and states["CheckDrift"]["Next"] == "Candidates"
    resources_ = template.to_json()["Resources"]
    [bundle] = [k for k, r in resources_.items() if r["Type"] == "Custom::DesiredState"]
    [rule] = [
        k for k, r in resources_.items() if r["Type"] == "AWS::Events::Rule" and k.startswith("ControllerOnDesired")
    ]
    assert rule in resources_[bundle]["DependsOn"], "the rule exists before the first event"
    [target] = resources(template, "AWS::Events::Rule")[rule]["Targets"]
    assert set(target["InputTransformer"]["InputPathsMap"].values()) == {
        "$.detail.bundle_key",
        "$.detail.digest",
        "$.detail.correlation_id",
    }


def test_the_lab_channel_is_a_stopped_basic_channel_with_siblings(deployment: GetDeployment) -> None:
    d = deployment(RELEASE, extra=EXTRA)
    media = d.template(d.stage.media)
    channels = {c["ChannelName"]: c for c in resources(media, "AWS::MediaTailor::Channel").values()}
    assert channels["fastm-lab"]["Tier"] == "BASIC"
    release = d.template(d.stage.release)
    [promote] = [
        f
        for f in resources(release, "AWS::Lambda::Function").values()
        if f.get("Handler") == "fast_channels.functions.release.promote.handler"
    ]
    assert "TOOL_FUNCTIONS" in promote["Environment"]["Variables"], "it chooses the lab only with the agent tools"
    policies = {p["ChannelName"]: p for p in resources(media, "AWS::MediaTailor::ChannelPolicy").values()}
    assert '"-p-?"' in json.dumps(policies["fastm-lab"]["Policy"]), "the lab channel admits its siblings"
    assert d.stage.release.rendered.bundle.channels[1].lab
    off = deployment({**RELEASE, "modules.release.lab_channel": False}, extra=EXTRA)
    assert "fastm-lab" not in {
        c["ChannelName"] for c in resources(off.template(off.stage.media), "AWS::MediaTailor::Channel").values()
    }


def test_the_appconfig_environment_monitors_the_candidate_alarm(deployment: GetDeployment) -> None:
    d = deployment(RELEASE, extra=EXTRA)
    ops = d.template(d.stage.ops)
    [environment] = resources(ops, "AWS::AppConfig::Environment").values()
    [monitor] = environment["Monitors"]
    assert monitor["AlarmArn"] == "arn:aws:cloudwatch:us-east-1:123456789012:alarm:fastm-release-candidate"
    role_id = monitor["AlarmRoleArn"]["Fn::GetAtt"][0]
    role = resources(ops, "AWS::IAM::Role")[role_id]
    assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {"Service": "appconfig.amazonaws.com"}
    # AppConfig finds a composite alarm only with an unfiltered DescribeAlarms (fastm 2026-10-29)
    [describe] = [s for r, s in statements(ops) if r == role_id]
    assert (describe["Action"], describe["Resource"]) == ("cloudwatch:DescribeAlarms", "*")
    plain = deployment({**RELEASE, "modules.release.enabled": False}, extra=EXTRA)
    [unmonitored] = resources(plain.template(plain.stage.ops), "AWS::AppConfig::Environment").values()
    assert "Monitors" not in unmonitored
    # the monitors watch AllAtOnce's 10-minute bake: monitored, the flags deploy with no bake
    [flags] = resources(ops, "AWS::AppConfig::Deployment").values()
    [at_once] = resources(ops, "AWS::AppConfig::DeploymentStrategy").values()
    assert (at_once["Name"], at_once["FinalBakeTimeInMinutes"], at_once["DeploymentDurationInMinutes"]) == (
        "fastm-flags-at-once",
        0,
        0,
    )
    assert "Ref" in flags["DeploymentStrategyId"]
    [plain_flags] = resources(plain.template(plain.stage.ops), "AWS::AppConfig::Deployment").values()
    assert plain_flags["DeploymentStrategyId"] == "AppConfig.AllAtOnce"
    release = d.template(d.stage.release)
    [candidate] = resources(release, "AWS::CloudWatch::CompositeAlarm").values()
    assert candidate["AlarmName"] == "fastm-release-candidate"
    rule = json.dumps(candidate["AlarmRule"])
    for sibling in ("FastmDemoPA", "FastmDemoPB", "FastmLabPA", "FastmLabPB"):  # the children's logical ids
        assert sibling in rule
    assert "FastmDemoPZ" not in rule and "FastmLabPZ" not in rule  # the probes are never candidates
    strategies = {s["Name"]: s for s in resources(release, "AWS::AppConfig::DeploymentStrategy").values()}
    at_once = strategies.pop("fastm-release-at-once")
    assert (at_once["DeploymentDurationInMinutes"], at_once["FinalBakeTimeInMinutes"]) == (0, 0), "no bake to wait out"
    [strategy] = strategies.values()
    assert (strategy["GrowthType"], strategy["GrowthFactor"], strategy["DeploymentDurationInMinutes"]) == (
        "LINEAR",
        10,
        30,
    )


def test_a_probe_opens_its_own_session_and_waits_for_its_logs_before_its_verdict(deployment: GetDeployment) -> None:
    d = deployment(RELEASE, extra=EXTRA)
    states = _definition(d.template(d.stage.release))["States"]
    processor = states["SmokeAndSoak"]["ItemProcessor"]
    inner = processor["States"]
    assert processor["StartAt"] == "Candidate" and inner["Candidate"]["Next"] == "SmokeOpen"
    for probe, then in (("Smoke", "SoakOpen"), ("Soak", "Soaked")):
        assert inner[f"{probe}Open"]["Next"] == f"{probe}Watch"
        seen = inner[f"{probe}Seen?"]
        assert seen["Choices"][0]["Next"] == f"{probe}LogDelay" and seen["Default"] == f"{probe}Watch"
        delay = inner[f"{probe}LogDelay"]
        assert (delay["Type"], delay["Seconds"], delay["Next"]) == ("Wait", 180, f"{probe}Judge")
        assert inner[f"{probe}Passed?"]["Choices"][0]["Next"] == then
        assert inner[f"{probe}Passed?"]["Default"] == f"{probe}Failed"
    breaks = {probe: inner[f"{probe}Judge"]["Arguments"]["Payload"]["breaks"] for probe in ("Smoke", "Soak")}
    assert breaks == {"Smoke": 2, "Soak": 3}


def test_a_failed_smoke_rolls_back_and_stops_the_lab_only_if_it_started_it(deployment: GetDeployment) -> None:
    d = deployment(RELEASE, extra=EXTRA)
    states = _definition(d.template(d.stage.release))["States"]
    [caught] = states["SmokeAndSoak"]["Catch"]
    assert caught["Next"] == "LabToStop?"
    choice = states["LabToStop?"]
    assert choice["Choices"][0]["Next"] == "LabStopAfterFailure" and choice["Default"] == "Rollback"
    assert states["LabStopAfterFailure"]["Next"] == "Rollback"
    assert states["Rollback"]["Next"] == "RolledBack"


def test_the_marker_detector_counts_a_breaks_first_marker_and_alarms_on_two_lossy_minutes(
    deployment: GetDeployment,
) -> None:
    d = deployment(RELEASE, extra=EXTRA)
    template = d.template(d.stage.release)
    filters = {f["FilterName"]: f for f in resources(template, "AWS::Logs::MetricFilter").values()}
    assert filters["AdMarkersFound"]["FilterPattern"] == (
        '{ ($.eventType = "AD_MARKER_FOUND") && ($.adMarkers != "*,DURATION=*") }'
    )
    assert filters["AdsRequests"]["FilterPattern"] == '{ $.eventType = "MAKING_ADS_REQUEST" }'
    alarms = {a["AlarmName"]: a for a in resources(template, "AWS::CloudWatch::Alarm").values()}
    for name in ("fastm-demo-p-a", "fastm-demo-p-b", "fastm-lab-p-b", "fastm-lab-x"):
        alarm = alarms[f"{name}-markers-without-ads-request"]
        assert (alarm["EvaluationPeriods"], alarm["DatapointsToAlarm"], alarm["Threshold"]) == (10, 2, 0.5)
        assert alarm["TreatMissingData"] == "notBreaching"
        [expression] = [m for m in alarm["Metrics"] if "Expression" in m]
        assert expression["Expression"].startswith("IF(FILL(markers, 0) >= 5, ")
    [candidate] = resources(template, "AWS::CloudWatch::CompositeAlarm").values()
    assert "FastmLabXMarkers" not in json.dumps(candidate["AlarmRule"]), "lab-x is never a candidate"
    off = deployment({**RELEASE, "modules.release.lab_channel": False}, extra=EXTRA)
    names_ = {a["AlarmName"] for a in resources(off.template(off.stage.release), "AWS::CloudWatch::Alarm").values()}
    assert "fastm-lab-x-markers-without-ads-request" not in names_


def test_template_snapshot(
    deployment: GetDeployment, normalized: Callable[..., Any], snapshot_json: SnapshotAssertion
) -> None:
    d = deployment(RELEASE, extra=EXTRA)

    assert normalized(d.template(d.stage.release)) == snapshot_json
