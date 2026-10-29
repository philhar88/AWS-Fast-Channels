"""The siblings' MediaTailor vended logs: one log group, encrypted with the module's own KMS key (DESIGN §5.7).

The deliveries already leave out every field that can hold the origin secret or the playback ticket
(``core.mediatailor_logs``). The key policy delegates nothing to IAM: the account administers the key (and may change
the policy, recorded by CloudTrail), and CloudWatch Logs encrypts and decrypts for this one log group. The key protects
the data at rest and lets it be shredded (disabling the key makes the group unreadable), but it does not choose the
readers: CloudWatch Logs decrypts for any caller IAM lets read the group (observed on fastm, 2026-10-29, although the
CloudWatch Logs guide says readers need ``kms:Decrypt``). The release guard denies reading the group to every role of
the stage but the principals given to :meth:`ReleaseLogs.grant_read`, which also get ``kms:Decrypt`` through
``logs.<region>.amazonaws.com`` in case CloudWatch Logs enforces it.
"""

from typing import Final

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from constructs import Construct

from fast_channels.core.monetization import names
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.logs import DELIVERY_PRINCIPAL, log_group_arn

KEY_ADMIN_ACTIONS: Final = (
    "kms:Create*",
    "kms:Describe*",
    "kms:Enable*",
    "kms:List*",
    "kms:Put*",
    "kms:Update*",
    "kms:Revoke*",
    "kms:Disable*",
    "kms:Get*",
    "kms:Delete*",
    "kms:TagResource",
    "kms:UntagResource",
    "kms:ScheduleKeyDeletion",
    "kms:CancelKeyDeletion",
    "kms:RotateKeyOnDemand",
)
"""What the account may do with the key: administer it, not use it."""
READ_ACTIONS: Final = ("kms:Decrypt", "kms:DescribeKey")


class ReleaseLogs(Construct):
    """Log group ``/aws/vendedlogs/mediatailor/<prefix>-release`` behind the delivery destination
    ``<prefix>-release-mediatailor``, encrypted with a dedicated, rotated KMS key.

    Args:
        prefix: deployment prefix.
        retention: log retention.
        removal_policy: of the log group and the key.

    Attributes:
        key: the KMS key. log_group: the log group. destination_arn: the delivery destination's ARN.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        name = names.log_group(prefix)
        group_arn = log_group_arn(self, name)
        self.key = kms.Key(
            self,
            "Key",
            description=f"FAST Channels ({prefix}): the monetized playback configurations' MediaTailor logs",
            enable_key_rotation=True,
            removal_policy=removal_policy,
            policy=iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        sid="AccountAdministers",
                        principals=[iam.AccountRootPrincipal()],
                        actions=list(KEY_ADMIN_ACTIONS),
                        resources=["*"],
                    ),
                    iam.PolicyStatement(
                        sid="CloudWatchLogsUsesItForThisLogGroup",
                        principals=[iam.ServicePrincipal(f"logs.{stack.region}.amazonaws.com")],
                        actions=[
                            "kms:Encrypt",
                            "kms:Decrypt",
                            "kms:ReEncrypt*",
                            "kms:GenerateDataKey*",
                            "kms:Describe*",
                        ],
                        resources=["*"],
                        conditions={"ArnEquals": {"kms:EncryptionContext:aws:logs:arn": group_arn}},
                    ),
                ]
            ),
        )
        self.log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=name,
            encryption_key=self.key,
            retention=retention,
            removal_policy=removal_policy,
        )
        self.log_group.add_to_resource_policy(
            iam.PolicyStatement(
                sid="VendedLogDelivery",
                principals=[iam.ServicePrincipal(DELIVERY_PRINCIPAL)],
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{group_arn}:log-stream:*"],
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {"aws:SourceArn": arn(self, "logs", "delivery-source", f"{prefix}-*", colon=True)},
                },
            )
        )
        destination = logs.CfnDeliveryDestination(
            self,
            "Destination",
            name=names.log_destination(prefix),
            destination_resource_arn=self.log_group.log_group_arn,
            output_format="json",
        )
        self.destination_arn = destination.attr_arn

    def grant_read(self, principal: iam.IRole) -> None:
        """Let ``principal`` read the log group (Logs Insights, GetLogEvents, FilterLogEvents) and decrypt it."""
        self.key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ArnPrincipal(principal.role_arn)],
                actions=list(READ_ACTIONS),
                resources=["*"],
                conditions={"StringEquals": {"kms:ViaService": f"logs.{Stack.of(self).region}.amazonaws.com"}},
            )
        )
        principal.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["logs:StartQuery", "logs:GetLogEvents", "logs:FilterLogEvents"],
                resources=[self.log_group.log_group_arn],
            )
        )
