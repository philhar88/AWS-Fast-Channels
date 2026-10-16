"""av1C fix-up on real MediaConvert AV1 init segments (all carry the constant 81 00 0c 00 record)."""

import struct
from pathlib import Path

import pytest

from fast_channels.core import manifests, mp4
from fast_channels.core.ladder import Ladder, VideoCodec

DATA = Path(__file__).parent / "data"


def av1c(init: bytes) -> bytes:
    (box,) = mp4.find_boxes(init, mp4.AV1C_PATH)
    return init[box.payload_offset : box.end]


def test_box_walker_on_a_real_init_segment() -> None:
    init = (DATA / "init/golden-av1-1080p-30init.cmfv").read_bytes()
    assert [b.type for b in mp4.iter_boxes(init)] == ["ftyp", "moov"]
    moov = list(mp4.iter_boxes(init))[1]
    assert [b.type for b in mp4.iter_boxes(init, moov.payload_offset, moov.end)] == ["mvhd", "iods", "trak", "mvex"]
    assert av1c(init) == bytes.fromhex("81000c00")  # MediaConvert's template record: level 0, 8-bit


@pytest.mark.parametrize(
    ("sample", "rung", "codecs"),
    [
        ("golden-av1-1080p-30init.cmfv", None, "av01.0.08M.10"),
        ("standard-av1-1080p-2997init.cmfv", "av1-1080p", "av01.0.08M.10"),
        ("standard-av1-360p-2997init.cmfv", "av1-360p", "av01.0.01M.10"),
    ],
)
def test_patch_matches_the_codecs_string(standard: Ladder, sample: str, rung: str | None, codecs: str) -> None:
    init = (DATA / "init" / sample).read_bytes()
    r = next(r for r in standard.video if r.name == rung) if rung else None
    width, height, fps = (r.width, r.height, standard.frame_rate.fps) if r else (1920, 1080, 30.0)
    level = mp4.av1_level_idx(width, height, fps)
    patched = mp4.patch_av1c(init, level_idx=level, bit_depth=10)
    record = av1c(patched)
    assert record[0] == 0x81
    assert (record[1] >> 5, record[1] & 0x1F) == (0, int(codecs.split(".")[2][:2]))  # profile, seq_level_idx
    assert (record[2] >> 7, record[2] >> 6 & 1, record[2] >> 5 & 1) == (0, 1, 0)  # Main tier, 10-bit
    assert record[2] & 0x1F == av1c(init)[2] & 0x1F == 0x0C  # 4:2:0 chroma flags kept
    (box,) = mp4.find_boxes(init, mp4.AV1C_PATH)
    changed = [i for i, (a, b) in enumerate(zip(init, patched, strict=True)) if a != b]
    assert changed == [box.payload_offset + 1, box.payload_offset + 2]  # only bytes 1-2 of the record
    assert mp4.patch_av1c(patched, level_idx=level, bit_depth=10) == patched  # idempotent


def test_levels_match_mediaconvert_codecs_for_every_av1_rung(standard: Ladder) -> None:
    text = (DATA / "standard-job/slate/main.m3u8").read_text()
    seen = {v.uri: v.codecs[0] for v in manifests.parse_multivariant(text)}
    for r in standard.video:
        if r.codec is VideoCodec.AV1:
            level = mp4.av1_level_idx(r.width, r.height, standard.frame_rate.fps)
            assert seen[f"main_{r.name}.m3u8"] == f"av01.0.{level:02d}M.10"
    assert mp4.av1_level_idx(3840, 2160, 30) == 12
    assert mp4.av1_level_idx(1920, 1080, 60) == 9
    with pytest.raises(ValueError, match=r"exceeds AV1 level 6\.3"):
        mp4.av1_level_idx(16384, 8704, 120)


def test_patch_rejects_bad_input() -> None:
    init = (DATA / "init/golden-av1-1080p-30init.cmfv").read_bytes()
    with pytest.raises(ValueError, match="no av1C"):
        mp4.patch_av1c((DATA / "init/standard-hevc-1080p-2997init.cmfv").read_bytes(), level_idx=8, bit_depth=8)
    with pytest.raises(ValueError, match="marker"):
        mp4.patch_av1c(init.replace(bytes.fromhex("81000c00"), bytes.fromhex("01000c00")), level_idx=8, bit_depth=8)
    with pytest.raises(ValueError, match="profile must be"):
        mp4.patch_av1c(init, level_idx=40, bit_depth=10)
    with pytest.raises(ValueError, match="Professional profile"):
        mp4.patch_av1c(init, level_idx=8, bit_depth=12)
    twelve = mp4.patch_av1c(init, level_idx=8, bit_depth=12, profile=2)
    assert av1c(twelve)[1:3] == bytes([2 << 5 | 8, 0b0110_1100])


def test_box_sizes() -> None:
    large = struct.pack(">I4sQ", 1, b"free", 20) + b"abcd"
    to_end = struct.pack(">I4s", 0, b"mdat") + b"xyz"
    boxes = list(mp4.iter_boxes(large + to_end))
    assert [(b.type, b.size, b.header_size) for b in boxes] == [("free", 20, 16), ("mdat", 11, 8)]
    with pytest.raises(ValueError, match="truncated"):
        list(mp4.iter_boxes(struct.pack(">I4s", 100, b"moov")))


def test_a_text_segment_round_trips() -> None:
    segment = (DATA / "captions/tos-cc-en_000000001.cmft").read_bytes()
    fragment = mp4.read_fragment(segment)
    assert (fragment.start, fragment.duration) == (0, 540540)  # 6.006 s at 90 kHz
    assert fragment.sample.startswith(b'<?xml version="1.0" encoding="UTF-8"?><tt ')
    assert mp4.write_fragment(segment, fragment, sequence=1) == segment
    assert mp4.track_timescale((DATA / "captions/tos-cc-eninit.cmft").read_bytes()) == 90000


def test_a_text_segment_is_restamped_with_the_templates_boxes() -> None:
    template = (DATA / "captions/tos-cc-en_000000001.cmft").read_bytes()
    fragment = mp4.Fragment(start=22_882_860, duration=180_180, sample=b"<tt/>")

    segment = mp4.write_fragment(template, fragment, sequence=44)

    assert mp4.read_fragment(segment) == fragment
    boxes = [(b.type, b.size) for b in mp4.iter_boxes(segment)]
    assert boxes == [("styp", 24), ("sidx", 52), ("moof", 104), ("mdat", 13)]
    assert segment[:24] == template[:24]
    sidx = next(b for b in mp4.iter_boxes(segment) if b.type == "sidx")
    earliest, first_offset, _, count, size, duration = struct.unpack_from(">QQHHII", segment, sidx.payload_offset + 12)
    assert (earliest, first_offset, count, size, duration) == (22_882_860, 0, 1, 104 + 13, 180_180)
    (sequence,) = struct.unpack_from(">I", segment, segment.index(b"mfhd") + 8)
    assert sequence == 44


def test_other_layouts_are_not_text_fragments() -> None:
    init = (DATA / "captions/tos-cc-eninit.cmft").read_bytes()
    with pytest.raises(ValueError, match="one moof"):
        mp4.read_fragment(init)
    segment = (DATA / "captions/tos-cc-en_000000001.cmft").read_bytes()
    with pytest.raises(ValueError, match="one moof"):
        mp4.read_fragment(segment + segment[24:])
