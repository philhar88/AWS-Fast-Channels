"""Encoding ladders: the single definition of every variant a channel carries.

Plain HLS: H.264/AVC video with AAC-LC stereo muxed into the same MPEG-TS segments, up to 1080p. Channel Assembly
matches variants by position and requires every VOD source of a channel (programs, filler, ad slates, house ads) to
share the exact rendition set (channel-assembly.md §0.3), so a ladder is immutable and identified by a content
fingerprint. ``ladder.version`` (``<id>@<fingerprint>``) is the key used everywhere.
"""

import hashlib
import json
import os
import re
import tomllib
from collections.abc import Iterable
from fractions import Fraction
from functools import cached_property
from math import ceil
from pathlib import Path
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fast_channels.core.cost import encode_cost_per_minute
from fast_channels.core.timeline import FrameRate, Grid

_SLUG = r"^[a-z0-9][a-z0-9-]*$"
LADDERS_DIR_ENV: Final = "FAST_LADDERS_DIR"
BUILTIN_DIR: Final = Path(__file__).parent / "ladders"

PEAK_FACTOR: Final = Fraction(11, 10)
"""Nominal BANDWIDTH = MaxBitrate x 1.1 + audio: HRD-capped low rungs peaked at 1.11x the cap, higher rungs at
0.4-0.8x (ladders.md §3.4)."""
HRD_MAX_HEIGHT: Final = 432  # HrdBufferSize = MaxBitrate on rungs <= 432p keeps peaks near the cap (§4.1)
MAX_FPS: Final = 60  # Apple HLS 1.19
MAX_WIDTH: Final = 1920
MAX_HEIGHT: Final = 1080
AAC_LC_KBPS: Final = frozenset({64, 80, 96, 112, 128, 160, 192, 224, 256, 288, 320})
"""MediaConvert AAC-LC 2.0 bitrates at 48 kHz (ladders.md §2.5)."""


class VideoRendition(BaseModel):
    """One AVC rung. ``max_kbps`` is the QVBR cap; ``hrd_kbps`` the HRD buffer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=_SLUG, max_length=32)
    width: int = Field(ge=96, le=MAX_WIDTH, multiple_of=2)
    height: int = Field(ge=96, le=MAX_HEIGHT, multiple_of=2)
    qvbr: int = Field(ge=1, le=10)
    max_kbps: int = Field(ge=1)
    hrd_kbps: int | None = Field(None, ge=1)
    profile: Literal["HIGH", "MAIN"] = "HIGH"

    @property
    def name_modifier(self) -> str:
        """MediaConvert output NameModifier (``_avc-720p``)."""
        return f"_{self.name}"


class Audio(BaseModel):
    """The AAC-LC stereo 48 kHz CBR track muxed into every variant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kbps: int = 128

    @model_validator(mode="after")
    def _bitrate(self) -> Self:
        if self.kbps not in AAC_LC_KBPS:
            raise ValueError(f"{self.kbps} kbit/s is not an AAC-LC stereo bitrate")
        return self


