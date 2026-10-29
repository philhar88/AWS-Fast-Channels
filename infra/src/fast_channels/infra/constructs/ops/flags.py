"""The stage's AWS AppConfig application, with the ops module's feature flags (profile ``flags``, ``core.flags``),
to which the Release Controller's release pointer (M3-04) adds its profile."""

import json

from aws_cdk import aws_appconfig as appconfig
from aws_cdk import aws_iam as iam
from constructs import Construct

from fast_channels.core.flags import PROFILE, Flags, flags_document
from fast_channels.infra.arns import arn


class OpsAppConfig(Construct):
    """AppConfig application ``<prefix>-fast`` with one environment, ``<prefix>``, and the feature-flag profile
    ``flags`` deployed to it (all at once).

    The environment skips AppConfig's deletion protection check (``BYPASS``): a stage is destroyed right after its
    end-to-end run, when the protection (if the account turns it on) would still count the profiles as in use. The
    stack deploys ``flags`` as the profile's default; an operator who changes a flag in AppConfig deploys a new
    version, which stays until a stack deployment with a changed default.

    Args:
        prefix: deployment prefix.
        flags: the flag values to deploy.

    Attributes:
        application: the application.
        environment: its environment.
        flags: the ``flags`` profile.
    """

    def __init__(self, scope: Construct, id: str, *, prefix: str, flags: Flags | None = None) -> None:
        super().__init__(scope, id)
        self.application = appconfig.Application(
            self,
            "Application",
            application_name=f"{prefix}-fast",
            description=f"FAST Channels ({prefix}): feature flags and the release pointer",
        )
        self.environment = appconfig.Environment(
            self,
            "Environment",
            application=self.application,
            environment_name=prefix,
            description=f"The {prefix} stage",
            deletion_protection_check=appconfig.DeletionProtectionCheck.BYPASS,
        )
        self.flags = appconfig.HostedConfiguration(
            self,
            "Flags",
            application=self.application,
            name=PROFILE,
            description="Kill switches of the agents, modules and runbooks (D§19.5)",
            type=appconfig.ConfigurationType.FEATURE_FLAGS,
            content=appconfig.ConfigurationContent.from_inline_json(
                json.dumps(flags_document(flags or Flags()), sort_keys=True)
            ),
            deploy_to=[self.environment],
            deployment_strategy=appconfig.DeploymentStrategy.from_deployment_strategy_id(
                self, "AllAtOnce", appconfig.DeploymentStrategyId.ALL_AT_ONCE
            ),
            deletion_protection_check=appconfig.DeletionProtectionCheck.BYPASS,
        )

    def grant_read_flags(self, grantee: iam.IGrantable) -> None:
        """Let ``grantee`` read the ``flags`` profile (``StartConfigurationSession``, ``GetLatestConfiguration``)."""
        resource = arn(
            self,
            "appconfig",
            "application",
            f"{self.application.application_id}/environment/{self.environment.environment_id}"
            f"/configuration/{self.flags.configuration_profile_id}",
        )
        grantee.grant_principal.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["appconfig:StartConfigurationSession", "appconfig:GetLatestConfiguration"],
                resources=[resource],
            )
        )
