"""Encoding ladders: the single definition of every rendition a channel profile carries.

Channel Assembly matches variants and subtitle renditions by position and requires every VOD source of a channel
(programs, filler, ad slates, house ads) to share the exact rendition set (channel-assembly.md §0.3,
docs/captions.md), so a ladder is immutable and identified by a content fingerprint.
``ladder.version`` (``<id>@<fingerprint>``) is the key used everywhere.
"""

import hashlib
import json
import os
import re
import tomllib
from collections.abc import Iterable
from enum import StrEnum
from fractions import Fraction
from functools import cached_property
from math import ceil
from pathlib import Path
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fast_channels.core.captions import CaptionTrack, primary_language
from fast_channels.core.cost import encode_cost_per_minute
from fast_channels.core.timeline import FrameRate, Grid

_SLUG = r"^[a-z0-9][a-z0-9-]*$"
LADDERS_DIR_ENV: Final = "FAST_LADDERS_DIR"
BUILTIN_DIR: Final = Path(__file__).parent / "ladders"

PEAK_FACTOR: Final = Fraction(11, 10)
"""Nominal BANDWIDTH = MaxBitrate x 1.1 + audio: HRD-capped low rungs peaked at 1.11x the cap, higher rungs at
0.4-0.8x (ladders.md §3.4)."""
HLSJS_START_BPS: Final = 500_000  # hls.js skips a codec at start if its lowest BANDWIDTH exceeds this (§3.5)
HRD_MAX_HEIGHT: Final = 432  # HrdBufferSize = MaxBitrate on rungs <= 432p keeps peaks near the cap (§4.1)
MAX_FPS: Final = 60  # Apple HLS 1.19
MAX_AVC_AV1_PIXELS: Final = 4096 * 2160  # MediaConvert AVC/AV1 output limit (mediaconvert.md §2.1)
HEVC_L51_PIXELS: Final = 8_912_896  # HEVC level 5.1 MaxLumaPs / MaxLumaSr: Apple HLS 1.6b upper bound
HEVC_L51_RATE: Final = 534_773_760


class VideoCodec(StrEnum):
    AVC = "avc"
    HEVC = "hevc"
    AV1 = "av1"


class AudioProfile(StrEnum):
    LC = "lc"
    HE_V1 = "he1"
    HE_V2 = "he2"


# MediaConvert AAC 2.0 bitrates at 48 kHz (ladders.md §2.5); Apple 2.4: no HE-AAC above 64 kbit/s.
AAC_KBPS: Final[dict[AudioProfile, frozenset[int]]] = {
    AudioProfile.LC: frozenset({64, 80, 96, 112, 128, 160, 192, 224, 256, 288, 320, 384, 448, 512, 576}),
    AudioProfile.HE_V1: frozenset({16, 20, 24, 28, 32, 40, 48, 56, 64}),
    AudioProfile.HE_V2: frozenset({20, 24, 28, 32, 40, 48, 56, 64}),
}
_CODEC_PROFILES: Final[dict[VideoCodec, dict[int, frozenset[str]]]] = {
    VideoCodec.AVC: {8: frozenset({"HIGH", "MAIN"})},
    VideoCodec.HEVC: {8: frozenset({"MAIN_MAIN"}), 10: frozenset({"MAIN10_MAIN"})},
    VideoCodec.AV1: {8: frozenset(), 10: frozenset()},
}
_DEFAULT_PROFILE: Final = {
    (VideoCodec.AVC, 8): "HIGH",
    (VideoCodec.HEVC, 8): "MAIN_MAIN",
    (VideoCodec.HEVC, 10): "MAIN10_MAIN",
}


