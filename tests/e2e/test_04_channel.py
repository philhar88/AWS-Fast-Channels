"""Scheduling through the tools, then the channel on air through the CDN (SPEC §6 and §11).

The schedule and channels tools are invoked as AgentCore Gateway invokes them. Three programs with one break each
are published to start in a few minutes and the channel is started; one SSAI and one SGAI session then watch the
breaks air. MediaTailor imports a new creative at its first sight and fills that break with the slate, so ads are
asserted from the second break on.

The SGAI checks are server-side: the interstitial DATERANGE, its filled asset list and the ad playlists. Whether a
player enters the interstitial is not covered (hls.js 1.7.3 stalls entering an append-in-place interstitial when
the AAC audio is muxed, docs/ad-insertion.md).
"""

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest
from _stack import Plane, Stage
from harness import PROGRAMS, Channel, Observation

from fast_channels.core.manifests import expected_signature, ladder_signature

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder
    from fast_channels.functions.tools.library_store import Asset

pytestmark = pytest.mark.e2e

SCHEDULING_ERROR = "SCHEDULING_ERROR"


def test_plan_is_publishable(plan: dict[str, Any]) -> None:
    errors = [violation for violation in plan["violations"] if violation["severity"] == "error"]
    assert plan["publishable"] and not errors, plan["violations"]
    assert len(plan["programs"]) == PROGRAMS
    assert sum(len(program["breaks"]) for program in plan["programs"]) >= 2, plan["programs"]


def test_publish(plan: dict[str, Any], published: dict[str, Any]) -> None:
    assert published["plan_id"] == plan["plan_id"]
    assert published["published"] == PROGRAMS
    assert [p["name"] for p in published["programs"]] == [p["name"] for p in plan["programs"]]


def test_channel_running(running: dict[str, Any], channel: Channel) -> None:
    assert (running["channel"], running["state"]) == (channel.name, "RUNNING")


def test_ssai_multivariant_lists_the_ladder(observation: Observation, ladder: Ladder) -> None:
    """Channel Assembly and MediaTailor pass the ladder's variants through, each AVC with the muxed AAC-LC in
    CODECS, and there are no audio or subtitle renditions."""
    assert sorted(ladder_signature(observation.ssai_multivariant)) == sorted(expected_signature(ladder))
    assert "#EXT-X-MEDIA:" not in observation.ssai_multivariant, observation.ssai_multivariant[:500]


def test_ssai_plays_every_variant(observation: Observation, ladder: Ladder) -> None:
    """A media playlist per variant plays, and its content (programs, slates, filler) is MPEG-TS."""
    assert len(observation.ssai) == len(ladder.video), observation.report()
    for variant, segments in observation.ssai.items():
        content = [uri for uri, s in segments.items() if not s.is_ad_of(observation.house_ads)]
        assert content and all(urlsplit(uri).path.endswith(".ts") for uri in content), (variant, content[:3])


def test_ssai_ads_from_the_second_break(observation: Observation) -> None:
    unfilled = [variant for variant in observation.ssai if not observation.ssai_filled(variant)]
    assert observation.ssai and not unfilled, f"no stitched ads in {unfilled}: {observation.report()}"


def test_sgai_interstitial_when_the_break_airs(observation: Observation) -> None:
    assert observation.interstitials, f"no HLS Interstitial DATERANGE: {observation.report()}"
    assert all("X-ASSET-LIST" in daterange for daterange in observation.interstitials.values())


def test_sgai_asset_lists_carry_ads(observation: Observation) -> None:
    assert observation.sgai_filled, f"every asset list was empty: {observation.report()}"
    assert set(observation.ad_playlists.values()) == {200}, observation.ad_playlists


def test_sgai_ads_match_the_ladder(observation: Observation, ladder: Ladder) -> None:
    """The preconditioned house ads were encoded on the channel's ladder: an interstitial's multivariant, as
    MediaTailor re-hosts it, lists the content's variants."""
    assert observation.ad_multivariants, f"no SGAI ad multivariant: {observation.report()}"
    for uri, text in observation.ad_multivariants.items():
        assert sorted(ladder_signature(text)) == sorted(expected_signature(ladder)), uri


def test_no_scheduling_errors(stage: Stage, channel: Channel, ingested: Asset, observation: Observation) -> None:
    """Neither the channel nor the test pattern's VOD source raised a SCHEDULING_ERROR (also when Channel Assembly
    reports it as a PROGRAM_ALERT, with the code in the message)."""
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    location = ingested.source_location or stage.output(Plane.MEDIA, "SourceLocationName")
    resources = [
        mediatailor.describe_channel(ChannelName=channel.name)["Arn"],
        mediatailor.describe_vod_source(SourceLocationName=location, VodSourceName=str(ingested.vod_source))["Arn"],
    ]
    pages = (page for arn in resources for page in mediatailor.get_paginator("list_alerts").paginate(ResourceArn=arn))
    errors = [
        alert
        for page in pages
        for alert in page.get("Items", [])
        if SCHEDULING_ERROR in (alert.get("Category", ""), alert["AlertCode"])
        or SCHEDULING_ERROR in alert.get("AlertMessage", "")
    ]
    assert not errors, errors
