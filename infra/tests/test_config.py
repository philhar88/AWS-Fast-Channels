import json
from collections.abc import Callable
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, get_args, get_origin

import pytest
from aws_cdk import RemovalPolicy
from aws_cdk import aws_logs as logs
from pydantic import BaseModel

from fast_channels.core.ladder import Ladder
from fast_channels.core.model_registry import Effort, ModeEffort
from fast_channels.core.monetization.bundle import ROLLOUTS, RolloutTier
from fast_channels.core.profiles import CATALOGUE, DEFAULT_PACKAGE_PROFILES, RenditionProfile
from fast_channels.infra.config import (
    EVAL_RECORDS_ENV,
    AdsMode,
    ChannelTier,
    ConfigError,
    DemoLine,
    ExternalVastAds,
    FastChannelsConfig,
    LedgerLock,
    ModulesConfig,
    PbsVersion,
    PreconditionedAds,
    ReleaseStrategy,
    Removal,
    Simulator,
    TransactionSearchMode,
    geo_prefix,
)

type Load = Callable[..., FastChannelsConfig]

SHIPPED: dict[str, Any] = json.loads((Path(__file__).resolve().parents[2] / "cdk.json").read_text())["context"][
    "fast-channels"
]


US_HAIKU = "us.anthropic.claude-haiku-5-5"


def with_shipped(**overrides: Any) -> dict[str, Any]:
    return {"fast-channels": SHIPPED, **{f"fast-channels.{k}": v for k, v in overrides.items()}}


def config_error(load: Load, raw: dict[str, Any], region: str | None = None) -> str:
    with pytest.raises(ConfigError) as error:
        load(raw, region=region)
    return str(error.value)


def test_shipped_configuration(load_config: Load) -> None:
    config = load_config({"fast-channels": SHIPPED}, region="us-east-1")

    assert (config.prefix, config.removal) == ("fast", Removal.DESTROY)
    assert config.ladders.builtin == ("standard",)
    assert config.channel_assembly.package_profiles == DEFAULT_PACKAGE_PROFILES == ("hls",)
    [channel] = config.channels.iac
    assert (channel.name, channel.ladder, channel.tier) == ("fast-demo", "standard", ChannelTier.BASIC)
    assert channel.outputs == DEFAULT_PACKAGE_PROFILES
    assert config.ads == PreconditionedAds()
    assert config.agent.enabled
    assert config.agent.model == US_HAIKU  # {geo} in cdk.json, resolved for us-east-1
    assert config.agent.allowed_models == (
        US_HAIKU,
        "us.anthropic.claude-sonnet-5-5",
        "global.anthropic.claude-haiku-5-5",
        "global.anthropic.claude-sonnet-5-5",
    )
    assert config.agent.console_dev_url is None
    assert config.observability.transaction_search is TransactionSearchMode.OFF


def spelled_out(annotation: Any, value: Any, path: str) -> None:
    """Every section in ``value``, a configuration value of type ``annotation``, sets each of its fields: in nested
    sections, list items, mapping values and the members of unions (the one whose fields ``value`` has)."""
    annotation = getattr(annotation, "__value__", annotation)  # a type alias
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        spelled_out(args[0], value, path)
    elif origin is UnionType and isinstance(value, dict):
        sections = [a for a in args if isinstance(a, type) and issubclass(a, BaseModel)]
        matching = [section for section in sections if set(value) == set(section.model_fields)]
        assert matching, f"{path}: {sorted(value)} are not the fields of any of {sections}"
        spelled_out(matching[0], value, path)
    elif origin is tuple and isinstance(value, list):
        for index, item in enumerate(value):
            spelled_out(args[0], item, f"{path}[{index}]")
    elif origin is dict and isinstance(value, dict):
        for key, item in value.items():
            spelled_out(args[1], item, f"{path}.{key}")
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        assert isinstance(value, dict) and set(value) == set(annotation.model_fields), path
        for name, item in value.items():
            spelled_out(annotation.model_fields[name].annotation, item, f"{path}.{name}")


def test_shipped_configuration_spells_out_every_field() -> None:
    spelled_out(FastChannelsConfig, SHIPPED, "fast-channels")


