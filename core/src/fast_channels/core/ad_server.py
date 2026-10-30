"""Prebid results for an ad server's VAST (M8-09, ADR 0034): the standard Prebid video ad-server flow, for a break.

With ``modules.demand.ad_server`` set, the monetized pair siblings call an ad server instead of Prebid Server:

1. **PRE_SESSION** blanks ``player_params.hb_pb``, ``hb_uuid`` and ``hb_bidder``, so a value a player sends at session
   start never reaches the ad server.
2. **PRE_ADS** runs the auction: an ``HTTP_REQUEST`` POST to Prebid Server through its CloudFront and origin rule (the
   apply adds the origin secret and the release), cached per session and break. The body names only a stored request
   (``fast-adsrv-pod-<seconds>s``), which asks the fast-ctv module to cache the composed pod in Prebid Cache and answer
   its Prebid targeting. The root commits the targeting to ``player_params.hb_*`` for this break only.
3. **The ad server** (the ADS URL template, :func:`ads_url`) gets ``hb_pb``, ``hb_uuid`` and ``hb_bidder`` as
   key-values.
   A line item matched on ``hb_pb`` returns a VAST Wrapper to ``https://<cache host>/cache?uuid=<hb_uuid>``, which
   MediaTailor follows to the pod; anything else is the ad server's own (house) ad.

The pod rules, the reservations and the trackers stay the Pod Decision Service's and the module's: the cached pod is
exactly what the direct route would have answered. :func:`mock_answer` is the demo's ad server (``mock``), a GAM-like
line-item match behind the origin secret.
"""

import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final
from urllib.parse import quote
from xml.sax.saxutils import escape

from fast_channels.core.monetization.bundle import HookTree, SiblingContext, Step
from fast_channels.core.monetization.functions import (
    Cache,
    CacheScope,
    CustomOutputConfiguration,
    Function,
    FunctionType,
    Hook,
    HttpRequestConfiguration,
    function_stem,
    literal,
    reexport,
)
from fast_channels.core.monetization.playback import (
    AD_SERVER_TARGETING,
    HEADER_CONFIGURATION,
    HEADER_SESSION,
    PBS_AUCTION_PATH,
    AdsRoute,
)

NAME: Final = "ad-server"


class AdServer(StrEnum):
    """``modules.demand.ad_server``: where a monetized break's ad request goes."""

    NONE = "none"
    """MediaTailor POSTs to Prebid Server, which answers the pod as VAST (the direct route)."""
    MOCK = "mock"
    """The demo's GAM-like ad server, behind the origin secret."""
    GAM = "gam"
    """Google Ad Manager: the publisher's VAST tag with the targeting in ``cust_params``."""
    FREEWHEEL = "freewheel"
    """FreeWheel: the publisher's ad request with the targeting among its key-values."""


TARGETING_KEYS: Final = AD_SERVER_TARGETING
"""The Prebid targeting keys the ad server receives (as ``player_params.<key>`` in the ADS URL template)."""
PLACEHOLDER: Final = "{prebid}"
"""Where a GAM or FreeWheel tag (``modules.demand.ad_server_tag``) takes the targeting key-values."""
POD_CACHE_SECONDS: Final = 300
"""How long a cached pod lives in Prebid Cache: longer than a prefetch's retrieval-to-break gap (about 140 s) plus
the break, so MediaTailor can still follow the Wrapper."""
STORED_REQUEST_PREFIX: Final = {False: "fast-adsrv-pod-", True: "fast-adsrv-pod-kids-"}
"""Stored requests of the ad-server route (the same pods as ``fast-pod-*`` plus the pod-cache ask)."""
CTV_PRICE_GRANULARITY: Final[Mapping[str, Any]] = {
    "precision": 2,
    "ranges": [
        {"min": 0, "max": 20, "increment": 0.10},
        {"min": 20, "max": 50, "increment": 0.50},
        {"min": 50, "max": 100, "increment": 1.00},
    ],
}
"""``hb_pb`` buckets for CTV: 10 cents up to $20, 50 cents up to $50, $1 up to $100 (CTV CPMs run well above web
display's, so Prebid's ``medium`` and ``high`` granularities cap too low). One line item per bucket matches it."""

