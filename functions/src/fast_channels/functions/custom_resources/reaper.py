"""``Custom::RuntimeResourceReaper``: delete a deployment's runtime resources when its stack is deleted.

CloudFormation owns the containers (source location, IaC channels, playback configurations, built-in job templates);
the ingest pipeline and the agent create their contents at runtime (SPEC §0.3). Left behind, those contents keep
billing (agent channels) and block the containers' deletion (a source location with VOD sources cannot be deleted).

The resource is asynchronous: :func:`on_event` only acknowledges and :func:`is_complete` removes what it can within
one invocation, and is polled until nothing is left:

1. agent channels (``<prefix>-agent-*`` tagged ``fast:app=<prefix>``): stop, delete programs
   (``channel_ops.drain_channel``, within the invocation's time budget), the channel policy, the playback
   configuration of the same name with its vended-log deliveries, then the channel;
2. remaining agent playback configurations (``<prefix>-agent-*``) and their deliveries;
3. VOD sources of the deployment's source location and of runtime source locations, at the 1 TPS quota. Programs
   orphaned by a channel deleted out of band lock a VOD source for 6-8 minutes (channel-assembly.md §9.7); those
   are retried on the next poll;
4. runtime source locations (tagged ``fast:owner`` ``pipeline`` or ``agent``);
5. runtime MediaConvert job templates of the deployment's category (same tags; built-in ones carry ``iac``).

It reaps only when ``Enabled`` is ``true`` and the stack itself is being deleted (or rolled back after a failed
creation): removing or replacing the resource in an update, or rolling an update back, never touches runtime
resources. Errors other than "still in use" are raised: the provider calls :func:`is_complete` once synchronously
(an error there fails the deletion with the AWS error; deleting the stack again resumes) and retries errors on
later polls.
"""

import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.data_classes import CloudFormationCustomResourceEvent, event_source
from botocore.exceptions import ClientError

from fast_channels.core.naming import is_agent_owned
from fast_channels.core.tags import TAG_APP, TAG_OWNER, owned_by, runtime_owned
from fast_channels.functions import aws
from fast_channels.functions.channel_ops import (
    MISSING,
    delete_deliveries,
    drain_channel,
    error_code,
    ignore_missing,
)

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

RESOURCE_TYPE: Final = "Custom::RuntimeResourceReaper"
REAPING_STACK_STATUSES: Final = frozenset({"DELETE_IN_PROGRESS", "ROLLBACK_IN_PROGRESS"})
"""ROLLBACK_IN_PROGRESS only follows a failed stack creation; an update rollback is UPDATE_ROLLBACK_IN_PROGRESS."""
APP_TAG: Final = TAG_APP
"""Former names of the ``fast_channels.core.tags`` keys, kept for existing imports (``scripts/cleanup.py``)."""
OWNER_TAG: Final = TAG_OWNER
DELETE_VOD_SOURCE_INTERVAL_S: Final = 1.0
"""DeleteVodSource is limited to 1 TPS per account (channel-assembly.md §5.6)."""
TIME_MARGIN_MS: Final = 60_000
"""Stop starting new deletions this long before the invocation times out."""

logger = Logger()
metrics = Metrics()


@dataclass(frozen=True, slots=True)
class Deployment:
    """Which runtime resources belong to the deployment (the resource properties)."""

    prefix: str
    source_location: str
    job_template_category: str
    table_name: str

    @classmethod
    def of(cls, properties: Mapping[str, Any]) -> Deployment:
        return cls(
            prefix=properties["Prefix"],
            source_location=properties["SourceLocation"],
            job_template_category=properties["JobTemplateCategory"],
            table_name=properties["TableName"],
        )

    def owns(self, tags: Mapping[str, str]) -> bool:
        """Tagged as this deployment's (``fast:app``)."""
        return owned_by(tags, self.prefix)

    def created_at_runtime(self, tags: Mapping[str, str]) -> bool:
        """This deployment's and created by the pipeline or the agent (CONTRACTS §E)."""
        return runtime_owned(tags, self.prefix)

    def owns_agent_resource(self, name: str, tags: Mapping[str, str]) -> bool:
        """An agent-owned MediaTailor resource (``<prefix>-agent-*``) of this deployment."""
        return is_agent_owned(name, self.prefix) and self.owns(tags)


