import pytest

from fast_channels.core.ladder import Ladder


@pytest.fixture(scope="session")
def standard() -> Ladder:
    return Ladder.builtin("standard")


@pytest.fixture(scope="session")
def uhd() -> Ladder:
    return Ladder.builtin("uhd")
