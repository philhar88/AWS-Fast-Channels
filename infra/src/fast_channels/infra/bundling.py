"""Docker-free bundling of uv workspace packages for Lambda and AgentCore (Python 3.14, arm64), with a Docker
fallback: one pipeline for the functions code asset and the agent code zip."""

import os
import shutil
import subprocess
import sys
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


LAMBDA_TASK_ROOT: Final = "/var/task"
"""Where Lambda unpacks a function's code: the path compiled code objects name."""
_COMPILE: Final = ("-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash")
_DETERMINISTIC: Final = {"PYTHONHASHSEED": "0", "SOURCE_DATE_EPOCH": "0"}
"""Fixed set iteration order (frozenset constants are marshalled in it) and no timestamps: the same sources compile to
the same bytes, so the asset hash (of the output) changes only with the code."""


@jsii.implements(ILocalBundling)
class _UvBundling:
    """Export a workspace package's locked dependency closure and install it for the target platform."""

    def __init__(
        self, package: str, *, extra_files: Mapping[str, str], normalize_modes: bool, compile_bytecode: bool
    ) -> None:
        self._package = package
        self._extra_files = dict(extra_files)
        self._normalize_modes = normalize_modes
        self._compile_bytecode = compile_bytecode

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
        if self._compile_bytecode:
            compile_bytecode(Path(output_dir))
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


def compile_bytecode(root: Path) -> None:
    """Precompile every module under ``root`` for the runtime's Python, as unchecked hash-based ``.pyc`` files that
    name their source under :data:`LAMBDA_TASK_ROOT`, deterministically (:data:`_DETERMINISTIC`).

    Lambda's code directory is read-only, so a bundle without bytecode compiles every module it imports on each cold
    start: the PDS's imports took 1,134 ms without ``.pyc`` and 168 ms with them (locally), and its functions logged
    their first client 1.5-2.1 s into init.

    Raises:
        RuntimeError: the build runs on another Python than the runtime's (its bytecode would be ignored).
    """
    if f"{sys.version_info.major}.{sys.version_info.minor}" != _PYTHON:
        raise RuntimeError(f"bytecode for Python {_PYTHON} needs that Python, not {sys.version.split()[0]}")
    command = [sys.executable, *_COMPILE, "-s", str(root), "-p", LAMBDA_TASK_ROOT, str(root)]
    subprocess.run(command, check=True, env={**os.environ, **_DETERMINISTIC})  # noqa: S603 - fixed argv, no shell


def normalize_modes(root: Path) -> None:
    """Directories and executables 755, files 644, whatever the build host's umask (agentcore.md §4.1 limits)."""
    for directory, _, files in os.walk(root):
        Path(directory).chmod(0o755)
        for name in files:
            path = Path(directory, name)
            path.chmod(0o755 if path.stat().st_mode & 0o100 else 0o644)


def uv_bundling(
    package: str,
    *,
    extra_files: Mapping[str, str] | None = None,
    normalize_modes: bool = False,
    compile_bytecode: bool = False,
) -> BundlingOptions:
    """Bundling of ``package`` and its locked dependencies into an asset's root.

    Args:
        package: uv workspace package name, e.g. ``fast-channels-functions``.
        extra_files: repository-relative files to copy into the root, by their name there.
        normalize_modes: set every mode to 755/644 (the AgentCore Runtime rejects other modes).
        compile_bytecode: precompile every module (:func:`compile_bytecode`), for functions whose cold start matters.
    """
    extra = dict(extra_files or {})
    export = " ".join(_EXPORT)
    install = " ".join(_INSTALL)
    steps = [
        f"uv {export} --package {package} --output-file /tmp/r.txt",
        f"uv {install} --target /asset-output -r /tmp/r.txt",
        *(f"cp {source} /asset-output/{name}" for source, name in extra.items()),
        *(
            [
                " ".join(
                    [
                        *(f"{k}={v}" for k, v in _DETERMINISTIC.items()),
                        "python",
                        *_COMPILE,
                        f"-s /asset-output -p {LAMBDA_TASK_ROOT} /asset-output",
                    ]
                )
            ]
            if compile_bytecode
            else []
        ),
        *(["chmod -R u=rwX,go=rX /asset-output"] if normalize_modes else []),
    ]
    return BundlingOptions(
        image=DockerImage.from_registry(UV_IMAGE),
        local=_UvBundling(  # type: ignore[arg-type]
            package, extra_files=extra, normalize_modes=normalize_modes, compile_bytecode=compile_bytecode
        ),
        # The container runs as the host user, without a writable HOME; its cache starts empty.
        environment={"UV_CACHE_DIR": "/tmp/uv-cache"},  # noqa: S108 - inside the throwaway build container
        command=["bash", "-c", " && ".join(steps)],
    )


def workspace_code(package: str, *, compile_bytecode: bool = False) -> lambda_.Code:
    """Lambda code asset containing ``package`` and its locked dependencies (content-hashed on output), with
    precompiled bytecode if ``compile_bytecode``."""
    return lambda_.Code.from_asset(
        str(REPO_ROOT),
        # The hash of the installed bundle: it changes exactly when code or locked dependencies do. No ``exclude``:
        # CDK applies it to the bundle's fingerprint too (``["**"]`` made the hash constant and froze deployments).
        asset_hash_type=AssetHashType.OUTPUT,
        bundling=uv_bundling(package, compile_bytecode=compile_bytecode),
    )