@dataclass(slots=True)
class Budget:
    """Time left in this invocation, and what was reaped so far."""

    context: LambdaContext
    reaped: Counter[str]

    @property
    def exhausted(self) -> bool:
        return self.context.get_remaining_time_in_millis() < TIME_MARGIN_MS

    def count(self, kind: str) -> None:
        self.reaped[kind] += 1


@logger.inject_lambda_context(correlation_id_path="RequestId")
@event_source(data_class=CloudFormationCustomResourceEvent)
def on_event(event: CloudFormationCustomResourceEvent, context: LambdaContext) -> dict[str, Any]:
    """Acknowledge every request; :func:`is_complete` does the work.

    The physical id never changes: a new one would make CloudFormation delete the old resource, i.e. reap.
    """
    logger.info("reaper request", extra={"request_type": event.request_type})
    return {"PhysicalResourceId": event.physical_resource_id or f"{event.resource_properties['Prefix']}-reaper"}


@logger.inject_lambda_context(correlation_id_path="RequestId")
@event_source(data_class=CloudFormationCustomResourceEvent)
def is_complete(event: CloudFormationCustomResourceEvent, context: LambdaContext) -> dict[str, bool]:
    """Reap a batch on stack deletion; complete once nothing is left (immediately for any other request)."""
    if event.request_type != "Delete" or not should_reap(event):
        return {"IsComplete": True}
    budget = Budget(context, Counter())
    pending = reap(Deployment.of(event.resource_properties), budget)
    logger.info("reaper pass", extra={"reaped": dict(budget.reaped), "pending": pending[:50]})
    if budget.reaped:
        for kind, count in budget.reaped.items():
            metrics.add_metric(name=f"Reaped{kind}", unit=MetricUnit.Count, value=count)
        metrics.flush_metrics()
    return {"IsComplete": not pending}


def should_reap(event: CloudFormationCustomResourceEvent) -> bool:
    """Enabled, and the stack is going away (not an update that removes or replaces this resource)."""
    if event.resource_properties.get("Enabled") != "true":
        logger.info("reaper disabled (removal policy retain)")
        return False
    [stack] = aws.client("cloudformation").describe_stacks(StackName=event.stack_id)["Stacks"]
    status = stack["StackStatus"]
    logger.info("stack status", extra={"stack_status": status})
    return status in REAPING_STACK_STATUSES


def reap(deployment: Deployment, budget: Budget) -> list[str]:
    """Delete what can be deleted now; returns what is left (empty when done)."""
    steps: tuple[Callable[[Deployment, Budget], list[str]], ...] = (
        _agent_channels,
        _agent_playback_configurations,
        _vod_sources,
        _runtime_source_locations,
        _runtime_job_templates,
    )
    pending: list[str] = []
    for step in steps:
        if budget.exhausted:
            return [*pending, "(time budget exhausted)"]
        pending += step(deployment, budget)
    return pending


# --- steps ---------------------------------------------------------------------------------------------------------


def _agent_channels(deployment: Deployment, budget: Budget) -> list[str]:
    mediatailor = aws.client("mediatailor")
    channels = [
        c["ChannelName"]
        for c in _items(mediatailor, "list_channels")
        if deployment.owns_agent_resource(c["ChannelName"], c.get("Tags", {}))
    ]
    for done, channel in enumerate(channels):
        if (
            budget.exhausted
            or not drain_channel(
                channel, table_name=deployment.table_name, out_of_time=lambda: budget.exhausted
            ).complete
        ):
            return [f"channel/{c}" for c in channels[done:]]
        ignore_missing(mediatailor.delete_channel_policy, ChannelName=channel)
        _delete_playback_configuration(channel, budget)
        ignore_missing(mediatailor.delete_channel, ChannelName=channel)
        budget.count("Channels")
        logger.info("agent channel deleted", extra={"channel": channel})
    return []


