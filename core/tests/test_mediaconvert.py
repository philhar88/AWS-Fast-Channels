"""compile_job_settings: one Apple HLS group of muxed MPEG-TS variants on the Basic tier, and the per-job overrides.

hls-ts/: what the compiled STANDARD template wrote for a 40 s clip and a 15-GOP slate (mediastreamvalidator: PASS).
"""

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from syrupy.extensions.json import JSONSnapshotExtension

from fast_channels.core.esam import build_esam
from fast_channels.core.ladder import Ladder
from fast_channels.core.manifests import media_segment_durations, parse_multivariant
from fast_channels.core.mediaconvert import (
    AUDIO_SELECTOR,
    GROUP_TYPE,
    GeneratorInput,
    JobOverrides,
    build_job_overrides,
    compile_job_settings,
    job_template_props,
    output_prefix,
)
from fast_channels.core.profiles import profile

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

CAPTURE = Path(__file__).parent / "data/hls-ts"
FRAME_RATE = {
    "FramerateControl": "SPECIFIED",
    "FramerateNumerator": 30000,
    "FramerateDenominator": 1001,
    "FramerateConversionAlgorithm": "DUPLICATE_DROP",
}
AAC_LC = {
    "AudioSourceName": AUDIO_SELECTOR,
    "LanguageCodeControl": "USE_CONFIGURED",
    "CustomLanguageCode": "en",
    "CodecSettings": {
        "Codec": "AAC",
        "AacSettings": {
            "CodecProfile": "LC",
            "RateControlMode": "CBR",
            "Bitrate": 128_000,
            "CodingMode": "CODING_MODE_2_0",
            "SampleRate": 48000,
            "Specification": "MPEG4",
        },
    },
}
PROFESSIONAL_TIER: frozenset[str] = frozenset(
    {
        "Esam",
        "AccelerationSettings",
        "AudioNormalizationSettings",
        "CaptionDescriptions",
        "CaptionSelectors",
        "CaptionLanguageMappings",
        "AdditionalManifests",
        "AdMarkers",
        "H265Settings",
        "Av1Settings",
        "NoiseReducer",
        "DolbyVision",
        "Hdr10Plus",
        "ImageInserter",
        "MotionImageInserter",
        "PartitionedVideoProcessing",
    }
)
"""Settings that would move a job off the Basic tier (or bring back removed features: ESAM, captions, I-frames)."""


