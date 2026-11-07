"""The analysis proxy: a small MP4 of a title, made by MediaConvert from the title's own HLS encode.

Rekognition Video and Transcribe read MP4 (not MPEG-TS), and a video model reads at most 1 GB from S3, so the
workflow encodes one proxy per title at 640 by 360 from the encode's smallest variant that is at least that tall (the
smallest one otherwise). MediaConvert reads HLS with MPEG-TS segments as an input. The proxy bills at the Basic tier
(SD, single pass) and expires a day after it was made.
"""

from typing import Any, Final

from fast_channels.core.manifests import Variant, parse_multivariant

WIDTH: Final = 640
HEIGHT: Final = 360
MAX_BITRATE: Final = 800_000
"""QVBR ceiling: an hour of proxy is at most about 360 MB, well inside a video model's 1 GB S3 limit."""
AUDIO_BITRATE: Final = 96_000
PROXY_NAME: Final = "proxy"
"""The file name MediaConvert gives the output (``<destination>proxy.mp4``)."""


def analysis_variant(multivariant: str) -> str:
    """The URI (as written in the playlist) of the smallest variant at least :data:`HEIGHT` tall, or of the smallest.

    Raises:
        ValueError: the playlist lists no variant.
    """
    variants = parse_multivariant(multivariant)
    if not variants:
        raise ValueError("the multivariant playlist lists no variant")

    def height(variant: Variant) -> int:
        return variant.resolution[1] if variant.resolution else 0

    tall = [v for v in variants if height(v) >= HEIGHT]
    return min(tall or variants, key=lambda v: (height(v), v.bandwidth)).uri


def proxy_job(*, input_uri: str, destination: str, metadata: dict[str, str]) -> dict[str, Any]:
    """MediaConvert ``CreateJob`` ``Settings`` and ``UserMetadata`` of a title's proxy: ``input_uri`` is the variant's
    media playlist (``s3://...m3u8``), ``destination`` the output prefix (``s3://bucket/proxies/<id>/<asset>/``)."""
    return {
        "Settings": {
            "TimecodeConfig": {"Source": "ZEROBASED"},
            "Inputs": [
                {
                    "FileInput": input_uri,
                    "TimecodeSource": "ZEROBASED",
                    "AudioSelectors": {"Audio Selector 1": {"DefaultSelection": "DEFAULT"}},
                    "VideoSelector": {},
                }
            ],
            "OutputGroups": [
                {
                    "Name": "Proxy",
                    "OutputGroupSettings": {
                        "Type": "FILE_GROUP_SETTINGS",
                        "FileGroupSettings": {"Destination": f"{destination}{PROXY_NAME}"},
                    },
                    "Outputs": [
                        {
                            "ContainerSettings": {
                                "Container": "MP4",
                                "Mp4Settings": {"MoovPlacement": "PROGRESSIVE_DOWNLOAD"},
                            },
                            "VideoDescription": {
                                "Width": WIDTH,
                                "Height": HEIGHT,
                                "ScalingBehavior": "DEFAULT",
                                "CodecSettings": {
                                    "Codec": "H_264",
                                    "H264Settings": {
                                        "RateControlMode": "QVBR",
                                        "QvbrSettings": {"QvbrQualityLevel": 6},
                                        "MaxBitrate": MAX_BITRATE,
                                        "CodecProfile": "MAIN",
                                        "SceneChangeDetect": "ENABLED",
                                        "QualityTuningLevel": "SINGLE_PASS",
                                    },
                                },
                            },
                            "AudioDescriptions": [
                                {
                                    "AudioSourceName": "Audio Selector 1",
                                    "CodecSettings": {
                                        "Codec": "AAC",
                                        "AacSettings": {
                                            "Bitrate": AUDIO_BITRATE,
                                            "CodingMode": "CODING_MODE_2_0",
                                            "SampleRate": 48_000,
                                        },
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        "UserMetadata": dict(metadata),
    }
