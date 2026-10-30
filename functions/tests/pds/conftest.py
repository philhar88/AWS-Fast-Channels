"""The PDS function tests' fixtures: DynamoDB Local (``dynamodb_local``)."""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

from dynamodb_local import dynamodb, dynamodb_endpoint

__all__ = ["dynamodb", "dynamodb_endpoint"]
