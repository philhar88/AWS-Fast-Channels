"""Find a FAST Channels deployment's CloudFormation stacks and read their outputs.

Shared by the operator scripts (``seed_sample_content.py``, ``trace.py``, ``cleanup.py``) and ``tests/e2e``. A
deployment is identified by its prefix: ``--prefix``, by default the ``fast-channels.prefix`` context value in
cdk.json. Its stacks are ``FastChannels[-<prefix>]-{Data,Media,Operator}`` in the region the CDK CLI deploys to:
``AWS_REGION``, else ``AWS_DEFAULT_REGION``, else the profile's (boto3 alone ignores ``AWS_REGION``). Output keys
are the contract in :data:`OUTPUT_KEYS`.

Deliberately free of ``aws_cdk`` imports: loading the CDK starts a Node.js process, which a CLI does not need.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn

import boto3
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_cloudformation import CloudFormationClient

REPO: Final = Path(__file__).resolve().parents[1]
CDK_JSON: Final = REPO / "cdk.json"
DEFAULT_PREFIX: Final = "fast"
"""``fast_channels.infra.stage.DEFAULT_PREFIX``: the deployment whose stacks carry no prefix."""


class Plane(StrEnum):
    """The stacks of a deployment that scripts read (the Edge and AccountBaseline stacks have no script outputs)."""

    DATA = "Data"
    MEDIA = "Media"
    OPERATOR = "Operator"


OUTPUT_KEYS: Final[dict[Plane, tuple[str, ...]]] = {
    Plane.DATA: ("IngestBucketName", "MediaBucketName", "LogsBucketName", "LibraryTableName", "CatalogTableName"),
    Plane.MEDIA: (
        "CdnBaseUrl",
        "ContentBaseUrl",
        "EventBusName",
        "IngestStateMachineArn",
        "LadderProvisionerArn",
        "SourceLocationName",
        "DefaultLadderVersion",
        "Channels",
        "DashboardUrl",
    ),
    Plane.OPERATOR: (
        "ConsoleUrl",
        "UserPoolId",
        "ConsoleClientId",
        "CognitoDomain",
        "RuntimeArn",
        "RuntimeInvokeUrl",
        "GatewayUrl",
        "ToolFunctions",
    ),
}
"""Outputs every deployment has. Two more exist only with what they describe: ``MemoryId`` (Data) with the agent
plane, and ``AutopilotClientId`` (Operator) when an IaC channel has an ``autopilot_cron``."""


class StackError(LookupError):
    """A stack of the deployment, or one of its outputs, does not exist."""


def configured_prefix(cdk_json: Path = CDK_JSON) -> str:
    """The deployment prefix in cdk.json (context ``fast-channels.prefix``)."""
    context = json.loads(cdk_json.read_text())["context"]
    return str(context.get("fast-channels", {}).get("prefix", DEFAULT_PREFIX))


def stage_id(prefix: str) -> str:
    """Stack name prefix of a deployment, as ``fast_channels.infra.stage.stage_id`` derives it."""
    return "FastChannels" if prefix == DEFAULT_PREFIX else f"FastChannels-{prefix}"


def cdk_session() -> boto3.Session:
    """An AWS session in the region the CDK CLI would deploy to."""
    return boto3.Session(region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))


def run(main: Callable[[], int]) -> NoReturn:
    """Exit with ``main()``'s status. A missing stack or output, missing credentials or a refused AWS call end the
    script with the error message instead of a traceback."""
    try:
        sys.exit(main())
    except (StackError, BotoCoreError, ClientError) as error:
        sys.exit(f"error: {error}")


def add_prefix_argument(parser: argparse.ArgumentParser) -> None:
    """The ``--prefix`` option every script shares."""
    parser.add_argument(
        "--prefix",
        help=f"deployment prefix (default: fast-channels.prefix in cdk.json, currently {configured_prefix()!r})",
    )


@dataclass
class Stage:
    """One deployment: its stack names, statuses and outputs (read once per stack and cached)."""

    prefix: str
    session: boto3.Session = field(default_factory=cdk_session)
    _outputs: dict[Plane, dict[str, str]] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def of(cls, prefix: str | None = None, *, session: boto3.Session | None = None) -> Stage:
        """The deployment ``prefix`` (default: the cdk.json prefix) in the session's account and region."""
        return cls(prefix or configured_prefix(), session or cdk_session())

    @property
    def region(self) -> str:
        if not (region := self.session.region_name):
            raise StackError("no AWS region configured: set AWS_REGION (or a profile region)")
        return region

    @cached_property
    def cloudformation(self) -> CloudFormationClient:
        return self.session.client("cloudformation", region_name=self.region)

    def stack_name(self, plane: Plane) -> str:
        return f"{stage_id(self.prefix)}-{plane}"

    def status(self, plane: Plane) -> str | None:
        """``StackStatus`` of the plane's stack, or None when it does not exist."""
        stack = self._describe(plane)
        return None if stack is None else str(stack["StackStatus"])

    def outputs(self, plane: Plane) -> dict[str, str]:
        """Every output of the plane's stack; raises StackError when the stack does not exist."""
        if plane not in self._outputs:
            stack = self._describe(plane)
            if stack is None:
                raise StackError(
                    f"stack {self.stack_name(plane)} does not exist in {self.region}: deploy the stage, or pass the "
                    "--prefix of a deployed one"
                )
            self._outputs[plane] = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
        return self._outputs[plane]

    def output(self, plane: Plane, key: str) -> str:
        """One output value; raises StackError naming the outputs the stack does have."""
        outputs = self.outputs(plane)
        if key not in outputs:
            raise StackError(
                f"stack {self.stack_name(plane)} has no output {key} (it has: {', '.join(sorted(outputs)) or 'none'})"
            )
        return outputs[key]

    def json_output(self, plane: Plane, key: str) -> Any:
        """An output holding JSON (``Channels``, ``ToolFunctions``), decoded."""
        value = self.output(plane, key)
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise StackError(f"output {key} of {self.stack_name(plane)} is not JSON: {error}") from None

    def _describe(self, plane: Plane) -> dict[str, Any] | None:
        try:
            [stack] = self.cloudformation.describe_stacks(StackName=self.stack_name(plane))["Stacks"]
        except ClientError as error:
            if "does not exist" in error.response.get("Error", {}).get("Message", ""):
                return None
            raise
        return dict(stack)
