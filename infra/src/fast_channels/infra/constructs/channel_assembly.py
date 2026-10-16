"""Channel Assembly and MediaTailor ad insertion (SPEC §5.3).

- :class:`ChannelAssemblyOrigin`: the media bucket as an ``S3_SIGV4`` source location.
- :class:`MediaTailorLogs`: one vended-logs destination shared by every playback configuration.
- :class:`AdInsertion`: a playback configuration (SSAI and SGAI) with its three vended-log deliveries.
- :class:`LinearChannel`: an IaC channel with its own ``AdInsertion``, SigV4-only channel policy and lifecycle.
- :class:`AsRunLogging`: retention on the account's as-run log and an ``AIRED`` reconciler for the EPG.
- :class:`RuntimeResourceReaper`: deletes runtime resources (agent channels, VOD sources, job templates) on stack
  deletion, before the containers they would block.
- :func:`playback_configuration_template`: what agent-created channels reuse (``MediaExports``).

Resource shapes come from :mod:`fast_channels.core.specs`, which the agent tools use for the same resources.
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, Self, cast

from aws_cdk import Aws, CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_logs_destinations as log_destinations
from aws_cdk import aws_mediatailor as mediatailor
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import custom_resources as cr
from constructs import Construct

from fast_channels.core import specs
from fast_channels.core.profiles import profile
from fast_channels.infra import nag
from fast_channels.infra.config import ChannelTier
from fast_channels.infra.constructs._provider import acknowledge_framework, trace_waiter
from fast_channels.infra.constructs.functions import FastFunction

if TYPE_CHECKING:
    from aws_cdk import aws_dynamodb as dynamodb
    from constructs import IConstruct

    from fast_channels.core.timeline import ProgrammingBrief
    from fast_channels.infra.stacks.media import ChannelExports

AS_RUN_LOG_GROUP: Final = "MediaTailor/Channel/AsRunLog"
"""Channel Assembly's as-run log group, shared by every channel of the account and region (channel-assembly.md §7.2)."""
VENDED_LOG_TYPES: Final = {
    "AD_DECISION_SERVER_LOGS": "ads",
    "MANIFEST_SERVICE_LOGS": "manifest",
    "TRANSCODE_LOGS": "transcode",
}
"""MediaTailor vended-log types -> delivery source name suffix (``<playback configuration>-<suffix>``)."""
GSI1: Final = "GSI1"
_CHANNEL_HANDLER: Final = "fast_channels.functions.custom_resources.channel_lifecycle.handler"
_TOKEN: Final = re.compile(r"\$\{Token\[[^\]]*\]\}")


def _arn(service: str, resource: str) -> str:
    """ARN in this account and region with pseudo parameters, so cdk-nag finding ids do not depend on the env."""
    return f"arn:{Aws.PARTITION}:{service}:{Aws.REGION}:{Aws.ACCOUNT_ID}:{resource}"


def _finding(service: str, resource: str) -> str:
    """cdk-nag ``AwsSolutions-IAM5`` id of a wildcard :func:`_arn`."""
    return nag.wildcard_resource(f"arn:<AWS::Partition>:{service}:<AWS::Region>:<AWS::AccountId>:{resource}")


def _with_tokens[R](build: Callable[..., R], **kwargs: Any) -> R:
    """``build(**kwargs)`` for a ``core.specs`` builder whose string arguments contain CDK tokens.

    The builders parse URLs and validate ADS variables, and an unresolved token (``${Token[...]}``) is neither a
    host nor free of brackets. Each token is swapped for a unique placeholder host during the call and put back in
    the result.
    """
    placeholders: dict[str, str] = {}

    def hide(match: re.Match[str]) -> str:
        placeholder = f"cdk-token-{len(placeholders)}.invalid"
        placeholders[placeholder] = match.group()
        return placeholder

    def restore(value: Any) -> Any:
        match value:
            case str():
                for placeholder, token in placeholders.items():
                    value = value.replace(placeholder, token)
                return value
            case dict():
                return {k: restore(v) for k, v in value.items()}
            case list():
                return [restore(v) for v in value]
            case _:
                return value

    return cast("R", restore(build(**{k: _TOKEN.sub(hide, v) if isinstance(v, str) else v for k, v in kwargs.items()})))


def _override(resource: Any, properties: Mapping[str, Any]) -> None:
    """Set CloudFormation ``properties`` verbatim (core.specs shapes are in CloudFormation case)."""
    for key, value in properties.items():
        resource.add_property_override(key, value)


