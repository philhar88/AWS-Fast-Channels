"""Shapes transcribed from the verified e2e configuration (e2e.md §2.6-2.12)."""

from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import pytest

from fast_channels.core import specs
from fast_channels.core.profiles import DEFAULT_PACKAGE_PROFILES, profile
from fast_channels.core.timeline import Plan, PlanInputAsset, ProgrammingBrief, plan_block

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

CHANNEL_ARN = "arn:aws:mediatailor:us-east-1:123456789012:channel/fast-demo"
CONFIG_ARN = "arn:aws:mediatailor:us-east-1:123456789012:playbackConfiguration/fast-demo"
CDN = "https://d111111abcdef8.cloudfront.net"
VAST_NS = {"v": "http://www.iab.com/VAST"}


def test_channel_outputs_are_hls_only() -> None:
    outputs = specs.channel_outputs([profile(n) for n in DEFAULT_PACKAGE_PROFILES])
    assert outputs == [
        {
            "ManifestName": "hls",
            "SourceGroup": "hls",
            "HlsPlaylistSettings": {"ManifestWindowSeconds": 60, "AdMarkupType": ["DATERANGE"]},  # SGAI needs DATERANGE
        }
    ]
    outputs[0]["HlsPlaylistSettings"]["AdMarkupType"].append("x")  # callers get independent copies
    assert specs.channel_outputs([profile("hls")])[0]["HlsPlaylistSettings"]["AdMarkupType"] == ["DATERANGE"]
    assert specs.channel_outputs([]) == []


def test_http_package_configurations() -> None:
    expected = [{"Path": "/assets/bbb-1a2b3c/standard_0123abcd45/main.m3u8", "SourceGroup": "hls", "Type": "HLS"}]
    for prefix in ("assets/bbb-1a2b3c/standard_0123abcd45/", "/assets/bbb-1a2b3c/standard_0123abcd45"):
        assert specs.http_package_configurations(prefix, [profile("hls")]) == expected
    # The package configuration's SourceGroup is the channel output's: CA serves that manifest on the output.
    (output,) = specs.channel_outputs([profile("hls")])
    assert output["SourceGroup"] == expected[0]["SourceGroup"]


@pytest.fixture
def plan(standard: Ladder) -> Plan:
    assets = [
        PlanInputAsset(
            asset_id=f"ep-{n}",
            vod_source=f"fast-ep-{n}",
            content_id=f"c{n}",
            duration_ms=1_320_000,
            break_offsets_ms=list(range(6006, 1_320_000, 6006)),
            ladder_version="standard@0123abcd45",
        )
        for n in (1, 2)
    ]
    return plan_block(
        channel="fast-demo",
        ladder_version="standard@0123abcd45",
        grid=standard.grid,
        start_ms=1_792_000_000_000,
        assets=assets,
        brief=ProgrammingBrief(),
        now_ms=0,
    )


def test_program_requests_are_absolute_at_every_planned_start(plan: Plan) -> None:
    slates = {30: "fast-slate-30"}
    first, second = specs.program_requests(
        plan, source_location="fast-origin", slate_vod_sources=slates, after_program="20261016T110000-earlier"
    )
    assert first["ScheduleConfiguration"]["Transition"] == {
        "Type": "ABSOLUTE",
        "RelativePosition": "AFTER_PROGRAM",
        "ScheduledStartTimeMillis": 1_792_000_000_000,
        "RelativeProgram": "20261016T110000-earlier",
    }
    assert second["ScheduleConfiguration"]["Transition"] == {
        "Type": "ABSOLUTE",
        "RelativePosition": "AFTER_PROGRAM",
        "ScheduledStartTimeMillis": plan.programs[1].start_ms,
        "RelativeProgram": plan.programs[0].name,
    }
    assert plan.programs[1].start_ms == plan.programs[0].end_ms
    assert (first["ChannelName"], first["VodSourceName"]) == ("fast-demo", "fast-ep-1")
    (ad_break,) = first["AdBreaks"]
    b = plan.programs[0].breaks[0]
    assert ad_break == {
        "OffsetMillis": b.offset_ms,
        "MessageType": "TIME_SIGNAL",
        "Slate": {"SourceLocationName": "fast-origin", "VodSourceName": "fast-slate-30"},
        "TimeSignalMessage": {
            "SegmentationDescriptors": [
                {
                    "SegmentationEventId": b.event_id,
                    "SegmentationUpidType": 12,
                    "SegmentationUpid": b.upid,
                    "SegmentationTypeId": 52,
                    "SegmentNum": 1,
                    "SegmentsExpected": 1,
                }
            ]
        },
    }


def _positions(requests: list[dict[str, object]]) -> list[tuple[object, object]]:
    transitions = [r["ScheduleConfiguration"]["Transition"] for r in requests]  # type: ignore[index]
    return [(t["RelativePosition"], t.get("RelativeProgram")) for t in transitions]


