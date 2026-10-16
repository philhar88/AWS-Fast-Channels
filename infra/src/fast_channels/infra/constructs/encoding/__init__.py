"""Encoding: ladders as MediaConvert job templates, the ingest pipeline and ladder kits (SPEC §5.1-5.2).

* :class:`EncodingLadder` (``ladder``) - a ladder version's ``CfnJobTemplate``, compiled by ``core.mediaconvert``.
* :class:`IngestPipeline` (``pipeline``) - the MediaConvert queue and role and the Standard JSONata state machine
  Prepare -> Transcode -> Finalize -> Register -> Publish, started by uploads to the ingest bucket, by
  ``Ingest Requested`` events and by StartExecution (tools, kit provisioning).
* :class:`LadderProvisioner` (``kits``) - the state machine that encodes a ladder's kit (filler, break slates,
  house ads and their VAST pod) as IngestPipeline child executions.
* :class:`LadderKit` (``kits``) - a deploy-time custom resource that provisions a built-in ladder's kit.
"""

from fast_channels.infra.constructs.encoding.kits import (
    BRAND_DIR,
    BRAND_PREFIX,
    LadderKit,
    LadderProvisioner,
    brand_digest,
)
from fast_channels.infra.constructs.encoding.ladder import EncodingLadder, cfn_settings
from fast_channels.infra.constructs.encoding.pipeline import (
    EVENT_MAX_AGE,
    EXECUTION_TIMEOUT,
    STEP_TIMEOUT,
    TRANSCODE_TIMEOUT,
    VIDEO_EXTENSIONS,
    IngestPipeline,
)

__all__ = [
    "BRAND_DIR",
    "BRAND_PREFIX",
    "EVENT_MAX_AGE",
    "EXECUTION_TIMEOUT",
    "STEP_TIMEOUT",
    "TRANSCODE_TIMEOUT",
    "VIDEO_EXTENSIONS",
    "EncodingLadder",
    "IngestPipeline",
    "LadderKit",
    "LadderProvisioner",
    "brand_digest",
    "cfn_settings",
]
