"""The crawl module's PRE_ADS tree (DESIGN §9.4): a cached lookup of the break's catalog context object, contributed
to the release bundle as a :class:`~fast_channels.core.monetization.bundle.BundleContribution`.

On every general sibling, the PRE_ADS root executor runs one ``HTTP_REQUEST``, ``<p>_pa_meta``: a GET of
``<content base>/catalog/ctx/v1/<content id>.json``, where the content id is the break's SCTE-35 UPID (``FST3`` +
content id; MediaTailor may or may not strip the format identifier, so both forms work). The response is cached for
300 s per playback configuration under ``meta|v1|<UPID>``: about 4 upstream calls per title per configuration per
cache window, whatever the audience (P0-10), and a missing title is a cached 404 too (the catalog route).

The root commits one value, the ``x-fast-ctx`` request header: the object's ``hdr`` when the lookup answered 200 with
a ``v1`` object, else empty. The lookup's URL, headers and cache key read only the UPID; nothing goes into
``player_params`` (the POST body carries none, DESIGN §9.4), and a failure, a timeout or a missing UPID sends the ad
request without the header (fail open).
"""

from collections.abc import Iterable
from typing import Final

from fast_channels.core.crawl.catalog_context import CACHE_CONTROL, CONTEXT_PREFIX, CONTEXT_VERSION, OUTPUT_MAX_CHARS
from fast_channels.core.monetization.bundle import HookTree, SiblingContext, Step
from fast_channels.core.monetization.functions import (
    Cache,
    CacheScope,
    Function,
    FunctionType,
    Hook,
    HttpRequestConfiguration,
    function_stem,
)
from fast_channels.core.monetization.playback import AdsRoute, SiblingRole

NAME: Final = "crawl"
META: Final = "pa_meta"
"""The lookup's name in FunctionIds (``<p>_pa_meta_rNNNN``)."""
HEADER: Final = "x-fast-ctx"
"""The request header the PBS module reads (``Signals`` in ``pbs/fast-ctv``) and the PDS takes as a fallback."""
HEADER_OUTPUT: Final = f"adsRequest.headers.{HEADER}"
REQUEST_TIMEOUT_MS: Final = 250
EXECUTOR_BUDGET_MS: Final = 400
"""The tree's share of the PRE_ADS root timeout. Every millisecond comes out of the ad request's wall clock (P0-03):
validator R-P3 keeps the Live and VOD maximum at least this plus ``AdsRequestTimeoutMilliseconds``."""
CACHE_SECONDS: Final = int(CACHE_CONTROL.removeprefix("max-age="))
CACHE_KEY_PREFIX: Final = f"meta|v{CONTEXT_VERSION}|"
CACHE_REASON: Final = (
    "The context object depends on the title alone (the break's UPID) and the publisher keeps it for 300 s, so every "
    "session and break of the title shares one answer."
)
"""Why the lookup is cached per title (validator R-F8)."""
UPID_PRESENT: Final = "{% $exists(scte.segmentation_upid) and $string(scte.segmentation_upid) != '' %}"
"""RunCondition: prefetched RECURRING breaks and breaks without a UPID skip the lookup."""


def lookup_url(content_base_url: str) -> str:
    """The lookup's ``Url`` expression: the context object of the break's UPID, with ``FST3`` removed if present.

    Raises:
        ValueError: ``content_base_url`` is not an https URL, or holds a character a JSONata string literal cannot.
    """
    base = content_base_url.rstrip("/")
    if not base.startswith("https://") or any(char in base for char in "'\\"):
        raise ValueError(f"the content base URL must be https:// without quotes or backslashes, got {base!r}")
    return (
        "{% ($u := $string(scte.segmentation_upid); "
        "$id := $substring($u, 0, 4) = 'FST3' ? $substring($u, 4) : $u; "
        f"'{base}/{CONTEXT_PREFIX}' & $encodeUrlComponent($id) & '.json') %}}"
    )


def pa_meta(prefix: str, *, content_base_url: str) -> Function:
    """``<p>_pa_meta``: the cached GET of the break's catalog context object (deployment-wide)."""
    return Function(
        function_id=function_stem(prefix, META),
        function_type=FunctionType.HTTP_REQUEST,
        description="Show-level content metadata by SCTE-35 UPID content id. Reads no session or player data.",
        http_request_configuration=HttpRequestConfiguration(
            method_type="GET",
            url=lookup_url(content_base_url),
            headers={"Accept": "application/json"},
            request_timeout_milliseconds=REQUEST_TIMEOUT_MS,
            cache=Cache(
                ttl_minimum_seconds=CACHE_SECONDS,
                ttl_maximum_seconds=CACHE_SECONDS,
                key=f"{{% '{CACHE_KEY_PREFIX}' & $string(scte.segmentation_upid) %}}",
                scope=CacheScope.TITLE,
                reason=CACHE_REASON,
            ),
            output={
                "temp.c_ok": f"{{% response.statusCode = 200 and response.body.v = {CONTEXT_VERSION} %}}",
                # Bounded for R-F1: the publisher keeps hdr within the limit, the expression proves it.
                "temp.c_hdr": f"{{% $substring($string(response.body.hdr), 0, {OUTPUT_MAX_CHARS}) %}}",
            },
        ),
    )


class CrawlContribution:
    """The PRE_ADS content lookup on every general sibling that POSTs to Prebid Server (not the house-only probe)."""

    name = NAME

    def hook_trees(self, sibling: SiblingContext) -> Iterable[HookTree]:
        if sibling.role is not SiblingRole.GENERAL or sibling.route is not AdsRoute.PBS:
            return
        yield HookTree(
            hook=Hook.PRE_ADS,
            steps=(
                Step(
                    function=pa_meta(sibling.prefix, content_base_url=sibling.content_base_url),
                    run_condition=UPID_PRESENT,
                ),
            ),
            outputs={HEADER_OUTPUT: "{% temp.c_ok = true ? temp.c_hdr : '' %}"},
            budget_ms=EXECUTOR_BUDGET_MS,
        )
