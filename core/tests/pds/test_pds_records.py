import json
from decimal import Decimal
from ipaddress import ip_network
from typing import Any

from fast_channels.core.pds.contributors import Contribution
from fast_channels.core.pds.models import AdmKind, Bid, Mode, PolicyRequest, SelectRequest
from fast_channels.core.pds.pod import Outcome, PodContext, select_pod
from fast_channels.core.pds.policy import Demand, decide
from fast_channels.core.pds.records import POLICY_RECORD, SELECT_RECORD, PolicyRecord, policy_record, select_record

TICKET = "AbCdEfGhIjKlMnOpQrStUv"
VIEWER_IP = "81.2.69.160"
UA = "Mozilla/5.0 (SMART-TV; Linux) Tizen"
XFF = f"{VIEWER_IP}, 130.176.12.7, 3.81.20.30, 130.176.40.9"


class _Crawl:
    name = "crawl"

    def contribute(self, request: PolicyRequest, mode: Mode) -> Contribution:
        return Contribution(app_content={"id": request.upid}, record={"enr": 1})


class _Broken:
    name = "broken"

    def contribute(self, request: PolicyRequest, mode: Mode) -> Contribution:
        raise RuntimeError


def test_policy_record_holds_outcomes_never_viewer_values() -> None:
    request = PolicyRequest(
        v=1,
        t=TICKET,
        sid="s-1",
        pc="fast-demo-p-a",
        fv="r0003",
        evid="77",
        upid="tears-of-steel",
        dur_ms=60060,
        ua=UA,
        xff=XFF,
        cfva="3.81.20.30:4000",
        ctx="ctx-secretish-value",
    )
    decision = decide(
        request, Demand({"amt": {}}), origin_facing=(ip_network("130.176.0.0/18"),), contributors=[_Crawl(), _Broken()]
    )
    record = policy_record(request, decision, latency_ms=3.25)
    assert record.model_dump(mode="json") == {
        "v": 1,
        "decision_id": "s-1.77",
        "sid": "s-1",
        "evid": "77",
        "pc": "fast-demo-p-a",
        "probe": False,
        "latency_ms": 3.25,
        "record": POLICY_RECORD,
        "upid": "tears-of-steel",
        "fv": "r0003",
        "dur_ms": 60060,
        "mode": "CONTEXTUAL",
        "reason": "unknown",
        "bidders": ["amt"],
        "devicetype": 3,
        "ip_rule": "ok",
        "ctx_len": 19,
        "contributors": ["crawl"],
        "skipped": ["broken"],
        "contributed": {"enr": 1},
    }
    text = record.model_dump_json()
    for value in (TICKET, VIEWER_IP, "3.81.20.30", UA, "ctx-secretish-value"):
        assert value not in text
    assert PolicyRecord.model_validate_json(text) == record


def _bid(ref: str, **fields: Any) -> Bid:
    values: dict[str, Any] = {
        "bidder": "amt",
        "impid": "s1",
        "price": Decimal("12.5"),
        "dur": 30,
        "vast_dur_ms": 30030,
        "crid": ref,
        "adomain": [f"{ref}.example"],
        "adm_kind": AdmKind.INLINE,
    }
    return Bid(ref=ref, **(values | fields))


def test_select_record_keeps_every_bid_at_or_above_the_floor() -> None:
    request = SelectRequest(
        v=1,
        decision_id="s-1.77",
        sid="s-1",
        pc="fast-demo-p-a",
        evid="77",
        dur_ms=60060,
        mode=Mode.FULL,
        slots=[{"impid": "s1", "poddur": 60, "rqddurs": [30]}, {"impid": "s2", "poddur": 60, "rqddurs": [30]}],
        bids=[
            _bid("line", bidder="ownbook", price=Decimal(8), dealid="d1"),
            _bid("paid"),
            _bid("cheap", impid="s2", price=Decimal("0.2")),
            _bid("other", impid="s2", price=Decimal(3)),
        ],
    )
    decision = select_pod(request, PodContext(tier0_deals={"d1"}, floor_cpm=Decimal(1)))
    record = select_record(request, decision, latency_ms=4.0)
    dumped = record.model_dump(mode="json")
    assert dumped["record"] == SELECT_RECORD
    assert (dumped["pod"], dumped["standalone"], dumped["reason"]) == (["line", "other"], ["paid"], "filled")
    assert (dumped["filled_ms"], dumped["limit_ms"], dumped["poddur"], dumped["below_floor"]) == (60060, 60060, 60, 1)
    assert [(b["ref"], b["outcome"], b["tier0"], b["price"]) for b in dumped["bids"]] == [
        ("line", "won", True, 8.0),
        ("paid", "standalone", False, 12.5),
        ("other", "won", False, 3.0),
    ]
    assert json.loads(record.model_dump_json())["bids"][1]["price"] == 12.5  # a number, not a string
    assert decision.outcomes["cheap"] is Outcome.FLOOR
