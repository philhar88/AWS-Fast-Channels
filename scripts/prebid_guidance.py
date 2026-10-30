#!/usr/bin/env -S uv run python
"""The AWS Prebid Server guidance, pinned with the repository's overlays (docs/demand.md, ADR-0020).

    uv run python scripts/prebid_guidance.py checkout          # the pinned commit, clean, in build/prebid-guidance/
    uv run python scripts/prebid_guidance.py verify            # checkout, then: pin, hashes and patches hold
    uv run python scripts/prebid_guidance.py apply             # checkout, verify, apply every overlay
    uv run python scripts/prebid_guidance.py render-config --stage fastm   # the stage's Prebid Server configuration

The guidance is deployed as its own CDK app (ADR-0002 exception A), never composed into this repository's app.
``vendor/prebid-guidance/PIN.toml`` pins its repository, tag and commit, the overlays, and the SHA-256 of every
upstream file an overlay patches or relies on. Every command starts from a fresh checkout of the pinned commit, and
``verify`` and ``apply`` refuse it when the tag moved, a pinned file changed or a patch no longer applies: an upstream
change is reviewed against the overlays before the pin moves.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import boto3
import stage_args
from _demand import DemandError, DemandStage, iterate_tool_versions
from _stack import Plane, StackError, Stage

from fast_channels.core.ad_server import AdServer
from fast_channels.core.pbs_config import HouseAd, PbsSettings, render, tracker_host

if TYPE_CHECKING:
    from fast_channels.infra.config import FastChannelsConfig

REPO: Final = Path(__file__).resolve().parents[1]
VENDOR: Final = REPO / "vendor" / "prebid-guidance"
PIN_FILE: Final = VENDOR / "PIN.toml"
BUILD: Final = REPO / "build" / "prebid-guidance"
DEFAULT_CHECKOUT: Final = BUILD / "checkout"
MODULE_SOURCES: Final = REPO / "pbs" / "fast-ctv" / "src" / "main"
"""The fast-ctv module's sources (``pbs/fast-ctv``), copied into the checkout by overlay O4 when they exist."""
MODULE_DESTINATION: Final = Path("source/loadtest/amt-bidder/fast-ctv/src/main")
APPLIED_MARKER: Final = ".fast-overlays.json"
"""Written into the checkout by :func:`apply`: the commit, the overlays and whether the module was copied."""
_COMMIT: Final = re.compile(r"[0-9a-f]{40}")
_PATCHED_PATH: Final = re.compile(r"^\+\+\+ b/(\S+)$", re.MULTILINE)


class GuidanceError(RuntimeError):
    """The pin, the checkout or an overlay does not hold."""


@dataclass(frozen=True)
class Overlay:
    """One overlay: a patch, and files copied from ``vendor/prebid-guidance`` into the checkout."""

    id: str
    title: str
    patch: Path
    files: Mapping[str, str]

    def patched_paths(self) -> set[str]:
        """Checkout-relative paths the patch changes."""
        return set(_PATCHED_PATH.findall(self.patch.read_text()))


@dataclass(frozen=True)
class PbsBuild:
    """How to build a Prebid Server Java release the guidance does not (``[pbs_java.builds."<release>"]``)."""

    jdk: str
    """The Corretto major version (overlay O6's ``JDK`` build argument)."""
    mvn_options: str


@dataclass(frozen=True)
class Jdk:
    """A JDK archive scripts/pbs_module.py may download (``[jdks."<major>".<os>-<arch>]``)."""

    url: str
    sha256: str


