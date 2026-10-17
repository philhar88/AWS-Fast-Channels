from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from pydantic import ValidationError

from fast_channels.core.cost import encode_cost, encode_cost_per_minute
from fast_channels.core.ingest import IngestRequest, asset_id_for
from fast_channels.core.ladder import Ladder
from fast_channels.core.mediaconvert import build_job_overrides
from fast_channels.functions import aws
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import library
from fast_channels.functions.tools import library_store as store
from fast_channels.functions.tools.registry import Approval, ToolContext, ToolError, load

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

TABLE = "library-test"
INGEST = "ingest-bucket"
MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:fast-ingest"
NOW = "2026-10-16T12:00:00Z"
STANDARD = Ladder.builtin("standard")
LITE = "lite@5306b91dd0"  # another ladder version of the asset
CTX = ToolContext(tool="library___ingest", correlation_id="session-1")
SOURCE = f"s3://{INGEST}/mezzanine/big_buck_bunny.mov"
STARTED = datetime(2026, 10, 16, 12, 0, 1, tzinfo=UTC)
_serialize = TypeSerializer().serialize


def dynamo(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _serialize(v) for k, v in item.items()}


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in {
        "LIBRARY_TABLE": TABLE,
        "INGEST_BUCKET": INGEST,
        "INGEST_STATE_MACHINE_ARN": MACHINE,
        "DEFAULT_LADDER_VERSION": STANDARD.version,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(store, "now_iso", lambda: NOW)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def ddb() -> Iterator[Stubber]:
    with Stubber(aws.table(TABLE).meta.client) as stub:
        yield stub
        stub.assert_no_pending_responses()


@pytest.fixture
def sfn() -> Iterator[Stubber]:
    with Stubber(aws.client("stepfunctions")) as stub:
        yield stub
        stub.assert_no_pending_responses()


@pytest.fixture
def s3() -> Iterator[Stubber]:
    with Stubber(aws.client("s3")) as stub:
        yield stub
        stub.assert_no_pending_responses()


def asset(**changes: Any) -> store.Asset:
    fields: dict[str, Any] = {
        "asset_id": "bbb-1a2b3c",
        "ladder_version": STANDARD.version,
        "kind": "content",
        "status": "READY",
        "title": "Big Buck Bunny",
        "content_id": "bbb",
        "duration_ms": 596_462,
        "break_offsets_ms": [6006, 12012, 18018],
        "vod_source": "fast-bbb-1a2b3c",
        "correlation_id": "session-0-bbb-1a2b3c",
        "updated_at": NOW,
    }
    return store.Asset(**fields | changes)


def get(stub: Stubber, key: dict[str, str], item: dict[str, Any] | None) -> None:
    stub.add_response("get_item", {"Item": dynamo(item)} if item else {}, {"TableName": TABLE, "Key": key})


def versions(stub: Stubber, asset_id: str, assets: list[store.Asset]) -> None:
    expected = {"TableName": TABLE, "KeyConditionExpression": Key("pk").eq(f"ASSET#{asset_id}")}
    stub.add_response("query", {"Items": [dynamo(a.to_item()) for a in assets]}, expected)


def ladder(stub: Stubber, status: str = "READY", version: str = STANDARD.version) -> None:
    record = store.LadderRecord(ladder_version=version, status=status)
    get(stub, store.ladder_key(version), record.to_item())


# --- find_assets / get_asset ---------------------------------------------------------------------------------------


def test_find_assets_returns_compact_rows(ddb: Stubber) -> None:
    more = asset(asset_id="sintel-4d5e6f")
    response = {"Items": [dynamo(asset().to_item()), dynamo(more.to_item())]}
    ddb.add_response(
        "query",
        response,
        {
            "TableName": TABLE,
            "IndexName": "GSI1",
            "KeyConditionExpression": Key("gsi1pk").eq(f"KIND#content#{STANDARD.version}"),
            "ScanIndexForward": False,
        },
    )
    result = library.find_assets(library.FindAssetsInput(kind="content", ladder_version=STANDARD.version, limit=1), CTX)
    (row,) = result.assets
    assert row.model_dump() == {
        "asset_id": "bbb-1a2b3c",
        "kind": "content",
        "ladder_version": STANDARD.version,
        "status": "READY",
        "owner": "pipeline",
        "title": "Big Buck Bunny",
        "content_id": "bbb",
        "duration_ms": 596_462,
        "break_offset_count": 3,
        "vod_source": "fast-bbb-1a2b3c",
        "updated_at": NOW,
    }
    token = store.decode_token(result.next_token, store.GSI1_KEYS)
    assert token is not None and token["pk"] == "ASSET#bbb-1a2b3c"


def test_get_asset_prefers_the_default_ladder(ddb: Stubber) -> None:
    newer_lite = asset(ladder_version=LITE, updated_at="2026-10-17T00:00:00Z")
    versions(ddb, "bbb-1a2b3c", [newer_lite, asset()])
    detail = library.get_asset(library.GetAssetInput(asset_id="bbb-1a2b3c"), CTX)
    assert detail.ladder_version == STANDARD.version
    assert detail.break_offsets_ms == [6006, 12012, 18018]
    assert detail.correlation_id == "session-0-bbb-1a2b3c"
    assert detail.other_ladder_versions == [LITE]


def test_get_asset_falls_back_to_the_latest_encode(ddb: Stubber) -> None:
    old, new = (
        asset(ladder_version=LITE),
        asset(ladder_version="x@0000000000", updated_at="2027-01-01T00:00:00Z"),
    )
    versions(ddb, "bbb-1a2b3c", [old, new])
    assert library.get_asset(library.GetAssetInput(asset_id="bbb-1a2b3c"), CTX).ladder_version == "x@0000000000"


def test_get_asset_with_an_explicit_ladder(ddb: Stubber) -> None:
    versions(ddb, "bbb-1a2b3c", [asset(), asset(ladder_version=LITE)])
    request = library.GetAssetInput(asset_id="bbb-1a2b3c", ladder_version=LITE)
    assert library.get_asset(request, CTX).other_ladder_versions == [STANDARD.version]


def test_get_asset_errors(ddb: Stubber) -> None:
    versions(ddb, "nope", [])
    with pytest.raises(ToolError) as raised:
        library.get_asset(library.GetAssetInput(asset_id="nope"), CTX)
    assert raised.value.code == "NOT_FOUND"

    versions(ddb, "bbb-1a2b3c", [asset()])
    with pytest.raises(ToolError) as raised:
        library.get_asset(library.GetAssetInput(asset_id="bbb-1a2b3c", ladder_version=LITE), CTX)
    assert raised.value.code == "NOT_FOUND" and STANDARD.version in raised.value.hint


# --- ingest --------------------------------------------------------------------------------------------------------


def test_ingest_requires_approval() -> None:
    assert load("library")["ingest"].approval is Approval.CONFIRM


def test_ingest_starts_the_pipeline_named_after_the_session(ddb: Stubber, s3: Stubber, sfn: Stubber) -> None:
    asset_id = asset_id_for(SOURCE)
    name = f"session-1-{asset_id}"
    payload = IngestRequest(
        source_uri=SOURCE,
        asset_id=asset_id,
        content_id="bbb",
        cue_points_ms=[300_000],
        ladder_version=STANDARD.version,
        correlation_id=name,
        metadata={"agent_session": "session-1", "title": "Big Buck Bunny"},
    )
    ladder(ddb)
    get(ddb, store.asset_key(asset_id, STANDARD.version), None)
    s3.add_response("head_object", {"ContentLength": 10}, {"Bucket": INGEST, "Key": "mezzanine/big_buck_bunny.mov"})
    sfn.add_response(
        "start_execution",
        {"executionArn": library.execution_arn(MACHINE, name), "startDate": STARTED},
        {"stateMachineArn": MACHINE, "name": name, "input": payload.model_dump_json(exclude_none=True)},
    )
    request = library.IngestInput(
        source_uri=SOURCE, content_id="bbb", title="Big Buck Bunny", cue_points_ms=[300_000], duration_ms=596_462
    )
    result = library.ingest(request, CTX)
    assert result.model_dump() == {
        "asset_id": asset_id,
        "ladder_version": STANDARD.version,
        "kind": "content",
        "execution_name": name,
        "execution_arn": f"arn:aws:states:us-east-1:123456789012:execution:fast-ingest:{name}",
        "started_at": "2026-10-16T12:00:01.000Z",
        # 300 s is between boundaries of the 6.006 s segment grid: ESAM splits there, at the Professional tier.
        "pricing_tier": "PROFESSIONAL",
        "cost_per_minute_usd": encode_cost_per_minute(STANDARD, professional=True),
        "estimated_cost_usd": encode_cost(STANDARD, 596_462, professional=True),
    }


@pytest.mark.parametrize(
    ("cue_points", "tier"),
    [([6006, 300_310], "BASIC"), ([6006, 13_480], "PROFESSIONAL")],
    ids=["segment-boundaries", "one-between-boundaries"],
)
def test_ingest_prices_esam_as_the_pipeline_will(ddb: Stubber, sfn: Stubber, cue_points: list[int], tier: str) -> None:
    source = "https://cdn.example.com/film.mp4"
    ladder(ddb)
    get(ddb, store.asset_key(asset_id_for(source), STANDARD.version), None)
    sfn.add_response(
        "start_execution",
        {"executionArn": library.execution_arn(MACHINE, f"session-1-{asset_id_for(source)}"), "startDate": STARTED},
        {"stateMachineArn": MACHINE, "name": f"session-1-{asset_id_for(source)}", "input": ANY},
    )
    request = library.IngestInput(source_uri=source, cue_points_ms=cue_points, duration_ms=60_000)

    result = library.ingest(request, CTX)

    professional = build_job_overrides(
        ladder=STANDARD,
        media_bucket="media",
        asset_id="film",
        source_uri=source,
        generator=None,
        cue_points_ms=cue_points,
        correlation_id="run",
        app="fast",
    ).professional
    assert result.pricing_tier == tier == ("PROFESSIONAL" if professional else "BASIC")
    assert result.estimated_cost_usd == encode_cost(STANDARD, 60_000, professional=professional)


def test_ingest_retries_failed_assets_and_skips_the_s3_check_for_https(ddb: Stubber, sfn: Stubber) -> None:
    source = "https://cdn.example.com/ad.mp4"
    asset_id = asset_id_for(source)
    ladder(ddb, status="PROVISIONING")
    get(ddb, store.asset_key(asset_id, STANDARD.version), asset(asset_id=asset_id, status="FAILED").to_item())
    sfn.add_response(
        "start_execution",
        {"executionArn": library.execution_arn(MACHINE, f"session-1-{asset_id}"), "startDate": STARTED},
        {"stateMachineArn": MACHINE, "name": f"session-1-{asset_id}", "input": ANY},
    )
    result = library.ingest(library.IngestInput(source_uri=source, kind="ad"), CTX)
    assert result.kind == "ad" and result.estimated_cost_usd is None
    assert (result.pricing_tier, result.cost_per_minute_usd) == ("BASIC", encode_cost_per_minute(STANDARD))


@pytest.mark.parametrize(
    ("status", "code"),
    [(None, "NOT_FOUND"), ("PROPOSED", "CONFLICT"), ("FAILED", "CONFLICT")],
)
def test_ingest_needs_a_ladder_with_a_job_template(ddb: Stubber, status: str | None, code: str) -> None:
    if status is None:
        get(ddb, store.ladder_key(STANDARD.version), None)
    else:
        ladder(ddb, status=status)
    with pytest.raises(ToolError) as raised:
        library.ingest(library.IngestInput(source_uri=SOURCE), CTX)
    assert raised.value.code == code


def test_ingest_refuses_assets_already_in_the_library(ddb: Stubber) -> None:
    ladder(ddb)
    get(ddb, store.asset_key(asset_id_for(SOURCE), STANDARD.version), asset(status="INGESTING").to_item())
    with pytest.raises(ToolError) as raised:
        library.ingest(library.IngestInput(source_uri=SOURCE), CTX)
    assert raised.value.code == "CONFLICT" and "INGESTING" in raised.value.message


def test_ingest_refuses_sources_outside_the_ingest_bucket(ddb: Stubber) -> None:
    ladder(ddb)
    get(ddb, store.asset_key(asset_id_for("s3://other/x.mp4"), STANDARD.version), None)
    with pytest.raises(ToolError) as raised:
        library.ingest(library.IngestInput(source_uri="s3://other/x.mp4"), CTX)
    assert raised.value.code == "VALIDATION" and INGEST in raised.value.hint


@pytest.mark.parametrize(("error", "code"), [("404", "NOT_FOUND"), ("403", "FORBIDDEN")])
def test_ingest_checks_the_source_object(ddb: Stubber, s3: Stubber, error: str, code: str) -> None:
    ladder(ddb)
    get(ddb, store.asset_key(asset_id_for(SOURCE), STANDARD.version), None)
    s3.add_client_error("head_object", service_error_code=error, http_status_code=int(error))
    with pytest.raises(ToolError) as raised:
        library.ingest(library.IngestInput(source_uri=SOURCE), CTX)
    assert raised.value.code == code


def test_ingest_reports_a_repeated_ingest_in_the_same_session(ddb: Stubber, s3: Stubber, sfn: Stubber) -> None:
    ladder(ddb)
    get(ddb, store.asset_key(asset_id_for(SOURCE), STANDARD.version), None)
    s3.add_response("head_object", {})
    sfn.add_client_error("start_execution", service_error_code="ExecutionAlreadyExists")
    with pytest.raises(ToolError) as raised:
        library.ingest(library.IngestInput(source_uri=SOURCE), CTX)
    assert raised.value.code == "CONFLICT" and "get_ingest_status" in raised.value.hint


def test_ingest_without_a_default_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFAULT_LADDER_VERSION")
    settings.cache_clear()
    with pytest.raises(ToolError) as raised:
        library.ingest(library.IngestInput(source_uri=SOURCE), CTX)
    assert raised.value.code == "VALIDATION"


def test_ingest_validates_through_the_core_request(ddb: Stubber) -> None:
    ladder(ddb)
    with pytest.raises(ValidationError, match="cue points must not be negative"):
        library.ingest(library.IngestInput(source_uri=SOURCE, cue_points_ms=[-1]), CTX)


# --- get_ingest_status ---------------------------------------------------------------------------------------------


def _execution(name: str, status: str, request: IngestRequest | None, **extra: Any) -> dict[str, Any]:
    return {
        "executionArn": library.execution_arn(MACHINE, name),
        "stateMachineArn": MACHINE,
        "name": name,
        "status": status,
        "startDate": STARTED,
        "input": request.model_dump_json() if request else '{"detail-type": "Object Created"}',
        **extra,
    }


def test_status_by_execution_name_resolves_the_asset(ddb: Stubber, sfn: Stubber) -> None:
    request = IngestRequest(source_uri=SOURCE, asset_id="bbb-1a2b3c", ladder_version=STANDARD.version)
    sfn.add_response(
        "describe_execution",
        _execution("session-1-bbb-1a2b3c", "RUNNING", request),
        {"executionArn": library.execution_arn(MACHINE, "session-1-bbb-1a2b3c")},
    )
    get(ddb, store.asset_key("bbb-1a2b3c", STANDARD.version), asset(status="INGESTING").to_item())
    status = library.get_ingest_status(library.IngestStatusInput(execution_name="session-1-bbb-1a2b3c"), CTX)
    assert status.execution is not None and status.execution.status == "RUNNING"
    assert status.asset is not None and status.asset.status == "INGESTING"
    assert status.done is False


def test_status_by_asset_id_follows_its_correlation_id(ddb: Stubber, sfn: Stubber) -> None:
    get(ddb, store.asset_key("bbb-1a2b3c", STANDARD.version), asset(status="FAILED", error="Probe failed").to_item())
    cause = "x" * 5000
    sfn.add_response(
        "describe_execution",
        _execution("session-0-bbb-1a2b3c", "FAILED", None, stopDate=STARTED, error="States.TaskFailed", cause=cause),
        {"executionArn": library.execution_arn(MACHINE, "session-0-bbb-1a2b3c")},
    )
    status = library.get_ingest_status(library.IngestStatusInput(asset_id="bbb-1a2b3c"), CTX)
    assert status.execution is not None
    assert (status.execution.error, len(status.execution.cause or "")) == ("States.TaskFailed", 1000)
    assert status.execution.stopped_at == "2026-10-16T12:00:01.000Z"
    assert status.done is True


def test_status_of_an_asset_whose_execution_is_gone(ddb: Stubber, sfn: Stubber) -> None:
    get(ddb, store.asset_key("bbb-1a2b3c", STANDARD.version), asset().to_item())
    sfn.add_client_error("describe_execution", service_error_code="ExecutionDoesNotExist")
    status = library.get_ingest_status(library.IngestStatusInput(asset_id="bbb-1a2b3c"), CTX)
    assert status.execution is None and status.done is True


def test_status_of_s3_triggered_runs_and_unknown_ingests(ddb: Stubber, sfn: Stubber) -> None:
    sfn.add_response("describe_execution", _execution("s3-run", "SUCCEEDED", None))
    status = library.get_ingest_status(library.IngestStatusInput(execution_name="s3-run"), CTX)
    assert status.asset is None and status.done is True

    sfn.add_client_error("describe_execution", service_error_code="ExecutionDoesNotExist")
    with pytest.raises(ToolError) as raised:
        library.get_ingest_status(library.IngestStatusInput(execution_name="nope"), CTX)
    assert raised.value.code == "NOT_FOUND"

    get(ddb, store.asset_key("nope", STANDARD.version), None)
    with pytest.raises(ToolError):
        library.get_ingest_status(library.IngestStatusInput(asset_id="nope"), CTX)


def test_status_needs_an_asset_or_execution() -> None:
    with pytest.raises(ValidationError, match="asset_id or execution_name"):
        library.IngestStatusInput()


def test_unexpected_aws_errors_reach_the_dispatcher(ddb: Stubber, s3: Stubber, sfn: Stubber) -> None:
    for _ in range(2):
        ladder(ddb)
        get(ddb, store.asset_key(asset_id_for(SOURCE), STANDARD.version), None)
    s3.add_client_error("head_object", service_error_code="500", http_status_code=500)
    s3.add_response("head_object", {})
    sfn.add_client_error("start_execution", service_error_code="StateMachineDoesNotExist")
    sfn.add_client_error("describe_execution", service_error_code="InvalidArn")
    for call in (
        lambda: library.ingest(library.IngestInput(source_uri=SOURCE), CTX),
        lambda: library.ingest(library.IngestInput(source_uri=SOURCE), CTX),
        lambda: library.get_ingest_status(library.IngestStatusInput(execution_name="x"), CTX),
    ):
        with pytest.raises(ClientError):
            call()


def test_lambda_handler_reports_invalid_arguments() -> None:
    context = SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "library___ingest"}),
        aws_request_id="req-1",
        function_name="library",
        function_version="1",
        invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:library",
        memory_limit_in_mb=512,
        log_group_name="g",
        log_stream_name="s",
        get_remaining_time_in_millis=lambda: 1000,
    )
    result = library.handler({"source_uri": "ftp://x"}, cast("LambdaContext", context))
    assert result["ok"] is False and result["error"]["code"] == "VALIDATION"
