from fractions import Fraction
from typing import Any

import pytest
from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import ANY

from fast_channels.core.ladder import Ladder
from fast_channels.core.naming import digest
from fast_channels.functions.ingest import prepare
from fast_channels.functions.ingest.prepare import (
    IngestInProgressError,
    IngestRefusedError,
    Source,
    frame_rate_family,
    refusals,
    warnings,
)
from fast_channels.functions.media_library import to_dynamo

STANDARD = Ladder.builtin("standard")
SOURCE = "s3://ingest/movies/big_buck_bunny.mp4"
TEMPLATE = "fast-standard_" + STANDARD.fingerprint
EXECUTION = "arn:aws:states:us-east-1:123456789012:execution:fast-ingest:run"
OTHER = "arn:aws:states:us-east-1:123456789012:execution:fast-ingest:other"
_typed = TypeSerializer().serialize


def ladder_item(**overrides: Any) -> dict[str, Any]:
    item = {
        "pk": "LADDER#standard",
        "sk": f"V#{STANDARD.version}",
        "status": "READY",
        "job_template": TEMPLATE,
        "ladder": STANDARD.model_dump(mode="json"),
        **overrides,
    }
    return {"Item": {k: _typed(v) for k, v in to_dynamo({k: v for k, v in item.items() if v is not None}).items()}}


def probe_result(**video: Any) -> dict[str, Any]:
    properties = {"Width": 1920, "Height": 1080, "FrameRate": {"Numerator": 30000, "Denominator": 1001}, **video}
    return {
        "ProbeResults": [
            {
                "Container": {
                    "Duration": 596.4,
                    "Format": "mp4",
                    "Tracks": [
                        {"TrackType": "video", "Codec": "AVC", "VideoProperties": properties},
                        {"TrackType": "audio", "Codec": "AAC", "AudioProperties": {"Channels": 2}},
                    ],
                }
            }
        ]
    }


def expect_ladder(stubs: Any, response: dict[str, Any]) -> None:
    stubs["dynamodb"].add_response(
        "get_item",
        response,
        {"TableName": "library", "Key": {"pk": "LADDER#standard", "sk": f"V#{STANDARD.version}"}},
    )


def expect_claim(stubs: Any) -> None:
    stubs["dynamodb"].add_response("put_item", {}, {"TableName": "library", "Item": ANY, "ConditionExpression": ANY})


def source(**fields: Any) -> Source:
    defaults: dict[str, Any] = {
        "duration_ms": 60_000,
        "width": 1920,
        "height": 1080,
        "frame_rate": Fraction(30000, 1001),
        "hdr": False,
        "has_video": True,
        "has_audio": True,
    }
    return Source(**(defaults | fields))


@pytest.mark.parametrize(
    ("fps", "family"), [(23.976, 60), (24, 60), (29.97, 60), (30, 60), (59.94, 60), (25, 50), (50, 50), (12.5, 50)]
)
def test_frame_rate_families(fps: float, family: int) -> None:
    assert frame_rate_family(fps) == family


def test_refusals() -> None:
    assert refusals(source(), STANDARD) == []
    assert refusals(source(frame_rate=Fraction(24000, 1001)), STANDARD) == []
    assert refusals(source(frame_rate=Fraction(25)), STANDARD) == [
        "converting 25.000 fps to the ladder's 29.970 fps crosses the 50/60 Hz families (judder); use a ladder of "
        "the source's family or set allow_frame_rate_conversion"
    ]
    convertible = STANDARD.model_copy(
        update={"options": STANDARD.options.model_copy(update={"allow_frame_rate_conversion": True})}
    )
    assert refusals(source(frame_rate=Fraction(25)), convertible) == []
    assert len(refusals(source(hdr=True, has_audio=False, has_video=False), STANDARD)) == 3


def test_warnings() -> None:
    assert warnings(source(), STANDARD) == []
    assert warnings(source(height=720, frame_rate=Fraction(30)), STANDARD) == [
        "the source is 720 lines high; rungs above it are upscaled",
        "the frame rate is converted from 30.000 to 29.970",
    ]


@pytest.mark.parametrize(
    "video",
    [
        {"TransferCharacteristics": "SMPTE_2084"},
        {"CodecMetadata": {"TransferCharacteristics": "ARIB_B67"}},
        {"CodecMetadata": {"Hdr10PlusPresence": "PRESENT"}},
        {"CodecMetadata": {"DolbyVision": {"Rpu": "PRESENT"}}},
    ],
    ids=["pq", "hlg", "hdr10plus", "dolby-vision"],
)
def test_probe_detects_hdr(stubs: Any, video: dict[str, Any]) -> None:
    stubs["mediaconvert"].add_response("probe", probe_result(**video), {"InputFiles": [{"FileUrl": SOURCE}]})

    assert prepare.probe(SOURCE).hdr


