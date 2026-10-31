"""CloudWatch Logs plumbing shared by every plane: log groups that AWS services write to, and vended-log deliveries.

A log group that a service writes to carries its own log-group-scoped resource policy (``AWS::Logs::LogGroup``
``ResourcePolicyDocument``). The account-level ``AWS::Logs::ResourcePolicy`` (and the L2 ``add_to_resource_policy``,
which grows it) is limited to 10 documents per region of 5,120 characters each, shared by every application in the
account (e2e.md §2.4, _review.md). Every policy here names the account and the exact delivery sources or rule that
may write.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, cast

import jsii
from aws_cdk import CfnResource, RemovalPolicy, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_logs as logs
from aws_cdk.interfaces.aws_events import IRuleRef  # noqa: TC002 - jsii resolves bind() annotations at runtime

from fast_channels.infra.arns import arn

if TYPE_CHECKING:
    from constructs import Construct, IConstruct

VENDED_LOGS_PREFIX: Final = "/aws/vendedlogs/"
"""Log groups named like this never grow the account-level resource policy of CloudWatch Logs."""
DELIVERY_PRINCIPAL: Final = "delivery.logs.amazonaws.com"


def log_group_arn(scope: IConstruct, name: str) -> str:
    """ARN of the log group ``name`` in ``scope``'s stack, without the ``:*`` suffix of ``LogGroup.log_group_arn``."""
    return arn(scope, "logs", "log-group", name, colon=True)


def _service_log_group(
    scope: Construct,
    id: str,
    *,
    name: str,
    statement: Mapping[str, Any],
    retention: logs.RetentionDays,
    removal_policy: RemovalPolicy,
) -> logs.LogGroup:
    log_group = logs.LogGroup(
        scope,
        id,
        log_group_name=name,
        retention=retention,
        removal_policy=removal_policy,
        log_group_class=logs.LogGroupClass.STANDARD,  # INFREQUENT_ACCESS has no metric filters or live tail
    )
    cfn = cast("logs.CfnLogGroup", log_group.node.default_child)
    cfn.resource_policy_document = {"Version": "2012-10-17", "Statement": [statement]}
    return log_group


def vended_log_group(
    scope: Construct,
    id: str,
    *,
    name: str,
    delivery_sources: str,
    retention: logs.RetentionDays,
    removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    sid: str = "VendedLogDelivery",
) -> logs.LogGroup:
    """A ``/aws/vendedlogs/...`` log group that vended-log deliveries from this account's delivery sources named
    ``delivery_sources`` (one name, or a pattern such as ``"fast-*"``) may write to (verified through
    CloudFormation for the event-bus, MediaTailor and AgentCore deliveries).

    Raises:
        ValueError: ``name`` does not start with ``/aws/vendedlogs/``.
    """
    if not name.startswith(VENDED_LOGS_PREFIX):
        raise ValueError(f"vended-log destinations must be named {VENDED_LOGS_PREFIX}...: {name}")
    statement = {
        "Sid": sid,
        "Effect": "Allow",
        "Principal": {"Service": DELIVERY_PRINCIPAL},
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": f"{log_group_arn(scope, name)}:log-stream:*",
        "Condition": {
            "StringEquals": {"aws:SourceAccount": Stack.of(scope).account},
            "ArnLike": {"aws:SourceArn": arn(scope, "logs", "delivery-source", delivery_sources, colon=True)},
        },
    }
    return _service_log_group(
        scope, id, name=name, statement=statement, retention=retention, removal_policy=removal_policy
    )


def rule_log_group(
    scope: Construct,
    id: str,
    *,
    name: str,
    rule_name: str,
    retention: logs.RetentionDays,
    removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
) -> logs.LogGroup:
    """A log group that the default-bus EventBridge rule ``rule_name`` may write matched events to (verified with
    MediaConvert job events, 2026-10-16); deliver to it with :class:`LogGroupTarget`.

    The rule ARN is built from its name, so the policy does not reference the rule, which references this group.
    """
    statement = {
        "Sid": "EventBridgeRuleTarget",
        "Effect": "Allow",
        "Principal": {"Service": "events.amazonaws.com"},
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": f"{log_group_arn(scope, name)}:*",
        "Condition": {
            "StringEquals": {"aws:SourceAccount": Stack.of(scope).account},
            "ArnEquals": {"aws:SourceArn": arn(scope, "events", "rule", rule_name)},
        },
    }
    return _service_log_group(
        scope, id, name=name, statement=statement, retention=retention, removal_policy=removal_policy
    )


# jsii calls bind(): its annotation types are runtime imports.
@jsii.implements(events.IRuleTarget)
class LogGroupTarget:
    """Rule target delivering events to a :func:`rule_log_group`, whose own resource policy admits the rule.
    ``targets.CloudWatchLogGroup`` would add an account-level resource policy through a custom resource."""

    def __init__(self, log_group: logs.ILogGroup) -> None:
        self._log_group = log_group

    def bind(self, rule: IRuleRef, id: str | None = None) -> events.RuleTargetConfig:
        """The log group ARN as the rule's target."""
        return events.RuleTargetConfig(arn=log_group_arn(self._log_group, self._log_group.log_group_name))


def deliver(
    scope: Construct,
    id: str,
    *,
    source: logs.CfnDeliverySource | str,
    destination_arn: str,
    after: CfnResource | None = None,
    **options: Any,
) -> logs.CfnDelivery:
    """A vended-log delivery from ``source`` (a delivery source, or the name of one that another stack created) to
    the delivery destination ``destination_arn`` (``options`` are further ``CfnDelivery`` properties, such as
    ``record_fields``).

    CloudWatch Logs creates the deliveries of one source, or to one destination, one at a time: pass the previous
    one as ``after`` to serialize them.
    """
    name = source.ref if isinstance(source, logs.CfnDeliverySource) else source
    delivery = logs.CfnDelivery(
        scope, id, delivery_source_name=name, delivery_destination_arn=destination_arn, **options
    )
    if after is not None:
        delivery.add_resource_dependency(after)
    return delivery
