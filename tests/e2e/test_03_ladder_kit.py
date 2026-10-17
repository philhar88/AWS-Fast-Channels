"""The default ladder's kit, provisioned at deploy time (SPEC §5.2): filler and break slates as VOD sources, house
ads as SGAI-ready creatives and the static VAST pod, all readable through the CDN as MediaTailor reads them."""

from typing import TYPE_CHECKING

import pytest
from _stack import Plane, Stage
from playback import media_segments, renditions, vast_ads, vast_durations, vast_media_files

from fast_channels.core.manifests import expected_signature, ladder_signature
from fast_channels.core.specs import vast_key
from fast_channels.core.timeline import BREAK_SLATE_GOPS, HOUSE_AD_GOPS
from fast_channels.functions.tools.library_store import LadderRecord, LadderStatus, ladder_key

if TYPE_CHECKING:
    from harness import Player

pytestmark = pytest.mark.e2e

HLS: str = "application/x-mpegURL"


@pytest.fixture(scope="module")
def kit_ladder(stage: Stage) -> LadderRecord:
    """The media library's record of the deployment's default ladder version."""
    version = stage.output(Plane.MEDIA, "DefaultLadderVersion")
    table = stage.session.resource("dynamodb", region_name=stage.region).Table(
        stage.output(Plane.DATA, "LibraryTableName")
    )
    item = table.get_item(Key=ladder_key(version)).get("Item")
    assert item, f"no LADDER item for {version}"
    return LadderRecord.from_item(item)


def test_default_ladder_kit_ready(stage: Stage, kit_ladder: LadderRecord) -> None:
    assert kit_ladder.status is LadderStatus.READY, kit_ladder.error
    kit = kit_ladder.kit
    assert kit.filler
    assert sorted(kit.break_slates) == sorted(BREAK_SLATE_GOPS), kit.break_slates
    assert len(kit.house_ads) == len(HOUSE_AD_GOPS), kit.house_ads
    assert kit.vast_key == vast_key(kit_ladder.ladder_version)
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    location = stage.output(Plane.MEDIA, "SourceLocationName")
    for vod_source in (kit.filler, *kit.break_slates.values()):
        mediatailor.describe_vod_source(SourceLocationName=location, VodSourceName=vod_source)


def test_vast_pod_and_creatives_readable_via_cdn(stage: Stage, player: Player, kit_ladder: LadderRecord) -> None:
    """The pod cycles through the house ads until it covers the longest break slate, each with one streaming HLS
    file: a multivariant with the ladder's variants (preconditioned ads must match the content's), whose media
    playlists use absolute ``.ts`` segment URIs on the CDN (SGAI players resolve asset URIs against MediaTailor,
    e2e.md §5.4)."""
    content = stage.output(Plane.MEDIA, "ContentBaseUrl").rstrip("/")
    vast = player.get(f"{content}/{kit_ladder.kit.vast_key}")
    assert "xml" in vast.headers.get("content-type", ""), vast.headers
    pod = vast_ads(vast.text)
    house_ads = kit_ladder.kit.house_ads
    assert set(pod) == set(house_ads) and pod == [house_ads[n % len(house_ads)] for n in range(len(pod))], pod
    ladder = kit_ladder.definition()
    longest_break_s = ladder.grid.gop_exact(max(BREAK_SLATE_GOPS)) / 1000
    assert sum(vast_durations(vast.text)) >= longest_break_s, vast_durations(vast.text)
    files = vast_media_files(vast.text)
    assert [f.mime_type for f in files] == [HLS] * len(pod), files
    for media_file in {f.uri: f for f in files}.values():
        multivariant = player.get(media_file.uri)
        assert ladder_signature(multivariant.text) == expected_signature(ladder), media_file
        first = next(iter(renditions(multivariant.text, str(multivariant.url)).values()))
        segments = media_segments(player.get(first).text, first)
        absolute_ts = all(s.uri.startswith(content) and s.uri.endswith(".ts") for s in segments)
        assert segments and absolute_ts, (first, segments[:2])
        assert player.get(segments[0].uri).content, segments[0]
    assert player.get(f"{content}/ads/b.gif").headers.get("content-type") == "image/gif"
