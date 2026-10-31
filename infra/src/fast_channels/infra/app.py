"""CDK app entry point (cdk.json: ``python -m fast_channels.infra.app``)."""

import os
import sys
from typing import Final

from aws_cdk import App, Environment, Validations
from cdk_nag import AwsSolutionsChecks, ServerlessChecks

from fast_channels.infra.config import ConfigError, FastChannelsConfig
from fast_channels.infra.stage import FastChannelsStage, stage_id

AS_RUN_ROUTER: Final = "as-run-router"
"""Context key (``-c as-run-router=true``, set by ``make as-run-router``) that adds the account's as-run router
stack to the synthesized stage; without it no other command selects that stack (ADR-0032)."""
ACCOUNT_BASELINE: Final = "account-baseline"
"""Context key (``-c account-baseline=true``, set by ``make account-baseline``) that adds the account baseline
stack (Transaction Search) to the synthesized stage, for ``observability.transaction_search: enable``; without it
no other command selects that stack, so ``make destroy`` never meets its termination protection."""


def build(app: App, *, account: str | None, region: str | None) -> FastChannelsStage:
    """Add the FAST Channels stage configured by ``app``'s context, checked by cdk-nag v3 (AWS Solutions and
    Serverless packs) at synth time, with the account's as-run router if the context sets :data:`AS_RUN_ROUTER` and
    the account baseline if it sets :data:`ACCOUNT_BASELINE`.

    Raises:
        ConfigError: the configuration is invalid or the region is unknown.
    """
    if not region:
        raise ConfigError("cannot determine the deployment region: set AWS_REGION or configure a profile region")
    config = FastChannelsConfig.from_context(app.node, region=region)
    stage = FastChannelsStage(
        app,
        stage_id(config.prefix),
        config=config,
        env=Environment(account=account, region=region),
        as_run_router=_flag(app, AS_RUN_ROUTER),
        account_baseline=_flag(app, ACCOUNT_BASELINE),
    )
    Validations.of(app).add_plugins(AwsSolutionsChecks(app), ServerlessChecks(app))
    return stage


def _flag(app: App, key: str) -> bool:
    """Whether the context sets ``key`` to true (``-c <key>=true``)."""
    return str(app.node.try_get_context(key)).lower() == "true"


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
