"""MediaConvert settings compiled from a ladder: one CMAF group with every codec, plus per-job overrides.

The template reproduces the verified golden job (mediaconvert.md §9.4) field for field, with the v3 changes:
frame rate normalised to the ladder (``SPECIFIED`` + ``DUPLICATE_DROP``), an adaptive deinterlacer, loudness
normalisation, ``MpdManifestBandwidthType: MAX``, ``CustomLanguageCode``, I-frame playlists per option, an
``AdditionalManifests`` entry per applicable profile, and no ``Destination``. Each job sends only sparse overrides
(Inputs, Destination, optional Esam): CreateJob deep-merges them into the template (ARCHITECTURE-SPEC App. A).
"""

from collections.abc import Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.esam import build_esam
from fast_channels.core.ladder import AudioProfile, AudioRendition, Ladder, VideoCodec, VideoRendition
from fast_channels.core.naming import ladder_key
from fast_channels.core.profiles import applicable, selected_outputs

AUDIO_SELECTOR: Final = "Audio Selector 1"
LANGUAGE: Final = "en"  # BCP-47 "en", not MediaConvert's default "eng" (ladders.md §3.3 item 6)
STREAM_NAME: Final = "English"  # NAME in EXT-X-MEDIA; MediaConvert allows no hyphens here (mediaconvert.md §11)
_AAC_PROFILE: Final = {AudioProfile.LC: "LC", AudioProfile.HE_V1: "HEV1", AudioProfile.HE_V2: "HEV2"}


def _frame_rate(ladder: Ladder) -> dict[str, Any]:
    # Normalising every source to the ladder rate keeps GOPs, segments and slates on one grid (_review.md, media).
    return {
        "FramerateControl": "SPECIFIED",
        "FramerateNumerator": ladder.frame_rate.numerator,
        "FramerateDenominator": ladder.frame_rate.denominator,
        "FramerateConversionAlgorithm": "DUPLICATE_DROP",
    }


def _avc_hevc(ladder: Ladder, r: VideoRendition) -> dict[str, Any]:
    max_bps = r.max_kbps * 1000
    settings = {
        "GopSize": ladder.grid.gop_frames,
        "GopSizeUnits": "FRAMES",
        "GopClosedCadence": 1,
        "MinIInterval": 0,
        "SceneChangeDetect": "TRANSITION_DETECTION",
        "QualityTuningLevel": "SINGLE_PASS_HQ",
        "RateControlMode": "QVBR",
        "QvbrSettings": {"QvbrQualityLevel": r.qvbr},
        "MaxBitrate": max_bps,
        "HrdBufferSize": r.hrd_kbps * 1000 if r.hrd_kbps else 2 * max_bps,
        **_frame_rate(ladder),
        "ParControl": "INITIALIZE_FROM_SOURCE",
        "InterlaceMode": "PROGRESSIVE",
        "AdaptiveQuantization": "AUTO",
        "GopBReference": "ENABLED",
        "DynamicSubGop": "ADAPTIVE",
        "CodecProfile": r.codec_profile,
        "CodecLevel": "AUTO",
    }
    if r.codec is VideoCodec.AVC:
        return {"Codec": "H_264", "H264Settings": settings | {"EntropyEncoding": "CABAC", "Syntax": "DEFAULT"}}
    return {
        "Codec": "H_265",
        "H265Settings": settings
        | {
            "WriteMp4PackagingType": "HVC1",  # default HEV1 is "not recommended" by Apple (HLS 1.10)
            "AlternateTransferFunctionSei": "DISABLED",
            "TemporalIds": "DISABLED",
            "SampleAdaptiveOffsetFilterMode": "ADAPTIVE",
        },
    }


def _av1(ladder: Ladder, r: VideoRendition) -> dict[str, Any]:
    # AV1: QVBR only, GOP in frames only, no HRD/closed-GOP/scene-change controls (mediaconvert.md §2.2).
    return {
        "Codec": "AV1",
        "Av1Settings": {
            "RateControlMode": "QVBR",
            "QvbrSettings": {"QvbrQualityLevel": r.qvbr},
            "MaxBitrate": r.max_kbps * 1000,
            "GopSize": ladder.grid.gop_frames,
            "NumberBFramesBetweenReferenceFrames": 7,
            "BitDepth": f"BIT_{r.bit_depth}",
            **_frame_rate(ladder),
            "AdaptiveQuantization": "HIGH",
            "SpatialAdaptiveQuantization": "ENABLED",
            "FilmGrainSynthesis": "ENABLED" if ladder.options.film_grain else "DISABLED",
        },
    }


