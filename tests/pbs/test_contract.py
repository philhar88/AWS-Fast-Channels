"""Contract tests of Prebid Server with the fast-ctv module (marker ``pbs``), against the jar and :mod:`local_mocks`.

- **No-hook contract:** third-party bidders are called only after a positive, in-time ``/policy`` answer. Module
  disabled, module absent (stock jar), ``/policy`` unreachable, slower than the module's timeout, cut by the account's
  group timeout, failing or malformed, module execution off for the account, hook ids absent from the plan: zero
  third-party calls, for every privacy mode.
- **Header isolation:** no ``X-Fast-*`` header and no ``User-Agent`` reaches a bidder.
- **Wire fields:** the pod fields reach OpenRTB 2.5 and 2.6 bidders intact; content metadata in ``app.content``,
  OpenRTB 2.6 fields in ``app.content.ext`` on Prebid Server 3.43.0.
- **The impression rule:** every rendered inline ad has an impression; ours is on every ad.
"""

import re
import time
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any

import pytest
from local_mocks import CONTENT, POLICY_TICKETS, POSITIVE, THIRD_PARTIES, Mocks
from pbs_process import ACCOUNTS, Pbs

from fast_channels.core.pbs_config import POD_SECONDS

if TYPE_CHECKING:
    import httpx

pytestmark = pytest.mark.pbs

FALLBACK_TICKETS = [ticket for ticket in POLICY_TICKETS if ticket not in POSITIVE]
ALL_TICKETS = list(POLICY_TICKETS)


def third_party_calls(mocks: Mocks, pbs: Pbs, ticket: str, **kwargs: Any) -> tuple[httpx.Response, int]:
    """One auction and the third-party calls it made (auctions run one at a time)."""
    started = time.time()
    response = pbs.auction(ticket, **kwargs)
    time.sleep(0.05)  # calls are recorded when they arrive; the auction has answered by then
    return response, len(mocks.third_party_calls(started))


def ads(vast: str) -> list[ET.Element]:
    root = ET.fromstring(vast)
    assert root.tag == "VAST"
    assert root.attrib["version"] == "4.2"
    return root.findall("Ad")


def test_a_positive_policy_answer_adds_the_simulator_and_the_pod_is_vast(mocks: Mocks, pbs: Pbs) -> None:
    response, calls = third_party_calls(mocks, pbs, "FULL1")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert calls == 2  # amt and echo26
    pod = ads(response.text)
    winners = [(ad.findtext("InLine/AdTitle"), ad.get("sequence")) for ad in pod if ad.get("sequence")]
    assert winners == [("sim1", "1"), ("sim2", "2"), ("sim3", "3"), ("house2", "4")]  # sim4 repeats sim3's domain
    assert all(ad.get("sequence") is None for ad in pod[len(winners) :])  # the standalones follow


@pytest.mark.parametrize("ticket", ALL_TICKETS)
def test_no_third_party_call_without_a_positive_in_time_answer(mocks: Mocks, pbs: Pbs, ticket: str) -> None:
    response, calls = third_party_calls(mocks, pbs, ticket)

    assert response.status_code == 200
    assert calls == (2 if ticket in POSITIVE else 0), POLICY_TICKETS[ticket]
    assert ads(response.text), "the own book's house ads fill the pod"


@pytest.mark.parametrize(
    ("variant", "ticket"),
    [
        *((variant, ticket) for variant in ("module-off", "absent-hooks") for ticket in ("FULL1", "CTX1")),
        # late1 is answered in time for the module (25 ms of 50) and for the shipped account (group 80 ms), but past
        # this account's 5 ms group timeout: Prebid Server's group executor drops the hook's result.
        ("group-cut", "late1"),
    ],
)
def test_accounts_without_a_complete_policy_hook_call_no_third_party(
    mocks: Mocks, pbs: Pbs, variant: str, ticket: str
) -> None:
    _, calls = third_party_calls(mocks, pbs, ticket, account=ACCOUNTS[variant])

    assert calls == 0


def test_an_unreachable_policy_calls_no_third_party(mocks: Mocks, pbs: Pbs) -> None:
    with mocks.policy_unreachable():
        for ticket in ALL_TICKETS:
            response, calls = third_party_calls(mocks, pbs, ticket)
            assert (response.status_code, calls) == (200, 0), ticket
            assert ads(response.text)


@pytest.mark.parametrize("ticket", ["FULL1", "PSEUDO1", "CTX1", "SAFE1", "KIDS1"])
def test_module_disabled_host_wide_calls_no_third_party(mocks: Mocks, pbs_module_disabled: Pbs, ticket: str) -> None:
    response, calls = third_party_calls(mocks, pbs_module_disabled, ticket)

    assert calls == 0
    assert response.headers["content-type"].startswith("application/json")  # MediaTailor refuses it: the slate


@pytest.mark.parametrize("ticket", ["FULL1", "PSEUDO1", "CTX1", "SAFE1", "KIDS1"])
def test_the_stock_jar_calls_no_third_party(mocks: Mocks, pbs_stock: Pbs, ticket: str) -> None:
    response, calls = third_party_calls(mocks, pbs_stock, ticket)

    assert calls == 0
    assert response.headers["content-type"].startswith("application/json")


