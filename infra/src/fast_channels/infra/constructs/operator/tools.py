"""Agent tool Lambdas: one function per Gateway target of the tool registry (SPEC §6, CONTRACTS §B, §C).

Each function gets only the environment (CONTRACTS §C) and the IAM its target's tool module uses. MediaTailor
permissions follow the name partition (_review.md, IAM lens): ``<prefix>-*`` for every channel of the deployment,
``<prefix>-agent-*`` for what the agent may create, delete or re-point; creation calls without resource-level
permissions are allowed only with this deployment's ``fast:app`` tag (``aws:RequestTag``).
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from aws_cdk import ArnFormat, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct

from fast_channels.core.naming import agent_namespace
from fast_channels.core.tags import TAG_APP
from fast_channels.functions.tools.registry import TARGETS, ToolSpec, load
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.storage import GSI1
from fast_channels.infra.grants import allow

if TYPE_CHECKING:
    from fast_channels.infra.config import CdnEndpoint
    from fast_channels.infra.exports import DataExports, MediaExports

APP_TAG: Final = TAG_APP
"""``core.tags.TAG_APP`` (kept for v3.0 imports)."""
_TABLE_READ: Final = ("GetItem", "BatchGetItem", "Query", "Scan", "ConditionCheckItem")
_TABLE_WRITE: Final = ("PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem")
_CHANNELS: Final = "every channel of the deployment is named <prefix>-* (config validation)"
_DESCRIPTIONS: Final = {
    "catalog": "Agent tools: reference catalog search",
    "library": "Agent tools: media library and ingest",
    "origins": "Agent tools: Channel Assembly source locations and VOD sources",
    "schedule": "Agent tools: channel schedules (plan, publish, EPG)",
    "channels": "Agent tools: linear channels and their playback configurations",
    "encoding": "Agent tools: encoding ladders and their kits",
}
_TIMEOUTS: Final = {
    "schedule": Duration.minutes(5),  # publish creates programs one by one at <= 3 TPS (channel-assembly.md)
    "origins": Duration.minutes(2),  # VOD source calls are limited to 1 TPS
    # delete_channel waits for STOPPED, then drains up to 400 programs at <= 3 TPS (~135 s); start waits for RUNNING
    "channels": Duration.minutes(5),
}
ENVIRONMENT: Final = {
    "catalog": ("CATALOG_TABLE",),
    "library": ("LIBRARY_TABLE", "INGEST_BUCKET", "INGEST_STATE_MACHINE_ARN", "DEFAULT_LADDER_VERSION"),
    "origins": ("LIBRARY_TABLE", "MEDIA_BUCKET", "SOURCE_LOCATION", "PACKAGE_PROFILES", "DEFAULT_LADDER_VERSION"),
    "schedule": ("LIBRARY_TABLE", "CATALOG_TABLE", "MEDIA_BUCKET", "SOURCE_LOCATION", "EVENT_BUS"),
    "channels": (
        "LIBRARY_TABLE",
        "MEDIA_BUCKET",
        "SOURCE_LOCATION",
        "CDN_BASE_URL",
        "CDN_ENDPOINTS",
        "PACKAGE_PROFILES",
        "DEFAULT_LADDER_VERSION",
        "PLAYBACK_CONFIG_TEMPLATE",
        "AUTO_STOP_HOURS",
        "MAX_AGENT_CHANNELS",
        "SCHEDULER_GROUP",
        "SCHEDULER_ROLE_ARN",
    ),
    "encoding": ("LIBRARY_TABLE", "PROVISION_STATE_MACHINE_ARN", "MEDIACONVERT_QUEUE_ARN"),
}
"""Environment variables (besides ``FAST_PREFIX``) of each target's function, as its tool module reads them."""


@dataclass(frozen=True, slots=True)
class ToolTarget:
    """A Gateway Lambda target: the function that serves every tool of one registry target."""

    name: str
    """Target name; the Gateway exposes its tools as ``<name>___<tool>``."""
    description: str
    function: lambda_.IFunction
    tools: tuple[ToolSpec[Any, Any], ...]


def registry_tools() -> dict[str, tuple[ToolSpec[Any, Any], ...]]:
    """Every registered tool by target, in documentation order (imports each target's module)."""
    return {target: tuple(load(target).values()) for target in TARGETS}


