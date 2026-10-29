"""Names of the Pod Decision Service's resources, shared by the stack (``infra``) and the deploy script
(``scripts/_demand.py``), so both sides always agree."""

from enum import StrEnum
from typing import Final

from fast_channels.core.tags import tag_key

MODULE: Final = "pds"
MODULE_TAG: Final = tag_key("module")
"""``fast:module=pds`` marks what the module deploys (inventory)."""
ALIAS: Final = "live"
"""The alias every PDS function serves from; provisioned concurrency is set on it."""


class Endpoint(StrEnum):
    """The service's paths on its internal load balancer, each a function of its own."""

    POLICY = "/policy"
    SELECT = "/select"
    OWN_BOOK = "/ownbook/openrtb"

    @property
    def function_suffix(self) -> str:
        return {"/policy": "policy", "/select": "select", "/ownbook/openrtb": "ownbook"}[self.value]


def function_name(prefix: str, endpoint: Endpoint) -> str:
    """``<prefix>-pds-policy``, ``<prefix>-pds-select``, ``<prefix>-pds-ownbook``."""
    return f"{prefix}-pds-{endpoint.function_suffix}"


def table_name(prefix: str) -> str:
    """The PDS's DynamoDB table (lines, budgets, caps, sessions, reservations)."""
    return f"{prefix}-pds"


def census_gate(prefix: str) -> str:
    """The state machine that sets the functions' provisioned concurrency with the running channels."""
    return f"{prefix}-pds-census-gate"


def publisher_function(prefix: str) -> str:
    """The snapshot publisher."""
    return f"{prefix}-pds-publisher"


def decision_stream(prefix: str, record: str) -> str:
    """The Firehose stream that carries one kind of decision record (``policy``, ``select``) into the lake."""
    return f"{prefix}-pds-{record}"


def decision_table(record: str) -> str:
    """The lake table of one kind of decision record: ``pds_policy``, ``pds_select``."""
    return f"pds_{record}"


DECISIONS_PREFIX: Final = "pds/"
"""Key prefix of the decision records in the lake bucket: ``pds/<record>/<yyyy>/<MM>/<dd>/<HH>/``."""
