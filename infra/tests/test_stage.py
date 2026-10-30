import importlib.util
import io
import json
import shlex
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from aws_cdk import App, Environment, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm
from aws_cdk.assertions import Template

from fast_channels.core.naming import check_prefix
from fast_channels.infra.app import build
from fast_channels.infra.config import ConfigError, FastChannelsConfig, ModulesConfig
from fast_channels.infra.stage import FastChannelsStage, stage_id

type GetDeployment = Callable[..., Any]

REPO = Path(__file__).resolve().parents[2]
CDK_JSON = REPO / "cdk.json"

# A single global profile: one model id whatever the deployment region.
GLOBAL_MODELS = {
    "agent.model": "global.anthropic.claude-haiku-5-5",
    "agent.allowed_models": ["global.anthropic.claude-haiku-5-5"],
}
EVERYTHING = {**GLOBAL_MODELS, "observability.transaction_search": "enable"}
WITH_BASELINE = {**EVERYTHING, "account-baseline": True}
"""What ``make account-baseline`` synthesizes (``-c account-baseline=true``)."""


def names(deployment: Any) -> dict[str, str]:
    return {stack.node.id: stack.stack_name for stack in deployment.stacks}


def test_default_stacks(default: Any) -> None:
    assert names(default) == {
        "Data": "FastChannels-Data",
        "Media": "FastChannels-Media",
        "Operator": "FastChannels-Operator",
    }
    assert (default.stage.edge, default.stage.baseline) == (None, None)


def test_operator_stack_only_with_the_agent(deployment: GetDeployment) -> None:
    assert set(names(deployment({"agent.enabled": False}))) == {"Data", "Media"}


def test_edge_stack_in_us_east_1_when_deploying_elsewhere(deployment: GetDeployment) -> None:
    d = deployment(GLOBAL_MODELS, region="eu-west-1")

    assert d.stage.edge is not None
    assert (d.stage.edge.stack_name, d.stage.edge.region) == ("FastChannels-Edge-eu-west-1", "us-east-1")
    assert {stack.region for stack in d.stacks if stack is not d.stage.edge} == {"eu-west-1"}


def test_edge_stacks_of_one_prefix_in_two_regions_do_not_collide(deployment: GetDeployment) -> None:
    edges = [deployment(GLOBAL_MODELS, region=region).stage.edge for region in ("eu-west-1", "ap-northeast-1")]

    assert [edge.stack_name for edge in edges] == ["FastChannels-Edge-eu-west-1", "FastChannels-Edge-ap-northeast-1"]


@pytest.mark.parametrize("mode", ["off", "existing", "enable"])
def test_no_account_baseline_without_the_context_flag(deployment: GetDeployment, mode: str) -> None:
    """`make deploy` and `make destroy` never select the account baseline, whose termination protection would stop
    `cdk destroy '**'` part-way."""
    assert deployment({"observability.transaction_search": mode}).stage.baseline is None


@pytest.mark.parametrize("flag", [True, "true", "TRUE"])
def test_only_the_context_flag_adds_the_account_baseline(deployment: GetDeployment, flag: object) -> None:
    enable = {"observability.transaction_search": "enable"}
    baseline = deployment({**enable, "account-baseline": flag}).stage.baseline

    assert baseline is not None
    assert (baseline.stack_name, baseline.termination_protection) == ("FastChannels-AccountBaseline", True)
    assert deployment({**enable, "account-baseline": "false"}).stage.baseline is None


@pytest.mark.parametrize("mode", ["off", "existing"])
def test_the_account_baseline_needs_transaction_search_enable(fresh_deployment: GetDeployment, mode: str) -> None:
    with pytest.raises(ConfigError, match=f"transaction_search is '{mode}': set it to 'enable'"):
        fresh_deployment({"observability.transaction_search": mode, "account-baseline": True})


@pytest.mark.parametrize("overrides", [{}, EVERYTHING], ids=["default", "transaction-search-enable"])
def test_make_destroy_selects_no_termination_protected_stack(
    fresh_deployment: GetDeployment, tmp_path: Path, overrides: dict[str, Any]
) -> None:
    """The cloud assembly that `make deploy` and `make destroy` select (`'**'`, no account-level flag): with
    `removal: destroy` no stack in it is termination protected, so `cdk destroy` can delete every one."""
    fresh_deployment(overrides, region="eu-west-1", outdir=tmp_path).app.synth()
    [manifest] = tmp_path.glob("assembly-*/manifest.json")
    artifacts = json.loads(manifest.read_text())["artifacts"].values()
    protection = {
        a["properties"]["stackName"]: a["properties"].get("terminationProtection", False)
        for a in artifacts
        if a["type"] == "aws:cloudformation:stack"
    }

    assert "FastChannels-AccountBaseline" not in protection
    assert not any(protection.values()), protection


