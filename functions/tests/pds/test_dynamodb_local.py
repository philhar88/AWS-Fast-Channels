"""The DynamoDB Local fixture removes containers that a killed session left behind."""

from datetime import UTC, datetime

from dynamodb_local import stale


def test_only_containers_older_than_any_session_are_stale() -> None:
    listing = "aaa 2026-10-29 15:20:00 +0000 UTC\nbbb 2026-10-29 16:59:59 +0000 UTC\nccc 2026-10-29 17:30:00 +0100 CET"

    assert stale(listing, datetime(2026, 10, 29, 18, tzinfo=UTC)) == ["aaa"]
    assert stale("", datetime(2026, 10, 29, 18, tzinfo=UTC)) == []
