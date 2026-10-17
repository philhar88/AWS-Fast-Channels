"""compile_job_settings reproduces the verified golden job (mediaconvert.md §9.4) except for listed v3 changes."""

import json
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from syrupy.extensions.json import JSONSnapshotExtension

from fast_channels.core.captions import CaptionFile, CaptionKind, CaptionPlan, CaptionSource, CaptionTrack
from fast_channels.core.esam import build_esam
from fast_channels.core.ladder import Ladder, VideoCodec
from fast_channels.core.mediaconvert import (
    GeneratorInput,
    additional_manifests,
    build_job_overrides,
    caption_scan_settings,
    compile_job_settings,
    count_cues,
    job_template_props,
    output_prefix,
)
from fast_channels.core.profiles import applicable, selected_outputs

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

DATA = Path(__file__).parent / "data"

GOLDEN: dict[str, Any] = json.loads((DATA / "golden-job.json").read_text())["Settings"]
GOLDEN_GROUP = GOLDEN["OutputGroups"][0]
GOLDEN_OUTPUTS = {o["NameModifier"]: o for o in GOLDEN_GROUP["Outputs"]}
RUNG_VALUES = {"QvbrSettings", "MaxBitrate", "HrdBufferSize", "CodecProfile", "BitDepth"}
FRAME_RATE = {
    "FramerateControl": "SPECIFIED",
    "FramerateNumerator": 30000,
    "FramerateDenominator": 1001,
    "FramerateConversionAlgorithm": "DUPLICATE_DROP",
}


def shared(
    compiled: dict[str, Any], golden: dict[str, Any], *, varies: AbstractSet[str] = frozenset()
) -> dict[str, Any]:
    """Assert every golden field is reproduced (except ``varies``); return the fields v3 adds."""
    for key, value in golden.items():
        if key not in varies:
            assert compiled.get(key) == value, key
    return {k: v for k, v in compiled.items() if k not in golden}


@pytest.fixture(scope="module")
def settings(standard: Ladder) -> dict[str, Any]:
    return compile_job_settings(standard)


def _output(settings: dict[str, Any], name: str) -> dict[str, Any]:
    return next(o for o in settings["OutputGroups"][0]["Outputs"] if o["NameModifier"] == name)


def test_job_level_settings(settings: dict[str, Any]) -> None:
    assert shared(settings, GOLDEN, varies={"Inputs", "Esam", "OutputGroups"}) == {}
    assert "Inputs" not in settings and "Esam" not in settings  # per job, sent as overrides


def test_cmaf_group(settings: dict[str, Any], standard: Ladder) -> None:
    group = settings["OutputGroups"][0]
    assert shared(group, GOLDEN_GROUP, varies={"OutputGroupSettings", "Outputs"}) == {}
    cmaf, golden = group["OutputGroupSettings"]["CmafGroupSettings"], GOLDEN_GROUP["OutputGroupSettings"]
    assert group["OutputGroupSettings"]["Type"] == golden["Type"]
    changes = {"Destination", "MpdManifestBandwidthType", "AdditionalManifests"}
    assert shared(cmaf, golden["CmafGroupSettings"], varies=changes) == {}
    assert "Destination" not in cmaf  # set per asset by the job overrides
    assert cmaf["MpdManifestBandwidthType"] == "MAX"  # SPEC-FINAL §3: deterministic DASH @bandwidth
    assert cmaf["AdditionalManifests"] == additional_manifests(standard)


def test_additional_manifests_cover_every_applicable_profile(standard: Ladder) -> None:
    manifests = {m["ManifestNameModifier"]: m["SelectedOutputs"] for m in additional_manifests(standard)}
    assert list(manifests) == ["_avc", "_hevc", "_av1", "_sd", "_hd"]
    for p in applicable(standard):
        if p.manifest_suffix:
            assert manifests[p.manifest_suffix] == selected_outputs(standard, p)


