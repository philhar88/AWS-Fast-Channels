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


def target_group(prefix: str, endpoint: Endpoint) -> str:
    """The load balancer's target group of ``endpoint``: a fixed name, which its function's invoke permission names
    (``infra.constructs.lambda_target``)."""
    return f"{prefix}-pds-{endpoint.function_suffix}"


def table_name(prefix: str) -> str:
    """The PDS's DynamoDB table (lines, budgets, caps, sessions, reservations)."""
    return f"{prefix}-pds"


def census_gate(prefix: str) -> str:
    """The state machine that sets the functions' provisioned concurrency with the running channels."""
    return f"{prefix}-pds-census-gate"


def publisher_alarm(prefix: str) -> str:
    """The snapshot publisher's dead-letter alarm."""
    return f"{prefix}-pds-publisher-dead-letters"


def dead_letter_alarm(prefix: str, function: str) -> str:
    """The dead-letter alarm of one of the PDS's asynchronous functions (``delivery``, ``sweeper``)."""
    return f"{prefix}-pds-{function}-dead-letters"


PENDING_INDEX: Final = "pending"
"""The table's sparse index of open reservations: partition ``pending`` (``RESERVED``), sort ``pending_at``."""


def decision_stream(prefix: str, record: str) -> str:
    """The Firehose stream that carries one kind of decision record (``policy``, ``select``, ``delivery``) into the
    lake."""
    return f"{prefix}-pds-{record}"


def decision_table(record: str) -> str:
    """The lake table of one kind of decision record: ``pds_policy``, ``pds_select``, ``pds_delivery``."""
    return f"pds_{record}"


class Parameter(StrEnum):
    """The SSM parameters ``/<prefix>/pds/<name>`` the Data and Media stacks write for the Pds stack.

    The Pds stack exists only in a synth with the guidance's network, so it cannot take weak references to them:
    their own deploys (``make deploy``, without that network) would drop the outputs those need.
    """

    CONFIG_BUCKET = "config-bucket"
    LAKE_BUCKET = "lake-bucket"
    MEDIA_BUCKET = "media-bucket"
    """The media bucket, which holds the ladder kits' ads (Data stack)."""
    CONTENT_BASE_URL = "content-base-url"
    """The CDN's content base URL, our tracker's host (Media stack)."""
    LADDER_VERSION = "ladder-version"
    """The default ladder's version, whose kit the own book bids (Media stack; read at every publish)."""
    CATALOG_CONTEXT_BUCKET = "catalog-context-bucket"
    """The crawl module's catalog-context bucket, whose objects the policy function copies (Media stack, with the
    module on)."""


class Published(StrEnum):
    """The SSM parameters ``/<prefix>/pds/<name>`` the Pds stack writes for the stacks deployed after it."""

    DELIVERY_FUNCTION = "delivery-function"
    """The delivery function's ARN (with the release module on): the Release stack subscribes it to the siblings' log
    group, so their impressions are counted too."""
    POLICY_LOG_GROUP = "policy-log-group"
    """The ``/policy`` function's log group (with the release module on): the release smoke test counts a session's
    decision records there."""


def stack_parameters(*, crawl: bool) -> tuple[Parameter, ...]:
    """The parameters the Pds stack reads: every one, but the catalog-context bucket's only with the crawl module."""
    return tuple(p for p in Parameter if crawl or p is not Parameter.CATALOG_CONTEXT_BUCKET)


def parameter_name(prefix: str, parameter: Parameter | Published) -> str:
    """``/<prefix>/pds/<parameter>``."""
    return f"/{prefix}/pds/{parameter.value}"


DECISIONS_PREFIX: Final = "pds/"
"""Key prefix of the decision records in the lake bucket: ``pds/<record>/<yyyy>/<MM>/<dd>/<HH>/``."""
