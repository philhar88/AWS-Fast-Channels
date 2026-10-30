"""The Release Controller's state table, bundle reads and origin secret: one release per digest, versioned channel
writes, the origin slot."""

import io
import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from fast_channels.core.monetization.bundle import bundle_document
from fast_channels.core.monetization.release import ChannelState, ReleaseStatus
from fast_channels.functions.release import store

type Stub = Callable[[str], Stubber]
TABLE = "fastm-release"
AT = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def get(pk: str) -> dict[str, object]:
    return {"TableName": TABLE, "Key": {"pk": pk}, "ConsistentRead": True}


def conditional_failure(stubber: Stubber, operation: str) -> None:
    stubber.add_client_error(operation, service_error_code="ConditionalCheckFailedException")


def body(text: str) -> StreamingBody:
    data = text.encode()
    return StreamingBody(io.BytesIO(data), len(data))


def test_load_and_bundle_read_the_release_bucket(kit: SimpleNamespace, stub: Stub) -> None:
    bundle = kit.make_bundle()
    text = json.dumps(bundle_document(bundle))
    key = f"release/{bundle.digest}/bundle.json"
    s3 = stub("s3")
    s3.add_response("get_object", {"Body": body(text)}, {"Bucket": "fastm-release-bundles", "Key": key})
    assert store.bundle(key) == bundle
    assert store.bundle(key) == bundle  # cached: one read
    s3.add_response("get_object", {"Body": body("{}")}, {"Bucket": "fastm-release-bundles", "Key": "bad"})
    with pytest.raises(ValueError, match="bad is not a bundle document"):
        store.bundle("bad")


def test_allocate_gives_an_event_one_release(stub: Stub) -> None:
    dynamodb = stub("dynamodb")
    dynamodb.add_response("get_item", {"Item": {"pk": {"S": "REQUEST#d1"}, "release": {"N": "2"}}}, get("REQUEST#d1"))
    assert store.allocate("d1") == 2  # a redelivered event

    dynamodb.add_response("get_item", {}, get("REQUEST#d2"))
    dynamodb.add_response(
        "update_item",
        {"Attributes": {"last": {"N": "3"}}},
        {
            "TableName": TABLE,
            "Key": {"pk": "COUNTER"},
            "UpdateExpression": "ADD #last :one",
            "ExpressionAttributeNames": {"#last": "last"},
            "ExpressionAttributeValues": {":one": 1},
            "ReturnValues": "UPDATED_NEW",
        },
    )
    dynamodb.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": {"pk": "REQUEST#d2", "release": 3},
            "ConditionExpression": "attribute_not_exists(pk)",
        },
    )
    assert store.allocate("d2") == 3


def test_allocate_returns_the_release_a_concurrent_execution_took(stub: Stub) -> None:
    dynamodb = stub("dynamodb")
    dynamodb.add_response("get_item", {}, get("REQUEST#d3"))
    dynamodb.add_response("update_item", {"Attributes": {"last": {"N": "5"}}})
    conditional_failure(dynamodb, "put_item")
    dynamodb.add_response("get_item", {"Item": {"pk": {"S": "REQUEST#d3"}, "release": {"N": "4"}}}, get("REQUEST#d3"))
    assert store.allocate("d3") == 4
    dynamodb.add_response("get_item", {}, get("REQUEST#d4"))
    dynamodb.add_response("update_item", {"Attributes": {"last": {"N": "6"}}})
    dynamodb.add_client_error("put_item", service_error_code="ProvisionedThroughputExceededException")
    with pytest.raises(store.ClientError):
        store.allocate("d4")


def test_status_and_channel_state_round_trip(stub: Stub) -> None:
    dynamodb = stub("dynamodb")
    record = ReleaseStatus(
        release=7, digest="a" * 64, bundle_key="release/a/bundle.json", change_id="c7", status="APPLYING", at=AT
    )
    dynamodb.add_response(
        "put_item", {}, {"TableName": TABLE, "Item": {"pk": "RELEASE#r0007", "doc": record.model_dump_json()}}
    )
    store.put_status(record)
    dynamodb.add_response("get_item", {"Item": {"doc": {"S": record.model_dump_json()}}}, get("RELEASE#r0007"))
    assert store.status(7) == record
    dynamodb.add_response("get_item", {}, get("RELEASE#r0008"))
    assert store.status(8) is None

    state = ChannelState(channel="fastm-demo", stable="fastm-demo-p-a", idle="fastm-demo-p-b")
    dynamodb.add_response("get_item", {}, get("CHANNEL#fastm-demo"))
    assert store.channel("fastm-demo") == (None, 0)
    dynamodb.add_response(
        "get_item", {"Item": {"doc": {"S": state.model_dump_json()}, "version": {"N": "3"}}}, get("CHANNEL#fastm-demo")
    )
    dynamodb.add_response("get_item", {}, get("CHANNEL#fastm-news"))
    assert store.channels(["fastm-demo", "fastm-news"]) == {"fastm-demo": state}


def test_put_channel_checks_the_version(stub: Stub) -> None:
    dynamodb = stub("dynamodb")
    state = ChannelState(channel="fastm-demo")
    dynamodb.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": {"pk": "CHANNEL#fastm-demo", "doc": state.model_dump_json(), "version": 4},
            "ConditionExpression": "attribute_not_exists(pk) OR #version = :version",
            "ExpressionAttributeNames": {"#version": "version"},
            "ExpressionAttributeValues": {":version": 3},
        },
    )
    store.put_channel(state, version=3)
    conditional_failure(dynamodb, "put_item")
    with pytest.raises(store.StaleStateError, match="fastm-demo changed while it was being released"):
        store.put_channel(state, version=3)
    dynamodb.add_client_error("put_item", service_error_code="InternalServerError", http_status_code=500)
    with pytest.raises(store.ClientError):
        store.put_channel(state, version=3)