@dataclass(frozen=True)
class Pin:
    """``PIN.toml``: the pinned guidance, its overlays in order and the upstream file hashes."""

    repository: str
    tag: str
    commit: str
    license: str
    overlays: tuple[Overlay, ...]
    patched: Mapping[str, str]
    relied_on: Mapping[str, str]
    vendor: Path = VENDOR
    pbs_java_repository: str = "https://github.com/prebid/prebid-server-java"
    pbs_java_commits: Mapping[str, str] = field(default_factory=dict)
    """Prebid Server Java release -> the commit its tag must resolve to."""
    pbs_java_builds: Mapping[str, PbsBuild] = field(default_factory=dict)
    """Releases the guidance does not build -> their JDK and Maven options."""
    jdks: Mapping[str, Mapping[str, Jdk]] = field(default_factory=dict)
    """JDK major version -> ``<os>-<arch>`` -> its pinned archive."""

    @classmethod
    def load(cls, path: Path = PIN_FILE) -> Pin:
        data = tomllib.loads(path.read_text())
        guidance = data["guidance"]
        if not _COMMIT.fullmatch(guidance["commit"]):
            raise GuidanceError(f"{path}: commit must be a full 40-character SHA-1, got {guidance['commit']!r}")
        overlays = tuple(
            Overlay(entry["id"], entry["title"], path.parent / entry["patch"], dict(entry.get("files", {})))
            for entry in data["overlays"]
        )
        return cls(
            repository=guidance["repository"],
            tag=guidance["tag"],
            commit=guidance["commit"],
            license=guidance["license"],
            overlays=overlays,
            patched=dict(data["patched"]),
            relied_on=dict(data.get("relied_on", {})),
            vendor=path.parent,
            pbs_java_repository=data["pbs_java"]["repository"],
            pbs_java_commits=dict(data["pbs_java"]["commits"]),
            pbs_java_builds={
                release: PbsBuild(jdk=str(build["jdk"]), mvn_options=str(build["mvn_options"]))
                for release, build in data["pbs_java"].get("builds", {}).items()
            },
            jdks={
                major: {platform: Jdk(url=entry["url"], sha256=entry["sha256"]) for platform, entry in archives.items()}
                for major, archives in data.get("jdks", {}).items()
            },
        )

    @property
    def hashes(self) -> dict[str, str]:
        """Every pinned upstream file, patched or relied on."""
        return {**self.relied_on, **self.patched}

    def unpinned_patch_targets(self) -> set[str]:
        """Files an overlay patches that ``[patched]`` lacks (the pin must cover every one)."""
        return {path for overlay in self.overlays for path in overlay.patched_paths()} - set(self.patched)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def changed_files(pin: Pin, checkout: Path) -> list[str]:
    """Pinned upstream files missing from ``checkout`` or whose SHA-256 is not the pinned one."""
    return sorted(
        rel for rel, digest in pin.hashes.items() if not (checkout / rel).is_file() or sha256(checkout / rel) != digest
    )


def git(*args: str, cwd: Path) -> str:
    """Run git in ``cwd``; raise GuidanceError with its message on failure."""
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args],  # noqa: S607 - git from PATH, like every developer tool here
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        raise GuidanceError(f"git {' '.join(args)}: {run.stderr.strip() or run.stdout.strip()}")
    return run.stdout.strip()


def checkout(pin: Pin, directory: Path) -> None:
    """Fetch the pinned tag into ``directory`` and reset it to the pinned commit, discarding anything else there.

    Raises:
        GuidanceError: the tag does not resolve to the pinned commit (upstream moved it), or git failed.
    """
    if not (directory / ".git").is_dir():
        directory.mkdir(parents=True, exist_ok=True)
        git("init", "--quiet", cwd=directory)
        git("remote", "add", "origin", pin.repository, cwd=directory)
    git(
        "fetch",
        "--quiet",
        "--force",
        "--depth",
        "1",
        "origin",
        f"+refs/tags/{pin.tag}:refs/tags/{pin.tag}",
        cwd=directory,
    )
    if (resolved := git("rev-parse", f"{pin.tag}^{{commit}}", cwd=directory)) != pin.commit:
        raise GuidanceError(f"tag {pin.tag} of {pin.repository} is {resolved}, not the pinned {pin.commit}")
    git("checkout", "--quiet", "--force", "--detach", pin.commit, cwd=directory)
    git("clean", "--quiet", "-ffdx", cwd=directory)


def verify(pin: Pin, directory: Path) -> None:
    """The fresh checkout in ``directory`` is the pinned commit, every pinned file has its hash and every overlay's
    patch applies.

    Raises:
        GuidanceError: listing each problem.
    """
    problems = [
        f"PIN.toml [patched] lacks {path}, which an overlay patches" for path in sorted(pin.unpinned_patch_targets())
    ]
    if (head := git("rev-parse", "HEAD", cwd=directory)) != pin.commit:
        problems.append(f"{directory} is at {head}, not the pinned {pin.commit}")
    problems += [f"upstream file changed or missing: {rel}" for rel in changed_files(pin, directory)]
    if not problems:
        for overlay in pin.overlays:
            try:
                git("apply", "--check", str(overlay.patch), cwd=directory)
            except GuidanceError as error:
                problems.append(f"{overlay.id} does not apply: {error}")
    if problems:
        raise GuidanceError("the pinned guidance does not hold:\n" + "\n".join(f"  - {p}" for p in problems))


def apply(pin: Pin, directory: Path, *, module: Path | None = MODULE_SOURCES) -> list[str]:
    """Check the pinned commit out into ``directory``, verify it and apply every overlay; return the overlay ids.

    Overlay O4's script compiles the fast-ctv module when the checkout carries its sources: ``module`` is copied
    there when it exists.
    """
    checkout(pin, directory)
    verify(pin, directory)
    for overlay in pin.overlays:
        git("apply", "--whitespace=error", str(overlay.patch), cwd=directory)
        for source, destination in overlay.files.items():
            target = directory / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pin.vendor / source, target)
    with_module = module is not None and module.is_dir()
    if module is not None and with_module:
        shutil.copytree(module, directory / MODULE_DESTINATION, dirs_exist_ok=True)
    applied = [overlay.id for overlay in pin.overlays]
    marker = {"tag": pin.tag, "commit": pin.commit, "overlays": applied, "module": with_module}
    (directory / APPLIED_MARKER).write_text(json.dumps(marker) + "\n")
    return applied