@pytest.mark.parametrize(
    ("codec", "golden_name", "settings_key"),
    [
        (VideoCodec.AVC, "_avc_1080p", "H264Settings"),
        (VideoCodec.HEVC, "_hevc_1080p", "H265Settings"),
        (VideoCodec.AV1, "_av1_1080p", "Av1Settings"),
    ],
)
def test_video_outputs(
    settings: dict[str, Any], standard: Ladder, codec: VideoCodec, golden_name: str, settings_key: str
) -> None:
    golden = GOLDEN_OUTPUTS[golden_name]
    rendition = next(r for r in standard.video if r.codec is codec and r.height == 1080)
    output = _output(settings, rendition.name_modifier)
    assert shared(output, golden, varies={"NameModifier", "ContainerSettings", "VideoDescription"}) == {}
    cmfc = output["ContainerSettings"]["CmfcSettings"]
    assert output["ContainerSettings"]["Container"] == "CMFC"
    assert cmfc == {"AudioRenditionSets": rendition.audio_group, "IFrameOnlyManifest": "EXCLUDE"}
    description = output["VideoDescription"]
    added = shared(description, golden["VideoDescription"], varies={"CodecSettings"})
    assert added == {
        "VideoPreprocessors": {"Deinterlacer": {"Algorithm": "INTERPOLATE", "Mode": "DEINTERLACE", "Control": "NORMAL"}}
    }
    codec_settings = description["CodecSettings"][settings_key]
    golden_codec = golden["VideoDescription"]["CodecSettings"][settings_key]
    assert description["CodecSettings"]["Codec"] == golden["VideoDescription"]["CodecSettings"]["Codec"]
    added = shared(codec_settings, golden_codec, varies=RUNG_VALUES | {"FramerateControl"})
    assert {k: codec_settings[k] for k in FRAME_RATE} == FRAME_RATE
    assert set(added) == set(FRAME_RATE) - {"FramerateControl"}
    assert codec_settings["QvbrSettings"] == {"QvbrQualityLevel": rendition.qvbr}
    assert codec_settings["MaxBitrate"] == rendition.max_kbps * 1000


def test_rung_specific_values(settings: dict[str, Any]) -> None:
    hevc = _output(settings, "_hevc-1080p")["VideoDescription"]["CodecSettings"]["H265Settings"]
    assert (hevc["CodecProfile"], hevc["WriteMp4PackagingType"], hevc["HrdBufferSize"]) == (
        "MAIN_MAIN",
        "HVC1",
        11_000_000,
    )
    floor = _output(settings, "_avc-234p")["VideoDescription"]["CodecSettings"]["H264Settings"]
    assert floor["HrdBufferSize"] == floor["MaxBitrate"] == 120_000
    av1 = _output(settings, "_av1-360p")["VideoDescription"]["CodecSettings"]["Av1Settings"]
    assert (av1["BitDepth"], av1["GopSize"], av1["FilmGrainSynthesis"]) == ("BIT_10", 60, "DISABLED")
    assert "HrdBufferSize" not in av1


def test_audio_outputs(settings: dict[str, Any], standard: Ladder) -> None:
    golden = GOLDEN_OUTPUTS["_aac-lc-128k"]
    output = _output(settings, "_aac-lc-128")
    golden_cmfc = golden["ContainerSettings"]["CmfcSettings"]
    assert shared(output["ContainerSettings"]["CmfcSettings"], golden_cmfc, varies={"AudioGroupId"}) == {}
    (description,), (golden_description,) = output["AudioDescriptions"], golden["AudioDescriptions"]
    added = shared(description, golden_description, varies={"LanguageCode", "StreamName", "CodecSettings"})
    assert added == {
        "CustomLanguageCode": "en",
        "AudioNormalizationSettings": {
            "Algorithm": "ITU_BS_1770_3",
            "AlgorithmControl": "CORRECT_AUDIO",
            "TargetLkfs": -24.0,
            "PeakCalculation": "NONE",
            "LoudnessLogging": "DONT_LOG",
        },
    }
    assert "LanguageCode" not in description and description["StreamName"] == "English"
    aac, golden_aac = description["CodecSettings"]["AacSettings"], golden_description["CodecSettings"]["AacSettings"]
    assert shared(aac, golden_aac) == {}
    profiles = {
        o["NameModifier"]: o["AudioDescriptions"][0]["CodecSettings"]["AacSettings"]["CodecProfile"]
        for o in settings["OutputGroups"][0]["Outputs"]
        if "AudioDescriptions" in o
    }
    assert profiles == {"_aac-lc-128": "LC", "_aac-he1-64": "HEV1", "_aac-he2-32": "HEV2"}


