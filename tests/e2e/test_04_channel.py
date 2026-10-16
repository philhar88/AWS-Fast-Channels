"""Scheduling through the tools, then the channel on air through the CDN (SPEC §6 and §11).

The schedule and channels tools are invoked as AgentCore Gateway invokes them. Three programs with one break each
are published to start in a few minutes and the channel is started; one SSAI, one SGAI and one DASH session then
watch the breaks air. MediaTailor imports a new creative at its first sight and fills that break with the slate,
so ads are asserted from the second break on.
"""

from typing import TYPE_CHECKING, Any

import pytest
from _stack import Plane, Stage
from harness import PROGRAMS, Channel, Observation, Player
from playback import codec_tags, media_segments, renditions, video_codecs

from fast_channels.core.profiles import ManifestFormat, profile

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder
    from fast_channels.functions.tools._library_store import Asset

pytestmark = pytest.mark.e2e

SCHEDULING_ERROR = "SCHEDULING_ERROR"
CODEC_TAGS = {"avc": "avc1", "hevc": "hvc1", "av1": "av01"}
"""Sample entries of each codec family in HLS ``CODECS`` (HEVC as hvc1, ladders.md §8)."""


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


def test_ssai_multivariant_has_every_codec(observation: Observation, ladder: Ladder) -> None:
    families = {codec.value for codec in ladder.codecs}
    assert video_codecs(observation.ssai_multivariant) == families
    assert codec_tags(observation.ssai_multivariant) == {CODEC_TAGS[family] for family in families}


def test_ssai_media_playlist_per_codec_family(observation: Observation, ladder: Ladder) -> None:
    for family in sorted(codec.value for codec in ladder.codecs):
        assert observation.ssai.get(family), f"no {family} segments: {observation.report()}"


def test_ssai_single_codec_outputs(player: Player, channel: Channel) -> None:
    """Each per-codec HLS output (hls-avc, hls-hevc, hls-av1) carries that codec family only, and plays."""
    checked = 0
    for output in channel.outputs:
        rendition_profile = profile(output)
        if rendition_profile.format is not ManifestFormat.HLS or len(rendition_profile.codecs or ()) != 1:
            continue
        multivariant = player.get(channel.playback[output]["ssai"])
        [family] = [codec.value for codec in rendition_profile.codecs or ()]
        assert video_codecs(multivariant.text) == {family}, (output, multivariant.text[:500])
        media = renditions(multivariant.text, str(multivariant.url))[family]
        assert media_segments(player.get(media).text, media), f"{output}: {media} has no segments"
        checked += 1
    assert checked, f"channel {channel.name} has no single-codec HLS output: {channel.outputs}"


def test_ssai_ads_from_the_second_break(observation: Observation) -> None:
    unfilled = [rendition for rendition in observation.ssai if not observation.ssai_filled(rendition)]
    assert observation.ssai and not unfilled, f"no stitched ads in {unfilled}: {observation.report()}"


def test_sgai_interstitial_when_the_break_airs(observation: Observation) -> None:
    assert observation.interstitials, f"no HLS Interstitial DATERANGE: {observation.report()}"
    assert all("X-ASSET-LIST" in daterange for daterange in observation.interstitials.values())


def test_sgai_asset_lists_carry_ads(observation: Observation) -> None:
    assert observation.sgai_filled, f"every asset list was empty: {observation.report()}"
    assert set(observation.ad_playlists.values()) == {200}, observation.ad_playlists


def test_dash_mpd(observation: Observation, channel: Channel) -> None:
    assert observation.mpds, f"no MPD: {observation.report()}"
    mpd = observation.mpds[-1]
    assert mpd.type == "dynamic" and mpd.periods, mpd
    dash = next(output for output in channel.outputs if "dash" in channel.playback[output])
    expected = {CODEC_TAGS[codec.value] for codec in profile(dash).codecs or ()}
    video = {codec.partition(".")[0] for codec in mpd.codecs} - {"mp4a"}
    assert not expected or video == expected, (dash, sorted(mpd.codecs))


def test_dash_ad_periods_from_the_second_break(observation: Observation) -> None:
    assert observation.dash_ad_periods, f"no ad period in the MPD: {observation.report()}"


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