def test_probe_reads_the_source(stubs: Any) -> None:
    stubs["mediaconvert"].add_response("probe", probe_result(), {"InputFiles": [{"FileUrl": SOURCE}]})

    assert prepare.probe(SOURCE) == Source(
        duration_ms=596_400,
        width=1920,
        height=1080,
        frame_rate=Fraction(30000, 1001),
        hdr=False,
        has_video=True,
        has_audio=True,
    )


def test_probe_refuses_unreadable_sources(stubs: Any) -> None:
    stubs["mediaconvert"].add_client_error("probe", "BadRequestException", "Unable to open the input file")

    with pytest.raises(IngestRefusedError, match="Unable to open"):
        prepare.probe(SOURCE)


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ({}, "not provisioned"),
        (ladder_item(status="PROPOSED"), "not provisioned"),
        (ladder_item(job_template=None), "not provisioned"),
        (ladder_item(ladder=Ladder.builtin("uhd").model_dump(mode="json")), "has version uhd@"),
    ],
    ids=["missing", "proposed", "no-template", "other-definition"],
)
def test_load_ladder_refusals(stubs: Any, item: dict[str, Any], message: str) -> None:
    expect_ladder(stubs, item)

    with pytest.raises(IngestRefusedError, match=message):
        prepare.load_ladder(STANDARD.version)


def test_load_ladder_falls_back_to_the_builtin_definition(stubs: Any) -> None:
    expect_ladder(stubs, ladder_item(ladder=None))

    assert prepare.load_ladder(STANDARD.version) == (STANDARD, TEMPLATE)


def test_handler_prepares_a_file(stubs: Any, lambda_context: Any) -> None:
    expect_ladder(stubs, ladder_item())
    stubs["mediaconvert"].add_response(
        "probe", probe_result(Height=720, Width=1280), {"InputFiles": [{"FileUrl": SOURCE}]}
    )
    expect_claim(stubs)
    request = {
        "source_uri": SOURCE,
        "ladder_version": STANDARD.version,
        "content_id": "bbb",
        "cue_points_ms": [298_000],
        "metadata": {"title": "Big Buck Bunny"},
    }

    result = prepare.handler(
        {"request": request, "execution": "big-buck-bunny-run", "execution_arn": EXECUTION}, lambda_context
    )

    assert result["asset_id"].startswith("big-buck-bunny-")
    assert result["correlation_id"] == "big-buck-bunny-run"
    assert result["title"] == "Big Buck Bunny"
    assert result["cue_points_ms"] == [298_298]  # snapped to the 2.002 s GOP grid: GOP 149
    assert result["warnings"] == ["the source is 720 lines high; rungs above it are upscaled"]
    assert result["estimated_cost_usd"] > 0
    assert result["output_prefix"] == f"assets/{result['asset_id']}/standard_{STANDARD.fingerprint}/"
    job = result["job"]
    assert job["JobTemplate"] == TEMPLATE
    assert job["AccelerationSettings"] == {"Mode": "PREFERRED"}
    assert job["ClientRequestToken"] == digest("big-buck-bunny-run", 32)
    assert job["Settings"]["Inputs"][0]["FileInput"] == SOURCE
    assert "Esam" in job["Settings"]  # GOP 149 is not on the 3-GOP segment grid
    assert job["UserMetadata"]["correlation_id"] == "big-buck-bunny-run"
    assert Ladder.model_validate(result["ladder"]) == STANDARD


def test_handler_prepares_a_generated_slate_without_probing(stubs: Any, lambda_context: Any) -> None:
    expect_ladder(stubs, ladder_item())
    expect_claim(stubs)
    request = {
        "generator": {"image_s3_uri": "s3://ingest/brand/abc/break.png", "duration_gops": 15, "label": "break-15"},
        "kind": "slate",
        "ladder_version": STANDARD.version,
        "correlation_id": "kit-run-break-15",
        "AWS_STEP_FUNCTIONS_STARTED_BY_EXECUTION_ID": "arn:aws:states:us-east-1:123456789012:execution:p:kit-run",
    }

    result = prepare.handler(
        {"request": request, "execution": "kit-run-break-15", "execution_arn": EXECUTION}, lambda_context
    )

    assert result["kind"] == "slate"
    assert result["label"] == "break-15"
    assert result["job"]["AccelerationSettings"] == {"Mode": "DISABLED"}
    assert result["job"]["Settings"]["Inputs"][0]["VideoGenerator"]["Duration"] == 30030
    assert result["estimated_cost_usd"] > 0


def test_handler_refuses_hdr_before_claiming(stubs: Any, lambda_context: Any) -> None:
    expect_ladder(stubs, ladder_item())
    stubs["mediaconvert"].add_response("probe", probe_result(TransferCharacteristics="SMPTE_2084"), None)

    with pytest.raises(IngestRefusedError, match="HDR"):
        prepare.handler(
            {"request": {"source_uri": SOURCE, "ladder_version": STANDARD.version}, "execution": "x"}, lambda_context
        )


