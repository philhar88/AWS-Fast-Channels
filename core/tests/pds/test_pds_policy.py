"""Privacy modes, the /policy patch and the fixtures shared with the Prebid Server module."""

import json
from ipaddress import IPv4Address, IPv6Address, ip_network
from pathlib import Path
from typing import Any

import pytest

from fast_channels.core.pds.contributors import Contribution
from fast_channels.core.pds.models import DeviceType, Mode, PolicyRequest, PolicyResponse, Reason
from fast_channels.core.pds.policy import (
    ANONYMOUS,
    Consent,
    Demand,
    Resolution,
    Viewer,
    decide,
    is_kids_configuration,
    policy_response,
    resolve,
    ticket_status,
)
from fast_channels.core.pds.xff import IpRule

FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures" / "pds" / "policy"
ORIGIN_FACING = (ip_network("130.176.0.0/18"),)
DEMAND = Demand({"amt": {}, "other": {"placementId": "p"}})
TICKET = "AbCdEfGhIjKlMnOpQrStUv"
VIEWER = Viewer(
    vid="v1", cid="c1", arm="b", tier=2, gpp="DBABMA~CPXxRfAPXxRfAAfKABENB-CgAAAAAAAAAAYgAAAAAAAA", gpp_sid=(7,)
)
XFF = "81.2.69.160, 130.176.12.7, 3.81.20.30, 130.176.40.9"


def request(**fields: Any) -> PolicyRequest:
    values: dict[str, Any] = {"v": 1, "sid": "s-1", "pc": "fast-demo-p-a", "evid": "77", "dur_ms": 60060, "xff": XFF}
    return PolicyRequest(**(values | fields))


@pytest.mark.parametrize(
    ("ticket", "status"),
    [("", Reason.MISSING), ("abc", Reason.INVALID), (TICKET[:21], Reason.INVALID), (TICKET, None)],
)
def test_ticket_status(ticket: str, status: Reason | None) -> None:
    assert ticket_status(ticket) is status


@pytest.mark.parametrize(
    ("pc", "kids"),
    [
        ("fast-demo-p-k", True),
        ("fast-demo-p-l", True),
        ("fast-demo-p-a", False),
        ("fast-demo-p-z", False),
        ("fast-demo", False),
        ("-p-k", False),
        ("fast-kids-demo-p-b", False),
    ],
)
def test_kids_configurations(pc: str, kids: bool) -> None:
    assert is_kids_configuration(pc) is kids


@pytest.mark.parametrize(
    "failure",
    [Reason.MISSING, Reason.INVALID, Reason.UNKNOWN, Reason.REPLAY, Reason.PC_MISMATCH, Reason.EXPIRED_OR_RACED],
)
def test_identity_failures_are_contextual_with_every_viewer_parameter_zeroed(failure: Reason) -> None:
    for consent in (*Consent, None):
        assert resolve(failure, viewer=VIEWER, consent=consent) == Resolution(Mode.CONTEXTUAL, failure, ANONYMOUS)
    assert Viewer(vid=None, cid=None, arm=None, tier=None, gpp=None, gpp_sid=()) == ANONYMOUS


@pytest.mark.parametrize(
    ("consent", "expected"),
    [
        (Consent.FULL, Resolution(Mode.FULL, Reason.BOUND, VIEWER)),
        (Consent.PSEUDONYMOUS, Resolution(Mode.PSEUDONYMOUS, Reason.BOUND, VIEWER)),
        (Consent.OPTED_OUT, Resolution(Mode.CONTEXTUAL, Reason.OPTED_OUT, Viewer(gpp=VIEWER.gpp, gpp_sid=(7,)))),
        (Consent.UNAVAILABLE, Resolution(Mode.SAFE, Reason.CONSENT_UNAVAILABLE)),
        (None, Resolution(Mode.SAFE, Reason.CONSENT_UNAVAILABLE)),
    ],
)
def test_bound_viewers_by_consent(consent: Consent | None, expected: Resolution) -> None:
    assert resolve(Reason.BOUND, viewer=VIEWER, consent=consent) == expected


def test_kids_and_probe_come_first() -> None:
    kids = Resolution(Mode.KIDS, Reason.KIDS)
    assert resolve(Reason.BOUND, viewer=VIEWER, consent=Consent.FULL, probe=True, kids=True) == kids
    assert resolve(Reason.BOUND, viewer=VIEWER, consent=Consent.FULL, kids=True) == kids
    probe = Resolution(Mode.SAFE, Reason.PROBE)
    assert resolve(Reason.BOUND, viewer=VIEWER, consent=Consent.FULL, probe=True) == probe


def test_a_probe_on_a_kids_configuration_keeps_coppa() -> None:
    response = decide(request(pc="fast-demo-p-k", probe=True), DEMAND, origin_facing=()).response
    assert (response.mode, response.regs.coppa, response.bidders) == (Mode.KIDS, 1, {})


