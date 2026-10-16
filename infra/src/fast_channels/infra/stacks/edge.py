"""``FastChannels-Edge``: CloudFront resources that must live in us-east-1 when the deployment runs elsewhere."""

from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Stack

from fast_channels.infra import nag

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig
    from fast_channels.infra.stacks.data import DataExports

EDGE_REGION: Final = "us-east-1"
"""Region of CloudFront's control plane: standard-logs v2 delivery sources, ACM certificates, WAF web ACLs."""


class EdgeStack(Stack):
    """Hosts ``delivery.CdnAccessLogs`` (and later a CLOUDFRONT-scope WAF) when the stage's region is not
    us-east-1; in us-east-1 those constructs live in the Media stack and this stack is not created.

    Cross-region wiring (verified by ``infra/tests/test_stage.py``): with weak cross-stack references (cdk.json
    ``@aws-cdk/core:defaultCrossStackReferences``) a value from the deployment region resolves to
    ``Fn::GetStackOutput {StackName, Region, OutputName}``; no ``cross_region_references`` custom resources.
    That works for L1 attribute tokens: :class:`DataExports` references and ``Distribution.distribution_arn``.
    An L2 attribute of a resource with a generated name (``Bucket.bucket_arn``) raises
    ``CannotUseCrossEnvironment`` instead.

    Args:
        config: deployment configuration.
        data: DataStack references (``logs_bucket`` receives the CloudFront logs).
    """

    def __init__(
        self, scope: Construct, id: str, *, config: FastChannelsConfig, data: DataExports, **kwargs: Any
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): CloudFront log delivery", **kwargs)
        self.logs_bucket = data.logs_bucket
        # Integration point (stage-2 delivery agent): add a `distribution` parameter (the stage passes
        # media.distribution), create delivery.CdnAccessLogs(distribution, logs_bucket=self.logs_bucket) here and
        # drop this acknowledgement: CloudFormation cannot deploy a stack without resources.
        nag.acknowledge(self, {nag.EMPTY_TEMPLATE: "Shell stack until CdnAccessLogs is added"})
