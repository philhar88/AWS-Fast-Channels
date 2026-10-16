"""cdk-nag v3 acknowledgements: always per construct, always with a reason.

cdk-nag v3 runs as a CDK policy-validation plugin (``Validations.of(app).add_plugins(...)``, see ``app.py``) and
replaced v2's ``NagSuppressions`` with ``Validations.of(scope).acknowledge(...)``. An acknowledgement covers the
construct and its descendants. v3 has no prefix matching: every finding id (e.g. ``AwsSolutions-IAM5[Resource::*]``)
is acknowledged individually, so acknowledge on the narrowest construct that causes it (cdk-standards.md §3).

Finding ids render CloudFormation references with cdk-nag's ``flattenCfnReference``: ``<Logical.Attr>`` for
``Fn::GetAtt``, ``<Logical>`` for ``Ref`` and JSON for anything else, such as the ``Fn::GetStackOutput`` of a weak
cross-stack reference. That JSON exists only once CDK has resolved the references between stacks, so
:func:`acknowledge_references` records its acknowledgements in a construct validation, which runs after that step
and before the policy-validation plugins.
"""

import json
from collections.abc import Mapping
from typing import Any, Final

# jsii calls ``validate`` back and resolves annotations at runtime: keep these imports out of TYPE_CHECKING.
import jsii
from aws_cdk import Acknowledgment, DefaultStackSynthesizer, Stack, Validations
from constructs import IConstruct, IValidation

LAMBDA_DLQ: Final = "Serverless-LambdaDLQ"
S3_SERVER_ACCESS_LOGS: Final = "AwsSolutions-S1"
SQS_DLQ: Final = "AwsSolutions-SQS3"


def acknowledge(scope: IConstruct, findings: Mapping[str, str]) -> None:
    """Acknowledge cdk-nag ``findings`` (finding id -> reason) on ``scope`` and its descendants."""
    Validations.of(scope).acknowledge(*(Acknowledgment(id=id_, reason=reason) for id_, reason in findings.items()))


def acknowledge_references(scope: IConstruct, findings: Mapping[str, str]) -> None:
    """:func:`acknowledge` for finding ids that embed CDK tokens, e.g.
    ``nag.wildcard_resource(f"{bucket.bucket_arn}/epg/*")`` on an imported (cross-stack) bucket."""
    scope.node.add_validation(_ResolvedAcknowledgments(scope, findings))


@jsii.implements(IValidation)
class _ResolvedAcknowledgments:
    def __init__(self, scope: IConstruct, findings: Mapping[str, str]) -> None:
        self._scope = scope
        self._findings = dict(findings)

    def validate(self) -> list[str]:
        stack = Stack.of(self._scope)
        acknowledge(self._scope, {flatten(stack.resolve(id_)): reason for id_, reason in self._findings.items()})
        return []


def flatten(value: Any) -> str:
    """cdk-nag's ``flattenCfnReference`` (cdk-nag 3.0.2, ``utils/flatten-cfn-reference``)."""
    match value:
        case None:
            text = ""
        case str():
            text = value.replace("${", "<").replace("}", ">")
        case {"Fn::Join": [delimiter, items]}:
            text = str(delimiter).join(flatten(item) for item in items)
        case {"Fn::Sub": inner} | {"Fn::ImportValue": inner}:
            text = flatten(inner)
        case {"Fn::GetAtt": [resource, attribute]}:
            text = f"<{flatten(resource)}.{flatten(attribute)}>"
        case {"Ref": name}:
            text = f"<{flatten(name)}>"
        case _:
            text = json.dumps(value, separators=(",", ":"))
    return text


def managed_policy(policy: str) -> str:
    """``AwsSolutions-IAM4`` finding id for an AWS managed policy, e.g. ``"service-role/AWSLambdaRole"``."""
    return f"AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/{policy}]"


def wildcard_resource(resource: str = "*") -> str:
    """``AwsSolutions-IAM5`` finding id for a wildcard resource, e.g. ``"*"`` or ``"<MediaBucket.Arn>/*"``."""
    return f"AwsSolutions-IAM5[Resource::{resource}]"


def wildcard_action(action: str) -> str:
    """``AwsSolutions-IAM5`` finding id for a wildcard action, e.g. ``"s3:GetObject*"``."""
    return f"AwsSolutions-IAM5[Action::{action}]"


def file_asset_objects(stack: Stack) -> str:
    """ARN of the objects of the stack's CDK file-asset bucket (what CDK grants asset readers), for
    :func:`wildcard_resource`."""
    synthesizer = stack.synthesizer
    qualifier = getattr(synthesizer, "bootstrap_qualifier", None) or DefaultStackSynthesizer.DEFAULT_QUALIFIER
    bucket = (
        DefaultStackSynthesizer.DEFAULT_FILE_ASSETS_BUCKET_NAME.replace("${Qualifier}", qualifier)
        .replace("${AWS::AccountId}", stack.account)
        .replace("${AWS::Region}", stack.region)
    )
    return f"arn:{stack.partition}:s3:::{bucket}/*"