def test_spelled_out_finds_a_missing_field_in_nested_sections() -> None:
    task = dict(SHIPPED["modules"]["models"]["tasks"]["scheduler.plan"], effort={"chat": "low"})
    shipped = {**SHIPPED, "modules": {**SHIPPED["modules"], "models": {"enabled": False, "tasks": {"t": task}}}}

    with pytest.raises(AssertionError, match=r"fast-channels\.modules\.models"):
        spelled_out(FastChannelsConfig, shipped, "fast-channels")


def test_model_defaults_without_context(load_config: Load) -> None:
    config = load_config({})

    assert config == FastChannelsConfig()
    assert config.channels.iac == ()
    assert config.removal_policy == RemovalPolicy.DESTROY
    assert config.observability.log_retention == logs.RetentionDays.ONE_WEEK


def test_cli_string_overrides_are_json_decoded(load_config: Load) -> None:
    config = load_config(
        with_shipped(
            **{
                "agent.enabled": "false",
                "channels.max_agent_channels": "5",
                "removal": "retain",
                "observability.log_retention_days": "90",
            }
        )
    )

    assert not config.agent.enabled
    assert config.channels.max_agent_channels == 5
    assert config.removal_policy == RemovalPolicy.RETAIN
    assert config.observability.log_retention == logs.RetentionDays.THREE_MONTHS


def test_overrides_address_list_items_and_deep_merge_objects(load_config: Load) -> None:
    config = load_config(
        with_shipped(
            **{
                "channels.iac.0.tier": "STANDARD",
                "agent": '{"admin_email": "ops@example.com"}',
                "channels.iac.0.brief": {"ratings": ["TV-G"]},
            }
        )
    )

    [channel] = config.channels.iac
    assert channel.tier is ChannelTier.STANDARD
    assert channel.brief is not None and channel.brief.ratings == ["TV-G"]
    assert config.agent.admin_email == "ops@example.com" and config.agent.enabled  # merged, not replaced


def test_prefix_override_needs_matching_channel_names(load_config: Load) -> None:
    config = load_config(with_shipped(**{"prefix": "acme", "channels.iac.0.name": "acme-news"}))

    assert (config.prefix, config.channels.iac[0].name) == ("acme", "acme-news")


def test_a_prefix_with_a_hyphen_is_refused_with_the_reason(load_config: Load) -> None:
    message = config_error(load_config, with_shipped(**{"prefix": "fast-x", "channels.iac.0.name": "fast-x-demo"}))

    assert (
        "  - fast-channels.prefix: 'fast-x' is not a deployment prefix: use 2-16 lowercase letters and digits, "
        "starting with a letter, without hyphens; resource names and IAM grants are scoped by '<prefix>-*', so the "
        "grants of a deployment 'fast' ('fast-*') would cover the resources of 'fast-x'"
    ) in message


def test_whole_block_as_json_string(load_config: Load) -> None:
    config = load_config({"fast-channels": json.dumps({"prefix": "demo", "agent": {"enabled": False}})})

    assert (config.prefix, config.agent.enabled, config.channels.iac) == ("demo", False, ())


