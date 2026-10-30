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

from fast_channels.core.demo_demand import vast_ad
from fast_channels.core.monetization.playback import POD_SECONDS_FIELD, STORED_REQUEST, ads_body
from fast_channels.core.monetization.validators import MISSING_DURATION_SECONDS
from fast_channels.core.pbs_config import (
    OWN_BOOK,
    POD_MAX_SECONDS,
    POD_MIN_SECONDS,
    POD_SECONDS,
    POLICY_HOOK,
    SHIPPED_ADAPTERS,
    SIMULATOR,
    VAST_HOOK,
    HouseAd,
    PbsSettings,
    account,
    auction_body,
    house_ads,
    prebid_config,
    private_zone,
    render,
    stored_request,
    stored_request_id,
    stored_request_ids,
    to_yaml,
    tracker_host,
)
from fast_channels.core.pds.book import AD_SYSTEM, Creative

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
            "stored_requests/fast-pod.json",
            "stored_requests/fast-pod-kids.json",
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


def test_the_two_settings_prebid_server_4_requires() -> None:
    """Prebid Server Java 4.x refuses to start without them (P0-20 §4.13)."""
    config = prebid_config(SETTINGS)
    assert config["adapters"]["generic"]["aliases"][OWN_BOOK]["meta-info"] == {"vendor-id": 0}
    assert config["gdpr"]["vendorlist"]["live"] == {"startup-cache-dir": "/var/tmp/vendor-live"}


def test_the_stored_request_is_a_template_pod_of_one_30_s_slot_for_the_own_book_only() -> None:
    """The module copies the slot once per whole ``ext.fast.slot_s`` of the break (``ext.fast.pod_s``)."""
    request = stored_request(SETTINGS)
    [slot] = request["imp"]

    assert request["id"] == "fast-pod"
    assert slot["id"] == "s1"
    video = slot["video"]
    assert (video["podid"], video["poddur"], video["rqddurs"], video["slotinpod"]) == (1, 30, [30], 0)
    assert slot["ext"]["prebid"]["bidder"] == {OWN_BOOK: {}}
    assert slot["ssai"] == 3
    assert "device" not in request
    assert "user" not in request
    assert request["tmax"] == 500
    assert request["regs"] == {"coppa": 0}
    assert request["app"]["publisher"]["id"] == "fastm"
    assert request["app"]["content"] == {"livestream": 1, "context": 1, "cattax": 9}
    assert request["source"]["schain"]["nodes"] == [{"asi": "d111111abcdef8.cloudfront.net", "sid": "fastm", "hp": 1}]
    assert request["ext"] == {
        "prebid": {"multibid": [{"bidder": OWN_BOOK, "maxbids": 9}]},  # house ads and lines
        "fast": {"slot_s": 30},
    }


def test_the_kids_stored_request_sets_coppa() -> None:
    kids = stored_request(SETTINGS, kids=True)
    assert kids["id"] == "fast-pod-kids"
    assert kids["regs"] == {"coppa": 1}
    assert kids["imp"] == stored_request(SETTINGS)["imp"]


def test_stored_request_ids() -> None:
    assert (stored_request_id(), stored_request_id(kids=True)) == ("fast-pod", "fast-pod-kids")
    assert stored_request_ids() == ("fast-pod", "fast-pod-kids")
    assert stored_request_ids(ad_server=True) == ("fast-pod", "fast-pod-kids", "fast-adsrv-pod", "fast-adsrv-pod-kids")
    assert {kids: stored_request_id(kids=kids) for kids in (False, True)} == STORED_REQUEST, "the siblings' bodies"


def test_the_module_shapes_every_grid_length_and_no_missing_one() -> None:
    """``pod-min-s`` and ``pod-max-s`` cover the grid; 0 s and MediaTailor's 300 s for a missing duration are outside,
    so the module answers them with an empty pod and the slate plays."""
    module = prebid_config(SETTINGS)["hooks"]["fast-ctv"]
    assert (module["pod-min-s"], module["pod-max-s"]) == (POD_MIN_SECONDS, POD_MAX_SECONDS) == (30, 120)
    assert all(POD_MIN_SECONDS <= seconds <= POD_MAX_SECONDS for seconds in POD_SECONDS)
    assert not POD_MIN_SECONDS <= MISSING_DURATION_SECONDS <= POD_MAX_SECONDS
    assert POD_MIN_SECONDS > 0


