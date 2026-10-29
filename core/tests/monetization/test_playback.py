"""Sibling playback configurations and their OpenRTB POST (DESIGN §6.2-6.3)."""

import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from fast_channels.core.monetization import playback as pb
from fast_channels.core.monetization.functions import Hook


def _sibling(kit: SimpleNamespace, **overrides: object) -> pb.PlaybackConfiguration:
    arguments: dict[str, object] = {
        "name": "fastm-demo-p-a",
        "video_content_source_url": kit.CA_URL,
        "content_base_url": kit.CDN,
        "pbs_origin": f"{kit.PBS}/",
        "log_percent": 100,
        "function_mapping": {Hook.PRE_SESSION: "fastm_ps_root"},
    }
    return pb.sibling_playback_configuration(**(arguments | overrides))  # type: ignore[arg-type]


def test_sibling_names() -> None:
    assert pb.sibling_name("fastm-demo", "a") == "fastm-demo-p-a"
    for letter in ("", "ab", "A", "-"):
        with pytest.raises(ValueError, match="one of"):
            pb.sibling_name("fastm-demo", letter)
    with pytest.raises(ValueError, match="longer than 64"):
        pb.sibling_name("c" * 61, "a")


def test_the_post_carries_no_player_value(kit: SimpleNamespace) -> None:
    assert pb.pbs_ads_url(f"{kit.PBS}/") == (
        f"{kit.PBS}/openrtb2/auction?evid=[scte.event_id]&upid=[scte.segmentation_upid]&dur=[session.avail_duration_ms]"
    )
    body = pb.ads_body(kids=False)
    assert body == '{"ext":{"prebid":{"storedrequest":{"id":"fast-pod-{{session.avail_duration_secs}}s"}}}}'
    rendered = json.loads(body.replace("{{session.avail_duration_secs}}", "60"))
    assert rendered["ext"]["prebid"]["storedrequest"]["id"] == "fast-pod-60s"
    assert "fast-pod-kids-{{session.avail_duration_secs}}s" in pb.ads_body(kids=True)
    assert pb.ads_headers("fastm-demo-p-a", probe=False, ticketed=False) == {
        "Content-Type": "application/json",
        "X-Fast-Pc": "fastm-demo-p-a",
        "X-Fast-Sid": "{{session.uuid}}",
    }
    probe = pb.ads_headers("fastm-demo-p-z", probe=True, ticketed=True)
    assert probe["X-Fast-Probe"] == "1"
    assert probe["X-Fast-T"] == "{{player_params.t}}"


def test_a_sibling_differs_from_v3_in_the_ads_settings_only(kit: SimpleNamespace) -> None:
    sibling = _sibling(kit)
    api = sibling.api()
    assert api["PersonalizationThresholdSeconds"] == 121
    assert api["AdDecisionServerUrl"].startswith(f"{kit.PBS}/openrtb2/auction?")
    request = api["AdDecisionServerConfiguration"]
    assert request["HttpRequest"]["Method"] == "POST"
    assert request["HttpRequest"]["CompressRequest"] == "NONE"
    assert request["VastResponse"] == {"AdSequencingMode": "FOLLOW_AD_SEQUENCE"}
    assert api["AvailSuppression"] == {"Mode": "BEHIND_LIVE_EDGE", "Value": "00:00:00"}
    assert api["FunctionMapping"] == {"PRE_SESSION_INITIALIZATION": "fastm_ps_root"}
    assert "SlateAdUrl" not in api  # the Channel Assembly break slate plays the unfilled time
    # The rest is the v3.0.0 configuration's.
    assert api["InsertionMode"] == "STITCHED_ONLY"
    assert api["AdConditioningConfiguration"] == {"StreamingMediaFileConditioning": "NONE"}
    assert api["AdsPersonalizationTimeouts"] == {
        "AdsRequestTimeoutMilliseconds": 2000,
        "LiveMaximumAdsPersonalizationTimeMilliseconds": 6000,
        "VodMaximumAdsPersonalizationTimeMilliseconds": 6000,
    }
    assert api["LogConfiguration"]["EnabledLoggingStrategies"] == ["VENDED_LOGS"]
    assert sibling.opt_in_events == {"RAW_ADS_RESPONSE"}
    assert sibling.ad_sequencing_mode == "FOLLOW_AD_SEQUENCE"
    assert sibling.body == pb.ads_body(kids=False)


def test_variants(kit: SimpleNamespace) -> None:
    lab = _sibling(kit, lab=True)
    assert lab.opt_in_events == {
        "RAW_ADS_RESPONSE",
        "PRE_SESSION_INIT_HOOK_SUMMARY",
        "PRE_SESSION_INIT_FUNCTION_COMPLETED",
    }
    assert _sibling(kit, route=pb.AdsRoute.OPAQUE).ad_sequencing_mode == "IGNORE_AD_SEQUENCE"
    assert "kids" in (_sibling(kit, kids=True).body or "")
    ticketed = _sibling(kit, ticketed=True, probe=True)
    assert ticketed.headers["X-Fast-T"] == "{{player_params.t}}"
    bare = pb.PlaybackConfiguration.model_validate(
        {
            "Name": "x",
            "VideoContentSourceUrl": "https://ca",
            "AdDecisionServerUrl": "https://ads",
            "LogConfiguration": {"PercentEnabled": 0, "EnabledLoggingStrategies": ["VENDED_LOGS"]},
        }
    )
    assert (bare.headers, bare.body, bare.ad_sequencing_mode, bare.opt_in_events) == ({}, None, None, frozenset())
    with pytest.raises(ValidationError, match="SlateAdUrl"):
        pb.PlaybackConfiguration.model_validate(bare.api() | {"SlateAdUrl": "https://slate"})


def test_render_adds_what_only_the_apply_knows(kit: SimpleNamespace) -> None:
    sibling = _sibling(kit)
    put, logs = pb.render(sibling, prefix="fastm", release=12, change_id="01JCHANGE", origin_secret=SecretStr("A-1"))
    headers = put["AdDecisionServerConfiguration"]["HttpRequest"]["Headers"]
    assert headers["X-Fast-Origin"] == "A-1"
    assert headers["X-Fast-Fv"] == "r0012"
    assert put["FunctionMapping"] == {"PRE_SESSION_INITIALIZATION": "fastm_ps_root_r0012"}
    assert put["Tags"] == {
        "fast:app": "fastm",
        "fast:owner": "release-controller",
        "fast:release": "r0012",
        "fast:change": "01JCHANGE",
    }
    assert "LogConfiguration" not in put
    assert logs == {"PlaybackConfigurationName": "fastm-demo-p-a", **sibling.api()["LogConfiguration"]}
    assert "X-Fast-Origin" not in sibling.headers  # the desired state never holds the secret
    assert "A-1" not in json.dumps(sibling.api())
