"""Bring-your-own-CMS contract: the catalog the agent searches (mirrored by contracts/catalog.openapi.yaml)."""

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


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

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.available_from and self.available_until and self.available_until <= self.available_from:
            raise ValueError("available_until must be after available_from")
        return self


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
