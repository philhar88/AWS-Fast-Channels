"""CloudWatch vended-log and X-Ray trace deliveries of AgentCore resources (SPEC §10).

AgentCore Gateway and Memory create no deliveries of their own (agentcore.md §4.5). Every destination log group is
a :func:`~fast_channels.infra.constructs.logs.vended_log_group` (re-exported here) whose own resource policy
admits exactly the delivery source that feeds it, so no delivery appends to the account-wide
``AWSLogDeliveryWrite20150319`` policy and no ``AWS::Logs::ResourcePolicy`` is created (_review.md, observability
lens).
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Final

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_logs as logs
from aws_cdk import aws_xray as xray
from constructs import Construct

from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.logs import DELIVERY_PRINCIPAL, deliver, vended_log_group

__all__ = [
    "AGENTCORE_LOG_TYPES",
    "LOG_DELIVERY_PRINCIPAL",
    "AgentTelemetry",
    "LogDelivery",
    "LogType",
    "TraceDeliveryPolicy",
    "vended_log_group",
]

LOG_DELIVERY_PRINCIPAL: Final = DELIVERY_PRINCIPAL


class LogType(StrEnum):
    """Vended log types of AgentCore resources."""

    APPLICATION_LOGS = "APPLICATION_LOGS"
    USAGE_LOGS = "USAGE_LOGS"
    TRACES = "TRACES"


class LogDelivery(Construct):
    """Delivery of one log type of an AWS resource to a CloudWatch Logs group, or of its traces to X-Ray.

    Args:
        name: delivery source name, unique in the account and region (``[\\w-]``, at most 60 characters).
        resource_arn: ARN of the resource whose logs are delivered (Gateway, Memory, ...).
        log_type: what is delivered; ``TRACES`` go to X-Ray, which needs :class:`TraceDeliveryPolicy`.
        log_group: destination of every other log type.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        resource_arn: str,
        log_type: LogType,
        log_group: logs.ILogGroup | None = None,
    ) -> None:
        super().__init__(scope, id)
        if (log_type is LogType.TRACES) != (log_group is None):
            raise ValueError("TRACES are delivered to X-Ray; every other log type needs a log group")
        self.source = logs.CfnDeliverySource(self, "Source", name=name, log_type=log_type, resource_arn=resource_arn)
        if log_group is None:
            self.destination = logs.CfnDeliveryDestination(
                self, "Destination", name=name, delivery_destination_type="XRAY"
            )
        else:
            self.destination = logs.CfnDeliveryDestination(
                self,
                "Destination",
                name=name,
                delivery_destination_type="CWL",
                destination_resource_arn=log_group.log_group_arn,
            )
        self.delivery = deliver(
            self, "Resource", source=self.source, destination_arn=self.destination.attr_arn, after=self.source
        )


class TraceDeliveryPolicy(Construct):
    """X-Ray resource policy that lets CloudWatch vended logs deliver the traces of ``resource_arns``, from
    delivery sources named ``delivery_sources`` (a name or pattern).

    X-Ray allows only a handful of resource policies per account and region, so one deployment uses one policy
    for all its AgentCore resources; deliveries of TRACES must depend on it.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        policy_name: str,
        resource_arns: Sequence[str],
        delivery_sources: str = "*",
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        statement = {
            "Sid": "AgentCoreTraceDelivery",
            "Effect": "Allow",
            "Principal": {"Service": DELIVERY_PRINCIPAL},
            "Action": "xray:PutTraceSegments",
            "Resource": "*",
            "Condition": {
                "ForAllValues:ArnLike": {"logs:LogGeneratingResourceArns": list(resource_arns)},
                "StringEquals": {"aws:SourceAccount": stack.account},
                "ArnLike": {"aws:SourceArn": arn(self, "logs", "delivery-source", delivery_sources, colon=True)},
            },
        }
        self.policy = xray.CfnResourcePolicy(
            self,
            "Resource",
            policy_name=policy_name,
            policy_document=stack.to_json_string({"Version": "2012-10-17", "Statement": [statement]}),
        )


AGENTCORE_LOG_TYPES: Final = {
    "gateway": (LogType.APPLICATION_LOGS,),
    "memory": (LogType.APPLICATION_LOGS,),
    "runtime": (LogType.APPLICATION_LOGS, LogType.USAGE_LOGS),
}
"""Vended log types delivered per AgentCore resource (SPEC §10)."""


class AgentTelemetry(Construct):
    """Vended logs of the Gateway, Memory and Runtime, plus their traces when CloudWatch Transaction Search is on.

    Logs go to ``/aws/vendedlogs/bedrock-agentcore/<prefix>/<resource>/<LOG_TYPE>`` with retention; each group admits
    only its own delivery source ``<prefix>-<resource>-<log-type>``. X-Ray rejects
    trace deliveries while the account's trace segment destination is not CloudWatch Logs ("X-Ray Delivery
    Destination is supported with CloudWatch Logs as a Trace Segment Destination", observed), so ``traces`` must
    only be set when Transaction Search is enabled.

    Args:
        prefix: deployment prefix (log group and delivery names).
        resources: ARN of the ``gateway``, ``memory`` and ``runtime``.
        traces: also deliver TRACES to X-Ray.
        log_retention, removal_policy: of the log groups.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        resources: Mapping[str, str],
        traces: bool,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        if unknown := sorted(set(resources) - set(AGENTCORE_LOG_TYPES)):
            raise ValueError(f"no log types known for {unknown}")
        self.log_groups: dict[tuple[str, LogType], logs.LogGroup] = {}
        for resource, resource_arn in resources.items():
            for log_type in AGENTCORE_LOG_TYPES[resource]:
                key = f"{resource.title()}{log_type.name.title().replace('_', '')}"
                name = _delivery_name(prefix, resource, log_type)
                group = vended_log_group(
                    self,
                    key,
                    name=f"/aws/vendedlogs/bedrock-agentcore/{prefix}/{resource}/{log_type}",
                    delivery_sources=name,
                    retention=log_retention,
                    removal_policy=removal_policy,
                )
                self.log_groups[resource, log_type] = group
                delivery = LogDelivery(
                    self,
                    f"{key}Delivery",
                    name=name,
                    resource_arn=resource_arn,
                    log_type=log_type,
                    log_group=group,
                )
                delivery.node.add_dependency(group)
        if traces:
            policy = TraceDeliveryPolicy(
                self,
                "TracePolicy",
                policy_name=f"{prefix}-agentcore-traces",
                resource_arns=list(resources.values()),
                delivery_sources=_delivery_name(prefix, "*", LogType.TRACES),
            )
            for resource, resource_arn in resources.items():
                delivery = LogDelivery(
                    self,
                    f"{resource.title()}Traces",
                    name=_delivery_name(prefix, resource, LogType.TRACES),
                    resource_arn=resource_arn,
                    log_type=LogType.TRACES,
                )
                delivery.node.add_dependency(policy)


def _delivery_name(prefix: str, resource: str, log_type: LogType) -> str:
    return f"{prefix}-{resource}-{log_type.lower().replace('_', '-')}"
