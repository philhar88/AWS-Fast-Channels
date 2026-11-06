"""Viewer profiles: their key, their item and how a tier is read back (ADR-0037)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from fast_channels.core.pds.viewers import profile_item, profile_key, tier_of

VID = "0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 11, 5, 9, 30, tzinfo=timezone(timedelta(hours=1)))


def test_a_profile_is_keyed_by_the_viewer_id() -> None:
    assert profile_key(VID) == {"pk": f"VIEWER#{VID}", "sk": "PROFILE"}


@pytest.mark.parametrize("vid", ["", "0123", VID.upper(), f"{VID}0", "auth0|user-1-sub-0123456789abcd"])
def test_anything_but_a_viewer_id_is_refused(vid: str) -> None:
    with pytest.raises(ValueError, match="32 lowercase hex"):
        profile_key(vid)


def test_a_personas_profile_takes_its_tier_from_the_list() -> None:
    assert profile_item(VID, "dana", now=NOW) == {
        "pk": f"VIEWER#{VID}",
        "sk": "PROFILE",
        "persona": "dana",
        "tier": "light",
        "updated_at": NOW.astimezone(UTC).isoformat(timespec="seconds"),
    }
    assert profile_item(VID, "ben", now=NOW)["tier"] == "holdout"


@pytest.mark.parametrize(("persona", "message"), [("mia", "no profile"), ("guest", "no profile"), ("x", "unknown")])
def test_a_kids_profile_the_guest_and_strangers_get_none(persona: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        profile_item(VID, persona, now=NOW)


@pytest.mark.parametrize(
    ("item", "tier"),
    [
        ({"tier": {"S": "light"}}, "light"),
        (None, None),
        ({}, None),
        ({"tier": {"N": "3"}}, None),
        ({"tier": {"S": "Light!"}}, None),
    ],
)
def test_the_tier_read_back(item: dict[str, dict[str, str]] | None, tier: str | None) -> None:
    assert tier_of(item) == tier
