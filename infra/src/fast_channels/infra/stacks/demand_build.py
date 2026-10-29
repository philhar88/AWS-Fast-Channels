"""``FastChannels-<prefix>-DemandBuild``: what the Prebid Server guidance is built and deployed with."""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack, Tags

from fast_channels.core.demand import MODULE, MODULE_TAG
from fast_channels.infra.constructs.demand.guidance_bootstrap import GuidanceExecutionPolicy
from fast_channels.infra.constructs.demand.image_build import ImageBuild

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig


class DemandBuildStack(Stack):
    """The Prebid Server image repository and its x86 build project, and the CloudFormation execution policy of the
    guidance's dedicated CDK bootstrap.

    Deployed before the bootstrap (which takes ``GuidanceExecutionPolicyArn``) and before the guidance (which runs
    the image in ``ImageRepositoryUri``). It takes no references from other stacks.

    Args:
        config: deployment configuration (``prefix``, ``removal``, ``observability.log_retention_days``).

    Attributes:
        image: the repository and the build project.
        execution_policy: the guidance's CloudFormation execution policy.
    """

    def __init__(self, scope: Construct, id: str, *, config: FastChannelsConfig, **kwargs: Any) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): demand image and deploy", **kwargs)
        Tags.of(self).add(MODULE_TAG, MODULE)
        self.image = ImageBuild(
            self,
            "Image",
            prefix=config.prefix,
            removal_policy=config.removal_policy,
            log_retention=config.observability.log_retention,
        )
        self.execution_policy = GuidanceExecutionPolicy(self, "GuidanceBootstrap", prefix=config.prefix)
        outputs = {
            "ImageRepositoryUri": (self.image.repository.repository_uri, "ECR repository of the Prebid Server images"),
            "ImageBuildProject": (self.image.project.project_name, "x86 CodeBuild project that builds them"),
            "GuidanceExecutionPolicyArn": (
                self.execution_policy.policy.managed_policy_arn,
                "CloudFormation execution policy of the guidance's dedicated CDK bootstrap",
            ),
        }
        for key, (value, description) in outputs.items():
            CfnOutput(self, key, value=value, description=description)