def test_errors_list_every_problem_with_its_path(load_config: Load) -> None:
    message = config_error(
        load_config,
        {
            "fast-channels": {
                "prefix": "Fast!",
                "agent": {"model": "anthropic.unknown"},
                "observability": {"log_retention_days": 10},
                "typo": 1,
            }
        },
    )

    assert message.splitlines()[0] == "invalid FAST Channels configuration:"
    assert "  - fast-channels.prefix: 'Fast!' is not a deployment prefix: use 2-16 lowercase letters" in message
    assert "  - fast-channels.agent: model 'anthropic.unknown' is not in allowed_models" in message
    assert "  - fast-channels.observability.log_retention_days: CloudWatch Logs supports retention of" in message
    assert "  - fast-channels.typo: Extra inputs are not permitted" in message


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"channels.iac.0.outputs": ["hls", "dash-avc"]}, "unknown rendition profiles ['dash-avc']; choose from"),
        ({"channels.iac.0.outputs": []}, "at least one output is required"),
        ({"channel_assembly.package_profiles": ["hls", "hls-avc"]}, "unknown rendition profiles ['hls-avc']"),
        ({"channel_assembly.package_profiles": ["hls", "hls"]}, "duplicate entries: hls"),
        ({"channel_assembly.package_profiles": []}, "between 1 and 5 profiles are required, got 0"),
        ({"channel_assembly.package_profiles": ["hls"] * 6}, "between 1 and 5 profiles are required, got 6"),
        ({"channels.iac.0.name": "demo"}, "name must start with 'fast'- but not 'fast'-agent-"),
        ({"channels.iac.0.name": "fast-agent-demo"}, "name must start with 'fast'- but not 'fast'-agent-"),
        ({"channels.iac": [{"name": "fast-a"}, {"name": "fast-a"}]}, "duplicate entries: fast-a"),
        ({"channels.iac.0.ladder": "uhd"}, "ladder 'uhd' is not in ladders.builtin ['standard']"),
        ({"ladders.builtin": ["standard", "missing"]}, "unknown ladder 'missing'"),
        ({"ladders.builtin": ["standard", "uhd"]}, "unknown ladder 'uhd': there is no ladders/uhd.toml"),
        ({"captions": {"enabled": True}}, "fast-channels.captions: Extra inputs are not permitted"),
        ({"ladders.default": "uhd"}, "default ladder 'uhd' is not in builtin ['standard']"),
        ({"channels.iac.0.autopilot_cron": "every hour"}, "String should match pattern"),
        ({"delivery.content_domain": "media.example.com"}, "content_domain and certificate_arn must be set together"),
        (
            {
                "delivery.content_domain": "media.example.com",
                "delivery.certificate_arn": "arn:aws:acm:eu-west-1:123456789012:certificate/abc",
            },
            "fast-channels.delivery.certificate_arn: String should match pattern",
        ),
        ({"channels.iac.5.tier": "STANDARD"}, "'5' is not an index of a 1-item list"),
    ],
)
def test_invalid_configurations(load_config: Load, overrides: dict[str, Any], expected: str) -> None:
    assert expected in config_error(load_config, with_shipped(**overrides))


def test_channel_outputs_must_be_package_profiles(load_config: Load, monkeypatch: pytest.MonkeyPatch) -> None:
    # The catalogue has one profile today; with a second, a channel may only publish the registered ones.
    monkeypatch.setattr("fast_channels.infra.config.CATALOGUE", (*CATALOGUE, RenditionProfile(name="hls-alt")))

    message = config_error(load_config, with_shipped(**{"channels.iac.0.outputs": ["hls", "hls-alt"]}))
    config = load_config(
        with_shipped(**{"channel_assembly.package_profiles": ["hls", "hls-alt"], "channels.iac.0.outputs": ["hls-alt"]})
    )

    assert "channels.iac[fast-demo]: outputs ['hls-alt'] are not in channel_assembly.package_profiles" in message
    assert config.channels.iac[0].outputs == ("hls-alt",)


def test_external_vast_ads_play_on_the_hls_output(load_config: Load) -> None:
    ads = {"mode": "external_vast", "url": "https://ads.example.com/vast?sid=[session.uuid]"}

    config = load_config(with_shipped(ads=ads))

    assert config.ads == ExternalVastAds(mode=AdsMode.EXTERNAL_VAST, url=ads["url"])
    assert config.channels.iac[0].outputs == ("hls",)


def test_external_vast_needs_a_url(load_config: Load) -> None:
    assert "fast-channels.ads.external_vast.url: Field required" in config_error(
        load_config, with_shipped(ads={"mode": "external_vast"})
    )


@pytest.mark.parametrize(
    ("region", "overrides", "expected"),
    [
        ("us-west-1", {}, "region us-west-1: MediaTailor Channel Assembly and MediaConvert are not both available"),
        ("af-south-1", {}, "region af-south-1: Amazon Bedrock AgentCore is not available; set agent.enabled=false"),
        (
            "eu-west-1",
            {"agent.model": US_HAIKU, "agent.allowed_models": [US_HAIKU]},
            "'us.anthropic.claude-haiku-5-5' is a us. inference profile, which eu-west-1 cannot invoke; "
            "use eu.anthropic.claude-haiku-5-5 or global.anthropic.claude-haiku-5-5",
        ),
    ],
)
def test_region_checks(load_config: Load, region: str, overrides: dict[str, Any], expected: str) -> None:
    assert expected in config_error(load_config, with_shipped(**overrides), region=region)


def test_region_checks_follow_the_agent_plane(load_config: Load) -> None:
    assert not load_config(with_shipped(**{"agent.enabled": False}), region="af-south-1").agent.enabled
    eu = "eu.anthropic.claude-haiku-5-5"
    config = load_config(
        with_shipped(**{"agent.model": eu, "agent.allowed_models": [eu, "global.anthropic.claude-haiku-5-5"]}),
        region="eu-central-1",
    )

    assert config.agent.model == eu


