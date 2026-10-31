"""Fixtures of the Prebid Server contract tests (marker ``pbs``, deselected by default):

    uv run python scripts/prebid_guidance.py apply && uv run python scripts/pbs_module.py build
    uv run pytest tests/pbs -m pbs

Without the jar, Java or the applied guidance checkout, every test is skipped with the reason.
"""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).parent))

from local_mocks import Mocks
from pbs_process import CHECKOUT, MissingPrerequisiteError, Pbs, find_jar, free_port, local_config, start


@pytest.fixture(scope="session")
def mocks(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Mocks]:
    if not (CHECKOUT / ".fast-overlays.json").is_file():
        pytest.skip("no applied guidance checkout: run `uv run python scripts/prebid_guidance.py apply`")
    servers = Mocks(CHECKOUT, tmp_path_factory.mktemp("mocks"))
    servers.start()
    yield servers
    servers.stop()


def _launch(
    mocks: Mocks,
    tmp_path_factory: pytest.TempPathFactory,
    jar_name: str,
    *,
    module: bool,
    analytics: bool = False,
) -> Iterator[Pbs]:
    try:
        jar = find_jar(jar_name)
    except MissingPrerequisiteError as missing:
        pytest.skip(str(missing))
    out = tmp_path_factory.mktemp(jar_name.removesuffix(".jar"))
    port = free_port()
    yield from start(jar, local_config(mocks, out, port=port, module=module, analytics=analytics), port, mocks)


@pytest.fixture(scope="session")
def pbs(mocks: Mocks, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pbs]:
    """The module's jar, module on."""
    yield from _launch(mocks, tmp_path_factory, "prebid-server.jar", module=True)


@pytest.fixture(scope="session")
def pbs_module_disabled(mocks: Mocks, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pbs]:
    """The module's jar with ``hooks.fast-ctv.enabled: false``."""
    yield from _launch(mocks, tmp_path_factory, "prebid-server.jar", module=False)


@pytest.fixture(scope="session")
def pbs_analytics(mocks: Mocks, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pbs]:
    """The module's jar with the guidance's log analytics reporter on (``EnableLogAnalytics``, off in every stage)."""
    yield from _launch(mocks, tmp_path_factory, "prebid-server.jar", module=True, analytics=True)


@pytest.fixture(scope="session")
def pbs_stock(mocks: Mocks, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pbs]:
    """The jar without the module's classes (the guidance's image without the module), module configured on."""
    yield from _launch(mocks, tmp_path_factory, "prebid-server-stock.jar", module=True)
