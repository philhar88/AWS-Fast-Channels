"""The runtime-resource reaper: deletes what the pipeline and the agent created when the stack is deleted."""

from typing import TYPE_CHECKING

from aws_cdk import Aws, CustomResource, Duration, RemovalPolicy
from aws_cdk import aws_logs as logs
from constructs import Construct

from fast_channels.core.naming import agent_namespace
from fast_channels.core.tags import TAG_APP
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs._provider import provider
from fast_channels.infra.constructs.functions import FastFunction

if TYPE_CHECKING:
    from aws_cdk import aws_dynamodb as dynamodb
    from constructs import IConstruct

    from fast_channels.infra.constructs.channel_assembly.origin import ChannelAssemblyOrigin


class RuntimeResourceReaper(Construct):
    """Deletes the deployment's runtime resources when its stack is deleted (SPEC §0.3, §5.3).

    An asynchronous custom resource (``fast_channels.functions.custom_resources.reaper``) whose deletion removes
    agent channels with their programs, policies, playback configurations and deliveries, the VOD sources of the
    deployment's and of runtime source locations (1 TPS, retrying through the 6-8 minute lock of orphaned programs),
    runtime source locations, and runtime MediaConvert job templates of the deployment's category. Only resources
    tagged ``fast:app=<prefix>`` (and, except in the deployment's source location, ``fast:owner`` ``pipeline`` or
    ``agent``) are touched; the IAM grants match names ``<prefix>-*`` (``-`` included, so deployment ``fast`` never
    reaches ``fastci-*``) and deliveries tagged ``fast:app=<prefix>``.

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
        on_event, is_complete = (
            FastFunction(
                self,
                name.title().replace("_", ""),
                handler=f"fast_channels.functions.custom_resources.reaper.{name}",
                description=f"Runtime resource reaper ({name.replace('_', ' ')})",
                timeout=timeout,
                log_retention=log_retention,
                removal_policy=removal_policy,
            )
            for name, timeout in (("on_event", Duration.seconds(30)), ("is_complete", Duration.minutes(5)))
        )
        self._grant(is_complete, prefix=prefix, library=library)
        reaper = provider(
            self,
            "Provider",
            on_event=on_event,
            is_complete=is_complete,
            waiter_log_group_name=f"/aws/vendedlogs/states/{prefix}-reaper",
            # Below CloudFormation's one-hour custom-resource timeout; 1,000 VOD sources take ~17 min at 1 TPS.
            total_timeout=Duration.minutes(55),
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        self.resource = CustomResource(
            self,
            "Resource",
            service_token=reaper.service_token,
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
        agent = f"{agent_namespace(prefix)}*"
        handler.allow(
            [  # list operations have no resource-level permissions
                "mediatailor:ListChannels",
                "mediatailor:ListPlaybackConfigurations",
                "mediatailor:ListSourceLocations",
                "mediaconvert:ListJobTemplates",
                "logs:DescribeDeliveries",
                "logs:DescribeDeliverySources",
            ],
            ["*"],
            why="List and describe-deliveries operations have no resource-level permissions",
        )
        handler.allow(
            [
                "mediatailor:DescribeChannel",
                "mediatailor:StopChannel",
                "mediatailor:GetChannelSchedule",
                "mediatailor:DeleteChannelPolicy",
                "mediatailor:DeleteChannel",
            ],
            [arn(self, "mediatailor", "channel", agent)],
            why="Agent channels are created at runtime in the <prefix>-agent- namespace",
        )
        handler.allow(
            ["mediatailor:DeleteProgram"],
            [arn(self, "mediatailor", "program", f"{agent}/*")],
            why="Programs are created at runtime",
        )
        handler.allow(
            ["mediatailor:GetPlaybackConfiguration", "mediatailor:DeletePlaybackConfiguration"],
            [arn(self, "mediatailor", "playbackConfiguration", agent)],
            why="Agent playback configurations are created at runtime",
        )
        handler.allow(
            ["mediatailor:DeleteSourceLocation"],
            [arn(self, "mediatailor", "sourceLocation", f"{prefix}-*")],
            why="Runtime source locations; the handler only deletes runtime-tagged ones",
        )
        handler.allow(
            # ListVodSources is authorized on vodSource/<location>/* (observed live), not on the location.
            ["mediatailor:ListVodSources", "mediatailor:DeleteVodSource"],
            [arn(self, "mediatailor", "vodSource", f"{prefix}-*/*")],
            why="VOD sources are created at runtime by the pipeline and the agent",
        )
        handler.allow(
            ["logs:DeleteDelivery"],
            [arn(self, "logs", "delivery", "*", colon=True)],
            why="Delivery ids are generated; only deliveries tagged with this deployment (those of agent playback "
            "configurations)",
            conditions={"StringEquals": {f"aws:ResourceTag/{TAG_APP}": prefix}},
        )
        handler.allow(
            ["logs:DeleteDeliverySource"],
            [arn(self, "logs", "delivery-source", agent, colon=True)],
            why="Delivery sources of agent playback configurations are created at runtime",
        )
        handler.allow(
            ["mediaconvert:ListTagsForResource", "mediaconvert:DeleteJobTemplate"],
            [arn(self, "mediaconvert", "jobTemplates", f"{prefix}-*")],
            why="Runtime job templates; the handler only deletes runtime-tagged ones of the deployment's category",
        )
        handler.allow(
            ["dynamodb:Query"],
            [library.table_arn],
            conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"CHANNEL#{agent}"]}},
        )
        handler.allow(["cloudformation:DescribeStacks"], [Aws.STACK_ID])
