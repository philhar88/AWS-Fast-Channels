"""ARNs of a deployment's resources, for IAM and resource policies."""

from typing import TYPE_CHECKING

from aws_cdk import ArnFormat, Stack

if TYPE_CHECKING:
    from constructs import IConstruct


def arn(scope: IConstruct, service: str, resource: str, name: str, *, colon: bool = False) -> str:
    """``arn:<partition>:<service>:<region>:<account>:<resource>/<name>`` in ``scope``'s stack, or
    ``...:<resource>:<name>`` with ``colon`` (log groups, delivery sources, executions).

    The account and region are literals where the stack's environment is concrete and pseudo parameters otherwise,
    as with ``Stack.format_arn``; ``name`` may hold wildcards or tokens.
    """
    return Stack.of(scope).format_arn(
        service=service,
        resource=resource,
        resource_name=name,
        arn_format=ArnFormat.COLON_RESOURCE_NAME if colon else ArnFormat.SLASH_RESOURCE_NAME,
    )
