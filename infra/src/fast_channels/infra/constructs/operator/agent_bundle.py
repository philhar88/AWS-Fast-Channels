"""AgentCore Runtime code zip of the ``fast-channels-agent`` package (agent/README.md, "Code zip").

The zip root holds the locked dependency closure installed for the Runtime's managed Python 3.14 on arm64, plus the
two-line ``agent/main.py`` shim the Runtime starts (``opentelemetry-instrument main.py``). Built locally with uv
(no Docker), falling back to the uv container image.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

import jsii
from aws_cdk import AssetHashType, BundlingOptions, DockerImage, ILocalBundling
from aws_cdk import aws_bedrockagentcore as agentcore

from fast_channels.infra.bundling import REPO_ROOT

AGENT_PACKAGE: Final = "fast-channels-agent"
ENTRY_POINT: Final = ("opentelemetry-instrument", "main.py")
"""ADOT auto-instrumentation around the shim; the managed runtime copes with the build host's shebang
(agentcore.md §4.1)."""
_SHIM: Final = Path("agent/main.py")
_PLATFORM: Final = "aarch64-manylinux2014"
_PYTHON: Final = "3.14"
_EXPORT: Final = (
    "export",
    "--package",
    AGENT_PACKAGE,
    "--frozen",
    "--no-dev",
    "--no-editable",
    "--no-hashes",
    "--quiet",
    "--output-file",
)
_INSTALL: Final = (
    "pip",
    "install",
    "--no-sources",
    "--no-installer-metadata",
    "--no-compile-bytecode",
    "--quiet",
    "--python-platform",
    _PLATFORM,
    "--python",
    _PYTHON,
    # uv reuses a cached wheel of a workspace package until a cache key changes (agent/README.md).
    "--refresh-package",
    AGENT_PACKAGE,
)


@jsii.implements(ILocalBundling)
class _UvAgentBundling:
    """Install the agent's locked closure for the Runtime into the asset output directory."""

    # jsii passes ``options`` positionally.
    def try_bundle(self, output_dir: str, options: BundlingOptions) -> bool:
        uv = shutil.which("uv")
        if uv is None:
            return False
        with tempfile.TemporaryDirectory() as work:
            requirements = str(Path(work) / "requirements.txt")
            subprocess.run([uv, *_EXPORT, requirements], check=True, cwd=REPO_ROOT)  # noqa: S603 - fixed argv
            subprocess.run(  # noqa: S603 - fixed argv built here, no shell
                [uv, *_INSTALL, "--target", output_dir, "--requirement", requirements], check=True, cwd=REPO_ROOT
            )
        shutil.copyfile(REPO_ROOT / _SHIM, Path(output_dir) / _SHIM.name)
        _normalize_modes(Path(output_dir))
        return True


def _normalize_modes(root: Path) -> None:
    """Directories and executables 755, files 644, whatever the build host's umask (agentcore.md §4.1 limits)."""
    for directory, _, files in os.walk(root):
        Path(directory).chmod(0o755)
        for name in files:
            path = Path(directory, name)
            path.chmod(0o755 if path.stat().st_mode & 0o100 else 0o644)


def agent_code() -> agentcore.AgentRuntimeArtifact:
    """The scheduler agent's code-zip artifact (content-hashed on the installed bundle)."""
    export = " ".join(_EXPORT)
    install = " ".join(_INSTALL)
    return agentcore.AgentRuntimeArtifact.from_code_asset(
        path=str(REPO_ROOT),
        runtime=agentcore.AgentCoreRuntime.PYTHON_3_14,
        entrypoint=list(ENTRY_POINT),
        asset_hash_type=AssetHashType.OUTPUT,
        bundling=BundlingOptions(
            image=DockerImage.from_registry("ghcr.io/astral-sh/uv:python3.14-bookworm"),
            local=_UvAgentBundling(),  # type: ignore[arg-type]
            environment={"UV_CACHE_DIR": "/tmp/uv-cache"},  # noqa: S108 - inside the throwaway build container
            command=[
                "bash",
                "-c",
                f"uv {export} /tmp/r.txt && uv {install} --target /asset-output -r /tmp/r.txt && "
                f"cp {_SHIM} /asset-output/{_SHIM.name} && chmod -R u=rwX,go=rX /asset-output",
            ],
        ),
    )
