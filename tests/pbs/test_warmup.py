"""The warm-up's zero-recorder gate (W-12, marker ``pbs``): on a fresh Prebid Server whose every adapter endpoint is a
local recorder (``local_mocks``), warm-up probes on the warm stored request reach no recorder and create every warm
adapter's metric keys.

Prebid Server Java creates an adapter's per-request-type metric key in a ``HashMap`` without a lock
(``AdapterTypeMetrics.requestType``): the first concurrent auctions that reach a new key can fail with a
``ConcurrentModificationException`` (HTTP 500). The warm stored request names each configured adapter and answers it
from a stored bid response, so the warm-up creates the keys before real auctions arrive, and no partner sees a
synthetic request. The mock ``/policy`` answers the warm probe with every third party (``amt``, ``echo26``): the
module adds none of them to a warm request, and keeps only the stored-response ones. An adapter that builds more than
one request would be called for real: ``pbs_config.warm_adapters`` refuses every adapter this gate has not covered.
"""

import re
import time
from typing import TYPE_CHECKING, Final

import pytest

from fast_channels.core.pbs_config import WARM_VETTED_ADAPTERS

if TYPE_CHECKING:
    from local_mocks import Mocks
    from pbs_process import Pbs

pytestmark = pytest.mark.pbs

ATTEMPTS: Final = 5


def request_type_keys(metrics: str, adapter: str) -> list[str]:
    """The adapter's per-request-type counters in Prometheus's text format (``adapter.<a>.requests.type.<t>``)."""
    pattern = re.compile(rf"^adapter[_.]{re.escape(adapter)}[_.]requests[_.]type[_.]\S*", re.MULTILINE)
    return pattern.findall(metrics)


def test_the_warm_up_reaches_no_recorder_and_creates_every_warm_adapters_metric_keys(
    mocks: Mocks, pbs_fresh: Pbs
) -> None:
    before = pbs_fresh.metrics()
    for adapter in WARM_VETTED_ADAPTERS:
        assert request_type_keys(before, adapter) == [], f"{adapter}: a key exists before any auction"
    started = time.time()
    created: dict[str, list[str]] = {}
    for _ in range(ATTEMPTS):
        response = pbs_fresh.warm()
        assert response.status_code == 200, response.text[:300]
        assert response.text.startswith('<VAST version="4.2"'), response.text[:300]
        time.sleep(0.05)
        created = {adapter: request_type_keys(pbs_fresh.metrics(), adapter) for adapter in WARM_VETTED_ADAPTERS}
        if all(created.values()):
            break
    assert all(created.values()), f"no metric key after {ATTEMPTS} warm probes: {created}"
    print(f"\nmetric keys created by the warm-up: {created}")
    assert mocks.third_party_calls(started) == [], "a warm-up must never call a partner"
    log = pbs_fresh.log.read_text()
    assert "Request to real" not in log, "an adapter built more than one request and was called for real"
    line = next(line for line in log.splitlines() if '"evt":"fast-ctv-request"' in line and '"warm":true' in line)
    assert '"probe":true' in line
