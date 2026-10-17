from typing import Any, Final

import pytest
from botocore.stub import ANY

from fast_channels.core.ladder import Ladder
from fast_channels.core.manifests import ladder_signature, parse_multivariant
from fast_channels.core.naming import creative_id, ladder_path
from fast_channels.functions.ingest import finalize
from fast_channels.functions.ingest.finalize import InvalidOutputsError

STANDARD = Ladder.builtin("standard")
CONTENT_BASE = "https://media.example.com"
PROFILES = ("hls",)
CONTENT_BOUNDARIES = [6006, 12012, 18018, 24024, 30030, 36036]  # 6 x 6.006 s segments, then 4.037 s


def asset(kind: str, asset_id: str, prefix: str, cue_points_ms: list[int] | None = None) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "kind": kind,
        "ladder_version": STANDARD.version,
        "correlation_id": f"{asset_id}-run",
        "ladder": STANDARD.model_dump(mode="json"),
        "output_prefix": prefix,
        "cue_points_ms": cue_points_ms or [],
    }


def run(store: Any, item: dict[str, Any]) -> finalize.Finalized:
    return finalize.finalize(store, item, content_base_url=CONTENT_BASE, package_profiles=PROFILES)


def test_slate_is_verified_and_fixed_up(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "break-15-1a2b3c")
    objects_before = dict(objects)
    s = store(objects)

    result = run(s, asset("slate", "break-15-1a2b3c", prefix))

    assert result.duration_ms == 30030  # 15 GOPs of 60 frames at 29.97 fps: 5 segments
    assert result.break_offsets_ms == []
    assert result.creative is None
    assert tuple(tuple(row) for row in result.signature) == ladder_signature(
        objects_before[f"{prefix}main.m3u8"].decode()
    )
    [registration] = result.registrations
    assert registration == {
        "asset_id": "break-15-1a2b3c",
        "ladder_version": STANDARD.version,
        "kind": "slate",
        "vod_source": f"break-15-1a2b3c-{ladder_path(STANDARD.version)}",
        "output_prefix": prefix,
        "profiles": ["hls"],
        "correlation_id": "break-15-1a2b3c-run",
    }
    # The multivariant advertises the ladder's nominal bandwidths (the QVBR slate measured about 0.3 Mbit/s).
    main = parse_multivariant(s.objects[f"{prefix}main.m3u8"].decode())
    assert [(v.bandwidth, v.average_bandwidth) for v in main] == [STANDARD.nominal_bandwidth(r) for r in STANDARD.video]
    assert result.rewritten == [f"{prefix}main.m3u8"]
    assert set(s.writes) == {f"{prefix}main.m3u8"}  # media playlists and segments stay as MediaConvert wrote them


def test_finalize_is_idempotent(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "filler-1a2b3c")
    s = store(objects)
    run(s, asset("slate", "filler-1a2b3c", prefix))
    again = store(s.objects)

    assert run(again, asset("slate", "filler-1a2b3c", prefix)).rewritten == []
    assert again.writes == {}


@pytest.mark.parametrize(
    ("cue_points", "offsets"),
    [
        ([], CONTENT_BOUNDARIES),
        ([12012, 24024], [12012, 24024]),
        ([12112], [12012]),
        ([12113, 13480, 15015, 40073], []),
    ],
    ids=["natural", "cue-points", "within-100-ms", "not-a-boundary"],
)
def test_content_break_offsets(store: Any, encode: Any, cue_points: list[int], offsets: list[int]) -> None:
    prefix, objects = encode("content", "bbb-1a2b3c")

    result = run(store(objects), asset("content", "bbb-1a2b3c", prefix, cue_points))

    assert result.break_offsets_ms == offsets
    assert result.duration_ms == 40073


def esam_split(text: str, durations: list[str]) -> str:
    """``text`` (a media playlist of the content sample) with its segments replaced by ``durations``."""
    lines = text.splitlines()
    header = lines[: next(i for i, line in enumerate(lines) if line.startswith("#EXTINF:"))]
    stem = next(line for line in lines if line.endswith(".ts")).rsplit("_", 1)[0]
    segments = [f"#EXTINF:{d},\n{stem}_{n:05d}.ts" for n, d in enumerate(durations, start=1)]
    return "\n".join([*header, *segments, "#EXT-X-ENDLIST"]) + "\n"


# What ESAM made of the content on a real TS job (2026-10-17): cues at 13.480 s and 26.994 s (frames 404 and 809)
# split every variant there, each adding one short segment, and the 6.006 s grid resumed at 18.018 s and 30.030 s.
ESAM_DURATIONS: Final = ["6.006", "6.006", "1.468", "4.538", "6.006", "2.970", "3.036", "6.006", "4.037"]


