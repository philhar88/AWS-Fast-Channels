"""What the ingest pipeline adds to the media-library schema: its owner, the tags of the resources it creates and the
input key of kit child executions.

Keys, statuses and attribute codecs are :mod:`fast_channels.functions.media_library`'s; resource names are
:mod:`fast_channels.core.naming`'s (``job_template_name`` and ``vod_source_name`` are re-exported here for existing
imports).
"""

from typing import Final

from fast_channels.core.naming import job_template_name, vod_source_name
from fast_channels.core.tags import Owner, resource_tags

__all__ = ["OWNER", "PARENT_EXECUTION_KEY", "job_template_name", "runtime_tags", "vod_source_name"]

OWNER: Final = Owner.PIPELINE.value
"""``fast:owner`` of every resource the pipeline creates (and ``owner`` of its asset items)."""
PARENT_EXECUTION_KEY: Final = "AWS_STEP_FUNCTIONS_STARTED_BY_EXECUTION_ID"
"""Input key Step Functions adds to child executions started with ``associate_with_parent`` (ladder kits)."""


def runtime_tags(prefix: str, *, ladder_version: str, correlation_id: str, **extra: str) -> dict[str, str]:
    """Tags of a runtime resource the pipeline creates (CONTRACTS §E); the reaper finds them by ``fast:owner``."""
    return resource_tags(prefix, Owner.PIPELINE, ladder_version=ladder_version, correlation_id=correlation_id, **extra)
