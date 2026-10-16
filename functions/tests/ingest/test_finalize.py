from typing import Any

import pytest
from botocore.stub import ANY

from fast_channels.core.ladder import Ladder
from fast_channels.core.manifests import ladder_signature, parse_multivariant
from fast_channels.core.mp4 import AV1C_PATH, find_boxes
from fast_channels.core.naming import creative_id, ladder_path
from fast_channels.functions.ingest import finalize
from fast_channels.functions.ingest.finalize import InvalidOutputsError, child_uris, duration_ms, with_base_url

STANDARD = Ladder.builtin("standard")
CONTENT_BASE = "https://media.example.com"
PROFILES = ("hls", "hls-avc", "hls-hevc", "hls-av1", "dash-avc")


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


def av1c(init: bytes) -> tuple[int, int]:
    """(seq_level_idx, high_bitdepth) of an init segment's av1C."""
    [box] = find_boxes(init, AV1C_PATH)
    record = init[box.payload_offset : box.end]
    return record[1] & 0x1F, record[2] >> 6 & 1


def test_slate_is_verified_and_fixed_up(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "break-15-1a2b3c")
    objects_before = dict(objects)
    s = store(objects)

    result = run(s, asset("slate", "break-15-1a2b3c", prefix))

    assert result.duration_ms == 10010  # 5 GOPs of 60 frames at 29.97 fps
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
        "profiles": list(PROFILES),
        "correlation_id": "break-15-1a2b3c-run",
    }
    # Every AV1 init now declares its rung's level and 10-bit depth (MediaConvert writes level 0, 8-bit).
    levels = {r.name: av1c(s.objects[f"{prefix}main_{r.name}init.cmfv"]) for r in STANDARD.video if r.codec == "av1"}
    assert levels == {"av1-1080p": (8, 1), "av1-720p": (5, 1), "av1-540p": (4, 1), "av1-360p": (1, 1)}
    # Every HLS multivariant advertises the ladder's nominal bandwidths.
    main = parse_multivariant(s.objects[f"{prefix}main.m3u8"].decode())
    assert [(v.bandwidth, v.average_bandwidth) for v in main] == [STANDARD.nominal_bandwidth(r) for r in STANDARD.video]
    assert set(result.rewritten) == set(s.writes)
    assert {k.rsplit("/", 1)[-1] for k in s.writes if k.endswith(".m3u8")} == {
        "main.m3u8",
        "main_avc.m3u8",
        "main_hevc.m3u8",
        "main_av1.m3u8",
        "main_sd.m3u8",
        "main_hd.m3u8",
    }


def test_finalize_is_idempotent(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "filler-1a2b3c")
    s = store(objects)
    run(s, asset("slate", "filler-1a2b3c", prefix))
    again = store(s.objects)

    assert run(again, asset("slate", "filler-1a2b3c", prefix)).rewritten == []
    assert again.writes == {}


@pytest.mark.parametrize(
    ("cue_points", "offsets"),
    [([], [4004]), ([4004], [4004]), ([4100], [4004]), ([8008], [])],
    ids=["natural", "esam", "snapped-nearby", "not-a-boundary"],
)
def test_content_break_offsets(store: Any, encode: Any, cue_points: list[int], offsets: list[int]) -> None:
    prefix, objects = encode("content", "src25i-1a2b3c")  # ESAM split at 4004 ms, every track

    result = run(store(objects), asset("content", "src25i-1a2b3c", prefix, cue_points))

    assert result.break_offsets_ms == offsets
    assert result.duration_ms == 10010


def test_missing_profile_manifest_fails(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "filler-1a2b3c")
    del objects[f"{prefix}main_av1.mpd"]

    with pytest.raises(InvalidOutputsError, match=r"main_av1\.mpd"):
        run(store(objects), asset("slate", "filler-1a2b3c", prefix))


def test_wrong_ladder_fails(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "filler-1a2b3c")
    text = objects[f"{prefix}main_hevc.m3u8"].decode()
    objects[f"{prefix}main_hevc.m3u8"] = text.replace("RESOLUTION=1920x1080", "RESOLUTION=1280x720", 1).encode()

    with pytest.raises(InvalidOutputsError, match=r"main_hevc\.m3u8 does not have the variants"):
        run(store(objects), asset("slate", "filler-1a2b3c", prefix))


def test_missing_av1_init_fails(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "filler-1a2b3c")
    del objects[f"{prefix}main_av1-540pinit.cmfv"]

    with pytest.raises(InvalidOutputsError, match="init segment"):
        run(store(objects), asset("slate", "filler-1a2b3c", prefix))