def _agent_playback_configurations(deployment: Deployment, budget: Budget) -> list[str]:
    names = [
        p["Name"]
        for p in _items(aws.client("mediatailor"), "list_playback_configurations")
        if deployment.owns_agent_resource(p["Name"], p.get("Tags", {}))
    ]
    for done, name in enumerate(names):
        if budget.exhausted:
            return [f"playbackConfiguration/{n}" for n in names[done:]]
        _delete_playback_configuration(name, budget)
    return []


def _vod_sources(deployment: Deployment, budget: Budget) -> list[str]:
    mediatailor = aws.client("mediatailor")
    pending: list[str] = []
    for location in [deployment.source_location, *_runtime_locations(deployment)]:
        try:
            sources = [s["VodSourceName"] for s in _items(mediatailor, "list_vod_sources", SourceLocationName=location)]
        except ClientError as error:
            if error_code(error) in MISSING:
                continue
            raise
        for done, source in enumerate(sources):
            if budget.exhausted:
                return [*pending, *(f"vodSource/{location}/{s}" for s in sources[done:])]
            if _delete_vod_source(location, source):
                budget.count("VodSources")
            else:
                pending.append(f"vodSource/{location}/{source}")
            time.sleep(DELETE_VOD_SOURCE_INTERVAL_S)
    return pending


def _runtime_source_locations(deployment: Deployment, budget: Budget) -> list[str]:
    pending: list[str] = []
    for location in _runtime_locations(deployment):
        try:
            aws.client("mediatailor").delete_source_location(SourceLocationName=location)
            budget.count("SourceLocations")
        except ClientError as error:
            if error_code(error) in MISSING:
                continue
            if error_code(error) != "BadRequestException":  # still has sources: retried on the next poll
                raise
            pending.append(f"sourceLocation/{location}")
    return pending


def _runtime_job_templates(deployment: Deployment, budget: Budget) -> list[str]:
    mediaconvert = aws.client("mediaconvert")
    pages = mediaconvert.get_paginator("list_job_templates").paginate(Category=deployment.job_template_category)
    for template in (t for page in pages for t in page["JobTemplates"]):
        tags = mediaconvert.list_tags_for_resource(Arn=template["Arn"])["ResourceTags"].get("Tags", {})
        if deployment.created_at_runtime(tags):
            ignore_missing(mediaconvert.delete_job_template, Name=template["Name"])
            budget.count("JobTemplates")
            logger.info("runtime job template deleted", extra={"job_template": template["Name"]})
    return []


# --- helpers -------------------------------------------------------------------------------------------------------


def _items(client: Any, operation: str, **request: Any) -> Iterator[dict[str, Any]]:
    """Every ``Items`` entry of a paginated MediaTailor List* operation."""
    for page in client.get_paginator(operation).paginate(**request):
        yield from page["Items"]


def _runtime_locations(deployment: Deployment) -> list[str]:
    """Source locations the agent or pipeline created for this deployment (e.g. on operators' own buckets)."""
    return [
        location["SourceLocationName"]
        for location in _items(aws.client("mediatailor"), "list_source_locations")
        if location["SourceLocationName"] != deployment.source_location
        and deployment.created_at_runtime(location.get("Tags", {}))
    ]


def _delete_vod_source(location: str, source: str) -> bool:
    """``False`` while programs still refer to the VOD source (BadRequestException)."""
    try:
        aws.client("mediatailor").delete_vod_source(SourceLocationName=location, VodSourceName=source)
    except ClientError as error:
        if error_code(error) in MISSING:
            return True
        if error_code(error) != "BadRequestException":
            raise
        logger.info("VOD source still in use", extra={"vod_source": source, "error": str(error)})
        return False
    return True


def _delete_playback_configuration(name: str, budget: Budget) -> None:
    """Delete a playback configuration after its vended-log deliveries and delivery sources."""
    mediatailor = aws.client("mediatailor")
    try:
        arn = mediatailor.get_playback_configuration(Name=name)["PlaybackConfigurationArn"]
    except ClientError as error:
        if error_code(error) in MISSING:
            return
        raise
    delete_deliveries(arn)
    ignore_missing(mediatailor.delete_playback_configuration, Name=name)
    budget.count("PlaybackConfigurations")
    logger.info("agent playback configuration deleted", extra={"playback_configuration": name})