def test_stack_dependencies(deployment: GetDeployment) -> None:
    stage = deployment(EVERYTHING, region="eu-west-1").stage
    with_baseline = deployment(WITH_BASELINE, region="eu-west-1").stage
    with_models = deployment({"modules.models.enabled": True}).stage

    def deps(stack: Stack) -> set[str]:
        return {d.node.id for d in stack.dependencies}

    assert deps(stage.data) == set()
    assert deps(stage.media) == {"Data"}
    assert deps(stage.operator) == {"Data", "Media"}
    assert deps(with_baseline.operator) == {"Data", "Media", "AccountBaseline"}
    assert deps(stage.edge) == {"Data", "Media"}
    assert deps(with_models.models) == set()
    assert deps(with_models.operator) == {"Data", "Media", "Models"}


def test_memory_is_created_before_any_operator_resource(default: Any) -> None:
    assert default.stage.data in default.stage.operator.dependencies
    assert default.stage.data.memory is not None


@pytest.mark.parametrize(("removal", "protected"), [("destroy", False), ("retain", True)])
def test_termination_protection_follows_removal(deployment: GetDeployment, removal: str, protected: bool) -> None:
    d = deployment({**WITH_BASELINE, "removal": removal}, region="eu-west-1")

    assert {s.node.id: s.termination_protection for s in d.stacks} == {
        "Data": protected,
        "Media": protected,
        "Operator": protected,
        "Edge": protected,
        "AccountBaseline": True,
    }


def test_every_resource_and_stack_is_tagged(default: Any) -> None:
    for stack in default.stacks:
        assert stack.tags.tag_values() == {"fast:app": "fast", "fast:owner": "iac"}
    for bucket in default.template(default.stage.data).find_resources("AWS::S3::Bucket").values():
        assert {"Key": "fast:app", "Value": "fast"} in bucket["Properties"]["Tags"]
        assert {"Key": "fast:owner", "Value": "iac"} in bucket["Properties"]["Tags"]


def test_other_prefixes_get_their_own_stack_names(deployment: GetDeployment) -> None:
    d = deployment({"prefix": "acme", "channels.iac": []})

    assert stage_id("fast") == "FastChannels"
    assert names(d) == {
        "Data": "FastChannels-acme-Data",
        "Media": "FastChannels-acme-Media",
        "Operator": "FastChannels-acme-Operator",
    }
    d.template(d.stage.data).has_resource_properties("AWS::BedrockAgentCore::Memory", {"Name": "acme_memory"})


