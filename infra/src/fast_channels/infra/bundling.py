"""Docker-free bundling of uv workspace packages for Lambda and AgentCore (Python 3.14, arm64), with a Docker
fallback: one pipeline for the functions code asset and the agent code zip."""

import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import jsii
from aws_cdk import AssetHashType, BundlingOptions, DockerImage, ILocalBundling
from aws_cdk import aws_lambda as lambda_

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
UV_IMAGE: Final = "ghcr.io/astral-sh/uv:python3.14-bookworm"
"""Image of the Docker fallback, used when ``uv`` is not on the build host's PATH."""
_PLATFORM: Final = "aarch64-manylinux2014"
_PYTHON: Final = "3.14"
_EXPORT: Final = ("export", "--frozen", "--no-dev", "--no-editable", "--no-hashes", "--quiet")
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
)


@jsii.implements(ILocalBundling)
class _UvBundling:
    """Export a workspace package's locked dependency closure and install it for the target platform."""

    def __init__(self, package: str, *, extra_files: Mapping[str, str], normalize_modes: bool) -> None:
        self._package = package
        self._extra_files = dict(extra_files)
        self._normalize_modes = normalize_modes

    # jsii passes ``options`` positionally.
    def try_bundle(self, output_dir: str, options: BundlingOptions) -> bool:
        uv = shutil.which("uv")
        if uv is None:
            return False
        with tempfile.TemporaryDirectory() as work:
            requirements = Path(work) / "requirements.txt"
            run = [uv, *_EXPORT, "--package", self._package, "--output-file", str(requirements)]
            subprocess.run(run, check=True, cwd=REPO_ROOT)  # noqa: S603 - fixed argv built here, no shell
            refresh = _refresh_workspace_packages(requirements)
            install = [uv, *_INSTALL, *refresh, "--target", output_dir, "--requirement", str(requirements)]
            subprocess.run(install, check=True, cwd=REPO_ROOT)  # noqa: S603 - fixed argv built here, no shell
        for source, name in self._extra_files.items():
            shutil.copyfile(REPO_ROOT / source, Path(output_dir) / name)
        if self._normalize_modes:
            normalize_modes(Path(output_dir))
        return True


def _refresh_workspace_packages(requirements: Path) -> list[str]:
    """``--refresh-package`` for every workspace package (``./<dir>`` lines) in the export.

    uv reuses its cached build of a local package until that package's pyproject.toml changes, so without this a
    bundle silently keeps stale sources (observed); registry wheels stay cached.
    """
    directories = [line.strip() for line in requirements.read_text().splitlines() if line.startswith("./")]
    names = [tomllib.loads((REPO_ROOT / d / "pyproject.toml").read_text())["project"]["name"] for d in directories]
    return [arg for name in names for arg in ("--refresh-package", name)]


def normalize_modes(root: Path) -> None:
    """Directories and executables 755, files 644, whatever the build host's umask (agentcore.md §4.1 limits)."""
    for directory, _, files in os.walk(root):
        Path(directory).chmod(0o755)
        for name in files:
            path = Path(directory, name)
            path.chmod(0o755 if path.stat().st_mode & 0o100 else 0o644)


def uv_bundling(
    package: str, *, extra_files: Mapping[str, str] | None = None, normalize_modes: bool = False
) -> BundlingOptions:
    """Bundling of ``package`` and its locked dependencies into an asset's root.

    Args:
        package: uv workspace package name, e.g. ``fast-channels-functions``.
        extra_files: repository-relative files to copy into the root, by their name there.
        normalize_modes: set every mode to 755/644 (the AgentCore Runtime rejects other modes).
    """
    extra = dict(extra_files or {})
    export = " ".join(_EXPORT)
    install = " ".join(_INSTALL)
    steps = [
        f"uv {export} --package {package} --output-file /tmp/r.txt",
        f"uv {install} --target /asset-output -r /tmp/r.txt",
        *(f"cp {source} /asset-output/{name}" for source, name in extra.items()),
        *(["chmod -R u=rwX,go=rX /asset-output"] if normalize_modes else []),
    ]
    return BundlingOptions(
        image=DockerImage.from_registry(UV_IMAGE),
        local=_UvBundling(package, extra_files=extra, normalize_modes=normalize_modes),  # type: ignore[arg-type]
        # The container runs as the host user, without a writable HOME; its cache starts empty.
        environment={"UV_CACHE_DIR": "/tmp/uv-cache"},  # noqa: S108 - inside the throwaway build container
        command=["bash", "-c", " && ".join(steps)],
    )


def workspace_code(package: str) -> lambda_.Code:
    """Lambda code asset containing ``package`` and its locked dependencies (content-hashed on output)."""
    return lambda_.Code.from_asset(
        str(REPO_ROOT),
        # The hash of the installed bundle: it changes exactly when code or locked dependencies do. No ``exclude``:
        # CDK applies it to the bundle's fingerprint too (``["**"]`` made the hash constant and froze deployments).
        asset_hash_type=AssetHashType.OUTPUT,
        bundling=uv_bundling(package),
    )