AUCTION: Final = "pa_auction"
BLANK: Final = "ps_hb"
AUCTION_REQUEST_TIMEOUT_MS: Final = 1200
"""The auction's ``RequestTimeoutMilliseconds``: Prebid Server's worst case is about 1,000 ms (the policy hook's
group 110 ms, ``tmax`` 500 ms, the exitpoint's /select group 250 ms and the pod's cache write)."""
AUCTION_BUDGET_MS: Final = 1300
"""The auction's share of the PRE_ADS root timeout (its request timeout and a margin; R-F7). With the 2,000 ms ADS
timeout the Live maximum needs at least 3,300 ms (R-P3; the siblings have 6,000)."""
BLANK_BUDGET_MS: Final = 100
AUCTION_CACHE_SECONDS: Final = 30
"""The auction's MediaTailor cache: one answer per session and break, kept for the shortest break."""
AUCTION_KEY: Final = "{% 'auction|v1|' & session.uuid & '|' & $string(scte.event_id) %}"
AUCTION_REASON: Final = (
    "An auction's bids and its cached pod belong to one session and one break; no other session or break may reuse "
    "them."
)
_UUID4: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def stored_request_id(seconds: int, *, kids: bool = False) -> str:
    """``fast-adsrv-pod-<seconds>s`` (``fast-adsrv-pod-kids-<seconds>s`` for child-directed configurations)."""
    return f"{STORED_REQUEST_PREFIX[kids]}{seconds}s"


def player_param(key: str) -> str:
    """``player_params.<key>``: where the PRE_ADS root commits a targeting key for this break."""
    return f"player_params.{key}"


def _template_values(encoded: bool) -> str:
    pairs = "&".join(f"{key}=[{player_param(key)}]" for key in TARGETING_KEYS)
    return quote(pairs, safe="[]._") if encoded else pairs


def ads_url(kind: AdServer, *, tag: str = "", mock_url: str = "", sibling: str = "") -> str:
    """The ADS URL template of an ad-server sibling.

    - ``mock``: ``<mock_url>/ads?pc=<sibling>&sid=…&evid=…&dur=…&hb_pb=…&hb_uuid=…&hb_bidder=…``;
    - ``gam``: ``tag`` with :data:`PLACEHOLDER` (inside ``cust_params``) replaced by the URL-encoded key-values
      (``hb_pb%3D[player_params.hb_pb]%26…``), as Prebid's GAM setup passes them;
    - ``freewheel``: ``tag`` with :data:`PLACEHOLDER` (in the key-value section after the first ``;``) replaced by
      ``hb_pb=[player_params.hb_pb]&…``.

    Raises:
        ValueError: ``none``, a tag without the placeholder, or a URL that is not https.
    """
    match kind:
        case AdServer.MOCK:
            base = mock_url.rstrip("/")
            if not base.startswith("https://"):
                raise ValueError(f"the mock ad server's URL is https://…, got {mock_url!r}")
            return (
                f"{base}/ads?pc={sibling}&sid=[session.uuid]&evid=[scte.event_id]&dur=[session.avail_duration_ms]"
                f"&{_template_values(encoded=False)}"
            )
        case AdServer.GAM | AdServer.FREEWHEEL:
            if not tag.startswith("https://") or tag.count(PLACEHOLDER) != 1:
                raise ValueError(f"a {kind} tag is an https URL with {PLACEHOLDER} once where the targeting goes")
            return tag.replace(PLACEHOLDER, _template_values(encoded=kind is AdServer.GAM))
    raise ValueError("ad_server none has no ad-server ADS URL")


# --- the Functions -------------------------------------------------------------------------------------------------


