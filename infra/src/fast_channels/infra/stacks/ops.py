"""``FastChannels-<prefix>-Ops``: the ops module (``modules.ops``), deployed after Data and Media."""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack

from fast_channels.infra.constructs.ops import Lake, OpsAppConfig
from fast_channels.infra.exports import OpsExports

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig
    from fast_channels.infra.exports import DataExports, MediaExports

__all__ = ["OpsExports", "OpsStack"]


class OpsStack(Stack):
    """The stage's lake (Glue database, tables and views over the CDN and MediaTailor logs, Athena workgroup, the
    MediaTailor log copies) and its AppConfig application.

    The lake bucket itself is in the Data stack (``DataExports.lake_bucket``); the census's ``Census Changed``
    events come from the Media stack. Deleting this stack first removes the log copies' deliveries, then the
    tables; the bucket and its objects go with the Data stack.

    Args:
        config: deployment configuration; ``modules.ops.enabled`` must be true.
        data: DataStack references (the lake and logs buckets).
        media: MediaStack references (the distribution and the IaC playback configurations).

    Attributes:
        lake: the :class:`~fast_channels.infra.constructs.ops.Lake`.
        appconfig: the :class:`~fast_channels.infra.constructs.ops.OpsAppConfig`.
        exports: what later module stacks consume (:class:`OpsExports`).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        data: DataExports,
        media: MediaExports,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): ops module and lake", **kwargs)
        if data.lake_bucket is None:
            raise ValueError("the Ops stack needs the Data stack's lake bucket (modules.ops.enabled)")
        prefix = config.prefix
        self.lake = Lake(
            self,
            "Lake",
            prefix=prefix,
            lake_bucket=data.lake_bucket,
            logs_bucket=data.logs_bucket,
            distribution_id=media.distribution.distribution_id,
            playback_configurations=[channel.playback_configuration_name for channel in media.channels],
            retention_days=config.modules.ops.lake_retention_days,
            removal_policy=config.removal_policy,
        )
        self.appconfig = OpsAppConfig(self, "AppConfig", prefix=prefix)
        self.exports = OpsExports(
            appconfig_application=self.appconfig.application,
            appconfig_environment=self.appconfig.environment,
            lake_database=self.lake.database_name,
            lake_workgroup=self.lake.workgroup.name,
        )
        outputs = {
            "LakeDatabase": (self.lake.database_name, "Glue database of the lake"),
            "LakeWorkGroup": (self.lake.workgroup.name, "Athena workgroup for lake queries"),
            "AppConfigApplicationId": (self.appconfig.application.application_id, "AppConfig application"),
            "AppConfigEnvironmentId": (self.appconfig.environment.environment_id, "AppConfig environment"),
        }
        for name, (value, description) in outputs.items():
            CfnOutput(self, name, value=value, description=description)
