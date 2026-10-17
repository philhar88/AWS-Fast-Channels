"""MediaConvert settings compiled from a ladder: one Apple HLS group of MPEG-TS outputs, plus per-job overrides.

Each output is one variant: an AVC rung with the AAC-LC track muxed into the same transport stream, so every variant
shares every segment boundary and MediaConvert writes a plain multivariant playlist without audio groups. Every
setting stays on the Basic pricing tier (AVC single-pass HQ with QVBR; no ESAM, ad markers, acceleration, audio
normalisation, captions or ID3). Frame rates are normalised to the ladder (``SPECIFIED`` + ``DUPLICATE_DROP``) so
GOPs, segments and slates share one grid. Each job sends only sparse overrides (Inputs, Destination): CreateJob
deep-merges them into the template (ARCHITECTURE-SPEC App. A).
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.naming import ladder_path
from fast_channels.core.tags import TAG_APP, TAG_LADDER

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder, VideoRendition

AUDIO_SELECTOR: Final = "Audio Selector 1"
LANGUAGE: Final = "en"  # BCP-47 "en", not MediaConvert's default "eng" (ladders.md §3.3 item 6)
GROUP_TYPE: Final = "HLS_GROUP_SETTINGS"


def _frame_rate(ladder: Ladder) -> dict[str, Any]:
    return {
        "FramerateControl": "SPECIFIED",
        "FramerateNumerator": ladder.frame_rate.numerator,
        "FramerateDenominator": ladder.frame_rate.denominator,
        "FramerateConversionAlgorithm": "DUPLICATE_DROP",
    }


def _h264(ladder: Ladder, r: VideoRendition) -> dict[str, Any]:
    max_bps = r.max_kbps * 1000
    return {
        "Codec": "H_264",
        "H264Settings": {
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
            "CodecProfile": r.profile,
            "CodecLevel": "AUTO",
            "EntropyEncoding": "CABAC",
            "Syntax": "DEFAULT",
        },
    }


def _audio(ladder: Ladder) -> dict[str, Any]:
    return {
        "AudioSourceName": AUDIO_SELECTOR,
        "LanguageCodeControl": "USE_CONFIGURED",
        "CustomLanguageCode": LANGUAGE,
        "CodecSettings": {
            "Codec": "AAC",
            "AacSettings": {
                "CodecProfile": "LC",
                "RateControlMode": "CBR",
                "Bitrate": ladder.audio.kbps * 1000,
                "CodingMode": "CODING_MODE_2_0",
                "SampleRate": 48000,
                "Specification": "MPEG4",
            },
        },
    }


def _output(ladder: Ladder, r: VideoRendition) -> dict[str, Any]:
    return {
        "NameModifier": r.name_modifier,
        "ContainerSettings": {
            "Container": "M3U8",
            "M3u8Settings": {
                # Channel Assembly needs equal-length variants; audio otherwise ends a few ms short of the video.
                "AudioDuration": "MATCH_VIDEO_DURATION",
                "AudioFramesPerPes": 4,
                "PcrControl": "PCR_EVERY_PES_PACKET",
                "PmtPid": 480,
                "VideoPid": 481,
                "AudioPids": [482],
                "Scte35Source": "NONE",
                "TimedMetadata": "NONE",
                "NielsenId3": "NONE",
            },
        },
        "OutputSettings": {"HlsSettings": {"IFrameOnlyManifest": "EXCLUDE"}},
        "VideoDescription": {
            "Width": r.width,
            "Height": r.height,
            "ScalingBehavior": "DEFAULT",
            "Sharpness": 50,
            "ColorMetadata": "INSERT",
            # NORMAL control leaves frames flagged progressive untouched; INTERPOLATE keeps it on the Basic tier.
            "VideoPreprocessors": {
                "Deinterlacer": {"Algorithm": "INTERPOLATE", "Mode": "DEINTERLACE", "Control": "NORMAL"}
            },
            "CodecSettings": _h264(ladder, r),
        },
        "AudioDescriptions": [_audio(ladder)],
    }


def compile_job_settings(ladder: Ladder) -> dict[str, Any]:
    """JobTemplate ``Settings`` for ``ladder`` (no Inputs, no Destination; the queue lives outside)."""
    return {
        "TimecodeConfig": {"Source": "ZEROBASED"},
        "FollowSource": 1,
        "OutputGroups": [
            {
                "Name": "Apple HLS",
                "CustomName": "hls",
                "OutputGroupSettings": {
                    "Type": GROUP_TYPE,
                    "HlsGroupSettings": {
                        "SegmentControl": "SEGMENTED_FILES",
                        "SegmentLength": int(ladder.segment_seconds),
                        # Closed GOPs on the grid make every segment exactly segment_gops GOPs long.
                        "SegmentLengthControl": "GOP_MULTIPLE",
                        "MinSegmentLength": 0,
                        "MinFinalSegmentLength": 1,
                        "ManifestDurationFormat": "FLOATING_POINT",
                        "CodecSpecification": "RFC_6381",
                        "StreamInfResolution": "INCLUDE",
                        "ClientCache": "ENABLED",
                        "ManifestCompression": "NONE",  # CloudFront compresses playlists (Apple 10.1)
                        "TargetDurationCompatibilityMode": "SPEC_COMPLIANT",  # TARGETDURATION 6 for 6.006 s segments
                        "OutputSelection": "MANIFESTS_AND_SEGMENTS",
                        "DirectoryStructure": "SINGLE_DIRECTORY",
                        "ProgramDateTime": "EXCLUDE",
                        "CaptionLanguageSetting": "NONE",  # CLOSED-CAPTIONS=NONE on every variant (Apple 433088)
                        "TimedMetadataId3Frame": "NONE",
                        "DestinationSettings": {"S3Settings": {"StorageClass": "STANDARD"}},
                    },
                },
                "Outputs": [_output(ladder, r) for r in ladder.video],
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
        "Settings": compile_job_settings(ladder),
        "Tags": {TAG_APP: category, TAG_LADDER: ladder.version},
    }


class GeneratorInput(BaseModel):
    """A still-image input (slate or house ad) rendered for exactly ``duration_gops`` GOPs of the ladder."""

    model_config = ConfigDict(extra="forbid")

    image_s3_uri: str = Field(pattern=r"^s3://[^/]+/.+\.(png|PNG|tga|TGA|bmp|BMP)$")
    duration_gops: int = Field(ge=1, le=43_200)
    label: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=48)


class JobOverrides(BaseModel):
    """Per-asset CreateJob parameters to send together with the ladder's JobTemplate."""

    settings: dict[str, Any]
    user_metadata: dict[str, str]
    destination_prefix: str
    snapped_cue_points_ms: list[int]