class ChannelAssemblyOrigin(Construct):
    """The media bucket as a Channel Assembly source location with ``S3_SIGV4`` access.

    Channel Assembly reads manifests from the bucket with SigV4, which needs the regional virtual-hosted BaseUrl
    (path-style, global and dual-stack hosts are rejected, channel-assembly.md §2.2) and a bucket policy that admits
    ``mediatailor.amazonaws.com`` for this source location (``MediaStorage.allow_channel_assembly_read``).
    Segment URLs in channel manifests are absolute and taken from ``DefaultSegmentDeliveryConfiguration``; SSAI does
    not rewrite them (channel-assembly.md §6.3), so the content CDN host is chosen here.

    Args:
        name: source location name; must match the bucket policy's ``sourceLocation/<prefix>*``.
        bucket: the media bucket (SSE-S3: SSE-KMS breaks manifest reads, channel-assembly.md §2.4).
        content_base_url: absolute base URL of content segments (``delivery.content_domain`` or CloudFront).

    Attributes:
        source_location: the ``CfnSourceLocation``.
        name: its name. arn: its ARN. base_url: its S3 BaseUrl.
    """

    def __init__(self, scope: Construct, id: str, *, name: str, bucket: s3.IBucket, content_base_url: str) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.name = name
        self.base_url = f"https://{bucket.bucket_name}.s3.{stack.region}.{stack.url_suffix}"
        self._bucket = bucket
        self.source_location = mediatailor.CfnSourceLocation(
            self,
            "Resource",
            source_location_name=name,
            http_configuration=mediatailor.CfnSourceLocation.HttpConfigurationProperty(base_url=self.base_url),
            access_configuration=mediatailor.CfnSourceLocation.AccessConfigurationProperty(access_type="S3_SIGV4"),
            default_segment_delivery_configuration=(
                mediatailor.CfnSourceLocation.DefaultSegmentDeliveryConfigurationProperty(base_url=content_base_url)
            ),
        )
        self.arn = self.source_location.attr_arn

    def grant_vod_sources(self, grantee: iam.IGrantable) -> iam.Grant:
        """Let ``grantee`` register and maintain VOD sources here (the ingest pipeline, the origins tools).

        CreateVodSource also checks that the *caller* can read the manifests (channel-assembly.md §2.3), hence
        ``s3:GetObject`` on the asset prefix. The grantee's own construct acknowledges the resulting wildcards.
        """
        self._bucket.grant_read(grantee, "assets/*")
        return iam.Grant.add_to_principal(
            grantee=grantee,
            actions=[
                "mediatailor:CreateVodSource",
                "mediatailor:DescribeVodSource",
                "mediatailor:UpdateVodSource",
                "mediatailor:DeleteVodSource",
                "mediatailor:TagResource",
            ],
            resource_arns=[_arn("mediatailor", f"vodSource/{self.name}/*")],
        )


