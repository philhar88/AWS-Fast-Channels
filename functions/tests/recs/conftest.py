"""The recommender tests' fixtures: a stand-in Bedrock Runtime client (``fake_bedrock``)."""

import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).parent))

from fake_bedrock import FakeBedrock


@pytest.fixture
def bedrock() -> FakeBedrock:
    return FakeBedrock()
