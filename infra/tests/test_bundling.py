"""Real (uv) bundling of the shared Lambda code asset; the other tests skip bundling."""

from pathlib import Path

import pytest
from aws_cdk import App, FileSystem, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Template

from fast_channels.infra.bundling import REPO_ROOT, workspace_code

PACKAGES = (REPO_ROOT / "core/src", REPO_ROOT / "functions/src")


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    outdir = tmp_path_factory.mktemp("cdk.out")
    stack = Stack(App(outdir=str(outdir)), "Bundle")
    lambda_.Function(
        stack,
        "Fn",
        runtime=lambda_.Runtime.PYTHON_3_14,
        handler="index.handler",
        code=workspace_code("fast-channels-functions"),
    )
    [function] = Template.from_stack(stack).find_resources("AWS::Lambda::Function").values()
    asset_hash = str(function["Properties"]["Code"]["S3Key"]).removesuffix(".zip")
    return outdir / f"asset.{asset_hash}", asset_hash


def test_bundle_holds_the_current_workspace_sources(bundle: tuple[Path, str]) -> None:
    directory, _ = bundle
    sources = [f for root in PACKAGES for f in root.rglob("*") if f.is_file() and "__pycache__" not in f.parts]

    assert sources
    for source in sources:  # uv reuses stale builds of local packages unless refreshed
        relative = source.relative_to(next(root for root in PACKAGES if source.is_relative_to(root)))
        assert (directory / relative).read_bytes() == source.read_bytes(), relative
    assert (directory / "fast_channels/core/ladders/standard.toml").is_file()


def test_bundle_has_arm64_wheels_and_its_own_sdk(bundle: tuple[Path, str]) -> None:
    directory, _ = bundle

    assert list((directory / "pydantic_core").glob("*aarch64-linux-gnu.so"))
    assert (directory / "aws_lambda_powertools").is_dir()
    # The functions declare boto3, so they run on the locked SDK, not the runtime's.
    assert (directory / "boto3").is_dir() and (directory / "botocore").is_dir()


def test_asset_hash_is_the_fingerprint_of_the_bundle(bundle: tuple[Path, str]) -> None:
    directory, asset_hash = bundle

    assert FileSystem.fingerprint(str(directory)) == asset_hash