class VideoRendition(BaseModel):
    """One video rung. ``max_kbps`` is the QVBR cap; ``hrd_kbps`` the HRD buffer (AVC/HEVC only)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=_SLUG, max_length=32)
    codec: VideoCodec
    width: int = Field(ge=96, le=8192, multiple_of=2)
    height: int = Field(ge=96, le=4320, multiple_of=2)
    qvbr: int = Field(ge=1, le=10)
    max_kbps: int = Field(ge=1)
    hrd_kbps: int | None = Field(None, ge=1)
    audio_group: str
    bit_depth: Literal[8, 10] = 8
    profile: str | None = None

    @property
    def name_modifier(self) -> str:
        """MediaConvert output NameModifier (``_avc-720p-a``)."""
        return f"_{self.name}"

    @property
    def codec_profile(self) -> str | None:
        """MediaConvert CodecProfile (``HIGH``, ``MAIN_MAIN``, ``MAIN10_MAIN``); AV1 has none (always Main)."""
        return self.profile or _DEFAULT_PROFILE.get((self.codec, self.bit_depth))

    @model_validator(mode="after")
    def _codec_rules(self) -> Self:
        depths = _CODEC_PROFILES[self.codec]
        if self.bit_depth not in depths:
            raise ValueError(f"{self.name}: {self.codec} does not support {self.bit_depth}-bit")
        if self.profile is not None and self.profile not in depths[self.bit_depth]:
            raise ValueError(f"{self.name}: profile {self.profile} is invalid for {self.bit_depth}-bit {self.codec}")
        if self.codec is VideoCodec.AV1 and self.hrd_kbps is not None:
            raise ValueError(f"{self.name}: AV1 has no HRD buffer setting")
        return self


class AudioRendition(BaseModel):
    """One AAC stereo 48 kHz rendition group (``aac-lc-128``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: str = Field(pattern=_SLUG, max_length=32)
    profile: AudioProfile
    kbps: int

    @property
    def name_modifier(self) -> str:
        """MediaConvert output NameModifier (``_aac-lc-128``)."""
        return f"_{self.group_id}"

    @model_validator(mode="after")
    def _bitrate(self) -> Self:
        if self.kbps not in AAC_KBPS[self.profile]:
            raise ValueError(f"{self.group_id}: {self.kbps} kbit/s is not a valid {self.profile} stereo bitrate")
        return self


class LadderOptions(BaseModel):
    """Encode options that apply to every rendition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    iframe_playlists: bool = False  # off until CA/SSAI/SGAI pass-through is verified (_review.md, IFrameOnlyManifest)
    loudness_lkfs: float | None = Field(-24.0, ge=-59, le=0)  # MediaTailor normalises ads to -24 LKFS too
    acceleration: Literal["PREFERRED", "ENABLED", "DISABLED"] = "PREFERRED"
    film_grain: bool = False
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
    audio: tuple[AudioRendition, ...] = Field(min_length=1)
    captions: tuple[CaptionTrack, ...] = ()
    options: LadderOptions = LadderOptions()

    @model_validator(mode="after")
    def _policy(self) -> Self:
        if problems := self.problems():
            raise ValueError("; ".join(problems))
        return self

    @property
    def grid(self) -> Grid:
        """GOP/segment grid: GOP = round(gop_seconds x fps) frames for every codec."""
        return Grid(
            frame_rate=self.frame_rate,
            gop_frames=self.frame_rate.frames(self.gop_seconds),
            segment_gops=round(self.segment_seconds / self.gop_seconds),
        )

    @cached_property
    def fingerprint(self) -> str:
        """10-hex SHA-256 of the canonical JSON of everything except ``id`` and ``description``.

        A ladder without caption tracks leaves ``captions`` out, so it keeps the version it had before ladders
        could carry them.
        """
        exclude = {"id", "description"} | (set() if self.captions else {"captions"})
        canonical = json.dumps(self.model_dump(mode="json", exclude=exclude), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:10]

    @property
    def version(self) -> str:
        """``<id>@<fingerprint>``: the ladder identity used in keys, tags, items and events."""
        return f"{self.id}@{self.fingerprint}"

    @property
    def codecs(self) -> frozenset[VideoCodec]:
        return frozenset(r.codec for r in self.video)

    @property
    def max_height(self) -> int:
        return max(r.height for r in self.video)

    def audio_for(self, rendition: VideoRendition) -> AudioRendition:
        """The audio rendition group a video rendition plays with."""
        return next(a for a in self.audio if a.group_id == rendition.audio_group)

    def nominal_bandwidth(self, rendition: VideoRendition) -> tuple[int, int]:
        """Deterministic (BANDWIDTH, AVERAGE-BANDWIDTH) of a variant in bit/s, independent of the content."""
        video, audio = rendition.max_kbps * 1000, self.audio_for(rendition).kbps * 1000
        return ceil(video * PEAK_FACTOR) + audio, video + audio

    def pricing_tier(self) -> Literal["BASIC", "PROFESSIONAL"]:
        """MediaConvert pricing tier: Basic covers AVC single-pass only, without normalisation, acceleration or
        captions.

        The deinterlacer runs in NORMAL mode, which leaves progressive frames untouched.
        """
        basic = (
            self.codecs == {VideoCodec.AVC}
            and self.options.loudness_lkfs is None
            and self.options.acceleration == "DISABLED"
            and not self.captions
        )
        return "BASIC" if basic else "PROFESSIONAL"

    def problems(self) -> list[str]:
        """Every ladder-level rule this ladder breaks (empty when valid)."""
        return [*self._structure_problems(), *self._player_problems(), *self._codec_limit_problems()]

    def _structure_problems(self) -> Iterable[str]:
        names = [r.name for r in self.video] + [a.group_id for a in self.audio]
        if duplicates := sorted({n for n in names if names.count(n) > 1}):
            yield f"duplicate rendition names {duplicates}"
        languages = [primary_language(t.language) for t in self.captions]
        if duplicates := sorted({lang for lang in languages if languages.count(lang) > 1}):
            yield f"more than one caption track for {duplicates}"
        groups, used = {a.group_id for a in self.audio}, {r.audio_group for r in self.video}
        if missing := sorted(used - groups):
            yield f"unknown audio groups {missing}"
        if unused := sorted(groups - used):
            yield f"audio groups {unused} are not used by any video rendition"
        if not float(self.gop_seconds).is_integer() or not float(self.segment_seconds).is_integer():
            yield "gop_seconds and segment_seconds must be whole seconds (MediaConvert FragmentLength/SegmentLength)"
        elif self.segment_seconds % self.gop_seconds:
            yield "segment_seconds must be a multiple of gop_seconds"
        if self.frame_rate.fps > MAX_FPS:
            yield f"frame rate {self.frame_rate.fps:.3f} exceeds {MAX_FPS} fps"
        # NTSC rates stretch a 2 s GOP by 1001/1000 (60 frames = 2.002 s); anything further off is not whole frames.
        if abs(self.grid.gop_frames / self.frame_rate.fps - self.gop_seconds) > self.gop_seconds * 0.0011:
            yield "gop_seconds is not a whole number of frames at this frame rate"
        if self.options.film_grain and VideoCodec.AV1 not in self.codecs:
            yield "film_grain applies to AV1 renditions only"

    def _player_problems(self) -> Iterable[str]:
        first = self.video[0]
        avc_720 = [r for r in self.video if r.codec is VideoCodec.AVC and r.height == 720]
        if not avc_720 or first != min(avc_720, key=lambda r: r.max_kbps):
            yield "the first rendition must be the lowest AVC 720p rung (Apple HLS 1.32 default variant)"
        groups = {a.group_id for a in self.audio}
        playable = [r for r in self.video if r.audio_group in groups]  # unknown groups are reported above
        for codec in sorted({r.codec for r in playable}):
            lowest = min(self.nominal_bandwidth(r)[0] for r in playable if r.codec is codec)
            if lowest > HLSJS_START_BPS:
                yield f"lowest {codec} variant is {lowest} bit/s; hls.js needs one <= {HLSJS_START_BPS}"
        for r in self.video:
            if r.codec is not VideoCodec.AV1 and r.height <= HRD_MAX_HEIGHT and r.hrd_kbps != r.max_kbps:
                yield f"{r.name}: hrd_kbps must equal max_kbps at <= {HRD_MAX_HEIGHT}p"

    def _codec_limit_problems(self) -> Iterable[str]:
        for r in self.video:
            pixels = r.width * r.height
            if r.codec is VideoCodec.HEVC:
                if pixels > HEVC_L51_PIXELS or pixels * self.frame_rate.fps > HEVC_L51_RATE:
                    yield f"{r.name}: exceeds HEVC level 5.1 (Apple HLS 1.6b)"
            elif r.width > 4096 or pixels > MAX_AVC_AV1_PIXELS:
                yield f"{r.name}: MediaConvert {r.codec} output is limited to 4096x2160"

    @classmethod
    def load(cls, path: Path) -> Ladder:
        """Read and validate a ladder TOML file."""
        return cls.model_validate(tomllib.loads(path.read_text()))

    @classmethod
    def builtin(cls, name: str) -> Ladder:
        """A built-in ladder (``standard``, ``uhd``) from ``$FAST_LADDERS_DIR`` or the package data directory."""
        if not re.fullmatch(_SLUG, name):
            raise ValueError(f"invalid ladder name {name!r}")
        return cls.load(Path(os.environ.get(LADDERS_DIR_ENV, BUILTIN_DIR)) / f"{name}.toml")

    def to_toml(self) -> str:
        """TOML document that :meth:`load` reads back into an equal ladder."""
        data = self.model_dump(mode="json", exclude_none=True)
        lines = [f"{key} = {_toml_value(data[key])}" for key in ("id", "description", "gop_seconds", "segment_seconds")]
        lines.insert(2, f"frame_rate = {_toml_value(str(self.frame_rate))}")
        lines += ["", "[options]", *(f"{k} = {_toml_value(v)}" for k, v in data["options"].items())]
        for table in ("video", "audio", "captions"):
            for row in data[table]:
                lines += ["", f"[[{table}]]", *(f"{k} = {_toml_value(v)}" for k, v in row.items())]
        return "\n".join(lines) + "\n"

    def with_captions(self, tracks: Iterable[CaptionTrack]) -> Ladder:
        """This ladder with ``tracks`` as its caption tracks: a new version, unless they are the ones it has."""
        return Ladder.model_validate(self.model_dump() | {"captions": tuple(tracks)})


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

    codecs: list[VideoCodec] | None = None
    max_height: int | None = Field(None, gt=0)
    min_height: int | None = Field(None, gt=0)
    quality_shift: int = Field(0, ge=-2, le=2)
    drop_rungs: list[str] = []
    captions: list[CaptionTrack] | None = None  # replaces the caption tracks; [] removes them
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
        (ops.codecs is None or rendition.codec in ops.codecs)
        and (ops.max_height is None or rendition.height <= ops.max_height)
        and (ops.min_height is None or rendition.height >= ops.min_height)
        and rendition.name not in ops.drop_rungs
    )


def _rendition_names(ladder: Ladder) -> set[str]:
    return (
        {r.name for r in ladder.video}
        | {a.group_id for a in ladder.audio}
        | {c.name_modifier[1:] for c in ladder.captions}
    )


def propose(base: Ladder, ops: LadderOps) -> LadderProposal:
    """Apply ``ops`` to ``base`` without side effects: filter rungs, shift QVBR, validate, diff and cost.

    Audio groups no video rung uses any more are dropped. ``ladder`` is ``None`` when the result is invalid;
    ``errors`` then says why and the diff and cost describe the rejected candidate.
    """
    warnings = [f"unknown rung {name}" for name in ops.drop_rungs if name not in {r.name for r in base.video}]
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
    used = {r.audio_group for r in video}
    fields = {name: getattr(base, name) for name in Ladder.model_fields} | {
        "id": ops.new_id or base.id,
        "video": tuple(video),
        "audio": tuple(a for a in base.audio if a.group_id in used),
        "captions": base.captions if ops.captions is None else tuple(ops.captions),
    }
    candidate = Ladder.model_construct(**fields)  # unvalidated, so the rejected candidate can still be costed
    errors = candidate.problems() if video else ["no video renditions left"]
    ladder = None if errors else Ladder.model_validate(fields)
    if ladder is not None and ladder.version == base.version:
        warnings.append("no change: the proposal equals the base ladder")
    before, after = _rendition_names(base), _rendition_names(candidate)
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
