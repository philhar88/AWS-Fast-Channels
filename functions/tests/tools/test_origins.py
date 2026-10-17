import io
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from pydantic import ValidationError

from fast_channels.core.ladder import Ladder
from fast_channels.core.manifests import expected_signature
from fast_channels.functions import aws
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import library_store as store
from fast_channels.functions.tools import origins
from fast_channels.functions.tools.registry import Approval, ToolContext, ToolError, load

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

REPO = Path(__file__).resolve().parents[3]
SAMPLES = REPO / "core/tests/data/standard-job/src25i"  # a real STANDARD encode: 10 s, ESAM split at 4004 ms
TABLE = "library-test"
PREFIX = "fast"
REGION = "us-east-1"
ACCOUNT = "123456789012"
BUCKET = "acme-media"
LOCATION = "fast-agent-acme"
LOCATION_ARN = f"arn:aws:mediatailor:{REGION}:{ACCOUNT}:sourceLocation/{LOCATION}"
BASE_URL = f"https://{BUCKET}.s3.{REGION}.amazonaws.com"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/fast-origins-role"
NOW = "2026-10-16T12:00:00Z"
WHEN = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
STANDARD = Ladder.builtin("standard").with_captions(())  # the external content fixtures have no captions
VOD_SOURCE = f"fast-agent-pilot-{STANDARD.fingerprint}"
VOD_ARN = f"arn:aws:mediatailor:{REGION}:{ACCOUNT}:vodSource/{LOCATION}/{VOD_SOURCE}"
CTX = ToolContext(tool="origins___register_vod_source", correlation_id="session-1")
MPD = b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"/>'
TAGS = {"fast:app": PREFIX, "fast:owner": "agent", "fast:correlation-id": "session-1"}
_serialize = TypeSerializer().serialize


