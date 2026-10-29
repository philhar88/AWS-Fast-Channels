"""``PdsTable``: the Pod Decision Service's state, ``<prefix>-pds``.

One table, keys ``pk``/``sk``: own-book lines (``LINE#<id>``), sharded budget counters (``BUDGET#<line>``), caps
(``CAP#<key>``), sessions (``SESS#<sid>``) and reservations (``RES#<decision_id>``). On demand, point-in-time
recovery, AWS-owned encryption. The ``ttl`` attribute only cleans up: every authorization uses explicit times.
"""

from typing import TYPE_CHECKING, Final

from aws_cdk import RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb

from fast_channels.core.pds.names import table_name

if TYPE_CHECKING:
    from constructs import Construct

TTL_ATTRIBUTE: Final = "ttl"


class PdsTable(dynamodb.TableV2):
    """The PDS table (module docstring)."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(
            scope,
            id,
            table_name=table_name(prefix),
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            time_to_live_attribute=TTL_ATTRIBUTE,
            removal_policy=removal_policy,
            deletion_protection=removal_policy == RemovalPolicy.RETAIN,
        )
