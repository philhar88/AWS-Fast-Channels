"""Docker-free Lambda bundling of uv workspace packages, with a Docker fallback."""

import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Final

import jsii
from aws_cdk import AssetHashType, BundlingOptions, DockerImage, ILocalBundling
from aws_cdk import aws_lambda as lambda_

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
_PLATFORM: Final = "aarch64-manylinux2014"
_PYTHON: Final = "3.14"


@jsii.implements(ILocalBundling)
class _UvBundling:
    """Export a workspace package's locked dependency closure and install it for Lambda (arm64)."""

    def __init__(self, package: str) -> None:
        self._package = package

    # jsii passes ``options`` positionally.
    def try_bundle(self, output_dir: str, options: BundlingOptions) -> bool:
        uv = shutil.which("uv")
        if uv is None:
            return False
        requirements = Path(output_dir) / "requirements.txt"
        subprocess.run(  # noqa: S603 - fixed argv built here, no shell
            [
                uv,
                "export",
                "--package",
                self._package,
                "--frozen",
                "--no-dev",
                "--no-editable",
                "--no-hashes",
                "--quiet",
                "--output-file",
                str(requirements),
            ],
            check=True,
            cwd=REPO_ROOT,
        )
        subprocess.run(  # noqa: S603 - fixed argv built here, no shell
            [
                uv,
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
                "--target",
                output_dir,
                *_refresh_workspace_packages(requirements),
                "--requirement",
                str(requirements),
            ],
            check=True,
            cwd=REPO_ROOT,
        )
        requirements.unlink()
        return True


def _refresh_workspace_packages(requirements: Path) -> list[str]:
    """``--refresh-package`` for every workspace package (``./<dir>`` lines) in the export.

    uv reuses its cached build of a local package until that package's pyproject.toml changes, so without this a
    bundle silently keeps stale sources (observed); registry wheels stay cached.
    """
    directories = [line.strip() for line in requirements.read_text().splitlines() if line.startswith("./")]
    names = [tomllib.loads((REPO_ROOT / d / "pyproject.toml").read_text())["project"]["name"] for d in directories]
    return [arg for name in names for arg in ("--refresh-package", name)]


def workspace_code(package: str) -> lambda_.Code:
    """Lambda code asset containing ``package`` and its locked dependencies (content-hashed on output)."""
    return lambda_.Code.from_asset(
        str(REPO_ROOT),
        # The hash of the installed bundle: it changes exactly when code or locked dependencies do. No ``exclude``:
        # CDK applies it to the bundle's fingerprint too (``["**"]`` made the hash constant and froze deployments).
        asset_hash_type=AssetHashType.OUTPUT,
        bundling=BundlingOptions(
            image=DockerImage.from_registry("ghcr.io/astral-sh/uv:python3.14-bookworm"),
            local=_UvBundling(package),  # type: ignore[arg-type]
            # The container runs as the host user, without a writable HOME; its cache starts empty.
            environment={"UV_CACHE_DIR": "/tmp/uv-cache"},  # noqa: S108 - inside the throwaway build container
            command=[
                "bash",
                "-c",
                (
                    f"uv export --package {package} --frozen --no-dev --no-editable --no-hashes -o /tmp/r.txt && "
                    "uv pip install --no-sources --no-installer-metadata --no-compile-bytecode "
                    f"--python-platform {_PLATFORM} --python {_PYTHON} --target /asset-output -r /tmp/r.txt"
                ),
            ],
        ),
    )
