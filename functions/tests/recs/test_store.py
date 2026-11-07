from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fast_channels.core.recs.router import Strategy
from fast_channels.core.recs.serve import RailDoc
from fast_channels.core.recs.validate import RailTile
from fast_channels.functions.recs import store

if TYPE_CHECKING:
    from conftest import Tables

VID = "0" * 32
NOW = datetime(2026, 11, 10, 19, tzinfo=UTC).timestamp()
DOC = RailDoc(
    strategy=Strategy.LLM,
    title="For you",
    tiles=(RailTile(content_id="a", channel="c", reason="Sci-fi", evidence=("declared_genre",)),),
)


def test_the_first_sessions_auth_time_is_kept(tables: Tables) -> None:
    first = store.seen(tables.recs, VID, 1_000, now=NOW)
    again = store.seen(tables.recs, VID, 2_000, now=NOW)

    assert first.first_auth == again.first_auth == 1_000
    assert store.viewer(tables.recs, VID).first_auth == 1_000
    assert store.viewer(tables.recs, "1" * 32) == store.ViewerState()


def test_one_build_at_a_time_until_the_lease_ends(tables: Tables) -> None:
    assert store.claim_build(tables.recs, VID, now=NOW)
    assert not store.claim_build(tables.recs, VID, now=NOW + 10)
    assert store.claim_build(tables.recs, VID, now=NOW + store.BUILD_LEASE_SECONDS + 1)


def test_a_build_records_its_rail_or_its_failure(tables: Tables) -> None:
    store.claim_build(tables.recs, VID, now=NOW)
    store.failed(tables.recs, VID, "cap", now=NOW)
    failed = store.viewer(tables.recs, VID)
    assert (failed.failure, failed.failed_at, failed.building_at) == ("cap", NOW, None)

    store.built(tables.recs, VID, "hash", now=NOW + 60)
    built = store.viewer(tables.recs, VID)
    assert (built.rail_key, built.built_at, built.failure) == ("hash", NOW + 60, None)


def test_rails_are_kept_for_their_hours(tables: Tables) -> None:
    store.put_rail(tables.recs, "hash", DOC, hours=36, now=NOW)

    assert store.get_rail(tables.recs, "hash", now=NOW + 3600) == DOC
    assert store.get_rail(tables.recs, "hash", now=NOW + 37 * 3600) is None  # expired; TTL deletion lags
    assert store.get_rail(tables.recs, "other", now=NOW) is None
