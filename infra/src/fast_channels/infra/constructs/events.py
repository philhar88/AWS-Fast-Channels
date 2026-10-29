"""The deployment's EventBridge bus for domain events (``core.events``), which every plane publishes to."""

from typing import Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_logs as logs
from constructs import Construct

from fast_channels.infra.constructs.logs import deliver, log_group_arn, vended_log_group

DEFAULT_ARCHIVE_RETENTION: Final = Duration.days(30)


class FastEventBus(Construct):
    """The deployment's ``<prefix>-fast-channels`` EventBridge bus for domain events (``core.events``, source
    ``fast-channels.<prefix>``), with an archive for replays and INFO-level logs including event details
    delivered to ``/aws/vendedlogs/events/<prefix>-fast-channels``. Every other construct receives
    :attr:`bus` as ``event_bus``.

    INFO logs every ``PutEvents`` ingestion, rule match and target invocation, and every error (the ERROR level is
    included); EventBridge logs on a best-effort basis.

    Args:
        prefix: deployment prefix.
        log_retention: retention of the log group.
        archive_retention: how long the archive keeps events.
        removal_policy: what happens to the log group when the stack is deleted.

    Attributes:
        name: the bus name, ``<prefix>-fast-channels``.
        bus: the event bus.
        archive: archive of every event on the bus.
        log_group: the bus log group.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        log_retention: logs.RetentionDays,
        archive_retention: Duration = DEFAULT_ARCHIVE_RETENTION,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.name = name = f"{prefix}-fast-channels"
        self.bus = events.EventBus(
            self,
            "Bus",
            event_bus_name=name,
            description=f"FAST Channels ({prefix}) domain events",
            log_config=events.LogConfig(level=events.Level.INFO, include_detail=events.IncludeDetail.FULL),
        )
        self.archive = self.bus.archive(
            "Archive",
            archive_name=name,
            description="Every FAST Channels event, for replay",
            event_pattern=events.EventPattern(account=[Stack.of(self).account]),
            retention=archive_retention,
        )
        self.log_group = vended_log_group(
            self,
            "Logs",
            name=f"/aws/vendedlogs/events/{name}",
            delivery_sources=f"{name}-*",
            retention=log_retention,
            removal_policy=removal_policy,
        )
        source = logs.CfnDeliverySource(
            self, "LogSource", name=f"{name}-info", log_type="INFO_LOGS", resource_arn=self.bus.event_bus_arn
        )
        destination = logs.CfnDeliveryDestination(
            self,
            "LogDestination",
            name=f"{name}-logs",
            destination_resource_arn=log_group_arn(self, self.log_group.log_group_name),
            output_format="json",
        )
        deliver(self, "LogDelivery", source=source, destination_arn=destination.attr_arn)
