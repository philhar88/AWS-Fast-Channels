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
RECORD: Final = "pds"
"""The service's record in the stage's private zone: ``pds.<prefix>.fast.internal`` (``core.pbs_config.pds_host``)."""


class Endpoint(StrEnum):
    """The service's paths on its internal load balancer, each a function of its own."""

    POLICY = "/policy"
    SELECT = "/select"
    OWN_BOOK = "/ownbook/openrtb"

    @property
    def function_suffix(self) -> str:
        return {"/policy": "policy", "/select": "select", "/ownbook/openrtb": "ownbook"}[self.value]


def table_name(prefix: str) -> str:
    """The PDS's DynamoDB table (lines, budgets, caps, sessions, reservations)."""
    return f"{prefix}-pds"


def census_gate(prefix: str) -> str:
    """The state machine that sets the functions' provisioned concurrency with the running channels."""
    return f"{prefix}-pds-census-gate"


def publisher_alarm(prefix: str) -> str:
    """The snapshot publisher's dead-letter alarm."""
    return f"{prefix}-pds-publisher-dead-letters"


def decision_stream(prefix: str, record: str) -> str:
    """The Firehose stream that carries one kind of decision record (``policy``, ``select``) into the lake."""
    return f"{prefix}-pds-{record}"


def decision_table(record: str) -> str:
    """The lake table of one kind of decision record: ``pds_policy``, ``pds_select``."""
    return f"pds_{record}"


class Parameter(StrEnum):
    """The SSM parameters ``/<prefix>/pds/<name>`` the Data stack writes for the Pds stack.

    The Pds stack exists only in a synth with the guidance's network, so it cannot take weak references to the Data
    stack: the Data stack's own deploy (``make deploy``, without that network) would drop the outputs they need.
    """

    CONFIG_BUCKET = "config-bucket"
    LAKE_BUCKET = "lake-bucket"


def parameter_name(prefix: str, parameter: Parameter) -> str:
    """``/<prefix>/pds/<parameter>``."""
    return f"/{prefix}/pds/{parameter.value}"


DECISIONS_PREFIX: Final = "pds/"
"""Key prefix of the decision records in the lake bucket: ``pds/<record>/<yyyy>/<MM>/<dd>/<HH>/``."""
