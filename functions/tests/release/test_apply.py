"""The apply path's steps: validation and refusal, immutable FunctionIds, a sibling's configuration and logs, the plan,
and the failure record. Stubbers fail any call a test did not queue, so none of them reaches ``PutChannelPolicy``."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.stub import ANY, Stubber
from pydantic import SecretStr

from fast_channels.core import specs
from fast_channels.core.mediatailor_logs import ADS_LOG_FIELDS, MANIFEST_LOG_FIELDS
from fast_channels.core.monetization.bundle import Bundle, bundle_document
from fast_channels.core.monetization.playback import release_tags, render
from fast_channels.core.monetization.release import ReleaseStatus, applied, playback_plan
from fast_channels.core.monetization.validators import InvalidBundleError
from fast_channels.functions.release import apply, store

type Stub = Callable[[str], Stubber]
AT = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
ARN = "arn:aws:mediatailor:us-east-1:123456789012:playbackConfiguration/"
HLS = "https://abc123.mediatailor.us-east-1.amazonaws.com/v1/master/0123abcd/"
RELEASE_SOURCE = Path(apply.__file__).parent
RELEASE_DESTINATION = "arn:aws:logs:us-east-1:123456789012:delivery-destination:fastm-release-mediatailor"
LAKE_DESTINATION = "arn:aws:logs:us-east-1:123456789012:delivery-destination:fastm-lake-"
CDN = "https://d111111abcdef8.cloudfront.net"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apply, "_now", lambda: AT)


def not_found(stubber: Stubber, operation: str, **expected: Any) -> None:
    stubber.add_client_error(
        operation, service_error_code="NotFoundException", http_status_code=404, expected_params=expected
    )


def test_the_controller_never_changes_a_channel_policy() -> None:
    """P0-08: the channel policy is the static pattern set with the channel; a release never touches it."""
    for path in RELEASE_SOURCE.glob("*.py"):
        text = path.read_text()
        assert "put_channel_policy" not in text and "PutChannelPolicy" not in text.replace(
            "Nothing here calls ``PutChannelPolicy``", ""
        ), path.name


# --- prepare and accept --------------------------------------------------------------------------------------------

SIBLINGS = ["fastm-demo-p-a", "fastm-demo-p-b", "fastm-demo-p-z"]


def test_prepare_allocates_and_accept_chooses_every_sibling_of_a_new_channel(
    kit: SimpleNamespace, state: Any, ledger: Any
) -> None:
    bundle = kit.make_bundle()
    key = state.add(bundle)
    plan = apply.prepare(key, bundle.digest, "deploy-1", "exec-1")
    assert plan == {
        "skip": False,
        "release": 1,
        "change_id": ANY,
        "bundle_key": key,
        "digest": bundle.digest,
        "request": "deploy-1",
    }
    assert state.statuses == {} and ledger.entries == []  # nothing recorded before the lock
    accepted = apply.accept(key, bundle.digest, 1, plan["change_id"], request="deploy-1", execution="exec-1")
    assert accepted == {"siblings": SIBLINGS}
    assert state.holder == 1
    assert state.statuses[1].status == "APPLYING" and state.statuses[1].change_id == plan["change_id"]
    assert ledger.actions() == [("release/bundle", "release.accept")]
    assert ledger.entries[0].detail | {"execution": "exec-1"} == {
        "digest": bundle.digest,
        "release": 1,
        "siblings": SIBLINGS,
        "request": "deploy-1",
        "execution": "exec-1",
    }


def test_accept_waits_for_the_release_holding_the_lock(kit: SimpleNamespace, state: Any, ledger: Any) -> None:
    bundle = kit.make_bundle()
    key = state.add(bundle)
    state.holder = 4
    with pytest.raises(store.ReleaseBusyError):
        apply.accept(key, bundle.digest, 5, "c5", request="deploy-5", execution="exec-5")
    assert state.statuses == {} and ledger.entries == []


def test_prepare_skips_an_event_already_applied_and_keeps_the_change_id_of_a_retry(
    kit: SimpleNamespace, state: Any, ledger: Any
) -> None:
    bundle = kit.make_bundle()
    key = state.add(bundle)
    first = apply.prepare(key, bundle.digest, "deploy-1", "exec-1")
    apply.accept(key, bundle.digest, 1, first["change_id"], request="deploy-1", execution="exec-1")
    again = apply.prepare(key, bundle.digest, "deploy-1", "exec-2")  # delivered twice, the first execution failed
    assert again["release"] == 1 and again["change_id"] == first["change_id"]
    state.statuses[1] = state.statuses[1].model_copy(update={"status": "APPLIED"})
    assert apply.prepare(key, bundle.digest, "deploy-1", "exec-3") == {"skip": True, "release": 1}
    # A later deployment that brings the same bundle back (after another one) is a new release.
    back = apply.prepare(key, bundle.digest, "deploy-3", "exec-4")
    assert back["release"] == 2 and back["change_id"] != first["change_id"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda text: text.replace("abc.channel-assembly", "secret-value-b.channel-assembly"), "the origin secret"),
        (lambda text: "{}", "R-"),
    ],
    ids=["secret", "shape"],
)
def test_prepare_refuses_and_ledgers_a_bundle_that_breaks_a_rule(
    kit: SimpleNamespace, state: Any, ledger: Any, change: Callable[[str], str], message: str
) -> None:
    bundle = kit.make_bundle()
    key = state.add(bundle)
    state.documents[key] = change(state.documents[key])
    with pytest.raises(InvalidBundleError, match=message):
        apply.prepare(key, bundle.digest, "deploy-1", "exec-1")
    assert ledger.actions() == [("release/bundle", "release.reject")]
    assert ledger.entries[0].detail["count"] >= 1
    assert "secret-value" not in json.dumps(ledger.entries[0].detail)  # paths and rules, never the value
    assert state.requests == {} and state.statuses == {}  # nothing allocated


def test_prepare_refuses_an_event_whose_digest_is_not_the_bundles(
    kit: SimpleNamespace, state: Any, ledger: Any
) -> None:
    bundle = kit.make_bundle()
    key = state.add(bundle)
    with pytest.raises(InvalidBundleError, match="the event's digest is not the bundle's"):
        apply.prepare(key, "0" * 64, "deploy-1", "exec-1")
    assert ledger.actions() == [("release/bundle", "release.reject")]


# --- functions ------------------------------------------------------------------------------------------------------


def test_functions_creates_the_trees_of_the_chosen_siblings_children_first_with_tags(
    kit: SimpleNamespace, state: Any, stub: Stub
) -> None:
    bundle = kit.make_bundle()
    key = state.add(bundle)
    tags = release_tags("fastm", release=3, change_id="c3")
    mediatailor = stub("mediatailor")
    for function in bundle.released_functions(3, tags=tags):
        request = function.put_request()
        assert request["Tags"] == tags
        not_found(mediatailor, "get_function", FunctionId=function.function_id)
        mediatailor.add_response(
            "put_function",
            {"FunctionId": function.function_id, "FunctionType": function.function_type.value},
            request,
        )
    assert apply.put_functions(key, 3, "c3", ["fastm-demo-p-b"]) == {
        "created": ["fastm_ps_ctl_r0003", "fastm_ps_root_r0003"],
        "existing": [],
    }


def test_functions_creates_nothing_for_siblings_without_a_mapping(kit: SimpleNamespace, state: Any, stub: Stub) -> None:
    document = bundle_document(kit.make_bundle())["bundle"]
    del document["siblings"][1]["playback"]["FunctionMapping"]
    key = state.add(Bundle.model_validate(document))
    assert apply.put_functions(key, 3, "c3", ["fastm-demo-p-b"]) == {"created": [], "existing": []}


def test_functions_leaves_an_unchanged_id_and_never_updates_one(kit: SimpleNamespace, state: Any, stub: Stub) -> None:
    bundle = kit.make_bundle()
    key = state.add(bundle)
    tags = release_tags("fastm", release=3, change_id="c3")
    leaf, root = bundle.released_functions(3, tags=tags)
    mediatailor = stub("mediatailor")
    for function in (leaf, root):  # a retried step: both exist as created, so nothing is put (no Tags either)
        mediatailor.add_response(
            "get_function", function.put_request() | {"Arn": f"arn:{function.function_id}"}, {"FunctionId": ANY}
        )
    assert apply.put_functions(key, 3, "c3", SIBLINGS) == {
        "created": [],
        "existing": [leaf.function_id, root.function_id],
    }

    changed = leaf.put_request(tags=False) | {"Description": "edited in the console"}
    mediatailor.add_response("get_function", changed, {"FunctionId": leaf.function_id})
    with pytest.raises(apply.ImmutableFunctionError, match=r"fastm_ps_ctl_r0003 exists with another definition"):
        apply.put_functions(key, 3, "c3", SIBLINGS)


def test_functions_raise_other_errors(kit: SimpleNamespace, state: Any, stub: Stub) -> None:
    key = state.add(kit.make_bundle())
    stub("mediatailor").add_client_error("get_function", service_error_code="AccessDeniedException")
    with pytest.raises(apply.ClientError):
        apply.put_functions(key, 1, "c1", SIBLINGS)


# --- configure ------------------------------------------------------------------------------------------------------


def expect_deliveries(
    logs: Stubber,
    sibling: str,
    *,
    existing: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> None:
    """The delivery sources of ``sibling`` not in ``sources`` (tagged when new) and the deliveries not in
    ``existing``: every log type to the module's encrypted log group, ads and manifest also to the lake and without the
    never-stored fields."""
    tags = release_tags("fastm", release=2, change_id="c2")
    lake = {"s3DeliveryConfiguration": {"suffixPath": "{yyyy}/{MM}/{dd}/{HH}", "enableHiveCompatiblePath": False}}
    made = {(d["deliverySourceName"], d["deliveryDestinationArn"]) for d in existing or []}
    known = {source["name"]: source["resourceArns"] for source in sources or []}
    logs.add_response("describe_deliveries", {"deliveries": existing or []}, {})
    logs.add_response("describe_delivery_sources", {"deliverySources": sources or []}, {})
    for log_type, suffix, fields in (
        ("AD_DECISION_SERVER_LOGS", "ads", ADS_LOG_FIELDS),
        ("MANIFEST_SERVICE_LOGS", "manifest", MANIFEST_LOG_FIELDS),
        ("TRANSCODE_LOGS", "transcode", None),
    ):
        source = f"{sibling}-{suffix}"
        if known.get(source) != [f"{ARN}{sibling}"]:
            request = {"name": source, "resourceArn": f"{ARN}{sibling}", "logType": log_type}
            logs.add_response("put_delivery_source", {}, request | ({} if source in known else {"tags": tags}))
        record = {"recordFields": list(fields)} if fields else {}
        destinations: list[tuple[str, dict[str, Any]]] = [(RELEASE_DESTINATION, {})]
        if fields:
            destinations.append((f"{LAKE_DESTINATION}{suffix}", lake))
        for destination, options in destinations:
            if (source, destination) not in made:
                request = {"deliverySourceName": source, "deliveryDestinationArn": destination, "tags": tags}
                logs.add_response("create_delivery", {}, request | record | options)


@pytest.mark.parametrize(("slot", "secret"), [("a", "secret-value-a"), ("b", "secret-value-b")])
def test_configure_puts_the_sibling_with_the_current_secret_and_its_restricted_logs(
    kit: SimpleNamespace, state: Any, stub: Stub, slot: str, secret: str
) -> None:
    bundle = kit.make_bundle()
    key = state.add(bundle)
    state.slot = slot
    sibling = bundle.siblings[1]
    put, logging = render(sibling.playback, prefix="fastm", release=2, change_id="c2", origin_secret=SecretStr(secret))
    assert put["AdDecisionServerConfiguration"]["HttpRequest"]["Headers"]["X-Fast-Origin"] == secret
    assert put["FunctionMapping"] == {"PRE_SESSION_INITIALIZATION": "fastm_ps_root_r0002"}
    mediatailor, logs = stub("mediatailor"), stub("logs")
    mediatailor.add_response(
        "put_playback_configuration",
        {"PlaybackConfigurationArn": f"{ARN}{sibling.name}", "HlsConfiguration": {"ManifestEndpointPrefix": HLS}},
        put,
    )
    mediatailor.add_response("configure_logs_for_playback_configuration", {"PercentEnabled": 100}, logging)
    expect_deliveries(logs, sibling.name)
    assert apply.configure(key, sibling.name, 2, "c2") == {
        "sibling": "fastm-demo-p-b",
        "channel": "fastm-demo",
        "hls_prefix": HLS,
    }


def test_configure_keeps_the_deliveries_a_retry_already_made(kit: SimpleNamespace, state: Any, stub: Stub) -> None:
    bundle = kit.make_bundle()
    key = state.add(bundle)
    sibling = bundle.siblings[0]
    put, logging = render(
        sibling.playback, prefix="fastm", release=2, change_id="c2", origin_secret=SecretStr("secret-value-a")
    )
    mediatailor, logs = stub("mediatailor"), stub("logs")
    mediatailor.add_response(
        "put_playback_configuration",
        {"PlaybackConfigurationArn": f"{ARN}{sibling.name}", "HlsConfiguration": {"ManifestEndpointPrefix": HLS}},
        put,
    )
    mediatailor.add_response("configure_logs_for_playback_configuration", {"PercentEnabled": 100}, logging)
    existing = [
        {"id": "d1", "deliverySourceName": f"{sibling.name}-ads", "deliveryDestinationArn": kit.DESTINATION},
        {"id": "d2", "deliverySourceName": f"{sibling.name}-ads", "deliveryDestinationArn": kit.LAKE_ADS},
    ]
    sources = [
        {"name": f"{sibling.name}-ads", "resourceArns": [f"{ARN}{sibling.name}"]},  # made by the first attempt
        {"name": f"{sibling.name}-manifest", "resourceArns": ["arn:elsewhere"]},  # put again, without tags
    ]
    expect_deliveries(logs, sibling.name, existing=existing, sources=sources)
    apply.configure(key, sibling.name, 2, "c2")


# --- publish --------------------------------------------------------------------------------------------------------


def expect_plan(s3: Stubber, *, configuration: str, release: int, hls: str = HLS) -> None:
    plan = playback_plan(
        channel="fastm-demo",
        configuration=configuration,
        release=release,
        playback=specs.playback_urls(cdn_base=CDN, playback_prefix_hls=hls, outputs=("hls",)),
        at=AT,
    )
    s3.add_response(
        "put_object",
        {},
        {
            "Bucket": "fastm-media",
            "Key": "plan/playback/fastm-demo.json",
            "Body": plan.model_dump_json(indent=2).encode(),
            "ContentType": "application/json",
            "CacheControl": "max-age=30",
        },
    )


def results(*letters: str, hls: str = HLS) -> list[dict[str, Any]]:
    return [{"sibling": f"fastm-demo-p-{x}", "channel": "fastm-demo", "hls_prefix": hls} for x in letters]


def test_publish_records_the_first_release_and_writes_the_stable_siblings_plan(
    kit: SimpleNamespace, state: Any, ledger: Any, stub: Stub
) -> None:
    bundle = kit.make_bundle()
    key = state.add(bundle)
    state.statuses[1] = ReleaseStatus(
        release=1, digest=bundle.digest, bundle_key=key, change_id="c1", status="APPLYING", at=AT
    )
    state.holder = 1
    expect_plan(stub("s3"), configuration="fastm-demo-p-a", release=1)
    assert apply.publish(key, bundle.digest, 1, "c1", results("a", "b", "z")) == {
        "release": 1,
        "plans": {"fastm-demo": "fastm-demo-p-a"},
    }
    assert state.holder is None
    channel, version = state.states["fastm-demo"]
    assert (channel.stable, channel.idle, version) == ("fastm-demo-p-a", "fastm-demo-p-b", 1)
    assert state.statuses[1].status == "APPLIED"
    assert ledger.actions() == [("release/fastm-demo", "release.apply")]
    assert ledger.entries[0].detail == {
        "release": 1,
        "digest": bundle.digest,
        "siblings": ["fastm-demo-p-a", "fastm-demo-p-b", "fastm-demo-p-z"],
        "stable": "fastm-demo-p-a",
        "idle": "fastm-demo-p-b",
    }
    # A retried step (after a StaleStateError on another channel) writes nothing twice: no plan is queued.
    assert apply.publish(key, bundle.digest, 1, "c1", results("a", "b", "z"))["plans"] == {}
    assert state.states["fastm-demo"][1] == 1 and len(ledger.entries) == 1


def test_publish_of_a_later_release_keeps_the_plan_on_the_stable_sibling(
    kit: SimpleNamespace, state: Any, ledger: Any, stub: Stub
) -> None:
    bundle = kit.make_bundle()
    key = state.add(bundle)
    stable = HLS.replace("0123abcd", "stable")
    siblings = ["fastm-demo-p-a", "fastm-demo-p-b", "fastm-demo-p-z"]
    first = applied(None, channel="fastm-demo", siblings=siblings, release=1, digest="d" * 64, change_id="c1", at=AT)
    state.states["fastm-demo"] = (first, 1)
    stub("mediatailor").add_response(
        "get_playback_configuration",
        {"HlsConfiguration": {"ManifestEndpointPrefix": stable}},
        {"Name": "fastm-demo-p-a"},
    )
    expect_plan(stub("s3"), configuration="fastm-demo-p-a", release=1, hls=stable)
    apply.publish(key, bundle.digest, 2, "c2", results("b"))
    channel, version = state.states["fastm-demo"]
    assert version == 2 and channel.siblings["fastm-demo-p-b"].release == 2
    assert channel.siblings["fastm-demo-p-a"].release == 1
    assert state.statuses[2].status == "APPLIED"


# --- fail and the handler -------------------------------------------------------------------------------------------


def test_fail_marks_the_release_and_ledgers_the_error(kit: SimpleNamespace, state: Any, ledger: Any) -> None:
    bundle = kit.make_bundle()
    state.statuses[4] = ReleaseStatus(
        release=4, digest=bundle.digest, bundle_key="k", change_id="c4", status="APPLYING", at=AT
    )
    error = {"Error": "ImmutableFunctionError", "Cause": "x" * 5000}
    state.holder = 4
    result = apply.fail(4, "c4", error, "exec-4")
    assert state.statuses[4].status == "FAILED" and state.holder is None
    assert len(result["error"]) < 1100
    assert ledger.actions() == [("release/bundle", "release.fail")]
    assert ledger.entries[0].subject == "r0004" and ledger.entries[0].correlation_id == "c4"
    state.holder = 6  # another release's lock stays
    apply.fail(None, None, {"Error": "States.Timeout"}, "exec-5")
    apply.fail(5, "c5", {"Error": "States.Timeout"}, "exec-5")
    assert state.holder == 6
    assert ledger.entries[1].subject == "unallocated" and ledger.entries[1].correlation_id == "exec-5"
    assert ledger.entries[2].subject == "r0005"


def test_the_handler_runs_the_steps_without_touching_the_channel_policy(
    kit: SimpleNamespace, state: Any, ledger: Any, stub: Stub, context: Any
) -> None:
    """A whole first release through the handler: every AWS call is a queued one."""
    bundle = kit.make_bundle()
    key = state.add(bundle)
    start = {"step": "prepare", "bundle_key": key, "digest": bundle.digest, "request": "deploy-1", "execution": "e"}
    prepared = apply.handler(start, context)
    common = {"bundle_key": key, "digest": bundle.digest, "release": 1, "change_id": prepared["change_id"]}
    accepted = apply.handler({"step": "accept", "request": "deploy-1", "execution": "e", **common}, context)
    mediatailor, logs = stub("mediatailor"), stub("logs")
    tags = release_tags("fastm", release=1, change_id=prepared["change_id"])
    for function in bundle.released_functions(1, tags=tags):
        not_found(mediatailor, "get_function", FunctionId=function.function_id)
        mediatailor.add_response(
            "put_function", {"FunctionId": function.function_id, "FunctionType": "CUSTOM_OUTPUT"}, None
        )
    apply.handler({"step": "functions", "siblings": accepted["siblings"], **common}, context)
    configured = []
    for name in accepted["siblings"]:
        mediatailor.add_response(
            "put_playback_configuration",
            {"PlaybackConfigurationArn": f"{ARN}{name}", "HlsConfiguration": {"ManifestEndpointPrefix": HLS}},
            None,
        )
        mediatailor.add_response("configure_logs_for_playback_configuration", {"PercentEnabled": 100}, None)
        logs.add_response("describe_deliveries", {"deliveries": []}, {})
        logs.add_response("describe_delivery_sources", {"deliverySources": []}, {})
        for deliveries in (2, 2, 1):  # ads and manifest to the log group and the lake, transcode to the log group
            logs.add_response("put_delivery_source", {}, None)
            for _ in range(deliveries):
                logs.add_response("create_delivery", {}, None)
        configured.append(apply.handler({"step": "configure", "sibling": name, **common}, context))
    stub("s3").add_response("put_object", {}, None)
    published = apply.handler({"step": "publish", "results": configured, **common}, context)
    assert published == {"release": 1, "plans": {"fastm-demo": "fastm-demo-p-a"}}
    assert apply.handler({"step": "fail", "error": {"Error": "E"}, "execution": "e"}, context)["release"] is None
    with pytest.raises(ValueError, match="unknown step 'promote'"):
        apply.handler({"step": "promote"}, context)