@pytest.mark.parametrize("mode", [Mode.SAFE, Mode.KIDS])
def test_safe_and_kids_add_no_bidder(mode: Mode) -> None:
    response = policy_response(request(), Resolution(mode, Reason.PROBE), DEMAND, ip=None, devicetype=None)
    assert response.bidders == {}
    assert response.regs.coppa == (1 if mode is Mode.KIDS else 0)
    assert response.fpd.bidders == ["ownbook"]


def test_third_party_modes_get_the_allow_list_minus_shed_bidders() -> None:
    shed = Demand(DEMAND.bidders, shed=frozenset({"other"}))
    response = policy_response(request(), resolve(Reason.MISSING), shed, ip=None, devicetype=None)
    assert response.bidders == {"amt": {}}
    full = policy_response(
        request(), resolve(Reason.BOUND, viewer=VIEWER, consent=Consent.FULL), DEMAND, ip=None, devicetype=None
    )
    assert full.bidders == {"amt": {}, "other": {"placementId": "p"}}
    assert full.regs.gpp == VIEWER.gpp and full.regs.gpp_sid == [7]
    assert full.user is None and full.deals == []  # FULL user data and deals arrive with identity


def test_device_fields_by_address_family() -> None:
    resolution = resolve(Reason.MISSING)
    v4 = policy_response(request(), resolution, DEMAND, ip=IPv4Address("81.2.69.160"), devicetype=DeviceType.PHONE)
    assert (v4.device.ip, v4.device.ipv6, v4.device.devicetype) == (IPv4Address("81.2.69.160"), None, 4)
    v6 = policy_response(request(), resolution, DEMAND, ip=IPv6Address("2a02:8010::1"), devicetype=None)
    assert (v6.device.ip, v6.device.ipv6) == (None, IPv6Address("2a02:8010::1"))
    none = policy_response(request(), resolution, DEMAND, ip=None, devicetype=None)
    assert none.model_dump(mode="json")["device"] == {"ip": None, "ipv6": None, "devicetype": None}


def test_decide_runs_the_chain() -> None:
    ua = "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/537.36 TV Safari/537.36"
    decision = decide(request(ua=ua, cfva="3.81.20.30:46532"), DEMAND, origin_facing=ORIGIN_FACING)
    response = decision.response
    assert (response.mode, response.reason, response.decision_id) == (Mode.CONTEXTUAL, Reason.MISSING, "s-1.77")
    assert response.device.ip == IPv4Address("81.2.69.160")
    assert response.device.devicetype is DeviceType.CONNECTED_TV
    assert decision.ip_rule is IpRule.OK
    assert response.app_content is None


def test_decide_identity() -> None:
    assert decide(request(t=TICKET), DEMAND, origin_facing=()).response.reason is Reason.UNKNOWN  # no lookup yet
    assert decide(request(t="short"), DEMAND, origin_facing=()).response.reason is Reason.INVALID
    bound = decide(
        request(t=TICKET), DEMAND, origin_facing=(), identity=Reason.BOUND, viewer=VIEWER, consent=Consent.PSEUDONYMOUS
    )
    assert bound.response.mode is Mode.PSEUDONYMOUS
    kids = decide(request(), DEMAND, origin_facing=(), kids_channel=True).response
    assert (kids.mode, kids.regs.coppa, kids.bidders) == (Mode.KIDS, 1, {})
    probe = decide(request(probe=True, pc="fast-demo-p-z"), DEMAND, origin_facing=()).response
    assert (probe.mode, probe.reason, probe.bidders) == (Mode.SAFE, Reason.PROBE, {})


class _Content:
    name = "crawl"

    def contribute(self, request: PolicyRequest, mode: Mode) -> Contribution:
        return Contribution(app_content={"id": request.upid}, record={"enr": 1})


def test_decide_merges_contributions() -> None:
    decision = decide(request(upid="tears-of-steel"), DEMAND, origin_facing=(), contributors=[_Content()])
    assert decision.response.app_content == {"id": "tears-of-steel"}
    assert decision.contributions.record == {"enr": 1}


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.stem)
def test_policy_fixtures_hold_what_the_rules_answer(path: Path) -> None:
    document = json.loads(path.read_text())
    context = document["context"]
    decision = decide(
        PolicyRequest.model_validate(document["request"]),
        Demand(context["demand"]),
        origin_facing=[ip_network(network) for network in context["origin_facing"]],
        kids_channel=context["kids_channel"],
    )
    assert PolicyResponse.model_validate(document["response"]) == decision.response
    assert decision.response.model_dump(mode="json") == document["response"]


def test_policy_fixtures_cover_the_viewer_ip_shapes() -> None:
    names = {path.stem for path in FIXTURES.glob("*.json")}
    assert {"contextual-missing", "contextual-spoofed-xff", "contextual-direct-session", "safe-probe"} <= names
    spoofed = json.loads((FIXTURES / "contextual-spoofed-xff.json").read_text())
    assert spoofed["request"]["xff"].startswith("203.0.113.7")
    assert spoofed["response"]["device"]["ip"] == "81.2.69.160"
