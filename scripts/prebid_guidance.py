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
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import stage_args
from _stack import Plane, Stage

from fast_channels.core.pbs_config import PbsSettings, render, tracker_host

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


def stage_config(stage: str | None, prefix: str | None, region: str) -> FastChannelsConfig:
    """The configuration of ``stage`` (``stages/<stage>.json`` over cdk.json), optionally under another ``prefix``,
    validated for ``region``. Loads the CDK (a Node.js process), as ``cdk synth`` does."""
    from aws_cdk import App  # noqa: PLC0415 - only render-config needs the CDK

    from fast_channels.infra.config import FastChannelsConfig  # noqa: PLC0415

    context = dict(json.loads((REPO / "cdk.json").read_text())["context"])
    if stage:
        context |= stage_args.context(stage_args.with_prefix(stage_args.load(stage), prefix))
    elif prefix:
        context["fast-channels.prefix"] = json.dumps(prefix)
    return FastChannelsConfig.from_context(App(context=context).node, region=region)


def pbs_settings(config: FastChannelsConfig, content_base_url: str) -> PbsSettings:
    """The Prebid Server settings of a deployment whose CDN serves content, and our tracker, at
    ``content_base_url`` (the Media stack's ``ContentBaseUrl``)."""
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
    rendering.add_argument("--stage", help="a stage (stages/<name>.json); default: cdk.json alone")
    rendering.add_argument("--prefix", help="deployment prefix (replaces the stage's)")
    rendering.add_argument("--region", help="deployment region (default: AWS_REGION, else us-east-1)")
    rendering.add_argument("--content-base-url", help="the stage's ContentBaseUrl (default: its Media stack output)")
    rendering.add_argument("--out", type=Path, help="output directory (default build/prebid-guidance/<prefix>-config)")
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
    except (GuidanceError, stage_args.StageError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
