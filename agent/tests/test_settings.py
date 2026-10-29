import pytest

from fast_channels.agent.settings import FALLBACK_MODEL_ID, Settings, SettingsError

BASE = {"AWS_REGION": "us-east-1", "GATEWAY_URL": "https://g.example/mcp"}
PROFILE = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"


def test_v3_environment() -> None:
    settings = Settings.from_env({**BASE, "ALLOWED_MODELS": "us.anthropic.claude-sonnet-5-5, "})

    assert settings.allowed_models == {FALLBACK_MODEL_ID, "us.anthropic.claude-sonnet-5-5"}
    assert (settings.model_task_parameter, settings.model_profile_arn, settings.model_task_default) == (
        None,
        None,
        None,
    )
    assert (settings.model_counter_table, settings.alert_topic_arn) == (None, None)


def test_model_registry_environment() -> None:
    settings = Settings.from_env(
        {
            **BASE,
            "MODEL_TASK_PARAMETER": "/fast/models/tasks/scheduler.plan",
            "MODEL_PROFILE_ARN": PROFILE,
            "MODEL_COUNTER_TABLE": "fast-models",
            "MODEL_TASK_DEFAULT": '{"model": "us.anthropic.claude-haiku-5-5"}',
            "ALERT_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:fast-alerts",
        }
    )

    assert settings.model_task_parameter == "/fast/models/tasks/scheduler.plan"
    assert (settings.model_profile_arn, settings.model_counter_table) == (PROFILE, "fast-models")
    assert settings.model_task_default == '{"model": "us.anthropic.claude-haiku-5-5"}'
    assert settings.alert_topic_arn == "arn:aws:sns:us-east-1:123456789012:fast-alerts"


def test_region_and_gateway_are_required() -> None:
    with pytest.raises(SettingsError, match="AWS_REGION and GATEWAY_URL"):
        Settings.from_env({"AWS_REGION": "us-east-1"})
