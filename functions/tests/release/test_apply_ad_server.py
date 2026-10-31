"""The apply path with the ad-server route (M8-09): the auction Function gets the current origin secret and the
release at PutFunction, tagged with the secret's slot; a third-party ad server's sibling never gets the secret."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from fast_channels.core.ad_server import AdServer, AdServerContribution, ads_url
from fast_channels.core.monetization.bundle import AdServerRoute, Bundle
from fast_channels.core.monetization.playback import release_tags, render, render_function
from fast_channels.core.pbs_config import stored_request_ids
from fast_channels.functions.release import apply

PBS = "https://d222222abcdef8.cloudfront.net"
MOCK = "https://abc123.execute-api.us-east-1.amazonaws.com"
GAM_TAG = "https://pubads.g.doubleclick.net/gampad/ads?iu=/1/f&cust_params={prebid}"
STORED = list(stored_request_ids(ad_server=True))
ARN = "arn:aws:mediatailor:us-east-1:123456789012:playbackConfiguration/"
HLS = "https://abc.mediatailor.us-east-1.amazonaws.com/v1/master/h/fastm-demo-p-a/"


def _bundle(kit: SimpleNamespace, kind: AdServer = AdServer.MOCK) -> Bundle:
    url = ads_url(kind, tag=GAM_TAG if kind is AdServer.GAM else "", mock_url=MOCK, sibling="{sibling}")
    bundle: Bundle = kit.make_bundle(
        stored_requests=STORED,
        contributions=[AdServerContribution(pbs_origin=PBS)],
        ad_server=AdServerRoute(url=url, origin=kind is AdServer.MOCK),
    )
    return bundle


@pytest.mark.parametrize(("slot", "secret"), [("a", "secret-value-a"), ("b", "secret-value-b")])
def test_the_auction_is_created_with_the_current_secret_and_its_slot(
    kit: SimpleNamespace, state: Any, stub: Any, slot: str, secret: str
) -> None:
    bundle = _bundle(kit)
    key = state.add(bundle)
    state.slot = slot
    tags = release_tags("fastm", release=3, change_id="c3")
    mediatailor = stub("mediatailor")
    expected: dict[str, dict[str, Any]] = {}
    wanted = {"fastm_ps_hb", "fastm_ps_ctl", "fastm_ps_root", "fastm_demo_p_b_pa_auction", "fastm_demo_p_b_pa_root"}
    for function in (f for f in bundle.functions if f.function_id in wanted):
        http = function.http_request_configuration
        origin = http is not None and bool(http.demand_origin)
        released = function.released(3, tags=tags | ({"fast:origin-slot": slot} if origin else {}))
        if origin:
            released = render_function(released, release=3, origin_secret=SecretStr(secret))
        request = released.put_request()
        expected[released.function_id] = request
        mediatailor.add_client_error(
            "get_function", service_error_code="NotFoundException", expected_params={"FunctionId": released.function_id}
        )
        mediatailor.add_response(
            "put_function", {"FunctionId": released.function_id, "FunctionType": function.function_type.value}, request
        )

    result = apply.put_functions(key, 3, "c3", ["fastm-demo-p-b"])

    assert sorted(result["created"]) == sorted(expected)
    auction = expected["fastm_demo_p_b_pa_auction_r0003"]
    headers = auction["HttpRequestConfiguration"]["Headers"]
    assert (headers["X-Fast-Origin"], headers["X-Fast-Fv"]) == (secret, "r0003")
    assert auction["Tags"]["fast:origin-slot"] == slot
    assert {"CacheScope", "CacheReason", "DemandOrigin"}.isdisjoint(auction["HttpRequestConfiguration"])
    others = [r for function_id, r in expected.items() if function_id != "fastm_demo_p_b_pa_auction_r0003"]
    assert all("secret-value" not in json.dumps(r) and "fast:origin-slot" not in r["Tags"] for r in others)


def test_a_third_party_sibling_is_configured_without_the_secret(
    kit: SimpleNamespace, state: Any, stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(kit, AdServer.GAM)
    key = state.add(bundle)
    sibling = next(s for s in bundle.siblings if s.letter == "a")
    put, logging = render(
        sibling.playback, prefix="fastm", release=2, change_id="c2", origin_secret=SecretStr("secret-value-a")
    )
    assert "secret-value" not in json.dumps(put)
    assert put["AdDecisionServerConfiguration"]["HttpRequest"] == {"Method": "GET"}
    mediatailor = stub("mediatailor")
    mediatailor.add_response(
        "put_playback_configuration",
        {"PlaybackConfigurationArn": f"{ARN}{sibling.name}", "HlsConfiguration": {"ManifestEndpointPrefix": HLS}},
        put,
    )
    mediatailor.add_response("configure_logs_for_playback_configuration", {"PercentEnabled": 100}, logging)
    monkeypatch.setattr(apply, "deliver", lambda *_args, **_kwargs: [])  # the deliveries are test_apply's
    assert apply.configure(key, sibling.name, 2, "c2")["sibling"] == sibling.name