def stage_config(
    stage: str | None, prefix: str | None, region: str, modules: list[str] | None = None
) -> FastChannelsConfig:
    """The configuration of ``stage`` (``stages/<stage>.json`` over cdk.json), optionally under another ``prefix``
    and with only ``modules`` on, validated for ``region``. Loads the CDK (a Node.js process), as ``cdk synth``
    does."""
    from aws_cdk import App  # noqa: PLC0415 - only render-config needs the CDK

    from fast_channels.infra.config import FastChannelsConfig  # noqa: PLC0415

    context = dict(json.loads((REPO / "cdk.json").read_text())["context"])
    if stage:
        context |= stage_args.context(stage_args.resolve(stage, prefix, modules))
    elif prefix:
        context["fast-channels.prefix"] = json.dumps(prefix)
    return FastChannelsConfig.from_context(App(context=context).node, region=region)


def pbs_settings(config: FastChannelsConfig, content_base_url: str, house: Sequence[HouseAd] = ()) -> PbsSettings:
    """The Prebid Server settings of a deployment whose CDN serves content, and our tracker, at
    ``content_base_url`` (the Media stack's ``ContentBaseUrl``), with the kit's ``house`` ads for the module's pod
    of last resort."""
    demand, pds = config.modules.demand, config.modules.pds
    return PbsSettings(
        prefix=config.prefix,
        simulator=demand.mode == "demo",
        server_instances=demand.server_instances,
        tracker_base_url=content_base_url.rstrip("/"),
        publisher_domain=tracker_host(content_base_url),
        policy_timeout_ms=pds.policy_timeout_ms,
        policy_group_timeout_ms=pds.policy_group_timeout_ms,
        select_timeout_ms=pds.select_timeout_ms,
        select_group_timeout_ms=pds.select_group_timeout_ms,
        house_ads=tuple(house),
        ad_server=demand.ad_server is not AdServer.NONE,
        pod_cache_seconds=demand.pod_cache_seconds,
    )


def write_config(files: Mapping[str, str], out: Path) -> list[Path]:
    """Write rendered files under ``out`` (replacing it); return the paths written."""
    shutil.rmtree(out, ignore_errors=True)
    written = []
    for rel, content in sorted(files.items()):
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written.append(path)
    return written


def render_config(args: argparse.Namespace) -> int:
    region = args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    config = stage_config(args.stage, args.prefix, region)
    if not config.modules.demand.enabled:
        raise GuidanceError(f"stage {config.prefix}: modules.demand is off, so there is no Prebid Server to configure")
    content_base_url = args.content_base_url or Stage.of(config.prefix).output(Plane.MEDIA, "ContentBaseUrl")
    out = args.out or BUILD / f"{config.prefix}-config"
    written = write_config(render(pbs_settings(config, content_base_url)), out)
    print(f"{len(written)} files for stage {config.prefix} under {out}")
    return 0


STAGE_COMMANDS: Final = {
    "doctor": "check the tools a demand deploy uses",
    "bootstrap": "create or update the guidance's dedicated CDK bootstrap",
    "build-image": "build the Prebid Server image on the stage's x86 CodeBuild project",
    "deploy": "deploy the demand module: repository stacks, bootstrap, image, guidance, SSM contract, smoke",
    "outputs": "write the SSM contract /<prefix>/demand/* from the deployed guidance",
    "upload-config": "sync the rendered configuration, verify it in S3, and roll the tasks if it changed",
    "smoke": "synthetic MediaTailor POSTs through the guidance's CloudFront and WAF",
    "herd": "a break herd of probe auctions through the guidance's CloudFront (the warm-up project)",
    "destroy": "destroy the demand module, including what the guidance's templates retain",
    "inventory": "list what is left of the stage's demand module",
}


def demand_stage(args: argparse.Namespace) -> DemandStage:
    """The stage the command acts on."""
    region = args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    modules = args.modules.split(",") if args.modules else None
    config = stage_config(args.stage, args.prefix, region, modules)
    if not config.modules.demand.enabled:
        raise GuidanceError(f"stage {config.prefix}: modules.demand is off")
    return DemandStage(config, args.stage, boto3.Session(region_name=region), modules=modules)


def content_base_url(args: argparse.Namespace, prefix: str) -> str:
    return args.content_base_url or Stage.of(prefix).output(Plane.MEDIA, "ContentBaseUrl")