@pytest.mark.parametrize(
    ("region", "geo"),
    [
        ("us-east-1", "us"),
        ("us-west-2", "us"),
        ("ca-central-1", "us"),  # a source region of the us. profiles
        ("eu-west-1", "eu"),
        ("eu-central-1", "eu"),
        ("ap-southeast-2", "au"),
        ("ap-northeast-1", "global"),  # jp. has no Sonnet 5.5
        ("ap-south-1", "global"),  # no apac. profile of the 5.5 models
        ("sa-east-1", "global"),
    ],
)
def test_shipped_models_resolve_to_the_regions_geography(load_config: Load, region: str, geo: str) -> None:
    config = load_config({"fast-channels": SHIPPED}, region=region)

    assert geo_prefix(region) == geo
    assert config.agent.model == f"{geo}.anthropic.claude-haiku-5-5"
    assert config.agent.allowed_models == tuple(
        dict.fromkeys(
            [
                f"{geo}.anthropic.claude-haiku-5-5",
                f"{geo}.anthropic.claude-sonnet-5-5",
                "global.anthropic.claude-haiku-5-5",
                "global.anthropic.claude-sonnet-5-5",
            ]
        )
    )


def test_geo_placeholder_waits_for_a_region(load_config: Load) -> None:
    unresolved = load_config({"fast-channels": SHIPPED})
    resolved = unresolved.for_region("eu-west-1")

    assert unresolved.agent.unresolved
    assert unresolved.agent.model == "{geo}.anthropic.claude-haiku-5-5"
    assert not resolved.agent.unresolved
    assert resolved.agent.model == "eu.anthropic.claude-haiku-5-5"
    assert FastChannelsConfig().for_region("us-east-1").agent.model == US_HAIKU


def test_for_region_reports_the_regions_problems(load_config: Load) -> None:
    config = load_config(with_shipped(**{"agent.model": US_HAIKU, "agent.allowed_models": [US_HAIKU]}))

    with pytest.raises(ConfigError, match="which eu-west-1 cannot invoke"):
        config.for_region("eu-west-1")


def test_duplicate_models_are_still_refused(load_config: Load) -> None:
    message = config_error(load_config, with_shipped(**{"agent.allowed_models": [US_HAIKU, US_HAIKU]}))

    assert "duplicate entries: us.anthropic.claude-haiku-5-5" in message


@pytest.mark.parametrize("url", ["http://localhost:5173/", "http://127.0.0.1:4173/"])
def test_console_dev_url_accepts_local_origins_only(load_config: Load, url: str) -> None:
    assert load_config(with_shipped(**{"agent.console_dev_url": url})).agent.console_dev_url == url
    assert "String should match pattern" in config_error(
        load_config, with_shipped(**{"agent.console_dev_url": "http://evil.example.com:5173/"})
    )


def test_console_dev_url_is_off_by_default(load_config: Load) -> None:
    assert load_config({"fast-channels": SHIPPED}).agent.console_dev_url is None


@pytest.mark.parametrize(
    ("overrides", "recorded"),
    [
        ({}, False),
        ({"observability.transaction_search": "enable"}, True),
        ({"observability.transaction_search": "existing", "observability.capture_prompts": False}, False),
        ({"observability.capture_prompts": True}, True),
    ],
)
def test_prompts_are_recorded_only_with_evaluations_or_on_request(
    load_config: Load, overrides: dict[str, Any], recorded: bool
) -> None:
    assert load_config(with_shipped(**overrides)).observability.records_prompts is recorded


def test_configuration_is_immutable(load_config: Load) -> None:
    config = load_config({"fast-channels": SHIPPED})

    with pytest.raises(ValueError, match="frozen"):
        config.prefix = "other"  # type: ignore[misc]


def test_json_schema_documents_every_field() -> None:
    schema = FastChannelsConfig.model_json_schema()

    assert "Name prefix of every resource" in schema["properties"]["prefix"]["description"]
    assert "description" in schema["$defs"]["ChannelConfig"]["properties"]["autopilot_cron"]


def test_deployments_encode_the_builtin_ladders(load_config: Load) -> None:
    config = load_config(with_shipped())

    assert config.ladder("standard") == Ladder.builtin("standard")
    assert config.ladder("standard").version == "standard@843ed1d05a"  # HLS MPEG-TS, AVC + AAC-LC muxed