def test_ad_gets_an_sgai_ready_copy(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "house-ad-2-1a2b3c")
    s = store(objects)

    result = run(s, asset("ad", "house-ad-2-1a2b3c", prefix))

    creative = creative_id("house-ad-2-1a2b3c", STANDARD.version, CONTENT_BASE)
    ads = f"ads/{ladder_path(STANDARD.version)}/{creative}/"
    assert result.registrations == []
    assert result.creative == finalize.Creative(
        creative_id=creative,
        ad_prefix=ads,
        hls_uri=f"{CONTENT_BASE}/{ads}main.m3u8",
        dash_uri=f"{CONTENT_BASE}/{ads}main_avc.mpd",
    )
    # The multivariant keeps relative children (MediaTailor requires them next to it) and nominal bandwidths.
    main = s.objects[f"{ads}main.m3u8"].decode()
    assert main == s.objects[f"{prefix}main.m3u8"].decode()
    assert all(not v.uri.startswith("http") for v in parse_multivariant(main))
    # Media playlists point at the original segments on the content host (no segment copies).
    media = s.objects[f"{ads}main_av1-360p.m3u8"].decode()
    assert f'#EXT-X-MAP:URI="{CONTENT_BASE}/{prefix}main_av1-360pinit.cmfv"' in media
    assert f"{CONTENT_BASE}/{prefix}main_av1-360p_000000001.cmfv" in media.splitlines()
    assert f"{ads}main_aac-he2-32.m3u8" in s.objects
    assert not any(k.startswith(ads) and k.endswith((".cmfv", ".cmfa")) for k in s.writes)
    mpd = s.objects[f"{ads}main_avc.mpd"].decode()
    assert f"<BaseURL>{CONTENT_BASE}/{prefix}</BaseURL>" in mpd
    assert {f"{ads}main_hevc.mpd", f"{ads}main_av1.mpd", f"{ads}main_sd.m3u8"} <= s.objects.keys()


def test_dash_profile_of_the_vast_follows_the_package_profiles(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "house-ad-1-1a2b3c")
    item = asset("ad", "house-ad-1-1a2b3c", prefix)

    result = finalize.finalize(store(objects), item, content_base_url=CONTENT_BASE, package_profiles=("dash-hevc",))

    assert result.creative is not None
    assert result.creative.dash_uri.endswith("/main_hevc.mpd")


def test_with_base_url_goes_before_the_first_period() -> None:
    mpd = '<?xml version="1.0"?>\n<MPD type="static">\n  <Period id="1">\n  </Period>\n</MPD>\n'

    assert with_base_url(mpd, "https://cdn/a&b/") == (
        '<?xml version="1.0"?>\n<MPD type="static">\n  <BaseURL>https://cdn/a&amp;b/</BaseURL>\n'
        '  <Period id="1">\n  </Period>\n</MPD>\n'
    )
    with pytest.raises(InvalidOutputsError):
        with_base_url("<MPD/>", "https://cdn/")


def test_duration_is_exact() -> None:
    playlist = "#EXTM3U\n" + "#EXTINF:6.006,\nseg.cmfv\n" * 1000 + "#EXTINF:2.002,\nlast.cmfv\n"

    assert duration_ms(playlist) == 6_008_002  # 1000 x 6006 + 2002, no float drift


def test_child_uris_include_renditions() -> None:
    multivariant = (
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=1,AUDIO="a"\nvideo.m3u8\n'
        '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="video_I-Frame.m3u8"\n'
    )

    assert child_uris(multivariant) == ["video.m3u8", "audio.m3u8", "video_I-Frame.m3u8"]


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

    assert result["duration_ms"] == 10010
    assert result["registrations"][0]["vod_source"].startswith("filler-1a2b3c-")


def test_missing_media_playlist_fails(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "filler-1a2b3c")
    del objects[f"{prefix}main_aac-he2-32.m3u8"]

    with pytest.raises(InvalidOutputsError, match=r"missing .*main_aac-he2-32\.m3u8"):
        run(store(objects), asset("slate", "filler-1a2b3c", prefix))


def test_av1_playlist_without_init_fails(store: Any, encode: Any) -> None:
    prefix, objects = encode("slate", "filler-1a2b3c")
    key = f"{prefix}main_av1-720p.m3u8"
    objects[key] = b"".join(line for line in objects[key].splitlines(keepends=True) if b"EXT-X-MAP" not in line)

    with pytest.raises(InvalidOutputsError, match="has no EXT-X-MAP"):
        run(store(objects), asset("slate", "filler-1a2b3c", prefix))


def test_iframe_playlists_are_raised_to_version_5(store: Any) -> None:
    ladder = STANDARD.model_copy(update={"options": STANDARD.options.model_copy(update={"iframe_playlists": True})})
    iframes = '#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-I-FRAMES-ONLY\n#EXT-X-MAP:URI="init.cmfv"\n#EXTINF:2.002,\nseg.cmfv\n'
    s = store()
    encode = finalize.Encode(ladder=ladder, prefix="assets/a/l/", multivariants={}, media={"v_I-Frame.m3u8": iframes})

    assert finalize.normalize(s, encode) == ["assets/a/l/v_I-Frame.m3u8"]
    assert b"#EXT-X-VERSION:5" in s.objects["assets/a/l/v_I-Frame.m3u8"]
