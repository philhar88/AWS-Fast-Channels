import hashlib

import pytest

from fast_channels.core.pds.buckets import ARM_SEED, CRAWL_SEED, bucket, crawl_holdout, in_holdout


def test_bucket_is_sha256_of_seed_and_key_mod_100() -> None:
    key = "6f1c2a9e-4b7d-4e2a-9c1e-2d3f4a5b6c7d.820461826"
    expected = int(hashlib.sha256(f"crawl-v1{key}".encode()).hexdigest(), 16) % 100
    assert bucket(CRAWL_SEED, key) == expected
    assert crawl_holdout("6f1c2a9e-4b7d-4e2a-9c1e-2d3f4a5b6c7d", "820461826", 100) is True


def test_buckets_are_stable() -> None:
    # Recorded once. A change here reassigns every arm and holdout: that needs a new seed, not a new hash.
    assert bucket(ARM_SEED, "viewer-1") == 54
    assert bucket(CRAWL_SEED, "sid.1") == 68


def test_holdout_share_matches_its_percent() -> None:
    keys = [(f"session-{n}", str(n * 7)) for n in range(20_000)]
    share = sum(crawl_holdout(sid, evid, 20) for sid, evid in keys) / len(keys)
    assert 0.19 < share < 0.21
    assert not any(in_holdout(CRAWL_SEED, sid, 0) for sid, _ in keys[:100])
    assert all(in_holdout(CRAWL_SEED, sid, 100) for sid, _ in keys[:100])


@pytest.mark.parametrize("percent", [-1, 101])
def test_holdout_percent_bounds(percent: int) -> None:
    with pytest.raises(ValueError, match=r"0\.\.100"):
        in_holdout(CRAWL_SEED, "k", percent)