def test_handler_refuses_invalid_requests(lambda_context: Any) -> None:
    with pytest.raises(IngestRefusedError, match="invalid ingest request"):
        prepare.handler({"request": {"ladder_version": STANDARD.version}, "execution": "x"}, lambda_context)


def claim_conflict(stubs: Any, holder_status: str | None, item_status: str = "INGESTING") -> None:
    """A claim that finds another execution's item; ``holder_status`` is that execution's (None: not asked)."""
    expect_ladder(stubs, ladder_item())
    stubs["mediaconvert"].add_response("probe", probe_result(), None)
    stubs["dynamodb"].add_client_error("put_item", "ConditionalCheckFailedException")
    holder = {
        "pk": "ASSET#x",
        "sk": "LADDER#y",
        "status": item_status,
        "correlation_id": "other",
        "execution_arn": OTHER,
    }
    stubs["dynamodb"].add_response("get_item", {"Item": {k: _typed(v) for k, v in holder.items()}}, None)
    if holder_status is None:
        return
    stubs["stepfunctions"].add_response(
        "describe_execution",
        {
            "executionArn": OTHER,
            "stateMachineArn": "arn:sm",
            "status": holder_status,
            "startDate": "2026-10-16T00:00:00Z",
        },
        {"executionArn": OTHER},
    )


def run_file(lambda_context: Any) -> dict[str, Any]:
    return prepare.handler(
        {
            "request": {"source_uri": SOURCE, "ladder_version": STANDARD.version},
            "execution": "x",
            "execution_arn": EXECUTION,
        },
        lambda_context,
    )


def test_handler_detects_a_concurrent_ingest(stubs: Any, lambda_context: Any) -> None:
    claim_conflict(stubs, "RUNNING")

    with pytest.raises(IngestInProgressError, match="already being ingested"):
        run_file(lambda_context)


def test_handler_takes_over_the_claim_of_an_ended_execution(stubs: Any, lambda_context: Any) -> None:
    claim_conflict(stubs, "ABORTED")
    stubs["dynamodb"].add_response("put_item", {}, {"TableName": "library", "Item": ANY, "ConditionExpression": ANY})

    assert run_file(lambda_context)["correlation_id"] == "x"


def test_handler_loses_a_race_for_an_ended_claim(stubs: Any, lambda_context: Any) -> None:
    claim_conflict(stubs, "TIMED_OUT")
    stubs["dynamodb"].add_client_error("put_item", "ConditionalCheckFailedException")

    with pytest.raises(IngestInProgressError):
        run_file(lambda_context)


def test_a_ready_encode_of_a_file_is_never_encoded_again_in_place(stubs: Any, lambda_context: Any) -> None:
    # A re-upload of the same key (or an Ingest Requested for an existing asset) would overwrite the manifests and
    # segments its VOD source serves.
    claim_conflict(stubs, holder_status=None, item_status="READY")

    with pytest.raises(IngestRefusedError, match=r"already READY .* upload the file under a new key"):
        run_file(lambda_context)


def test_a_stale_encoded_claim_of_a_file_is_refused_too(stubs: Any, lambda_context: Any) -> None:
    claim_conflict(stubs, "FAILED", item_status="ENCODED")

    with pytest.raises(IngestRefusedError, match="already ENCODED"):
        run_file(lambda_context)


def test_a_file_claims_only_a_missing_failed_or_own_item(stubs: Any, lambda_context: Any) -> None:
    expect_ladder(stubs, ladder_item())
    stubs["mediaconvert"].add_response("probe", probe_result(), None)
    condition = Attr("pk").not_exists() | Attr("status").is_in(["FAILED"]) | Attr("correlation_id").eq("x")
    stubs["dynamodb"].add_response(
        "put_item", {}, {"TableName": "library", "Item": ANY, "ConditionExpression": condition}
    )

    run_file(lambda_context)


def test_a_kit_still_replaces_its_previous_encode(stubs: Any, lambda_context: Any) -> None:
    # Kit stills are deterministic: their provisioner re-encodes them when the kit's other inputs change.
    expect_ladder(stubs, ladder_item())
    condition = (
        Attr("pk").not_exists()
        | Attr("status").is_in(["FAILED", "ENCODED", "READY"])
        | Attr("correlation_id").eq("kit-filler")
    )
    stubs["dynamodb"].add_response(
        "put_item", {}, {"TableName": "library", "Item": ANY, "ConditionExpression": condition}
    )
    generator = {"image_s3_uri": "s3://media/brand/filler.png", "duration_gops": 15, "label": "filler"}
    request = {
        "generator": generator,
        "kind": "slate",
        "ladder_version": STANDARD.version,
        "correlation_id": "kit-filler",
    }

    prepare.handler({"request": request, "execution": "kit-filler", "execution_arn": EXECUTION}, lambda_context)
