#!/usr/bin/env -S uv run python
"""Turn a stage file into CDK context arguments, or print its deployment prefix.

    uv run python scripts/stage_args.py context fastm                  # -c 'fast-channels.prefix="fastm"' -c ...
    uv run python scripts/stage_args.py prefix fastm                   # fastm
    uv run python scripts/stage_args.py context fastm --prefix fastx   # the same stage under another prefix
    uv run python scripts/stage_args.py context fastci --modules ops,demand   # only these modules on

A stage is ``stages/<name>.json``: a partial ``fast-channels`` configuration, a JSON object whose keys are top-level
fields of the block in cdk.json (``prefix``, ``observability``, ``modules``, ...). Each key becomes one
``-c fast-channels.<key>=<JSON>`` argument, which the configuration loader deep-merges into cdk.json's block
(``fast_channels.infra.config``), so a stage states only what differs from cdk.json and must name its prefix.
``context`` prints the arguments quoted for a POSIX shell; ``make <target> STAGE=<name>`` passes them to the CDK CLI
and the prefix to the operator scripts.

Only the stage file's shape and prefix are checked here, without loading the CDK (which starts a Node.js process):
``cdk synth`` validates the merged configuration.
"""

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parents[1]
STAGES: Final = REPO / "stages"
CDK_JSON: Final = REPO / "cdk.json"
CONTEXT_KEY: Final = "fast-channels"
PREFIX: Final = re.compile(r"[a-z][a-z0-9-]{0,14}[a-z0-9]")
"""``FastChannelsConfig.prefix``."""
_NAME: Final = re.compile(r"[a-z][a-z0-9-]*")


class StageError(ValueError):
    """The stage does not exist or its file is not a usable partial configuration."""


def configuration_keys() -> frozenset[str]:
    """Top-level fields of the ``fast-channels`` block (cdk.json spells out every one)."""
    return frozenset(json.loads(CDK_JSON.read_text())["context"][CONTEXT_KEY])


def stage_names() -> list[str]:
    """The stages: one per ``stages/<name>.json``."""
    return sorted(path.stem for path in STAGES.glob("*.json"))


def load(name: str) -> dict[str, Any]:
    """The overrides of stage ``name`` (``stages/<name>.json``).

    Raises:
        StageError: no such stage, or its file is not a JSON object of known top-level fields with a valid prefix.
    """
    path = STAGES / f"{name}.json"
    if not _NAME.fullmatch(name) or not path.is_file():
        raise StageError(f"unknown stage {name!r}: the stages are {', '.join(stage_names()) or 'none'} ({STAGES})")
    try:
        stage = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise StageError(f"{path} is not JSON: {error}") from None
    if not isinstance(stage, dict):
        raise StageError(f"{path} must hold a JSON object (a partial {CONTEXT_KEY} block)")
    if unknown := sorted(set(stage) - configuration_keys()):
        raise StageError(f"{path}: {unknown} are not {CONTEXT_KEY} fields")
    prefix = stage.get("prefix")
    if not isinstance(prefix, str) or not PREFIX.fullmatch(prefix):
        raise StageError(f"{path}: prefix must be a deployment prefix matching {PREFIX.pattern}, got {prefix!r}")
    return stage


def with_prefix(stage: dict[str, Any], prefix: str | None) -> dict[str, Any]:
    """``stage`` deployed under ``prefix`` instead of its own, when given."""
    if prefix is None:
        return stage
    if not PREFIX.fullmatch(prefix):
        raise StageError(f"--prefix must match {PREFIX.pattern}, got {prefix!r}")
    return {**stage, "prefix": prefix}


def with_modules(stage: dict[str, Any], modules: list[str] | None) -> dict[str, Any]:
    """``stage`` with only ``modules`` on, when given: every other module the stage names is turned off (the
    configuration still refuses a module whose dependencies are off)."""
    if modules is None:
        return stage
    named = stage.get("modules", {})
    if unknown := sorted(set(modules) - set(named)):
        raise StageError(f"--modules {unknown} are not modules of the stage ({', '.join(named)})")
    return {
        **stage,
        "modules": {name: value if name in modules else {"enabled": False} for name, value in named.items()},
    }


def resolve(name: str, prefix: str | None = None, modules: list[str] | None = None) -> dict[str, Any]:
    """Stage ``name`` under ``prefix`` (when given) with only ``modules`` on (when given)."""
    return with_modules(with_prefix(load(name), prefix), modules)


def context(stage: dict[str, Any]) -> dict[str, str]:
    """CDK context entries: ``fast-channels.<key>`` -> the key's value as compact JSON."""
    return {f"{CONTEXT_KEY}.{key}": json.dumps(value, separators=(",", ":")) for key, value in stage.items()}


def arguments(stage: dict[str, Any]) -> list[str]:
    """The CDK CLI arguments that apply ``stage``: ``-c <key>=<JSON>`` per top-level key."""
    return [argument for key, value in context(stage).items() for argument in ("-c", f"{key}={value}")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", choices=("context", "prefix"), help="what to print")
    parser.add_argument("stage", help=f"a stage: {', '.join(stage_names())} (stages/<name>.json)")
    parser.add_argument("--prefix", help="deploy the stage under this prefix instead of its own")
    parser.add_argument("--modules", help="comma-separated modules to keep on (the others are turned off)")
    args = parser.parse_args(argv)
    try:
        stage = resolve(args.stage, args.prefix, args.modules.split(",") if args.modules else None)
    except StageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(shlex.join(arguments(stage)) if args.output == "context" else stage["prefix"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
