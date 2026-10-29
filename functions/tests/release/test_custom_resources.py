"""``Custom::DesiredState`` (validate, store, announce a new digest) and ``Custom::ReleaseReaper`` (delete what the
controller created, owner-checked, in dependency order)."""

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from fast_channels.core.monetization.bundle import bundle_document
from fast_channels.core.monetization.playback import release_tags
from fast_channels.core.monetization.validators import InvalidBundleError
from fast_channels.functions.release import desired_state, reaper

type Stub = Callable[[str], Stubber]
OWNED = release_tags("fastm", release=1, change_id="c1")
PC = "arn:aws:mediatailor:us-east-1:123456789012:playbackConfiguration/"


def as_property(bundle: Any) -> str:
    """The ``Bundle`` property as the stack sets it: the bundle's JSON (its stored document's ``bundle``)."""
    return json.dumps(bundle_document(bundle)["bundle"], sort_keys=True, separators=(",", ":"))


def event(request: str, bundle: Any = None, *, old: Any = None) -> dict[str, Any]:
    properties = {"Prefix": "fastm", "Bucket": "fastm-release-bundles", "EventBus": "fastm-fast-channels"}
    found: dict[str, Any] = {
        "RequestType": request,
        "RequestId": f"{request.lower()}-1",
        "ResourceProperties": properties | ({"Bundle": as_property(bundle)} if bundle else {}),
    }
    if old is not None:
        found["OldResourceProperties"] = properties | {"Bundle": as_property(old)}
    if request != "Create":
        found["PhysicalResourceId"] = "fastm-desired-state"
    return found


def expect_put(s3: Stubber, bundle: Any) -> str:
    key = f"release/{bundle.digest}/bundle.json"
    s3.add_response(
        "put_object",
        {},
        {
            "Bucket": "fastm-release-bundles",
            "Key": key,
            "Body": json.dumps(bundle_document(bundle), indent=2, sort_keys=True).encode(),
            "ContentType": "application/json",
        },
    )
    return key


def expect_event(events: Stubber, bundle: Any, key: str, request_id: str, *, failed: int = 0) -> None:
    detail = {"bundle_key": key, "digest": bundle.digest, "correlation_id": request_id}
    events.add_response(
        "put_events",
        {"FailedEntryCount": failed, "Entries": [{"ErrorCode": "x"}] if failed else [{"EventId": "1"}]},
        {
            "Entries": [
                {
                    "Source": ANY,
                    "DetailType": "Desired State Rendered",
                    "Detail": json.dumps(detail, separators=(",", ":")),
                    "EventBusName": "fastm-fast-channels",
                }
            ]
        },
    )


def test_desired_state_stores_and_announces_a_new_bundle(kit: SimpleNamespace, stub: Stub, context: Any) -> None:
    bundle = kit.make_bundle()
    key = expect_put(stub("s3"), bundle)
    expect_event(stub("events"), bundle, key, "create-1")
    assert desired_state.handler(event("Create", bundle), context) == {
        "PhysicalResourceId": "fastm-desired-state",
        "Data": {"BundleKey": key, "Digest": bundle.digest},
    }


def test_desired_state_announces_an_update_only_when_the_digest_changes(
    kit: SimpleNamespace, stub: Stub, context: Any
) -> None:
    bundle, wider = kit.make_bundle(), kit.make_bundle(log_percent=50)
    expect_put(stub("s3"), bundle)
    desired_state.handler(event("Update", bundle, old=bundle), context)  # no event queued: none may be put
    key = expect_put(stub("s3"), wider)
    expect_event(stub("events"), wider, key, "update-1")
    desired_state.handler(event("Update", wider, old=bundle), context)


def test_desired_state_fails_the_deployment_for_an_invalid_bundle_or_a_lost_event(
    kit: SimpleNamespace, stub: Stub, context: Any
) -> None:
    bundle = kit.make_bundle()
    bad = event("Create", bundle)
    document = json.loads(bad["ResourceProperties"]["Bundle"])
    document["siblings"][0]["playback"]["PersonalizationThresholdSeconds"] = 10
    bad["ResourceProperties"]["Bundle"] = json.dumps(document)
    with pytest.raises(InvalidBundleError, match="R-P"):
        desired_state.handler(bad, context)  # nothing stored, nothing announced
    key = expect_put(stub("s3"), bundle)
    expect_event(stub("events"), bundle, key, "create-1", failed=1)
    with pytest.raises(RuntimeError, match="Desired State Rendered was not published"):
        desired_state.handler(event("Create", bundle), context)


def test_an_old_bundle_this_code_no_longer_reads_counts_as_changed(
    kit: SimpleNamespace, stub: Stub, context: Any
) -> None:
    bundle = kit.make_bundle()
    update = event("Update", bundle, old=bundle)
    update["OldResourceProperties"]["Bundle"] = json.dumps({"v": 0, "renamed": True})  # an earlier schema
    key = expect_put(stub("s3"), bundle)
    expect_event(stub("events"), bundle, key, "update-1")
    desired_state.handler(update, context)


def test_desired_state_delete_leaves_the_bundles(context: Any) -> None:
    assert desired_state.handler(event("Delete"), context) == {"PhysicalResourceId": "fastm-desired-state"}


# --- reaper ---------------------------------------------------------------------------------------------------------


def reaper_event(request: str, *, old: list[str] | None = None) -> dict[str, Any]:
    properties = {"Prefix": "fastm", "Channels": ["fastm-demo"], "MediaBucket": "fastm-media"}
    event: dict[str, Any] = {"RequestType": request, "RequestId": "r-1", "ResourceProperties": properties}
    if old is not None:
        event["OldResourceProperties"] = properties | {"Channels": old}
    return event


