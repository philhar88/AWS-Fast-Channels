"""Rendition profiles: the manifests an encode offers Channel Assembly, one package configuration each.

An encode writes one HLS multivariant playlist, ``main.m3u8``, with every variant of the ladder; that is the only
profile, ``hls``. The catalogue stays a list so a deployment names the profiles it registers (``package_profiles``)
and a channel the outputs it serves.
"""

from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

MANIFEST_BASE: Final = "main"
"""File name of the output group's own manifest; MediaConvert appends NameModifiers to it for media playlists."""


class RenditionProfile(BaseModel):
    """A named manifest of an encode."""

    model_config = ConfigDict(frozen=True)

    name: str

    def manifest_key(self, base: str) -> str:
        """Key of this profile's manifest under an asset prefix: ``<base>/main.m3u8``."""
        return f"{base.rstrip('/')}/{MANIFEST_BASE}.m3u8"


CATALOGUE: Final[tuple[RenditionProfile, ...]] = (RenditionProfile(name="hls"),)

DEFAULT_PACKAGE_PROFILES: Final[tuple[str, ...]] = ("hls",)
"""Channel Assembly package configurations registered on every VOD source."""

_BY_NAME: Final = {p.name: p for p in CATALOGUE}


def profile(name: str) -> RenditionProfile:
    """Catalogue profile by name."""
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(f"unknown rendition profile {name!r}; known: {sorted(_BY_NAME)}") from None


def applicable(ladder: Ladder) -> list[RenditionProfile]:
    """Catalogue profiles an encode of ``ladder`` writes."""
    return list(CATALOGUE)
