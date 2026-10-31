"""The demand module's edge, on stages that deployed it (``make deploy-demand``; skipped otherwise): MediaTailor's POST
through the guidance's CloudFront, WAF and Prebid Server with the fast-ctv module, until the Pod Decision Service
exists (M2): every break length from 30 to 120 s, on the grid or not, answers a VAST 4.2 pod of one slot per whole
30 s, a zero or missing (300 s) length an empty one; the origin rule group keeps out everything but the stage's
MediaTailor requests, an account without the module calls no third party, and a small break herd is served.

Independent of the channel tests; about two minutes.
"""

import time
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Final

import pytest
from _demand import DemandStage
from harness_demand import DemandEdge
from prebid_guidance import stage_config

from fast_channels.core.pbs_config import POD_SECONDS, SLOT_SECONDS

if TYPE_CHECKING:
    import httpx
    from _stack import Stage

pytestmark = pytest.mark.e2e

HERD_RATE: Final = 100
HERD_SECONDS: Final = 6
HERD_P99_MS: Final = 1000


@pytest.fixture(scope="module")
def edge(stage: Stage) -> DemandEdge:
    found = DemandEdge(stage.prefix, stage.session)
    if not found.deployed:
        pytest.skip(f"stage {stage.prefix} has no demand module deployed (no /{stage.prefix}/demand/ contract)")
    return found


def vast_pod(response: httpx.Response) -> list[ET.Element]:
    assert response.status_code == 200, response.text[:300]
    assert response.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(response.text)
    assert (root.tag, root.get("version")) == ("VAST", "4.2")
    return root.findall("Ad")


@pytest.mark.parametrize("seconds", sorted({*POD_SECONDS, 48}))
def test_every_break_length_in_range_answers_its_pod(edge: DemandEdge, seconds: int) -> None:
    """One stored request for every length: the module shapes the pod from ``ext.fast.pod_s``. 48 s is off the
    grid, as a mid-break joiner's remainder is: one 30 s slot, which the house ads fill (the probe asks no one)."""
    sequenced = [ad for ad in vast_pod(edge.auction(seconds)) if ad.get("sequence")]

    assert 1 <= len(sequenced) <= seconds // SLOT_SECONDS, [ad.findtext("InLine/AdTitle") for ad in sequenced]


@pytest.mark.parametrize("seconds", [0, 20, 300])
def test_a_break_length_out_of_range_answers_an_empty_pod(edge: DemandEdge, seconds: int) -> None:
    """300 s is what MediaTailor sends for a missing duration: the module rejects the auction and answers an empty
    VAST 4.2 document, so the slate plays."""
    assert vast_pod(edge.auction(seconds)) == []


def test_only_the_stages_mediatailor_requests_pass_the_edge(edge: DemandEdge) -> None:
    assert edge.auction(60, secret=False).status_code == 403, "an auction without the origin secret"
    assert edge.auction(60, headers={"X-Fast-Origin": "not-the-secret"}).status_code == 403
    assert edge.get("/info/bidders").status_code == 403, "any other path"
    assert edge.get("/status").status_code in (200, 204), "the health check"


def test_an_account_without_the_module_calls_no_third_party(edge: DemandEdge) -> None:
    started = int(time.time() * 1000)

    response = edge.auction(60, account=f"{edge.prefix}-no-module")
    time.sleep(10)  # the simulator's log line arrives within seconds

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json"), "no fast-ctv exitpoint: Prebid's JSON"
    assert edge.simulator_requests(started) == 0


def test_a_small_break_herd_is_served(stage: Stage, edge: DemandEdge) -> None:
    demand = DemandStage(stage_config(None, stage.prefix, stage.region), None, stage.session)

    herd = demand.herd(HERD_RATE, HERD_SECONDS)

    assert herd["requests"] == HERD_RATE * HERD_SECONDS
    assert herd["non_200_ratio"] == 0, herd["status_codes"]
    assert herd["p99_ms"] < HERD_P99_MS, herd
