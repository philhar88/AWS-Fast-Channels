"""The v1 wire models: strict, versioned, consistent."""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from fast_channels.core.pds.models import (
    SCHEMA_VERSION,
    Deal,
    PolicyRequest,
    PolicyResponse,
    Reason,
    SelectRequest,
    SelectResponse,
    decision_id,
)

SID = "6f1c2a9e-4b7d-4e2a-9c1e-2d3f4a5b6c7d"
POLICY: dict[str, Any] = {"v": 1, "sid": SID, "pc": "fast-demo-p-a", "evid": "820461826", "dur_ms": 60060}
SLOTS = [{"impid": "s1", "poddur": 60, "rqddurs": [30]}, {"impid": "s2", "poddur": 60, "rqddurs": [30]}]
BID: dict[str, Any] = {"ref": "b1", "bidder": "amt", "impid": "s1", "price": 12.5}


def select(**fields: Any) -> dict[str, Any]:
    base = {
        "v": 1,
        "decision_id": f"{SID}.820461826",
        "sid": SID,
        "pc": "fast-demo-p-a",
        "evid": "820461826",
        "dur_ms": 60060,
        "mode": "FULL",
        "slots": SLOTS,
        "bids": [BID],
    }
    return base | fields


def test_policy_request_defaults_and_decision_id() -> None:
    request = PolicyRequest.model_validate(POLICY)
    assert (request.t, request.xff, request.cfva, request.ctx, request.probe) == ("", "", "", "", False)
    assert request.decision_id == decision_id(SID, "820461826") == f"{SID}.820461826"
    assert SCHEMA_VERSION == 1


@pytest.mark.parametrize(
    "change",
    [
        {"v": 2},
        {"t": "has space"},
        {"t": "a" * 23},
        {"sid": ""},
        {"sid": "x;y"},
        {"pc": "a/b"},
        {"evid": "12a"},
        {"dur_ms": -1},
        {"ctx": "x" * 1001},
        {"unknown": 1},
    ],
)
def test_policy_request_is_strict(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PolicyRequest.model_validate(POLICY | change)


def test_policy_response_defaults_are_the_empty_patch() -> None:
    response = PolicyResponse(decision_id="s.1", mode="CONTEXTUAL", reason=Reason.MISSING)
    assert response.model_dump(mode="json") == {
        "v": 1,
        "decision_id": "s.1",
        "mode": "CONTEXTUAL",
        "reason": "missing",
        "bidders": {},
        "regs": {"coppa": 0, "gpp": None, "gpp_sid": None},
        "device": {"ip": None, "ipv6": None, "devicetype": None},
        "app_content": None,
        "user": None,
        "fpd": {"bidders": []},
        "deals": [],
    }


def test_deals_carry_prices_as_numbers() -> None:
    deal = Deal(id="light-pod-1", bidfloor=Decimal("25.5"), at=3, wseat=["seat-1"])
    assert deal.model_dump(mode="json") == {"id": "light-pod-1", "bidfloor": 25.5, "at": 3, "wseat": ["seat-1"]}


def test_select_request_accepts_the_module_shape() -> None:
    request = SelectRequest.model_validate(select(mode=None))
    assert request.mode is None
    assert request.bids[0].price == Decimal("12.5")
    assert (request.bids[0].dur, request.bids[0].adm_kind, request.bids[0].house) == (None, None, False)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"decision_id": f"{SID}.1"}, "decision_id must be"),
        ({"slots": [SLOTS[0], SLOTS[0]]}, "impids must be unique"),
        ({"bids": [BID, BID]}, "refs must be unique"),
        ({"bids": [BID | {"impid": "s9"}]}, "unknown imps"),
        ({"slots": []}, "at least 1"),
        ({"bids": [BID | {"price": -1}]}, "greater than or equal"),
        ({"bids": [BID | {"adm_kind": "Linear"}]}, "InLine"),
    ],
)
def test_select_request_is_consistent(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SelectRequest.model_validate(select(**change))


def test_select_response_needs_a_reason() -> None:
    with pytest.raises(ValidationError):
        SelectResponse.model_validate({"v": 1, "pod": []})
    with pytest.raises(ValidationError):
        SelectResponse.model_validate({"v": 1, "pod": [{"impid": "s1", "ref": "b", "sequence": 0}], "reason": "filled"})