class MediaTailorLogs(Construct):
    """The vended-logs destination of every MediaTailor playback configuration in the deployment.

    One CloudWatch Logs group ``/aws/vendedlogs/mediatailor/<prefix>`` behind one delivery destination; each
    playback configuration adds a delivery source and a delivery per log type (:meth:`deliver`; the agent's
    channels do the same through the API). The log group carries its own resource policy for the delivery service
    (``AWS::Logs::LogGroup.ResourcePolicyDocument``). The L2 ``add_to_resource_policy`` would instead grow the
    account-wide policy, which is limited to 5,120 characters and 10 per region (_review.md, account-level policy).

    Args:
        prefix: deployment prefix; delivery sources must be named ``<prefix>-*``.
        retention: log retention.
        removal_policy: what happens to the log group when the stack is deleted.

    Attributes:
        log_group: the destination log group. destination_arn: ARN of the delivery destination
            (``MediaExports.mediatailor_log_destination_arn``).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        name = f"/aws/vendedlogs/mediatailor/{prefix}"
        self.log_group = logs.LogGroup(
            self, "Logs", log_group_name=name, retention=retention, removal_policy=removal_policy
        )
        cast("logs.CfnLogGroup", self.log_group.node.default_child).resource_policy_document = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "MediaTailorVendedLogs",
                    "Effect": "Allow",
                    "Principal": {"Service": "delivery.logs.amazonaws.com"},
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": _arn("logs", f"log-group:{name}:log-stream:*"),
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                        "ArnLike": {"aws:SourceArn": _arn("logs", f"delivery-source:{prefix}-*")},
                    },
                }
            ],
        }
        self._destination = logs.CfnDeliveryDestination(
            self,
            "Destination",
            name=f"{prefix}-mediatailor",
            destination_resource_arn=self.log_group.log_group_arn,
            output_format="json",
        )
        self.destination_arn = self._destination.attr_arn
        self._last_delivery: logs.CfnDelivery | None = None

    def deliver(self, scope: Construct, playback_configuration: mediatailor.CfnPlaybackConfiguration) -> None:
        """Deliver the three log types of ``playback_configuration`` here, from delivery sources in ``scope``.

        Deliveries to one destination are created one after another (the research stack chained them too).
        """
        for log_type, suffix in VENDED_LOG_TYPES.items():
            source = logs.CfnDeliverySource(
                scope,
                f"{suffix.title()}LogSource",
                name=f"{playback_configuration.name}-{suffix}",
                log_type=log_type,
                resource_arn=playback_configuration.attr_playback_configuration_arn,
            )
            delivery = logs.CfnDelivery(
                scope,
                f"{suffix.title()}LogDelivery",
                delivery_source_name=source.ref,
                delivery_destination_arn=self.destination_arn,
            )
            if self._last_delivery is not None:
                delivery.add_resource_dependency(self._last_delivery)
            self._last_delivery = delivery


def playback_configuration_template(
    scope: Construct,
    *,
    content_base_url: str,
    log_destination_arn: str,
    log_percent: int,
    ads_url: str | None = None,
) -> str:
    """JSON of what agent-created playback configurations share (``MediaExports.playback_configuration_template``,
    the tools' ``PLAYBACK_CONFIG_TEMPLATE``)::

        {"content_base_url": str, "log_percent": int, "log_destination_arn": str,
         "log_types": {"AD_DECISION_SERVER_LOGS": "ads", "MANIFEST_SERVICE_LOGS": "manifest", "TRANSCODE_LOGS":
         "transcode"}, "ads_url": str}   # ads_url only with an external ADS

    A tool builds a channel's configuration with ``core.specs.playback_configuration(name=<channel>,
    video_content_source_url=<channel base URL>, ads_url=template.get("ads_url") or core.specs.ads_url(...),
    content_base_url, log_percent, tags)``, applies it with ``core.specs.playback_configuration_requests`` and
    delivers each log type from a delivery source named ``<channel>-<log_types[type]>`` (at most 60 characters,
    so channel names of at most 50) to ``log_destination_arn`` (verified live with an agent-style channel).

    Args:
        content_base_url: CDN-neutral base URL of content and ads.
        log_destination_arn: :attr:`MediaTailorLogs.destination_arn`.
        log_percent: share of sessions that log (``observability.mediatailor_log_percent``).
        ads_url: the external ADS URL (``ads.mode = external_vast``); ``None`` for preconditioned ads.
    """
    return Stack.of(scope).to_json_string(
        {
            "content_base_url": content_base_url,
            "log_percent": log_percent,
            "ads_url": ads_url,
            "log_destination_arn": log_destination_arn,
            "log_types": VENDED_LOG_TYPES,
        }
    )


class AdInsertion(Construct):
    """A MediaTailor playback configuration serving SSAI and SGAI (``PLAYER_SELECT``) from one channel, with its
    vended logs.

    The configuration is ``core.specs.playback_configuration`` verbatim (preconditioned ads, all three
    personalization timeouts, absolute CDN prefixes, VENDED_LOGS), so IaC channels and agent channels are identical.

    Args:
        name: configuration name (the channel name by convention; the channel policy admits exactly this one).
        channel_url: Channel Assembly base URL of the channel (``https://<host>/v1/channel/<name>/``); MediaTailor
            signs requests to Channel Assembly hosts with SigV4 (channel-assembly.md §6.1).
        ads_url: ADS URL (``core.specs.ads_url`` for preconditioned ads); may contain CDK tokens.
        content_base_url: base URL of ad and content segments; may contain CDK tokens.
        vended_logs: the shared vended-logs destination.
        log_percent: share of sessions that log (100 for demos, about 10 in production).

    Attributes:
        playback_configuration: the ``CfnPlaybackConfiguration``. name, arn, hls_prefix, dash_prefix: its name,
            ARN and HLS/DASH ``ManifestEndpointPrefix``.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        channel_url: str,
        ads_url: str,
        content_base_url: str,
        vended_logs: MediaTailorLogs,
        log_percent: int = 100,
    ) -> None:
        super().__init__(scope, id)
        properties = specs.to_cfn_playback_configuration(
            _with_tokens(
                specs.playback_configuration,
                name=name,
                video_content_source_url=channel_url,
                ads_url=ads_url,
                content_base_url=content_base_url,
                log_percent=log_percent,
                tags={},  # fast:app and fast:owner come from the stage's tags
            )
        )
        required = {key: properties.pop(key) for key in ("Name", "AdDecisionServerUrl", "VideoContentSourceUrl")}
        properties.pop("Tags")
        self.playback_configuration = mediatailor.CfnPlaybackConfiguration(
            self,
            "Resource",
            name=required["Name"],
            ad_decision_server_url=required["AdDecisionServerUrl"],
            video_content_source_url=required["VideoContentSourceUrl"],
        )
        _override(self.playback_configuration, properties)
        vended_logs.deliver(self, self.playback_configuration)
        self.name = name
        self.arn = self.playback_configuration.attr_playback_configuration_arn
        self.hls_prefix = self.playback_configuration.attr_hls_configuration_manifest_endpoint_prefix
        self.dash_prefix = self.playback_configuration.attr_dash_configuration_manifest_endpoint_prefix


