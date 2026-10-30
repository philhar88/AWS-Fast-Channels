"""The ad-server route (M8-09): the PRE_ADS auction, the sibling's ADS URL template, the validators that hold them,
and the mock ad server's line-item match."""

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from fast_channels.core import ad_server as adsrv
from fast_channels.core.ad_server import AdServer, AdServerContribution
from fast_channels.core.crawl.functions import CrawlContribution
from fast_channels.core.monetization import validators as v
from fast_channels.core.monetization.bundle import AdServerRoute, Bundle, ChannelInput, build_bundle
from fast_channels.core.monetization.functions import CacheScope, Hook
from fast_channels.core.monetization.playback import AdsRoute, render
from fast_channels.core.monetization.validators import Rule

PREFIX = "fastm"
CHANNEL = "fastm-demo"
CDN = "https://d111111abcdef8.cloudfront.net"
PBS = "https://d222222abcdef8.cloudfront.net"
MOCK = "https://abc123.execute-api.us-east-1.amazonaws.com"
CA_URL = "https://abc.channel-assembly.mediatailor.us-east-1.amazonaws.com/v1/channel/fastm-demo/"
GRID = (30, 60, 90, 120)
STORED = (
    *(f"fast-pod-{s}s" for s in GRID),
    *(f"fast-pod-kids-{s}s" for s in GRID),
    *(adsrv.stored_request_id(s) for s in GRID),
    *(adsrv.stored_request_id(s, kids=True) for s in GRID),
)
GAM_TAG = (
    "https://pubads.g.doubleclick.net/gampad/ads?iu=/1234/fast/demo&sz=1920x1080&gdfp_req=1&output=vast"
    "&env=vp&unviewed_position_start=1&correlator=[avail.random]&cust_params={prebid}%26channel%3Ddemo"
)
FREEWHEEL_TAG = (
    "https://12345.v.fwmrm.net/ad/g/1?nw=12345&prof=12345:fast_live&csid=fast_demo&caid=[scte.segmentation_upid]"
    "&resp=vast4&flag=+sltp+exvt+slcb;{prebid};slid=pre&tpcl=MIDROLL&ptgt=a"
)


def bundle(kind: AdServer = AdServer.MOCK, *, grid: tuple[int, ...] = GRID, **overrides: Any) -> Bundle:
    url = adsrv.ads_url(
        kind,
        tag={AdServer.GAM: GAM_TAG, AdServer.FREEWHEEL: FREEWHEEL_TAG}.get(kind, ""),
        mock_url=MOCK,
        sibling="{sibling}",
    )
    arguments: dict[str, Any] = {
        "prefix": PREFIX,
        "channels": [ChannelInput(name=CHANNEL, video_content_source_url=CA_URL)],
        "content_base_url": CDN,
        "pbs_origin": PBS,
        "log_percent": 100,
        "stored_requests": STORED,
        "grid_seconds": grid,
        "contributions": [CrawlContribution(), AdServerContribution(pbs_origin=PBS)],
        "ad_server": AdServerRoute(url=url, origin=kind is AdServer.MOCK),
    }
    return build_bundle(**(arguments | overrides))


def messages(found: list[v.Violation], rule: Rule) -> list[str]:
    return [f"{x.where}: {x.message}" for x in found if x.rule is rule]


# --- the bundle ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [AdServer.MOCK, AdServer.GAM, AdServer.FREEWHEEL])
def test_the_ad_server_route_validates(kind: AdServer) -> None:
    assert v.validate_bundle(bundle(kind)) == []


