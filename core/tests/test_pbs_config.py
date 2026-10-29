"""pbs_config: the Prebid Server configuration of a stage (stored requests, account, application configuration).

The rendered files of a sample stage are golden snapshots (``__snapshots__/test_pbs_config/``): any change to what
Prebid Server loads shows up as a reviewed diff.
"""

import json
import re
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from pydantic import ValidationError
from syrupy.extensions.json import JSONSnapshotExtension
from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode

from fast_channels.core.pbs_config import (
    OWN_BOOK,
    POD_SECONDS,
    POLICY_HOOK,
    SHIPPED_ADAPTERS,
    SIMULATOR,
    VAST_HOOK,
    PbsSettings,
    account,
    pds_host,
    prebid_config,
    private_zone,
    render,
    stored_request,
    stored_request_id,
    to_yaml,
    tracker_host,
)

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

CDN = "https://d111111abcdef8.cloudfront.net"
SETTINGS = PbsSettings(
    prefix="fastm",
    simulator=True,
    server_instances=2,
    tracker_base_url=CDN,
    publisher_domain="d111111abcdef8.cloudfront.net",
)
SPRING_PLACEHOLDERS = {
    "${SSL_KEYSTORE_PATH}",
    "${SSL_KEYSTORE_PASS}",
    "${CACHE_HOST}",
    "${SETTINGS_S3_ENDPOINT}",
    "${SETTINGS_S3_BUCKET}",
    "${SETTINGS_S3_REGION}",
    "${LOG_ANALYTICS_ENABLED}",
}
"""Environment variables the guidance's task definition and entrypoint set, as its default configuration uses them."""


class YamlSnapshot(SingleFileSnapshotExtension):
    file_extension = "yaml"
    _write_mode = WriteMode.TEXT


def test_rendered_files() -> None:
    assert sorted(render(SETTINGS)) == sorted(
        [
            "current-config/prebid-config.yaml",
            *(f"stored_requests/fast-pod-{seconds}s.json" for seconds in POD_SECONDS),
            *(f"stored_requests/fast-pod-kids-{seconds}s.json" for seconds in POD_SECONDS),
            "stored_accounts/fastm.json",
        ]
    )


@pytest.mark.parametrize("path", sorted(render(SETTINGS)))
def test_golden_files(path: str, snapshot: SnapshotAssertion) -> None:
    content = render(SETTINGS)[path]
    if path.endswith(".json"):
        assert json.loads(content) == snapshot.use_extension(JSONSnapshotExtension)
    else:
        assert content == snapshot.use_extension(YamlSnapshot)


@pytest.mark.parametrize("seconds", POD_SECONDS)
def test_stored_request_is_one_pod_of_30_s_slots_for_the_own_book_only(seconds: int) -> None:
    request = stored_request(SETTINGS, seconds)
    slots = request["imp"]

    assert request["id"] == f"fast-pod-{seconds}s"
    assert len(slots) == seconds // 30
    assert [slot["video"]["poddur"] for slot in slots] == [seconds] * len(slots)
    assert {slot["video"]["podid"] for slot in slots} == {1}
    assert all(slot["video"]["rqddurs"] == [30] for slot in slots)
    assert [slot["ext"]["prebid"]["bidder"] for slot in slots] == [{OWN_BOOK: {}}] * len(slots)
    assert all(slot["ssai"] == 3 for slot in slots)
    assert "device" not in request
    assert "user" not in request
    assert request["tmax"] == 500
    assert request["regs"] == {"coppa": 0}
    assert request["app"]["publisher"]["id"] == "fastm"
    assert request["app"]["content"] == {"livestream": 1, "context": 1, "cattax": 9}
    assert request["source"]["schain"]["nodes"] == [{"asi": "d111111abcdef8.cloudfront.net", "sid": "fastm", "hp": 1}]


@pytest.mark.parametrize(("seconds", "positions"), [(30, [0]), (60, [1, -1]), (90, [1, 0, -1]), (120, [1, 0, 0, -1])])
def test_slot_in_pod_marks_the_first_and_last_slot(seconds: int, positions: list[int]) -> None:
    assert [slot["video"]["slotinpod"] for slot in stored_request(SETTINGS, seconds)["imp"]] == positions


def test_kids_stored_requests_set_coppa() -> None:
    for seconds in POD_SECONDS:
        kids = stored_request(SETTINGS, seconds, kids=True)
        assert kids["id"] == f"fast-pod-kids-{seconds}s"
        assert kids["regs"] == {"coppa": 1}
        assert kids["imp"] == stored_request(SETTINGS, seconds)["imp"]


@pytest.mark.parametrize("seconds", [0, 31, 300, 150])
def test_off_grid_breaks_have_no_stored_request(seconds: int) -> None:
    with pytest.raises(ValueError, match="no stored request"):
        stored_request(SETTINGS, seconds)


def test_stored_request_ids() -> None:
    assert stored_request_id(60) == "fast-pod-60s"
    assert stored_request_id(60, kids=True) == "fast-pod-kids-60s"


def test_account_runs_both_hooks_inside_their_group_timeouts() -> None:
    settings = SETTINGS.model_copy(update={"policy_group_timeout_ms": 200, "select_group_timeout_ms": 250})
    stages = account(settings)["hooks"]["execution-plan"]["endpoints"]["/openrtb2/auction"]["stages"]

    assert stages["processed-auction-request"]["groups"] == [
        {"timeout": 200, "hook-sequence": [{"module-code": "fast-ctv", "hook-impl-code": POLICY_HOOK}]}
    ]
    assert stages["exitpoint"]["groups"] == [
        {"timeout": 250, "hook-sequence": [{"module-code": "fast-ctv", "hook-impl-code": VAST_HOOK}]}
    ]