def auction_url(pbs_origin: str) -> str:
    """The auction's ``Url``: Prebid Server's auction with the break in the query, as the direct route sends it."""
    base = pbs_origin.rstrip("/")
    if not base.startswith("https://") or any(char in base for char in "'\\"):
        raise ValueError(f"the demand origin is https:// without quotes or backslashes, got {base!r}")
    return (
        f"{{% '{base}{PBS_AUCTION_PATH}?evid=' & $encodeUrlComponent($string(scte.event_id)) & '&upid=' & "
        "$encodeUrlComponent($string(scte.segmentation_upid)) & '&dur=' & $string(session.avail_duration_ms) %}"
    )


def auction_body(*, kids: bool) -> str:
    """The auction's ``Body``: the stored request for the break's length, nothing from the player (R-P1's rule)."""
    stored = STORED_REQUEST_PREFIX[kids]
    return (
        "{% $string({'ext': {'prebid': {'storedrequest': {'id': '"
        f"{stored}' & $string(session.avail_duration_secs) & 's'}}}}}}}}) %}}"
    )


def _targeting(key: str, length: int) -> str:
    """A step output: ``key`` of the answer's one bid's targeting, capped (R-F1), or nothing."""
    return (
        "{% ($t := response.statusCode = 200 ? response.body.seatbid[0].bid[0].ext.prebid.targeting; "
        f"$exists($t.{key}) ? $substring($string($t.{key}), 0, {length}) : '') %}}"
    )


TARGETING_LENGTHS: Final = {"hb_pb": 16, "hb_uuid": 64, "hb_bidder": 32}


def pa_auction(prefix: str, *, sibling: str, pbs_origin: str, kids: bool) -> Function:
    """``<p>_<sibling>_pa_auction``: the break's Prebid Server auction for the ad server (per sibling: it names it)."""
    return Function(
        function_id=function_stem(prefix, AUCTION, sibling=sibling),
        function_type=FunctionType.HTTP_REQUEST,
        description="Prebid Server auction of the break; the fast-ctv module caches the pod and answers targeting.",
        http_request_configuration=HttpRequestConfiguration(
            method_type="POST",
            url=auction_url(pbs_origin),
            body=auction_body(kids=kids),
            headers={
                "Content-Type": "application/json",
                HEADER_CONFIGURATION: sibling,
                HEADER_SESSION: "{% session.uuid %}",
                "User-Agent": "{% session.user_agent %}",
            },
            request_timeout_milliseconds=AUCTION_REQUEST_TIMEOUT_MS,
            cache=Cache(
                ttl_minimum_seconds=AUCTION_CACHE_SECONDS,
                ttl_maximum_seconds=AUCTION_CACHE_SECONDS,
                key=AUCTION_KEY,
            ),
            cache_scope=CacheScope.SESSION_BREAK,
            cache_reason=AUCTION_REASON,
            demand_origin=True,
            output={f"temp.a_{key[3:]}": _targeting(key, length) for key, length in TARGETING_LENGTHS.items()},
        ),
    )


def ps_hb(prefix: str) -> Function:
    """``<p>_ps_hb``: blanks the targeting keys at session start, so the player cannot set them."""
    return Function(
        function_id=function_stem(prefix, BLANK),
        function_type=FunctionType.CUSTOM_OUTPUT,
        description="Blanks the Prebid targeting player parameters: only the break's auction sets them.",
        custom_output_configuration=CustomOutputConfiguration(
            output={player_param(key): literal("") for key in TARGETING_KEYS}
        ),
    )


def _commit(key: str) -> str:
    """The root's commit of a targeting key: the auction's value when it returned a uuid, else empty."""
    return f"{{% $exists(temp.a_uuid) and temp.a_uuid != '' ? temp.a_{key[3:]} : '' %}}"


