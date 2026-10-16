"""Docker-free Lambda bundling of uv workspace packages, with a Docker fallback."""

import shutil
import subprocess
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
    def try_bundle(self, output_dir: str, options: BundlingOptions) -> bool:  # noqa: ARG002
        uv = shutil.which("uv")
        if uv is None:
            return False
        requirements = Path(output_dir) / "requirements.txt"
        subprocess.run(
            [uv, "export", "--package", self._package, "--frozen", "--no-dev", "--no-editable", "--no-hashes",
             "--quiet", "--output-file", str(requirements)],
            check=True, cwd=REPO_ROOT,
        )
        subprocess.run(
            [uv, "pip", "install", "--no-sources", "--no-installer-metadata", "--no-compile-bytecode", "--quiet",
             "--python-platform", _PLATFORM, "--python", _PYTHON, "--target", output_dir,
             "--requirement", str(requirements)],
            check=True, cwd=REPO_ROOT,
        )
        requirements.unlink()
        return True


def workspace_code(package: str) -> lambda_.Code:
    """Lambda code asset containing ``package`` and its locked dependencies (content-hashed on output)."""
    return lambda_.Code.from_asset(
        str(REPO_ROOT),
        asset_hash_type=AssetHashType.OUTPUT,
        exclude=["**"],
        bundling=BundlingOptions(
            image=DockerImage.from_registry("ghcr.io/astral-sh/uv:python3.14-bookworm"),
            local=_UvBundling(package),  # type: ignore[arg-type]
            command=["bash", "-c", (
                f"uv export --package {package} --frozen --no-dev --no-editable --no-hashes -o /tmp/r.txt && "
                "uv pip install --no-sources --no-installer-metadata --no-compile-bytecode "
                f"--python-platform {_PLATFORM} --python {_PYTHON} --target /asset-output -r /tmp/r.txt"
            )],
        ),
    )