def test_the_pair_takes_the_route_and_the_probe_stays_on_prebid_server() -> None:
    built = bundle()
    routes = {s.letter: s.route for s in built.siblings}
    assert routes == {"a": AdsRoute.AD_SERVER, "b": AdsRoute.AD_SERVER, "z": AdsRoute.PBS}
    a = next(s for s in built.siblings if s.letter == "a")
    assert a.playback.ad_decision_server_url == (
        f"{MOCK}/ads?pc=fastm-demo-p-a&sid=[session.uuid]&evid=[scte.event_id]&dur=[session.avail_duration_ms]"
        "&hb_pb=[player_params.hb_pb]&hb_uuid=[player_params.hb_uuid]&hb_bidder=[player_params.hb_bidder]"
    )
    request = a.playback.api()["AdDecisionServerConfiguration"]
    assert request == {
        "HttpRequest": {"Method": "GET", "Headers": {"X-Fast-Pc": "fastm-demo-p-a"}},
        "VastResponse": {"AdSequencingMode": "FOLLOW_AD_SEQUENCE"},
    }
    # The crawl lookup serves the direct route; an ad-server sibling runs the auction alone, per sibling.
    pre_ads = built.function(a.playback.function_mapping[Hook.PRE_ADS])
    assert pre_ads is not None
    assert [ref.function_id for ref in pre_ads.steps] == ["fastm_demo_p_a_pa_auction"]
    assert pre_ads.sequential_executor_configuration.timeout_milliseconds == adsrv.AUCTION_BUDGET_MS
    pre_session = built.function(a.playback.function_mapping[Hook.PRE_SESSION])
    assert [ref.function_id for ref in pre_session.steps] == ["fastm_ps_hb", "fastm_ps_ctl"]


def test_the_auction() -> None:
    auction = bundle().function("fastm_demo_p_a_pa_auction")
    assert auction is not None
    http = auction.http_request_configuration
    assert http.method_type == "POST" and http.demand_origin and http.cache_scope is CacheScope.SESSION_BREAK
    assert http.cache.key == "{% 'auction|v1|' & session.uuid & '|' & $string(scte.event_id) %}"
    assert (http.cache.ttl_minimum_seconds, http.cache.ttl_maximum_seconds) == (30, 30)
    assert http.headers == {
        "Content-Type": "application/json",
        "X-Fast-Pc": "fastm-demo-p-a",
        "X-Fast-Sid": "{% session.uuid %}",
        "User-Agent": "{% session.user_agent %}",
    }
    assert "fast-adsrv-pod-' & $string(session.avail_duration_secs)" in http.body
    # The apply adds the secret and the release; PutFunction never sees the bundle's own members.
    sent = auction.put_request()["HttpRequestConfiguration"]
    assert {"CacheScope", "CacheReason", "DemandOrigin"}.isdisjoint(sent)


def test_a_third_party_ad_server_gets_no_header_of_ours() -> None:
    built = bundle(AdServer.GAM)
    a = next(s for s in built.siblings if s.letter == "a")
    assert a.playback.api()["AdDecisionServerConfiguration"]["HttpRequest"] == {"Method": "GET"}
    put, _ = render(a.playback, prefix=PREFIX, release=3, change_id="c", origin_secret=SecretStr("s3cret"))
    assert "s3cret" not in json.dumps(put)
    assert put["AdDecisionServerUrl"].startswith("https://pubads.g.doubleclick.net/gampad/ads?")
    assert (
        "cust_params=hb_pb%3D[player_params.hb_pb]%26hb_uuid%3D[player_params.hb_uuid]%26hb_bidder%3D"
        in (put["AdDecisionServerUrl"])
    )
    mock = next(s for s in bundle().siblings if s.letter == "a")
    put, _ = render(mock.playback, prefix=PREFIX, release=3, change_id="c", origin_secret=SecretStr("s3cret"))
    assert put["AdDecisionServerConfiguration"]["HttpRequest"]["Headers"]["X-Fast-Origin"] == "s3cret"