def pc(name: str, tags: dict[str, str]) -> dict[str, Any]:
    return {"Name": name, "PlaybackConfigurationArn": f"{PC}{name}", "Tags": tags}


def expect_sibling_deleted(mediatailor: Stubber, logs: Stubber, sibling: str) -> None:
    """``channel_ops.delete_deliveries`` (by the sources' resource ARN), then the configuration."""
    logs.add_response(
        "describe_delivery_sources",
        {
            "deliverySources": [
                {"name": f"{sibling}-ads", "resourceArns": [f"{PC}{sibling}"]},
                {"name": f"{sibling}-manifest", "resourceArns": [f"{PC}{sibling}"]},
                {"name": f"{sibling}-x-ads", "resourceArns": [f"{PC}{sibling}-x"]},  # a name that only looks alike
            ]
        },
    )
    logs.add_response(
        "describe_deliveries",
        {
            "deliveries": [
                {"id": f"{sibling}-1", "deliverySourceName": f"{sibling}-ads"},
                {"id": f"{sibling}-2", "deliverySourceName": f"{sibling}-manifest"},
                {"id": "other", "deliverySourceName": f"{sibling}-x-ads"},
            ]
        },
    )
    logs.add_response("delete_delivery", {}, {"id": f"{sibling}-1"})
    logs.add_client_error("delete_delivery", service_error_code="ResourceNotFoundException")  # already gone
    logs.add_response("delete_delivery_source", {}, {"name": f"{sibling}-ads"})
    logs.add_response("delete_delivery_source", {}, {"name": f"{sibling}-manifest"})
    mediatailor.add_response("delete_playback_configuration", {}, {"Name": sibling})


def test_the_reaper_deletes_only_what_the_controller_owns_in_dependency_order(stub: Stub, context: Any) -> None:
    mediatailor, logs, s3 = stub("mediatailor"), stub("logs"), stub("s3")
    mediatailor.add_response(
        "list_playback_configurations",
        {
            "Items": [
                pc("fastm-demo", {"fast:app": "fastm"}),  # the v3.0.0 configuration
                pc("fastm-demo-p-a", OWNED),
                pc("fastm-demo-p-b", OWNED),
                pc("fastm-demo-p-q", {"fast:app": "fastm"}),  # not the controller's
                pc("fastm-news-p-a", OWNED),  # not a channel of this stack
                pc("fastm-demo-p-ab", OWNED),
            ]
        },
    )
    for sibling in ("fastm-demo-p-a", "fastm-demo-p-b"):
        expect_sibling_deleted(mediatailor, logs, sibling)
    s3.add_response("delete_object", {}, {"Bucket": "fastm-media", "Key": "plan/playback/fastm-demo.json"})
    mediatailor.add_response(
        "list_functions",
        {
            "Items": [
                {"FunctionId": "fastm_ps_ctl_r0001", "FunctionType": "CUSTOM_OUTPUT", "Tags": OWNED},
                {"FunctionId": "fastm_ps_root_r0001", "FunctionType": "SEQUENTIAL_EXECUTOR", "Tags": OWNED},
                {"FunctionId": "fastm_hand_made", "FunctionType": "CUSTOM_OUTPUT", "Tags": {}},
                {"FunctionId": "fastx_ps_root_r0001", "FunctionType": "SEQUENTIAL_EXECUTOR", "Tags": OWNED},
            ]
        },
    )
    mediatailor.add_response("delete_function", {}, {"FunctionId": "fastm_ps_root_r0001"})
    mediatailor.add_response("delete_function", {}, {"FunctionId": "fastm_ps_ctl_r0001"})
    assert reaper.handler(reaper_event("Delete"), context) == {"PhysicalResourceId": "fastm-release-reaper"}


def test_an_update_reaps_the_siblings_of_channels_taken_out_of_the_module(stub: Stub, context: Any) -> None:
    mediatailor, logs, s3 = stub("mediatailor"), stub("logs"), stub("s3")
    assert reaper.handler(reaper_event("Update", old=["fastm-demo"]), context)  # no channel removed: nothing
    mediatailor.add_response(
        "list_playback_configurations", {"Items": [pc("fastm-news-p-a", OWNED), pc("fastm-demo-p-a", OWNED)]}
    )
    expect_sibling_deleted(mediatailor, logs, "fastm-news-p-a")
    s3.add_response("delete_object", {}, {"Bucket": "fastm-media", "Key": "plan/playback/fastm-news.json"})
    reaper.handler(reaper_event("Update", old=["fastm-demo", "fastm-news"]), context)  # the Functions stay


def test_the_reaper_raises_other_errors_and_ignores_create(stub: Stub, context: Any) -> None:
    assert reaper.handler(reaper_event("Create"), context) == {"PhysicalResourceId": "fastm-release-reaper"}
    mediatailor, logs = stub("mediatailor"), stub("logs")
    mediatailor.add_response("list_playback_configurations", {"Items": [pc("fastm-demo-p-a", OWNED)]})
    logs.add_response(
        "describe_delivery_sources",
        {"deliverySources": [{"name": "fastm-demo-p-a-ads", "resourceArns": [f"{PC}fastm-demo-p-a"]}]},
    )
    logs.add_response("describe_deliveries", {"deliveries": []})
    logs.add_client_error("delete_delivery_source", service_error_code="AccessDeniedException")
    with pytest.raises(ClientError):
        reaper.handler(reaper_event("Delete"), context)
    assert reaper.siblings("fastm", []) == {}
