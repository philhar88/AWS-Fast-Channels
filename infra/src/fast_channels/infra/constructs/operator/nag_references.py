"""cdk-nag acknowledgments of findings whose ids contain CloudFormation references.

cdk-nag v3 matches acknowledgment ids exactly, and renders references in them with its ``flattenCfnReference``:
``<Logical.Attr>`` for ``Fn::GetAtt``, ``<Logical>`` for ``Ref`` and JSON for anything else, such as the
``Fn::GetStackOutput`` of a weak cross-stack reference. That JSON exists only once CDK has resolved the
references between stacks, so these acknowledgments are recorded by a construct validation, which runs after that
step and before the policy-validation plugins.
"""

import json
from collections.abc import Mapping
from typing import Any

# jsii calls ``validate`` back and resolves annotations at runtime: keep these imports out of TYPE_CHECKING.
import jsii
from aws_cdk import DefaultStackSynthesizer, Stack
from constructs import IConstruct, IValidation

from fast_channels.infra import nag


def acknowledge_references(scope: IConstruct, findings: Mapping[str, str]) -> None:
    """Acknowledge cdk-nag ``findings`` (finding id -> reason) on ``scope``, where ids may embed CDK tokens, e.g.
    ``nag.wildcard_resource(f"{bucket.bucket_arn}/epg/*")``."""
    scope.node.add_validation(_ResolvedAcknowledgments(scope, findings))


@jsii.implements(IValidation)
class _ResolvedAcknowledgments:
    def __init__(self, scope: IConstruct, findings: Mapping[str, str]) -> None:
        self._scope = scope
        self._findings = dict(findings)

    def validate(self) -> list[str]:
        stack = Stack.of(self._scope)
        nag.acknowledge(self._scope, {flatten(stack.resolve(id_)): reason for id_, reason in self._findings.items()})
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


def file_asset_objects(stack: Stack) -> str:
    """ARN of the objects of the stack's CDK file-asset bucket (what asset readers are granted)."""
    synthesizer = stack.synthesizer
    qualifier = getattr(synthesizer, "bootstrap_qualifier", None) or DefaultStackSynthesizer.DEFAULT_QUALIFIER
    bucket = (
        DefaultStackSynthesizer.DEFAULT_FILE_ASSETS_BUCKET_NAME.replace("${Qualifier}", qualifier)
        .replace("${AWS::AccountId}", stack.account)
        .replace("${AWS::Region}", stack.region)
    )
    return f"arn:{stack.partition}:s3:::{bucket}/*"
