"""The stage's AWS AppConfig application, with the ops module's feature flags (profile ``flags``, ``core.flags``),
to which the Release Controller's release pointer (M3-04) adds its profile."""

import json
from collections.abc import Sequence

from aws_cdk import Duration
from aws_cdk import aws_appconfig as appconfig
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_iam as iam
from constructs import Construct

from fast_channels.core.flags import PROFILE, Flags, flags_document
from fast_channels.infra import nag
from fast_channels.infra.arns import arn


class OpsAppConfig(Construct):
    """AppConfig application ``<prefix>-fast`` with one environment, ``<prefix>``, and the feature-flag profile
    ``flags`` deployed to it (all at once).

    The environment skips AppConfig's deletion protection check (``BYPASS``): a stage is destroyed right after its
    end-to-end run, when the protection (if the account turns it on) would still count the profiles as in use. The
    stack deploys ``flags`` as the profile's default; an operator who changes a flag in AppConfig deploys a new
    version, which stays until a stack deployment with a changed default.

    With the release module on, the environment is monitored by the Release Controller's composite alarm
    (``monitors``, by name: the ``Release`` stack creates it later). A gradual deployment of the release pointer rolls
    back when it goes to ALARM; a monitored deployment while it does not exist yet rolls back at once ("The following
    CloudWatch monitors do not exist"). The monitors also watch ``AppConfig.AllAtOnce``'s 10-minute bake (fastm,
    2026-10-29: a deployment rolled back 2 minutes in, when the alarm went to ALARM), so the ``flags`` profile then
    deploys with a strategy of no bake (``<prefix>-flags-at-once``): a kill switch is never rolled back by a release
    alarm.

    Args:
        prefix: deployment prefix.
        flags: the flag values to deploy.
        monitors: names of the alarms the environment watches.

    Attributes:
        application: the application.
        environment: its environment.
        flags: the ``flags`` profile.
    """

    def __init__(
        self, scope: Construct, id: str, *, prefix: str, flags: Flags | None = None, monitors: Sequence[str] = ()
    ) -> None:
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
            monitors=self._monitors(monitors) or None,
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
            deployment_strategy=self._at_once(prefix)
            if monitors
            else appconfig.DeploymentStrategy.from_deployment_strategy_id(
                self, "AllAtOnce", appconfig.DeploymentStrategyId.ALL_AT_ONCE
            ),
            deletion_protection_check=appconfig.DeletionProtectionCheck.BYPASS,
        )

    def _at_once(self, prefix: str) -> appconfig.IDeploymentStrategy:
        """All at once and no bake, so no monitor can roll it back."""
        return appconfig.DeploymentStrategy(
            self,
            "FlagsAtOnce",
            deployment_strategy_name=f"{prefix}-flags-at-once",
            description="Kill switches: at once, with no bake for an environment monitor to roll back",
            rollout_strategy=appconfig.RolloutStrategy.linear(
                growth_factor=100, deployment_duration=Duration.minutes(0), final_bake_time=Duration.minutes(0)
            ),
        )

    def _monitors(self, names: Sequence[str]) -> list[appconfig.Monitor]:
        if not names:
            return []
        alarms = [arn(self, "cloudwatch", "alarm", name, colon=True) for name in names]
        role = iam.Role(
            self,
            "MonitorRole",
            description="AWS AppConfig: read the alarms that roll a release pointer deployment back",
            assumed_by=iam.ServicePrincipal("appconfig.amazonaws.com"),
        )
        # AppConfig calls DescribeAlarms by name, which returns metric alarms only, then without a filter, which needs
        # "*": with the alarm's ARN alone it finds no composite alarm and rolls every deployment of the environment
        # back ("The following CloudWatch monitors do not exist", fastm 2026-10-29).
        role.add_to_policy(iam.PolicyStatement(actions=["cloudwatch:DescribeAlarms"], resources=["*"]))
        nag.acknowledge_references(
            role,
            {nag.wildcard_resource(): "DescribeAlarms without a filter: how AppConfig finds a composite alarm"},
        )
        return [
            appconfig.Monitor.from_cloud_watch_alarm(
                cw.Alarm.from_alarm_arn(self, f"Monitor{n}", alarm_arn), alarm_role=role
            )
            for n, alarm_arn in enumerate(alarms)
        ]

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
