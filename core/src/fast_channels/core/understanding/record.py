"""One title's understanding: the ``UNDERSTANDING`` item beside its catalog item (contracts/content-understanding).

The item lives in the ``ReferenceCatalog`` table under the title's partition, ``pk = CONTENT#<content_id>``, with
``sk = UNDERSTANDING``. The CMS owns the ``META`` item and may rewrite it at any time; this item is the module's
alone, so a catalog sync never erases an analysis and an analysis never edits the CMS's data.
"""

from datetime import datetime
from enum import StrEnum
from functools import cache
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from fast_channels.core.catalog import CONTENT_ID_PATTERN
from fast_channels.core.crawl.taxonomy import CT31_IDS, load_taxonomy

TASK: Final = "understanding.describe"
"""The model-registry task of the describe step (``modules.models.tasks``)."""
ITEM_SK: Final = "UNDERSTANDING"
"""Sort key of the item, beside the CMS's ``META`` in the title's partition."""
VERSION: Final = 1
MAX_CATEGORIES: Final = 5
"""Content Taxonomy 3.1 categories per title, most apt first."""
MAX_KEYWORDS: Final = 10
KEYWORD_MAX_CHARS: Final = 40
MAX_MOODS: Final = 3
SYNOPSIS_MAX_CHARS: Final = 300


class Mood(StrEnum):
    """The tone of a title: a fixed vocabulary, so rails and buyers can rely on its words."""

    UPLIFTING = "uplifting"
    TENSE = "tense"
    DARK = "dark"
    HUMOROUS = "humorous"
    ROMANTIC = "romantic"
    CALM = "calm"
    ENERGETIC = "energetic"
    SAD = "sad"
    SUSPENSEFUL = "suspenseful"
    WHIMSICAL = "whimsical"
    EPIC = "epic"
    INFORMATIVE = "informative"


MOODS: Final = tuple(mood.value for mood in Mood)
"""The mood vocabulary, in a fixed order (the recommender's validator and fixtures import it)."""


class BrandSafety(StrEnum):
    """The title's brand-safety risk tier, in the spirit of the brand safety floor and suitability tiers of the
    industry framework: ``floor`` is content no advertiser should be next to, then high, medium and low risk."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    FLOOR = "floor"

    @property
    def rank(self) -> int:
        return list(BrandSafety).index(self)

    def worse(self, other: BrandSafety) -> BrandSafety:
        """The riskier of the two tiers."""
        return self if self.rank >= other.rank else other


class Analysis(StrEnum):
    """An analysis the workflow can run; each one is switched on or off per deployment for its cost."""

    TRANSCRIPT = "transcript"
    """Amazon Transcribe: speech, spoken language, keywords."""
    SHOTS = "shots"
    """Rekognition segment detection, shots."""
    TECHNICAL_CUES = "technical_cues"
    """Rekognition segment detection, technical cues: credits, black frames, color bars, slates, studio logos."""
    LABELS = "labels"
    """Rekognition label detection: objects, scenes, activities."""
    MODERATION = "moderation"
    """Rekognition content moderation: the visual evidence of the brand-safety tier."""


class Status(StrEnum):
    """``complete`` when every configured analysis and the describe step succeeded; ``partial`` when the item was
    written from what did (the workflow fails open)."""

    COMPLETE = "complete"
    PARTIAL = "partial"


@cache
def categories() -> dict[str, str]:
    """Content Taxonomy 3.1 category names by id: the taxonomy's categories, not its descriptive vectors (the file's
    section after the ``# Descriptive vectors`` comment)."""
    names = load_taxonomy()
    vectors: set[str] = set()
    in_vectors = False
    for line in CT31_IDS.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            in_vectors = line.startswith("# Descriptive vectors")
        elif in_vectors and line:
            vectors.add(line.split("\t", 1)[0])
    return {id_: name for id_, name in names.items() if id_ not in vectors}


class Understanding(BaseModel):
    """What the workflow found in one title (``CONTENT#<content_id>`` / ``UNDERSTANDING``)."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_attribute_docstrings=True)

    v: Literal[1] = VERSION
    """Schema version; a new schema is a new version, never an in-place change."""
    content_id: str = Field(pattern=CONTENT_ID_PATTERN, max_length=64)
    """The title's catalog id."""
    asset_id: str = Field(min_length=1, max_length=128)
    """The encode that was analysed."""
    status: Status
    analyses: tuple[Analysis, ...]
    """The analyses that succeeded and fed the item."""
    cat: tuple[str, ...] = Field(default=(), max_length=MAX_CATEGORIES)
    """IAB Tech Lab Content Taxonomy 3.1 category ids (AdCOM ``cattax`` 9), most apt first."""
    keywords: tuple[str, ...] = Field(default=(), max_length=MAX_KEYWORDS)
    """Lower-case descriptive keywords, most apt first."""
    moods: tuple[Mood, ...] = Field(default=(), max_length=MAX_MOODS)
    brand_safety: BrandSafety | None = None
    """The riskier of the model's tier and the tier of the moderation labels; null when neither ran."""
    moderation: tuple[str, ...] = ()
    """Top-level moderation categories found (Rekognition's taxonomy), the evidence of the tier."""
    synopsis: str = Field(default="", max_length=SYNOPSIS_MAX_CHARS)
    """One or two sentences, without spoilers."""
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    """The spoken language (ISO 639-1), from Transcribe."""
    shots: int | None = Field(default=None, ge=0)
    speech_ratio: float | None = Field(default=None, ge=0, le=1)
    """Share of the runtime with speech."""
    duration_ms: int = Field(ge=0)
    model: str | None = None
    """The model the describe step ran on (the task's foundation model id); null when it did not run."""
    analyzed_at: AwareDatetime

    @field_validator("cat")
    @classmethod
    def _taxonomy(cls, cat: tuple[str, ...]) -> tuple[str, ...]:
        if unknown := [id_ for id_ in cat if id_ not in categories()]:
            raise ValueError(f"not Content Taxonomy 3.1 categories: {unknown}")
        if len(set(cat)) != len(cat):
            raise ValueError("categories repeat")
        return cat

    @field_validator("keywords")
    @classmethod
    def _keywords(cls, keywords: tuple[str, ...]) -> tuple[str, ...]:
        if bad := [k for k in keywords if k != k.strip().lower() or not k or len(k) > KEYWORD_MAX_CHARS]:
            raise ValueError(f"keywords are lower case, trimmed and at most {KEYWORD_MAX_CHARS} characters: {bad}")
        return keywords


def item_key(content_id: str) -> dict[str, str]:
    """The item's key in the catalog table."""
    return {"pk": f"CONTENT#{content_id}", "sk": ITEM_SK}


def stamp(moment: datetime) -> str:
    """``analyzed_at`` as stored (ISO 8601, UTC, seconds)."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
