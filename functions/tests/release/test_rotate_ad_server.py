"""The rotation with the ad-server route (M8-09): the auction Function carries the origin secret itself and is
immutable, so the switch creates every kept tree that carries it again under a new release with the new value, and
the retire refuses while a kept Function still carries the old one."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.ad_server import AdServer, AdServerContribution, ads_url
from fast_channels.core.monetization.bundle import AdServerRoute
from fast_channels.core.monetization.release import ReleaseStatus, applied
from fast_channels.core.pbs_config import stored_request_ids
from fast_channels.functions.release import apply, rotate

if TYPE_CHECKING:
    from fast_channels.core.monetization.functions import Function

AT = datetime(2026, 10, 30, 12, tzinfo=UTC)
CHANNEL = "fastm-demo"
SIBLINGS = [f"{CHANNEL}-p-a", f"{CHANNEL}-p-b", f"{CHANNEL}-p-z"]
PAIR = SIBLINGS[:2]
STORED = list(stored_request_ids(ad_server=True))
MOCK = "https://abc123.execute-api.us-east-1.amazonaws.com"


@pytest.fixture
def released(state: Any, kit: SimpleNamespace) -> SimpleNamespace:
    """fastm-demo released twice on the ad-server route: r0001 on its three siblings, then r0002 on its idle
    sibling b (b keeps r0002 and r0001)."""
    bundle = kit.make_bundle(
        stored_requests=STORED,
        contributions=[AdServerContribution(pbs_origin="https://d222222abcdef8.cloudfront.net")],
        ad_server=AdServerRoute(url=ads_url(AdServer.MOCK, mock_url=MOCK, sibling="{sibling}"), origin=True),
    )
    key = state.add(bundle)
    for release, change in ((1, "c1"), (2, "c2")):
        state.statuses[release] = ReleaseStatus(
            release=release, digest=bundle.digest, bundle_key=key, change_id=change, status="PROMOTED", at=AT
        )
    first = applied(None, channel=CHANNEL, siblings=SIBLINGS, release=1, digest=bundle.digest, change_id="c1", at=AT)
    second = applied(first, channel=CHANNEL, siblings=[PAIR[1]], release=2, digest=bundle.digest, change_id="c2", at=AT)
    state.states[CHANNEL] = (second, 2)
    state.requests = {"release-1": 1, "release-2": 2}  # the counter is at r0002
    return SimpleNamespace(bundle=bundle, key=key)


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, tuple[str, ...], str | None]]:
    """``apply.put_functions`` calls: (release, siblings, slot); the plans rewritten are not written."""
    calls: list[tuple[int, tuple[str, ...], str | None]] = []

    def put_functions(
        bundle_key: str, release: int, change_id: str, siblings: list[str], *, slot: str | None = None
    ) -> dict[str, list[str]]:
        calls.append((release, tuple(siblings), slot))
        return {"created": [], "existing": []}

    monkeypatch.setattr(apply, "put_functions", put_functions)
    monkeypatch.setattr(
        apply, "write_plan", lambda channel, state, configured, at: calls.append((0, (state.stable or "",), None))
    )
    return calls


def test_the_trees_that_carry_the_secret_are_the_pairs_kept_releases(state: Any, released: SimpleNamespace) -> None:
    """The pair runs the auction, the probe does not; b keeps two releases."""
    trees = rotate.origin_trees(state.channels([CHANNEL]), "x1")
    assert trees == {1: [PAIR[0], PAIR[1]], 2: [PAIR[1]]}


def test_the_switch_creates_them_again_with_the_new_value_and_moves_the_records(
    state: Any, ledger: Any, released: SimpleNamespace, created: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    puts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        rotate.aws, "client", lambda service: SimpleNamespace(put_playback_configuration=lambda **kw: puts.append(kw))
    )
    result = rotate.switch("b", "x1")

    assert created[:2] == [(3, tuple(PAIR), "b"), (4, (PAIR[1],), "b")], "one new release per old one, slot b"
    assert created[2] == (0, (PAIR[0],), None), "the stable sibling moved: its channel's plan is rewritten"
    after, _ = state.channel(CHANNEL)
    assert {n: r.release for n, r in after.siblings.items()} == {PAIR[0]: 3, PAIR[1]: 4, SIBLINGS[2]: 1}
    assert after.history == {PAIR[0]: (3,), PAIR[1]: (4, 3), SIBLINGS[2]: (1,)}
    assert (state.statuses[3].change_id, state.statuses[4].bundle_key) == ("x1", released.key)
    assert (state.statuses[3].recreated_from, state.statuses[4].recreated_from) == (1, 2)
    assert state.statuses[1].recreated_from is None
    mapping = {
        p["Name"]: (p["FunctionMapping"], p["AdDecisionServerConfiguration"]["HttpRequest"]["Headers"]) for p in puts
    }
    assert all(root.endswith("_r0003") for root in mapping[PAIR[0]][0].values())
    assert mapping[PAIR[1]][1]["X-Fast-Fv"] == "r0004" and mapping[PAIR[1]][1]["X-Fast-Origin"] == "secret-value-b"
    assert all(root.endswith("_r0001") for root in mapping[SIBLINGS[2]][0].values()), "the probe keeps its tree"
    assert result["switched"] == SIBLINGS and state.slot == "b"
    [entry] = [e for e in ledger.entries if e.action == "rotation.switch"]
    assert entry.detail["recreated"] == {
        f"{PAIR[0]}@r0001": "r0003",
        f"{PAIR[1]}@r0001": "r0003",
        f"{PAIR[1]}@r0002": "r0004",
    }
    assert "secret-value" not in str(entry.detail)


def test_the_re_creation_runs_once(state: Any, released: SimpleNamespace, created: list[Any]) -> None:
    """A retried switch allocates nothing twice: the releases this rotation created carry the new value."""
    first = rotate.recreate("b", "x1")
    calls = len(created)
    assert rotate.recreate("b", "x1") == {} and len(created) == calls
    assert first and len(state.requests) == 4


def test_the_auction_is_created_with_the_slot_the_rotation_asks_for(
    state: Any, released: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    put: list[Function] = []
    monkeypatch.setattr(apply, "put_function", lambda function: put.append(function) or True)
    apply.put_functions(released.key, 3, "x1", [PAIR[1]], slot="b")  # the current slot is a
    [auction] = [f for f in put if f.function_id == "fastm_demo_p_b_pa_auction_r0003"]
    request = auction.put_request()
    assert request["HttpRequestConfiguration"]["Headers"]["X-Fast-Origin"] == "secret-value-b"
    assert request["Tags"][apply.ORIGIN_SLOT_TAG] == "b"


def _listed(ids_slots: list[tuple[str, str | None]]) -> SimpleNamespace:
    items = [
        {
            "FunctionId": function_id,
            "FunctionType": "HTTP_REQUEST",
            "Tags": {"fast:owner": "release-controller", "fast:app": "fastm", **({"fast:origin-slot": s} if s else {})},
        }
        for function_id, s in ids_slots
    ]
    return SimpleNamespace(get_paginator=lambda name: SimpleNamespace(paginate=lambda: [{"Items": items}]))


@pytest.mark.parametrize(
    ("listed", "refused"),
    [
        ([("fastm_demo_p_b_pa_auction_r0002", "a")], True),  # b keeps r0002, which carries slot a
        ([("fastm_demo_p_b_pa_auction_r0002", "b"), ("fastm_demo_p_a_pa_auction_r0009", "a")], False),
        ([("fastm_demo_p_z_pa_auction_r0001", "a")], False),  # nobody runs it
    ],
)
def test_the_retire_refuses_while_a_kept_function_carries_the_old_value(
    state: Any, ledger: Any, released: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, listed: Any, refused: bool
) -> None:
    retired: list[str] = []
    monkeypatch.setattr(rotate, "_new_value", retired.append)
    monkeypatch.setattr(rotate, "admit_values", lambda values: None)
    monkeypatch.setattr(rotate.reaper.aws, "client", lambda service: _listed(listed))
    state.holder, state.owner = rotate.ROTATION, "x1"
    if refused:
        with pytest.raises(rotate.RefusedError, match="1 Functions a sibling keeps still carry slot a"):
            rotate.retire("a", "x1")
        assert not retired and state.holder == rotate.ROTATION, "both values stay admitted, the fail step unlocks"
    else:
        assert rotate.retire("a", "x1") == {"retired": "a"} and retired == ["a"]