def test_one_release_holds_the_lock_at_a_time(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.time, "time", lambda: 1_000.5)
    dynamodb = stub("dynamodb")
    dynamodb.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": {"pk": "LOCK", "release": 3, "owner": "r0003", "expires": 1_000 + 9_600},  # RELEASE_LOCK_SECONDS
            "ConditionExpression": (
                "attribute_not_exists(pk) OR #expires < :now OR (#release = :release AND #owner = :owner)"
            ),
            "ExpressionAttributeNames": {"#release": "release", "#owner": "owner", "#expires": "expires"},
            "ExpressionAttributeValues": {":release": 3, ":owner": "r0003", ":now": 1_000},
        },
    )
    store.lock(3)
    conditional_failure(dynamodb, "put_item")
    with pytest.raises(store.ReleaseBusyError, match="r0004 waits"):
        store.lock(4)
    dynamodb.add_client_error("put_item", service_error_code="InternalServerError", http_status_code=500)
    with pytest.raises(store.ClientError):
        store.lock(4)
    unlock = {
        "TableName": TABLE,
        "Key": {"pk": "LOCK"},
        "ConditionExpression": "#release = :release AND #owner = :owner",
        "ExpressionAttributeNames": {"#release": "release", "#owner": "owner"},
        "ExpressionAttributeValues": {":release": 3, ":owner": "r0003"},
    }
    dynamodb.add_response("delete_item", {}, unlock)
    store.unlock(3)
    conditional_failure(dynamodb, "delete_item")  # another release holds it, or nobody: left as it is
    store.unlock(3)
    dynamodb.add_client_error("delete_item", service_error_code="InternalServerError", http_status_code=500)
    with pytest.raises(store.ClientError):
        store.unlock(3)


def test_the_origin_slot_and_its_secret(stub: Stub) -> None:
    dynamodb = stub("dynamodb")
    dynamodb.add_response("get_item", {}, get("ORIGIN"))
    assert store.origin_slot() == "a"
    dynamodb.add_response("get_item", {"Item": {"slot": {"S": "b"}}}, get("ORIGIN"))
    assert store.origin_slot() == "b"
    dynamodb.add_response("get_item", {"Item": {"slot": {"S": "c"}}}, get("ORIGIN"))
    with pytest.raises(ValueError, match="unknown origin secret slot 'c'"):
        store.origin_slot()
    stub("secretsmanager").add_response(
        "get_secret_value", {"SecretString": "value-b"}, {"SecretId": "fastm/demand/origin-b"}
    )
    assert store.secret("b").get_secret_value() == "value-b"


def test_the_pointer_and_the_lock_holder(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    dynamodb = stub("dynamodb")
    dynamodb.add_response("get_item", {}, get("POINTER"))
    assert store.pointer() == {}
    dynamodb.add_response(
        "put_item", {}, {"TableName": TABLE, "Item": {"pk": "POINTER", "doc": '{"fastm-demo": "fastm-demo-p-b"}'}}
    )
    store.put_pointer({"fastm-demo": "fastm-demo-p-b"})
    dynamodb.add_response("get_item", {"Item": {"doc": {"S": '{"fastm-demo": "fastm-demo-p-b"}'}}}, get("POINTER"))
    assert store.pointer() == {"fastm-demo": "fastm-demo-p-b"}
    monkeypatch.setattr(store.time, "time", lambda: 1_000.0)
    dynamodb.add_response("get_item", {}, get("LOCK"))
    assert store.holder() is None
    dynamodb.add_response("get_item", {"Item": {"release": {"N": "4"}, "expires": {"N": "999"}}}, get("LOCK"))
    assert store.holder() is None  # expired
    dynamodb.add_response("get_item", {"Item": {"release": {"N": "4"}, "expires": {"N": "2000"}}}, get("LOCK"))
    assert store.holder() == 4


def test_each_rotation_owns_the_lock_it_holds(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.time, "time", lambda: 1_000.5)
    dynamodb = stub("dynamodb")
    dynamodb.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": {"pk": "LOCK", "release": 0, "owner": "rotation-1", "expires": 1_000 + 9_600},
            "ConditionExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ExpressionAttributeValues": {":release": 0, ":owner": "rotation-1", ":now": 1_000},
        },
    )
    store.lock(store.ROTATION, owner="rotation-1")
    conditional_failure(dynamodb, "put_item")  # rotation-1 holds it: rotation-2 waits
    with pytest.raises(store.ReleaseBusyError, match="the rotation waits"):
        store.lock(store.ROTATION, owner="rotation-2")
    conditional_failure(dynamodb, "delete_item")
    store.unlock(store.ROTATION, owner="rotation-2")  # not its lock: left to rotation-1


def test_a_switch_records_when_the_siblings_changed_secret(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.time, "time", lambda: 1_793_345_492.4)
    dynamodb = stub("dynamodb")
    dynamodb.add_response(
        "put_item", {}, {"TableName": TABLE, "Item": {"pk": "ORIGIN", "slot": "b", "at": 1_793_345_492}}
    )
    store.put_origin_slot("b")
    dynamodb.add_response("get_item", {}, get("ORIGIN"))
    assert store.origin_rotated_at() is None
    dynamodb.add_response("get_item", {"Item": {"slot": {"S": "b"}, "at": {"N": "1793345492"}}}, get("ORIGIN"))
    assert store.origin_rotated_at() == datetime(2026, 10, 30, 7, 31, 32, tzinfo=UTC)
