"""Generated captions: a source that speaks (Amazon Polly) and carries no captions. The caption scan finds none in
the video, the stage's captioner (Elemental Inference smart subtitles) transcribes it, and the encode reads the
generated TTML into the ladder's English IMSC track. Skipped on stages that never generate captions."""

import re
from typing import TYPE_CHECKING

import pytest
from _stack import Plane, Stage
from harness import SPOKEN_ASSET_ID

from fast_channels.core.captions import CaptionOrigin
from fast_channels.core.manifests import subtitle_renditions
from fast_channels.core.mediaconvert import output_prefix
from fast_channels.functions.tools.library_store import Asset, AssetStatus

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

pytestmark = pytest.mark.e2e

WORDS = ("captions", "generated", "audio")
"""Words of harness.SPEECH a transcription cannot miss."""


def test_the_spoken_language_was_captioned(stage: Stage, spoken: Asset) -> None:
    assert spoken.status is AssetStatus.READY
    english = next(c for c in spoken.captions if c.language == "en")
    assert english.origin is CaptionOrigin.GENERATED, spoken.captions
    assert english.engine == stage.output(Plane.MEDIA, "CaptionEngine")
    assert english.uri and english.uri.startswith(f"s3://{stage.output(Plane.DATA, 'IngestBucketName')}/captions/")


def test_the_generated_file_transcribes_the_speech(stage: Stage, spoken: Asset) -> None:
    english = next(c for c in spoken.captions if c.language == "en")
    bucket, _, key = str(english.uri).removeprefix("s3://").partition("/")
    s3 = stage.session.client("s3", region_name=stage.region)
    text = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode().lower()
    assert all(word in text for word in WORDS), text
    first_cue = re.search(r'<p begin="(\d\d):(\d\d):(\d\d)\.(\d{3})"', text)
    assert first_cue and 1.5 <= int(first_cue[3]) + int(first_cue[4]) / 1000 <= 4, text  # speech starts at 2 s


def test_the_encode_carries_the_generated_captions(stage: Stage, spoken: Asset, ladder: Ladder) -> None:
    s3 = stage.session.client("s3", region_name=stage.region)
    bucket, prefix = stage.output(Plane.DATA, "MediaBucketName"), output_prefix(SPOKEN_ASSET_ID, ladder.version)

    def read(key: str) -> bytes:
        return bytes(s3.get_object(Bucket=bucket, Key=f"{prefix}{key}")["Body"].read())

    main = read("main.m3u8").decode()
    english = next(a["URI"] for a in subtitle_renditions(main) if a["LANGUAGE"] == "en")
    segments = [line for line in read(english).decode().splitlines() if line and not line.startswith("#")]
    imsc = b"".join(read(segment) for segment in segments).replace(b"&#xA0;", b" ").decode(errors="replace").lower()
    assert any(word in imsc for word in WORDS), imsc[:2000]