def test_consumers_get_weak_references_also_across_regions(fresh_deployment: Callable[..., Any]) -> None:
    d = fresh_deployment(GLOBAL_MODELS, region="eu-west-1")
    exports = d.stage.data.exports
    role = iam.Role(d.stage.media, "Consumer", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    exports.media_bucket.grant_read(role)
    exports.library_table.grant_read_write_data(role)
    assert exports.memory is not None
    exports.memory.grant_read(role)
    ssm.StringParameter(d.stage.edge, "LogsBucket", string_value=exports.logs_bucket.bucket_arn)

    media = json.dumps(d.template(d.stage.media).to_json())
    edge = d.template(d.stage.edge).to_json()

    assert "Fn::ImportValue" not in media
    assert media.count('"Fn::GetStackOutput": {"StackName": "FastChannels-Data", "Region": "eu-west-1"') >= 3
    [parameter] = (r for r in edge["Resources"].values() if r["Type"] == "AWS::SSM::Parameter")
    reference = parameter["Properties"]["Value"]["Fn::GetStackOutput"]
    assert (reference["StackName"], reference["Region"]) == ("FastChannels-Data", "eu-west-1")
    assert "BucketPolicy" not in media  # grants never touch DataStack's resource policies


TOPOLOGIES = {"us-east-1": ({}, "us-east-1"), "everything-elsewhere": (WITH_BASELINE, "eu-west-1")}


@pytest.mark.parametrize(("overrides", "region"), TOPOLOGIES.values(), ids=TOPOLOGIES.keys())
def test_no_account_level_logs_resource_policy_outside_the_baseline(
    deployment: GetDeployment, overrides: dict[str, Any], region: str
) -> None:
    d = deployment(overrides, region=region)

    for stack in d.stacks:
        expected = 1 if stack is d.stage.baseline else 0
        d.template(stack).resource_count_is("AWS::Logs::ResourcePolicy", expected)


@pytest.mark.parametrize(("overrides", "region"), TOPOLOGIES.values(), ids=TOPOLOGIES.keys())
def test_no_public_function_urls_or_invoke_permissions(
    deployment: GetDeployment, overrides: dict[str, Any], region: str
) -> None:
    d = deployment(overrides, region=region)

    for stack in d.stacks:
        template = d.template(stack)
        template.resource_count_is("AWS::Lambda::Url", 0)
        for permission in template.find_resources("AWS::Lambda::Permission").values():
            props = permission["Properties"]
            assert props["Principal"] != "*" and "FunctionUrlAuthType" not in props, stack.stack_name


def test_region_is_required() -> None:
    with pytest.raises(ConfigError, match="cannot determine the deployment region"):
        build(App(), account=None, region=None)
    with pytest.raises(ValueError, match="region must be known at synth time"):
        FastChannelsStage(App(), "Stage", config=FastChannelsConfig(), env=Environment())


def test_unresolved_account_still_synthesizes_across_regions(fresh_deployment: Callable[..., Any]) -> None:
    d = fresh_deployment(GLOBAL_MODELS, region="eu-west-1", account=None)
    ssm.StringParameter(d.stage.edge, "LogsBucket", string_value=d.stage.data.exports.logs_bucket.bucket_arn)

    resources = d.template(d.stage.edge).to_json()["Resources"].values()
    [parameter] = (r for r in resources if r["Type"] == "AWS::SSM::Parameter")
    assert parameter["Properties"]["Value"]["Fn::GetStackOutput"]["Region"] == "eu-west-1"


def test_other_cdns_reach_the_channels_tool(deployment: GetDeployment) -> None:
    cdns = [{"name": "cdn-b", "manifest_base": "https://b.example.com", "content_base": "https://b.example.com/c"}]
    d = deployment({"delivery.cdns": cdns})

    [variables] = [
        fn["Properties"]["Environment"]["Variables"]
        for fn in d.template(d.stage.operator).find_resources("AWS::Lambda::Function").values()
        if "CDN_ENDPOINTS" in fn["Properties"].get("Environment", {}).get("Variables", {})
    ]
    assert json.loads(variables["CDN_ENDPOINTS"]) == cdns


def runtime_environment(deployment: Any) -> dict[str, str]:
    [runtime] = deployment.template(deployment.stage.operator).find_resources("AWS::BedrockAgentCore::Runtime").values()
    return dict(runtime["Properties"]["EnvironmentVariables"])


@pytest.mark.parametrize(
    ("region", "geo"), [("us-east-1", "us"), ("eu-west-1", "eu"), ("ap-southeast-2", "au"), ("sa-east-1", "global")]
)
def test_shipped_configuration_deploys_in_every_agentcore_geography(
    deployment: GetDeployment, region: str, geo: str
) -> None:
    environment = runtime_environment(deployment(region=region))  # no model overrides: cdk.json uses {geo}

    assert environment["DEFAULT_MODEL_ID"] == f"{geo}.anthropic.claude-haiku-5-5"
    assert "{geo}" not in environment["ALLOWED_MODELS"]


def test_stage_resolves_a_configuration_validated_without_a_region() -> None:
    flags = {k: v for k, v in json.loads(CDK_JSON.read_text())["context"].items() if k != "fast-channels"}
    app = App(context={**flags, "aws:cdk:bundling-stacks": []})
    stage = FastChannelsStage(
        app, "S", config=FastChannelsConfig(), env=Environment(account="123456789012", region="eu-west-1")
    )
    assert stage.operator is not None
    [runtime] = Template.from_stack(stage.operator).find_resources("AWS::BedrockAgentCore::Runtime").values()

    assert runtime["Properties"]["EnvironmentVariables"]["DEFAULT_MODEL_ID"] == "eu.anthropic.claude-haiku-5-5"


def _stage_args() -> ModuleType:
    """``scripts/stage_args.py``, which ``make <target> STAGE=<name>`` runs."""
    spec = importlib.util.spec_from_file_location("fast_script_stage_args", REPO / "scripts" / "stage_args.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


stage_args = _stage_args()
STAGES = sorted(path.stem for path in (REPO / "stages").glob("*.json"))


def stage_overrides(name: str, *argv: str) -> dict[str, str]:
    """``deployment()`` overrides from what ``stage_args.py context <name>`` prints: the CLI's strings."""
    out = io.StringIO()
    with redirect_stdout(out):
        assert stage_args.main(["context", name, *argv]) == 0
    words = shlex.split(out.getvalue())
    assert words[::2] == ["-c"] * (len(words) // 2)
    return dict(word.removeprefix("fast-channels.").split("=", 1) for word in words[1::2])


def shipped_with(overrides: dict[str, str]) -> FastChannelsConfig:
    """cdk.json's configuration with ``-c fast-channels.<key>=<value>`` overrides, validated for us-east-1."""
    flags = json.loads(CDK_JSON.read_text())["context"]
    app = App(context={**flags, **{f"fast-channels.{key}": value for key, value in overrides.items()}})
    return FastChannelsConfig.from_context(app.node, region="us-east-1")


def test_shipped_stages() -> None:
    assert STAGES == stage_args.stage_names() == ["fastci", "fastm"]
    assert f"^{stage_args.PREFIX.pattern}$" == FastChannelsConfig.model_json_schema()["properties"]["prefix"]["pattern"]


@pytest.mark.parametrize("name", STAGES)
def test_stage_files_name_their_prefix_and_turn_every_module_on(name: str) -> None:
    config = shipped_with(stage_overrides(name))

    assert config.prefix == name
    assert all(config.modules.enabled(module) for module in ModulesConfig.model_fields)
    assert config.modules.release.channels == (f"{name}-demo",)
    assert config.modules.demand.simulator == "lambda"  # needs no VPC of its own
    assert config.modules.ops.ledger_retention_days == 1  # so that the stage can be destroyed the next day


def test_shipped_prefixes_are_hyphen_free_so_none_falls_inside_anothers_grants() -> None:
    """cdk.json's prefix and every stage file's pass ``core.naming.check_prefix``: no shipped deployment's
    ``<prefix>-*`` names and grants cover another's resources."""
    prefixes = [json.loads(CDK_JSON.read_text())["context"]["fast-channels"]["prefix"]]
    prefixes += [stage_args.load(name)["prefix"] for name in STAGES]

    assert [check_prefix(prefix) for prefix in prefixes] == prefixes
    assert [(a, b) for a in prefixes for b in prefixes if a != b and b.startswith(f"{a}-")] == []


def test_stage_prefix_resolution(capsys: pytest.CaptureFixture[str]) -> None:
    assert stage_args.main(["prefix", "fastm"]) == 0
    assert stage_args.main(["prefix", "fastm", "--prefix", "fastx"]) == 0

    assert capsys.readouterr().out.splitlines() == ["fastm", "fastx"]


def test_a_stage_can_keep_only_some_modules_on(capsys: pytest.CaptureFixture[str]) -> None:
    """The demand leg deploys ``fastci`` with ops and demand only (``MODULES=ops,demand``)."""
    stage = stage_args.resolve("fastci", "fastcix", ["ops", "demand"])
    config = shipped_with(
        {f"modules.{name}": value for name, value in stage["modules"].items()} | {"prefix": "fastcix"}
    )

    assert {name for name in ModulesConfig.model_fields if config.modules.enabled(name)} == {"ops", "demand"}
    assert config.modules.demand.simulator == "lambda", "the stage's own settings of a kept module stay"
    assert stage_args.main(["context", "fastci", "--modules", "ops,demand"]) == 0
    assert '"models":{"enabled":false}' in capsys.readouterr().out
    with pytest.raises(stage_args.StageError, match="are not modules of the stage"):
        stage_args.resolve("fastci", None, ["ops", "nothing"])


def test_a_stage_under_another_prefix_expands_it() -> None:
    config = shipped_with(stage_overrides("fastci", "--prefix", "fastcipr7"))

    assert (config.prefix, config.channels.iac[0].name) == ("fastcipr7", "fastcipr7-demo")
    assert config.modules.release.channels == ("fastcipr7-demo",)


@pytest.mark.parametrize(
    ("files", "argv", "expected"),
    [
        ({}, ["context", "fastz"], "unknown stage 'fastz': the stages are fastci, fastm"),
        ({}, ["context", "../cdk"], "unknown stage '../cdk'"),
        ({}, ["context", "fastm", "--prefix", "Fast_M"], "--prefix must match"),
        ({"bad": "[1, 2]"}, ["prefix", "bad"], "must hold a JSON object (a partial fast-channels block)"),
        ({"bad": "{"}, ["prefix", "bad"], "is not JSON"),
        ({"bad": '{"prefix": "bad", "modulez": {}}'}, ["prefix", "bad"], "['modulez'] are not fast-channels fields"),
        ({"bad": '{"modules": {}}'}, ["prefix", "bad"], "prefix must be a deployment prefix matching"),
        ({"bad": '{"prefix": "fast_m"}'}, ["context", "bad"], "got 'fast_m'"),
        ({"bad": '{"prefix": "fastm-x"}'}, ["context", "bad"], "(no hyphen: names and IAM grants are scoped by"),
        ({}, ["context", "fastm", "--prefix", "fastm-x"], "--prefix must match [a-z][a-z0-9]{1,15} (no hyphen"),
    ],
)
def test_unusable_stages_fail_before_any_command_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    files: dict[str, str],
    argv: list[str],
    expected: str,
) -> None:
    for path in (REPO / "stages").glob("*.json"):
        (tmp_path / path.name).write_text(path.read_text())
    for name, text in files.items():
        (tmp_path / f"{name}.json").write_text(text)
    monkeypatch.setattr(stage_args, "STAGES", tmp_path)

    assert stage_args.main(argv) == 1
    captured = capsys.readouterr()
    assert (captured.out, expected in captured.err) == ("", True), captured.err


MERGED_MODULES = ("models", "ops", "release", "demand")
"""Modules whose code is merged; a module's pull request adds it here, with tests of what it synthesizes."""
NEEDED_MODULES = ("pds",)
"""Modules a merged one needs on (``release`` needs ``pds``) whose code is not merged yet: they add nothing, so the
comparison below still isolates the merged code."""
MODULE_STACKS = {"Crawl": "FastChannels-fastm-Crawl"}
"""Stacks of merged modules kept off in the comparison (``crawl``, whose needs would turn every module on) ..."""
MODULE_CHANGES = ("Media",)
"""... and the shared stacks they change: crawl adds its catalog-context bucket and route to Media. Their own tests
pin those changes down (``test_crawl.py::test_the_module_changes_nothing_else_in_the_media_stack``)."""


def test_fastm_synthesizes_only_what_the_merged_modules_add(deployment: GetDeployment, normalized: Any) -> None:
    """Enabling a module changes nothing until its code is merged: the ``fastm`` stage, with every module on,
    synthesizes exactly what the shipped configuration does with only the merged modules (and those they need) on,
    plus what the modules kept off add. What the merged modules add has its own tests (``test_release_policy.py``,
    ``test_crawl.py``, ``test_demand_stacks.py``)."""
    stage = stage_args.load("fastm")
    fastm = deployment(stage_overrides("fastm"))
    on = (*NEEDED_MODULES, *MERGED_MODULES)
    merged = deployment(
        {
            "prefix": "fastm",
            "observability": stage["observability"],  # the stage reads the shared as-run router (not a module)
            **{f"modules.{name}": stage["modules"][name] for name in on},
        }
    )

    merged_modules = FastChannelsConfig.from_context(merged.app.node).modules
    assert FastChannelsConfig.from_context(fastm.app.node).modules != merged_modules
    assert {name for name in ModulesConfig.model_fields if merged_modules.enabled(name)} == set(on)
    assert names(merged) == {
        "Data": "FastChannels-fastm-Data",
        "Media": "FastChannels-fastm-Media",
        "Models": "FastChannels-fastm-Models",
        "Ops": "FastChannels-fastm-Ops",
        "Operator": "FastChannels-fastm-Operator",
        "DemandEdge": "FastChannels-fastm-DemandEdge",
        "DemandBuild": "FastChannels-fastm-DemandBuild",
    }
    assert names(fastm) == names(merged) | MODULE_STACKS
    modules_on = {stack.node.id: stack for stack in fastm.stacks}
    for modules_merged in merged.stacks:
        if modules_merged.node.id in MODULE_CHANGES:
            continue
        stack = modules_on[modules_merged.node.id]
        assert normalized(fastm.template(stack)) == normalized(merged.template(modules_merged)), stack.stack_name


def test_module_stacks_follow_the_stacks_they_build_on(deployment: GetDeployment) -> None:
    stage = deployment({"modules.ops.enabled": True}).stage

    assert {d.node.id for d in stage.ops.dependencies} == {"Data", "Media"}
    assert stage.ops not in stage.operator.dependencies