def _keys(node: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _keys(value)


def _values(document: dict[str, Any], name: str) -> set[Any]:
    return {v for k, v in _keys(document) if k == name}


@pytest.fixture(scope="module")
def settings(standard: Ladder) -> dict[str, Any]:
    return compile_job_settings(standard)


def _outputs(settings: dict[str, Any]) -> list[dict[str, Any]]:
    (group,) = settings["OutputGroups"]
    outputs: list[dict[str, Any]] = group["Outputs"]
    return outputs


def _overrides(ladder: Ladder, **changes: Any) -> JobOverrides:
    arguments: dict[str, Any] = {
        "ladder": ladder,
        "media_bucket": "media",
        "asset_id": "sintel-1a2b3c",
        "source_uri": "s3://in/sintel.mp4",
        "generator": None,
        "correlation_id": "c-1",
        "app": "fast",
    }
    return build_job_overrides(**arguments | changes)


def test_job_level_settings(settings: dict[str, Any]) -> None:
    assert set(settings) == {"TimecodeConfig", "FollowSource", "OutputGroups"}  # Inputs are per job overrides
    assert (settings["TimecodeConfig"], settings["FollowSource"]) == ({"Source": "ZEROBASED"}, 1)


def test_one_apple_hls_group(settings: dict[str, Any]) -> None:
    (group,) = settings["OutputGroups"]
    assert (group["Name"], group["CustomName"]) == ("Apple HLS", "hls")
    assert group["OutputGroupSettings"]["Type"] == GROUP_TYPE == "HLS_GROUP_SETTINGS"
    assert set(group["OutputGroupSettings"]) == {"Type", "HlsGroupSettings"}
    hls = group["OutputGroupSettings"]["HlsGroupSettings"]
    assert {k: hls[k] for k in ("SegmentControl", "SegmentLength", "SegmentLengthControl", "MinSegmentLength")} == {
        "SegmentControl": "SEGMENTED_FILES",
        "SegmentLength": 6,
        "SegmentLengthControl": "GOP_MULTIPLE",  # closed 2 s GOPs: every segment is exactly 3 GOPs (6.006 s)
        "MinSegmentLength": 0,
    }
    assert hls["CaptionLanguageSetting"] == "NONE"  # writes CLOSED-CAPTIONS=NONE on every variant
    assert hls["TargetDurationCompatibilityMode"] == "SPEC_COMPLIANT"  # TARGETDURATION:6 for 6.006 s segments
    assert hls["ManifestDurationFormat"] == "FLOATING_POINT"
    assert (hls["CodecSpecification"], hls["StreamInfResolution"]) == ("RFC_6381", "INCLUDE")
    assert (hls["DirectoryStructure"], hls["OutputSelection"]) == ("SINGLE_DIRECTORY", "MANIFESTS_AND_SEGMENTS")
    assert (hls["ProgramDateTime"], hls["TimedMetadataId3Frame"]) == ("EXCLUDE", "NONE")
    assert hls["ManifestCompression"] == "NONE"
    assert "Destination" not in hls  # set per asset by the job overrides


def test_outputs_are_muxed_ts_variants_in_multivariant_order(settings: dict[str, Any], standard: Ladder) -> None:
    outputs = _outputs(settings)
    assert [o["NameModifier"] for o in outputs] == ["_avc-720p", "_avc-1080p", "_avc-360p"]
    for output in outputs:
        assert set(output) == {
            "NameModifier",
            "ContainerSettings",
            "OutputSettings",
            "VideoDescription",
            "AudioDescriptions",
        }
        container = output["ContainerSettings"]
        assert container["Container"] == "M3U8" and set(container) == {"Container", "M3u8Settings"}
        m3u8 = container["M3u8Settings"]
        assert (m3u8["Scte35Source"], m3u8["TimedMetadata"], m3u8["NielsenId3"]) == ("NONE", "NONE", "NONE")
        assert m3u8["AudioDuration"] == "MATCH_VIDEO_DURATION"  # Channel Assembly needs equal-length tracks
        assert (m3u8["PmtPid"], m3u8["VideoPid"], m3u8["AudioPids"]) == (480, 481, [482])
        assert output["OutputSettings"] == {"HlsSettings": {"IFrameOnlyManifest": "EXCLUDE"}}  # no audio groups
        assert output["AudioDescriptions"] == [AAC_LC]  # one muxed AAC-LC track per variant


def test_the_capture_is_what_the_template_writes(standard: Ladder) -> None:
    """The real encode's playlists follow from the group settings: names, order, CODECS and segment cadence."""
    multivariant = (CAPTURE / "content/main.m3u8").read_text()
    assert [v.uri for v in parse_multivariant(multivariant)] == [f"main{r.name_modifier}.m3u8" for r in standard.video]
    assert all(line.endswith(",CLOSED-CAPTIONS=NONE") for line in multivariant.splitlines() if "STREAM-INF" in line)
    assert "#EXT-X-I-FRAME-STREAM-INF" not in multivariant and "#EXT-X-MEDIA:" not in multivariant
    playlist = (CAPTURE / "content/main_avc-720p.m3u8").read_text()
    assert "#EXT-X-TARGETDURATION:6" in playlist and "main_avc-720p_00001.ts" in playlist
    assert set(media_segment_durations(playlist)[:-1]) == {standard.grid.segment_ms / 1000}


@pytest.mark.parametrize(
    ("rung", "size", "qvbr", "max_bps", "hrd_bps"),
    [
        ("_avc-720p", (1280, 720), 7, 3_400_000, 6_800_000),
        ("_avc-1080p", (1920, 1080), 8, 6_000_000, 12_000_000),
        ("_avc-360p", (640, 360), 6, 800_000, 800_000),  # HRD = MaxBitrate at <= 432p
    ],
)
def test_video_settings(
    settings: dict[str, Any], rung: str, size: tuple[int, int], qvbr: int, max_bps: int, hrd_bps: int
) -> None:
    description = next(o for o in _outputs(settings) if o["NameModifier"] == rung)["VideoDescription"]
    assert (description["Width"], description["Height"]) == size
    assert description["VideoPreprocessors"] == {
        "Deinterlacer": {"Algorithm": "INTERPOLATE", "Mode": "DEINTERLACE", "Control": "NORMAL"}
    }
    assert set(description["CodecSettings"]) == {"Codec", "H264Settings"}
    assert description["CodecSettings"]["Codec"] == "H_264"
    h264 = description["CodecSettings"]["H264Settings"]
    assert {k: h264[k] for k in FRAME_RATE} == FRAME_RATE
    assert (h264["GopSize"], h264["GopSizeUnits"], h264["GopClosedCadence"]) == (60, "FRAMES", 1)
    assert (h264["RateControlMode"], h264["QvbrSettings"]) == ("QVBR", {"QvbrQualityLevel": qvbr})
    assert (h264["MaxBitrate"], h264["HrdBufferSize"]) == (max_bps, hrd_bps)
    assert (h264["CodecProfile"], h264["CodecLevel"]) == ("HIGH", "AUTO")
    assert h264["QualityTuningLevel"] == "SINGLE_PASS_HQ"  # multi-pass is Professional tier


def test_settings_stay_on_the_basic_tier(standard: Ladder, settings: dict[str, Any]) -> None:
    props = job_template_props(standard, name="fast-standard", queue_arn="arn:queue", category="fast")
    file = _overrides(standard, cue_points_ms=[18018, 42042])  # on the segment grid: no ESAM
    slate = _overrides(
        standard,
        source_uri=None,
        generator=GeneratorInput(image_s3_uri="s3://kit/slate.png", duration_gops=15, label="slate-15"),
    )
    for document in (settings, props, file.settings, slate.settings):
        assert not PROFESSIONAL_TIER & {k for k, _ in _keys(document)}
        assert _values(document, "QualityTuningLevel") <= {"SINGLE_PASS_HQ"}
        assert _values(document, "Codec") <= {"H_264", "AAC"}
        assert _values(document, "IFrameOnlyManifest") <= {"EXCLUDE"}


def test_rendition_options_reach_the_template(standard: Ladder) -> None:
    main = standard.model_copy(update={"video": (standard.video[0].model_copy(update={"profile": "MAIN"}),)})
    (output,) = _outputs(compile_job_settings(main))
    assert output["VideoDescription"]["CodecSettings"]["H264Settings"]["CodecProfile"] == "MAIN"
    louder = Ladder.model_validate(standard.model_dump() | {"audio": {"kbps": 192}})
    assert _values(compile_job_settings(louder), "Bitrate") == {192_000}  # the AAC bitrate of every variant


def test_the_grid_follows_the_ladder(standard: Ladder) -> None:
    pal = Ladder.model_validate(standard.model_dump() | {"frame_rate": "25", "segment_seconds": 4})
    settings = compile_job_settings(pal)
    assert settings["OutputGroups"][0]["OutputGroupSettings"]["HlsGroupSettings"]["SegmentLength"] == 4
    h264 = _outputs(settings)[0]["VideoDescription"]["CodecSettings"]["H264Settings"]
    assert (h264["GopSize"], h264["FramerateNumerator"], h264["FramerateDenominator"]) == (50, 25, 1)


def test_standard_settings_snapshot(settings: dict[str, Any], snapshot: SnapshotAssertion) -> None:
    assert settings == snapshot.use_extension(JSONSnapshotExtension)


def test_job_template_props(standard: Ladder) -> None:
    props = job_template_props(standard, name="fast-standard", queue_arn="arn:queue", category="fast")
    assert set(props) == {
        "Name",
        "Description",
        "Category",
        "Queue",
        "Priority",
        "StatusUpdateInterval",
        "Settings",
        "Tags",
    }  # no AccelerationSettings: accelerated transcoding is Professional tier
    assert (props["Name"], props["Category"], props["Queue"]) == ("fast-standard", "fast", "arn:queue")
    assert props["Settings"] == compile_job_settings(standard)
    assert props["Tags"] == {"fast:app": "fast", "fast:ladder": standard.version}
    assert props["Description"] == f"{standard.version}: {standard.description}"
    bare = standard.model_copy(update={"description": ""})
    assert job_template_props(bare, name="n", queue_arn="q", category="c")["Description"] == standard.version


def test_file_overrides(standard: Ladder, settings: dict[str, Any]) -> None:
    o = _overrides(standard)
    assert set(JobOverrides.model_fields) == {
        "settings",
        "user_metadata",
        "destination_prefix",
        "snapped_cue_points_ms",
        "professional",
    }
    assert set(o.settings) == {"Inputs", "OutputGroups"}
    assert o.settings["Inputs"] == [
        {
            "FileInput": "s3://in/sintel.mp4",
            "TimecodeSource": "ZEROBASED",
            "VideoSelector": {"ColorSpace": "FOLLOW", "Rotate": "AUTO"},
            "AudioSelectors": {AUDIO_SELECTOR: {"DefaultSelection": "DEFAULT"}},
        }
    ]
    prefix = f"assets/sintel-1a2b3c/{standard.version.replace('@', '_')}/"
    assert o.destination_prefix == prefix == output_prefix("sintel-1a2b3c", standard.version)
    assert o.settings["OutputGroups"] == [
        {"OutputGroupSettings": {"Type": GROUP_TYPE, "HlsGroupSettings": {"Destination": f"s3://media/{prefix}main"}}}
    ]
    # CreateJob deep-merges the override into the template's only group: same type, same settings key.
    template_group = settings["OutputGroups"][0]["OutputGroupSettings"]
    override_group = o.settings["OutputGroups"][0]["OutputGroupSettings"]
    assert override_group.keys() == template_group.keys() and override_group["Type"] == template_group["Type"]
    # The multivariant lands where the hls package configuration points.
    assert f"{prefix}main.m3u8" == profile("hls").manifest_key(prefix)
    assert o.user_metadata == {
        "app": "fast",
        "asset_id": "sintel-1a2b3c",
        "ladder": standard.version,
        "correlation_id": "c-1",
    }
    assert o.snapped_cue_points_ms == []


def test_cue_points_on_the_segment_grid_need_no_esam(standard: Ladder) -> None:
    o = _overrides(standard, cue_points_ms=[0, 6006, 12012, 12000])  # 12000 is within half a frame of 12012
    assert (o.snapped_cue_points_ms, o.professional, "Esam" in o.settings) == ([6006, 12012], False, False)
    o = _overrides(standard, cue_points_ms=[6006, 12100])
    assert (o.snapped_cue_points_ms, o.professional, "Esam" in o.settings) == ([6006, 12112], True, True)
    o = _overrides(standard, cue_points_ms=[0, 6006, 12012, 42042])
    assert (o.snapped_cue_points_ms, o.professional, "Esam" in o.settings) == ([6006, 12012, 42042], False, False)


def test_cue_points_snap_to_the_frame_and_add_esam(standard: Ladder) -> None:
    # The cues of the ESAM probe (2026-10-17): MediaConvert split all three variants at 13.480 s and 26.994 s.
    o = _overrides(standard, cue_points_ms=[27000, 13480, 13480, 0])
    assert o.snapped_cue_points_ms == [13480, 26994]  # frames 404 and 809, sorted, deduplicated, 0 is no break
    assert o.professional
    assert o.settings["Esam"] == build_esam([13480, 26994])
    assert o.settings["Esam"]["ResponseSignalPreroll"] == 0  # the default 4 s can signal a segment early
    assert all(abs(standard.grid.snap_to_frame(ms) - ms) <= 17 for ms in range(0, 120_000, 997))  # half a frame


def test_generator_overrides(standard: Ladder) -> None:
    generator = GeneratorInput(image_s3_uri="s3://kit/slate.png", duration_gops=15, label="slate-15")
    o = _overrides(standard, asset_id="slate-15", source_uri=None, generator=generator)
    (source,) = o.settings["Inputs"]
    assert set(source) == {"VideoGenerator", "TimecodeSource", "AudioSelectors"}
    assert source["VideoGenerator"] == {
        "Duration": 30030,
        "ImageInput": "s3://kit/slate.png",
        "Width": 1920,
        "Height": 1080,
        "FramerateNumerator": 30000,
        "FramerateDenominator": 1001,
        "Channels": 2,
        "SampleRate": 48000,
    }
    # The captured 15-GOP slate is exactly that long, in whole segments on every variant.
    for r in standard.video:
        durations = media_segment_durations((CAPTURE / f"slate/main{r.name_modifier}.m3u8").read_text())
        assert round(sum(durations) * 1000) == 30030 and len(durations) == 5
    assert o.user_metadata["label"] == "slate-15"
    assert o.snapped_cue_points_ms == []


def test_override_errors(standard: Ladder) -> None:
    generator = GeneratorInput(image_s3_uri="s3://kit/slate.png", duration_gops=1, label="s")
    with pytest.raises(ValueError, match="exactly one"):
        _overrides(standard, generator=generator)
    with pytest.raises(ValueError, match="exactly one"):
        _overrides(standard, source_uri=None)
    with pytest.raises(ValueError, match="no cue points"):
        _overrides(standard, source_uri=None, generator=generator, cue_points_ms=[2002])
    with pytest.raises(TypeError):
        _overrides(standard, captions=None)  # caption plans are gone
