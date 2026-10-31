"""Bring-your-own-CMS contract: the catalog the agent searches (mirrored by contracts/catalog.openapi.yaml)."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
DOMAIN_PATTERN: Final = r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
"""A DNS name with at least two labels, such as ``blender.org`` (at most :data:`DOMAIN_MAX_LENGTH` characters)."""
DOMAIN_MAX_LENGTH: Final = 253
LANGUAGE_PATTERN: Final = r"^[a-z]{2}$"
"""An ISO 639-1 language code, such as ``en``."""

_DOMAIN: Final = re.compile(DOMAIN_PATTERN)


class ContentType(StrEnum):
    """How a title is packaged."""

    MOVIE = "movie"
    EPISODE = "episode"
    SPECIAL = "special"
    TRAILER = "trailer"
    CLIP = "clip"


class ContentItem(BaseModel):
    """A catalog title with its editorial metadata, rights window and encoded assets."""

    model_config = ConfigDict(extra="ignore")

    content_id: str = Field(pattern=CONTENT_ID_PATTERN, max_length=64, description="Stable catalog id.")
    title: str = Field(min_length=1, description="Display title.")
    synopsis: str = Field("", description="Short description for EPGs and the agent.")
    rating: str | None = Field(None, description="Parental rating, e.g. TV-G.")
    genres: list[str] = Field([], description="Lower-case genre tags, e.g. animation.")
    duration_ms: int | None = Field(None, gt=0, description="Running time in milliseconds.")
    cue_points_ms: list[int] = Field([], description="Editorial ad cue points in milliseconds.")
    mezzanine_uri: str | None = Field(None, description="Source file to ingest (s3:// or https://).")
    available_from: datetime | None = Field(None, description="Start of the rights window (inclusive).")
    available_until: datetime | None = Field(None, description="End of the rights window (exclusive).")
    asset_ids: list[str] = Field([], description="Encoded assets of this title.")
    # Optional show-level metadata, which the crawl module puts into ad requests (docs/content-enrichment.md).
    series_title: str | None = Field(
        None, min_length=1, description="Show of an episode, or the franchise of a film that has one."
    )
    season: str | None = Field(None, min_length=1, description="Season label, e.g. Season 3.")
    episode: int | None = Field(None, ge=0, description="Episode number within the season.")
    content_type: ContentType | None = Field(None, description="How the title is packaged.")
    language: str | None = Field(None, pattern=LANGUAGE_PATTERN, description="Spoken language, ISO 639-1, e.g. en.")
    producer_name: str | None = Field(None, min_length=1, description="Producer or studio.")
    producer_domain: str | None = Field(
        None,
        pattern=DOMAIN_PATTERN,
        max_length=DOMAIN_MAX_LENGTH,
        description="The producer's web domain, e.g. blender.org.",
    )
    keywords: list[str] | None = Field(None, description="Descriptive tags that are not genres, e.g. live-action.")
    prodq: int | None = Field(
        None,
        ge=0,
        le=3,
        description="Production quality (AdCOM): 1 professionally produced, 2 prosumer, 3 user generated, 0 unknown.",
    )
    external_ids: dict[str, str] | None = Field(
        None, description="Ids of this title in other registries, keyed by each registry's domain."
    )

    @field_validator("external_ids")
    @classmethod
    def _registry_domains(cls, ids: dict[str, str] | None) -> dict[str, str] | None:
        if bad := [domain for domain, id_ in (ids or {}).items() if not is_domain(domain) or not id_]:
            raise ValueError(f"external_ids need a registry domain and a non-empty id: {bad}")
        return ids

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.available_from and self.available_until and self.available_until <= self.available_from:
            raise ValueError("available_until must be after available_from")
        return self


def is_domain(name: str) -> bool:
    """Whether ``name`` is a DNS name of at least two labels."""
    return len(name) <= DOMAIN_MAX_LENGTH and _DOMAIN.fullmatch(name) is not None


class ContentQuery(BaseModel):
    """Catalog search: free text plus genre/rating filters; all criteria are combined with AND."""

    model_config = ConfigDict(extra="ignore")

    text: str | None = Field(None, description="Words to match in title or synopsis.")
    genres: list[str] = Field([], description="Match any of these genres.")
    ratings: list[str] = Field([], description="Match any of these ratings.")
    max_duration_minutes: float | None = Field(None, gt=0, description="Longest running time to return.")
    limit: int = Field(20, ge=1, le=100, description="Page size.")
    next_token: str | None = Field(None, description="Token from the previous page.")


class ContentPage(BaseModel):
    """One page of search results."""

    items: list[ContentItem] = Field(description="Matching titles.")
    next_token: str | None = Field(None, description="Pass back to get the next page; absent on the last page.")