def test_output_order_is_multivariant_order(settings: dict[str, Any], standard: Ladder) -> None:
    names = [o["NameModifier"] for o in settings["OutputGroups"][0]["Outputs"]]
    assert names == [
        *(r.name_modifier for r in standard.video),
        *(a.name_modifier for a in standard.audio),
        *(c.name_modifier for c in standard.captions),
    ]
    assert names[0] == "_avc-720p-a"


@pytest.mark.parametrize("name", ["standard", "uhd"])
def test_video_preprocessing_allows_accelerated_transcoding(name: str) -> None:
    # Inverse telecine (ADAPTIVE or INVERSE_TELECINE) makes MediaConvert run PREFERRED jobs NOT_ACCELERATED.
    outputs = compile_job_settings(Ladder.builtin(name))["OutputGroups"][0]["Outputs"]
    modes = {
        o["VideoDescription"]["VideoPreprocessors"]["Deinterlacer"]["Mode"] for o in outputs if "VideoDescription" in o
    }
    assert modes == {"DEINTERLACE"}


def test_standard_settings_snapshot(settings: dict[str, Any], snapshot: SnapshotAssertion) -> None:
    assert settings == snapshot.use_extension(JSONSnapshotExtension)


def test_options_change_the_template(standard: Ladder, uhd: Ladder) -> None:
    options = standard.options.model_copy(update={"iframe_playlists": True, "loudness_lkfs": None, "film_grain": True})
    outputs = compile_job_settings(standard.model_copy(update={"options": options}))["OutputGroups"][0]["Outputs"]
    assert outputs[0]["ContainerSettings"]["CmfcSettings"]["IFrameOnlyManifest"] == "INCLUDE"
    assert (
        "AudioNormalizationSettings"
        not in _output({"OutputGroups": [{"Outputs": outputs}]}, "_aac-he2-32")["AudioDescriptions"][0]
    )
    av1 = next(o for o in outputs if o["NameModifier"] == "_av1-1080p")
    assert av1["VideoDescription"]["CodecSettings"]["Av1Settings"]["FilmGrainSynthesis"] == "ENABLED"
    uhd_hevc = _output(compile_job_settings(uhd), "_hevc-2160p-b")["VideoDescription"]["CodecSettings"]
    assert uhd_hevc["H265Settings"]["CodecProfile"] == "MAIN10_MAIN"


def test_job_template_props(standard: Ladder) -> None:
    props = job_template_props(standard, name="fast-standard", queue_arn="arn:queue", category="fast")
    assert props["AccelerationSettings"] == {"Mode": "PREFERRED"} and props["Queue"] == "arn:queue"
    assert props["Settings"] == compile_job_settings(standard)
    assert props["Tags"] == {"fast:app": "fast", "fast:ladder": standard.version}
    assert props["Description"].startswith(standard.version)
    bare = standard.model_copy(update={"description": ""})
    assert job_template_props(bare, name="n", queue_arn="q", category="c")["Description"] == standard.version


