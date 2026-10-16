#!/usr/bin/env -S uv run python
"""List, and with ``--apply`` delete, the runtime resources of a deployment that CloudFormation does not own.

    uv run python scripts/cleanup.py --prefix fastci            # dry run: what is left
    uv run python scripts/cleanup.py --prefix fastci --apply    # delete it, then run `make destroy` again

The ingest pipeline and the agent create resources at runtime, tagged ``fast:app=<prefix>``: agent channels and
their playback configurations, VOD sources (encodes and kit slates), source locations on operators' buckets, job
templates of agent-provisioned ladders and channel auto-stop schedules. On ``cdk destroy`` the Media stack's
reaper deletes them before their containers. When that fails (a VOD source locked by programs, an interrupted
destroy, a stack deleted with retained resources), this script finishes the job: ``--apply`` runs the reaper's
own steps (``fast_channels.functions.custom_resources.reaper.reap``) in its order, repeating them for up to 10
minutes while Channel Assembly keeps VOD sources locked, then deletes the auto-stop schedules.

Containers stay with CloudFormation: run ``make destroy`` again afterwards. While the stage is deployed and
healthy, ``--apply`` refuses unless ``--force`` is given, because its channels would lose their slates and content.
"""

import argparse
import sys
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import boto3
from _stack import Plane, StackError, Stage, add_prefix_argument, run
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError

from fast_channels.core.naming import mediatailor_name
from fast_channels.functions import aws
from fast_channels.functions.custom_resources import reaper
from fast_channels.functions.custom_resources.channel_lifecycle import error_code
from fast_channels.functions.tools.channels import STOP_CHANNEL_TARGET

if TYPE_CHECKING:
    from mypy_boto3_mediaconvert import MediaConvertClient
    from mypy_boto3_mediatailor import MediaTailorClient
    from mypy_boto3_scheduler import EventBridgeSchedulerClient

APPLY_SECONDS: Final = 600
"""Programs orphaned by a deleted channel lock their VOD sources for 6-8 minutes (channel-assembly.md §9.7)."""
PASS_INTERVAL_SECONDS: Final = 30
RECOVERABLE_STATUSES: Final = frozenset({None, "DELETE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED"})
"""Stack states that allow ``--apply``: deleted, or a destroy (or first deployment) that failed."""
AUTO_STOP: Final = "auto-stop schedule"


@dataclass(frozen=True, slots=True)
class Leftover:
    """A runtime resource that ``--apply`` deletes."""

    kind: str
    name: str


class Deadline(LambdaContext):
    """The reaper's time budget outside Lambda: a wall-clock deadline instead of the invocation's timeout."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def get_remaining_time_in_millis(self) -> int:  # type: ignore[override]  # a staticmethod in LambdaContext
        return max(0, round((self._end - time.monotonic()) * 1000))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="delete the resources (default: only list them)")
    parser.add_argument("--force", action="store_true", help="apply even while the stage is deployed and healthy")
    add_prefix_argument(parser)
    args = parser.parse_args()

    stage = Stage.of(args.prefix)
    # The reaper calls AWS through the functions' shared clients (adaptive retries for MediaTailor's low API
    # quotas), which use boto3's default session: put it in the stage's region, and use the same clients.
    boto3.setup_default_session(region_name=stage.region)
    mediatailor: MediaTailorClient = aws.client("mediatailor")
    scheduler: EventBridgeSchedulerClient = aws.client("scheduler")
    deployment = deployment_of(stage, mediatailor)
    leftovers = [*inventory(deployment, mediatailor, aws.client("mediaconvert")), *auto_stops(stage, scheduler)]
    print(f"Deployment {stage.prefix} in {stage.region}: {len(leftovers)} runtime resources")
    for leftover in leftovers:
        print(f"  {leftover.kind:<24} {leftover.name}")
    if not leftovers:
        return 0
    if not args.apply:
        print("\nDry run: pass --apply to delete them, in this order.")
        return 0
    if (deployed := deployed_stacks(stage)) and not args.force:
        print(
            f"\nRefusing while the stage serves ({', '.join(deployed)}): `make destroy` deletes these resources "
            "with its stacks; pass --force to delete them from a running stage anyway.",
            file=sys.stderr,
        )
        return 1
    pending = reap(deployment)
    delete_schedules(scheduler, [leftover for leftover in leftovers if leftover.kind == AUTO_STOP])
    if pending:
        print(f"\nNot deleted (still in use?): {', '.join(pending)}", file=sys.stderr)
        return 1
    print("\nDone. Run `make destroy` again to delete the stacks.")
    return 0


def deployment_of(stage: Stage, mediatailor: MediaTailorClient) -> reaper.Deployment:
    """What the reaper needs to know about the deployment."""
    try:
        table = stage.output(Plane.DATA, "LibraryTableName")
    except StackError:
        table = ""  # agent channels are then drained of the programs in their schedule only
    return reaper.Deployment(
        prefix=stage.prefix,
        source_location=iac_source_location(mediatailor, stage.prefix),
        job_template_category=stage.prefix,
        table_name=table,
    )


def iac_source_location(mediatailor: MediaTailorClient, prefix: str) -> str:
    """The deployment's CloudFormation source location (tagged ``fast:owner=iac``). Once it is deleted: the name
    the Media stack gives it, which the reaper then finds missing and skips."""
    for location in items(mediatailor, "list_source_locations"):
        tags = location.get("Tags", {})
        if tags.get(reaper.APP_TAG) == prefix and tags.get(reaper.OWNER_TAG) == "iac":
            return str(location["SourceLocationName"])
    return mediatailor_name(prefix, "media")


def inventory(
    deployment: reaper.Deployment, mediatailor: MediaTailorClient, mediaconvert: MediaConvertClient
) -> Iterator[Leftover]:
    """What :func:`reaper.reap` deletes, in its order, selected by the reaper's ownership rules."""
    for channel in items(mediatailor, "list_channels"):
        if deployment.owns_agent_resource(channel["ChannelName"], channel.get("Tags", {})):
            yield Leftover("agent channel", channel["ChannelName"])
    for config in items(mediatailor, "list_playback_configurations"):
        if deployment.owns_agent_resource(config["Name"], config.get("Tags", {})):
            yield Leftover("playback configuration", config["Name"])
    runtime = [
        location["SourceLocationName"]
        for location in items(mediatailor, "list_source_locations")
        if location["SourceLocationName"] != deployment.source_location
        and deployment.created_at_runtime(location.get("Tags", {}))
    ]
    for location in [deployment.source_location, *runtime]:
        try:
            sources = [s["VodSourceName"] for s in items(mediatailor, "list_vod_sources", SourceLocationName=location)]
        except ClientError as error:
            if error_code(error) != "NotFoundException":
                raise
            continue
        yield from (Leftover("VOD source", f"{location}/{source}") for source in sources)
    yield from (Leftover("source location", location) for location in runtime)
    pages = mediaconvert.get_paginator("list_job_templates").paginate(Category=deployment.job_template_category)
    for template in (t for page in pages for t in page["JobTemplates"]):
        tags = mediaconvert.list_tags_for_resource(Arn=template["Arn"])["ResourceTags"].get("Tags", {})
        if deployment.created_at_runtime(tags):
            yield Leftover("job template", template["Name"])