def _video_output(ladder: Ladder, r: VideoRendition) -> dict[str, Any]:
    codec = _av1(ladder, r) if r.codec is VideoCodec.AV1 else _avc_hevc(ladder, r)
    return {
        "NameModifier": r.name_modifier,
        "ContainerSettings": {
            "Container": "CMFC",
            "CmfcSettings": {
                "AudioRenditionSets": r.audio_group,
                "IFrameOnlyManifest": "INCLUDE" if ladder.options.iframe_playlists else "EXCLUDE",
            },
        },
        "VideoDescription": {
            "Width": r.width,
            "Height": r.height,
            "ScalingBehavior": "DEFAULT",
            "Sharpness": 50,
            "ColorMetadata": "INSERT",
            # NORMAL control leaves frames flagged progressive untouched; ADAPTIVE detects interlace/telecine.
            "VideoPreprocessors": {
                "Deinterlacer": {"Algorithm": "INTERPOLATE", "Mode": "ADAPTIVE", "Control": "NORMAL"}
            },
            "CodecSettings": codec,
        },
    }


def _audio_output(ladder: Ladder, a: AudioRendition) -> dict[str, Any]:
    description: dict[str, Any] = {
        "AudioSourceName": AUDIO_SELECTOR,
        "LanguageCodeControl": "USE_CONFIGURED",
        "CustomLanguageCode": LANGUAGE,
        "StreamName": STREAM_NAME,
        "CodecSettings": {
            "Codec": "AAC",
            "AacSettings": {
                "CodecProfile": _AAC_PROFILE[a.profile],
                "RateControlMode": "CBR",
                "Bitrate": a.kbps * 1000,
                "CodingMode": "CODING_MODE_2_0",
                "SampleRate": 48000,
                "Specification": "MPEG4",
            },
        },
    }
    if ladder.options.loudness_lkfs is not None:
        description["AudioNormalizationSettings"] = {
            "Algorithm": "ITU_BS_1770_3",
            "AlgorithmControl": "CORRECT_AUDIO",
            "TargetLkfs": ladder.options.loudness_lkfs,
            "PeakCalculation": "NONE",
            "LoudnessLogging": "DONT_LOG",
        }  # the default writes a *_loudness.csv per audio output
    return {
        "NameModifier": a.name_modifier,
        "ContainerSettings": {
            "Container": "CMFC",
            "CmfcSettings": {
                "AudioGroupId": a.group_id,
                "AudioTrackType": "ALTERNATE_AUDIO_AUTO_SELECT_DEFAULT",
                # Channel Assembly needs equal-length variants; audio otherwise ends ~18 ms short (ladders.md §3.3).
                "AudioDuration": "MATCH_VIDEO_DURATION",
            },
        },
        "AudioDescriptions": [description],
    }


def additional_manifests(ladder: Ladder) -> list[dict[str, Any]]:
    """One ``AdditionalManifests`` entry per distinct manifest suffix of the applicable profiles."""
    manifests: dict[str, list[str]] = {}
    for p in applicable(ladder):
        if p.manifest_suffix:
            manifests.setdefault(p.manifest_suffix, selected_outputs(ladder, p))
    return [{"ManifestNameModifier": suffix, "SelectedOutputs": outputs} for suffix, outputs in manifests.items()]


def compile_job_settings(ladder: Ladder) -> dict[str, Any]:
    """JobTemplate ``Settings`` for ``ladder`` (no Inputs, no Destination; Queue and acceleration live outside)."""
    return {
        "TimecodeConfig": {"Source": "ZEROBASED"},
        "FollowSource": 1,
        "OutputGroups": [
            {
                "Name": "CMAF",
                "CustomName": "cmaf_abr",
                "OutputGroupSettings": {
                    "Type": "CMAF_GROUP_SETTINGS",
                    "CmafGroupSettings": {
                        "SegmentControl": "SEGMENTED_FILES",
                        "SegmentLength": int(ladder.segment_seconds),
                        # Never MATCH: one 19.987 s segment at 29.97 fps, ERROR 1999 with AV1 (mediaconvert.md §8.7).
                        "SegmentLengthControl": "GOP_MULTIPLE",
                        "FragmentLength": int(ladder.gop_seconds),
                        "MinFinalSegmentLength": 1,
                        "WriteHlsManifest": "ENABLED",
                        "WriteDashManifest": "ENABLED",
                        "ManifestDurationFormat": "FLOATING_POINT",
                        "CodecSpecification": "RFC_6381",
                        "StreamInfResolution": "INCLUDE",
                        "ClientCache": "ENABLED",
                        "ManifestCompression": "NONE",  # CloudFront compresses playlists (Apple 10.1)
                        "MpdProfile": "MAIN_PROFILE",
                        "MpdManifestBandwidthType": "MAX",  # deterministic @bandwidth across assets
                        "DashManifestStyle": "BASIC",
                        "WriteSegmentTimelineInRepresentation": "ENABLED",
                        "PtsOffsetHandlingForBFrames": "ZERO_BASED",
                        "TargetDurationCompatibilityMode": "SPEC_COMPLIANT",  # TARGETDURATION 6 for 6.006 s segments
                        "VideoCompositionOffsets": "UNSIGNED",
                        "AdditionalManifests": additional_manifests(ladder),
                        "DestinationSettings": {"S3Settings": {"StorageClass": "STANDARD"}},
                    },
                },
                "Outputs": [
                    *(_video_output(ladder, r) for r in ladder.video),
                    *(_audio_output(ladder, a) for a in ladder.audio),
                ],
            }
        ],
    }


