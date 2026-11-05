"""The agentic-selling module's part of ``scripts/prebid_guidance.py`` (docs/selling.md): its images and its context.

The module's images are built from pinned upstream code (``vendor/agentic-selling/PIN.toml``) on the stage's arm64
CodeBuild project (the DemandBuild stack's), like the Prebid Server image on its x86 one: each build's context is
uploaded to the guidance's bootstrap bucket and the project builds and pushes ``<repository>:<context digest>``, which
it skips when the repository has that tag. The Selling stack is synthesized with the images' URIs, the catalog table
and CloudFront's origin-facing prefix list (:class:`~fast_channels.core.selling.names.SellingContext`).
"""

import shutil
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from botocore.exceptions import ClientError

from fast_channels.core.selling.names import Image, SellingContext, image_repository, runtime, seller

if TYPE_CHECKING:
    from _demand import DemandStage

REPO: Final = Path(__file__).resolve().parents[1]
VENDOR: Final = REPO / "vendor" / "agentic-selling"
BUILD: Final = REPO / "build" / "agentic-selling"
ORIGIN_FACING: Final = "com.amazonaws.global.cloudfront.origin-facing"
"""CloudFront's managed prefix list of origin-facing addresses, which the seller's load balancer admits."""


def pin() -> dict[str, Any]:
    """``vendor/agentic-selling/PIN.toml``."""
    with (VENDOR / "PIN.toml").open("rb") as file:
        return tomllib.load(file)


def build_variables(image: Image, pinned: Mapping[str, Any]) -> dict[str, str]:
    """What ``build-image.sh`` needs for ``image``: the repository and commit, and the agents' path and base."""
    if image is Image.SELLER:
        source = pinned["seller"]
        return {"IMAGE": image.value, "SOURCE_REPOSITORY": source["repository"], "SOURCE_COMMIT": source["commit"]}
    guidance = pinned["guidance"]
    agent = guidance[image.value]
    return {
        "IMAGE": image.value,
        "SOURCE_REPOSITORY": guidance["repository"],
        "SOURCE_COMMIT": guidance["commit"],
        "SOURCE_PATH": agent["path"],
        "BASE": agent["base"],
        "BASE_DIGEST": agent["digest"],
    }


def build_context(image: Image, out: Path) -> Path:
    """One image's build context under ``out``: the build script and the pins, and for the seller its bridge layer.
    Its digest names the image, so a new pin or bridge builds a new one."""
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    shutil.copy2(VENDOR / "build-image.sh", out / "build-image.sh")
    shutil.copy2(VENDOR / "PIN.toml", out / "PIN.toml")
    if image is Image.SELLER:
        shutil.copytree(VENDOR / "seller", out / "seller", ignore=shutil.ignore_patterns("__pycache__"))
    return out


def build_images(stage: DemandStage, digest: Any) -> dict[Image, str]:
    """Each image built on the stage's arm64 project (or found by its tag); their URIs.

    Args:
        stage: the demand stage, whose DemandBuild stack holds the project and the repositories.
        digest: the content digest of a build context (``_demand.context_digest``).
    """
    outputs = stage.stack_outputs(stage.build_stack)
    if "SellingImageBuildProject" not in outputs:
        raise RuntimeError(f"{stage.build_stack} has no selling image project: is modules.selling on?")
    pinned = pin()
    images = {}
    for image in Image:
        uri = outputs[f"{image.value.capitalize()}ImageRepositoryUri"]
        context = build_context(image, BUILD / f"{stage.prefix}-{image.value}")
        images[image] = stage.build_on_codebuild(
            outputs["SellingImageBuildProject"],
            repository=image_repository(stage.prefix, image),
            repository_uri=uri,
            context=context,
            tag=digest(context),
            variables={"REPOSITORY_URI": uri, **build_variables(image, pinned)},
        )
    return images


def origin_facing_prefix_list(stage: DemandStage) -> str:
    """The id of CloudFront's origin-facing managed prefix list in the stage's region."""
    lists = stage.client("ec2").describe_managed_prefix_lists(
        Filters=[{"Name": "prefix-list-name", "Values": [ORIGIN_FACING]}]
    )["PrefixLists"]
    if not lists:
        raise RuntimeError(f"no managed prefix list {ORIGIN_FACING} in {stage.region}")
    return str(lists[0]["PrefixListId"])


def selling_context(stage: DemandStage, digest: Any) -> SellingContext:
    """The Selling stack's context: its images (built now), the catalog table and the prefix list."""
    data = stage.stack_outputs(f"{stage.stage_id}-Data")
    return SellingContext(
        images=build_images(stage, digest),
        catalog_table=data["CatalogTableName"],
        origin_facing_prefix_list=origin_facing_prefix_list(stage),
    )


def residue(stage: DemandStage) -> list[str]:
    """What is left of the module beyond its stack and tagged resources: the agents' log groups (AgentCore creates
    them) and the seller's database snapshots (there should be none: no backups, no final snapshot)."""
    found = []
    logs = stage.client("logs")
    for prefix in (
        seller(stage.prefix),
        *(f"/aws/bedrock-agentcore/runtimes/{runtime(stage.prefix, i)}" for i in Image),
    ):
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            found += [f"log group {g['logGroupName']}" for g in page["logGroups"]]
    rds = stage.client("rds")
    with suppress(ClientError):  # no such instance: no snapshot either
        found += [
            f"database snapshot {s['DBSnapshotIdentifier']}"
            for s in rds.describe_db_snapshots(DBInstanceIdentifier=seller(stage.prefix)).get("DBSnapshots", [])
        ]
    return found
