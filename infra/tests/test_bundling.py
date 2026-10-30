"""Real (uv) bundling of the shared Lambda code asset; the other tests skip bundling."""

import marshal
import sys
from pathlib import Path

import pytest
from aws_cdk import App, FileSystem, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Template

from fast_channels.infra.bundling import LAMBDA_TASK_ROOT, REPO_ROOT, compile_bytecode, workspace_code

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


def test_compiled_bytecode_is_deterministic_unchecked_and_names_the_task_root(tmp_path: Path) -> None:
    """``compile_bytecode`` (the Pds stack's functions): unchecked hash-based ``.pyc`` files, the same bytes from
    the same sources, whose code objects name ``/var/task``."""
    source = tmp_path / "pkg" / "module.py"
    source.parent.mkdir()
    source.write_text("CONSTANT = frozenset({'a', 'b', 'c', 'd'})\n\ndef f():\n    return 'x' in CONSTANT\n")
    compiled = source.parent / "__pycache__" / f"module.{sys.implementation.cache_tag}.pyc"

    compile_bytecode(tmp_path)
    first = compiled.read_bytes()
    compiled.unlink()
    compile_bytecode(tmp_path)

    assert compiled.read_bytes() == first
    assert int.from_bytes(first[4:8], "little") == 0b01  # hash-based, the source is never checked
    code = marshal.loads(first[16:])
    assert code.co_filename == f"{LAMBDA_TASK_ROOT}/pkg/module.py"