def auto_stops(stage: Stage, scheduler: EventBridgeSchedulerClient) -> Iterator[Leftover]:
    """One-time channel auto-stop schedules (``channels___start_channel``) in the deployment's schedule groups.

    Schedules carry no tags, their group does (``fast:app``). Autopilot schedules in those groups belong to
    CloudFormation and target a Lambda function, not StopChannel.
    """
    groups = (g for page in scheduler.get_paginator("list_schedule_groups").paginate() for g in page["ScheduleGroups"])
    for group in groups:
        tags = {t["Key"]: t["Value"] for t in scheduler.list_tags_for_resource(ResourceArn=group["Arn"])["Tags"]}
        if tags.get(reaper.APP_TAG) != stage.prefix:
            continue
        pages = scheduler.get_paginator("list_schedules").paginate(GroupName=group["Name"])
        for schedule in (s for page in pages for s in page["Schedules"]):
            if schedule.get("Target", {}).get("Arn") == STOP_CHANNEL_TARGET:
                yield Leftover(AUTO_STOP, f"{group['Name']}/{schedule['Name']}")


def reap(deployment: reaper.Deployment) -> list[str]:
    """Run the reaper until nothing is left or the time is up; returns what could not be deleted."""
    reaper.logger.setLevel("WARNING")  # its INFO records are JSON for CloudWatch; the passes are printed instead
    deadline = Deadline(APPLY_SECONDS)
    while True:
        budget = reaper.Budget(deadline, Counter())
        pending = reaper.reap(deployment, budget)
        deleted = ", ".join(f"{count} {kind}" for kind, count in sorted(budget.reaped.items())) or "nothing"
        print(f"deleted {deleted}; {len(pending)} pending")
        if not pending or deadline.get_remaining_time_in_millis() < PASS_INTERVAL_SECONDS * 1000:
            return pending
        time.sleep(PASS_INTERVAL_SECONDS)


def delete_schedules(scheduler: EventBridgeSchedulerClient, schedules: list[Leftover]) -> None:
    for schedule in schedules:
        group, _, name = schedule.name.partition("/")
        try:
            scheduler.delete_schedule(GroupName=group, Name=name)
        except scheduler.exceptions.ResourceNotFoundException:
            continue  # it fired, and deleted itself, in the meantime
        print(f"deleted {AUTO_STOP} {schedule.name}")


def deployed_stacks(stage: Stage) -> list[str]:
    """Stacks of the stage whose resources still serve (they exist and did not fail)."""
    planes = (Plane.MEDIA, Plane.OPERATOR)
    return [stage.stack_name(plane) for plane in planes if stage.status(plane) not in RECOVERABLE_STATUSES]


def items(client: Any, operation: str, **request: Any) -> Iterator[dict[str, Any]]:
    """Every ``Items`` entry of a paginated MediaTailor List* operation."""
    for page in client.get_paginator(operation).paginate(**request):
        yield from page["Items"]


if __name__ == "__main__":
    run(main)
