"""``Custom::DesiredState``: the bundle CDK rendered, published at deploy time (DESIGN §8.1).

CloudFormation resolves the bundle's deploy-time values (the channels' URLs, the CDN), then this resource validates it
with every rule (a bundle that breaks one fails the deployment, before anything is applied), writes it to the release
bucket under its digest and, when the digest is new, puts ``Desired State Rendered`` on the stage's bus. The Release
Controller's state machine starts from that event. Deleting the resource leaves the bundles: the bucket goes with the
stack.
"""

import json
from typing import TYPE_CHECKING, Any

from aws_lambda_powertools import Logger

from fast_channels.core.events import DesiredStateRendered, put_events_entry
from fast_channels.core.monetization import names
from fast_channels.core.monetization.bundle import Bundle, bundle_document
from fast_channels.core.monetization.validators import require_valid, validate_bundle
from fast_channels.functions import aws

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()


def _digest(properties: dict[str, Any] | None) -> str | None:
    if not properties or "Bundle" not in properties:
        return None
    return Bundle.model_validate(json.loads(properties["Bundle"])).digest


def publish(event: dict[str, Any]) -> dict[str, Any]:
    """Validate, store and announce the bundle of a Create or Update."""
    properties = event["ResourceProperties"]
    bundle = Bundle.model_validate(json.loads(properties["Bundle"]))
    require_valid(validate_bundle(bundle))
    key = names.bundle_key(bundle.digest)
    aws.client("s3").put_object(
        Bucket=properties["Bucket"],
        Key=key,
        Body=json.dumps(bundle_document(bundle), indent=2, sort_keys=True).encode(),
        ContentType="application/json",
    )
    if _digest(event.get("OldResourceProperties")) == bundle.digest:
        logger.info("bundle unchanged", extra={"digest": bundle.digest})
    else:
        detail = DesiredStateRendered(bundle_key=key, digest=bundle.digest, correlation_id=event["RequestId"])
        response = aws.client("events").put_events(
            Entries=[put_events_entry(detail, prefix=properties["Prefix"], event_bus=properties["EventBus"])]
        )
        if response.get("FailedEntryCount"):
            raise RuntimeError(f"Desired State Rendered was not published: {response['Entries']}")
        logger.info("bundle published", extra={"digest": bundle.digest, "bundle_key": key})
    return {
        "PhysicalResourceId": f"{properties['Prefix']}-desired-state",
        "Data": {"BundleKey": key, "Digest": bundle.digest},
    }


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """The custom resource's Create, Update and Delete."""
    logger.set_correlation_id(event.get("RequestId"))
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": event["PhysicalResourceId"]}
    return publish(event)