MODULES = ("ops", "models", "demand", "pds", "release", "crawl")
RECS = "haiku-5-5-adaptive-8k"
"""The recommender's fixture eval record (``infra/tests/data/evals``)."""
RECOMMENDER: dict[str, Any] = {
    "model": "{geo}.anthropic.claude-haiku-5-5",
    "effort": "low",
    "max_tokens": 8000,
    "eval": RECS,
}
LINE: dict[str, Any] = {
    "id": "direct1",
    "advertiser": "Example Coffee Co.",
    "adomain": "coffee.example",
    "cat": ["IAB8"],
    "creatives": ["direct1"],
    "cpm": 25,
    "budget": 1,
    "caps": {"per_session": 2},
    "tier0": True,
}


def modules_on(*names: str) -> dict[str, Any]:
    return {f"modules.{name}.enabled": True for name in names}


def test_shipped_modules_are_off_and_at_their_defaults(load_config: Load) -> None:
    config = load_config({"fast-channels": SHIPPED})
    v3 = load_config({"fast-channels": {key: value for key, value in SHIPPED.items() if key != "modules"}})

    assert tuple(ModulesConfig.model_fields) == MODULES
    assert not any(config.modules.enabled(name) for name in MODULES)
    assert config.modules == ModulesConfig() == FastChannelsConfig().modules
    assert config == v3  # cdk.json's modules block spells out the defaults: it is v3.0.0's configuration


def test_shipped_model_registry_holds_the_scheduler_agent(load_config: Load) -> None:
    [(task, entry)] = load_config({"fast-channels": SHIPPED}, region="us-east-1").modules.models.tasks.items()

    assert task == "scheduler.plan"
    assert entry.model == US_HAIKU  # {geo}, resolved like agent.model
    assert entry.effort == ModeEffort(chat=Effort.LOW, plan=Effort.MEDIUM)
    assert entry.max_tokens == 16_000  # the agent's ModelSpec today


@pytest.mark.parametrize(
    ("module", "message", "closure"),
    [
        ("demand", "demand needs ops: enable it or turn demand off", ("ops",)),
        ("pds", "pds needs ops and demand: enable them or turn pds off", ("ops", "demand")),
        ("release", "release needs ops, demand and pds: enable them", ("ops", "demand", "pds")),
        ("crawl", "crawl needs pds and release: enable them", ("ops", "demand", "pds", "release")),
    ],
)
def test_modules_need_the_modules_they_build_on(
    load_config: Load, module: str, message: str, closure: tuple[str, ...]
) -> None:
    assert f"  - fast-channels.modules: {message}" in config_error(load_config, with_shipped(**modules_on(module)))
    assert load_config(with_shipped(**modules_on(module, *closure))).modules.enabled(module)


@pytest.mark.parametrize("module", ["ops", "models"])
def test_ops_and_models_stand_alone(load_config: Load, module: str) -> None:
    assert load_config(with_shipped(**modules_on(module)), region="us-east-1").modules.enabled(module)