def test_tags() -> None:
    assert (
        adsrv.ads_url(AdServer.FREEWHEEL, tag=FREEWHEEL_TAG).count(
            ";hb_pb=[player_params.hb_pb]&hb_uuid=[player_params.hb_uuid]&hb_bidder=[player_params.hb_bidder];"
        )
        == 1
    )
    for kind, tag in ((AdServer.GAM, "https://x.example/ads"), (AdServer.FREEWHEEL, "http://x/{prebid}")):
        with pytest.raises(ValueError, match="once"):
            adsrv.ads_url(kind, tag=tag)
    with pytest.raises(ValueError, match="https"):
        adsrv.ads_url(AdServer.MOCK, mock_url="http://x")
    with pytest.raises(ValueError, match="none"):
        adsrv.ads_url(AdServer.NONE)
    with pytest.raises(ValueError, match="https"):
        adsrv.auction_url("http://x")


# --- the validators ------------------------------------------------------------------------------------------------


def _edit(built: Bundle, change: Any) -> Bundle:
    document = built.model_dump(mode="json", by_alias=True, exclude_none=True)
    change(document)
    return Bundle.model_validate(document)


def _function(document: dict[str, Any], stem: str) -> dict[str, Any]:
    return next(f for f in document["functions"] if f["FunctionId"] == stem)


def test_the_auction_must_leave_the_secret_to_the_apply() -> None:
    def secret(document: dict[str, Any]) -> None:
        request = _function(document, "fastm_demo_p_a_pa_auction")["HttpRequestConfiguration"]
        request["Headers"]["X-Fast-Origin"] = "x"
        request["Url"] = "https://elsewhere.example/collect"
        request["MethodType"] = "GET"

    found = messages(v.validate_bundle(_edit(bundle(), secret)), Rule.P7)
    assert "function fastm_demo_p_a_pa_auction Headers: the apply adds the origin secret and the release" in found
    assert (
        "function fastm_demo_p_a_pa_auction Url: a demand-origin request POSTs to https://<origin>/openrtb2/auction?"
        in found
    )
    assert "function fastm_demo_p_a_pa_auction: a demand-origin request is a POST" in found


def test_an_ad_server_sibling_runs_one_auction_and_blanks_the_targeting() -> None:
    def no_auction(document: dict[str, Any]) -> None:
        for stem in ("fastm_demo_p_a_pa_auction", "fastm_demo_p_b_pa_auction"):
            _function(document, stem)["HttpRequestConfiguration"].pop("DemandOrigin")
        root = _function(document, "fastm_ps_root")["SequentialExecutorConfiguration"]
        root["Output"].pop("player_params.hb_uuid")

    found = messages(v.validate_bundle(_edit(bundle(), no_auction)), Rule.P7)
    assert "sibling fastm-demo-p-a: an ad-server sibling's PRE_ADS tree runs one auction, not 0" in found
    assert (
        "sibling fastm-demo-p-a: PRE_SESSION commits ['player_params.hb_uuid'] empty, so a player cannot set them"
        in found
    )


def test_only_an_ad_server_sibling_runs_an_auction() -> None:
    def misplaced(document: dict[str, Any]) -> None:
        probe = next(s for s in document["siblings"] if s["letter"] == "z")["playback"]
        probe["FunctionMapping"]["PRE_ADS_REQUEST"] = "fastm_demo_p_a_pa_root"

    found = messages(v.validate_bundle(_edit(bundle(), misplaced)), Rule.P7)
    assert "sibling fastm-demo-p-z: only an ad-server sibling runs a PRE_ADS auction" in found


def test_the_auctions_stored_requests_exist_for_the_grid() -> None:
    built = bundle(stored_requests=[s for s in STORED if s != adsrv.stored_request_id(60)])
    assert messages(v.validate_bundle(built), Rule.P5) == [
        f"function fastm_demo_p_{x}_pa_auction Body: no stored request fast-adsrv-pod-60s for the 60 s breaks"
        for x in "ab"
    ]


