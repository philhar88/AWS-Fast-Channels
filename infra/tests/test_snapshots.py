"""Snapshot of the stacks this package owns; update deliberately with ``pytest --snapshot-update``."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

SNAPSHOT_STACKS = ("data", "baseline")


@pytest.mark.parametrize("stack", SNAPSHOT_STACKS)
def test_template_snapshot(
    deployment: Callable[..., Any], normalized: Callable[..., Any], snapshot_json: SnapshotAssertion, stack: str
) -> None:
    d = deployment({"observability.transaction_search": "enable"})

    assert normalized(d.template(getattr(d.stage, stack))) == snapshot_json
