"""Ingest of a synthetic source: S3 upload with a JSON sidecar -> MediaConvert (one HLS MPEG-TS encode, AVC with
AAC-LC muxed into every variant; ESAM splits every variant at the frame-snapped cue point) -> Channel Assembly VOD
source (SPEC §5.1)."""

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from _stack import Plane, Stage
from harness import ASSET_ID, CONTENT_ID, CUE_POINT_MS, CUE_TOLERANCE_MS, PATTERN_SECONDS, Channel
from playback import spans

from fast_channels.core.cost import encode_cost
from fast_channels.core.ingest import AssetKind
from fast_channels.core.manifests import (
    attributes,
    check_hls,
    expected_signature,
    ladder_signature,
    media_segment_durations,
    segment_boundaries_ms,
)
from fast_channels.core.mediaconvert import output_prefix
from fast_channels.core.profiles import applicable, profile
from fast_channels.functions.tools.library_store import Asset, AssetStatus, asset_key

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

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


def test_variants_are_mpeg_ts_with_identical_segments(
    stage: Stage, ladder: Ladder, variant_playlists: dict[str, str]
) -> None:
    """Every variant is MPEG-TS (``.ts`` segments served as ``video/MP2T``) and has the same EXTINFs: the AAC is
    muxed into each, and ESAM splits them all at the same frame."""
    durations = {uri: media_segment_durations(playlist) for uri, playlist in variant_playlists.items()}
    assert len(durations) == len(ladder.video), sorted(durations)
    assert len({tuple(d) for d in durations.values()}) == 1, durations
    lines = [line.strip() for playlist in variant_playlists.values() for line in playlist.splitlines()]
    segments = [line for line in lines if line and not line.startswith("#")]
    assert segments and all(segment.endswith(".ts") for segment in segments), segments[:3]
    s3 = stage.session.client("s3", region_name=stage.region)
    bucket, prefix = stage.output(Plane.DATA, "MediaBucketName"), output_prefix(ASSET_ID, ladder.version)
    content_type = s3.head_object(Bucket=bucket, Key=f"{prefix}{segments[0]}")["ContentType"]
    assert content_type.lower() == TS_CONTENT_TYPE, (segments[0], content_type)


def test_esam_splits_every_variant_at_the_cue_point(ladder: Ladder, variant_playlists: dict[str, str]) -> None:
    """The frame-snapped cue point is off the segment grid, so the encode used ESAM: every variant has a segment
    boundary there, and the segment that ends there is shorter than a grid segment (the split)."""
    cue = ladder.grid.snap_to_frame(CUE_POINT_MS)
    assert not ladder.grid.on_segment_grid(cue), "the scenario's cue point should need ESAM"
    shared = segment_boundaries_ms(*variant_playlists.values())
    assert any(abs(boundary - cue) <= CUE_TOLERANCE_MS for boundary in shared), (cue, shared)
    for uri, playlist in variant_playlists.items():
        ending = [s for s in spans(playlist) if abs(s.ends_ms - cue) <= CUE_TOLERANCE_MS]
        assert len(ending) == 1 and ending[0].duration_ms < ladder.grid.segment_ms, (uri, cue, ending)


def test_break_offsets_are_the_frame_snapped_cue_point(ingested: Asset, ladder: Ladder) -> None:
    """The sidecar's cue point, snapped to the nearest frame, is the asset's cue point and its one break offset (the
    shared segment boundary there, within a millisecond)."""
    cue = ladder.grid.snap_to_frame(CUE_POINT_MS)
    assert cue != CUE_POINT_MS, "the scenario's cue point should exercise frame snapping"
    assert ingested.cue_points_ms == [cue], ingested.cue_points_ms
    offsets = ingested.break_offsets_ms
    assert len(offsets) == 1 and abs(offsets[0] - cue) <= CUE_TOLERANCE_MS, (cue, offsets)


def test_the_estimated_cost_is_on_the_professional_tier(library: Table, ingested: Asset, ladder: Ladder) -> None:
    """ESAM bills every output of the job at the Professional tier, and the recorded estimate says so. The estimate
    is made from the probed source before the encode, so it is matched within a second of the encode's duration."""
    item = library.get_item(Key=asset_key(ingested.asset_id, ladder.version)).get("Item") or {}
    cost = item.get("estimated_cost_usd")  # numbers read through the resource API are Decimals
    assert isinstance(cost, Decimal), f"{ingested.asset_id} records no estimated_cost_usd ({cost!r})"
    recorded = float(cost)
    assert ingested.duration_ms is not None
    durations = range(ingested.duration_ms - 1000, ingested.duration_ms + 1001, 500)
    professional = {encode_cost(ladder, ms, professional=True) for ms in durations}
    basic = {encode_cost(ladder, ms) for ms in durations}
    assert any(recorded == pytest.approx(cost, abs=1e-6) for cost in professional), (recorded, professional, basic)


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
