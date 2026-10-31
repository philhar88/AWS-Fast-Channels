"""``GuidanceExecutionPolicy``: what CloudFormation may do when it deploys the Prebid Server guidance.

The guidance deploys onto a CDK bootstrap of its own (overlay O1; qualifier = the prefix; toolkit stack
``<prefix>-cdk-bootstrap``), never onto the account's shared ``hnb659fds`` bootstrap, whose execution role is often
AdministratorAccess. ``scripts/prebid_guidance.py bootstrap`` creates that bootstrap with this managed policy as its
CloudFormation execution policy. The policy (``vendor/prebid-guidance/bootstrap-policy.json``) allows the services the
guidance's resources need; S3, Lambda and IAM only on the guidance's own names (``<prefix>-pbs*``); managed-policy
attachment only for the two Lambda execution policies the guidance uses; KMS without data access.
"""

import json
from pathlib import Path
from typing import Any, Final

from aws_cdk import Aws
from aws_cdk import aws_iam as iam
from constructs import Construct

from fast_channels.core.demand import bootstrap_qualifier, execution_policy
from fast_channels.infra import nag
from fast_channels.infra.bundling import REPO_ROOT

POLICY_FILE: Final = REPO_ROOT / "vendor" / "prebid-guidance" / "bootstrap-policy.json"


def policy_document(prefix: str, policy_file: Path = POLICY_FILE) -> dict[str, Any]:
    """The execution policy for ``prefix``, with ``${Prefix}``, ``${Qualifier}`` and the pseudo parameters filled."""
    text = policy_file.read_text()
    for placeholder, value in {
        "${Prefix}": prefix,
        "${Qualifier}": bootstrap_qualifier(prefix),
        "${AWS::Partition}": Aws.PARTITION,
        "${AWS::AccountId}": Aws.ACCOUNT_ID,
    }.items():
        text = text.replace(placeholder, value)
    document: dict[str, Any] = json.loads(text)
    return document


class GuidanceExecutionPolicy(Construct):
    """The managed policy ``<prefix>-guidance-cfn-exec``.

    Attributes:
        policy: the managed policy (its ARN is the stack output ``GuidanceExecutionPolicyArn``).
    """

    def __init__(self, scope: Construct, id: str, *, prefix: str) -> None:
        super().__init__(scope, id)
        self.policy = iam.ManagedPolicy(
            self,
            "Policy",
            managed_policy_name=execution_policy(prefix),
            description="FAST Channels: CloudFormation execution policy of the Prebid Server guidance's bootstrap",
            document=iam.PolicyDocument.from_json(policy_document(prefix)),
        )
        nag.acknowledge(
            self.policy,
            {
                "AwsSolutions-IAM5[Action::application-autoscaling:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::cloudfront:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::cloudtrail:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::cloudwatch:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::datasync:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::ec2:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::ecs:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::elasticache:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::elasticfilesystem:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::elasticloadbalancing:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::events:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::glue:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::logs:*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::wafv2:Get*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::wafv2:List*]": WHY_SERVICES,
                "AwsSolutions-IAM5[Action::s3:*]": "Only on the guidance's buckets and the bootstrap's assets",
                "AwsSolutions-IAM5[Action::lambda:*]": "Only on the guidance's functions and on layers",
                nag.wildcard_resource(): WHY_SERVICES,
            },
        )
        nag.acknowledge_references(
            self.policy,
            {
                nag.wildcard_resource(f"arn:<AWS::Partition>:s3:::{prefix}-pbs*"): WHY_NAMES,
                nag.wildcard_resource(f"arn:<AWS::Partition>:s3:::{prefix}-pbs*/*"): WHY_NAMES,
                nag.wildcard_resource(f"arn:<AWS::Partition>:s3:::{prefix}-cdk-assets-*"): WHY_NAMES,
                nag.wildcard_resource(f"arn:<AWS::Partition>:s3:::{prefix}-cdk-assets-*/*"): WHY_NAMES,
                nag.wildcard_resource(
                    f"arn:<AWS::Partition>:lambda:*:<AWS::AccountId>:function:{prefix}-pbs*"
                ): WHY_NAMES,
                nag.wildcard_resource("arn:<AWS::Partition>:lambda:*:<AWS::AccountId>:layer:*"): WHY_NAMES,
                nag.wildcard_resource("arn:<AWS::Partition>:cloudformation:*:aws:transform/*"): "The guidance's "
                "template uses the AWS::LanguageExtensions transform, which needs CreateChangeSet on it",
                nag.wildcard_resource(f"arn:<AWS::Partition>:iam::<AWS::AccountId>:role/{prefix}-pbs*"): WHY_NAMES,
                nag.wildcard_resource(
                    f"arn:<AWS::Partition>:ssm:*:<AWS::AccountId>:parameter/cdk-bootstrap/{prefix}/*"
                ): WHY_NAMES,
            },
        )


WHY_SERVICES: Final = (
    "CloudFormation execution policy of the guidance's dedicated bootstrap: it creates and deletes the guidance's "
    "~400 resources, whose names and identifiers CloudFormation generates"
)
WHY_NAMES: Final = "Scoped to the names CloudFormation gives the guidance's stacks' resources (<prefix>-pbs*)"