def test_program_requests_anchor_the_block_and_chain_its_programs(plan: Plan) -> None:
    first, second = (p.name for p in plan.programs)
    slates = {30: "fast-slate-30"}

    def positions(**anchor: str) -> list[tuple[object, object]]:
        return _positions(specs.program_requests(plan, source_location="o", slate_vod_sources=slates, **anchor))

    assert positions() == [("AFTER_PROGRAM", None), ("AFTER_PROGRAM", first)]  # an empty schedule
    assert positions(after_program="earlier") == [("AFTER_PROGRAM", "earlier"), ("AFTER_PROGRAM", first)]
    assert positions(before_program="later") == [("BEFORE_PROGRAM", "later"), ("AFTER_PROGRAM", first)]
    requests = specs.program_requests(plan, source_location="o", slate_vod_sources=slates)
    assert [r["ProgramName"] for r in requests] == [first, second]
    assert {r["ScheduleConfiguration"]["Transition"]["Type"] for r in requests} == {"ABSOLUTE"}
    with pytest.raises(ValueError, match="not both"):
        specs.program_requests(
            plan, source_location="o", slate_vod_sources=slates, after_program="a", before_program="b"
        )


def test_channel_policy_is_sigv4_only() -> None:
    policy = specs.channel_policy(
        channel_arn=CHANNEL_ARN, playback_configuration_arn=CONFIG_ARN, account="123456789012"
    )
    (statement,) = policy["Statement"]
    assert statement["Principal"] == {"Service": "mediatailor.amazonaws.com"}
    assert (statement["Action"], statement["Resource"]) == ("mediatailor:GetManifest", CHANNEL_ARN)
    assert statement["Condition"] == {
        "StringEquals": {"aws:SourceAccount": "123456789012"},
        "ArnLike": {"aws:SourceArn": CONFIG_ARN},
    }


@pytest.fixture
def config() -> dict[str, object]:
    return specs.playback_configuration(
        name="fast-demo",
        video_content_source_url="https://ca.example/v1/channel/fast-demo/",
        ads_url=specs.ads_url(content_base_url=CDN, ladder_version="standard@0123abcd45", channel="fast-demo"),
        content_base_url=CDN,
        log_percent=100,
        tags={"fast:app": "fast"},
    )


def test_playback_configuration(config: dict[str, object]) -> None:
    assert config["InsertionMode"] == "STITCHED_ONLY"  # SSAI only
    assert config["AdConditioningConfiguration"] == {"StreamingMediaFileConditioning": "NONE"}
    assert "DashConfiguration" not in config and "HlsConfiguration" not in config
    assert config["CdnConfiguration"] == {"AdSegmentUrlPrefix": CDN, "ContentSegmentUrlPrefix": CDN}
    assert config["AdsPersonalizationTimeouts"] == {
        "AdsRequestTimeoutMilliseconds": 2000,
        "LiveMaximumAdsPersonalizationTimeMilliseconds": 6000,
        "VodMaximumAdsPersonalizationTimeMilliseconds": 6000,
    }
    assert "SlateAdUrl" not in config
    put, logs = specs.playback_configuration_requests(config)
    assert "LogConfiguration" not in put and put["Name"] == "fast-demo"
    assert logs == {
        "PlaybackConfigurationName": "fast-demo",
        "PercentEnabled": 100,
        "EnabledLoggingStrategies": ["VENDED_LOGS"],
        "AdsInteractionLog": {"PublishOptInEventTypes": ["RAW_ADS_RESPONSE"]},
        "ManifestServiceInteractionLog": {"ExcludeEventTypes": ["GENERATED_MANIFEST"]},
    }
    with pytest.raises(ValueError, match="log_percent"):
        specs.playback_configuration(
            name="n", video_content_source_url="u", ads_url="https://a", content_base_url=CDN, log_percent=101, tags={}
        )


def test_to_cfn_playback_configuration(config: dict[str, object]) -> None:
    cfn = specs.to_cfn_playback_configuration(config)
    assert cfn["Tags"] == [{"Key": "fast:app", "Value": "fast"}]
    assert cfn["LogConfiguration"] == config["LogConfiguration"]
    post = config | {"AdDecisionServerConfiguration": {"HttpRequest": {"Method": "POST", "Body": "{}"}}}
    assert specs.to_cfn_playback_configuration(post)["AdDecisionServerConfiguration"] == {
        "HttpRequest": {"HttpMethod": "POST", "Body": "{}"}
    }
    assert post["AdDecisionServerConfiguration"] == {"HttpRequest": {"Method": "POST", "Body": "{}"}}  # unchanged