def output_prefix(asset_id: str, ladder_version: str) -> str:
    """S3 key prefix of an asset's encode: ``assets/<asset_id>/<ladder_version with @ -> _>/``."""
    return f"assets/{asset_id}/{ladder_path(ladder_version)}/"


def _input(ladder: Ladder, source_uri: str | None, generator: GeneratorInput | None) -> dict[str, Any]:
    audio: dict[str, Any] = {"AudioSelectors": {AUDIO_SELECTOR: {"DefaultSelection": "DEFAULT"}}}
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
    """Sparse job settings for one asset: its input and destination.

    Cue points are snapped to the segment grid, the only boundaries every variant shares on the Basic tier; the
    snapped values are what Channel Assembly ad breaks must use.
    """
    if (source_uri is None) == (generator is None):
        raise ValueError("exactly one of source_uri and generator is required")
    if generator is not None and cue_points_ms:
        raise ValueError("generated slates and house ads have no cue points")
    snapped = sorted({ladder.grid.snap_to_segment(ms) for ms in cue_points_ms} - {0})
    prefix = output_prefix(asset_id, ladder.version)
    settings: dict[str, Any] = {
        "Inputs": [_input(ladder, source_uri, generator)],
        "OutputGroups": [
            {
                "OutputGroupSettings": {
                    "Type": GROUP_TYPE,
                    "HlsGroupSettings": {"Destination": f"s3://{media_bucket}/{prefix}main"},
                }
            }
        ],
    }
    metadata = {"app": app, "asset_id": asset_id, "ladder": ladder.version, "correlation_id": correlation_id}
    if generator is not None:
        metadata["label"] = generator.label
    return JobOverrides(
        settings=settings, user_metadata=metadata, destination_prefix=prefix, snapped_cue_points_ms=snapped
    )
