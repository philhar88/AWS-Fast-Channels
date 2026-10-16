import pytest

from fast_channels.functions.settings import Settings


def test_require_names_the_missing_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIBRARY_TABLE", "library")
    monkeypatch.delenv("MEDIA_BUCKET", raising=False)

    Settings().require("library_table")
    with pytest.raises(RuntimeError, match="missing environment configuration: MEDIA_BUCKET"):
        Settings().require("library_table", "media_bucket")


def test_require_rejects_names_that_are_not_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_BUCKET", "media")

    with pytest.raises(ValueError, match=r"unknown settings \['media_buckt'\]"):
        Settings().require("media_bucket", "media_buckt")  # a typo used to pass silently


@pytest.mark.parametrize(
    ("variables", "expected"),
    [
        ({"CDN_BASE_URL": "https://d1.cloudfront.net/"}, "https://d1.cloudfront.net"),
        ({"CDN_DOMAIN": "https://d2.cloudfront.net"}, "https://d2.cloudfront.net"),  # the former name
        (
            {"CDN_BASE_URL": "https://d1.cloudfront.net", "CDN_DOMAIN": "https://d2.cloudfront.net"},
            "https://d1.cloudfront.net",
        ),
        ({}, ""),
    ],
)
def test_cdn_base_url(monkeypatch: pytest.MonkeyPatch, variables: dict[str, str], expected: str) -> None:
    for name in ("CDN_BASE_URL", "CDN_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)

    assert Settings().cdn_base_url == expected


def test_agent_prefix_is_the_agent_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAST_PREFIX", "fast")

    assert Settings().agent_prefix == "fast-agent-"
