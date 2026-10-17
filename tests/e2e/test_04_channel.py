"""Scheduling through the tools, then the channel on air through the CDN (SPEC §6 and §11).

The schedule and channels tools are invoked as AgentCore Gateway invokes them. Three programs with one break each
are published to start in a few minutes and the channel is started; an SSAI session then watches the breaks air in
every variant. Each break is at the test pattern's frame-snapped cue point, where ESAM split the encode. MediaTailor
imports a new creative at its first sight and fills that break with the slate, so ads are asserted from the second
break on.
"""

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

import pytest
from _stack import Plane, Stage
from harness import CUE_POINT_MS, CUE_TOLERANCE_MS, PROGRAMS, Channel, Observation
from playback import spans

from fast_channels.core.manifests import expected_signature, ladder_signature

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder
    from fast_channels.functions.tools.library_store import Asset

pytestmark = pytest.mark.e2e

SCHEDULING_ERROR = "SCHEDULING_ERROR"
PDT_TOLERANCE: Final = timedelta(milliseconds=1)
"""``EXT-X-PROGRAM-DATE-TIME``, ``START-DATE`` and EXTINF are all written in whole milliseconds."""


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


def test_breaks_start_at_the_frame_snapped_cue_point(
    observation: Observation, ingested: Asset, ladder: Ladder, variant_playlists: dict[str, str]
) -> None:
    """Channel Assembly leaves each program for its break exactly at the frame-snapped cue point, in every variant:
    the break's ad marker (SCTE-35 ``EXT-X-DATERANGE``) follows the pattern's segment that ends at the cue in the
    encode (the ESAM split), its ``START-DATE`` is that segment's end and the start of the break's first segment
    (program date-times), for at least the two breaks the session watched."""
    cue = ladder.grid.snap_to_frame(CUE_POINT_MS)
    ends_ms = {span.name: span.ends_ms for text in variant_playlists.values() for span in spans(text)}
    problems: list[str] = []
    for variant in observation.ssai:
        found = observation.splices.get(variant, {}).values()
        cuts = [(s, s.before) for s in found if s.before is not None and s.before.asset_id == ingested.asset_id]
        if len(cuts) < 2:
            problems.append(f"{variant}: {len(cuts)} breaks out of the test pattern seen")
        for cut, before in cuts:
            at = f"{variant} break {cut.id} at {cut.starts:%H:%M:%S.%f}"
            offset = ends_ms.get(before.name)
            if offset is None or abs(offset - cue) > CUE_TOLERANCE_MS:
                problems.append(f"{at}: after {before.name}, which ends at {offset} ms of the encode, not {cue} ms")
            if before.ends is None or abs(before.ends - cut.starts) > PDT_TOLERANCE:
                problems.append(f"{at}: the segment before it ends at {before.ends}")
            first = cut.after.starts if cut.after else None
            if first is None or abs(first - cut.starts) > PDT_TOLERANCE:
                problems.append(f"{at}: the break's first segment starts at {first}")
    assert observation.ssai and not problems, (problems, observation.report())


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
