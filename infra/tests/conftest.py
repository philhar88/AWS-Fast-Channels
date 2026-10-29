"""Fixtures: FAST Channels stages synthesized from the shipped cdk.json context, with asset bundling disabled."""

import json
import os
import re
import shutil
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from aws_cdk import App, Stack
from aws_cdk.assertions import Template
from syrupy.extensions.json import JSONSnapshotExtension

from fast_channels.core.ladder import BUILTIN_DIR, LADDERS_DIR_ENV, Ladder, LadderOps, propose
from fast_channels.infra.app import AS_RUN_ROUTER, build
from fast_channels.infra.config import EVAL_RECORDS, EVAL_RECORDS_ENV, FastChannelsConfig

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

    from fast_channels.infra.stage import FastChannelsStage

ACCOUNT = "123456789012"
REPO_ROOT = Path(__file__).resolve().parents[2]
CDK_CONTEXT: dict[str, Any] = json.loads((REPO_ROOT / "cdk.json").read_text())["context"]
type Overrides = Mapping[str, Any]
FIXTURE_RECORDS = Path(__file__).parent / "data" / "evals"
"""Eval records of tasks without a suite yet (the recommender's), for configurations these tests synthesize."""
os.environ[EVAL_RECORDS_ENV] = os.pathsep.join([str(EVAL_RECORDS), str(FIXTURE_RECORDS)])
WEAK_REFERENCE_OUTPUT = "PublishOutput"
"""Name prefix of the outputs behind weak cross-stack references (``Fn::GetStackOutput``)."""


def context(overrides: Overrides | None = None) -> dict[str, Any]:
    """cdk.json context plus ``-c fast-channels.<path>=<value>``-style overrides, with bundling disabled. The key
    ``as-run-router`` (``-c as-run-router=true``, ``make as-run-router``) is passed as it is."""
    dotted = {k if k == AS_RUN_ROUTER else f"fast-channels.{k}": v for k, v in (overrides or {}).items()}
    return {**CDK_CONTEXT, "aws:cdk:bundling-stacks": [], **dotted}


@dataclass(frozen=True, slots=True)
class Deployment:
    app: App
    stage: FastChannelsStage

    def template(self, stack: Stack | None) -> Template:
        assert stack is not None, "stack not part of this deployment"
        return Template.from_stack(stack)

    @property
    def stacks(self) -> list[Stack]:
        return [child for child in self.stage.node.children if isinstance(child, Stack)]


def deploy(
    overrides: Overrides | None = None,
    *,
    region: str = "us-east-1",
    account: str | None = ACCOUNT,
    outdir: Path | None = None,
) -> Deployment:
    """A new, unsynthesized deployment (tests may still add constructs to its stacks)."""
    app = App(context=context(overrides), outdir=str(outdir) if outdir else None)
    return Deployment(app, build(app, account=account, region=region))


@pytest.fixture
def fresh_deployment() -> Callable[..., Deployment]:
    """Build a new deployment that the test may extend before synthesizing."""
    return deploy


@pytest.fixture(scope="session")
def deployment() -> Callable[..., Deployment]:
    """Cached deployments by (overrides, region); do not add constructs to them."""
    cache: dict[str, Deployment] = {}

    def get(overrides: Overrides | None = None, *, region: str = "us-east-1") -> Deployment:
        key = json.dumps([overrides or {}, region], sort_keys=True)
        if key not in cache:
            cache[key] = deploy(overrides, region=region)
        return cache[key]

    return get


@pytest.fixture(scope="session")
def default(deployment: Callable[..., Deployment]) -> Deployment:
    """The shipped configuration in us-east-1."""
    return deployment()


@pytest.fixture(scope="module")
def second_ladder(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Ladder]:
    """A ladders directory (``FAST_LADDERS_DIR``) with ``standard`` and a second built-in, ``mobile``: standard up
    to 720p."""
    directory = tmp_path_factory.mktemp("ladders")
    shutil.copy(BUILTIN_DIR / "standard.toml", directory)
    mobile = propose(Ladder.builtin("standard"), LadderOps(max_height=720, new_id="mobile")).ladder
    assert mobile is not None
    (directory / "mobile.toml").write_text(mobile.to_toml())
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(LADDERS_DIR_ENV, str(directory))
        yield mobile


@pytest.fixture
def load_config() -> Callable[..., FastChannelsConfig]:
    """Load a configuration from raw CDK context (no cdk.json)."""

    def load(raw_context: Mapping[str, Any], *, region: str | None = None) -> FastChannelsConfig:
        return FastChannelsConfig.from_context(App(context=dict(raw_context)).node, region=region)

    return load


_ASSET_HASH = re.compile(r"\b[0-9a-f]{64}\b")


def _normalized(template: Template) -> dict[str, Any]:
    normalized: dict[str, Any] = json.loads(_ASSET_HASH.sub("<asset-hash>", json.dumps(template.to_json())))
    return normalized


@pytest.fixture
def normalized() -> Callable[[Template], dict[str, Any]]:
    """Template JSON with asset hashes replaced, so snapshots only change when the infrastructure does."""
    return _normalized


def _named_outputs(template: Template) -> set[str]:
    return {name for name in template.find_outputs("*") if not name.startswith(WEAK_REFERENCE_OUTPUT)}


@pytest.fixture
def named_outputs() -> Callable[[Template], set[str]]:
    """Output keys of a template, without the outputs CDK generates for weak cross-stack references."""
    return _named_outputs


@pytest.fixture
def snapshot_json(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Snapshots stored as reviewable JSON files under ``__snapshots__``."""
    return snapshot.use_extension(JSONSnapshotExtension)
