"""``Custom::LadderKit``: provisions a built-in ladder's kit at deploy time (SPEC §5.2).

Create and Update start the LadderProvisioner state machine, unless the ladder version is already READY with the
same kit fingerprint, so redeploys finish in seconds. The fingerprint is a hash of every resource property as
CloudFormation resolved it (ladder version, brand images, content host, package profiles, source location), so a
change to any input of the kit re-provisions it. :func:`is_complete` polls the execution. Delete does nothing: the
kit's VOD sources are runtime resources that the RuntimeResourceReaper removes with the stack.

Properties: ``LadderVersion``, ``Ladder`` (JSON of the definition), ``JobTemplate``, and the kit inputs that only
feed the fingerprint. Attributes: ``LadderVersion``, ``JobTemplate``, ``FillerVodSource``, ``BreakSlate<gops>``
for each of ``BREAK_SLATE_GOPS``, ``HouseAds`` (comma-separated creative ids), ``VastKey`` and ``VastUrl``.
"""

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger

from fast_channels.core.naming import digest, ladder_path
from fast_channels.core.tags import Owner
from fast_channels.core.timeline import BREAK_SLATE_GOPS
from fast_channels.functions import aws
from fast_channels.functions.media_library import LadderStatus, from_dynamo, ladder_key
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

RESOURCE_TYPE: Final = "Custom::LadderKit"
_RUNNING: Final = "RUNNING"

logger = Logger()


class KitProvisioningError(RuntimeError):
    """The provisioner failed, or finished without marking the ladder READY."""


def kit_fingerprint(properties: Mapping[str, Any]) -> str:
    """Hash of the resource properties (without the service token) that identifies a kit."""
    inputs = {k: v for k, v in properties.items() if k != "ServiceToken"}
    return digest(json.dumps(inputs, sort_keys=True, separators=(",", ":")), 16)


@logger.inject_lambda_context(clear_state=True)
def on_event(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Start provisioning on Create/Update (unless already provisioned); acknowledge Delete."""
    match event["RequestType"]:
        case "Create" | "Update":
            return start(event["ResourceProperties"], request_id=event["RequestId"])
        case _:
            return {"PhysicalResourceId": event["PhysicalResourceId"]}


def start(properties: Mapping[str, Any], *, request_id: str) -> dict[str, Any]:
    config = settings()
    config.require("library_table", "provision_state_machine_arn")
    ladder_version = properties["LadderVersion"]
    fingerprint = kit_fingerprint(properties)
    logger.append_keys(ladder_version=ladder_version, kit_fingerprint=fingerprint)
    item = _ladder_item(ladder_version)
    if item and item.get("status") == LadderStatus.READY and item.get("kit_fingerprint") == fingerprint:
        logger.info("kit already provisioned")
        return {"PhysicalResourceId": ladder_version}
    name = f"kit-{ladder_path(ladder_version)}-{digest(request_id)}"
    logger.set_correlation_id(name)
    execution = aws.client("stepfunctions").start_execution(
        stateMachineArn=config.provision_state_machine_arn,
        name=name,
        input=json.dumps(
            {
                "ladder": json.loads(properties["Ladder"]),
                "job_template": properties["JobTemplate"],
                "kit_fingerprint": fingerprint,
                "owner": Owner.IAC.value,
            }
        ),
    )
    logger.info("kit provisioning started", extra={"execution_arn": execution["executionArn"]})
    return {"PhysicalResourceId": ladder_version, "Data": {"ExecutionArn": execution["executionArn"]}}


@logger.inject_lambda_context(clear_state=True)
def is_complete(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Complete once the provisioner execution succeeded; the attributes come from the READY ladder item."""
    if event["RequestType"] == "Delete":
        return {"IsComplete": True}
    settings().require("library_table")
    if arn := (event.get("Data") or {}).get("ExecutionArn"):
        logger.set_correlation_id(arn.rsplit(":", 1)[-1])
        execution = aws.client("stepfunctions").describe_execution(executionArn=arn)
        if execution["status"] == _RUNNING:
            return {"IsComplete": False}
        if execution["status"] != "SUCCEEDED":
            detail = f"{execution.get('error', '')}: {execution.get('cause', '')}".strip(": ")
            raise KitProvisioningError(f"kit provisioning {execution['status']} ({arn}) {detail}"[:1000])
    ladder_version = event["ResourceProperties"]["LadderVersion"]
    item = _ladder_item(ladder_version)
    if not item or item.get("status") != LadderStatus.READY:
        raise KitProvisioningError(f"{ladder_version} is not READY after provisioning")
    return {"IsComplete": True, "Data": attributes(item)}


def attributes(item: Mapping[str, Any]) -> dict[str, str]:
    """Custom-resource attributes of a READY ladder item."""
    kit = item["kit"]
    return {
        "LadderVersion": item["ladder_version"],
        "JobTemplate": item["job_template"],
        "FillerVodSource": kit["filler"],
        **{f"BreakSlate{gops}": kit["break_slates"][str(gops)] for gops in BREAK_SLATE_GOPS},
        "HouseAds": ",".join(kit["house_ads"]),
        "VastKey": kit["vast_key"],
        "VastUrl": kit["vast_url"],
    }


def _ladder_item(ladder_version: str) -> dict[str, Any] | None:
    item = aws.table(settings().library_table).get_item(Key=ladder_key(ladder_version)).get("Item")
    return from_dynamo(item) if item else None
