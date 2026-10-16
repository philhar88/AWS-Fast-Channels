"""Ingest of a synthetic source: S3 upload with a JSON sidecar and a caption file -> MediaConvert (one CMAF encode with
every rendition profile and the ladder's IMSC tracks) -> Channel Assembly VOD source (SPEC §5.1)."""

import re
from typing import TYPE_CHECKING

import pytest
from _stack import Plane, Stage
from harness import ASSET_ID, CONTENT_ID, CUE_POINT_MS, PATTERN_SECONDS, Channel, caption_text

from fast_channels.core.captions import CaptionOrigin
from fast_channels.core.ingest import AssetKind
from fast_channels.core.manifests import (
    BOUNDARY_TOLERANCE_MS,
    attributes,
    expected_signature,
    ladder_signature,
    media_segment_durations,
    parse_multivariant,
    subtitle_renditions,
)
from fast_channels.core.mediaconvert import output_prefix
from fast_channels.core.mp4 import sample_entries
from fast_channels.core.profiles import ManifestFormat, applicable, profile
from fast_channels.functions.tools.library_store import Asset, AssetStatus

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

pytestmark = pytest.mark.e2e


def test_ingest_is_ready(ingested: Asset) -> None:
    assert ingested.status is AssetStatus.READY
    assert (ingested.kind, ingested.content_id) == (AssetKind.CONTENT, CONTENT_ID)
    assert ingested.duration_ms is not None
    assert abs(ingested.duration_ms - PATTERN_SECONDS * 1000) < 1000, ingested.duration_ms


def test_profile_manifests_written(stage: Stage, ingested: Asset, ladder: Ladder) -> None:
    """Every rendition profile the ladder supports is in S3, and every HLS one has exactly its ladder signature."""
    s3 = stage.session.client("s3", region_name=stage.region)
    bucket = stage.output(Plane.DATA, "MediaBucketName")
    problems = []
    for rendition_profile in applicable(ladder):
        key = rendition_profile.manifest_key(output_prefix(ASSET_ID, ladder.version))
        try:
            text = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
        except s3.exceptions.NoSuchKey:
            problems.append(f"{key} is missing")
            continue
        if rendition_profile.format is ManifestFormat.DASH:
            if "<MPD" not in text:
                problems.append(f"{key} is not an MPD")
        elif (signature := ladder_signature(text)) != expected_signature(ladder, rendition_profile):
            problems.append(f"{key} has ladder signature {signature}")
    assert not problems, problems


def test_break_offsets_include_the_cue_point(ingested: Asset, ladder: Ladder) -> None:
    """The sidecar's cue point, snapped to the GOP grid (30030 ms at 29.97 fps), is a valid break offset."""
    snapped = ladder.grid.snap_to_gop(CUE_POINT_MS)
    nearest = min(ingested.break_offsets_ms, key=lambda offset: abs(offset - snapped), default=None)
    assert nearest is not None and abs(nearest - snapped) <= BOUNDARY_TOLERANCE_MS, (snapped, ingested.break_offsets_ms)


def test_vod_source_has_every_package_configuration(stage: Stage, ingested: Asset, channel: Channel) -> None:
    """The VOD source carries a package configuration per registered profile, covering every channel output,
    each pointing at a manifest that exists."""
    assert ingested.vod_source, "no VOD source on the asset"
    location = ingested.source_location or stage.output(Plane.MEDIA, "SourceLocationName")
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    source = mediatailor.describe_vod_source(SourceLocationName=location, VodSourceName=ingested.vod_source)
    packages = {c["SourceGroup"]: c for c in source["HttpPackageConfigurations"]}
    assert set(packages) == set(ingested.packages), (sorted(packages), sorted(ingested.packages))
    assert set(channel.outputs) <= set(packages), f"outputs {channel.outputs} vs source groups {sorted(packages)}"
    s3 = stage.session.client("s3", region_name=stage.region)
    bucket = stage.output(Plane.DATA, "MediaBucketName")
    for group, package in packages.items():
        assert package["Type"] == profile(group).format.value, package
        s3.head_object(Bucket=bucket, Key=package["Path"].lstrip("/"))
    assert source["Tags"].get("fast:app") == stage.prefix, source["Tags"]


def test_captions_come_from_the_provided_file(ingested: Asset, ladder: Ladder) -> None:
    """The ``<asset>.en.vtt`` next to the upload is the English track; other ladder languages are empty tracks."""
    assert [c.language for c in ingested.captions] == [t.language for t in ladder.captions]
    english = next(c for c in ingested.captions if c.language == "en")
    assert english.origin is CaptionOrigin.PROVIDED and str(english.uri).endswith(f"/e2e/{ASSET_ID}.en.vtt"), english


def test_caption_tracks_follow_the_video_grid(stage: Stage, ingested: Asset, ladder: Ladder) -> None:
    """Every manifest lists the IMSC tracks; their segments end where the video's do, so every break offset is a
    boundary of every track (Channel Assembly's rule), and they carry the provided cues."""
    s3 = stage.session.client("s3", region_name=stage.region)
    bucket, prefix = stage.output(Plane.DATA, "MediaBucketName"), output_prefix(ASSET_ID, ladder.version)

    def text(key: str) -> str:
        return str(s3.get_object(Bucket=bucket, Key=f"{prefix}{key}")["Body"].read().decode())

    main = text("main.m3u8")
    assert [a["LANGUAGE"] for a in subtitle_renditions(main)] == [t.language for t in ladder.captions]
    first_variant = parse_multivariant(main)[0].uri
    for rendition in subtitle_renditions(main):
        subtitles, video = text(rendition["URI"]), text(first_variant)
        assert media_segment_durations(subtitles) == media_segment_durations(video), rendition["URI"]
    english = next(a["URI"] for a in subtitle_renditions(main) if a["LANGUAGE"] == "en")
    first = next(line for line in text(english).splitlines() if line and not line.startswith("#"))
    segment = s3.get_object(Bucket=bucket, Key=f"{prefix}{first}")["Body"].read().replace(b"&#xA0;", b" ")
    assert caption_text(1).encode() in segment  # MediaConvert writes the spaces of a cue as &#xA0;


def test_captions_are_their_own_cmaf_track(stage: Stage, ingested: Asset, ladder: Ladder) -> None:
    """CMAF carries one track per file: every IMSC1 rendition has its own init segment with one ``stpp`` track, and
    no video or audio init segment carries a second (caption) track."""
    s3 = stage.session.client("s3", region_name=stage.region)
    bucket, prefix = stage.output(Plane.DATA, "MediaBucketName"), output_prefix(ASSET_ID, ladder.version)

    def read(key: str) -> bytes:
        return bytes(s3.get_object(Bucket=bucket, Key=f"{prefix}{key}")["Body"].read())

    main = read("main.m3u8").decode()
    playlists = [*(v.uri for v in parse_multivariant(main)), *(a["URI"] for a in subtitle_renditions(main))]
    playlists += [a["URI"] for line in main.splitlines() if (a := attributes(line)).get("TYPE") == "AUDIO"]
    entries = {}
    for playlist in dict.fromkeys(playlists):
        init = re.search(r'#EXT-X-MAP:URI="([^"]+)"', read(playlist).decode())
        assert init, playlist
        entries[playlist] = sample_entries(read(init[1]))
    assert all(len(found) == 1 for found in entries.values()), entries
    for rendition in subtitle_renditions(main):
        assert entries[rendition["URI"]] == ["stpp"], rendition
