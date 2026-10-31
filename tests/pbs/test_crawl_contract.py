"""The content enrichment's contract tests (DESIGN §9.9, BUILD-PLAN M17-04; marker ``pbs``), against the jar and
:mod:`local_mocks`, whose policy answer is the crawl module's contributor on the sample catalog.

- **T1:** with the PRE_ADS lookup absent, failing or timing out (no ``x-fast-ctx`` header, or one that is not a
  context), every bidder's request equals the control (a holdout break) except the enrichment: for a title in the
  PDS's copy it is the copy's ``app.content``, for any other title nothing. A holdout break ignores the header.
- **T2:** every mapped field reaches the bidders natively (Prebid Server Java 4.4.0). ``app.content.data`` reaches
  only the bidders of the answer's first-party-data rule (the own book).
"""

import copy
import time
from typing import TYPE_CHECKING, Any, Final

import pytest
from local_mocks import (
    CONTEXTS,
    CRAWL,
    ENRICHED_SID,
    HOLDOUT_SID,
    LATE_HEADER,
    LATE_TITLE,
    PUBLISHER_DOMAIN,
    THIRD_PARTIES,
    TITLE,
    Mocks,
)

from fast_channels.core.crawl.ortb_content import NAMING
from fast_channels.core.pds.models import Mode, PolicyRequest

if TYPE_CHECKING:
    from pbs_process import Pbs

pytestmark = pytest.mark.pbs

BIDDERS: Final = (*THIRD_PARTIES, "/ownbook/openrtb")
EVID: Final = "820461826"
NEWER: Final = ("genres", "gtax", "realtime", "firstbroadcast", "data")
"""The OpenRTB 2.6-202505 content fields (first-class since Prebid Server Java 4.4.0)."""


def bid_requests(mocks: Mocks, pbs: Pbs, *, sid: str, upid: str, ctx: str | None = None) -> dict[str, dict[str, Any]]:
    """Each bidder's request for one contextual break of ``upid`` in session ``sid``, with ``x-fast-ctx`` when given,
    without the fields that differ between any two auctions (their ids and ``tmax``)."""
    started = time.time()
    headers = {"X-Fast-Sid": sid} | ({"x-fast-ctx": ctx} if ctx is not None else {})
    response = pbs.auction("", headers=headers, query=f"evid={EVID}&upid={upid}&dur=120120")
    assert response.status_code == 200, response.text
    time.sleep(0.05)  # calls are recorded when they arrive; the auction has answered by then
    requests = {}
    for path in BIDDERS:
        [call] = mocks.calls_to(path, started)
        requests[path] = _stable(call.body)
    return requests


def _stable(body: dict[str, Any]) -> dict[str, Any]:
    stable = copy.deepcopy(body)
    stable.pop("id", None)
    stable.pop("tmax", None)  # what is left of the auction's time
    stable.get("source", {}).pop("tid", None)
    for imp in stable.get("imp", []):
        imp.get("ext", {}).pop("tid", None)
    return stable


def without_enrichment(request: dict[str, Any], enrichment: dict[str, Any]) -> dict[str, Any]:
    """``request`` with the fields ``enrichment`` added to ``app.content`` taken out again."""
    stripped = copy.deepcopy(request)
    content = stripped["app"]["content"]
    for key in enrichment:
        if key == "ext":
            for inner in enrichment["ext"]:
                content.get("ext", {}).pop(inner, None)
            if content.get("ext") == {}:
                content.pop("ext")
        elif key not in {"cattax"}:  # the stored request's constant, which the enrichment repeats
            content.pop(key, None)
    return stripped


def expected(upid: str, ctx: str = "") -> dict[str, Any]:
    """What the contributor patches in for an enriched contextual break."""
    request = PolicyRequest(v=1, sid=ENRICHED_SID, pc="fastpbs-demo-p-a", evid=EVID, upid=upid, dur_ms=0, ctx=ctx)
    return dict(CRAWL.contribute(request, Mode.CONTEXTUAL).app_content)


# --- T1 -------------------------------------------------------------------------------------------------------------