def test_file_overrides_match_the_golden_input(standard: Ladder) -> None:
    o = build_job_overrides(
        ladder=standard,
        media_bucket="media",
        asset_id="bbb-1a2b3c",
        source_uri="s3://in/bbb.mp4",
        generator=None,
        correlation_id="c-1",
        app="fast",
    )
    (source,) = o.settings["Inputs"]
    empty = {"Captions en": {"SourceSettings": {"SourceType": "NULL_SOURCE"}}}  # no caption plan: empty tracks
    assert source == GOLDEN["Inputs"][0] | {"FileInput": "s3://in/bbb.mp4", "CaptionSelectors": empty}
    group = o.settings["OutputGroups"][0]["OutputGroupSettings"]
    destination = f"s3://media/assets/bbb-1a2b3c/{standard.version.replace('@', '_')}/main"
    assert group == {"Type": "CMAF_GROUP_SETTINGS", "CmafGroupSettings": {"Destination": destination}}
    assert o.destination_prefix == output_prefix("bbb-1a2b3c", standard.version)
    assert o.user_metadata == {
        "app": "fast",
        "asset_id": "bbb-1a2b3c",
        "ladder": standard.version,
        "correlation_id": "c-1",
    }
    assert (o.acceleration, o.snapped_cue_points_ms, "Esam" in o.settings) == (None, [], False)


def test_cue_points_snap_and_esam_only_when_needed(standard: Ladder) -> None:
    def overrides(cues: list[int]) -> Any:
        return build_job_overrides(
            ladder=standard,
            media_bucket="m",
            asset_id="a",
            source_uri="s3://in/a.mp4",
            generator=None,
            cue_points_ms=cues,
            correlation_id="c",
            app="fast",
        )

    on_grid = overrides([0, 6000, 12010])  # natural 6.006 s segment boundaries: no ESAM, no Pro-tier features
    assert on_grid.snapped_cue_points_ms == [6006, 12012] and "Esam" not in on_grid.settings
    off_grid = overrides([20000, 40000, 40001])
    assert off_grid.snapped_cue_points_ms == [20020, 40040]
    assert off_grid.settings["Esam"] == build_esam([20020, 40040])


def test_generator_overrides(standard: Ladder) -> None:
    generator = GeneratorInput(image_s3_uri="s3://kit/slate.png", duration_gops=15, label="slate-15")
    o = build_job_overrides(
        ladder=standard,
        media_bucket="m",
        asset_id="slate-15",
        source_uri=None,
        generator=generator,
        correlation_id="c",
        app="fast",
    )
    (source,) = o.settings["Inputs"]
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
    assert o.acceleration == "DISABLED"  # VideoGenerator inputs never accelerate (mediaconvert.md §7.2)
    assert o.user_metadata["label"] == "slate-15"


def test_override_errors(standard: Ladder) -> None:
    generator = GeneratorInput(image_s3_uri="s3://kit/slate.png", duration_gops=1, label="s")
    with pytest.raises(ValueError, match="exactly one"):
        build_job_overrides(
            ladder=standard,
            media_bucket="m",
            asset_id="a",
            source_uri="s3://x/y.mp4",
            generator=generator,
            correlation_id="c",
            app="fast",
        )
    with pytest.raises(ValueError, match="no cue points"):
        build_job_overrides(
            ladder=standard,
            media_bucket="m",
            asset_id="a",
            source_uri=None,
            generator=generator,
            cue_points_ms=[2002],
            correlation_id="c",
            app="fast",
        )