def test_account_keeps_ids_user_data_and_precise_geo_from_every_bidder_but_the_own_book() -> None:
    activities = account(SETTINGS)["privacy"]["allowactivities"]

    assert set(activities) == {"transmitEids", "transmitUfpd", "transmitPreciseGeo"}
    for activity in activities.values():
        assert activity["default"] is False
        assert activity["rules"] == [
            {"condition": {"component_type": ["BIDDER"], "component_name": [OWN_BOOK]}, "allow": True}
        ]


def test_account_is_the_stage_and_never_allows_debug() -> None:
    stage = account(SETTINGS)
    assert (stage["id"], stage["status"], stage["auction"]) == ("fastm", "active", {"debug-allow": False})


def test_shipped_adapters_are_off_and_the_simulator_follows_the_mode() -> None:
    for simulator in (True, False):
        adapters = prebid_config(SETTINGS.model_copy(update={"simulator": simulator}))["adapters"]
        assert all(adapters[name] == {"enabled": False} for name in SHIPPED_ADAPTERS)
        assert adapters[SIMULATOR] == {"enabled": simulator}


def test_the_own_book_and_the_module_call_the_pds_on_its_stable_private_name() -> None:
    config = prebid_config(SETTINGS)
    module = config["hooks"]["fast-ctv"]

    assert private_zone("fastm") == "fastm.fast.internal"
    assert pds_host("fastm") == "pds.fastm.fast.internal"
    assert config["adapters"]["generic"]["aliases"][OWN_BOOK] == {
        "enabled": True,
        "endpoint": "http://pds.fastm.fast.internal/ownbook/openrtb",
        "ortb-version": "ORTB_2_6",
    }
    assert module == {
        "enabled": True,
        "policy-url": "http://pds.fastm.fast.internal/policy",
        "policy-timeout-ms": 50,
        "select-url": "http://pds.fastm.fast.internal/select",
        "select-timeout-ms": 100,
        "tracker-template": f"{CDN}/ads/b.gif?d={{sid}}.{{evid}}&c={{crid}}&e={{event}}&enr={{enr}}",
        "log-requests": True,
    }


def test_one_event_loop_per_vcpu_and_the_guidance_cache_defaults() -> None:
    config = prebid_config(SETTINGS.model_copy(update={"server_instances": 4}))

    assert config["server"]["http"] == {"enabled": True, "port": 8443, "server-instances": 4}
    assert config["settings"]["in-memory-cache"] == {
        "cache-size": 10000,
        "ttl-seconds": 1200,
        "s3-update": {"refresh-rate": 900000, "timeout": 30000},
    }
    assert config["admin-endpoints"] == {"logging-changelevel": {"enabled": False}}
    assert config["gdpr"]["default-value"] == 0


def test_prebid_config_yaml_reads_back_as_the_data() -> None:
    text = render(SETTINGS)["current-config/prebid-config.yaml"]

    assert text.startswith("# Prebid Server configuration of stage fastm")
    assert yaml.safe_load(text) == prebid_config(SETTINGS)


def test_nothing_rendered_is_secret() -> None:
    """The origin secret lives in the WAF rule group: the configuration has no input for it, and the only
    placeholders are the guidance's own environment variables."""
    text = "".join(render(SETTINGS).values())

    assert not re.search(r"(?i)x-fast-origin|secret|password|token", text.replace("jks-password", ""))
    assert set(re.findall(r"\$\{[A-Z0-9_]+\}", text)) == SPRING_PLACEHOLDERS


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"a": True, "b": None, "c": 3, "d": "x"}, 'a: true\nb: null\nc: 3\nd: "x"\n'),
        ({"a": {"b": {"c": "1"}}}, 'a:\n  b:\n    c: "1"\n'),
        ({"a": [1, "two"]}, 'a:\n  - 1\n  - "two"\n'),
        ({"a": [{"b": 1, "c": [2]}, {"d": {}}]}, "a:\n  - b: 1\n    c:\n      - 2\n  - d: {}\n"),
        ({"a": {}, "b": []}, "a: {}\nb: []\n"),
        ({"key with space": 'quote " and \\'}, '"key with space": "quote \\" and \\\\"\n'),
        ({"/openrtb2/auction": "ü"}, '/openrtb2/auction: "ü"\n'),
    ],
)
def test_to_yaml(data: dict[str, Any], expected: str) -> None:
    assert to_yaml(data) == expected
    assert yaml.safe_load(expected) == data


def test_to_yaml_refuses_other_types() -> None:
    with pytest.raises(TypeError, match="float"):
        to_yaml({"a": 1.5})


def test_tracker_host() -> None:
    assert tracker_host("https://d1.cloudfront.net/") == "d1.cloudfront.net"
    with pytest.raises(ValueError, match="host"):
        tracker_host("not a url")


@pytest.mark.parametrize(
    "change",
    [
        {"prefix": "Fast"},
        {"server_instances": 0},
        {"tracker_base_url": "http://d1.cloudfront.net"},
        {"tracker_base_url": "https://d1.cloudfront.net/"},
        {"publisher_domain": "not a domain"},
        {"unknown": 1},
    ],
)
def test_settings_are_validated(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PbsSettings.model_validate(SETTINGS.model_dump() | change)
