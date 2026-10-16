"""Minimal ISO-BMFF reader and the AV1 ``av1C`` fix-up for MediaConvert init segments.

MediaConvert writes the same ``av1C`` (``81 00 0c 00``: level 2.0, 8-bit) for every AV1 rendition while the
bitstream and HLS ``CODECS`` say e.g. 10-bit level 4.0 (ladders.md §3.3 item 1). The AV1-ISOBMFF binding (§2.3)
requires the configuration record to match the sequence header, so Finalize rewrites bytes 1-2 in place:

    byte 0: marker(1) version(7)                                  = 0x81
    byte 1: seq_profile(3) seq_level_idx_0(5)
    byte 2: seq_tier_0(1) high_bitdepth(1) twelve_bit(1) monochrome(1)
            chroma_subsampling_x(1) chroma_subsampling_y(1) chroma_sample_position(2)
    byte 3: reserved(3) initial_presentation_delay_present(1) initial_presentation_delay_minus_one(4)
"""

import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

# Bytes between a box header and its first child box (FullBox header, entry count, sample entry fields).
_CHILD_OFFSET: Final = {"moov": 0, "trak": 0, "mdia": 0, "minf": 0, "stbl": 0, "stsd": 8, "av01": 78}
AV1C_PATH: Final = ("moov", "trak", "mdia", "minf", "stbl", "stsd", "av01", "av1C")

# AV1 spec Annex A.3: (seq_level_idx, MaxPicSize, MaxHSize, MaxVSize, MaxDisplayRate, MaxDecodeRate)
_AV1_LEVELS: Final = (
    (0, 147_456, 2048, 1152, 4_423_680, 5_529_600),  # 2.0
    (1, 278_784, 2816, 1584, 8_363_520, 10_454_400),  # 2.1
    (4, 665_856, 4352, 2448, 19_975_680, 24_969_600),  # 3.0
    (5, 1_065_024, 5504, 3096, 31_950_720, 39_938_400),  # 3.1
    (8, 2_359_296, 6144, 3456, 70_778_880, 77_856_768),  # 4.0
    (9, 2_359_296, 6144, 3456, 141_557_760, 155_713_536),  # 4.1
    (12, 8_912_896, 8192, 4352, 267_386_880, 273_715_200),  # 5.0
    (13, 8_912_896, 8192, 4352, 534_773_760, 547_430_400),  # 5.1
    (14, 8_912_896, 8192, 4352, 1_069_547_520, 1_094_860_800),  # 5.2
    (15, 8_912_896, 8192, 4352, 1_069_547_520, 1_176_502_272),  # 5.3
    (16, 35_651_584, 16384, 8704, 1_069_547_520, 1_176_502_272),  # 6.0
    (17, 35_651_584, 16384, 8704, 2_139_095_040, 2_189_721_600),  # 6.1
    (18, 35_651_584, 16384, 8704, 4_278_190_080, 4_379_443_200),  # 6.2
    (19, 35_651_584, 16384, 8704, 4_278_190_080, 4_706_009_088),  # 6.3
)


@dataclass(frozen=True, slots=True)
class Box:
    """An ISO-BMFF box: four-character ``type`` at byte ``offset``, ``size`` bytes including its header."""

    type: str
    offset: int
    size: int
    header_size: int

    @property
    def payload_offset(self) -> int:
        return self.offset + self.header_size

    @property
    def end(self) -> int:
        return self.offset + self.size


def iter_boxes(data: bytes, offset: int = 0, end: int | None = None) -> Iterator[Box]:
    """Boxes laid out back to back in ``data[offset:end]`` (32-bit, 64-bit and to-end sizes)."""
    end = len(data) if end is None else end
    while offset + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            (size,) = struct.unpack_from(">Q", data, offset + 8)
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            raise ValueError(f"truncated {kind.decode('latin-1')!r} box at byte {offset}")
        yield Box(kind.decode("latin-1"), offset, size, header)
        offset += size


def find_boxes(data: bytes, path: Sequence[str]) -> Iterator[Box]:
    """Every box reached by following ``path`` from the top level (e.g. :data:`AV1C_PATH`)."""

    def walk(start: int, stop: int, depth: int) -> Iterator[Box]:
        for box in iter_boxes(data, start, stop):
            if box.type != path[depth]:
                continue
            if depth == len(path) - 1:
                yield box
            elif box.type in _CHILD_OFFSET:
                yield from walk(box.payload_offset + _CHILD_OFFSET[box.type], box.end, depth + 1)

    return walk(0, len(data), 0)


def patch_av1c(init_segment: bytes, *, level_idx: int, bit_depth: int, profile: int = 0) -> bytes:
    """Copy of ``init_segment`` whose ``av1C`` declares ``profile``, ``level_idx`` and ``bit_depth`` (idempotent).

    Tier, monochrome and chroma fields are kept from the original record. Raises ValueError when the segment
    has no valid ``av1C`` or the values are out of range.
    """
    if not 0 <= profile <= 2 or not 0 <= level_idx <= 31 or bit_depth not in (8, 10, 12):
        raise ValueError("profile must be 0-2, level_idx 0-31 and bit_depth 8, 10 or 12")
    if bit_depth == 12 and profile != 2:
        raise ValueError("12-bit AV1 requires the Professional profile (2)")
    patched = bytearray(init_segment)
    boxes = list(find_boxes(init_segment, AV1C_PATH))
    if not boxes:
        raise ValueError("no av1C box in this init segment")
    for box in boxes:
        at = box.payload_offset
        if box.size - box.header_size < 4 or patched[at] != 0x81:
            raise ValueError("av1C has no marker/version byte 0x81")
        patched[at + 1] = profile << 5 | level_idx
        high_bitdepth, twelve_bit = int(bit_depth > 8), int(bit_depth == 12)
        patched[at + 2] = patched[at + 2] & 0b1001_1111 | high_bitdepth << 6 | twelve_bit << 5
    return bytes(patched)


def av1_level_idx(width: int, height: int, fps: float) -> int:
    """Smallest AV1 ``seq_level_idx`` whose Annex A limits fit a ``width`` x ``height`` stream at ``fps``.

    Matches MediaConvert's CODECS strings: 360p30 -> 1 (2.1), 720p30 -> 5 (3.1), 1080p30 -> 8 (4.0),
    2160p30 -> 12 (5.0).
    """
    pixels = width * height
    rate = pixels * fps
    for idx, max_pic, max_h, max_v, max_display, max_decode in _AV1_LEVELS:
        if pixels <= max_pic and width <= max_h and height <= max_v and rate <= max_display and rate <= max_decode:
            return idx
    raise ValueError(f"{width}x{height}@{fps} exceeds AV1 level 6.3")