def test_caption_outputs_are_imsc_tracks_in_every_manifest(settings: dict[str, Any], standard: Ladder) -> None:
    assert _output(settings, "_cc-en") == {
        "NameModifier": "_cc-en",
        "ContainerSettings": {"Container": "CMFC"},
        "CaptionDescriptions": [
            {
                "CaptionSelectorName": "Captions en",
                "CustomLanguageCode": "en",
                "LanguageDescription": "English",
                "DestinationSettings": {
                    "DestinationType": "IMSC",
                    "ImscDestinationSettings": {"Accessibility": "ENABLED", "StylePassthrough": "ENABLED"},
                },
            }
        ],
    }
    # MediaConvert writes SUBTITLES into every variant of every manifest, so every manifest lists the tracks.
    assert all(m["SelectedOutputs"][-1] == "_cc-en" for m in additional_manifests(standard))
    subtitles = standard.with_captions([CaptionTrack(language="es", label="Spanish", kind=CaptionKind.SUBTITLES)])
    description = _output(compile_job_settings(subtitles), "_cc-es")["CaptionDescriptions"][0]
    assert description["DestinationSettings"]["ImscDestinationSettings"]["Accessibility"] == "DISABLED"


def test_jobs_define_every_caption_selector(standard: Ladder) -> None:
    plan = CaptionPlan(
        sources=(CaptionSource.provided(CaptionFile(uri="s3://in/bbb.en.srt", language="en"), language="en"),)
    )
    common = {"ladder": standard, "media_bucket": "media", "asset_id": "a", "correlation_id": "c", "app": "fast"}
    file = build_job_overrides(**common, source_uri="s3://in/bbb.mp4", generator=None, captions=plan)
    assert file.settings["Inputs"][0]["CaptionSelectors"] == {
        "Captions en": {
            "SourceSettings": {"SourceType": "SRT", "FileSourceSettings": {"SourceFile": "s3://in/bbb.en.srt"}}
        }
    }
    still = build_job_overrides(
        **common,
        source_uri=None,
        generator=GeneratorInput(image_s3_uri="s3://b/s.png", duration_gops=15, label="filler"),
    )
    assert still.settings["Inputs"][0]["CaptionSelectors"] == {
        "Captions en": {"SourceSettings": {"SourceType": "NULL_SOURCE"}}
    }
    bare = build_job_overrides(
        **common | {"ladder": standard.with_captions(())}, source_uri="s3://in/bbb.mp4", generator=None
    )
    assert "CaptionSelectors" not in bare.settings["Inputs"][0]


def test_caption_scan_reads_embedded_captions_as_ttml() -> None:
    settings = caption_scan_settings(source_uri="s3://in/bbb.mxf", destination="s3://in/work/a/c/scan")
    (source,) = settings["Inputs"]
    assert source["FileInput"] == "s3://in/bbb.mxf" and source["TimecodeSource"] == "ZEROBASED"
    embedded = {"SourceType": "EMBEDDED", "EmbeddedSourceSettings": {"Source608ChannelNumber": 1}}
    assert source["CaptionSelectors"] == {"Captions scan": {"SourceSettings": embedded}}
    assert "AudioSelectors" not in source
    (group,) = settings["OutputGroups"]
    assert group["OutputGroupSettings"] == {
        "Type": "FILE_GROUP_SETTINGS",
        "FileGroupSettings": {"Destination": "s3://in/work/a/c/scan"},
    }
    video, captions = group["Outputs"]
    # A caption output needs a video output in its group (an audio output does not count): a tiny picture.
    assert (video["NameModifier"], video["ContainerSettings"]["Container"]) == ("_scan", "MP4")
    assert (video["VideoDescription"]["Width"], video["VideoDescription"]["Height"]) == (256, 144)
    assert "AudioDescriptions" not in video
    assert captions["CaptionDescriptions"][0]["DestinationSettings"] == {"DestinationType": "TTML"}
    assert (captions["NameModifier"], captions["Extension"]) == ("_captions", "ttml")


def test_count_cues() -> None:
    empty = '<tt><head><styling><style xml:id="s"/></styling></head><body><div/></body></tt>'
    assert count_cues(empty) == 0
    assert count_cues('<tt><body><div><p begin="1s">Hello</p><p begin="2s">There</p></div></body></tt>') == 2
    assert count_cues("<tt:tt><tt:body><tt:div><tt:p>x</tt:p></tt:div></tt:body></tt:tt>") == 1
