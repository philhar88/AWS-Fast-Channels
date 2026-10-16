from collections.abc import Callable
from typing import Any

from botocore.stub import Stubber

from fast_channels.functions.custom_resources.as_run import handler

type Stub = Callable[[str], Stubber]

GROUP = "MediaTailor/Channel/AsRunLog"
PROPS = {"LogGroupName": GROUP, "RetentionInDays": "7"}


def test_create_makes_the_group_and_sets_retention(
    stub: Stub, cfn_event: Callable[..., dict[str, Any]], context: Any
) -> None:
    logs = stub("logs")
    logs.add_response("create_log_group", {}, {"logGroupName": GROUP})
    logs.add_response("put_retention_policy", {}, {"logGroupName": GROUP, "retentionInDays": 7})

    response = handler(cfn_event("Custom::AsRunLogGroup", "Create", PROPS), context)

    assert response == {"PhysicalResourceId": GROUP}


def test_an_existing_group_only_gets_retention(
    stub: Stub, cfn_event: Callable[..., dict[str, Any]], context: Any, client_error: Callable[..., dict[str, Any]]
) -> None:
    logs = stub("logs")
    logs.add_client_error("create_log_group", **client_error("ResourceAlreadyExistsException"))
    logs.add_response("put_retention_policy", {}, {"logGroupName": GROUP, "retentionInDays": 30})
    props = {**PROPS, "RetentionInDays": "30"}

    handler(cfn_event("Custom::AsRunLogGroup", "Update", props, old=PROPS, physical_id=GROUP), context)


def test_delete_leaves_the_shared_group(stub: Stub, cfn_event: Callable[..., dict[str, Any]], context: Any) -> None:
    stub("logs")

    response = handler(cfn_event("Custom::AsRunLogGroup", "Delete", PROPS, physical_id=GROUP), context)

    assert response == {"PhysicalResourceId": GROUP}