def test_t1_a_holdout_break_is_the_control_and_ignores_the_header(mocks: Mocks, pbs: Pbs) -> None:
    control = bid_requests(mocks, pbs, sid=HOLDOUT_SID, upid=TITLE)
    for path, request in control.items():
        content = request["app"]["content"]
        assert "title" not in content and "id" not in content, path
        assert (content["livestream"], content["context"], content["cattax"]) == (1, 1, 9), "the stored constants"
    assert bid_requests(mocks, pbs, sid=HOLDOUT_SID, upid=TITLE, ctx=CONTEXTS[TITLE].hdr) == control


@pytest.mark.parametrize(
    "ctx",
    [None, "", "v9;id=late-title", "v1;id=late-title;g=not,valid ids"],
    ids=["lookup absent or timed out", "empty header", "another schema", "malformed"],
)
def test_t1_without_a_usable_lookup_an_unknown_title_is_the_control(mocks: Mocks, pbs: Pbs, ctx: str | None) -> None:
    control = bid_requests(mocks, pbs, sid=HOLDOUT_SID, upid=LATE_TITLE)
    assert bid_requests(mocks, pbs, sid=ENRICHED_SID, upid=LATE_TITLE, ctx=ctx) == control


@pytest.mark.parametrize("ctx", [None, "", "v1;id=sintel"], ids=["no header", "empty header", "another title's"])
def test_t1_a_title_in_the_copy_is_enriched_whatever_the_lookup_did(mocks: Mocks, pbs: Pbs, ctx: str | None) -> None:
    control = bid_requests(mocks, pbs, sid=HOLDOUT_SID, upid=TITLE)
    with_lookup = bid_requests(mocks, pbs, sid=ENRICHED_SID, upid=TITLE, ctx=CONTEXTS[TITLE].hdr)
    assert bid_requests(mocks, pbs, sid=ENRICHED_SID, upid=TITLE, ctx=ctx) == with_lookup
    enrichment = expected(TITLE)
    for path, request in with_lookup.items():
        assert without_enrichment(request, enrichment) == control[path], path


def test_t1_the_header_enriches_a_title_the_copy_lacks(mocks: Mocks, pbs: Pbs) -> None:
    control = bid_requests(mocks, pbs, sid=HOLDOUT_SID, upid=LATE_TITLE)
    enriched = bid_requests(mocks, pbs, sid=ENRICHED_SID, upid=LATE_TITLE, ctx=LATE_HEADER)
    enrichment = expected(LATE_TITLE, LATE_HEADER)
    assert (enrichment["id"], enrichment["genre"], enrichment["len"]) == (LATE_TITLE, "Animation & Anime", 60)
    for path, request in enriched.items():
        assert without_enrichment(request, enrichment) == control[path], path
        assert "title" not in request["app"]["content"], "the header carries no title"


# --- T2 -------------------------------------------------------------------------------------------------------------


def test_t2_every_mapped_field_reaches_the_bidders(mocks: Mocks, pbs: Pbs) -> None:
    sent = expected(TITLE)
    assert set(NAMING) - {"series", "season", "episode"} <= set(sent) | set(sent.get("ext", {}))
    received = bid_requests(mocks, pbs, sid=ENRICHED_SID, upid=TITLE)
    for path, request in received.items():
        content = request["app"]["content"]
        for key, value in sent.items():
            if key not in {"ext", *NEWER}:
                assert content.get(key) == value, (path, key, content.get(key))
        for key in NEWER:
            if key == "data" and path != "/ownbook/openrtb":
                assert "data" not in content, f"{path}: data is first-party data, for the own book only"
                continue
            assert content.get(key) == sent[key], (path, key, content.get(key))
        assert "ext" not in content or not set(NEWER) & set(content["ext"]), f"{path}: native, not in ext"


def test_t2_the_registry_ids_reach_the_own_book_alone(mocks: Mocks, pbs: Pbs) -> None:
    """``app.content.data`` is first-party data, which the answer's ``fpd.bidders`` rule gives the own book alone
    (ADR 0031). It holds catalog ids only."""
    cids = [{"name": PUBLISHER_DOMAIN, "cids": [TITLE]}]
    received = bid_requests(mocks, pbs, sid=ENRICHED_SID, upid=TITLE)
    for path, request in received.items():
        content = request["app"]["content"]
        assert content.get("data") == (cids if path == "/ownbook/openrtb" else None), path
        assert "data" not in content.get("ext", {}), path
