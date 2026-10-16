from typing import Any

import pytest

from fast_channels.core.captions import CaptionPlan, CaptionPolicy, CaptionsRequest, SourceCaptions, plan_captions
from fast_channels.core.ladder import Ladder
from fast_channels.core.mediaconvert import build_job_overrides
from fast_channels.functions.ingest import captions
from fast_channels.functions.ingest.captions import CaptionsMissingError
from fast_channels.functions.ingest.prepare import IngestRefusedError

STANDARD = Ladder.builtin("standard")
SOURCE = "s3://ingest/movies/bbb.mp4"
SCAN = "s3://ingest/work/bbb-1a2b3c/run-1/scan"
TTML = '<tt><body><div><p begin="00:00:01.000" end="00:00:02.500">Hello.</p></div></body></tt>'
EMPTY = "<tt><body><div/></body></tt>"


def asset(plan: CaptionPlan) -> dict[str, Any]:
    overrides = build_job_overrides(
        ladder=STANDARD,
        media_bucket="media",
        asset_id="bbb-1a2b3c",
        source_uri=SOURCE,
        generator=None,
        correlation_id="run-1",
        app="fast",
    )
    return {
        "asset_id": "bbb-1a2b3c",
        "ladder_version": STANDARD.version,
        "correlation_id": "run-1",
        "source_uri": SOURCE,
        "ladder": STANDARD.model_dump(mode="json"),
        "job": {"Settings": overrides.settings},
        "captions": plan.model_dump(mode="json"),
        "caption_scan": {"captions_uri": f"{SCAN}_captions.ttml"},
    }


def awaiting_scan(**policy: bool) -> CaptionPlan:
    return plan_captions(
        STANDARD.captions, CaptionsRequest(), SourceCaptions(audio_language="en"), CaptionPolicy(**policy)
    )


@pytest.fixture
def objects(store: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """The ingest bucket's objects, behind ``captions.S3Store``."""
    memory = store()
    monkeypatch.setattr(captions, "S3Store", lambda bucket: memory if bucket == "ingest" else pytest.fail(bucket))
    memory.bucket = "ingest"
    return memory.objects  # type: ignore[no-any-return]


def selector(result: dict[str, Any]) -> dict[str, Any]:
    return result["job"]["Settings"]["Inputs"][0]["CaptionSelectors"]["Captions en"]["SourceSettings"]


def test_embedded_captions_found_by_the_scan_are_read_by_the_encode(
    objects: dict[str, bytes], lambda_context: Any
) -> None:
    objects["work/bbb-1a2b3c/run-1/scan_captions.ttml"] = TTML.encode()

    result = captions.handler({"step": "scanned", "asset": asset(awaiting_scan())}, lambda_context)

    assert selector(result) == {"SourceType": "EMBEDDED", "EmbeddedSourceSettings": {"Source608ChannelNumber": 1}}
    assert result["captions"]["awaiting"] is None and "captioner_input" not in result


def test_without_embedded_captions_the_captioner_gets_the_source(
    objects: dict[str, bytes], lambda_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAPTIONER", "elemental-inference")
    objects["work/bbb-1a2b3c/run-1/scan_captions.ttml"] = EMPTY.encode()

    result = captions.handler({"step": "scanned", "asset": asset(awaiting_scan(can_generate=True))}, lambda_context)

    assert result["captions"]["awaiting"] == "generation"
    assert result["captioner_input"] == {
        "asset_id": "bbb-1a2b3c",
        "language": "en",
        "source_uri": SOURCE,
        "output_uri": "s3://ingest/captions/bbb-1a2b3c/run-1/en",
        "correlation_id": "run-1",
    }
    assert selector(result) == {"SourceType": "NULL_SOURCE"}  # Prepare's placeholder, until generated

    objects["captions/bbb-1a2b3c/run-1/en.ttml"] = TTML.encode()
    output = {"uri": "s3://ingest/captions/bbb-1a2b3c/run-1/en.ttml", "format": "ttml", "engine": "elemental-inference"}
    done = captions.handler({"step": "generated", "asset": result, "result": output}, lambda_context)

    assert selector(done) == {
        "SourceType": "TTML",
        "FileSourceSettings": {"SourceFile": "s3://ingest/captions/bbb-1a2b3c/run-1/en.ttml"},
    }
    assert "captioner_input" not in done
    assert done["captions"]["sources"][0] == {
        "language": "en",
        "origin": "generated",
        "uri": output["uri"],
        "format": "ttml",
        "offset_ms": 0,
        "frame_rate": None,
        "carriage": None,
        "engine": "elemental-inference",
    }


def test_a_failed_generation_leaves_the_track_empty_unless_required(
    objects: dict[str, bytes], lambda_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAPTIONER", "elemental-inference")
    waiting = awaiting_scan(can_generate=True).model_copy(update={"awaiting": "generation"})
    error = {"Error": "UnsupportedLanguageError", "Cause": "smart subtitles do not transcribe ja"}

    result = captions.handler({"step": "failed", "asset": asset(waiting), "error": error}, lambda_context)

    assert selector(result) == {"SourceType": "NULL_SOURCE"}
    assert result["captions"]["notes"][-1].startswith("caption generation failed: UnsupportedLanguageError")
    monkeypatch.setenv("CAPTIONS_REQUIRED", "true")
    captions.settings.cache_clear()
    with pytest.raises(IngestRefusedError, match="captions are required"):
        captions.handler({"step": "failed", "asset": asset(waiting), "error": error}, lambda_context)


def test_missing_files_fail_the_step(objects: dict[str, bytes], lambda_context: Any) -> None:
    with pytest.raises(CaptionsMissingError, match="wrote no"):
        captions.handler({"step": "scanned", "asset": asset(awaiting_scan())}, lambda_context)
    waiting = awaiting_scan(can_generate=True).model_copy(update={"awaiting": "generation"})
    outside = {"uri": "s3://media/x.ttml", "format": "ttml", "engine": "elemental-inference"}
    with pytest.raises(CaptionsMissingError, match="not in the ingest bucket"):
        captions.handler({"step": "generated", "asset": asset(waiting), "result": outside}, lambda_context)
    empty = {"uri": "s3://ingest/captions/x.ttml", "format": "ttml", "engine": "elemental-inference"}
    objects["captions/x.ttml"] = b""
    with pytest.raises(CaptionsMissingError, match="missing or empty"):
        captions.handler({"step": "generated", "asset": asset(waiting), "result": empty}, lambda_context)
    with pytest.raises(ValueError, match="unknown step"):
        captions.handler({"step": "guess", "asset": asset(waiting)}, lambda_context)
