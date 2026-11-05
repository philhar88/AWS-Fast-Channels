"""The identity module's PRE_SESSION ticket redeem in the release bundle (DESIGN-v2.2 §5.3, ADR-0018): which siblings
are ticketed, the tree's shape, and the validators it meets."""

from types import SimpleNamespace
from typing import Any

import pytest

from fast_channels.core.identity.functions import (
    CACHE_REASON,
    EXECUTOR_BUDGET_MS,
    OUTPUT,
    REQUEST_TIMEOUT_MS,
    TICKET_PRESENT,
    IdentityContribution,
    ps_redeem,
    redeem_body,
)
from fast_channels.core.monetization.bundle import SESSION_CONTROLS_BUDGET_MS, BundleContribution, ChannelInput
from fast_channels.core.monetization.functions import CacheScope, Hook
from fast_channels.core.monetization.playback import HEADER_TICKET, TICKET_TEMPLATE
from fast_channels.core.monetization.validators import Rule, validate_bundle

REDEEM_URL = "https://abc123.execute-api.us-east-1.amazonaws.com/v1/redeem"


@pytest.fixture
def ticketed(kit: SimpleNamespace) -> Any:
    return kit.make_bundle(contributions=[IdentityContribution(redeem_url=REDEEM_URL)], ticketed=True)


def test_the_ticketed_bundle_breaks_no_rule(ticketed: Any) -> None:
    assert validate_bundle(ticketed) == []
    assert isinstance(IdentityContribution(redeem_url=REDEEM_URL), BundleContribution)


def test_the_pair_redeems_and_forwards_the_ticket_and_the_probe_neither(ticketed: Any) -> None:
    by_letter = {s.letter: s for s in ticketed.siblings}
    for letter in ("a", "b"):
        sibling = by_letter[letter]
        root = ticketed.function(sibling.playback.function_mapping[Hook.PRE_SESSION])
        executor = root.sequential_executor_configuration
        assert root.function_id == f"fastm_demo_p_{letter}_ps_root"
        assert [(s.function_id, s.run_condition) for s in executor.function_list] == [
            (f"fastm_demo_p_{letter}_ps_redeem", TICKET_PRESENT),
            ("fastm_ps_ctl", None),
        ], "the redeem, then the session controls"
        assert executor.timeout_milliseconds == EXECUTOR_BUDGET_MS + SESSION_CONTROLS_BUDGET_MS == 1200
        assert "temp.redeem" not in executor.output, "the redeem's answer is never committed"
        assert sibling.playback.ad_decision_server_configuration.http_request.headers[HEADER_TICKET] == TICKET_TEMPLATE
    probe = by_letter["z"]
    assert probe.playback.function_mapping[Hook.PRE_SESSION] == "fastm_ps_root"
    assert HEADER_TICKET not in probe.playback.ad_decision_server_configuration.http_request.headers


def test_the_lab_channel_is_never_ticketed(kit: SimpleNamespace) -> None:
    """Its siblings publish their PRE_SESSION hook events, which would log the ticket (R-P2)."""
    lab = ChannelInput(name="fastm-lab", video_content_source_url=kit.CA_URL, lab=True)
    bundle = kit.make_bundle(
        channels=[ChannelInput(name=kit.CHANNEL, video_content_source_url=kit.CA_URL), lab],
        contributions=[IdentityContribution(redeem_url=REDEEM_URL)],
        ticketed=True,
    )
    assert validate_bundle(bundle) == []
    lab_siblings = [s for s in bundle.siblings if s.channel == "fastm-lab"]
    assert lab_siblings and all(s.playback.function_mapping[Hook.PRE_SESSION] == "fastm_ps_root" for s in lab_siblings)
    assert not any(
        HEADER_TICKET in s.playback.ad_decision_server_configuration.http_request.headers for s in lab_siblings
    )


def test_without_the_module_nothing_changes(kit: SimpleNamespace) -> None:
    assert kit.make_bundle(contributions=[IdentityContribution(redeem_url=REDEEM_URL)]) == kit.make_bundle()


def test_the_redeem_is_the_designed_function() -> None:
    function = ps_redeem("fastm", sibling="fastm-demo-p-a", redeem_url=REDEEM_URL)
    http = function.http_request_configuration
    assert function.function_id == "fastm_demo_p_a_ps_redeem"
    assert (http.method_type, http.url, http.request_timeout_milliseconds) == ("POST", REDEEM_URL, REQUEST_TIMEOUT_MS)
    assert http.body == (
        "{% $string({'t': $substring($replace($string(player_params.t), /[^A-Za-z0-9_-]/, ''), 0, 22), "
        "'sid': session.uuid, 'pc': 'fastm-demo-p-a'}) %}"
    )
    assert (http.cache, http.cache_scope, http.cache_reason) == (None, CacheScope.NONE, CACHE_REASON)
    assert http.output == OUTPUT
    assert "CacheScope" not in function.put_request() and "CacheReason" not in str(function.put_request())


def test_a_cached_redeem_is_refused(kit: SimpleNamespace, ticketed: Any) -> None:
    """R-F8: scope ``none`` sets no ``Cache``."""

    def cache(document: dict[str, Any]) -> None:
        kit.function_doc(document, "fastm_demo_p_a_ps_redeem")["HttpRequestConfiguration"]["Cache"] = {
            "TtlMinimumSeconds": 1,
            "TtlMaximumSeconds": 1,
            "Key": "{% 'redeem|v1|' & session.uuid %}",
        }

    assert Rule.F8 in kit.rules(validate_bundle(kit.edit(ticketed, cache)))


def test_a_redeem_that_writes_the_ticket_is_refused(kit: SimpleNamespace, ticketed: Any) -> None:
    """R-F6: PRE_SESSION never targets ``player_params.t``."""

    def write(document: dict[str, Any]) -> None:
        kit.function_doc(document, "fastm_demo_p_a_ps_redeem")["HttpRequestConfiguration"]["Output"] = {
            "player_params.t": "{% 'x' %}"
        }

    assert Rule.F6 in kit.rules(validate_bundle(kit.edit(ticketed, write)))


@pytest.mark.parametrize("url", ["http://redeem.example.com/v1/redeem", "redeem.example.com"])
def test_the_redeem_url_is_https(url: str) -> None:
    with pytest.raises(ValueError, match="https://"):
        IdentityContribution(redeem_url=url)


def test_a_configuration_with_a_quote_is_refused() -> None:
    with pytest.raises(ValueError, match="quote"):
        redeem_body("fastm-demo-p-'a")
