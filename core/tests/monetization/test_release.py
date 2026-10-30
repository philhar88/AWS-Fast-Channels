"""The Release Controller's state and decisions: which siblings a release configures, drift paths, the plan."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from fast_channels.core.mediatailor_logs import ADS_LOG_FIELDS, MANIFEST_LOG_FIELDS, RECORD_FIELDS, VENDED_LOG_TYPES
from fast_channels.core.monetization import names
from fast_channels.core.monetization.bundle import ChannelInput
from fast_channels.core.monetization.release import (
    ChannelState,
    PlaybackPlan,
    SiblingRecord,
    applied,
    differences,
    followed,
    kept_releases,
    last_good,
    mapped_functions,
    playback_plan,
    pointer,
    promoted,
    rolled_back,
    targets,
)

AT = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
DIGEST = "a" * 64


def test_names() -> None:
    assert names.parameter_name("fastm", names.Parameter.CDN_BASE_URL) == "/fastm/release/cdn-base-url"
    assert names.channel_parameter_name("fastm", "fastm-demo") == "/fastm/release/channels/fastm-demo"
    assert names.stack_name("FastChannels-fastm") == "FastChannels-fastm-Release"
    assert names.state_table("fastm") == names.state_machine("fastm") == "fastm-release"
    assert names.log_group("fastm") == "/aws/vendedlogs/mediatailor/fastm-release"
    assert names.log_destination("fastm") == "fastm-release-mediatailor"
    assert names.lake_destinations("fastm") == ("fastm-lake-ads", "fastm-lake-manifest")
    assert names.bundle_key(DIGEST) == f"release/{DIGEST}/bundle.json"
    assert names.plan_key("fastm-demo") == "plan/playback/fastm-demo.json"
    assert names.channel_chain("fastm-demo") == "release/fastm-demo"
    assert names.function_pattern("fastm") == "fastm_*"
    assert names.sibling_pattern("fastm-demo") == "fastm-demo-p-*"


def test_never_stored_log_fields() -> None:
    assert "requestHeaders" not in ADS_LOG_FIELDS and "rawAdsRequest" not in ADS_LOG_FIELDS
    assert not {"mediaTailorPath", "input", "http"} & set(MANIFEST_LOG_FIELDS)
    assert "output" in MANIFEST_LOG_FIELDS  # the PRE_SESSION hook's committed controls stay readable
    assert set(RECORD_FIELDS) < set(VENDED_LOG_TYPES)


def test_the_first_release_configures_every_sibling(kit: SimpleNamespace) -> None:
    bundle = kit.make_bundle()
    assert [s.name for s in targets(bundle, {})] == ["fastm-demo-p-a", "fastm-demo-p-b", "fastm-demo-p-z"]
    state = applied(
        None,
        channel="fastm-demo",
        siblings=["fastm-demo-p-a", "fastm-demo-p-b", "fastm-demo-p-z"],
        release=1,
        digest=DIGEST,
        change_id="c1",
        at=AT,
    )
    assert (state.stable, state.idle) == ("fastm-demo-p-a", "fastm-demo-p-b")
    assert state.siblings["fastm-demo-p-z"] == SiblingRecord(release=1, digest=DIGEST, change_id="c1", at=AT)
    assert state.history == {"fastm-demo-p-a": (1,), "fastm-demo-p-b": (1,), "fastm-demo-p-z": (1,)}


def test_later_releases_go_to_the_idle_sibling_only(kit: SimpleNamespace) -> None:
    bundle = kit.make_bundle()
    state = applied(
        None,
        channel="fastm-demo",
        siblings=["fastm-demo-p-a", "fastm-demo-p-b"],
        release=1,
        digest=DIGEST,
        change_id="c1",
        at=AT,
    )
    # The probe was not configured yet (it is new), so release 2 takes it with the idle sibling.
    assert [s.name for s in targets(bundle, {"fastm-demo": state})] == ["fastm-demo-p-b", "fastm-demo-p-z"]
    state = applied(
        state,
        channel="fastm-demo",
        siblings=["fastm-demo-p-b", "fastm-demo-p-z"],
        release=2,
        digest=DIGEST,
        change_id="c2",
        at=AT,
    )
    assert [s.name for s in targets(bundle, {"fastm-demo": state})] == ["fastm-demo-p-b"]
    assert (state.stable, state.idle) == ("fastm-demo-p-a", "fastm-demo-p-b")  # promotion is M3-04's
    for release in (3, 4, 5):
        state = applied(
            state,
            channel="fastm-demo",
            siblings=["fastm-demo-p-b"],
            release=release,
            digest=DIGEST,
            change_id=f"c{release}",
            at=AT,
        )
    assert state.history["fastm-demo-p-b"] == (5, 4, 3)  # three trees kept per sibling
    assert state.siblings["fastm-demo-p-a"].release == 1
    assert applied(
        state, channel="fastm-demo", siblings=["fastm-demo-p-b"], release=5, digest=DIGEST, change_id="c5", at=AT
    ).history["fastm-demo-p-b"] == (5, 4, 3)  # a retried release


def test_a_new_channel_is_bootstrapped_beside_released_ones(kit: SimpleNamespace) -> None:
    channels = [ChannelInput(name=n, video_content_source_url=kit.CA_URL) for n in ("fastm-demo", "fastm-news")]
    bundle = kit.make_bundle(channels=channels, probe_sibling=False)
    released = ChannelState(
        channel="fastm-demo",
        stable="fastm-demo-p-a",
        idle="fastm-demo-p-b",
        siblings={
            n: SiblingRecord(release=1, digest=DIGEST, change_id="c", at=AT)
            for n in ("fastm-demo-p-a", "fastm-demo-p-b")
        },
    )
    assert [s.name for s in targets(bundle, {"fastm-demo": released})] == [
        "fastm-demo-p-b",
        "fastm-news-p-a",
        "fastm-news-p-b",
    ]


def test_a_release_creates_the_trees_its_siblings_map(kit: SimpleNamespace) -> None:
    bundle = kit.make_bundle(contributions=[kit.redeem()])
    a, b, z = bundle.siblings
    assert [f.function_id for f in mapped_functions(bundle, [b])] == [
        "fastm_demo_p_b_ps_redeem",
        "fastm_ps_ctl",
        "fastm_demo_p_b_ps_root",
    ]
    assert {f.function_id for f in mapped_functions(bundle, [a, b, z])} >= {"fastm_demo_p_a_ps_redeem"}
    assert mapped_functions(bundle, []) == []


def test_differences_report_paths_never_values() -> None:
    desired = {
        "Name": "x",
        "Headers": {"X-Fast-Origin": "secret-a", "X-Fast-Pc": "x"},
        "Strategies": ["VENDED_LOGS", "LEGACY_CLOUDWATCH"],
        "Steps": [{"FunctionId": "f_r0001"}, {"FunctionId": "g_r0001"}],
    }
    actual = {
        "Name": "x",
        "Arn": "added by the service",
        "Headers": {"X-Fast-Origin": "secret-b", "X-Fast-Pc": "x"},
        "Strategies": ["LEGACY_CLOUDWATCH", "VENDED_LOGS"],
        "Steps": [{"FunctionId": "g_r0001"}, {"FunctionId": "f_r0001"}],
    }
    assert differences(desired, actual) == ["Headers.X-Fast-Origin", "Steps[0].FunctionId", "Steps[1].FunctionId"]
    assert differences(desired, desired) == []
    assert differences({"A": {"B": 1}}, {"A": "flat"}) == ["A"]
    assert differences({"A": 1}, {}) == ["A"]
    assert differences({"L": [1]}, {"L": [1, 2]}) == ["L"]
    assert differences({"L": [1]}, {"L": "x"}) == ["L"]
    assert differences({"A": 1}, None) == ["."]


def test_playback_plan() -> None:
    plan = playback_plan(
        channel="fastm-demo",
        configuration="fastm-demo-p-a",
        release=7,
        playback={"hls": "https://cdn/v1/master/h/fastm-demo-p-a/hls.m3u8"},
        at=AT,
    )
    assert plan == PlaybackPlan.model_validate_json(plan.model_dump_json())
    assert plan.release == "r0007" and plan.v == 1
    with pytest.raises(ValueError, match="release must be 1-9999"):
        playback_plan(channel="c", configuration="c-p-a", release=0, playback={}, at=AT)


def test_promotion_swaps_the_pair_and_rollback_returns_to_the_last_good_tree() -> None:
    names_ = ["fastm-demo-p-a", "fastm-demo-p-b", "fastm-demo-p-z"]
    state = applied(None, channel="fastm-demo", siblings=names_, release=1, digest=DIGEST, change_id="c1", at=AT)
    with pytest.raises(ValueError, match="never released"):
        promoted(ChannelState(channel="fastm-news"))
    state = applied(
        state, channel="fastm-demo", siblings=["fastm-demo-p-b"], release=2, digest=DIGEST, change_id="c2", at=AT
    )
    assert last_good(state, "fastm-demo-p-b") == 1 and last_good(state, "fastm-demo-p-x") is None
    swapped = promoted(state)
    assert (swapped.stable, swapped.idle) == ("fastm-demo-p-b", "fastm-demo-p-a")
    assert pointer({"fastm-demo": swapped, "fastm-news": ChannelState(channel="fastm-news")}) == {
        "fastm-demo": "fastm-demo-p-b"
    }
    good = SiblingRecord(release=1, digest=DIGEST, change_id="c1", at=AT)
    back = rolled_back(state, "fastm-demo-p-b", record=good)
    assert back.siblings["fastm-demo-p-b"] == good and back.history["fastm-demo-p-b"] == (1,)
    with pytest.raises(ValueError, match="rolls back to None"):
        rolled_back(back, "fastm-demo-p-b", record=good)
    assert kept_releases({"fastm-demo": state}) == {1, 2}


def test_the_probe_follows_the_stable_release_and_keeps_no_history() -> None:
    """M3-x4: the probe ``z`` is re-applied with the stable sibling's release. It keeps no rollback target of its own,
    so the trees of its earlier release are deleted once no other sibling keeps them."""
    names_ = ["fastm-demo-p-a", "fastm-demo-p-b", "fastm-demo-p-z"]
    state = applied(None, channel="fastm-demo", siblings=names_, release=1, digest=DIGEST, change_id="c1", at=AT)
    for release in range(2, 8):  # the pair siblings keep 3 releases each: none of them keeps 1
        idle = state.idle
        assert idle is not None
        state = promoted(
            applied(state, channel="fastm-demo", siblings=[idle], release=release, digest=DIGEST, change_id="c", at=AT)
        )
    assert kept_releases({"fastm-demo": state}) == {1, 2, 3, 4, 5, 6, 7}, "the probe still keeps release 1"
    record = SiblingRecord(release=7, digest=DIGEST, change_id="c7", at=AT)
    after = followed(state, "fastm-demo-p-z", record)
    assert after.siblings["fastm-demo-p-z"] == record and after.history["fastm-demo-p-z"] == (7,)
    assert kept_releases({"fastm-demo": after}) == {2, 3, 4, 5, 6, 7}, "release 1's trees can go"
    assert (after.stable, after.idle, after.siblings["fastm-demo-p-a"]) == (
        state.stable,
        state.idle,
        state.siblings["fastm-demo-p-a"],
    )