@pytest.mark.parametrize(("kids", "ad_server"), [(False, False), (True, False), (False, True), (True, True)])
def test_the_auction_body_is_the_siblings_rendered(kids: bool, ad_server: bool) -> None:
    body = auction_body(48, kids=kids, ad_server=ad_server)
    assert body == {
        "ext": {
            "prebid": {"storedrequest": {"id": stored_request_id(kids=kids, ad_server=ad_server)}},
            "fast": {"pod_s": "48"},
        }
    }
    if not ad_server:
        assert json.loads(ads_body(kids=kids).replace("{{session.avail_duration_secs}}", "48")) == body
    assert POD_SECONDS_FIELD == "pod_s"


def test_account_runs_both_hooks_inside_their_group_timeouts() -> None:
    settings = SETTINGS.model_copy(update={"policy_group_timeout_ms": 200, "select_group_timeout_ms": 300})
    stages = account(settings)["hooks"]["execution-plan"]["endpoints"]["/openrtb2/auction"]["stages"]

    assert stages["processed-auction-request"]["groups"] == [
        {"timeout": 200, "hook-sequence": [{"module-code": "fast-ctv", "hook-impl-code": POLICY_HOOK}]}
    ]
    assert stages["exitpoint"]["groups"] == [
        {"timeout": 300, "hook-sequence": [{"module-code": "fast-ctv", "hook-impl-code": VAST_HOOK}]}
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


def test_the_own_book_and_the_module_call_the_pds_sidecar_on_the_tasks_loopback() -> None:
    """ADR-0036: the PDS is a container of the same task, on 127.0.0.1:8090."""
    config = prebid_config(SETTINGS)
    module = config["hooks"]["fast-ctv"]

    assert private_zone("fastm") == "fastm.fast.internal"
    assert config["adapters"]["generic"]["aliases"][OWN_BOOK] == {
        "enabled": True,
        "endpoint": "http://127.0.0.1:8090/ownbook/openrtb",
        "ortb-version": "ORTB_2_6",
        "meta-info": {"vendor-id": 0},
    }
    assert module == {
        "enabled": True,
        "policy-url": "http://127.0.0.1:8090/policy",
        "policy-timeout-ms": 50,
        "select-url": "http://127.0.0.1:8090/select",
        "select-timeout-ms": 200,
        "tracker-template": f"{CDN}/ads/b.gif?d={{sid}}.{{evid}}&c={{crid}}&e={{event}}&enr={{enr}}",
        "house-ads": "[]",
        "pod-min-s": 30,
        "pod-max-s": 120,
        "log-requests": True,
    }


def test_the_kits_house_ads_are_the_modules_pod_of_last_resort() -> None:
    """``hooks.fast-ctv.house-ads``: the kit's house ads as the own book renders them, for a break the PDS could not
    be asked about."""
    kit = Creative(crid="house1", creative_id="h1-std-0a1b", hls_uri=f"{CDN}/ads/std/h1/main.m3u8", duration_ms=30_030)

    ads = house_ads([kit])
    module = prebid_config(SETTINGS.model_copy(update={"house_ads": ads}))["hooks"]["fast-ctv"]

    [ad] = json.loads(module["house-ads"])
    assert (ad["crid"], ad["dur"], ad["w"], ad["h"]) == ("house1", 30, 1920, 1080)
    assert ad["adm"] == vast_ad(kit, ad_system=AD_SYSTEM)
    with pytest.raises(ValueError, match="may not contain"):
        HouseAd(crid="x", adm="<VAST>${AUCTION_PRICE}</VAST>", dur=30, w=1, h=1)


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