def test_every_unmet_need_is_reported(load_config: Load) -> None:
    message = config_error(load_config, with_shipped(**modules_on("release", "crawl")))

    assert "release needs ops, demand and pds" in message
    assert "crawl needs pds: enable it or turn crawl off" in message


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"modules.ads": {"enabled": True}}, "fast-channels.modules.ads: Extra inputs are not permitted"),
        ({"modules.ops.typo": 1}, "fast-channels.modules.ops.typo: Extra inputs are not permitted"),
        (
            {"modules.release.channels": ["fast-news"]},
            "fast-channels.modules.release.channels: 'fast-news' is not an IaC channel (channels.iac: ['fast-demo'])",
        ),
        ({"modules.release.channels": ["{prefix}-demo", "fast-demo"]}, "duplicate entries: fast-demo"),
        (
            {
                **modules_on("ops", "demand", "pds", "release"),
                "channels.iac": [{"name": "fast-demo"}, {"name": "fast-lab"}],
            },
            "fast-channels.modules.release.lab_channel: 'fast-lab' is the release lab channel",
        ),
        (
            {**modules_on("ops"), "modules.ops.ledger_lock": "compliance"},
            "fast-channels.modules.ops.ledger_lock: compliance-mode records cannot be deleted",
        ),
        ({"modules.ops.ledger_retention_days": 0}, "greater than or equal to 1"),
        (
            {**modules_on("ops", "demand"), "prefix": "fastdemandxyz"},
            "fast-channels.modules.demand: the Prebid Server guidance deploys onto a CDK bootstrap qualified by the "
            "prefix, at most 10 characters; 'fastdemandxyz' is 13",
        ),
        (
            {
                **modules_on("models"),
                "modules.models.tasks": {"scheduler.plan": {"model": "us.anthropic.claude-opus-5-5"}},
            },
            "fast-channels.modules.models.tasks[scheduler.plan]: model 'us.anthropic.claude-opus-5-5' is not in "
            "agent.allowed_models",
        ),
        (
            {"modules.models.tasks": {"Scheduler": {"model": US_HAIKU}}},
            "task names must be dot-separated lowercase words, got ['Scheduler']",
        ),
        ({"modules.models.tasks": {"scheduler.plan": {"effort": "extreme"}}}, "fast-channels.modules.models.tasks"),
        (
            {**modules_on("models"), "modules.models.budgets_action": True},
            "fast-channels.modules.models.budgets_action: AWS Budgets emails notifications.email when an action runs",
        ),
        (
            {"modules.models.tasks": {"scheduler.plan": {"profile_arn": "arn:aws:bedrock:us-east-1:1:x"}}},
            "fast-channels.modules.models.tasks.scheduler.plan.profile_arn: Extra inputs are not permitted",
        ),
        (
            {
                "modules.models.tasks": {
                    "recommender": {"model": "global.anthropic.claude-haiku-5-5", "data_class": "viewer"}
                }
            },
            "fast-channels.modules.models.tasks.recommender: model 'global.anthropic.claude-haiku-5-5' is a global "
            "inference profile, which may process a request in any Region; a task with viewer data needs a geographic "
            "profile",
        ),
        ({"modules.demand.tasks": {"min": 5, "max": 4}}, "tasks.min (5) exceeds tasks.max (4)"),
        (
            {"modules.demand.dayparts": [{"cron": "cron(50 17 ? * MON-FRI *)", "min": 9}]},
            "dayparts ['cron(50 17 ? * MON-FRI *)']: min must lie between tasks.min and tasks.max",
        ),
        ({"modules.demand.dayparts": [{"cron": "rate(1 day)", "min": 3}]}, "String should match pattern"),
        ({"modules.demand.pbs_version": "4.3.0"}, "Input should be '3.43.0' or '4.4.0'"),
        ({"modules.demand.guidance_tag": "main"}, "String should match pattern"),
        (
            {"modules.pds.policy_group_timeout_ms": 40},
            "policy_group_timeout_ms (40) is shorter than policy_timeout_ms (50)",
        ),
        (
            {"modules.pds.select_timeout_ms": 250},
            "select_group_timeout_ms (200) is shorter than select_timeout_ms (250)",
        ),
        ({"modules.pds.demo_lines": [LINE, LINE]}, "duplicate entries: direct1"),
        ({"modules.pds.demo_lines": [{**LINE, "creatives": []}]}, "should have at least 1 item"),
        ({"modules.pds.demo_lines": [{**LINE, "budget": 0}]}, "greater than 0"),
        (
            {"modules.pds.demo_lines": [LINE, {**LINE, "id": "direct-2"}]},
            "a creative serves at most one line: duplicate entries: direct1",
        ),
        ({"modules.pds.demo_lines": [{**LINE, "creatives": ["sim1"]}]}, "not direct demo creatives: sim1"),
        ({"modules.pds.demo_lines": [{**LINE, "id": f"l{n}"} for n in range(9)]}, "at most 8 items"),
        ({"modules.crawl.holdout_percent": 101}, "less than or equal to 100"),
    ],
)
def test_invalid_module_configurations(load_config: Load, overrides: dict[str, Any], expected: str) -> None:
    assert expected in config_error(load_config, with_shipped(**overrides), region="us-east-1")


