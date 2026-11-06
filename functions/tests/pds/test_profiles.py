"""Viewer profiles in DynamoDB Local: the seed script's write, the sidecar's read (ADR-0037)."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from fast_channels.core.pds.viewers import profile_key
from fast_channels.functions.pds.profiles import put_profile, read_tier

if TYPE_CHECKING:
    from dynamodb_local import Tables

DANA = "0123456789abcdef0123456789abcdef"
MARCUS = "fedcba9876543210fedcba9876543210"
NOW = datetime(2026, 11, 5, 9, tzinfo=UTC)


def test_a_written_profile_reads_back_its_tier(dynamodb: Tables) -> None:
    put_profile(dynamodb.client, dynamodb.pds, DANA, "dana", now=NOW)
    put_profile(dynamodb.client, dynamodb.pds, MARCUS, "marcus", now=NOW)

    assert read_tier(dynamodb.client, dynamodb.pds, DANA) == "light"
    assert read_tier(dynamodb.client, dynamodb.pds, MARCUS) == "standard"
    assert dynamodb.item(profile_key(DANA)) == {
        "pk": {"S": f"VIEWER#{DANA}"},
        "sk": {"S": "PROFILE"},
        "persona": {"S": "dana"},
        "tier": {"S": "light"},
        "updated_at": {"S": "2026-11-05T09:00:00+00:00"},
    }


def test_a_viewer_without_a_profile_has_no_tier(dynamodb: Tables) -> None:
    assert read_tier(dynamodb.client, dynamodb.pds, DANA) is None


def test_writing_again_replaces_the_profile(dynamodb: Tables) -> None:
    put_profile(dynamodb.client, dynamodb.pds, DANA, "dana", now=NOW)
    put_profile(dynamodb.client, dynamodb.pds, DANA, "ben", now=NOW)
    assert read_tier(dynamodb.client, dynamodb.pds, DANA) == "holdout"


def test_nothing_is_written_for_a_persona_without_a_profile(dynamodb: Tables) -> None:
    with pytest.raises(ValueError, match="no profile"):
        put_profile(dynamodb.client, dynamodb.pds, DANA, "mia", now=NOW)
    assert dynamodb.item(profile_key(DANA)) is None
