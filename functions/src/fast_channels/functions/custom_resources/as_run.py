"""``Custom::AsRunLogGroup``: make sure Channel Assembly's as-run log group exists with a retention policy.

Channels with ``LogConfiguration.LogTypes: [AS_RUN]`` log to ``MediaTailor/Channel/AsRunLog``, one group shared by
every channel of the account and region. MediaTailor's service-linked role creates it on first use, without
retention (channel-assembly.md §7.2), so it cannot be a CloudFormation ``AWS::Logs::LogGroup`` (the creation would
fail where it already exists). This resource creates it when missing and sets its retention; deleting the resource
leaves the group to the account, since other deployments' channels may log to it (_review.md, as-run).
"""

from typing import TYPE_CHECKING, Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.data_classes import CloudFormationCustomResourceEvent, event_source
from botocore.exceptions import ClientError

from fast_channels.functions import aws

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()


@logger.inject_lambda_context(correlation_id_path="RequestId")
@event_source(data_class=CloudFormationCustomResourceEvent)
def handler(event: CloudFormationCustomResourceEvent, context: LambdaContext) -> dict[str, Any]:
    """Create (if missing) and set retention on Create and Update; Delete is a no-op."""
    name = event.resource_properties["LogGroupName"]
    if event.request_type != "Delete":
        ensure_log_group(name, retention_days=int(event.resource_properties["RetentionInDays"]))
    return {"PhysicalResourceId": name}


def ensure_log_group(name: str, *, retention_days: int) -> None:
    """Idempotently create log group ``name`` and set its retention."""
    logs = aws.client("logs")
    try:
        logs.create_log_group(logGroupName=name)
        logger.info("log group created", extra={"log_group": name})
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ResourceAlreadyExistsException":
            raise
    logs.put_retention_policy(logGroupName=name, retentionInDays=retention_days)
    logger.info("retention set", extra={"log_group": name, "retention_days": retention_days})