def test_no_client_header_reaches_a_bidder(mocks: Mocks, pbs: Pbs) -> None:
    started = time.time()
    pbs.auction("FULL1", headers={"X-Fast-Probe": "0", "X-Fast-Buffet": "1", "x-fast-ctx": "c" * 100})

    bidder_calls = [c for c in mocks.calls if c.at >= started and c.path in (*THIRD_PARTIES, "/ownbook/openrtb")]
    assert {c.path for c in bidder_calls} == {"/amt", "/echo26", "/ownbook/openrtb"}
    for call in bidder_calls:
        assert not [name for name in call.header_names if name.startswith("x-fast")], call.path
        assert "user-agent" not in call.header_names, call.path
        assert "x-forwarded-for" not in call.header_names, call.path


@pytest.mark.parametrize("path", ["/amt", "/echo26", "/ownbook/openrtb"])
def test_pod_fields_reach_bidders_intact(mocks: Mocks, pbs: Pbs, path: str) -> None:
    started = time.time()
    pbs.auction("FULL1", seconds=90)

    [call] = mocks.calls_to(path, started)
    imps = call.body["imp"]
    assert [imp["video"].get("poddur") for imp in imps] == [90, 90, 90]
    assert [imp["video"].get("podid") for imp in imps] == [1, 1, 1]
    assert [imp["video"].get("slotinpod") for imp in imps] == [1, 0, -1]
    assert [imp["video"].get("rqddurs") for imp in imps] == [[30]] * 3
    assert [imp.get("ssai") for imp in imps] == [3] * 3


def test_content_metadata_reaches_bidders_with_2_6_fields_in_ext_on_3_43(mocks: Mocks, pbs: Pbs) -> None:
    started = time.time()
    pbs.auction("CTX1")

    for path in THIRD_PARTIES:
        [call] = mocks.calls_to(path, started)
        content = call.body["app"]["content"]
        assert {key: content[key] for key in ("id", "title", "genre", "cat", "cattax", "len", "language")} == {
            key: CONTENT[key] for key in ("id", "title", "genre", "cat", "cattax", "len", "language")
        }, path
        assert content["ext"] == CONTENT["ext"], path
        assert content["livestream"] == 1, "the stored request's constants stay"


def test_the_policy_patch_reaches_the_request_and_ids_stay_with_the_own_book(mocks: Mocks, pbs: Pbs) -> None:
    started = time.time()
    pbs.auction("CTX1")

    [simulator] = mocks.calls_to("/amt", started)
    [own_book] = mocks.calls_to("/ownbook/openrtb", started)
    assert simulator.body["device"]["devicetype"] == 3
    assert simulator.body["device"]["ip"] == "198.51.100.0", "precise geolocation stays with the own book"
    assert own_book.body["device"]["ip"] == "198.51.100.7"


def test_every_inline_ad_has_an_impression_and_ours(mocks: Mocks, pbs: Pbs) -> None:
    response = pbs.auction("FULL1")

    pod = ads(response.text)
    assert pod
    for ad in pod:
        impressions = [i.text or "" for i in ad.findall("InLine/Impression")]
        assert impressions, ad.findtext("InLine/AdTitle")
        crid = ad.findtext("InLine/AdTitle")
        assert f"https://cdn.example/ads/b.gif?d=sid-contract.820461826&c={crid}&e=impression&enr=0" in impressions
    [sim1] = [ad for ad in pod if ad.findtext("InLine/AdTitle") == "sim1"]
    assert len(sim1.findall("InLine/Impression")) == 1, "sim1 has no impression of its own and no notices"


def test_when_select_fails_the_pod_is_house_ads(mocks: Mocks, pbs: Pbs) -> None:
    mocks.select_fails = True
    try:
        response = pbs.auction("FULL1")
    finally:
        mocks.select_fails = False

    titles = [ad.findtext("InLine/AdTitle") for ad in ads(response.text)]
    assert titles
    assert all(title is not None and title.startswith("house") for title in titles)


@pytest.mark.parametrize("seconds", POD_SECONDS)
def test_every_grid_duration_has_a_pod(pbs: Pbs, seconds: int) -> None:
    response = pbs.auction("CTX1", seconds=seconds)

    assert response.status_code == 200
    assert len([ad for ad in ads(response.text) if ad.get("sequence")]) >= 1


@pytest.mark.parametrize("seconds", [31, 300])
def test_an_off_grid_duration_is_refused(pbs: Pbs, seconds: int) -> None:
    """MediaTailor rounds half up and sends 300 s for a missing duration: no such stored request, so 400 and the
    slate."""
    assert pbs.auction("CTX1", seconds=seconds).status_code == 400


def test_the_module_logs_never_carry_the_ticket(pbs: Pbs) -> None:
    pbs.auction("FULL1")

    log = pbs.log.read_text()
    assert re.search(r'"evt":"fast-ctv-request"', log)
    assert re.search(r'"evt":"fast-ctv-pod"', log)
    assert "FULL1" not in log
    assert "contract-origin-secret" not in log


def test_2_6_bidders_get_native_2_6_fields_and_2_5_bidders_the_down_conversion(mocks: Mocks, pbs: Pbs) -> None:
    started = time.time()
    pbs.auction("CTX1")

    for path in ("/ownbook/openrtb", "/echo26"):
        [call] = mocks.calls_to(path, started)
        assert call.body["source"]["schain"]["nodes"][0]["sid"] == "fastpbs", path
    [simulator] = mocks.calls_to("/amt", started)
    assert "schain" not in simulator.body["source"]
    assert simulator.body["source"]["ext"]["schain"]["nodes"][0]["sid"] == "fastpbs"