@pytest.mark.parametrize(
    ("cue_points", "offsets"),
    [
        ([], [6006, 12012, 13480, 18018, 24024, 26994, 30030, 36036]),
        ([13480, 26994], [13480, 26994]),
        ([13481, 26993], [13480, 26994]),
        ([13480, 15015], [13480]),
    ],
    ids=["natural", "frame-snapped-cues", "ms-rounding", "one-not-a-boundary"],
)
def test_break_offsets_keep_frame_accurate_cues_that_esam_made_boundaries(
    store: Any, encode: Any, cue_points: list[int], offsets: list[int]
) -> None:
    prefix, objects = encode("content", "bbb-1a2b3c")
    for rendition in STANDARD.video:
        key = f"{prefix}main{rendition.name_modifier}.m3u8"
        objects[key] = esam_split(objects[key].decode(), ESAM_DURATIONS).encode()

    result = run(store(objects), asset("content", "bbb-1a2b3c", prefix, cue_points))

    assert result.break_offsets_ms == offsets
    assert result.duration_ms == 40073


def test_breaks_go_only_where_every_variant_has_a_boundary(store: Any, encode: Any) -> None:
    prefix, objects = encode("content", "bbb-1a2b3c")
    key = f"{prefix}main_avc-360p.m3u8"
    # One variant cut 8.008 + 4.004 s where the others have 6.006 + 6.006 s: 12.012 s is no longer shared.
    text = objects[key].decode()
    text = text.replace("#EXTINF:6.006,\nmain_avc-360p_00002.ts", "#EXTINF:8.008,\nmain_avc-360p_00002.ts")
    text = text.replace("#EXTINF:6.006,\nmain_avc-360p_00003.ts", "#EXTINF:4.004,\nmain_avc-360p_00003.ts")
    objects[key] = text.encode()

    result = run(store(objects), asset("content", "bbb-1a2b3c", prefix))

    assert result.break_offsets_ms == [6006, 18018, 24024, 30030, 36036]


def test_missing_manifest_fails(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "filler-1a2b3c")
    del objects[f"{prefix}main.m3u8"]

    with pytest.raises(InvalidOutputsError, match=r"missing manifest .*main\.m3u8"):
        run(store(objects), asset("slate", "filler-1a2b3c", prefix))


def test_wrong_ladder_fails(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "filler-1a2b3c")
    text = objects[f"{prefix}main.m3u8"].decode()
    objects[f"{prefix}main.m3u8"] = text.replace("RESOLUTION=1920x1080", "RESOLUTION=1280x720", 1).encode()

    with pytest.raises(InvalidOutputsError, match=r"main\.m3u8 does not have the variants"):
        run(store(objects), asset("slate", "filler-1a2b3c", prefix))


def test_missing_media_playlist_fails(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "filler-1a2b3c")
    del objects[f"{prefix}main_avc-1080p.m3u8"]

    with pytest.raises(InvalidOutputsError, match=r"missing .*main_avc-1080p\.m3u8"):
        run(store(objects), asset("slate", "filler-1a2b3c", prefix))


def test_ad_gets_a_manifest_copy_with_absolute_segment_uris(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "house-ad-2-1a2b3c")
    s = store(objects)

    result = run(s, asset("ad", "house-ad-2-1a2b3c", prefix))

    creative = creative_id("house-ad-2-1a2b3c", STANDARD.version, CONTENT_BASE)
    ads = f"ads/{ladder_path(STANDARD.version)}/{creative}/"
    assert result.registrations == []
    assert result.creative == finalize.Creative(
        creative_id=creative, ad_prefix=ads, hls_uri=f"{CONTENT_BASE}/{ads}main.m3u8"
    )
    # The multivariant keeps relative children (MediaTailor requires them next to it) and nominal bandwidths.
    main = s.objects[f"{ads}main.m3u8"].decode()
    assert main == s.objects[f"{prefix}main.m3u8"].decode()
    assert all(not v.uri.startswith("http") for v in parse_multivariant(main))
    # Media playlists point at the original segments on the content host (no segment copies).
    for rendition in STANDARD.video:
        media = s.objects[f"{ads}main{rendition.name_modifier}.m3u8"].decode().splitlines()
        assert f"{CONTENT_BASE}/{prefix}main{rendition.name_modifier}_00001.ts" in media
        assert "#EXT-X-ENDLIST" in media
    assert not any(k.endswith(".ts") for k in s.writes)
    assert {k for k in s.writes if k.startswith(ads)} == {f"{ads}main.m3u8"} | {
        f"{ads}main{r.name_modifier}.m3u8" for r in STANDARD.video
    }


def test_handler_records_the_encode(
    store: Any, encode: Any, stubs: Any, lambda_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, objects = encode("slate", "filler-1a2b3c")
    monkeypatch.setattr(finalize, "S3Store", lambda bucket: store(objects))
    stubs["dynamodb"].add_response(
        "update_item",
        {},
        {
            "TableName": "library",
            "Key": {"pk": "ASSET#filler-1a2b3c", "sk": f"LADDER#{STANDARD.version}"},
            "UpdateExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ExpressionAttributeValues": ANY,
            "ConditionExpression": ANY,
        },
    )

    result = finalize.handler({"asset": asset("slate", "filler-1a2b3c", prefix), "job_id": "j-1"}, lambda_context)

    assert result["duration_ms"] == 30030
    assert result["registrations"][0]["vod_source"].startswith("filler-1a2b3c-")