class AgentTools(Construct):
    """The tool Lambdas (``fast_channels.functions.tools.<target>.handler``) and their least-privilege roles.

    Also owns the EventBridge Scheduler group and role that ``channels___start_channel`` uses to stop a channel
    after ``auto_stop_hours`` (universal target ``mediatailor:stopChannel``); the role trusts only schedules of that
    group, and the channels function may pass it to Scheduler only.

    Args:
        prefix: deployment prefix (``FAST_PREFIX``, name partitions).
        data: Data stack resources (tables, buckets).
        media: Media stack exports (pipeline, ladders, origin, channels, delivery).
        package_profiles: ``channel_assembly.package_profiles``.
        auto_stop_hours: ``channels.max_runtime_hours``; ``None`` disables auto-stop.
        max_agent_channels: ``channels.max_agent_channels``.
        cdns: ``delivery.cdns``, the other CDNs whose playback URLs ``channels___get_channel`` lists.
        log_retention, removal_policy: of the function log groups.
        tools: tool specs by target; defaults to the registry.

    Attributes:
        targets: one :class:`ToolTarget` per registry target.
        functions: the FastFunctions by target.
        auto_stop_group, auto_stop_role: the auto-stop schedule group and the role its schedules assume.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        data: DataExports,
        media: MediaExports,
        package_profiles: Sequence[str],
        auto_stop_hours: float | None,
        max_agent_channels: int,
        cdns: Sequence[CdnEndpoint] = (),
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
        tools: Mapping[str, Sequence[ToolSpec[Any, Any]]] | None = None,
    ) -> None:
        super().__init__(scope, id)
        self._prefix, self._data, self._media = prefix, data, media
        self._stack = Stack.of(self)
        self.auto_stop_group = scheduler.ScheduleGroup(
            self, "AutoStop", schedule_group_name=f"{prefix}-auto-stop", removal_policy=RemovalPolicy.DESTROY
        )
        self.auto_stop_role = iam.Role(
            self,
            "AutoStopRole",
            description="EventBridge Scheduler: stop channels started by the agent tools after their maximum runtime",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self._stack.account},
                    "ArnEquals": {"aws:SourceArn": self.auto_stop_group.schedule_group_arn},
                },
            ),
        )
        allow(self.auto_stop_role, _mt("StopChannel"), [self._mediatailor("channel", f"{prefix}-*")], why=_CHANNELS)
        values = {
            "FAST_PREFIX": prefix,
            "LIBRARY_TABLE": data.library_table.table_name,
            "CATALOG_TABLE": data.catalog_table.table_name,
            "MEDIA_BUCKET": data.media_bucket.bucket_name,
            "INGEST_BUCKET": data.ingest_bucket.bucket_name,
            "EVENT_BUS": media.event_bus.event_bus_name,
            "INGEST_STATE_MACHINE_ARN": media.ingest_state_machine.state_machine_arn,
            "PROVISION_STATE_MACHINE_ARN": media.ladder_provisioner.state_machine_arn,
            "MEDIACONVERT_QUEUE_ARN": media.mediaconvert_queue_arn,
            "SOURCE_LOCATION": media.source_location_name,
            "CDN_BASE_URL": media.cdn_base_url,
            "CDN_ENDPOINTS": json.dumps([cdn.model_dump(mode="json") for cdn in cdns]),
            "PACKAGE_PROFILES": ",".join(package_profiles),
            "DEFAULT_LADDER_VERSION": media.default_ladder_version,
            "PLAYBACK_CONFIG_TEMPLATE": media.playback_configuration_template,
            "AUTO_STOP_HOURS": "" if auto_stop_hours is None else f"{auto_stop_hours:g}",
            "MAX_AGENT_CHANNELS": str(max_agent_channels),
            "SCHEDULER_GROUP": self.auto_stop_group.schedule_group_name,
            "SCHEDULER_ROLE_ARN": self.auto_stop_role.role_arn,
        }
        grants: Mapping[str, Callable[[FastFunction], None]] = {
            "catalog": self._grant_catalog,
            "library": self._grant_library,
            "origins": self._grant_origins,
            "schedule": self._grant_schedule,
            "channels": self._grant_channels,
            "encoding": self._grant_encoding,
        }
        specs = tools if tools is not None else registry_tools()
        self.functions: dict[str, FastFunction] = {}
        targets = []
        for target, target_tools in specs.items():
            function = FastFunction(
                self,
                target.title(),
                handler=f"fast_channels.functions.tools.{target}.handler",
                description=_DESCRIPTIONS[target],
                environment={name: values[name] for name in ("FAST_PREFIX", *ENVIRONMENT[target])},
                timeout=_TIMEOUTS.get(target, Duration.seconds(30)),
                log_retention=log_retention,
                removal_policy=removal_policy,
            )
            grants[target](function)
            nag.acknowledge(
                function, {nag.LAMBDA_DLQ: "Invoked synchronously by the AgentCore Gateway, which returns errors"}
            )
            self.functions[target] = function
            targets.append(ToolTarget(target, _DESCRIPTIONS[target], function.function, tuple(target_tools)))
        self.targets = tuple(targets)

    # --- per-target permissions (derived from each tool module's AWS calls) ------------------------------------

    def _grant_catalog(self, fn: FastFunction) -> None:
        self._table(fn, self._data.catalog_table, write=False)

    def _grant_library(self, fn: FastFunction) -> None:
        self._table(fn, self._data.library_table, write=False)
        fn.allow(
            ["s3:GetObject"],  # HeadObject of the requested mezzanine
            [self._data.ingest_bucket.arn_for_objects("*")],
            why="ingest refuses sources that are missing from the ingest bucket (any key)",
        )
        self._media.ingest_state_machine.grant_start_execution(fn.function)
        machine = self._stack.split_arn(
            self._media.ingest_state_machine.state_machine_arn, ArnFormat.COLON_RESOURCE_NAME
        ).resource_name
        fn.allow(
            ["states:DescribeExecution"],
            [arn(self, "states", "execution", f"{machine}:*", colon=True)],
            why="ingest status of the pipeline's executions",
        )

    def _grant_origins(self, fn: FastFunction) -> None:
        mt, prefix = self._mediatailor, self._prefix
        self._table(fn, self._data.library_table, write=True)
        fn.allow(
            ["s3:GetObject"],
            [self._data.media_bucket.arn_for_objects("assets/*")],
            why="CreateVodSource checks that the caller can read the encoded manifests (channel-assembly.md §2.1)",
        )
        fn.allow(_mt("ListSourceLocations", "ListAlerts"), ["*"])
        fn.allow(_mt("CreateSourceLocation", "CreateVodSource"), ["*"], conditions=self._tagged())
        fn.allow(
            _mt("TagResource"),
            [mt("sourceLocation", f"{agent_namespace(prefix)}*"), mt("vodSource", f"{prefix}-*/*")],
            why="tags of the source locations and VOD sources the tools create",
            conditions=self._tagged(),
        )
        fn.allow(
            _mt("DescribeSourceLocation"),
            [mt("sourceLocation", f"{prefix}-*")],
            why="the deployment's own source location and the agent's bring-your-own ones",
        )
        fn.allow(
            _mt("DescribeVodSource", "UpdateVodSource"),
            [mt("vodSource", f"{prefix}-*/*")],
            why="package configurations of VOD sources in the deployment's source locations",
        )
        fn.allow(_mt("DescribeChannel"), [mt("channel", f"{prefix}-*")], why=_CHANNELS)

    def _grant_schedule(self, fn: FastFunction) -> None:
        mt, prefix = self._mediatailor, self._prefix
        self._table(fn, self._data.library_table, write=True)
        self._table(fn, self._data.catalog_table, write=False)
        self._epg(fn, "s3:PutObject", "s3:DeleteObject")
        self._media.event_bus.grant_put_events_to(fn.function)
        fn.allow(_mt("ListAlerts", "CreateProgram"), ["*"])
        fn.allow(_mt("DescribeChannel", "GetChannelSchedule"), [mt("channel", f"{prefix}-*")], why=_CHANNELS)
        fn.allow(_mt("DeleteProgram"), [mt("program", f"{prefix}-*/*")], why="programs of the deployment's channels")

    def _grant_channels(self, fn: FastFunction) -> None:
        mt, prefix, agent = self._mediatailor, self._prefix, f"{agent_namespace(self._prefix)}*"
        agent_channels, agent_configurations = mt("channel", agent), mt("playbackConfiguration", agent)
        agent_sources = arn(self, "logs", "delivery-source", agent, colon=True)
        deliveries = arn(self, "logs", "delivery", "*", colon=True)
        destination = self._media.mediatailor_log_destination_arn
        owned = f"agent-owned {agent} resources only"
        self._table(fn, self._data.library_table, write=True)
        self._epg(fn, "s3:DeleteObject")  # the guide of a deleted channel
        fn.allow(_mt("ListChannels", "ListAlerts"), ["*"])
        fn.allow(
            _mt("DescribeChannel", "GetChannelSchedule", "StartChannel", "StopChannel"),
            [mt("channel", f"{prefix}-*")],
            why=_CHANNELS,
        )
        fn.allow(
            _mt("GetPlaybackConfiguration"),
            [mt("playbackConfiguration", f"{prefix}-*")],
            why="playback configurations of the deployment's channels",
        )
        fn.allow(_mt("CreateChannel", "PutPlaybackConfiguration"), ["*"], conditions=self._tagged())
        fn.allow(_mt("TagResource"), [agent_channels, agent_configurations], why=owned, conditions=self._tagged())
        fn.allow(
            _mt("ConfigureLogsForChannel", "PutChannelPolicy", "DeleteChannelPolicy", "DeleteChannel"),
            [agent_channels],
            why=owned,
        )
        fn.allow(
            # AllowVendedLogDeliveryForResource: delivery sources of playback configurations (ad-insertion.md §7.1)
            _mt(
                "ConfigureLogsForPlaybackConfiguration",
                "AllowVendedLogDeliveryForResource",
                "DeletePlaybackConfiguration",
            ),
            [agent_configurations],
            why=owned,
        )
        fn.allow(_mt("DeleteProgram"), [mt("program", f"{agent}/*")], why=owned)
        fn.allow(["logs:PutDeliverySource", "logs:DeleteDeliverySource"], [agent_sources, destination], why=owned)
        fn.allow(
            ["logs:CreateDelivery"],
            [agent_sources, destination, deliveries],
            why="delivery ids are generated; deliveries must carry this deployment's tag",
        )
        fn.allow(
            ["logs:TagResource"],
            [agent_sources, deliveries],
            why="tags of the delivery sources and deliveries of agent channels",
            conditions=self._tagged(),
        )
        fn.allow(
            ["logs:DeleteDelivery"],
            [deliveries],
            why="delivery ids are generated; only deliveries tagged with this deployment",
            conditions={"StringEquals": {f"aws:ResourceTag/{TAG_APP}": prefix}},
        )
        fn.allow(["logs:DescribeDeliveries"], ["*"])
        fn.allow(
            [f"scheduler:{action}" for action in ("CreateSchedule", "UpdateSchedule", "GetSchedule", "DeleteSchedule")],
            [arn(self, "scheduler", "schedule", f"{self.auto_stop_group.schedule_group_name}/*")],
            why="one-time auto-stop schedules (named after their channel) in the auto-stop group",
        )
        fn.allow(
            ["iam:PassRole"],
            [self.auto_stop_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
        )

    def _grant_encoding(self, fn: FastFunction) -> None:
        self._table(fn, self._data.library_table, write=True)
        self._media.ladder_provisioner.grant_start_execution(fn.function)
        fn.allow(["mediaconvert:CreateJobTemplate"], ["*"], conditions=self._tagged())
        fn.allow(
            ["mediaconvert:TagResource"],
            [arn(self, "mediaconvert", "jobTemplates", f"{self._prefix}-*")],
            why="runtime job templates are named <prefix>-<ladder> (the reaper deletes them by that prefix)",
            conditions=self._tagged(),
        )

    # --- helpers -----------------------------------------------------------------------------------------------

    def _table(self, fn: FastFunction, table: dynamodb.ITableV2, *, write: bool) -> None:
        """Item access to ``table`` and its ``GSI1`` (no ``index/*``, no ``DescribeTable``)."""
        actions = [*_TABLE_READ, *(_TABLE_WRITE if write else ())]
        fn.allow([f"dynamodb:{action}" for action in actions], [table.table_arn, f"{table.table_arn}/index/{GSI1}"])

    def _epg(self, fn: FastFunction, *actions: str) -> None:
        fn.allow(
            actions,
            [self._data.media_bucket.arn_for_objects("epg/*")],
            why="EPG feeds epg/<channel>.json and .xml (CONTRACTS §I)",
        )

    def _mediatailor(self, resource: str, name: str) -> str:
        return arn(self, "mediatailor", resource, name)

    def _tagged(self) -> dict[str, Any]:
        """Requests must tag what they create with this deployment's ``fast:app`` (CONTRACTS §E)."""
        return {"StringEquals": {f"aws:RequestTag/{TAG_APP}": self._prefix}}


def _mt(*actions: str) -> list[str]:
    return [f"mediatailor:{action}" for action in actions]
