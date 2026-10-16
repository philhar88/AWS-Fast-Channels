"""CDK app entry point (cdk.json: ``python -m fast_channels.infra.app``)."""

import os
import sys

from aws_cdk import App, Environment, Validations
from cdk_nag import AwsSolutionsChecks, ServerlessChecks

from fast_channels.infra.config import ConfigError, FastChannelsConfig
from fast_channels.infra.stage import FastChannelsStage, stage_id


def build(app: App, *, account: str | None, region: str | None) -> FastChannelsStage:
    """Add the FAST Channels stage configured by ``app``'s context, checked by cdk-nag v3 (AWS Solutions and
    Serverless packs) at synth time.

    Raises:
        ConfigError: the configuration is invalid or the region is unknown.
    """
    if not region:
        raise ConfigError("cannot determine the deployment region: set AWS_REGION or configure a profile region")
    config = FastChannelsConfig.from_context(app.node, region=region)
    stage = FastChannelsStage(
        app, stage_id(config.prefix), config=config, env=Environment(account=account, region=region)
    )
    Validations.of(app).add_plugins(AwsSolutionsChecks(app), ServerlessChecks(app))
    return stage


def main() -> None:
    """Synthesize the app for the account and region the CDK CLI resolved."""
    app = App()
    try:
        build(app, account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=os.environ.get("CDK_DEFAULT_REGION"))
    except ConfigError as error:
        sys.exit(str(error))
    app.synth()


if __name__ == "__main__":
    main()
