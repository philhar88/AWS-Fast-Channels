"""Minimal ISO-BMFF reader, the AV1 ``av1C`` fix-up for MediaConvert init segments and the movie fragment of a
CMAF text segment (read, and re-stamped when Finalize re-cuts a caption track).

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
_CHILD_OFFSET: Final = {
    "moov": 0,
    "trak": 0,
    "mdia": 0,
    "minf": 0,
    "stbl": 0,
    "stsd": 8,
    "av01": 78,
    "moof": 0,
    "traf": 0,
}
AV1C_PATH: Final = ("moov", "trak", "mdia", "minf", "stbl", "stsd", "av01", "av1C")
MDHD_PATH: Final = ("moov", "trak", "mdia", "mdhd")

# trun flags (ISO/IEC 14496-12 §8.8.8).
_DATA_OFFSET: Final = 0x000001
_FIRST_SAMPLE_FLAGS: Final = 0x000004
_SAMPLE_DURATION: Final = 0x000100
_SAMPLE_SIZE: Final = 0x000200

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


def with_compatible_brand(segment: bytes, brand: str) -> bytes:
    """Copy of a media segment whose ``styp`` lists ``brand`` (four characters) among its compatible brands.

    Idempotent. Nothing else moves relative to its box: ``sidx`` references and ``trun`` data offsets are relative to
    boxes after ``styp``. CMAF Ingest marks the last segment of a session with ``lmsg``. Raises ValueError without a
    leading ``styp``.
    """
    code = brand.encode("latin-1")
    if len(code) != 4:
        raise ValueError(f"a brand has four characters, not {brand!r}")
    first = next(iter_boxes(segment), None)
    if first is None or first.type != "styp" or first.header_size != 8:
        raise ValueError("the segment does not start with a styp box")
    compatible = segment[first.payload_offset + 8 : first.end]
    if any(compatible[i : i + 4] == code for i in range(0, len(compatible), 4)):
        return segment
    header = struct.pack(">I4s", first.size + 4, b"styp")
    return header + segment[first.payload_offset : first.end] + code + segment[first.end :]


def sample_entries(init_segment: bytes) -> list[str]:
    """The sample entry type of every track of an init segment (``avc1``, ``mp4a``, ``stpp``...): a CMAF track file
    has exactly one."""
    entries = []
    for stsd in find_boxes(init_segment, ("moov", "trak", "mdia", "minf", "stbl", "stsd")):
        entry = next(iter_boxes(init_segment, stsd.payload_offset + 8, stsd.end), None)
        entries.append(entry.type if entry else "")
    return entries


def track_timescale(init_segment: bytes) -> int:
    """Media timescale (``mdhd``) of a single-track init segment."""
    boxes = list(find_boxes(init_segment, MDHD_PATH))
    if len(boxes) != 1:
        raise ValueError(f"expected one track, found {len(boxes)}")
    at = boxes[0].payload_offset
    version = init_segment[at]
    (timescale,) = struct.unpack_from(">I", init_segment, at + 4 + (16 if version == 1 else 8))
    return int(timescale)


@dataclass(frozen=True, slots=True)
class Fragment:
    """The one sample of a media segment's movie fragment, as MediaConvert writes CMAF text tracks: its decode time
    and duration in the track timescale, and its data (one IMSC1 document)."""

    start: int
    duration: int
    sample: bytes


@dataclass(frozen=True, slots=True)
class _Layout:
    """Where the fields :func:`write_fragment` changes sit in a segment."""

    sequence: int
    """Byte offset of mfhd sequence_number."""
    decode_time: int
    """Byte offset of tfdt baseMediaDecodeTime (64-bit when ``wide``)."""
    wide: bool
    sample_duration: int
    """Byte offset of the trun sample_duration (sample_size follows)."""
    sidx: Box | None
    mdat: Box


def _layout(segment: bytes) -> _Layout:
    boxes = list(iter_boxes(segment))
    kinds = [b.type for b in boxes]
    if kinds.count("moof") != 1 or kinds.count("mdat") != 1 or kinds.count("sidx") > 1 or kinds[-1] != "mdat":
        raise ValueError(f"expected one moof, at most one sidx and a final mdat, not {kinds}")
    moof, mdat = boxes[kinds.index("moof")], boxes[-1]
    sidx = boxes[kinds.index("sidx")] if "sidx" in kinds else None
    mfhd = [b for b in iter_boxes(segment, moof.payload_offset, moof.end) if b.type == "mfhd"]
    trafs = list(find_boxes(segment, ("moof", "traf")))
    if len(mfhd) != 1 or len(trafs) != 1:
        raise ValueError("expected a movie fragment of one track")
    children = {b.type: b for b in iter_boxes(segment, trafs[0].payload_offset, trafs[0].end)}
    if "tfdt" not in children or "trun" not in children:
        raise ValueError("the track fragment has no tfdt or trun")
    tfdt, trun = children["tfdt"], children["trun"]
    flags, count, data_offset = struct.unpack_from(">IIi", segment, trun.payload_offset)
    required = _DATA_OFFSET | _SAMPLE_DURATION | _SAMPLE_SIZE
    if count != 1 or flags & required != required:
        raise ValueError("expected a trun of one sample with its data offset, duration and size")
    if moof.offset + data_offset != mdat.payload_offset:
        raise ValueError("the sample data is not the mdat payload")
    first_sample_flags = 4 if flags & _FIRST_SAMPLE_FLAGS else 0
    return _Layout(
        sequence=mfhd[0].payload_offset + 4,
        decode_time=tfdt.payload_offset + 4,
        wide=segment[tfdt.payload_offset] == 1,
        sample_duration=trun.payload_offset + 12 + first_sample_flags,
        sidx=sidx,
        mdat=mdat,
    )


def read_fragment(segment: bytes) -> Fragment:
    """The sample of a media segment with one movie fragment of one sample (``styp``, optional ``sidx``, ``moof``,
    ``mdat``). Raises ValueError for any other layout."""
    layout = _layout(segment)
    (start,) = struct.unpack_from(">Q" if layout.wide else ">I", segment, layout.decode_time)
    duration, size = struct.unpack_from(">II", segment, layout.sample_duration)
    sample = segment[layout.mdat.payload_offset : layout.mdat.end]
    if size != len(sample):
        raise ValueError("the sample is not the whole mdat")
    return Fragment(start=start, duration=duration, sample=sample)


def write_fragment(template: bytes, fragment: Fragment, *, sequence: int) -> bytes:
    """``template`` (a segment :func:`read_fragment` accepts) carrying ``fragment`` as movie fragment ``sequence``.

    Every box and flag of the template is kept; only the sequence number, decode time, sample duration and size, the
    ``sidx`` reference (earliest presentation time, size, duration) and the ``mdat`` change. The template's ``sidx``
    must use the track timescale, as MediaConvert's does.
    """
    layout = _layout(template)
    if not layout.wide and fragment.start >= 1 << 32:
        raise ValueError("the decode time does not fit the template's 32-bit tfdt")
    out = bytearray(template[: layout.mdat.offset])
    struct.pack_into(">I", out, layout.sequence, sequence)
    struct.pack_into(">Q" if layout.wide else ">I", out, layout.decode_time, fragment.start)
    struct.pack_into(">II", out, layout.sample_duration, fragment.duration, len(fragment.sample))
    mdat = struct.pack(">I4s", 8 + len(fragment.sample), b"mdat") + fragment.sample
    if layout.sidx is not None:
        referenced = layout.mdat.offset - layout.sidx.end + len(mdat)
        _restamp_sidx(template, out, layout, layout.sidx, fragment, referenced=referenced)
    return bytes(out + mdat)


def _restamp_sidx(
    template: bytes, out: bytearray, layout: _Layout, sidx: Box, fragment: Fragment, *, referenced: int
) -> None:
    at = sidx.payload_offset
    times = ">QQ" if template[at] == 1 else ">II"
    earliest, first_offset = struct.unpack_from(times, template, at + 12)
    reference = at + 12 + struct.calcsize(times) + 4
    (count,) = struct.unpack_from(">H", template, reference - 2)
    kind_size, duration = struct.unpack_from(">II", template, reference)
    (start,) = struct.unpack_from(">Q" if layout.wide else ">I", template, layout.decode_time)
    (sample_duration,) = struct.unpack_from(">I", template, layout.sample_duration)
    if count != 1 or first_offset != 0 or (earliest, duration) != (start, sample_duration):
        raise ValueError("expected a sidx of one reference in the track timescale, right before the fragment")
    struct.pack_into(times, out, at + 12, fragment.start, 0)
    struct.pack_into(">II", out, reference, kind_size & 0x8000_0000 | referenced, fragment.duration)
