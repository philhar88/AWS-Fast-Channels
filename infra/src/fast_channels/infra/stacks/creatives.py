"""``FastChannels-<prefix>-Creatives``: the creative registry and its readiness (``modules.creatives``;
docs/creatives.md).

Advertisers' own creatives, each moderated, conditioned to the stage's default ladder by the Media stack's ingest
pipeline, checked for ladder conformance and published to the PDS own book once READY. It is deployed beside the
guidance after the Pds stack (``scripts/prebid_guidance.py deploy``), whose publisher it runs; the Media stack lets its
ingest pipeline read the module's quarantine bucket.
"""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm

from fast_channels.core.creatives import names
from fast_channels.core.pds import names as pds_names
from fast_channels.infra.constructs.creatives.readiness import Readiness

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig


class CreativesStack(Stack):
    """The registry, the quarantine bucket and the readiness state machine.

    Args:
        config: deployment configuration (``modules.creatives``, the default ladder, log retention).
    """

    def __init__(self, scope: Construct, id: str, *, config: FastChannelsConfig, **kwargs: Any) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): creatives", **kwargs)
        Tags.of(self).add(names.MODULE_TAG, names.MODULE)
        prefix, creatives = config.prefix, config.modules.creatives

        def parameter(name: pds_names.Parameter | pds_names.Published) -> str:
            return ssm.StringParameter.value_for_string_parameter(self, pds_names.parameter_name(prefix, name))

        self.readiness = Readiness(
            self,
            "Readiness",
            prefix=prefix,
            ladder=config.ladder(config.ladders.default),
            moderation=creatives.moderation,
            min_confidence=creatives.min_confidence,
            media_bucket=s3.Bucket.from_bucket_name(self, "MediaBucket", parameter(pds_names.Parameter.MEDIA_BUCKET)),
            config_bucket=s3.Bucket.from_bucket_name(
                self, "ConfigBucket", parameter(pds_names.Parameter.CONFIG_BUCKET)
            ),
            publisher=parameter(pds_names.Published.PUBLISHER_FUNCTION),
            log_retention=config.observability.log_retention,
            removal_policy=config.removal_policy,
        )
        outputs = {
            "RegistryTable": (self.readiness.table.table_name, "The creative registry"),
            "QuarantineBucket": (self.readiness.bucket.bucket_name, "Creative sources, under incoming/<crid>/"),
            "StateMachineArn": (self.readiness.state_machine.state_machine_arn, "Readiness: register a creative"),
            "ReadinessFunction": (self.readiness.function.function.function_name, "The readiness steps"),
        }
        for key, (value, description) in outputs.items():
            CfnOutput(self, key, value=value, description=description)
