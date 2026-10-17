"""Ingest of a synthetic source: S3 upload with a JSON sidecar -> MediaConvert (one HLS MPEG-TS encode on the Basic
tier, AVC with AAC-LC muxed into every variant) -> Channel Assembly VOD source (SPEC §5.1)."""

from typing import TYPE_CHECKING

import pytest
from _stack import Plane, Stage
from harness import ASSET_ID, CONTENT_ID, CUE_POINT_MS, PATTERN_SECONDS, Channel

from fast_channels.core.ingest import AssetKind
from fast_channels.core.manifests import (
    attributes,
    check_hls,
    expected_signature,
    ladder_signature,
    media_segment_durations,
    parse_multivariant,
    segment_boundaries_ms,
)
from fast_channels.core.mediaconvert import output_prefix
from fast_channels.core.profiles import applicable, profile
from fast_channels.functions.tools.library_store import Asset, AssetStatus

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

pytestmark = pytest.mark.e2e

TS_CONTENT_TYPE = "video/mp2t"
"""Content-Type of an MPEG-TS segment (MediaConvert writes ``video/MP2T``; media types are case-insensitive)."""


def test_ingest_is_ready(ingested: Asset) -> None:
    assert ingested.status is AssetStatus.READY
    assert (ingested.kind, ingested.content_id) == (AssetKind.CONTENT, CONTENT_ID)
    assert ingested.duration_ms is not None
    assert abs(ingested.duration_ms - PATTERN_SECONDS * 1000) < 1000, ingested.duration_ms


def test_profile_manifests_written(stage: Stage, ingested: Asset, ladder: Ladder) -> None:
    """Every rendition profile's manifest is in S3 with exactly the ladder's signature (one AVC variant per rung,
    each with the muxed AAC-LC in CODECS), passes the multivariant checklist and says ``CLOSED-CAPTIONS=NONE`` on
    every variant."""
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
        if (signature := ladder_signature(text)) != expected_signature(ladder):
            problems.append(f"{key} has ladder signature {signature}")
        problems += [f"{key}: {finding}" for finding in check_hls(text)]
        stream_infs = [attributes(line) for line in text.splitlines() if line.startswith("#EXT-X-STREAM-INF:")]
        if any(variant.get("CLOSED-CAPTIONS") != "NONE" for variant in stream_infs):
            problems.append(f"{key}: a variant lacks CLOSED-CAPTIONS=NONE")
    assert not problems, problems


def test_variants_share_one_segment_grid(stage: Stage, ingested: Asset, ladder: Ladder) -> None:
    """There is no ESAM split on the Basic tier: every variant is MPEG-TS (``.ts`` segments served as
    ``video/MP2T``) with identical EXTINFs on the 6006 ms grid, and the snapped cue point is a boundary they share."""
    s3 = stage.session.client("s3", region_name=stage.region)
    bucket, prefix = stage.output(Plane.DATA, "MediaBucketName"), output_prefix(ASSET_ID, ladder.version)

    def text(key: str) -> str:
        return str(s3.get_object(Bucket=bucket, Key=f"{prefix}{key}")["Body"].read().decode())

    playlists = {variant.uri: text(variant.uri) for variant in parse_multivariant(text("main.m3u8"))}
    durations = {uri: media_segment_durations(playlist) for uri, playlist in playlists.items()}
    assert len({tuple(d) for d in durations.values()}) == 1, durations
    lines = [line.strip() for playlist in playlists.values() for line in playlist.splitlines()]
    segments = [line for line in lines if line and not line.startswith("#")]
    assert segments and all(segment.endswith(".ts") for segment in segments), segments[:3]
    content_type = s3.head_object(Bucket=bucket, Key=f"{prefix}{segments[0]}")["ContentType"]
    assert content_type.lower() == TS_CONTENT_TYPE, (segments[0], content_type)
    snapped = ladder.grid.snap_to_segment(CUE_POINT_MS)
    assert snapped in segment_boundaries_ms(*playlists.values()), (snapped, durations)


def test_break_offsets_include_the_cue_point(ingested: Asset, ladder: Ladder) -> None:
    """The sidecar's cue point, snapped to the segment grid (32000 ms -> 30030 ms at 29.97 fps), is a break offset,
    exactly."""
    snapped = ladder.grid.snap_to_segment(CUE_POINT_MS)
    assert snapped != CUE_POINT_MS, "the scenario's cue point should exercise snapping"
    assert snapped in ingested.break_offsets_ms, (snapped, ingested.break_offsets_ms)


def test_vod_source_has_every_package_configuration(stage: Stage, ingested: Asset, channel: Channel) -> None:
    """The VOD source carries an HLS package configuration per registered profile, covering every channel output,
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
        assert package["Type"] == "HLS" and package["Path"].endswith(profile(group).manifest_key("")), package
        s3.head_object(Bucket=bucket, Key=package["Path"].lstrip("/"))
    assert source["Tags"].get("fast:app") == stage.prefix, source["Tags"]
