"""``FastChannels-AccountBaseline``: account-and-region singletons, deployed when
``observability.transaction_search`` is ``enable``."""

from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Stack

from fast_channels.infra.constructs.observability_baseline import TransactionSearch

if TYPE_CHECKING:
    from constructs import Construct

STACK_NAME: Final = "FastChannels-AccountBaseline"
"""Fixed for every deployment in the account and region: a second deployment updates the same stack."""


class AccountBaselineStack(Stack):
    """CloudWatch Transaction Search (indexing 1 %). Termination protected and retained, so it is never part of a
    normal teardown; delete it deliberately to turn Transaction Search off."""

    def __init__(self, scope: Construct, id: str, **kwargs: Any) -> None:
        super().__init__(
            scope,
            id,
            stack_name=STACK_NAME,
            description="FAST Channels: account baseline (CloudWatch Transaction Search)",
            termination_protection=True,
            **kwargs,
        )
        self.transaction_search = TransactionSearch(self, "TransactionSearch")