def test_module_rules_accept_a_consistent_configuration(load_config: Load) -> None:
    recommender = {"model": "{geo}.anthropic.claude-haiku-5-5", "effort": "low", "max_tokens": 8000, "eval": RECS}
    config = load_config(
        with_shipped(
            **modules_on(*MODULES),
            **{
                "removal": "retain",
                "modules.ops.ledger_lock": "compliance",
                "modules.models.tasks": {"recommender": {**recommender, "data_class": "viewer"}},
                "modules.demand.simulator": "lambda",
                "modules.demand.tasks": {"min": 3, "max": 3},
                "modules.demand.dayparts": [{"cron": "cron(50 17 ? * MON-FRI *)", "min": 3}],
                "modules.pds.policy_group_timeout_ms": 50,
                "modules.pds.demo_lines": [LINE],
                "modules.release.channels": ["{prefix}-demo"],
                "channels.iac": [{"name": "{prefix}-demo"}, {"name": "{prefix}-lab"}],
                "modules.release.lab_channel": False,
            },
        ),
        region="us-east-1",
    )

    modules = config.modules
    assert all(modules.enabled(name) for name in MODULES)
    assert modules.ops.ledger_lock is LedgerLock.COMPLIANCE
    assert set(modules.models.tasks) == {"scheduler.plan", "recommender"}  # merged into cdk.json's entries
    assert modules.models.tasks["recommender"].effort is Effort.LOW
    assert (modules.demand.simulator, modules.demand.pbs_version) == (Simulator.LAMBDA, PbsVersion.V3_43_0)
    assert modules.pds.demo_lines == (DemoLine.model_validate(LINE),)
    assert modules.release.channels == ("fast-demo",)


def test_module_rules_wait_for_the_module(load_config: Load) -> None:
    config = load_config(
        with_shipped(
            **{
                "modules.ops.ledger_lock": "compliance",
                "modules.models.tasks": {"scheduler.plan": {"model": "us.anthropic.claude-opus-5-5"}},
                "channels.iac": [{"name": "{prefix}-demo"}, {"name": "{prefix}-lab"}],
            }
        ),
        region="us-east-1",
    )

    assert not any(config.modules.enabled(name) for name in MODULES)


def test_release_channels_expand_the_prefix(load_config: Load) -> None:
    config = load_config(
        with_shipped(
            **{"prefix": "acme", "channels.iac.0.name": "{prefix}-news", "modules.release.channels": ["{prefix}-news"]}
        )
    )

    assert config.modules.release.channels == ("acme-news",)


@pytest.mark.parametrize(("region", "geo"), [("us-east-1", "us"), ("eu-west-1", "eu"), ("sa-east-1", "global")])
def test_task_models_resolve_to_the_regions_geography(load_config: Load, region: str, geo: str) -> None:
    config = load_config(with_shipped(**modules_on("models")), region=region)

    assert config.modules.models.tasks["scheduler.plan"].model == f"{geo}.anthropic.claude-haiku-5-5"


def test_task_models_resolve_when_the_registry_keeps_its_defaults(load_config: Load) -> None:
    config = load_config({"fast-channels": {"modules": {"models": {"enabled": True}}}}, region="eu-west-1")

    assert config.modules.models.tasks["scheduler.plan"].model == "eu.anthropic.claude-haiku-5-5"


def test_viewer_data_tasks_refuse_a_region_whose_geography_is_global(load_config: Load) -> None:
    recommender = {**RECOMMENDER, "data_class": "viewer"}
    viewer = {
        "fast-channels": {
            "agent": {"enabled": False},
            "modules": {"models": {"enabled": True, "tasks": {"recommender": recommender}}},
        }
    }
    operational = {"fast-channels": {"modules": {"models": {"enabled": True}}}}

    message = config_error(load_config, viewer, region="sa-east-1")

    assert "model 'global.anthropic.claude-haiku-5-5' is a global inference profile" in message
    assert load_config(viewer, region="us-east-1").modules.models.tasks["recommender"].model == US_HAIKU
    assert load_config(operational, region="sa-east-1").modules.models.tasks["scheduler.plan"].model == (
        "global.anthropic.claude-haiku-5-5"
    )


def test_the_agent_needs_its_registry_entry(load_config: Load) -> None:
    tasks = {"recommender": RECOMMENDER}
    registry = {"modules": {"models": {"enabled": True, "tasks": tasks}}}

    message = config_error(load_config, {"fast-channels": registry}, region="us-east-1")

    assert "fast-channels.modules.models.tasks: 'scheduler.plan', the scheduler agent's entry, is missing" in message
    assert load_config({"fast-channels": {**registry, "agent": {"enabled": False}}}, region="us-east-1")


