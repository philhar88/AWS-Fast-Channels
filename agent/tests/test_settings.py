from decimal import Decimal

import pytest

from fast_channels.agent.settings import FALLBACK_MODEL_ID, Settings, SettingsError

BASE = {"AWS_REGION": "us-east-1", "GATEWAY_URL": "https://g.example/mcp"}
PROFILE = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"


def test_v3_environment() -> None:
    settings = Settings.from_env({**BASE, "ALLOWED_MODELS": "us.anthropic.claude-sonnet-5-5, "})

    assert settings.allowed_models == {FALLBACK_MODEL_ID, "us.anthropic.claude-sonnet-5-5"}
    assert (settings.model_task_parameter, settings.model_profile_arn, settings.daily_cap_usd) == (None, None, None)
    assert (settings.model_counter_table, settings.alert_topic_arn) == (None, None)


def test_model_registry_environment() -> None:
    settings = Settings.from_env(
        {
            **BASE,
            "MODEL_TASK_PARAMETER": "/fast/models/tasks/scheduler.plan",
            "MODEL_PROFILE_ARN": PROFILE,
            "MODEL_COUNTER_TABLE": "fast-models",
            "MODEL_DAILY_CAP_USD": "5.0",
            "ALERT_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:fast-alerts",
        }
    )

    assert settings.model_task_parameter == "/fast/models/tasks/scheduler.plan"
    assert (settings.model_profile_arn, settings.model_counter_table) == (PROFILE, "fast-models")
    assert settings.daily_cap_usd == Decimal(5)
    assert settings.alert_topic_arn == "arn:aws:sns:us-east-1:123456789012:fast-alerts"


@pytest.mark.parametrize("cap", ["0", "-1", "five", "NaN", "Infinity"])
def test_an_unusable_cap_stops_the_agent(cap: str) -> None:
    with pytest.raises(SettingsError, match="MODEL_DAILY_CAP_USD must be a positive number"):
        Settings.from_env({**BASE, "MODEL_DAILY_CAP_USD": cap})


def test_region_and_gateway_are_required() -> None:
    with pytest.raises(SettingsError, match="AWS_REGION and GATEWAY_URL"):
        Settings.from_env({"AWS_REGION": "us-east-1"})