class _ChannelProvider(Construct):
    """The stack's custom-resource provider for every :class:`LinearChannel` (one Lambda, three resource types)."""

    def __init__(self, scope: Construct, id: str, *, log_retention: logs.RetentionDays, removal_policy: RemovalPolicy):
        super().__init__(scope, id)
        self.handler = FastFunction(
            self,
            "Handler",
            handler=_CHANNEL_HANDLER,
            description="Linear channel custom resources: update guard, endpoint discovery, lifecycle",
            timeout=Duration.minutes(10),  # draining 400 programs at 3 TPS takes about 2.5 minutes
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        nag.acknowledge(self.handler, {nag.LAMBDA_DLQ: "Invoked synchronously by the custom-resource provider"})
        # The update guard stands down while its stack rolls back.
        self.handler.function.add_to_role_policy(
            iam.PolicyStatement(actions=["cloudformation:DescribeStacks"], resources=[Aws.STACK_ID])
        )
        self.provider = cr.Provider(
            self,
            "Provider",
            on_event_handler=self.handler.function,
            framework_lambda_logging_level=lambda_.ApplicationLogLevel.INFO,
        )
        acknowledge_framework(self.provider, self.handler.function)

    @classmethod
    def of(cls, scope: Construct, *, log_retention: logs.RetentionDays, removal_policy: RemovalPolicy) -> Self:
        stack = Stack.of(scope)
        existing = stack.node.try_find_child("LinearChannelProvider")
        if isinstance(existing, cls):
            return existing
        return cls(stack, "LinearChannelProvider", log_retention=log_retention, removal_policy=removal_policy)


class LinearChannel(Construct):
    """A Channel Assembly ``LINEAR`` channel owned by CloudFormation, with its per-channel ad insertion.

    - ``CfnChannel``: the filler slate, one output per rendition profile (``core.specs.channel_outputs``: HLS with
      DATERANGE ad markers, which SGAI requires, and DASH) and AS_RUN logging. Channels are created stopped and are
      never started automatically.
    - :class:`AdInsertion` ``<name>``, and a channel policy (``core.specs.channel_policy``) that lets only that
      playback configuration read the channel, with SigV4; anonymous requests get 403 (channel-assembly.md §6.1).
    - Three custom resources (``fast_channels.functions.custom_resources.channel_lifecycle``): a guard that fails an
      update of a running channel fast (or stops it when ``restart_on_update``), the endpoint lookup (CloudFormation
      exposes only the ARN), and the lifecycle that writes the ``CHANNEL#<name>`` ``META`` item, restarts a channel
      the guard stopped, and on deletion stops the channel and deletes its programs first. If an update fails after
      the guard stopped the channel, the rollback leaves it stopped.

    The filler slate cannot be deleted while a channel uses it ("There is at least one channel that uses it for
    filler slate", observed live), so register channels with :meth:`RuntimeResourceReaper.run_after_deleting`, and
    make them depend on whatever provisions the filler slate.

    Args:
        name: channel name (``<prefix>-...``, never ``<prefix>-agent-...``); also the playback configuration name.
        ladder_version: ladder of every program, slate and ad on the channel.
        outputs: rendition profiles published as outputs; every VOD source must be packaged for each.
        filler_slate: VOD source (in ``origin``) of the ladder's filler slate; it must exist before the channel.
        origin: the deployment's source location.
        vended_logs: vended-logs destination for the playback configuration.
        library: media library table (``CHANNEL#`` items).
        content_base_url: base URL of content and ad segments.
        log_retention: retention of the custom-resource function logs.
        ads_url: external ADS URL; default ``core.specs.ads_url`` (the ladder's preconditioned VAST pod).
        tier: channel tier (BASIC or STANDARD).
        brief: initial programming brief; the agent may change it later (deployments only overwrite it when this
            value changes).
        log_percent: share of playback sessions that emit vended logs.
        restart_on_update: let deployments stop a running channel to update it, and start it again.
        removal_policy: what happens to the custom-resource function logs when the stack is deleted.

    Attributes:
        channel: the ``CfnChannel``. ad_insertion: its :class:`AdInsertion`. exports: :class:`ChannelExports`.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        ladder_version: str,
        outputs: Sequence[str],
        filler_slate: str,
        origin: ChannelAssemblyOrigin,
        vended_logs: MediaTailorLogs,
        library: dynamodb.ITableV2,
        content_base_url: str,
        log_retention: logs.RetentionDays,
        ads_url: str | None = None,
        tier: ChannelTier = ChannelTier.BASIC,
        brief: ProgrammingBrief | None = None,
        log_percent: int = 100,
        restart_on_update: bool = False,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.name, self.ladder_version, self.outputs = name, ladder_version, tuple(outputs)
        provider = _ChannelProvider.of(self, log_retention=log_retention, removal_policy=removal_policy)
        spec = {
            "PlaybackMode": "LINEAR",
            "Tier": str(tier),
            "FillerSlate": {"SourceLocationName": origin.name, "VodSourceName": filler_slate},
            "Outputs": specs.channel_outputs([profile(o) for o in self.outputs]),
            "LogConfiguration": {"LogTypes": ["AS_RUN"]},
        }

        def resource(id: str, resource_type: str, **properties: Any) -> CustomResource:
            return CustomResource(
                self,
                id,
                service_token=provider.provider.service_token,
                resource_type=resource_type,
                properties={"ChannelName": name, **properties},
            )

        guard = resource("Guard", "Custom::ChannelGuard", RestartOnUpdate=restart_on_update, Spec=spec)
        self.channel = mediatailor.CfnChannel(self, "Resource", channel_name=name, playback_mode="LINEAR", outputs=[])
        _override(self.channel, {key: value for key, value in spec.items() if key != "PlaybackMode"})
        self.channel.node.add_dependency(guard, origin.source_location)

        endpoint = resource("Endpoint", "Custom::ChannelEndpoint", Outputs=list(self.outputs))
        endpoint.node.add_dependency(self.channel)
        channel_url = endpoint.get_att_string("ChannelUrl")
        preconditioned = {"content_base_url": content_base_url, "ladder_version": ladder_version, "channel": name}
        self.ad_insertion = AdInsertion(
            self,
            "AdInsertion",
            name=name,
            channel_url=channel_url,
            ads_url=ads_url or _with_tokens(specs.ads_url, **preconditioned),
            content_base_url=content_base_url,
            vended_logs=vended_logs,
            log_percent=log_percent,
        )
        policy = mediatailor.CfnChannelPolicy(
            self,
            "Policy",
            channel_name=name,
            policy=specs.channel_policy(
                channel_arn=self.channel.attr_arn,
                playback_configuration_arn=self.ad_insertion.arn,
                account=Aws.ACCOUNT_ID,
            ),
        )
        policy.add_resource_dependency(self.channel)

        lifecycle = resource(
            "Lifecycle",
            "Custom::ChannelLifecycle",
            TableName=library.table_name,
            RestartToken=guard.get_att_string("StoppedForUpdate"),
            Brief=json.dumps(brief.model_dump(mode="json"), sort_keys=True) if brief else "",
            Record={
                "name": name,
                "arn": self.channel.attr_arn,
                "tier": str(tier),
                "ladder_version": ladder_version,
                "outputs": list(self.outputs),
                "playback_configuration_name": self.ad_insertion.name,
                "playback_configuration_arn": self.ad_insertion.arn,
                "hls_prefix": self.ad_insertion.hls_prefix,
                "dash_prefix": self.ad_insertion.dash_prefix,
                "channel_url": channel_url,
                "source_location": origin.name,
                "filler_slate": filler_slate,
            },
        )
        lifecycle.node.add_dependency(policy)
        self._grant(provider.handler, library)

    def _grant(self, handler: FastFunction, library: dynamodb.ITableV2) -> None:
        handler.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "mediatailor:DescribeChannel",
                    "mediatailor:StartChannel",
                    "mediatailor:StopChannel",
                    "mediatailor:GetChannelSchedule",
                ],
                resources=[_arn("mediatailor", f"channel/{self.name}")],
            )
        )
        handler.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["mediatailor:DeleteProgram"], resources=[_arn("mediatailor", f"program/{self.name}/*")]
            )
        )
        handler.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query"],
                resources=[library.table_arn],
                conditions={"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [f"CHANNEL#{self.name}"]}},
            )
        )
        program = _finding("mediatailor", f"program/{self.name}/*")
        nag.acknowledge(
            handler.function.role,  # type: ignore[arg-type]
            {program: "Programs are created at runtime by the schedule tools"},
        )

    @property
    def exports(self) -> ChannelExports:
        """What the operator plane needs about this channel (``MediaExports.channels``)."""
        # stacks.media imports this module: import its dataclass when used, not when this module loads.
        from fast_channels.infra.stacks.media import ChannelExports  # noqa: PLC0415

        return ChannelExports(
            name=self.name,
            arn=self.channel.attr_arn,
            ladder_version=self.ladder_version,
            outputs=self.outputs,
            playback_configuration_name=self.ad_insertion.name,
            hls_prefix=self.ad_insertion.hls_prefix,
            dash_prefix=self.ad_insertion.dash_prefix,
        )


class AsRunLogging(Construct):
    """Retention on Channel Assembly's as-run log, and the reconciler that marks EPG programs ``AIRED``.

    The account-wide group ``MediaTailor/Channel/AsRunLog`` is created by MediaTailor on first use, without
    retention; an idempotent custom resource creates it if needed and sets its retention, and leaves it on deletion
    (other deployments may log there). A subscription filter sends the deployment's ``VodProgramStart`` events to
    ``fast_channels.functions.as_run_reconciler``. A log group has at most two subscription filters, so at most two
    deployments per account and region can reconcile as-run events.

    Args:
        prefix: deployment prefix; channels named ``<prefix>-*`` are reconciled.
        library: media library table (``PROGRAM`` items).
        as_run_retention_days: retention of the shared as-run log group, in days.
        log_retention: retention of the functions' own logs.
        removal_policy: what happens to the functions' logs when the stack is deleted.

    Attributes:
        log_group_name: the as-run log group. reconciler: the reconciler :class:`FastFunction`.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        library: dynamodb.ITableV2,
        as_run_retention_days: int,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.log_group_name = AS_RUN_LOG_GROUP
        handler = FastFunction(
            self,
            "Handler",
            handler="fast_channels.functions.custom_resources.as_run.handler",
            description=f"Create {AS_RUN_LOG_GROUP} if missing and set its retention",
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        handler.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:PutRetentionPolicy"],
                resources=[_arn("logs", f"log-group:{AS_RUN_LOG_GROUP}")],
            )
        )
        nag.acknowledge(handler, {nag.LAMBDA_DLQ: "Invoked synchronously by the custom-resource provider"})
        provider = cr.Provider(
            self,
            "Provider",
            on_event_handler=handler.function,
            framework_lambda_logging_level=lambda_.ApplicationLogLevel.INFO,
        )
        acknowledge_framework(provider, handler.function)
        log_group = CustomResource(
            self,
            "LogGroup",
            service_token=provider.service_token,
            resource_type="Custom::AsRunLogGroup",
            properties={"LogGroupName": AS_RUN_LOG_GROUP, "RetentionInDays": as_run_retention_days},
        )

        self.reconciler = FastFunction(
            self,
            "Reconciler",
            handler="fast_channels.functions.as_run_reconciler.handler",
            description="Mark scheduled programs AIRED from Channel Assembly as-run events",
            environment={"LIBRARY_TABLE": library.table_name},
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        self.reconciler.function.add_to_role_policy(
            iam.PolicyStatement(actions=["dynamodb:Query"], resources=[f"{library.table_arn}/index/{GSI1}"])
        )
        self.reconciler.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem"],
                resources=[library.table_arn],
                conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"CHANNEL#{prefix}-*"]}},
            )
        )
        nag.acknowledge(
            self.reconciler,
            {
                nag.LAMBDA_DLQ: "Asynchronous invocations by CloudWatch Logs are retried twice; the AIRED mark is "
                "informational (the as-run log keeps the record)"
            },
        )
        subscription = logs.SubscriptionFilter(
            self,
            "ProgramStarts",
            log_group=logs.LogGroup.from_log_group_name(self, "AsRunLog", AS_RUN_LOG_GROUP),
            destination=log_destinations.LambdaDestination(self.reconciler.function),
            filter_name=f"{prefix}-as-run",
            filter_pattern=logs.FilterPattern.literal(
                f'{{ ($.eventType = "VodProgramStart") && ($.channelName = "{prefix}-*") }}'
            ),
        )
        subscription.node.add_dependency(log_group)