def test_region_checks_cover_the_registry_without_the_agent(load_config: Load) -> None:
    us_only = {"agent.enabled": False, "agent.model": US_HAIKU, "agent.allowed_models": [US_HAIKU]}
    task = {"modules.models.tasks": {"scheduler.plan": {"model": US_HAIKU}}}

    message = config_error(load_config, with_shipped(**us_only, **task, **modules_on("models")), region="eu-west-1")

    assert "'us.anthropic.claude-haiku-5-5' is a us. inference profile, which eu-west-1 cannot invoke" in message
    assert not load_config(with_shipped(**us_only, **task), region="eu-west-1").modules.models.enabled


def test_json_schema_documents_every_module_field() -> None:
    definitions = FastChannelsConfig.model_json_schema()["$defs"]
    sections = ["ModulesConfig", *(f"{name.capitalize()}Module" for name in MODULES)]

    for name in [*sections, "ModelTask", "ModeEffort", "DemandTasks", "DemandDaypart", "DemoLine", "LineCaps"]:
        for field, schema in definitions[name]["properties"].items():
            section = definitions.get(schema.get("$ref", "").removeprefix("#/$defs/"), {})
            assert "description" in schema or "properties" in section, f"{name}.{field} has no documentation"


def test_release_strategies_name_the_core_rollouts() -> None:
    """``modules.release.strategy`` selects one of the bundle's AppConfig strategies (DESIGN §8.4)."""
    assert [s.value for s in ReleaseStrategy] == [t.value for t in RolloutTier]
    assert {RolloutTier(s.value) for s in ReleaseStrategy} == set(ROLLOUTS)


MODELS_ON = {"fast-channels": {"modules": {"models": {"enabled": True}}}}
SCHEDULER = {"model": "{geo}.anthropic.claude-haiku-5-5", "eval": "haiku-5-5-adaptive-16k"}


def test_the_shipped_scheduler_entry_has_its_committed_eval_record(load_config: Load) -> None:
    entry = load_config(MODELS_ON, region="us-east-1").modules.models.tasks["scheduler.plan"]

    assert entry.eval == "haiku-5-5-adaptive-16k"


def test_removing_the_record_fails_synth_with_a_clear_message(
    load_config: Load, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(EVAL_RECORDS_ENV, str(tmp_path))  # a records directory without the record

    message = config_error(load_config, MODELS_ON, region="us-east-1")

    assert (
        "fast-channels.modules.models.tasks[scheduler.plan]: eval record 'haiku-5-5-adaptive-16k' does not exist "
        f"({tmp_path}/scheduler.plan/haiku-5-5-adaptive-16k.json); run make eval TASK=scheduler.plan MODEL=<model>"
    ) in message
    assert not load_config({"fast-channels": {}}, region="us-east-1").modules.models.enabled  # off: no gate


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            {"model": "{geo}.anthropic.claude-sonnet-5-5"},
            "eval record 'haiku-5-5-adaptive-16k' does not qualify it: it tested anthropic.claude-haiku-5-5, not "
            "anthropic.claude-sonnet-5-5",
        ),
        (
            {"model": "{geo}.anthropic.claude-sonnet-5-5", "eval": "sonnet-5-5-adaptive-16k"},
            "eval record 'sonnet-5-5-adaptive-16k' does not exist",
        ),
        (
            {"max_tokens": 8000},
            "it ran with effort chat low / plan medium, thinking adaptive and max_tokens 16000, the entry has chat "
            "low / plan medium, adaptive and 8000",
        ),
        ({"eval": None}, "names no eval record; run make eval TASK=scheduler.plan MODEL=<model> and set eval"),
    ],
)
def test_a_model_or_setting_without_its_own_record_fails_synth(
    load_config: Load, entry: dict[str, Any], expected: str
) -> None:
    raw = {
        "modules": {
            "models": {
                "enabled": True,
                "tasks": {"scheduler.plan": {**SCHEDULER, **entry}},
            }
        }
    }

    assert expected in config_error(load_config, {"fast-channels": raw}, region="us-east-1")


def test_any_routing_of_the_tested_model_qualifies(load_config: Load) -> None:
    for region in ("eu-west-1", "sa-east-1"):  # eu. and global. profiles of the us.-tested Haiku 5.5
        assert load_config(MODELS_ON, region=region).modules.models.tasks["scheduler.plan"].eval
