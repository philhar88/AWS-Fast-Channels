import json
from datetime import UTC, datetime
from typing import Any

import pytest
from boto3.dynamodb.types import TypeSerializer

from fast_channels.core.ladder import Ladder
from fast_channels.core.naming import digest
from fast_channels.core.timeline import BREAK_SLATE_GOPS
from fast_channels.functions.custom_resources import ladder_kit
from fast_channels.functions.custom_resources.ladder_kit import KitProvisioningError, attributes, kit_fingerprint

STANDARD = Ladder.builtin("standard")
PROVISIONER = "arn:aws:states:us-east-1:123456789012:stateMachine:fast-ladder-provisioner"
EXECUTION = "arn:aws:states:us-east-1:123456789012:execution:fast-ladder-provisioner:kit-x"
PROPERTIES = {
    "ServiceToken": "arn:aws:lambda:us-east-1:123456789012:function:provider",
    "LadderVersion": STANDARD.version,
    "Ladder": STANDARD.model_dump_json(),
    "JobTemplate": f"fast-standard_{STANDARD.fingerprint}",
    "BrandDigest": "abc",
    "ContentBaseUrl": "https://media.example.com",
}
KIT = {
    "filler": "filler-vod",
    "break_slates": {str(g): f"break-{g}-vod" for g in BREAK_SLATE_GOPS},
    "house_ads": ["c2", "c1"],
    "vast_key": "ads/x/vast.xml",
    "vast_url": "https://media.example.com/ads/x/vast.xml",
}
_typed = TypeSerializer().serialize


def item(status: str, fingerprint: str | None = None) -> dict[str, Any]:
    fields = {
        "pk": "LADDER#standard",
        "sk": f"V#{STANDARD.version}",
        "ladder_version": STANDARD.version,
        "job_template": PROPERTIES["JobTemplate"],
        "status": status,
        "kit": KIT,
    }
    if fingerprint:
        fields["kit_fingerprint"] = fingerprint
    return {"Item": {k: _typed(v) for k, v in fields.items()}}


def expect_item(stubs: Any, response: dict[str, Any]) -> None:
    stubs["dynamodb"].add_response(
        "get_item",
        response,
        {"TableName": "library", "Key": {"pk": "LADDER#standard", "sk": f"V#{STANDARD.version}"}},
    )


def event(request_type: str, **extra: Any) -> dict[str, Any]:
    return {
        "RequestType": request_type,
        "RequestId": "req-1",
        "ResourceType": "Custom::LadderKit",
        "ResourceProperties": PROPERTIES,
        **extra,
    }


def test_fingerprint_ignores_the_service_token() -> None:
    assert kit_fingerprint(PROPERTIES) == kit_fingerprint({**PROPERTIES, "ServiceToken": "other"})
    assert kit_fingerprint(PROPERTIES) != kit_fingerprint({**PROPERTIES, "BrandDigest": "def"})


def test_create_starts_the_provisioner(stubs: Any, lambda_context: Any) -> None:
    expect_item(stubs, {})
    name = f"kit-standard_{STANDARD.fingerprint}-{digest('req-1')}"

    provision = {
        "ladder": STANDARD.model_dump(mode="json"),
        "job_template": PROPERTIES["JobTemplate"],
        "kit_fingerprint": kit_fingerprint(PROPERTIES),
        "owner": "iac",
    }
    stubs["stepfunctions"].add_response(
        "start_execution",
        {"executionArn": EXECUTION, "startDate": datetime.now(UTC)},
        {"stateMachineArn": PROVISIONER, "name": name, "input": json.dumps(provision)},
    )

    assert ladder_kit.on_event(event("Create"), lambda_context) == {
        "PhysicalResourceId": STANDARD.version,
        "Data": {"ExecutionArn": EXECUTION},
    }


def test_update_of_a_current_kit_is_a_no_op(stubs: Any, lambda_context: Any) -> None:
    expect_item(stubs, item("READY", kit_fingerprint(PROPERTIES)))

    assert ladder_kit.on_event(event("Update"), lambda_context) == {"PhysicalResourceId": STANDARD.version}


def test_update_of_a_stale_kit_reprovisions(stubs: Any, lambda_context: Any) -> None:
    expect_item(stubs, item("READY", "old"))
    stubs["stepfunctions"].add_response(
        "start_execution", {"executionArn": EXECUTION, "startDate": datetime.now(UTC)}, None
    )

    assert ladder_kit.on_event(event("Update"), lambda_context)["Data"] == {"ExecutionArn": EXECUTION}


def test_delete_is_a_no_op(lambda_context: Any) -> None:
    assert ladder_kit.on_event(event("Delete", PhysicalResourceId="v"), lambda_context) == {"PhysicalResourceId": "v"}
    assert ladder_kit.is_complete(event("Delete", PhysicalResourceId="v"), lambda_context) == {"IsComplete": True}


def describe(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "executionArn": EXECUTION,
        "stateMachineArn": PROVISIONER,
        "status": status,
        "startDate": datetime.now(UTC),
        **extra,
    }


def test_is_complete_polls_the_execution(stubs: Any, lambda_context: Any) -> None:
    stubs["stepfunctions"].add_response("describe_execution", describe("RUNNING"), {"executionArn": EXECUTION})

    assert ladder_kit.is_complete(event("Create", Data={"ExecutionArn": EXECUTION}), lambda_context) == {
        "IsComplete": False
    }


def test_is_complete_returns_the_kit(stubs: Any, lambda_context: Any) -> None:
    stubs["stepfunctions"].add_response("describe_execution", describe("SUCCEEDED"), {"executionArn": EXECUTION})
    expect_item(stubs, item("READY", "fp"))

    result = ladder_kit.is_complete(event("Create", Data={"ExecutionArn": EXECUTION}), lambda_context)

    assert result == {
        "IsComplete": True,
        "Data": attributes({"ladder_version": STANDARD.version, "job_template": PROPERTIES["JobTemplate"], "kit": KIT}),
    }
    assert result["Data"]["BreakSlate30"] == "break-30-vod"
    assert result["Data"]["HouseAds"] == "c2,c1"


def test_is_complete_without_execution_reads_the_current_kit(stubs: Any, lambda_context: Any) -> None:
    expect_item(stubs, item("READY", "fp"))

    assert ladder_kit.is_complete(event("Update"), lambda_context)["Data"]["FillerVodSource"] == "filler-vod"


@pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "ABORTED"])
def test_is_complete_reports_failures(stubs: Any, lambda_context: Any, status: str) -> None:
    stubs["stepfunctions"].add_response(
        "describe_execution", describe(status, error="IngestRefusedError", cause="HDR"), None
    )

    with pytest.raises(KitProvisioningError, match=f"{status}.*IngestRefusedError: HDR"):
        ladder_kit.is_complete(event("Create", Data={"ExecutionArn": EXECUTION}), lambda_context)


def test_is_complete_requires_a_ready_ladder(stubs: Any, lambda_context: Any) -> None:
    stubs["stepfunctions"].add_response("describe_execution", describe("SUCCEEDED"), None)
    expect_item(stubs, item("FAILED"))

    with pytest.raises(KitProvisioningError, match="not READY"):
        ladder_kit.is_complete(event("Create", Data={"ExecutionArn": EXECUTION}), lambda_context)
