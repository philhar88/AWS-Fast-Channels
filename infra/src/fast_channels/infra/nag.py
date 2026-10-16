"""cdk-nag v3 acknowledgements: always per construct, always with a reason.

cdk-nag v3 runs as a CDK policy-validation plugin (``Validations.of(app).add_plugins(...)``, see ``app.py``) and
replaced v2's ``NagSuppressions`` with ``Validations.of(scope).acknowledge(...)``. An acknowledgement covers the
construct and its descendants. v3 has no prefix matching: every finding id (e.g. ``AwsSolutions-IAM5[Resource::*]``)
is acknowledged individually, so acknowledge on the narrowest construct that causes it (cdk-standards.md §3).
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from aws_cdk import Acknowledgment, Validations

if TYPE_CHECKING:
    from constructs import IConstruct

LAMBDA_DLQ: Final = "Serverless-LambdaDLQ"
S3_SERVER_ACCESS_LOGS: Final = "AwsSolutions-S1"
SQS_DLQ: Final = "AwsSolutions-SQS3"
EMPTY_TEMPLATE: Final = "CloudFormation-Validate::F0001"
"""CDK's default CloudFormation rules (cdk.json ``validateAgainstDefaultRules``) reject stacks without resources."""


def acknowledge(scope: IConstruct, findings: Mapping[str, str]) -> None:
    """Acknowledge cdk-nag ``findings`` (finding id -> reason) on ``scope`` and its descendants."""
    Validations.of(scope).acknowledge(*(Acknowledgment(id=id_, reason=reason) for id_, reason in findings.items()))


def managed_policy(policy: str) -> str:
    """``AwsSolutions-IAM4`` finding id for an AWS managed policy, e.g. ``"service-role/AWSLambdaRole"``."""
    return f"AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/{policy}]"


def wildcard_resource(resource: str = "*") -> str:
    """``AwsSolutions-IAM5`` finding id for a wildcard resource, e.g. ``"*"`` or ``"<MediaBucket.Arn>/*"``."""
    return f"AwsSolutions-IAM5[Resource::{resource}]"


def wildcard_action(action: str) -> str:
    """``AwsSolutions-IAM5`` finding id for a wildcard action, e.g. ``"s3:GetObject*"``."""
    return f"AwsSolutions-IAM5[Action::{action}]"
