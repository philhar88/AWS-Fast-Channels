"""Per-task application inference profiles and the invoke grant of a task's role.

An application inference profile (AIP) copied from a task's system inference profile routes exactly like it, but its
``ModelId`` in the ``AWS/Bedrock`` metrics is the profile's own id and it carries the task's cost allocation tag: the
per-task cost and metrics primitive. A task's role may invoke only its own profile: the profile, plus the foundation
model in the Regions the profile routes to, only through that profile (``bedrock:InferenceProfileArn``). Measured
before this module was built: without the foundation-model statement Converse is denied, the system profile needs no
statement of its own, and a role granted one profile is denied another and the system profile itself.
"""

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from aws_cdk import CfnTag, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from constructs import Construct, IConstruct

from fast_channels.infra.exports import TaskProfile

if TYPE_CHECKING:
    from fast_channels.core.model_registry import TaskSettings

TASK_TAG: Final = "fast:task"
"""Tag of each profile: activate it as a cost allocation tag to see per-task spend in Cost Explorer and Budgets."""
INVOKE_ACTIONS: Final = ("bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream")
_PROFILE_PREFIXES: Final = frozenset({"global", "us", "eu", "apac", "ca", "jp", "au", "us-gov"})
_NAME_MAX: Final = 64


def profile_name(prefix: str, task: str) -> str:
    """``<prefix>-<task>`` with every run of ``.``, ``_`` or ``-`` as one ``-`` (profile names allow one separator
    between alphanumerics, at most 64 characters)."""
    name = re.sub(r"[._-]+", "-", f"{prefix}-{task}")
    if len(name) > _NAME_MAX:
        raise ValueError(f"inference profile name {name!r} is longer than {_NAME_MAX} characters")
    return name


def foundation_model(model: str) -> str:
    """The foundation model id of ``model``, without its ``<geo>.`` or ``global.`` inference-profile prefix."""
    prefix, _, family = model.partition(".")
    return family if prefix in _PROFILE_PREFIXES else model


def source_arn(scope: IConstruct, model: str) -> str:
    """ARN the profile copies: the system inference profile ``<geo>.<model>`` or ``global.<model>`` in the stack's
    Region and account, or a foundation model in the stack's Region."""
    stack = Stack.of(scope)
    prefix, _, _ = model.partition(".")
    if prefix in _PROFILE_PREFIXES:
        return f"arn:{stack.partition}:bedrock:{stack.region}:{stack.account}:inference-profile/{model}"
    return f"arn:{stack.partition}:bedrock:{stack.region}::foundation-model/{model}"


def task_invoke_statements(profile: TaskProfile, *, partition: str) -> list[iam.PolicyStatement]:
    """Invoke through ``profile`` only: the profile itself; the foundation model in any Region of its geography
    (and, for ``global.``, the Region-less global ARN); both only through the profile."""
    through_profile = {"bedrock:InferenceProfileArn": profile.arn}
    models = [f"arn:{partition}:bedrock:*::foundation-model/{foundation_model(profile.model)}"]
    if profile.model.startswith("global."):
        models.append(f"arn:{partition}:bedrock:::foundation-model/{foundation_model(profile.model)}")
    return [
        iam.PolicyStatement(actions=list(INVOKE_ACTIONS), resources=[profile.arn]),
        iam.PolicyStatement(
            actions=list(INVOKE_ACTIONS), resources=models, conditions={"StringEquals": through_profile}
        ),
    ]


class TaskProfiles(Construct):
    """One application inference profile per registry task, named ``<prefix>-<task>`` and tagged ``fast:task``.

    Changing a task's model replaces its profile (a new id): the registry entry and the task's role follow in the
    same deployment.

    Args:
        prefix: deployment prefix.
        tasks: registry entries by task name, with ``{geo}`` resolved.

    Attributes:
        profiles: each task's profile (:class:`TaskProfile`).
        resources: each task's ``AWS::Bedrock::ApplicationInferenceProfile``.
    """

    def __init__(self, scope: Construct, id: str, *, prefix: str, tasks: Mapping[str, TaskSettings]) -> None:
        super().__init__(scope, id)
        self.resources: dict[str, bedrock.CfnApplicationInferenceProfile] = {}
        self.profiles: dict[str, TaskProfile] = {}
        for task, entry in sorted(tasks.items()):
            resource = bedrock.CfnApplicationInferenceProfile(
                self,
                task,
                inference_profile_name=profile_name(prefix, task),
                description=f"FAST Channels {prefix} model task {task}",
                model_source=bedrock.CfnApplicationInferenceProfile.InferenceProfileModelSourceProperty(
                    copy_from=source_arn(self, entry.model)
                ),
                tags=[CfnTag(key=TASK_TAG, value=task)],
            )
            self.resources[task] = resource
            self.profiles[task] = TaskProfile(task=task, model=entry.model, arn=resource.attr_inference_profile_arn)
