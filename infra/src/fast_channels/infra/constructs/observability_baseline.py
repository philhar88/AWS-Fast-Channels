"""Account-level observability baseline: CloudWatch Transaction Search."""

from typing import Final

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_logs as logs
from aws_cdk import aws_xray as xray
from constructs import Construct

POLICY_NAME: Final = "FastChannels-TransactionSearch"


class TransactionSearch(Construct):
    """Send X-Ray spans to CloudWatch Logs (``aws/spans``) and index a sample of them (Transaction Search).

    AgentCore Runtime spans, Gateway and Memory traces and online evaluations need it (agentcore.md §4.5). It is
    an account-and-region singleton that changes how spans are billed, so this construct belongs in its own stack,
    and both resources are retained on deletion: deleting a FAST Channels stack never turns it off for the account.

    Args:
        indexing_percentage: share of spans indexed as trace summaries (1 % is free).
    """

    def __init__(self, scope: Construct, id: str, *, indexing_percentage: float = 1) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        log_groups = f"arn:{stack.partition}:logs:{stack.region}:{stack.account}:log-group"
        statement = {
            "Sid": "TransactionSearchXRayAccess",
            "Effect": "Allow",
            "Principal": {"Service": "xray.amazonaws.com"},
            "Action": "logs:PutLogEvents",
            "Resource": [f"{log_groups}:aws/spans:*", f"{log_groups}:/aws/application-signals/data:*"],
            "Condition": {
                "ArnLike": {"aws:SourceArn": f"arn:{stack.partition}:xray:{stack.region}:{stack.account}:*"},
                "StringEquals": {"aws:SourceAccount": stack.account},
            },
        }
        self.policy = logs.CfnResourcePolicy(
            self,
            "SpansPolicy",
            policy_name=POLICY_NAME,
            policy_document=stack.to_json_string({"Version": "2012-10-17", "Statement": [statement]}),
        )
        self.config = xray.CfnTransactionSearchConfig(self, "Config", indexing_percentage=indexing_percentage)
        self.config.add_resource_dependency(self.policy)
        for resource in (self.policy, self.config):
            resource.apply_removal_policy(RemovalPolicy.RETAIN)
