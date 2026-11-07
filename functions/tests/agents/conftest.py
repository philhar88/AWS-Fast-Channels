"""The agents function tests' fixtures: DynamoDB Local with the agents' and ledger tables (``agent_tables``)."""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

from agent_tables import agent_tables, dynamodb_endpoint

__all__ = ["agent_tables", "dynamodb_endpoint"]
