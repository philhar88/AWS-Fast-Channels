"""Ingest requests: the input of the IngestPipeline state machine (contracts/ingest-request.schema.json)."""

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fast_channels.core.mediaconvert import GeneratorInput  # noqa: TC001 - pydantic resolves field types at runtime
from fast_channels.core.naming import digest, slug

ASSET_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,47}$"
LADDER_VERSION_PATTERN = r"^[a-z][a-z0-9-]*@[0-9a-f]{10}$"


class AssetKind(StrEnum):
    CONTENT = "content"
    SLATE = "slate"
    AD = "ad"


def asset_id_for(source_uri: str) -> str:
    """Deterministic asset id: slug of the file name plus 6 hex of the full URI (``big-buck-bunny-1a2b3c``)."""
    stem = PurePosixPath(urlsplit(source_uri).path).stem
    return f"{slug(stem, max_len=41) or 'asset'}-{digest(source_uri)}"


class IngestRequest(BaseModel):
    """Encode one source (a file, or a still image for slates and house ads) with one ladder.

    Exactly one of ``source_uri`` and ``generator`` is set. ``correlation_id`` becomes the execution name.
    """

    model_config = ConfigDict(extra="forbid")

    source_uri: str | None = Field(
        None, pattern=r"^(s3|https)://\S+$", max_length=2048, description="Mezzanine file (s3:// or https://)."
    )
    generator: GeneratorInput | None = Field(None, description="Still image rendered for a whole number of GOPs.")
    kind: AssetKind = AssetKind.CONTENT
    asset_id: str | None = Field(None, pattern=ASSET_ID_PATTERN, description="Defaults to asset_id_for(source).")
    content_id: str | None = Field(None, min_length=1, max_length=64, description="Catalog content id.")
    cue_points_ms: list[int] = Field([], description="Editorial ad cue points (ms); snapped to the GOP grid.")
    ladder_version: str = Field(pattern=LADDER_VERSION_PATTERN, description="Ladder version, e.g. standard@0123abcd45.")
    correlation_id: str | None = Field(
        None, pattern=r"^[A-Za-z0-9_-]{1,80}$", description="Trace id; Step Functions execution name."
    )
    metadata: dict[str, str] = {}

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.source_uri is None) == (self.generator is None):
            raise ValueError("exactly one of source_uri and generator is required")
        if self.generator is not None and self.cue_points_ms:
            raise ValueError("generated slates and house ads have no cue points")
        if any(ms < 0 for ms in self.cue_points_ms):
            raise ValueError("cue points must not be negative")
        return self

    @property
    def resolved_asset_id(self) -> str:
        """``asset_id``, else derived from the source (or the generator's label, image and length)."""
        if self.asset_id:
            return self.asset_id
        if self.generator is None:
            return asset_id_for(str(self.source_uri))
        g = self.generator
        return f"{g.label[:41].rstrip('-')}-{digest(f'{g.image_s3_uri}#{g.duration_gops}')}"
