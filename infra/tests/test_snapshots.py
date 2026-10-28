"""Snapshots of the synthesized templates; update deliberately with ``pytest --snapshot-update`` and review the diff."""

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.infra import stage
from fast_channels.infra.stacks.operator import OperatorStack

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

SNAPSHOT_STACKS = ("data", "baseline")
SHIPPED_STACKS = {
    "us-east-1": ("data", "media", "operator"),  # what `make synth` synthesizes
    "eu-west-1": ("edge",),  # the stack the shipped configuration adds in any other region
}
"""Every stack the shipped configuration synthesizes, by region."""


@pytest.mark.parametrize("stack", SNAPSHOT_STACKS)
def test_template_snapshot(
    deployment: Callable[..., Any], normalized: Callable[..., Any], snapshot_json: SnapshotAssertion, stack: str
) -> None:
    d = deployment({"observability.transaction_search": "enable"})

    assert normalized(d.template(getattr(d.stage, stack))) == snapshot_json


@pytest.fixture
def console_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stand-in ``console/dist`` for the stage: the Operator template depends on whether the console was built,
    and CI's Python job never builds it."""
    (tmp_path / "index.html").write_text('<!doctype html><script type="module" src="/assets/index-abc.js"></script>')
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc.js").write_text("console.log('console');")
    monkeypatch.setattr(stage, "OperatorStack", partial(OperatorStack, console_dist=tmp_path))


@pytest.mark.usefixtures("console_build")
@pytest.mark.parametrize(
    ("region", "stack"), [(region, stack) for region, stacks in SHIPPED_STACKS.items() for stack in stacks]
)
def test_shipped_templates(
    fresh_deployment: Callable[..., Any],
    normalized: Callable[..., Any],
    snapshot_json: SnapshotAssertion,
    region: str,
    stack: str,
) -> None:
    """The shipped configuration synthesizes exactly the committed templates, first recorded from v3.0.0. Every
    module is off in it, so a module's pull request leaves these snapshots unchanged."""
    d = fresh_deployment(region=region)

    assert normalized(d.template(getattr(d.stage, stack))) == snapshot_json
