"""The sidecar tests' fixtures: DynamoDB Local (``functions/tests/pds/dynamodb_local.py``)."""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parents[2] / "functions" / "tests" / "pds"))

from dynamodb_local import dynamodb, dynamodb_endpoint

__all__ = ["dynamodb", "dynamodb_endpoint"]
