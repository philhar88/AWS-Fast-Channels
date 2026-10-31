"""The Release Controller's ownership guard (DESIGN §8.1): no other role of the stage may change what it owns.

With the release module on, every IAM role of the stage gets an explicit Deny of the writes the controller owns, on
the resources it owns: ``PutPlaybackConfiguration``/``DeletePlaybackConfiguration`` on the monetized channels'
siblings (``<channel>-p-*``), ``PutFunction``/``DeleteFunction`` on the deployment's Functions (``<p>_*``),
``CreatePrefetchSchedule``/``DeletePrefetchSchedule`` on the siblings' prefetch schedules, and ``PutChannelPolicy`` on
the monetized channels. Without it the agent tools' tag-conditioned ``PutPlaybackConfiguration`` on ``*`` could rewrite
a monetized configuration (and its FunctionMapping). The Release Controller's own roles are exempt; a break-glass
operator acts outside the stage. CloudFormation creates the channel policy with the deploying role, which is not a
stage role.

The same policy denies reading the siblings' log group (``LOG_READ_ACTIONS``) to every role of the stage but its named
readers, which are exempt as the controller's roles are: the group's KMS key does not limit who reads it.

One customer managed policy per stack holds the Deny and is attached to each of the stack's roles: attached policies
do not count against a role's inline policy size.
"""

from collections.abc import Iterable, Sequence
from typing import Final

import jsii
from aws_cdk import IAspect, Stack
from aws_cdk import aws_iam as iam
from constructs import IConstruct  # noqa: TC002 - jsii resolves visit()'s annotations at runtime

from fast_channels.core.monetization import names

DENIED_ACTIONS: Final = (
    "mediatailor:PutPlaybackConfiguration",
    "mediatailor:DeletePlaybackConfiguration",
    "mediatailor:PutFunction",
    "mediatailor:DeleteFunction",
    "mediatailor:CreatePrefetchSchedule",
    "mediatailor:DeletePrefetchSchedule",
    "mediatailor:PutChannelPolicy",
)


LOG_READ_ACTIONS: Final = (
    "logs:GetLogEvents",
    "logs:FilterLogEvents",
    "logs:StartQuery",
    "logs:StartLiveTail",
    "logs:CreateExportTask",
    "logs:PutSubscriptionFilter",
    "logs:DisassociateKmsKey",
)
"""Ways to read (or copy out) the siblings' log group. Its KMS key does not limit them: CloudWatch Logs decrypts for
any caller IAM lets read (observed on fastm, 2026-10-29), so the stage's other roles are denied them here."""


def owned_resources(*, partition: str, region: str, account: str, prefix: str, channels: Sequence[str]) -> list[str]:
    """ARNs of everything the Release Controller owns in the deployment."""
    base = f"arn:{partition}:mediatailor:{region}:{account}"
    return [
        *(f"{base}:playbackConfiguration/{names.sibling_pattern(c)}" for c in channels),
        *(f"{base}:prefetchSchedule/{names.sibling_pattern(c)}/*" for c in channels),
        *(f"{base}:channel/{c}" for c in channels),
        f"{base}:function/{names.function_pattern(prefix)}",
    ]


def deny_statement(
    *, partition: str, region: str, account: str, prefix: str, channels: Sequence[str]
) -> iam.PolicyStatement:
    """The Deny every role of the stage but the controller's gets."""
    return iam.PolicyStatement(
        sid="OnlyTheReleaseControllerChangesMonetization",
        effect=iam.Effect.DENY,
        actions=list(DENIED_ACTIONS),
        resources=owned_resources(
            partition=partition, region=region, account=account, prefix=prefix, channels=channels
        ),
    )


def log_deny_statement(*, partition: str, region: str, account: str, prefix: str) -> iam.PolicyStatement:
    """The Deny of reading the siblings' log group, for every role of the stage but its named readers."""
    group = f"arn:{partition}:logs:{region}:{account}:log-group:{names.log_group(prefix)}"
    return iam.PolicyStatement(
        sid="OnlyNamedReadersReadTheSiblingsLogs",
        effect=iam.Effect.DENY,
        actions=list(LOG_READ_ACTIONS),
        resources=[group, f"{group}:*"],
    )


@jsii.implements(IAspect)
class ReleaseGuard:
    """Attach the Deny to every :class:`aws_iam.Role` of the stage except ``exempt`` (by node path prefix).

    Args:
        prefix: deployment prefix.
        channels: the monetized channels.
        region: the deployment's region (the guard's ARNs name it, also in stacks of other regions).
        exempt: node paths of the constructs whose roles are the controller's or read the siblings' logs (each
            path and what is under it). None of them may do what the guard denies the others without its own grant.
    """

    def __init__(self, *, prefix: str, channels: Sequence[str], region: str, exempt: Iterable[str] = ()) -> None:
        self._prefix, self._channels, self._region = prefix, tuple(channels), region
        self._exempt = tuple(exempt)
        self._policies: dict[str, iam.ManagedPolicy] = {}

    def exempt(self, *paths: str) -> None:
        """Exempt the roles under ``paths`` (the controller's)."""
        self._exempt += paths

    def visit(self, node: IConstruct) -> None:
        path = node.node.path
        if not isinstance(node, iam.Role) or any(path == e or path.startswith(f"{e}/") for e in self._exempt):
            return
        stack = Stack.of(node)
        policy = self._policies.get(stack.node.path)
        if policy is None:
            policy = iam.ManagedPolicy(
                stack,
                "ReleaseGuard",
                description=f"FAST Channels ({self._prefix}): only the Release Controller changes monetization",
                statements=[
                    deny_statement(
                        partition=stack.partition,
                        region=self._region,
                        account=stack.account,
                        prefix=self._prefix,
                        channels=self._channels,
                    ),
                    log_deny_statement(
                        partition=stack.partition, region=self._region, account=stack.account, prefix=self._prefix
                    ),
                ],
            )
            self._policies[stack.node.path] = policy
        node.add_managed_policy(policy)