def stage_command(args: argparse.Namespace, pin: Pin) -> int:
    """Run one of :data:`STAGE_COMMANDS`."""
    if args.command == "doctor":
        tools = list(iterate_tool_versions())
        for name, version in tools:
            print(f"{name:16} {version}")
        return 1 if any(version.startswith("MISSING") for _, version in tools) else 0
    stage = demand_stage(args)
    started = time.monotonic()
    match args.command:
        case "bootstrap":
            stage.bootstrap()
        case "build-image":
            apply(pin, stage.checkout)
            print("\n".join(stage.build_images(pin)))
        case "deploy":
            base = content_base_url(args, stage.prefix)
            stage.deploy_repository_stacks()
            stage.bootstrap()
            apply(pin, stage.checkout)
            image = stage.build_image(pin)
            # With the PDS on, its sidecar joins Prebid Server's task (ADR-0036).
            pds = stage.sidecar(stage.build_pds_image(), base) if stage.config.modules.pds.enabled else None
            demo = stage.demo_index()
            stage.render_into_checkout(base, demo)
            stage.deploy_guidance(image, pds)
            stage.write_parameters(stage.discover(image))
            # The guidance uploads its configuration only when its stack is created: sync it on every deploy.
            stage.upload_config(base, prune=not args.keep_stale)
            stage.deploy_demand_stack(demo, base)
            stage.start_census_gate()
            stage.warm_running_tasks()
            if not args.no_smoke:
                return report_smoke(stage)
        case "outputs":
            stage.write_parameters(stage.discover())
        case "upload-config":
            stage.upload_config(content_base_url(args, stage.prefix), prune=not args.keep_stale)
        case "smoke":
            return report_smoke(stage)
        case "herd":
            print(json.dumps(stage.herd(args.rate, args.seconds), indent=2))
        case "destroy":
            stage.destroy()
            return report_inventory(stage)
        case "inventory":
            return report_inventory(stage)
    print(f"{args.command} done in {round(time.monotonic() - started)} s")
    return 0


def report_smoke(stage: DemandStage) -> int:
    results = stage.smoke()
    print(json.dumps(results, indent=2))
    return 0 if results["passed"] else 1


def report_inventory(stage: DemandStage) -> int:
    residue = stage.inventory()
    for item in residue:
        print(f"residue: {item}")
    print(
        f"{len(residue)} resources of stage {stage.prefix}'s demand module remain (KMS keys pending deletion "
        "are not counted)"
    )
    return 1 if residue else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("checkout", "check the pinned commit out, clean"),
        ("verify", "check out, then check the pin, the pinned files and the patches"),
        ("apply", "check out, verify and apply every overlay"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument(
            "--dir", type=Path, default=DEFAULT_CHECKOUT, help=f"checkout (default {DEFAULT_CHECKOUT})"
        )
    rendering = commands.add_parser("render-config", help="render a stage's Prebid Server configuration")
    for command in [rendering, *(commands.add_parser(name, help=text) for name, text in STAGE_COMMANDS.items())]:
        command.add_argument("--stage", help="a stage (stages/<name>.json); default: cdk.json alone")
        command.add_argument("--prefix", help="deployment prefix (replaces the stage's)")
        command.add_argument("--modules", help="comma-separated modules of the stage to keep on (others off)")
        command.add_argument("--region", help="deployment region (default: AWS_REGION, else us-east-1)")
        command.add_argument("--content-base-url", help="the stage's ContentBaseUrl (default: its Media stack output)")
    rendering.add_argument("--out", type=Path, help="output directory (default build/prebid-guidance/<prefix>-config)")
    commands.choices["deploy"].add_argument("--no-smoke", action="store_true", help="skip the synthetic POSTs")
    for name in ("deploy", "upload-config"):
        commands.choices[name].add_argument(
            "--keep-stale",
            action="store_true",
            help="upload the configuration but delete nothing it no longer renders (prune later with upload-config, "
            "once the siblings' rollout has moved them off what it named)",
        )
    commands.choices["herd"].add_argument("--rate", type=int, default=400, help="auctions per second (default 400)")
    commands.choices["herd"].add_argument("--seconds", type=int, default=6, help="duration (default 6 s)")
    args = parser.parse_args(argv)
    try:
        pin = Pin.load()
        match args.command:
            case "checkout":
                checkout(pin, args.dir)
                print(f"{args.dir}: {pin.tag} at {pin.commit}")
            case "verify":
                checkout(pin, args.dir)
                verify(pin, args.dir)
                print(f"{args.dir}: {pin.tag} at {pin.commit}; {len(pin.hashes)} pinned files unchanged; patches apply")
            case "apply":
                applied = apply(pin, args.dir)
                print(f"{args.dir}: {pin.tag} at {pin.commit} with overlays {', '.join(applied)}")
            case "render-config":
                return render_config(args)
            case _:
                return stage_command(args, pin)
    except (GuidanceError, DemandError, StackError, stage_args.StageError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
