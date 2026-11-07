"""``FastChannels-<prefix>-Recs``: the rail recommender (``modules.recs``; ADR-0027)."""

from typing import TYPE_CHECKING, Any, Final

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_ssm as ssm

from fast_channels.core.identity import names as identity_names
from fast_channels.core.identity.names import Parameter
from fast_channels.core.monetization import names as release_names
from fast_channels.core.recs import TASK
from fast_channels.infra.constructs.recs import IdentityValues, RailRecommender

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig
    from fast_channels.infra.exports import DataExports, MediaExports, ModelsExports

RAIL_URL_PARAMETER: Final = "rail-url"
"""``/<prefix>/recs/rail-url``: ``GET`` here answers the signed-in viewer's rail (the viewer app's ``railUrl``)."""


def rail_url_parameter(prefix: str) -> str:
    """``/<prefix>/recs/rail-url``."""
    return f"/{prefix}/recs/{RAIL_URL_PARAMETER}"


class RecsStack(Stack):
    """``GET /rail``, the rail builder and the hourly cohort rails, deployed when ``modules.recs.enabled``.

    The identity module's issuer, clients and viewer-id key are read from its SSM parameters at deploy time, as the
    Viewer stack does, and so are the ops module's AppConfig ids (the ``recs.strategy`` flag), which the Ops stack
    writes for the release module; the ``recommender`` task's profile comes from the Models stack. The rails recommend
    the viewer's channels: the IaC channels without the release module's lab channel.

    Args:
        config: deployment configuration (``modules.recs``, the ``recommender`` registry entry, ``observability``).
        data: Data stack references (the media bucket and the catalog table).
        media: Media stack exports (the channels and the alert topic).
        models: Models stack exports (the ``recommender`` task's profile).

    Attributes:
        recommender: the construct; rail_url: ``GET`` here.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        data: DataExports,
        media: MediaExports,
        models: ModelsExports,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): rail recommender", **kwargs)
        prefix, recs = config.prefix, config.modules.recs

        def identity(parameter: Parameter) -> str:
            return ssm.StringParameter.value_for_string_parameter(
                self, identity_names.parameter_name(prefix, parameter)
            )

        lab = config.lab_channel
        channels = [c.name for c in media.channels if lab is None or c.name != lab.name]
        self.recommender = RailRecommender(
            self,
            "Recommender",
            prefix=prefix,
            channels=channels,
            child_directed=config.modules.release.child_directed,
            cohorts=recs.cohorts,
            holdout_percent=recs.holdout_percent,
            refresh_hours=recs.refresh_hours,
            settings=config.modules.models.tasks[TASK],
            profile=models.profiles[TASK],
            identity=IdentityValues(
                issuer=identity(Parameter.ISSUER),
                client_ids=(identity(Parameter.CLIENT_ID),),  # the e2e tests sign in with the viewer app client
                vid_key_arn=identity(Parameter.VID_KEY),
            ),
            media_bucket=data.media_bucket,
            catalog_table=data.catalog_table,
            alert_topic=media.alert_topic,
            appconfig=self._appconfig(config),
            log_retention=config.observability.log_retention,
            removal_policy=config.removal_policy,
        )
        self.rail_url = self.recommender.rail_url
        ssm.StringParameter(
            self,
            "RailUrlParameter",
            parameter_name=rail_url_parameter(prefix),
            string_value=self.rail_url,
            description="GET here answers the signed-in viewer's rail (GET /rail)",
        )
        outputs = {
            "RailUrl": (self.rail_url, "GET /rail: the signed-in viewer's rail (a viewer access token)"),
            "RailBuilderName": (
                self.recommender.builder.function.function_name,
                'The rail builder; invoke it with {"cohorts": true} to build the cohort rails now',
            ),
            "RecsTableName": (self.recommender.table.table_name, "Viewers' rail state (V#<vid>) and built rails"),
        }
        for name, (value, description) in outputs.items():
            CfnOutput(self, name, value=value, description=description)

    def _appconfig(self, config: FastChannelsConfig) -> tuple[str, str] | None:
        """The ops module's AppConfig application and environment ids, or ``None`` without ops."""
        if not (config.modules.ops.enabled and config.modules.release.enabled):
            return None
        application, environment = (
            ssm.StringParameter.value_for_string_parameter(self, release_names.parameter_name(config.prefix, p))
            for p in (release_names.Parameter.APPCONFIG_APPLICATION, release_names.Parameter.APPCONFIG_ENVIRONMENT)
        )
        return application, environment