def job_template_props(ladder: Ladder, *, name: str, queue_arn: str, category: str) -> dict[str, Any]:
    """CreateJobTemplate request (API shape) for ``ladder``; ``category`` is the deployment prefix."""
    return {
        "Name": name,
        "Description": f"{ladder.version}: {ladder.description}" if ladder.description else ladder.version,
        "Category": category,
        "Queue": queue_arn,
        "Priority": 0,
        "StatusUpdateInterval": "SECONDS_60",  # fewer STATUS_UPDATE events in production (mediaconvert.md §7.2)
        "AccelerationSettings": {"Mode": ladder.options.acceleration},
        "Settings": compile_job_settings(ladder),
        "Tags": {"fast:app": category, "fast:ladder": ladder.version},
    }


class GeneratorInput(BaseModel):
    """A still-image input (slate or house ad) rendered for exactly ``duration_gops`` GOPs of the ladder."""

    model_config = ConfigDict(extra="forbid")

    image_s3_uri: str = Field(pattern=r"^s3://[^/]+/.+\.(png|PNG|tga|TGA|bmp|BMP)$")
    duration_gops: int = Field(ge=1, le=43_200)
    label: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=48)


class JobOverrides(BaseModel):
    """Per-asset CreateJob parameters to send together with the ladder's JobTemplate.

    ``acceleration`` overrides the template's mode when set (generator inputs can never be accelerated).
    """

    settings: dict[str, Any]
    user_metadata: dict[str, str]
    destination_prefix: str
    snapped_cue_points_ms: list[int]
    acceleration: Literal["PREFERRED", "ENABLED", "DISABLED"] | None = None


def output_prefix(asset_id: str, ladder_version: str) -> str:
    """S3 key prefix of an asset's encode: ``assets/<asset_id>/<ladder_version with @ -> _>/``."""
    return f"assets/{asset_id}/{ladder_key(ladder_version)}/"


def _input(ladder: Ladder, source_uri: str | None, generator: GeneratorInput | None) -> dict[str, Any]:
    audio = {"AudioSelectors": {AUDIO_SELECTOR: {"DefaultSelection": "DEFAULT"}}}
    if generator is None:
        return {
            "FileInput": source_uri,
            "TimecodeSource": "ZEROBASED",
            "VideoSelector": {"ColorSpace": "FOLLOW", "Rotate": "AUTO"},
            **audio,
        }
    top = max(ladder.video, key=lambda r: r.width * r.height)
    return {
        "VideoGenerator": {
            "Duration": ladder.grid.gop_exact(generator.duration_gops),  # frame-exact, e.g. 15 GOPs = 30030 ms
            "ImageInput": generator.image_s3_uri,
            "Width": top.width,
            "Height": top.height,
            "FramerateNumerator": ladder.frame_rate.numerator,
            "FramerateDenominator": ladder.frame_rate.denominator,
            "Channels": 2,
            "SampleRate": 48000,
        },
        "TimecodeSource": "ZEROBASED",
        **audio,
    }


def _needs_esam(ladder: Ladder, offsets_ms: Sequence[int]) -> bool:
    # Points on the natural segment grid already are boundaries on every track, so ESAM (Pro tier, 2 s split
    # segments, multi-period DASH) is only needed when some point is off it (_review.md, ESAM alignment).
    grid = ladder.grid
    return any(grid.gop_index(ms) % grid.segment_gops for ms in offsets_ms)


def build_job_overrides(
    *,
    ladder: Ladder,
    media_bucket: str,
    asset_id: str,
    source_uri: str | None,
    generator: GeneratorInput | None,
    cue_points_ms: Sequence[int] = (),
    correlation_id: str,
    app: str,
) -> JobOverrides:
    """Sparse job settings for one asset: input, destination and (only when needed) ESAM for its cue points.

    Cue points are snapped to the GOP grid; the snapped values are what Channel Assembly ad breaks must use.
    """
    if (source_uri is None) == (generator is None):
        raise ValueError("exactly one of source_uri and generator is required")
    if generator is not None and cue_points_ms:
        raise ValueError("generated slates and house ads have no cue points")
    grid = ladder.grid
    snapped = sorted({grid.snap_to_gop(ms) for ms in cue_points_ms} - {0})
    prefix = output_prefix(asset_id, ladder.version)
    settings: dict[str, Any] = {
        "Inputs": [_input(ladder, source_uri, generator)],
        "OutputGroups": [
            {
                "OutputGroupSettings": {
                    "Type": "CMAF_GROUP_SETTINGS",
                    "CmafGroupSettings": {"Destination": f"s3://{media_bucket}/{prefix}main"},
                }
            }
        ],
    }
    if _needs_esam(ladder, snapped):
        settings["Esam"] = build_esam(snapped)
    metadata = {"app": app, "asset_id": asset_id, "ladder": ladder.version, "correlation_id": correlation_id}
    if generator is not None:
        metadata["label"] = generator.label
    return JobOverrides(
        settings=settings,
        user_metadata=metadata,
        destination_prefix=prefix,
        snapped_cue_points_ms=snapped,
        acceleration="DISABLED" if generator is not None else None,
    )
