"""The demo panel's view of one session's decision records: joined per break, latest first, probes left out."""

import json
from typing import Any

import pytest

from fast_channels.functions.viewer.decisions import (
    AdLoad,
    PodAd,
    insights_query,
    parse_results,
    summarize,
    viewer_ids,
)

SID = "0f8e2b5c-3d0a-4a5e-9c1b-2f6d7e8a9b0c"
VID = "6b1f0c2d9e8a7b6c5d4e3f2a1b0c9d8e"


def policy(evid: str, *, at: str, mode: str = "FULL", reason: str = "bound", **extra: Any) -> dict[str, Any]:
    return {
        "record": "pds.policy",
        "decision_id": f"{SID}.{evid}",
        "sid": SID,
        "evid": evid,
        "pc": "fastm-demo-p-a",
        "probe": False,
        "mode": mode,
        "reason": reason,
        "dur_ms": 60060,
        "vid": VID,
        "@timestamp": at,
        **extra,
    }


def bid(ref: str, price: float, outcome: str, *, house: bool = False, bidder: str = "amt") -> dict[str, Any]:
    return {
        "ref": ref,
        "bidder": bidder,
        "seat": bidder,
        "impid": "1",
        "price": price,
        "dur": 30,
        "vast_dur_ms": 30030,
        "crid": f"cr-{ref}",
        "adomain": ["a.example"],
        "cat": [],
        "dealid": None,
        "house": house,
        "tier0": False,
        "outcome": outcome,
    }


def select(
    evid: str, *, at: str, tier: str | None, ad_load: dict[str, Any] | None, pod: list[str], bids: list[Any]
) -> dict[str, Any]:
    return {
        "record": "pds.select",
        "decision_id": f"{SID}.{evid}",
        "sid": SID,
        "evid": evid,
        "pc": "fastm-demo-p-a",
        "probe": False,
        "mode": "FULL",
        "dur_ms": 60060,
        "limit_ms": 60060,
        "poddur": 60,
        "reason": "partial" if len(pod) == 1 else "filled",
        "pod": pod,
        "standalone": [],
        "filled_ms": 30030 * len(pod),
        "bids": bids,
        "below_floor": 2,
        "tier": tier,
        "ad_load": ad_load,
        "@timestamp": at,
    }


LIGHT = {"policy": "light", "reason": "tier", "max_ads": 1, "bar_cpm": 20.0, "slots": 2}
STANDARD = {"policy": "standard", "reason": "control", "max_ads": None, "bar_cpm": 0.0, "slots": 2}


def test_a_light_break_shows_its_one_ad_the_policy_and_what_did_not_run() -> None:
    records = [
        policy("101", at="2026-11-05 20:04:00.120"),
        select(
            "101",
            at="2026-11-05 20:04:00.250",
            tier="light",
            ad_load=LIGHT,
            pod=["b2"],
            bids=[
                bid("b1", 12.5, "below_bar"),
                bid("b2", 35, "won"),
                bid("b3", 22.5, "ad_load"),
                bid("b4", 0.01, "standalone", house=True),
            ],
        ),
    ]

    [decision] = summarize(SID, records).breaks

    assert decision.decision_id == f"{SID}.101"
    assert decision.at == "2026-11-05T20:04:00.250+00:00"
    assert (decision.mode, decision.mode_reason, decision.tier, decision.pod_reason) == (
        "FULL",
        "bound",
        "light",
        "partial",
    )
    assert decision.ad_load == AdLoad(policy="light", reason="tier", max_ads=1, bar_cpm=20, slots=2)
    assert decision.ads == [PodAd(bidder="amt", price_cpm=35, house=False, deal=None, seconds=30.03)]
    assert (decision.filled_s, decision.break_s) == (30.03, 60.06)
    assert decision.dropped == {"ad_load": 1, "below_bar": 1, "below_floor": 2}


def test_breaks_come_latest_first_and_keep_what_they_have() -> None:
    records = [
        policy("101", at="2026-11-05 20:04:00.120"),
        select(
            "101",
            at="2026-11-05 20:04:00.250",
            tier="standard",
            ad_load=STANDARD,
            pod=["a", "b"],
            bids=[bid("a", 35, "won"), bid("b", 22.5, "won")],
        ),
        policy("102", at="2026-11-05 20:14:00.120", mode="CONTEXTUAL", reason="replay"),
        policy("103", at="2026-11-05 20:24:00.120", probe=True),
        {**policy("104", at="2026-11-05 20:34:00.120"), "sid": "another"},
    ]

    decisions = summarize(SID, records)

    assert [d.evid for d in decisions.breaks] == ["102", "101"]
    contextual, standard = decisions.breaks
    assert (contextual.mode, contextual.mode_reason, contextual.tier, contextual.ad_load, contextual.ads) == (
        "CONTEXTUAL",
        "replay",
        None,
        None,
        [],
    )
    assert [ad.price_cpm for ad in standard.ads] == [35, 22.5]
    assert standard.dropped == {"below_floor": 2}


def test_a_repeated_record_counts_once_with_its_latest_copy() -> None:
    first = select("101", at="2026-11-05 20:04:00.250", tier="light", ad_load=LIGHT, pod=[], bids=[])
    second = select(
        "101", at="2026-11-05 20:04:01.250", tier="light", ad_load=LIGHT, pod=["b"], bids=[bid("b", 35, "won")]
    )

    [decision] = summarize(SID, [first, second]).breaks

    assert len(decision.ads) == 1


def test_the_viewer_ids_come_from_the_policy_records_only() -> None:
    records = [policy("101", at="t"), {**policy("102", at="t"), "vid": None}, {"record": "pds.select", "vid": "x"}]
    assert viewer_ids(records) == {VID}


def test_the_query_takes_only_a_session_id() -> None:
    assert f'filter sid = "{SID}"' in insights_query(SID)
    with pytest.raises(ValueError, match="session id"):
        insights_query('x" or sid like /./ or "')


def test_results_parse_into_records_with_their_timestamp() -> None:
    rows = [
        [
            {"field": "@timestamp", "value": "2026-11-05 20:04:00.120"},
            {"field": "@message", "value": json.dumps({"record": "pds.policy"})},
        ],
        [{"field": "@timestamp", "value": "x"}, {"field": "@message", "value": "not json"}],
        [{"field": "@message", "value": "[1]"}],
    ]
    assert parse_results(rows) == [{"record": "pds.policy", "@timestamp": "2026-11-05 20:04:00.120"}]