class AdServerContribution:
    """The ad-server route's trees: the PRE_SESSION blank on every sibling, the PRE_ADS auction on every sibling whose
    route is :attr:`AdsRoute.AD_SERVER`."""

    name = NAME

    def __init__(self, *, pbs_origin: str, kids_channels: Iterable[str] = ()) -> None:
        self.pbs_origin = pbs_origin
        self.kids_channels = frozenset(kids_channels)

    def hook_trees(self, sibling: SiblingContext) -> Iterable[HookTree]:
        # Every sibling blanks the targeting (the probe too), so the deployment keeps one PRE_SESSION tree.
        blank = ps_hb(sibling.prefix)
        yield HookTree(
            hook=Hook.PRE_SESSION,
            steps=(Step(function=blank),),
            outputs={key: reexport(key) for key in blank.output},
            budget_ms=BLANK_BUDGET_MS,
        )
        if sibling.route is not AdsRoute.AD_SERVER:
            return
        auction = pa_auction(
            sibling.prefix,
            sibling=sibling.name,
            pbs_origin=self.pbs_origin,
            kids=sibling.channel in self.kids_channels,
        )
        yield HookTree(
            hook=Hook.PRE_ADS,
            steps=(Step(function=auction),),
            outputs={player_param(key): _commit(key) for key in TARGETING_KEYS},
            budget_ms=AUCTION_BUDGET_MS,
        )


# --- the mock ad server ---------------------------------------------------------------------------------------------

VAST_VERSION: Final = "4.2"


def price_bucket(value: str) -> Decimal | None:
    """``hb_pb`` as a bucket of :data:`CTV_PRICE_GRANULARITY` (a line item exists for each), or None."""
    try:
        price = Decimal(value)
    except InvalidOperation:
        return None
    if not price.is_finite() or price <= 0 or price.as_tuple().exponent != -CTV_PRICE_GRANULARITY["precision"]:
        return None
    for band in CTV_PRICE_GRANULARITY["ranges"]:
        low, high, step = (Decimal(str(band[k])) for k in ("min", "max", "increment"))
        if low < price <= high:
            return price if (price - low) % step == 0 else None
    return None


def wrapper(*, ad_id: str, target: str, impression: str, system: str = "fast-channels mock ad server") -> str:
    """A VAST 4.2 Wrapper to ``target`` that admits a pod (``allowMultipleAds``)."""
    return (
        f'<VAST version="{VAST_VERSION}"><Ad id="{escape(ad_id)}"><Wrapper followAdditionalWrappers="true" '
        f'allowMultipleAds="true"><AdSystem>{escape(system)}</AdSystem>'
        f"<Impression><![CDATA[{impression}]]></Impression>"
        f"<VASTAdTagURI><![CDATA[{target}]]></VASTAdTagURI></Wrapper></Ad></VAST>"
    )


def mock_answer(query: Mapping[str, str], *, cache_url: str, house_url: str, impression_url: str) -> tuple[str, str]:
    """The mock ad server's decision for one ad request: ``(line, VAST)``.

    Like a GAM network with Prebid line items at every bucket of :data:`CTV_PRICE_GRANULARITY`: a request whose
    ``hb_pb`` is such a bucket, with a uuid4 ``hb_uuid`` and a bidder, gets the Prebid line's Wrapper to
    ``<cache_url>?uuid=<hb_uuid>``; anything else gets the house line's Wrapper to ``house_url`` (the stage's house
    pod). ``impression_url`` is the ad server's own impression (the line in ``line=``).
    """
    bucket = price_bucket(query.get("hb_pb", ""))
    uuid = query.get("hb_uuid", "")
    if bucket is not None and _UUID4.fullmatch(uuid) and query.get("hb_bidder"):
        line, target = f"prebid-{bucket}", f"{cache_url}?uuid={uuid}"
    else:
        line, target = "house", house_url
    separator = "&" if "?" in impression_url else "?"
    impression = f"{impression_url}{separator}line={quote(line)}"
    return line, wrapper(ad_id=f"mock-{line}", target=target, impression=impression)
