"""Channel Assembly and MediaTailor ad insertion (SPEC §5.3).

- :class:`ChannelAssemblyOrigin` (``origin``): the media bucket as an ``S3_SIGV4`` source location.
- :class:`MediaTailorLogs` (``ad_insertion``): one vended-logs destination shared by every playback configuration.
- :class:`AdInsertion` (``ad_insertion``): a playback configuration (SSAI only, ``STITCHED_ONLY``) with its three
  vended-log deliveries; :func:`playback_configuration_template` is what agent-created channels reuse
  (``MediaExports``).
- :class:`LinearChannel` (``channel``): an IaC channel with its own ``AdInsertion``, SigV4-only channel policy and
  lifecycle.
- :class:`AsRunLogging` (``as_run``): retention on the account's as-run log and an ``AIRED`` reconciler for the EPG.
- :class:`RuntimeResourceReaper` (``reaper``): deletes runtime resources (agent channels, VOD sources, job
  templates) on stack deletion, before the containers they would block.

Resource shapes come from :mod:`fast_channels.core.specs`, which the agent tools use for the same resources.
"""

from fast_channels.infra.constructs.channel_assembly.ad_insertion import (
    VENDED_LOG_TYPES,
    AdInsertion,
    MediaTailorLogs,
    playback_configuration_template,
)
from fast_channels.infra.constructs.channel_assembly.as_run import AS_RUN_LOG_GROUP, AsRunLogging
from fast_channels.infra.constructs.channel_assembly.channel import LinearChannel
from fast_channels.infra.constructs.channel_assembly.origin import ChannelAssemblyOrigin
from fast_channels.infra.constructs.channel_assembly.reaper import RuntimeResourceReaper
from fast_channels.infra.constructs.storage import GSI1

__all__ = [
    "AS_RUN_LOG_GROUP",
    "GSI1",
    "VENDED_LOG_TYPES",
    "AdInsertion",
    "AsRunLogging",
    "ChannelAssemblyOrigin",
    "LinearChannel",
    "MediaTailorLogs",
    "RuntimeResourceReaper",
    "playback_configuration_template",
]
