"""Rendition profiles: named subsets of a ladder, each written as its own manifest by the same encode.

Every encode writes the full catalogue as MediaConvert ``AdditionalManifests`` (free: segments are shared,
ladders.md §3.7), so enabling a profile later never needs a re-encode. MediaConvert writes an HLS *and* a DASH
manifest per additional manifest, so ``hls-avc`` and ``dash-avc`` share one ``main_avc`` stem.
The combined ``main.mpd`` is never used: it mixes every codec in one AdaptationSet (ladders.md §3.3).
"""

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from fast_channels.core.ladder import Ladder, VideoCodec, VideoRendition

MANIFEST_BASE: Final = "main"
"""File name of the output group's own manifests; MediaConvert appends NameModifiers to it."""


class ManifestFormat(StrEnum):
    HLS = "HLS"
    DASH = "DASH"


class RenditionProfile(BaseModel):
    """A named rendition subset: codecs (``None`` = all) within a height range, in one manifest format."""

    model_config = ConfigDict(frozen=True)

    name: str
    format: ManifestFormat
    codecs: frozenset[VideoCodec] | None = None
    min_height: int = 0
    max_height: int = 100_000

    @property
    def manifest_suffix(self) -> str:
        """ManifestNameModifier: ``""`` for ``hls`` (the group's own ``main``), else ``_`` + name sans format."""
        _, _, selection = self.name.partition("-")
        return f"_{selection}" if selection else ""

    @property
    def extension(self) -> str:
        return ".m3u8" if self.format is ManifestFormat.HLS else ".mpd"

    def selects(self, r: VideoRendition) -> bool:
        """Whether rendition ``r`` belongs to this profile."""
        return (self.codecs is None or r.codec in self.codecs) and self.min_height <= r.height <= self.max_height

    def manifest_key(self, base: str) -> str:
        """Key of this profile's manifest under an asset prefix: ``<base>/main_avc.m3u8``."""
        return f"{base.rstrip('/')}/{MANIFEST_BASE}{self.manifest_suffix}{self.extension}"


def _single(codec: VideoCodec, fmt: ManifestFormat) -> RenditionProfile:
    return RenditionProfile(name=f"{fmt.lower()}-{codec}", format=fmt, codecs=frozenset({codec}))


CATALOGUE: Final[tuple[RenditionProfile, ...]] = (
    RenditionProfile(name="hls", format=ManifestFormat.HLS),
    *(_single(codec, ManifestFormat.HLS) for codec in VideoCodec),
    RenditionProfile(name="hls-sd", format=ManifestFormat.HLS, max_height=576),
    RenditionProfile(name="hls-hd", format=ManifestFormat.HLS, min_height=720, max_height=1080),
    RenditionProfile(name="hls-uhd", format=ManifestFormat.HLS, min_height=1440),
    *(_single(codec, ManifestFormat.DASH) for codec in VideoCodec),
)
"""hls (all codecs), hls-avc, hls-hevc, hls-av1, hls-sd, hls-hd, hls-uhd, dash-avc, dash-hevc, dash-av1."""

DEFAULT_PACKAGE_PROFILES: Final[tuple[str, ...]] = ("hls", "hls-avc", "hls-hevc", "hls-av1", "dash-avc")
"""Channel Assembly package configurations registered on every VOD source (quota: 5 per source)."""

_BY_NAME: Final = {p.name: p for p in CATALOGUE}


def profile(name: str) -> RenditionProfile:
    """Catalogue profile by name."""
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(f"unknown rendition profile {name!r}; known: {sorted(_BY_NAME)}") from None


def applicable(ladder: Ladder) -> list[RenditionProfile]:
    """Catalogue profiles that select at least one of the ladder's renditions (e.g. no ``hls-uhd`` below 1440p)."""
    return [p for p in CATALOGUE if any(p.selects(r) for r in ladder.video)]


def selected_outputs(ladder: Ladder, p: RenditionProfile) -> list[str]:
    """NameModifiers of the profile's video outputs plus the audio outputs those use, in ladder order."""
    video = [r for r in ladder.video if p.selects(r)]
    groups = {r.audio_group for r in video}
    return [r.name_modifier for r in video] + [a.name_modifier for a in ladder.audio if a.group_id in groups]