def test_ads_url() -> None:
    assert specs.ads_url(content_base_url=f"{CDN}/", ladder_version="standard@0123abcd45", channel="fast-demo") == (
        f"{CDN}/ads/standard_0123abcd45/vast.xml?ch=fast-demo&sid=[session.uuid]&evid=[scte.event_id]"
        "&upid=[scte.segmentation_upid]&dur_ms=[session.avail_duration_ms]"
    )
    assert specs.vast_key("standard@0123abcd45") == "ads/standard_0123abcd45/vast.xml"
    with pytest.raises(ValueError, match="https"):
        specs.ads_url(content_base_url="http://x", ladder_version="s@0123abcd45", channel="c")


def test_ads_variable_allow_list() -> None:
    ok = (
        "https://a/v?d=[session.avail_duration_secs]&p=[player_params.content_id]"
        "&u=[scte.segmentation_upid.private_data.1]"
    )
    assert specs.validate_ads_url(ok) == ok
    for bad in ("[avail.duration_secs]", "[session.avail_duration]", "[player_params.]", "[player_params.a-b]"):
        with pytest.raises(ValueError, match="unknown MediaTailor ADS variables"):
            specs.validate_ads_url(f"https://a/v?x={bad}")
    assert "session.avail_duration_ms" in specs.ADS_VARIABLES and "avail.duration_secs" not in specs.ADS_VARIABLES


def test_vast_pod() -> None:
    creatives = [
        specs.Creative(
            creative_id="house-30-standard_0123abcd45-a1b2c3",
            duration_ms=30030,
            hls_uri=f"{CDN}/ads/x/house-30/main.m3u8?a=1&b=2",
        ),
        specs.Creative(creative_id="house-14", duration_ms=3_725_014, hls_uri="h", width=1280, height=720),
    ]
    xml = specs.vast_pod(creatives=creatives, content_base_url=CDN)
    assert "?a=1&b=2]]>" in xml  # '&' stays raw inside CDATA
    root = ET.fromstring(xml)
    assert root.get("version") == "4.2"
    ads = root.findall("v:Ad", VAST_NS)
    assert [a.get("sequence") for a in ads] == ["1", "2"]
    linear = ads[0].find(".//v:Linear", VAST_NS)
    assert linear is not None
    assert linear.findtext("v:Duration", namespaces=VAST_NS) == "00:00:30.030"
    assert ads[1].findtext(".//v:Duration", namespaces=VAST_NS) == "01:02:05.014"
    files = linear.findall("v:MediaFiles/v:MediaFile", VAST_NS)
    assert [(f.get("delivery"), f.get("type"), f.get("width"), f.get("height")) for f in files] == [
        ("streaming", "application/x-mpegURL", "1920", "1080"),  # HLS only: no DASH MediaFile
    ]
    assert files[0].text == f"{CDN}/ads/x/house-30/main.m3u8?a=1&b=2"
    sd = ads[1].find(".//v:MediaFile", VAST_NS)
    assert sd is not None and (sd.get("width"), sd.get("height")) == ("1280", "720")
    assert "progressive" not in xml and "Mezzanine" not in xml and "dash" not in xml
    assert set(specs.Creative.model_fields) == {"creative_id", "duration_ms", "hls_uri", "width", "height"}
    events = [t.get("event") for t in linear.findall("v:TrackingEvents/v:Tracking", VAST_NS)]
    assert events == list(specs.QUARTILE_EVENTS)
    impression = ads[0].findtext(".//v:Impression", namespaces=VAST_NS)
    assert impression == f"{CDN}/ads/b.gif?evt=impression&cr=house-30-standard_0123abcd45-a1b2c3"
    assert ads[0].findtext(".//v:UniversalAdId", namespaces=VAST_NS) == creatives[0].creative_id


def test_playback_urls() -> None:
    prefix = "https://abc.mediatailor.us-east-1.amazonaws.com/v1/master/h/fast-demo/"
    urls = specs.playback_urls(cdn_base=f"{CDN}/", outputs=["hls"], playback_prefix_hls=prefix)
    assert urls == {"hls": f"{CDN}/v1/master/h/fast-demo/hls.m3u8"}
    assert specs.playback_urls(cdn_base=CDN, outputs=[], playback_prefix_hls=prefix) == {}


def test_vast_pod_repeats_creatives_to_cover_the_longest_break() -> None:
    ads = [specs.Creative(creative_id=f"house-{n}", duration_ms=30030, hls_uri="h") for n in (1, 2)]
    root = ET.fromstring(specs.vast_pod(creatives=ads, content_base_url=CDN, fill_ms=120120))
    pod = root.findall("v:Ad", VAST_NS)
    assert [a.get("sequence") for a in pod] == ["1", "2", "3", "4"]
    assert [a.findtext(".//v:UniversalAdId", namespaces=VAST_NS) for a in pod] == ["house-1", "house-2"] * 2
    assert len({a.get("id") for a in pod}) == 4  # Ad ids stay unique within the pod
    with pytest.raises(ValueError, match="at least one creative"):
        specs.vast_pod(creatives=[], content_base_url=CDN)
