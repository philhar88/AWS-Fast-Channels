"""The crawl module's PRE_ADS tree in the release bundle (DESIGN §9.4): shape, mapping and the validators it meets."""

from types import SimpleNamespace
from typing import Any

import pytest

from fast_channels.core.crawl.functions import (
    CACHE_KEY_PREFIX,
    EXECUTOR_BUDGET_MS,
    HEADER_OUTPUT,
    UPID_PRESENT,
    CrawlContribution,
    lookup_url,
    pa_meta,
)
from fast_channels.core.monetization.bundle import BundleContribution
from fast_channels.core.monetization.functions import Hook
from fast_channels.core.monetization.playback import ADS_REQUEST_TIMEOUT_MS, LIVE_MAXIMUM_MS
from fast_channels.core.monetization.validators import Rule, validate_bundle


@pytest.fixture
def crawled(kit: SimpleNamespace) -> Any:
    return kit.make_bundle(contributions=[CrawlContribution()])


def test_the_bundle_with_the_lookup_breaks_no_rule(crawled: Any) -> None:
    assert validate_bundle(crawled) == []
    assert isinstance(CrawlContribution(), BundleContribution)


def test_the_general_siblings_map_one_deployment_wide_tree_and_the_probe_none(crawled: Any) -> None:
    mapping = {s.letter: s.playback.function_mapping for s in crawled.siblings}
    assert mapping["a"][Hook.PRE_ADS] == mapping["b"][Hook.PRE_ADS] == "fastm_pa_root"
    assert Hook.PRE_ADS not in mapping["z"]
    root = crawled.function("fastm_pa_root")
    executor = root.sequential_executor_configuration
    assert executor.timeout_milliseconds == EXECUTOR_BUDGET_MS == 400
    assert [(ref.function_id, ref.run_condition) for ref in executor.function_list] == [("fastm_pa_meta", UPID_PRESENT)]
    assert executor.output == {HEADER_OUTPUT: "{% temp.c_ok = true ? temp.c_hdr : '' %}"}


def test_the_lookup_is_the_designed_function(kit: SimpleNamespace) -> None:
    """The same document lane E's validator tests use as the crawl shape (``kit.pa_meta``), DESIGN §9.4 with the
    header value capped for R-F1."""
    function = pa_meta(kit.PREFIX, content_base_url=kit.CDN)
    assert function == kit.pa_meta()
    http = function.http_request_configuration
    assert http.cache.key == f"{{% '{CACHE_KEY_PREFIX}' & $string(scte.segmentation_upid) %}}"
    assert (http.cache.ttl_minimum_seconds, http.cache.ttl_maximum_seconds, http.request_timeout_milliseconds) == (
        300,
        300,
        250,
    )
    assert "player_params" not in function.model_dump_json() and "session." not in function.model_dump_json()
    assert pa_meta(kit.PREFIX, content_base_url=f"{kit.CDN}/") == function  # a trailing slash changes nothing


def test_released_ids(crawled: Any) -> None:
    released = {f.function_id for f in crawled.released_functions(7, tags={"fast:release": "r0007"})}
    assert {"fastm_pa_meta_r0007", "fastm_pa_root_r0007"} <= released


def test_the_live_maximum_must_cover_the_lookup(kit: SimpleNamespace, crawled: Any) -> None:
    """R-P3: PRE_ADS time is taken from the ad request's wall clock (P0-03)."""
    assert LIVE_MAXIMUM_MS >= EXECUTOR_BUDGET_MS + ADS_REQUEST_TIMEOUT_MS

    def shrink(document: dict[str, Any]) -> None:
        timeouts = kit.playback_doc(document)["AdsPersonalizationTimeouts"]
        timeouts["LiveMaximumAdsPersonalizationTimeMilliseconds"] = EXECUTOR_BUDGET_MS + ADS_REQUEST_TIMEOUT_MS - 1

    assert Rule.P3 in kit.rules(validate_bundle(kit.edit(crawled, shrink)))


def test_an_unbounded_header_is_refused(kit: SimpleNamespace, crawled: Any) -> None:
    """R-F1: the design's bare ``response.body.hdr`` cannot be bounded, so the lookup caps it at 1,000."""

    def bare(document: dict[str, Any]) -> None:
        kit.function_doc(document, "fastm_pa_meta")["HttpRequestConfiguration"]["Output"]["temp.c_hdr"] = (
            "{% response.body.hdr %}"
        )

    assert Rule.F1 in kit.rules(validate_bundle(kit.edit(crawled, bare)))


def test_a_lookup_that_reads_the_session_is_refused(kit: SimpleNamespace, crawled: Any) -> None:
    """R-F5: the lookup's URL, headers and cache key read only the SCTE-35 ids."""

    def leak(document: dict[str, Any]) -> None:
        http = kit.function_doc(document, "fastm_pa_meta")["HttpRequestConfiguration"]
        http["Headers"]["X-Viewer"] = "{% session.uuid %}"

    assert Rule.F5 in kit.rules(validate_bundle(kit.edit(crawled, leak)))


@pytest.mark.parametrize("base", ["http://cdn.example.com", "https://cdn.example.com/it's", "cdn.example.com"])
def test_the_lookup_needs_an_https_base_url(base: str) -> None:
    with pytest.raises(ValueError, match="https://"):
        lookup_url(base)
