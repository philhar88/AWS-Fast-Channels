import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import RemovalPolicy
from aws_cdk import aws_logs as logs

from fast_channels.core.profiles import DEFAULT_PACKAGE_PROFILES
from fast_channels.infra.config import (
    AdsMode,
    ChannelTier,
    ConfigError,
    ExternalVastAds,
    FastChannelsConfig,
    PreconditionedAds,
    Removal,
    TransactionSearchMode,
)

type Load = Callable[..., FastChannelsConfig]

SHIPPED: dict[str, Any] = json.loads((Path(__file__).resolve().parents[2] / "cdk.json").read_text())["context"][
    "fast-channels"
]


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
    assert config.channel_assembly.package_profiles == DEFAULT_PACKAGE_PROFILES
    [channel] = config.channels.iac
    assert (channel.name, channel.ladder, channel.tier) == ("fast-demo", "standard", ChannelTier.BASIC)
    assert channel.outputs == DEFAULT_PACKAGE_PROFILES
    assert config.ads == PreconditionedAds()
    assert config.agent.enabled
    assert config.agent.model == "us.anthropic.claude-haiku-5-5"
    assert config.agent.allowed_models == (
        "us.anthropic.claude-haiku-5-5",
        "us.anthropic.claude-sonnet-5-5",
        "global.anthropic.claude-haiku-5-5",
        "global.anthropic.claude-sonnet-5-5",
    )
    assert config.observability.transaction_search is TransactionSearchMode.OFF


def test_shipped_configuration_spells_out_every_field() -> None:
    def keys(model: type[Any], data: dict[str, Any], path: str) -> None:
        assert set(data) == set(model.model_fields), path
        for name, value in data.items():
            annotation = model.model_fields[name].annotation
            if isinstance(value, dict) and isinstance(annotation, type):
                keys(annotation, value, f"{path}.{name}")

    keys(FastChannelsConfig, SHIPPED, "fast-channels")


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
                "agent": '{"guardrail_checks": true}',
                "channels.iac.0.brief": {"ratings": ["TV-G"]},
            }
        )
    )

    [channel] = config.channels.iac
    assert channel.tier is ChannelTier.STANDARD
    assert channel.brief is not None and channel.brief.ratings == ["TV-G"]
    assert config.agent.guardrail_checks and config.agent.enabled  # merged, not replaced


def test_prefix_override_needs_matching_channel_names(load_config: Load) -> None:
    config = load_config(with_shipped(**{"prefix": "acme", "channels.iac.0.name": "acme-news"}))

    assert (config.prefix, config.channels.iac[0].name) == ("acme", "acme-news")


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
    assert "  - fast-channels.prefix: String should match pattern" in message
    assert "  - fast-channels.agent: model 'anthropic.unknown' is not in allowed_models" in message
    assert "  - fast-channels.observability.log_retention_days: CloudWatch Logs supports retention of" in message
    assert "  - fast-channels.typo: Extra inputs are not permitted" in message


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"channels.iac.0.outputs": ["hls", "dash-hevc"]},
            "channels.iac[fast-demo]: outputs ['dash-hevc'] are not in channel_assembly.package_profiles",
        ),
        ({"channels.iac.0.outputs": ["hls-4k"]}, "unknown rendition profiles ['hls-4k']"),
        (
            {"channel_assembly.package_profiles": ["hls", "hls-avc", "hls-hevc", "hls-av1", "dash-avc", "dash-av1"]},
            "between 1 and 5 profiles are required, got 6",
        ),
        (
            {"channel_assembly.package_profiles": ["hls", "hls-uhd"], "channels.iac": []},
            "'hls-uhd' selects no rendition of ladder 'standard'",
        ),
        ({"channels.iac.0.name": "demo"}, "name must start with 'fast'- but not 'fast'-agent-"),
        ({"channels.iac.0.name": "fast-agent-demo"}, "name must start with 'fast'- but not 'fast'-agent-"),
        ({"channels.iac": [{"name": "fast-a"}, {"name": "fast-a"}]}, "duplicate entries: fast-a"),
        ({"channels.iac.0.ladder": "uhd"}, "ladder 'uhd' is not in ladders.builtin ['standard']"),
        ({"ladders.builtin": ["standard", "missing"]}, "unknown ladder 'missing'"),
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


def test_ladders_and_outputs_must_match(load_config: Load) -> None:
    config = load_config(
        with_shipped(
            **{
                "ladders.builtin": ["standard", "uhd"],
                "channels.iac": [{"name": "fast-uhd", "ladder": "uhd", "outputs": ["hls", "dash-av1"]}],
                "channel_assembly.package_profiles": ["hls", "dash-av1"],
            }
        )
    )

    assert config.channels.iac[0].ladder == "uhd"


def test_external_vast_requires_single_codec_outputs(load_config: Load) -> None:
    ads = {"mode": "external_vast", "url": "https://ads.example.com/vast?sid=[session.uuid]"}

    message = config_error(load_config, with_shipped(ads=ads))
    config = load_config(with_shipped(ads=ads, **{"channels.iac.0.outputs": ["hls-avc", "dash-avc"]}))

    assert "outputs ['hls', 'hls-avc', 'hls-hevc', 'hls-av1'] mix codecs" not in message
    assert "outputs ['hls'] mix codecs" in message
    assert config.ads == ExternalVastAds(mode=AdsMode.EXTERNAL_VAST, url=ads["url"])


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
            {},
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


def test_configuration_is_immutable(load_config: Load) -> None:
    config = load_config({"fast-channels": SHIPPED})

    with pytest.raises(ValueError, match="frozen"):
        config.prefix = "other"  # type: ignore[misc]


def test_json_schema_documents_every_field() -> None:
    schema = FastChannelsConfig.model_json_schema()

    assert "Name prefix of every resource" in schema["properties"]["prefix"]["description"]
    assert "description" in schema["$defs"]["ChannelConfig"]["properties"]["autopilot_cron"]
