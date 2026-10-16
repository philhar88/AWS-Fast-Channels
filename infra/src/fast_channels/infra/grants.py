"""Least-privilege grants that explain their wildcards to cdk-nag in the same call."""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from aws_cdk import aws_iam as iam

from fast_channels.infra import nag

if TYPE_CHECKING:
    from constructs import IConstruct


def allow(
    grantee: iam.IGrantable,
    actions: Sequence[str],
    resources: Sequence[str],
    *,
    why: str | None = None,
    conditions: Mapping[str, Any] | None = None,
) -> None:
    """Allow ``grantee`` ``actions`` on ``resources`` with one statement in its principal (role) policy.

    ``why`` is the reason cdk-nag gets for every wildcard resource (``AwsSolutions-IAM5``). Only ``*`` itself may go
    without one, for actions that have no resource-level permissions: the role of a ``FastFunction`` already
    acknowledges it for X-Ray.

    Raises:
        ValueError: a resource other than ``*`` has a wildcard and ``why`` is missing.
    """
    wildcards = [resource for resource in resources if "*" in resource]
    if why is None and any(resource != "*" for resource in wildcards):
        raise ValueError(f"explain why {actions} need the wildcard resources {wildcards}")
    principal = grantee.grant_principal
    principal.add_to_principal_policy(
        iam.PolicyStatement(actions=list(actions), resources=list(resources), conditions=conditions)
    )
    if why is not None and wildcards:
        nag.acknowledge_references(
            cast("IConstruct", principal), {nag.wildcard_resource(resource): why for resource in wildcards}
        )