class LadderOptions(BaseModel):
    """Encode options that apply to every rendition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allow_frame_rate_conversion: bool = False


class Ladder(BaseModel):
    """A complete, validated encoding ladder (renditions in multivariant order)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$", max_length=24)
    description: str = ""
    frame_rate: FrameRate
    gop_seconds: float = Field(2, gt=0)
    segment_seconds: float = Field(6, gt=0)
    video: tuple[VideoRendition, ...] = Field(min_length=1)
    audio: Audio = Audio()
    options: LadderOptions = LadderOptions()

    @model_validator(mode="after")
    def _policy(self) -> Self:
        if problems := self.problems():
            raise ValueError("; ".join(problems))
        return self

    @property
    def grid(self) -> Grid:
        """GOP/segment grid: GOP = round(gop_seconds x fps) frames."""
        return Grid(
            frame_rate=self.frame_rate,
            gop_frames=self.frame_rate.frames(self.gop_seconds),
            segment_gops=round(self.segment_seconds / self.gop_seconds),
        )

    @cached_property
    def fingerprint(self) -> str:
        """10-hex SHA-256 of the canonical JSON of everything except ``id`` and ``description``."""
        data = self.model_dump(mode="json", exclude={"id", "description"})
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:10]

    @property
    def version(self) -> str:
        """``<id>@<fingerprint>``: the ladder identity used in keys, tags, items and events."""
        return f"{self.id}@{self.fingerprint}"

    @property
    def max_height(self) -> int:
        return max(r.height for r in self.video)

    def nominal_bandwidth(self, rendition: VideoRendition) -> tuple[int, int]:
        """Deterministic (BANDWIDTH, AVERAGE-BANDWIDTH) of a variant in bit/s, independent of the content."""
        video, audio = rendition.max_kbps * 1000, self.audio.kbps * 1000
        return ceil(video * PEAK_FACTOR) + audio, video + audio

    def problems(self) -> list[str]:
        """Every ladder-level rule this ladder breaks (empty when valid)."""
        problems = []
        names = [r.name for r in self.video]
        if duplicates := sorted({n for n in names if names.count(n) > 1}):
            problems.append(f"duplicate rendition names {duplicates}")
        if not float(self.gop_seconds).is_integer() or not float(self.segment_seconds).is_integer():
            problems.append("gop_seconds and segment_seconds must be whole seconds (MediaConvert SegmentLength)")
        elif self.segment_seconds % self.gop_seconds:
            problems.append("segment_seconds must be a multiple of gop_seconds")
        if self.frame_rate.fps > MAX_FPS:
            problems.append(f"frame rate {self.frame_rate.fps:.3f} exceeds {MAX_FPS} fps")
        # NTSC rates stretch a 2 s GOP by 1001/1000 (60 frames = 2.002 s); anything further off is not whole frames.
        if abs(self.grid.gop_frames / self.frame_rate.fps - self.gop_seconds) > self.gop_seconds * 0.0011:
            problems.append("gop_seconds is not a whole number of frames at this frame rate")
        problems += [
            f"{r.name}: hrd_kbps must equal max_kbps at <= {HRD_MAX_HEIGHT}p"
            for r in self.video
            if r.height <= HRD_MAX_HEIGHT and r.hrd_kbps != r.max_kbps
        ]
        return problems

    @classmethod
    def load(cls, path: Path) -> Ladder:
        """Read and validate a ladder TOML file."""
        return cls.model_validate(tomllib.loads(path.read_text()))

    @classmethod
    def builtin(cls, name: str) -> Ladder:
        """A built-in ladder (``standard``) from ``$FAST_LADDERS_DIR`` or the package data directory."""
        if not re.fullmatch(_SLUG, name):
            raise ValueError(f"invalid ladder name {name!r}")
        return cls.load(Path(os.environ.get(LADDERS_DIR_ENV, BUILTIN_DIR)) / f"{name}.toml")

    def to_toml(self) -> str:
        """TOML document that :meth:`load` reads back into an equal ladder."""
        data = self.model_dump(mode="json", exclude_none=True)
        lines = [f"{key} = {_toml_value(data[key])}" for key in ("id", "description", "gop_seconds", "segment_seconds")]
        lines.insert(2, f"frame_rate = {_toml_value(str(self.frame_rate))}")
        for table in ("options", "audio"):
            lines += ["", f"[{table}]", *(f"{k} = {_toml_value(v)}" for k, v in data[table].items())]
        for row in data["video"]:
            lines += ["", "[[video]]", *(f"{k} = {_toml_value(v)}" for k, v in row.items())]
        return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    match value:
        case bool():
            return "true" if value else "false"
        case str():
            return json.dumps(value)  # JSON string escapes are valid TOML basic strings
        case _:
            return repr(value)


class LadderOps(BaseModel):
    """Operations the agent may apply to a base ladder; all optional."""

    model_config = ConfigDict(extra="forbid")

    max_height: int | None = Field(None, gt=0)
    min_height: int | None = Field(None, gt=0)
    quality_shift: int = Field(0, ge=-2, le=2)
    drop_rungs: list[str] = []
    new_id: str | None = Field(None, pattern=r"^[a-z][a-z0-9-]*$", max_length=24)


class LadderProposal(BaseModel):
    """Result of :func:`propose`: the new ladder (``None`` when invalid), a diff, validation and cost."""

    base_version: str
    ladder: Ladder | None
    added: list[str]
    removed: list[str]
    changed: list[str]
    errors: list[str]
    warnings: list[str]
    cost_per_minute_usd: float


def _keep(rendition: VideoRendition, ops: LadderOps) -> bool:
    return (
        (ops.max_height is None or rendition.height <= ops.max_height)
        and (ops.min_height is None or rendition.height >= ops.min_height)
        and rendition.name not in ops.drop_rungs
    )


def _names(video: Iterable[VideoRendition]) -> set[str]:
    return {r.name for r in video}


def propose(base: Ladder, ops: LadderOps) -> LadderProposal:
    """Apply ``ops`` to ``base`` without side effects: filter rungs, shift QVBR, validate, diff and cost.

    ``ladder`` is ``None`` when the result is invalid; ``errors`` then says why and the diff and cost describe the
    rejected candidate.
    """
    warnings = [f"unknown rung {name}" for name in ops.drop_rungs if name not in _names(base.video)]
    video: list[VideoRendition] = []
    shifted: list[str] = []
    for r in base.video:
        if not _keep(r, ops):
            continue
        qvbr = min(10, max(1, r.qvbr + ops.quality_shift))
        if qvbr != r.qvbr + ops.quality_shift:
            warnings.append(f"{r.name}: QVBR clamped to {qvbr}")
        if qvbr != r.qvbr:
            shifted.append(r.name)
        video.append(r.model_copy(update={"qvbr": qvbr}))
    fields = {name: getattr(base, name) for name in Ladder.model_fields} | {
        "id": ops.new_id or base.id,
        "video": tuple(video),
    }
    candidate = Ladder.model_construct(**fields)  # unvalidated, so the rejected candidate can still be costed
    errors = candidate.problems() if video else ["no video renditions left"]
    ladder = None if errors else Ladder.model_validate(fields)
    if ladder is not None and ladder.version == base.version:
        warnings.append("no change: the proposal equals the base ladder")
    before, after = _names(base.video), _names(video)
    return LadderProposal(
        base_version=base.version,
        ladder=ladder,
        added=sorted(after - before),
        removed=sorted(before - after),
        changed=shifted,
        errors=errors,
        warnings=warnings,
        cost_per_minute_usd=encode_cost_per_minute(candidate) if video else 0.0,
    )
