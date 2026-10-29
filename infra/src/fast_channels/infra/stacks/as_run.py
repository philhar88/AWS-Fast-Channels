"""``FastChannels-AsRun``: the account's as-run router, deployed by ``make as-run-router`` (ADR-0032)."""

from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Stack

from fast_channels.infra.constructs.channel_assembly import AsRunRouter

if TYPE_CHECKING:
    from aws_cdk import aws_logs as logs
    from constructs import Construct

STACK_NAME: Final = "FastChannels-AsRun"
"""Fixed for every deployment in the account and region: deploying it from another stage updates the same stack."""


class AsRunRouterStack(Stack):
    """The one subscription filter on Channel Assembly's as-run log, routing it to the default event bus for every
    stage with ``observability.as_run`` ``shared``.

    Part of a synthesized stage only with ``-c as-run-router=true`` (``make as-run-router``), so ``make deploy`` and
    ``make destroy`` never select it. Termination protected and its function's logs retained. Delete it deliberately
    once no stage reads the shared as-run log (``observability.as_run`` ``per_stage`` everywhere).

    Args:
        log_retention: retention of the router's logs.
        notification_email: subscribed to the router's alert topic.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        log_retention: logs.RetentionDays,
        notification_email: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            scope,
            id,
            stack_name=STACK_NAME,
            description="FAST Channels: the account's as-run router (shared as-run log)",
            termination_protection=True,
            **kwargs,
        )
        self.router = AsRunRouter(self, "Router", log_retention=log_retention, notification_email=notification_email)