def test_the_auction_reads_only_what_mediatailor_generates() -> None:
    def leak(document: dict[str, Any]) -> None:
        request = _function(document, "fastm_demo_p_a_pa_auction")["HttpRequestConfiguration"]
        request["Headers"]["X-Ip"] = "{% session.client_ip %}"
        request["Body"] = "{% $string({'ua': session.user_agent}) %}"

    found = messages(v.validate_bundle(_edit(bundle(), leak)), Rule.F5)
    assert any("Headers[X-Ip]: reads ['session.client_ip']" in m for m in found)
    assert any("Body: reads ['session.user_agent']" in m for m in found)


def test_the_ads_url_carries_only_the_targeting() -> None:
    def extra(document: dict[str, Any]) -> None:
        a = next(s for s in document["siblings"] if s["letter"] == "a")["playback"]
        a["AdDecisionServerUrl"] += "&vid=[player_params.vid]"

    assert messages(v.validate_bundle(_edit(bundle(), extra)), Rule.P2) == [
        "sibling fastm-demo-p-a AdDecisionServerUrl: carries ['player_params.vid']; never the URL"
    ]

    def header(document: dict[str, Any]) -> None:
        a = next(s for s in document["siblings"] if s["letter"] == "a")["playback"]
        a["AdDecisionServerConfiguration"]["HttpRequest"]["Headers"] = {"X-Fast-Pc": "fastm-demo-p-a"}

    assert messages(v.validate_bundle(_edit(bundle(AdServer.GAM), header)), Rule.P2) == [
        "sibling fastm-demo-p-a Headers: a third-party ad server gets no header of ours"
    ]


# --- the mock ad server ----------------------------------------------------------------------------------------------

UUID = "0b9a7c3e-1111-4222-8333-444455556666"
CACHE = f"{PBS}/cache"
HOUSE = f"{CDN}/ads/standard_843ed1d05a/vast.xml"
IMPRESSION = f"{CDN}/ads/b.gif?evt=adserver"


def test_a_prebid_line_redirects_to_the_cached_pod() -> None:
    line, vast = adsrv.mock_answer(
        {"hb_pb": "35.00", "hb_uuid": UUID, "hb_bidder": "fast-ctv"},
        cache_url=CACHE,
        house_url=HOUSE,
        impression_url=IMPRESSION,
    )
    assert line == "prebid-35.00"
    assert f"<VASTAdTagURI><![CDATA[{CACHE}?uuid={UUID}]]></VASTAdTagURI>" in vast
    assert 'allowMultipleAds="true"' in vast and f"{IMPRESSION}&line=prebid-35.00" in vast


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"hb_pb": "", "hb_uuid": "", "hb_bidder": ""},  # the auction found no bid
        {"hb_pb": "35.00", "hb_uuid": "not-a-uuid", "hb_bidder": "fast-ctv"},
        {"hb_pb": "35.25", "hb_uuid": UUID, "hb_bidder": "fast-ctv"},  # not a bucket: no line item
        {"hb_pb": "0.00", "hb_uuid": UUID, "hb_bidder": "fast-ctv"},
        {"hb_pb": "35", "hb_uuid": UUID, "hb_bidder": "fast-ctv"},
        {"hb_pb": "NaN", "hb_uuid": UUID, "hb_bidder": "fast-ctv"},
        {"hb_pb": "35.00", "hb_uuid": UUID, "hb_bidder": ""},
    ],
)
def test_anything_else_is_the_house_line(query: dict[str, str]) -> None:
    line, vast = adsrv.mock_answer(query, cache_url=CACHE, house_url=HOUSE, impression_url=IMPRESSION)
    assert line == "house"
    assert f"<VASTAdTagURI><![CDATA[{HOUSE}]]></VASTAdTagURI>" in vast


def test_the_buckets_are_the_stored_requests_granularity() -> None:
    assert adsrv.price_bucket("0.10") == Decimal("0.10")
    assert adsrv.price_bucket("20.00") == Decimal("20.00")
    assert adsrv.price_bucket("20.50") == Decimal("20.50")
    assert adsrv.price_bucket("20.10") is None
    assert adsrv.price_bucket("100.00") == Decimal("100.00")
    assert adsrv.price_bucket("101.00") is None
