"""AgentCore Runtime code zip of the ``fast-channels-agent`` package (agent/README.md, "Code zip").

The zip root holds the locked dependency closure installed for the Runtime's managed Python 3.14 on arm64, plus the
two-line ``agent/main.py`` shim the Runtime starts (``opentelemetry-instrument main.py``), every mode 755/644. Built
by :func:`fast_channels.infra.bundling.uv_bundling`: locally with uv, falling back to the uv container image.
"""

from typing import TYPE_CHECKING, Final

from aws_cdk import AssetHashType, BundlingOptions
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_assets as s3_assets

from fast_channels.infra.bundling import REPO_ROOT, uv_bundling

if TYPE_CHECKING:
    from constructs import Construct

AGENT_PACKAGE: Final = "fast-channels-agent"
ENTRY_POINT: Final = ("opentelemetry-instrument", "main.py")
"""ADOT auto-instrumentation around the shim; the managed runtime copes with the build host's shebang
(agentcore.md §4.1)."""
SHIM: Final = "agent/main.py"
"""The Runtime's entry module, copied to the zip root as ``main.py``."""


def agent_bundling() -> BundlingOptions:
    """How the code zip is built: the agent's locked closure, the shim at the root, modes normalized."""
    return uv_bundling(AGENT_PACKAGE, extra_files={SHIM: "main.py"}, normalize_modes=True)


def agent_asset(scope: Construct, id: str) -> s3_assets.Asset:
    """The scheduler agent's code zip as a file asset (content-hashed on the installed bundle)."""
    return s3_assets.Asset(
        scope, id, path=str(REPO_ROOT), asset_hash_type=AssetHashType.OUTPUT, bundling=agent_bundling()
    )


def agent_artifact(code: s3_assets.Asset) -> agentcore.AgentRuntimeArtifact:
    """The Runtime artifact of the code zip ``code`` (:func:`agent_asset`).

    The asset is created here, not by ``AgentRuntimeArtifact.from_code_asset``, whose asset only exists once the
    Runtime renders its properties: its object key is needed at construction to scope the role's reads.
    """
    location = s3.Location(bucket_name=code.s3_bucket_name, object_key=code.s3_object_key)
    return agentcore.AgentRuntimeArtifact.from_s3(location, agentcore.AgentCoreRuntime.PYTHON_3_14, list(ENTRY_POINT))
