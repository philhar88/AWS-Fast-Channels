"""``FastChannels-<prefix>-Ops``: the ops module (``modules.ops``), deployed after Data and Media."""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack

from fast_channels.core.flags import DemandMode, Flags
from fast_channels.core.runbooks import Runbook
from fast_channels.infra.constructs.observability import census_parameter_name
from fast_channels.infra.constructs.ops import (
    AccountQuotaAlarms,
    AlertsPoller,
    ConfigurationAlarms,
    Lake,
    LedgerWriter,
    OpsAppConfig,
    ProbeCanary,
    Runbooks,
    ZeroAdsDetector,
    off_air_alarm_name,
)
from fast_channels.infra.exports import OpsExports
from fast_channels.infra.stacks.edge import EDGE_REGION

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig
    from fast_channels.infra.exports import DataExports, MediaExports

__all__ = ["OpsExports", "OpsStack", "stage_flags"]


def stage_flags(config: FastChannelsConfig) -> Flags:
    """The ``flags`` profile's default for this deployment: every kill switch on (nothing killed; a module that is
    not deployed reads none), ``commercial`` off and ``demand.mode`` as configured."""
    demand = config.modules.demand
    return Flags(
        agents=True,
        demand=DemandMode(demand.mode.value) if demand.enabled else DemandMode.OFF,
        crawl=True,
        release_promote=True,
    )


class OpsStack(Stack):
    """The stage's lake (Glue database, tables and views over the CDN and MediaTailor logs, Athena workgroup, the
    MediaTailor log copies), the ledger writer with its Firehose stream, the account quota and hook alarms, the
    zero-ad-sessions detector, the Channel Assembly alerts poller (``Channel Alert``) and the AppConfig application.

    The lake bucket itself is in the Data stack (``DataExports.lake_bucket``); the census's ``Census Changed``
    events come from the Media stack. Deleting this stack first removes the log copies' deliveries, then the
    tables; the bucket and its objects go with the Data stack.

    Args:
        config: deployment configuration; ``modules.ops.enabled`` must be true.
        data: DataStack references (the lake and logs buckets).
        media: MediaStack references (the distribution and the IaC playback configurations).

    Attributes:
        lake: the :class:`~fast_channels.infra.constructs.ops.Lake`.
        ledger: the :class:`~fast_channels.infra.constructs.ops.LedgerWriter`.
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
        if data.lake_bucket is None or data.ledger_table is None or data.ledger_archive is None:
            raise ValueError("the Ops stack needs the Data stack's lake and ledger (modules.ops.enabled)")
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
            ledger_archive=data.ledger_archive,
            removal_policy=config.removal_policy,
        )
        self.ledger = LedgerWriter(
            self,
            "Ledger",
            prefix=prefix,
            table=data.ledger_table,
            archive=data.ledger_archive,
            topic=media.alert_topic,
            log_retention=config.observability.log_retention,
            removal_policy=config.removal_policy,
        )
        self.quota_alarms = AccountQuotaAlarms(self, "QuotaAlarms", prefix=prefix, topic=media.alert_topic)
        self.configuration_alarms = ConfigurationAlarms(
            self,
            "ConfigurationAlarms",
            configurations=[channel.playback_configuration_name for channel in media.channels],
            topic=media.alert_topic,
        )
        self.zero_ads = ZeroAdsDetector(
            self,
            "ZeroAds",
            prefix=prefix,
            ads_log_group=f"/aws/vendedlogs/mediatailor/{prefix}",
            topic=media.alert_topic,
            log_retention=config.observability.log_retention,
            removal_policy=config.removal_policy,
        )
        self.alerts = AlertsPoller(
            self,
            "Alerts",
            prefix=prefix,
            event_bus=media.event_bus,
            census_parameter=census_parameter_name(prefix),
            log_retention=config.observability.log_retention,
            removal_policy=config.removal_policy,
        )
        self.appconfig = OpsAppConfig(self, "AppConfig", prefix=prefix, flags=stage_flags(config))
        self.probe = ProbeCanary(
            self,
            "Probe",
            prefix=prefix,
            channels={
                channel.name: next(iter(channel.playback(media.cdn_base_url).values())) for channel in media.channels
            },
            census_parameter=census_parameter_name(prefix),
            event_bus=media.event_bus,
            artifacts_bucket=data.logs_bucket,
            topic=media.alert_topic,
        )
        self.runbooks = Runbooks(
            self,
            "Runbooks",
            prefix=prefix,
            event_bus=media.event_bus,
            ledger_table=data.ledger_table,
            library=data.library_table,
            catalog=data.catalog_table,
            appconfig=self.appconfig,
            channels=[channel.name for channel in media.channels],
            source_location=media.source_location_name,
            alarms={
                Runbook.R7: list(self.quota_alarms.alarms),
                Runbook.R9: [name for name in self.configuration_alarms.alarms if name.endswith("-getmanifest-errors")],
                Runbook.R12: [off_air_alarm_name(prefix)],
                # CloudFront's metrics, and so its alarm, are in us-east-1 only.
                Runbook.R17: [f"{prefix}-cdn-5xx"] if self.region == EDGE_REGION else [],
            },
            log_retention=config.observability.log_retention,
            removal_policy=config.removal_policy,
        )
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
            "LedgerStreamName": (self.ledger.stream.delivery_stream_name, "Firehose stream into the ledger archive"),
        }
        for name, (value, description) in outputs.items():
            CfnOutput(self, name, value=value, description=description)
