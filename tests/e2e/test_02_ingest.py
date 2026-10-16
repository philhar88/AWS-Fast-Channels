"""Ingest of a synthetic source: S3 upload with a JSON sidecar -> MediaConvert (one CMAF encode with every rendition
profile) -> Channel Assembly VOD source (SPEC §5.1)."""

from typing import TYPE_CHECKING

import pytest
from _stack import Plane, Stage
from harness import ASSET_ID, CUE_POINT_MS, PATTERN_SECONDS, Channel

from fast_channels.core.ingest import AssetKind
from fast_channels.core.manifests import BOUNDARY_TOLERANCE_MS, expected_signature, ladder_signature
from fast_channels.core.mediaconvert import output_prefix
from fast_channels.core.profiles import ManifestFormat, applicable, profile
from fast_channels.functions.tools._library_store import Asset, AssetStatus

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

pytestmark = pytest.mark.e2e


def test_ingest_is_ready(ingested: Asset) -> None:
    assert ingested.status is AssetStatus.READY
    assert (ingested.kind, ingested.content_id) == (AssetKind.CONTENT, ASSET_ID)
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
