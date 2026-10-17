"""A ladder version as a MediaConvert job template (SPEC §5.1)."""

import string
from typing import TYPE_CHECKING, Any

from aws_cdk import aws_mediaconvert as mediaconvert
from constructs import Construct

from fast_channels.core.mediaconvert import job_template_props
from fast_channels.core.naming import job_template_name
from fast_channels.core.tags import TAG_OWNER, Owner

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder


class EncodingLadder(Construct):
    """The MediaConvert job template of one ladder version.

    The settings come from ``core.mediaconvert.compile_job_settings``, without Inputs or Destination: every job
    sends those as sparse overrides, which MediaConvert deep-merges into the template (ARCHITECTURE-SPEC App. A).
    The name ``<prefix>-<ladder id>_<fingerprint>`` (``core.naming.job_template_name``) is create-only, so a new
    ladder version is a new template. ``Category`` is the deployment prefix, and ``fast:owner=iac`` keeps the
    runtime-resource reaper away from it.

    Args:
        ladder: the validated ladder.
        prefix: deployment prefix.
        queue_arn: on-demand queue of the jobs (:attr:`IngestPipeline.queue_arn`).

    Attributes:
        ladder: the ladder.
        version: ``<id>@<fingerprint>``.
        template_name: the job template name (a plain string).
        job_template: the ``CfnJobTemplate``.
    """

    def __init__(self, scope: Construct, id: str, *, ladder: Ladder, prefix: str, queue_arn: str) -> None:
        super().__init__(scope, id)
        self.ladder = ladder
        self.version = ladder.version
        self.template_name = job_template_name(prefix, ladder.version)
        props = job_template_props(ladder, name=self.template_name, queue_arn=queue_arn, category=prefix)
        self.job_template = mediaconvert.CfnJobTemplate(
            self,
            "Resource",
            name=props["Name"],
            description=props["Description"],
            category=props["Category"],
            queue=props["Queue"],
            priority=props["Priority"],
            status_update_interval=props["StatusUpdateInterval"],
            settings_json=cfn_settings(props["Settings"]),
            tags={**props["Tags"], TAG_OWNER: Owner.IAC.value},
        )


def cfn_settings(settings: Any) -> Any:
    """Job settings (API shape) with the member names CloudFormation's ``AWS::MediaConvert::JobTemplate`` handler
    expects in ``SettingsJson``.

    The handler binds ``SettingsJson`` to the Java SDK v2 builders, whose bean names lower-case a leading acronym
    after its first letter: ``IFrameOnlyManifest`` is rejected as unrecognized and must be ``IframeOnlyManifest``
    (observed in a deployment for ``CmfcSettings``; CreateJobTemplate takes the API names). It is the only such
    member in the compiled ladders (``OutputSettings.HlsSettings.IFrameOnlyManifest``); a capital after a digit or
    inside a name (``M3u8Settings``, ``Scte35Source``, ``GopBReference``) keeps its spelling.
    """
    match settings:
        case dict():
            return {_java_bean_name(k): cfn_settings(v) for k, v in settings.items()}
        case list():
            return [cfn_settings(v) for v in settings]
        case _:
            return settings


def _java_bean_name(member: str) -> str:
    acronym = len(member) - len(member.lstrip(string.ascii_uppercase))
    return member if acronym < 2 else member[0] + member[1:acronym].lower() + member[acronym:]