class RuntimeResourceReaper(Construct):
    """Deletes the deployment's runtime resources when its stack is deleted (SPEC §0.3, §5.3).

    An asynchronous custom resource (``fast_channels.functions.custom_resources.reaper``) whose deletion removes
    agent channels with their programs, policies, playback configurations and deliveries, the VOD sources of the
    deployment's and of runtime source locations (1 TPS, retrying through the 6-8 minute lock of orphaned programs),
    runtime source locations, and runtime MediaConvert job templates of the deployment's category. Only resources
    tagged ``fast:app=<prefix>`` (and, except in the deployment's source location, ``fast:owner`` ``pipeline`` or
    ``agent``) are touched.

    CloudFormation deletes a resource before the resources it depends on. The reaper therefore depends on what it
    must empty first: the source location here, and anything else through :meth:`run_before_deleting` (the shared
    log destination, the MediaConvert queue). IaC channels must be deleted before it (their lifecycle deletes
    their programs and frees the filler slate): :meth:`run_after_deleting`.

    It never reaps on an update, or when ``removal_policy`` is ``RETAIN``. Keep its construct id stable: a new
    logical id deletes the old resource, which is harmless (the handler checks that the stack itself is being
    deleted) but needless.

    Args:
        prefix: deployment prefix (``fast:app`` tag value, ``<prefix>-agent-`` namespace).
        origin: the deployment's source location.
        library: media library table (PROGRAM items of agent channels).
        log_retention: retention of the functions' logs.
        removal_policy: ``DESTROY`` reaps on stack deletion; ``RETAIN`` keeps everything.
        job_template_category: MediaConvert category of runtime job templates (default: ``prefix``).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        origin: ChannelAssemblyOrigin,
        library: dynamodb.ITableV2,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
        job_template_category: str | None = None,
    ) -> None:
        super().__init__(scope, id)
        functions = {
            name: FastFunction(
                self,
                name.title().replace("_", ""),
                handler=f"fast_channels.functions.custom_resources.reaper.{name}",
                description=f"Runtime resource reaper ({name.replace('_', ' ')})",
                timeout=timeout,
                log_retention=log_retention,
                removal_policy=removal_policy,
            )
            for name, timeout in (("on_event", Duration.seconds(30)), ("is_complete", Duration.minutes(5)))
        }
        for fn in functions.values():
            nag.acknowledge(fn, {nag.LAMBDA_DLQ: "Invoked synchronously by the custom-resource provider"})
        self._grant(functions["is_complete"], prefix=prefix, library=library)
        waiter_logs = logs.LogGroup(
            self,
            "WaiterLogs",
            log_group_name=f"/aws/vendedlogs/states/{prefix}-reaper",
            retention=log_retention,
            removal_policy=removal_policy,
        )
        provider = cr.Provider(
            self,
            "Provider",
            on_event_handler=functions["on_event"].function,
            is_complete_handler=functions["is_complete"].function,
            query_interval=Duration.seconds(30),
            # Below CloudFormation's one-hour custom-resource timeout; 1,000 VOD sources take ~17 min at 1 TPS.
            total_timeout=Duration.minutes(55),
            framework_lambda_logging_level=lambda_.ApplicationLogLevel.INFO,
            disable_waiter_state_machine_logging=False,
            waiter_state_machine_log_options=cr.LogOptions(
                destination=waiter_logs, level=sfn.LogLevel.ALL, include_execution_data=True
            ),
        )
        trace_waiter(provider)
        acknowledge_framework(provider, *(fn.function for fn in functions.values()))
        self.resource = CustomResource(
            self,
            "Resource",
            service_token=provider.service_token,
            resource_type="Custom::RuntimeResourceReaper",
            properties={
                "Prefix": prefix,
                "SourceLocation": origin.name,
                "JobTemplateCategory": job_template_category or prefix,
                "TableName": library.table_name,
                "Enabled": removal_policy == RemovalPolicy.DESTROY,
            },
        )
        self.run_before_deleting(origin)

    def run_before_deleting(self, *constructs: IConstruct) -> None:
        """Reap before CloudFormation deletes ``constructs`` (the reaper depends on them)."""
        self.resource.node.add_dependency(*constructs)

    def run_after_deleting(self, *constructs: IConstruct) -> None:
        """Reap only after CloudFormation deleted ``constructs`` (they depend on the reaper)."""
        for construct in constructs:
            construct.node.add_dependency(self.resource)

    def _grant(self, handler: FastFunction, *, prefix: str, library: dynamodb.ITableV2) -> None:
        agent = f"{prefix}-agent-*"
        scoped = (
            (
                "mediatailor",
                f"channel/{agent}",
                [
                    "mediatailor:DescribeChannel",
                    "mediatailor:StopChannel",
                    "mediatailor:GetChannelSchedule",
                    "mediatailor:DeleteChannelPolicy",
                    "mediatailor:DeleteChannel",
                ],
                "Agent channels are created at runtime in the <prefix>-agent- namespace",
            ),
            ("mediatailor", f"program/{agent}/*", ["mediatailor:DeleteProgram"], "Programs are created at runtime"),
            (
                "mediatailor",
                f"playbackConfiguration/{agent}",
                ["mediatailor:GetPlaybackConfiguration", "mediatailor:DeletePlaybackConfiguration"],
                "Agent playback configurations are created at runtime",
            ),
            (
                "mediatailor",
                f"sourceLocation/{prefix}*",
                ["mediatailor:DeleteSourceLocation"],
                "Runtime source locations; the handler only deletes runtime-tagged ones",
            ),
            (
                "mediatailor",
                f"vodSource/{prefix}*/*",
                # ListVodSources is authorized on vodSource/<location>/* (observed live), not on the location.
                ["mediatailor:ListVodSources", "mediatailor:DeleteVodSource"],
                "VOD sources are created at runtime by the pipeline and the agent",
            ),
            (
                "logs",
                "delivery:*",
                ["logs:DeleteDelivery"],
                "Delivery ids are generated; the handler only deletes deliveries of agent playback configurations",
            ),
            (
                "logs",
                f"delivery-source:{agent}",
                ["logs:DeleteDeliverySource"],
                "Delivery sources of agent playback configurations are created at runtime",
            ),
            (
                "mediaconvert",
                f"jobTemplates/{prefix}*",
                ["mediaconvert:ListTagsForResource", "mediaconvert:DeleteJobTemplate"],
                "Runtime job templates; the handler only deletes runtime-tagged ones of the deployment's category",
            ),
        )
        function = handler.function
        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[  # list operations have no resource-level permissions
                    "mediatailor:ListChannels",
                    "mediatailor:ListPlaybackConfigurations",
                    "mediatailor:ListSourceLocations",
                    "mediaconvert:ListJobTemplates",
                    "logs:DescribeDeliveries",
                    "logs:DescribeDeliverySources",
                ],
                resources=["*"],
            )
        )
        for service, resource, actions, _ in scoped:
            function.add_to_role_policy(iam.PolicyStatement(actions=actions, resources=[_arn(service, resource)]))
        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query"],
                resources=[library.table_arn],
                conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"CHANNEL#{agent}"]}},
            )
        )
        function.add_to_role_policy(
            iam.PolicyStatement(actions=["cloudformation:DescribeStacks"], resources=[Aws.STACK_ID])
        )
        nag.acknowledge(
            function.role,  # type: ignore[arg-type]
            {
                nag.wildcard_resource(): "List and describe-deliveries operations have no resource-level permissions",
                **{_finding(service, resource): reason for service, resource, _, reason in scoped},
            },
        )