def dynamo(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _serialize(v) for k, v in item.items()}


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in {
        "FAST_PREFIX": PREFIX,
        "LIBRARY_TABLE": TABLE,
        "MEDIA_BUCKET": "fast-media",
        "SOURCE_LOCATION": "fast-origin",
        "PACKAGE_PROFILES": "hls,hls-avc,hls-hevc,hls-av1,dash-avc",
        "DEFAULT_LADDER_VERSION": STANDARD.version,
        "AWS_REGION": REGION,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(store, "now_iso", lambda: NOW)
    settings.cache_clear()
    yield
    settings.cache_clear()


def _stub(client: Any) -> Iterator[Stubber]:
    with Stubber(client) as stub:
        yield stub
        stub.assert_no_pending_responses()


@pytest.fixture
def mt() -> Iterator[Stubber]:
    yield from _stub(aws.client("mediatailor"))


@pytest.fixture
def s3() -> Iterator[Stubber]:
    yield from _stub(aws.client("s3"))


@pytest.fixture
def sts() -> Iterator[Stubber]:
    yield from _stub(aws.client("sts"))


@pytest.fixture
def ddb() -> Iterator[Stubber]:
    yield from _stub(aws.table(TABLE).meta.client)


def location(name: str = LOCATION, base_url: str = BASE_URL, **extra: Any) -> dict[str, Any]:
    return {
        "SourceLocationName": name,
        "Arn": f"arn:aws:mediatailor:{REGION}:{ACCOUNT}:sourceLocation/{name}",
        "HttpConfiguration": {"BaseUrl": base_url},
        **extra,
    }


def get(ddb: Stubber, key: dict[str, str], item: dict[str, Any] | None) -> None:
    ddb.add_response("get_item", {"Item": dynamo(item)} if item else {}, {"TableName": TABLE, "Key": key})


def ladder(ddb: Stubber) -> None:
    get(
        ddb,
        store.ladder_key(STANDARD.version),
        store.LadderRecord(ladder_version=STANDARD.version, status="READY", ladder=STANDARD).to_item(),
    )


def body(data: bytes) -> dict[str, Any]:
    return {"Body": StreamingBody(io.BytesIO(data), len(data)), "ContentLength": len(data)}


def serve(s3: Stubber, *files: str | tuple[str, bytes], folder: str = "shows/pilot") -> None:
    """Stub GetObject for sample files (or (name, bytes) pairs) under ``folder``, in call order."""
    for file in files:
        name, data = file if isinstance(file, tuple) else (file, (SAMPLES / file).read_bytes())
        s3.add_response("get_object", body(data), {"Bucket": BUCKET, "Key": f"{folder}/{name}"})


TIMELINE = (  # first video playlist per codec, then every audio rendition (encode_timeline)
    "main_avc-720p-a.m3u8",
    "main_hevc-1080p.m3u8",
    "main_av1-1080p.m3u8",
    "main_aac-lc-128.m3u8",
    "main_aac-he1-64.m3u8",
    "main_aac-he2-32.m3u8",
)
PACKAGES = [
    {"source_group": "hls", "path": "/shows/pilot/main.m3u8"},
    {"source_group": "dash-avc", "path": "/shows/pilot/main_avc.mpd"},
]
CONFIGURATIONS = [
    {"Path": "/shows/pilot/main.m3u8", "SourceGroup": "hls", "Type": "HLS"},
    {"Path": "/shows/pilot/main_avc.mpd", "SourceGroup": "dash-avc", "Type": "DASH"},
]


def alert(code: str, category: str, minute: int) -> dict[str, Any]:
    return {
        "AlertCode": code,
        "AlertMessage": f"{code} happened",
        "Category": category,
        "LastModifiedTime": WHEN.replace(minute=minute),
        "RelatedResourceArns": [],
        "ResourceArn": VOD_ARN,
    }


def test_confirm_tools() -> None:
    tools = load("origins")
    confirmed = {name for name, spec in tools.items() if spec.approval is Approval.CONFIRM}
    assert confirmed == {"create_source_location", "update_package_configurations"}


# --- list_source_locations -----------------------------------------------------------------------------------------


def test_list_source_locations_classifies_owners(mt: Stubber) -> None:
    items = [
        location("fast-origin", "https://fast-media.s3.us-east-1.amazonaws.com", CreationTime=WHEN),
        location(
            LOCATION,
            AccessConfiguration={"AccessType": "S3_SIGV4"},
            DefaultSegmentDeliveryConfiguration={"BaseUrl": "https://cdn.acme.com"},
        ),
        location("other", "https://origin.example.com/vod"),
    ]
    mt.add_response("list_source_locations", {"Items": items}, {})
    rows = origins.list_source_locations(origins.ListSourceLocationsInput(), CTX).source_locations
    assert [(r.name, r.owner, r.bucket) for r in rows] == [
        ("fast-origin", "iac", "fast-media"),
        (LOCATION, "agent", BUCKET),
        ("other", "external", None),
    ]
    assert rows[0].created_at == "2026-10-16T12:00:00.000Z"
    assert (rows[1].access_type, rows[1].segment_delivery_url) == ("S3_SIGV4", "https://cdn.acme.com")


# --- create_source_location ----------------------------------------------------------------------------------------


def _create_input(**changes: str) -> origins.CreateSourceLocationInput:
    fields = {"name": "acme", "bucket": BUCKET, "segment_delivery_url": "https://cdn.acme.com"}
    return origins.CreateSourceLocationInput(**fields | changes)


def test_create_source_location_returns_the_bucket_policy(mt: Stubber, sts: Stubber) -> None:
    mt.add_client_error("describe_source_location", "NotFoundException", http_status_code=404)
    created = location(DefaultSegmentDeliveryConfiguration={"BaseUrl": "https://cdn.acme.com"})
    mt.add_response(
        "create_source_location",
        created,
        {
            "SourceLocationName": LOCATION,
            "HttpConfiguration": {"BaseUrl": BASE_URL},
            "AccessConfiguration": {"AccessType": "S3_SIGV4"},
            "DefaultSegmentDeliveryConfiguration": {"BaseUrl": "https://cdn.acme.com"},
            "Tags": TAGS,
        },
    )
    sts.add_response(
        "get_caller_identity",
        {"UserId": "AROA:fn", "Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/fast-origins-role/fn"},
    )
    result = origins.create_source_location(_create_input(), CTX)
    assert (result.name, result.arn, result.base_url, result.created) == (LOCATION, LOCATION_ARN, BASE_URL, True)
    assert result.bucket_policy_statements == origins.bucket_policy_statements(
        bucket=BUCKET, source_location_arn=LOCATION_ARN, reader_arn=ROLE_ARN
    )


def test_create_source_location_is_idempotent_per_bucket(mt: Stubber, sts: Stubber) -> None:
    mt.add_response("describe_source_location", location(), {"SourceLocationName": LOCATION})
    sts.add_response("get_caller_identity", {"Arn": f"arn:aws:iam::{ACCOUNT}:user/dev"})
    result = origins.create_source_location(_create_input(), CTX)
    assert result.created is False
    assert result.bucket_policy_statements[1]["Principal"] == {"AWS": f"arn:aws:iam::{ACCOUNT}:user/dev"}


def test_create_source_location_refuses_name_clashes_and_the_media_bucket(mt: Stubber) -> None:
    mt.add_response("describe_source_location", location(base_url="https://other.s3.us-east-1.amazonaws.com"))
    with pytest.raises(ToolError) as raised:
        origins.create_source_location(_create_input(), CTX)
    assert raised.value.code == "CONFLICT"
    with pytest.raises(ToolError) as raised:
        origins.create_source_location(_create_input(bucket="fast-media"), CTX)
    assert raised.value.code == "VALIDATION" and "fast-origin" in raised.value.hint


def test_create_source_location_rejects_dotted_buckets() -> None:
    with pytest.raises(ValidationError):
        _create_input(bucket="acme.media")


def test_bucket_policy_statements() -> None:
    mediatailor, tools = origins.bucket_policy_statements(
        bucket=BUCKET, source_location_arn=LOCATION_ARN, reader_arn=ROLE_ARN
    )
    assert mediatailor == {
        "Sid": "MediaTailorReadFastAgentAcme",
        "Effect": "Allow",
        "Principal": {"Service": "mediatailor.amazonaws.com"},
        "Action": "s3:GetObject",
        "Resource": f"arn:aws:s3:::{BUCKET}/*",
        "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}, "ArnLike": {"aws:SourceArn": LOCATION_ARN}},
    }
    assert tools == {
        "Sid": "FastChannelsToolsReadFastAgentAcme",
        "Effect": "Allow",
        "Principal": {"AWS": ROLE_ARN},
        "Action": "s3:GetObject",
        "Resource": f"arn:aws:s3:::{BUCKET}/*",
    }


# --- register_vod_source -------------------------------------------------------------------------------------------


def _register(**changes: Any) -> origins.RegisterVodSourceInput:
    fields: dict[str, Any] = {"asset_id": "pilot", "source_location": LOCATION, "packages": PACKAGES, "title": "Pilot"}
    return origins.RegisterVodSourceInput(**fields | changes)


def _prelude(ddb: Stubber, mt: Stubber) -> None:
    ladder(ddb)
    get(ddb, store.asset_key("pilot", STANDARD.version), None)
    mt.add_response("describe_source_location", location(), {"SourceLocationName": LOCATION})


def test_register_vod_source_verifies_the_ladder_and_computes_the_timeline(
    ddb: Stubber, mt: Stubber, s3: Stubber
) -> None:
    _prelude(ddb, mt)
    serve(s3, "main.m3u8", ("main_avc.mpd", MPD), *TIMELINE)
    mt.add_response(
        "create_vod_source",
        {"Arn": VOD_ARN, "VodSourceName": VOD_SOURCE},
        {
            "SourceLocationName": LOCATION,
            "VodSourceName": VOD_SOURCE,
            "HttpPackageConfigurations": CONFIGURATIONS,
            "Tags": TAGS | {"fast:ladder": STANDARD.version},
        },
    )
    expected = store.Asset(
        asset_id="pilot",
        ladder_version=STANDARD.version,
        kind="content",
        status="READY",
        owner="agent",
        title="Pilot",
        duration_ms=10_010,
        break_offsets_ms=[4004],  # the HE-AACv2 boundary trails video by 135 ms: within tolerance
        signature=[list(row) for row in expected_signature(STANDARD)],
        vod_source=VOD_SOURCE,
        source_location=LOCATION,
        packages={"hls": "/shows/pilot/main.m3u8", "dash-avc": "/shows/pilot/main_avc.mpd"},
        correlation_id="session-1",
        updated_at=NOW,
    )
    ddb.add_response(
        "put_item",
        {},
        {"TableName": TABLE, "Item": expected.to_item(), "ConditionExpression": Attr("pk").not_exists()},
    )
    mt.add_response("list_alerts", {"Items": [alert("NOT_PROCESSED", "INFO", 1)]}, {"ResourceArn": VOD_ARN})

    result = origins.register_vod_source(_register(), CTX)
    assert (result.vod_source, result.vod_source_arn, result.duration_ms, result.break_offset_count) == (
        VOD_SOURCE,
        VOD_ARN,
        10_010,
        1,
    )
    assert result.missing_source_groups == ["hls-avc", "hls-hevc", "hls-av1"]
    assert [a.code for a in result.alerts] == ["NOT_PROCESSED"]


def test_register_refuses_ladder_mismatches(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    _prelude(ddb, mt)
    serve(s3, "main.m3u8")
    with pytest.raises(ToolError) as raised:
        origins.register_vod_source(
            _register(packages=[{"source_group": "hls-avc", "path": "/shows/pilot/main.m3u8"}]), CTX
        )
    assert raised.value.code == "LADDER_MISMATCH"
    assert "16 variants but the ladder has 8" in raised.value.message


def test_register_names_the_first_differing_variant(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    _prelude(ddb, mt)
    shuffled = (SAMPLES / "main_avc.m3u8").read_text().replace("RESOLUTION=1280x720", "RESOLUTION=1280x704", 1)
    serve(s3, ("main_avc.m3u8", shuffled.encode()))
    with pytest.raises(ToolError) as raised:
        origins.register_vod_source(
            _register(packages=[{"source_group": "hls-avc", "path": "/shows/pilot/main_avc.m3u8"}]), CTX
        )
    assert "variant 1 is avc/1280x704/29.970/aac-lc-128 but the ladder has avc/1280x720" in raised.value.message


@pytest.mark.parametrize(
    ("served", "message"),
    [
        (("main.m3u8", b"<MPD/>"), "not an HLS multivariant"),
        (("main.m3u8", b"#EXTM3U\n#EXT-X-VERSION:6\n"), "not an HLS multivariant"),
    ],
)
def test_register_checks_manifest_formats(
    ddb: Stubber, mt: Stubber, s3: Stubber, served: tuple[str, bytes], message: str
) -> None:
    _prelude(ddb, mt)
    serve(s3, served)
    with pytest.raises(ToolError, match=message):
        origins.register_vod_source(_register(), CTX)


def test_register_checks_dash_manifests(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    _prelude(ddb, mt)
    serve(s3, "main.m3u8", ("main_avc.mpd", b"#EXTM3U"))
    with pytest.raises(ToolError, match="not a DASH MPD"):
        origins.register_vod_source(_register(), CTX)


def test_register_needs_complete_vod_playlists(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    _prelude(ddb, mt)
    live = (SAMPLES / "main_avc-720p-a.m3u8").read_text().replace("#EXT-X-ENDLIST\n", "")
    serve(s3, "main.m3u8", ("main_avc.mpd", MPD), ("main_avc-720p-a.m3u8", live.encode()))
    with pytest.raises(ToolError, match="EXT-X-ENDLIST"):
        origins.register_vod_source(_register(), CTX)


def test_register_refuses_absolute_child_uris(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    _prelude(ddb, mt)
    text = (SAMPLES / "main.m3u8").read_text().replace("\nmain_avc-720p-a.m3u8", "\nhttps://x/main_avc-720p-a.m3u8")
    serve(s3, ("main.m3u8", text.encode()), ("main_avc.mpd", MPD))
    with pytest.raises(ToolError, match="relative"):
        origins.register_vod_source(_register(), CTX)


@pytest.mark.parametrize(("code", "expected"), [("NoSuchKey", "NOT_FOUND"), ("AccessDenied", "FORBIDDEN")])
def test_register_reports_unreadable_manifests(
    ddb: Stubber, mt: Stubber, s3: Stubber, code: str, expected: str
) -> None:
    _prelude(ddb, mt)
    s3.add_client_error("get_object", service_error_code=code, http_status_code=404 if code == "NoSuchKey" else 403)
    with pytest.raises(ToolError) as raised:
        origins.register_vod_source(_register(), CTX)
    assert raised.value.code == expected


def test_register_refuses_oversized_manifests(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    _prelude(ddb, mt)
    s3.add_response("get_object", {"Body": StreamingBody(io.BytesIO(b""), 0), "ContentLength": 3 * 1024 * 1024})
    with pytest.raises(ToolError, match="2 MB"):
        origins.register_vod_source(_register(), CTX)


def test_register_refuses_non_s3_and_unknown_source_locations(ddb: Stubber, mt: Stubber) -> None:
    ladder(ddb)
    get(ddb, store.asset_key("pilot", STANDARD.version), None)
    mt.add_response("describe_source_location", location(base_url="https://origin.example.com"))
    with pytest.raises(ToolError) as raised:
        origins.register_vod_source(_register(), CTX)
    assert raised.value.code == "VALIDATION"

    ladder(ddb)
    get(ddb, store.asset_key("pilot", STANDARD.version), None)
    mt.add_client_error("describe_source_location", "NotFoundException", http_status_code=404)
    with pytest.raises(ToolError) as raised:
        origins.register_vod_source(_register(), CTX)
    assert raised.value.code == "NOT_FOUND"


def test_register_refuses_existing_assets_and_foreign_groups(ddb: Stubber) -> None:
    ladder(ddb)
    get(
        ddb,
        store.asset_key("pilot", STANDARD.version),
        {**store.asset_key("pilot", STANDARD.version), "kind": "content", "status": "READY"},
    )
    with pytest.raises(ToolError) as raised:
        origins.register_vod_source(_register(), CTX)
    assert raised.value.code == "CONFLICT"

    ladder(ddb)
    with pytest.raises(ToolError) as raised:
        origins.register_vod_source(_register(packages=[{"source_group": "hls-sd", "path": "/a/main_sd.m3u8"}]), CTX)
    assert raised.value.code == "VALIDATION" and "hls-sd" in raised.value.message


def test_register_needs_an_hls_package(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    _prelude(ddb, mt)
    serve(s3, ("main_avc.mpd", MPD))
    with pytest.raises(ToolError, match="HLS package"):
        origins.register_vod_source(_register(packages=PACKAGES[1:]), CTX)


@pytest.mark.parametrize(
    "packages",
    [
        [],
        [PACKAGES[0], PACKAGES[0]],
        [{"source_group": "hls-4k", "path": "/a.m3u8"}],
        [{"source_group": "hls", "path": "shows/a.m3u8"}],
    ],
)
def test_register_input_validation(packages: list[dict[str, str]]) -> None:
    with pytest.raises(ValidationError):
        _register(packages=packages)


def _registered_up_to_create(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    _prelude(ddb, mt)
    serve(s3, "main.m3u8", ("main_avc.mpd", MPD), *TIMELINE)


def test_register_adopts_an_identical_vod_source(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    _registered_up_to_create(ddb, mt, s3)
    mt.add_client_error("create_vod_source", "BadRequestException", "A vod-source with this name already exists")
    mt.add_response(
        "describe_vod_source",
        {"Arn": VOD_ARN, "HttpPackageConfigurations": list(reversed(CONFIGURATIONS))},
        {"SourceLocationName": LOCATION, "VodSourceName": VOD_SOURCE},
    )
    ddb.add_response("put_item", {})
    mt.add_response("list_alerts", {"Items": []})
    assert origins.register_vod_source(_register(), CTX).vod_source_arn == VOD_ARN


def test_register_conflicts_with_a_different_vod_source(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    _registered_up_to_create(ddb, mt, s3)
    mt.add_client_error("create_vod_source", "BadRequestException", "A vod-source with this name already exists")
    mt.add_response("describe_vod_source", {"Arn": VOD_ARN, "HttpPackageConfigurations": CONFIGURATIONS[:1]})
    with pytest.raises(ToolError) as raised:
        origins.register_vod_source(_register(), CTX)
    assert raised.value.code == "CONFLICT"


def test_register_explains_missing_bucket_policies(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    _registered_up_to_create(ddb, mt, s3)
    message = "Verify that EMT has the right IAM permissions to access the bucket"  # verified live
    mt.add_client_error("create_vod_source", "BadRequestException", message)
    with pytest.raises(ToolError) as raised:
        origins.register_vod_source(_register(), CTX)
    assert (raised.value.code, raised.value.retryable) == ("FORBIDDEN", True)
    assert "create_source_location" in raised.value.hint


def test_register_passes_other_errors_to_the_dispatcher(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    _registered_up_to_create(ddb, mt, s3)
    mt.add_client_error("create_vod_source", "TooManyRequestsException", "Rate exceeded")
    with pytest.raises(Exception, match="Rate exceeded"):
        origins.register_vod_source(_register(), CTX)


# --- update_package_configurations ---------------------------------------------------------------------------------


def _asset(**changes: Any) -> store.Asset:
    fields: dict[str, Any] = {
        "asset_id": "bbb-1a2b3c",
        "ladder_version": STANDARD.version,
        "kind": "content",
        "status": "READY",
        "vod_source": "fast-bbb-1a2b3c",
        "updated_at": NOW,
    }
    return store.Asset(**fields | changes)


def test_update_derives_pipeline_manifest_paths(ddb: Stubber, mt: Stubber) -> None:
    pipeline = _asset()
    get(ddb, pipeline.key(), pipeline.to_item())
    ladder(ddb)
    base = f"/assets/bbb-1a2b3c/standard_{STANDARD.fingerprint}"
    current = [
        {"Path": f"{base}/main.m3u8", "SourceGroup": "hls", "Type": "HLS"},
        {"Path": f"{base}/main_av1.m3u8", "SourceGroup": "hls-av1", "Type": "HLS"},
    ]
    mt.add_response(
        "describe_vod_source",
        {"Arn": VOD_ARN, "HttpPackageConfigurations": current},
        {"SourceLocationName": "fast-origin", "VodSourceName": "fast-bbb-1a2b3c"},
    )
    wanted = [current[0], {"Path": f"{base}/main_avc.mpd", "SourceGroup": "dash-avc", "Type": "DASH"}]
    mt.add_response(
        "update_vod_source",
        {"Arn": VOD_ARN},
        {"SourceLocationName": "fast-origin", "VodSourceName": "fast-bbb-1a2b3c", "HttpPackageConfigurations": wanted},
    )
    paths = {"hls": f"{base}/main.m3u8", "dash-avc": f"{base}/main_avc.mpd"}
    ddb.add_response("put_item", {}, {"TableName": TABLE, "Item": _asset(packages=paths).to_item()})
    mt.add_response("list_alerts", {"Items": [alert("BANDWIDTH_MISMATCH", "PLAYBACK_WARNING", 1)]})

    request = origins.UpdatePackagesInput(
        asset_id="bbb-1a2b3c", packages=[{"source_group": "hls"}, {"source_group": "dash-avc"}]
    )
    result = origins.update_package_configurations(request, CTX)
    assert (result.source_groups, result.added, result.removed, result.alerts) == (
        ["hls", "dash-avc"],
        ["dash-avc"],
        ["hls-av1"],
        [],
    )


def test_update_verifies_new_manifests_of_external_content(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    external = _asset(
        asset_id="pilot",
        owner="agent",
        source_location=LOCATION,
        vod_source=VOD_SOURCE,
        packages={"hls": "/shows/pilot/main.m3u8"},
    )
    get(ddb, external.key(), external.to_item())
    ladder(ddb)
    mt.add_response("describe_source_location", location(), {"SourceLocationName": LOCATION})
    serve(s3, "main_avc.m3u8")
    mt.add_response("describe_vod_source", {"Arn": VOD_ARN, "HttpPackageConfigurations": CONFIGURATIONS[:1]})
    wanted = [CONFIGURATIONS[0], {"Path": "/shows/pilot/main_avc.m3u8", "SourceGroup": "hls-avc", "Type": "HLS"}]
    mt.add_response(
        "update_vod_source",
        {"Arn": VOD_ARN},
        {"SourceLocationName": LOCATION, "VodSourceName": VOD_SOURCE, "HttpPackageConfigurations": wanted},
    )
    ddb.add_response("put_item", {})
    mt.add_response("list_alerts", {"Items": []})
    request = origins.UpdatePackagesInput(
        asset_id="pilot",
        packages=[{"source_group": "hls"}, {"source_group": "hls-avc", "path": "/shows/pilot/main_avc.m3u8"}],
    )
    assert origins.update_package_configurations(request, CTX).added == ["hls-avc"]


def test_update_needs_paths_it_cannot_derive(ddb: Stubber) -> None:
    external = _asset(owner="agent", packages={"hls": "/a/main.m3u8"})
    get(ddb, external.key(), external.to_item())
    ladder(ddb)
    request = origins.UpdatePackagesInput(asset_id="bbb-1a2b3c", packages=[{"source_group": "hls-hevc"}])
    with pytest.raises(ToolError, match="no hls-hevc manifest"):
        origins.update_package_configurations(request, CTX)


def test_update_needs_an_asset_with_a_vod_source(ddb: Stubber) -> None:
    ad = _asset(kind="ad", vod_source=None)
    get(ddb, ad.key(), ad.to_item())
    with pytest.raises(ToolError) as raised:
        origins.update_package_configurations(
            origins.UpdatePackagesInput(asset_id="bbb-1a2b3c", packages=[{"source_group": "hls"}]), CTX
        )
    assert raised.value.code == "NOT_FOUND"


# --- list_alerts ---------------------------------------------------------------------------------------------------


def test_list_alerts_filters_noise_and_orders_by_severity(mt: Stubber) -> None:
    described = {"Arn": "arn:channel/fast-demo", "LogConfiguration": {"LogTypes": []}}
    mt.add_response("describe_channel", described, {"ChannelName": "fast-demo"})
    items = [
        alert("SEGMENT_DURATION_MISMATCH", "PLAYBACK_WARNING", 9),
        alert("NOT_PROCESSED", "INFO", 8),
        alert("CODEC_MISMATCH", "PLAYBACK_WARNING", 3),
        alert("NO_COMMON_SEGMENT_BOUNDARY_FOR_AD_SLATE", "SCHEDULING_ERROR", 1),
        alert("UNAUTHORIZED", "SCHEDULING_ERROR", 5),
    ]
    mt.add_response("list_alerts", {"Items": items[:3], "NextToken": "t"}, {"ResourceArn": "arn:channel/fast-demo"})
    mt.add_response("list_alerts", {"Items": items[3:]}, {"ResourceArn": "arn:channel/fast-demo", "NextToken": "t"})
    result = origins.list_alerts(origins.ListAlertsInput(channel="fast-demo", limit=3), CTX)
    assert [a.code for a in result.alerts] == [
        "UNAUTHORIZED",
        "NO_COMMON_SEGMENT_BOUNDARY_FOR_AD_SLATE",
        "CODEC_MISMATCH",
    ]
    assert (result.scheduling_errors, result.suppressed, result.truncated) == (2, 1, True)
    assert result.alerts[0].last_modified == "2026-10-16T12:05:00.000Z"


def test_list_alerts_for_vod_sources_and_source_locations(mt: Stubber) -> None:
    mt.add_response(
        "describe_vod_source", {"Arn": VOD_ARN}, {"SourceLocationName": "fast-origin", "VodSourceName": "v"}
    )
    mt.add_response("list_alerts", {"Items": [alert("BANDWIDTH_MISMATCH", "PLAYBACK_WARNING", 1)]})
    noisy = origins.list_alerts(origins.ListAlertsInput(vod_source="v", include_noise=True), CTX)
    assert ([a.code for a in noisy.alerts], noisy.suppressed) == (["BANDWIDTH_MISMATCH"], 0)

    mt.add_response("describe_source_location", location(), {"SourceLocationName": LOCATION})
    mt.add_response("list_alerts", {"Items": []}, {"ResourceArn": LOCATION_ARN})
    assert origins.list_alerts(origins.ListAlertsInput(source_location=LOCATION), CTX).resource_arn == LOCATION_ARN


@pytest.mark.parametrize("fields", [{}, {"channel": "c", "vod_source": "v"}, {"channel": "c", "source_location": "s"}])
def test_list_alerts_needs_exactly_one_resource(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        origins.ListAlertsInput(**fields)


def test_unexpected_aws_errors_reach_the_dispatcher(ddb: Stubber, mt: Stubber, s3: Stubber) -> None:
    mt.add_client_error("describe_source_location", "AccessDeniedException", http_status_code=403)
    with pytest.raises(ClientError):
        origins.list_alerts(origins.ListAlertsInput(source_location=LOCATION), CTX)

    _prelude(ddb, mt)
    s3.add_client_error("get_object", service_error_code="SlowDown", http_status_code=503)
    with pytest.raises(ClientError):
        origins.register_vod_source(_register(), CTX)


def test_register_without_a_default_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFAULT_LADDER_VERSION")
    settings.cache_clear()
    with pytest.raises(ToolError) as raised:
        origins.register_vod_source(_register(), CTX)
    assert raised.value.code == "VALIDATION"


def test_lambda_handler_reports_invalid_arguments() -> None:
    context = SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "origins___create_source_location"}),
        aws_request_id="req-1",
        function_name="origins",
        function_version="1",
        invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:origins",
        memory_limit_in_mb=512,
        log_group_name="g",
        log_stream_name="s",
        get_remaining_time_in_millis=lambda: 1000,
    )
    result = origins.handler(
        {"name": "acme", "bucket": "a.b", "segment_delivery_url": "https://cdn"}, cast("LambdaContext", context)
    )
    assert result["ok"] is False and result["error"]["code"] == "VALIDATION"
