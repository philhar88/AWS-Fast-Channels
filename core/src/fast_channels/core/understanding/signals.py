"""The analyses' results, reduced to what the describe step reads: a compact summary of a title's speech and
pictures. Pure: the workflow's collector pages the AWS results and passes them in.

- Transcribe's transcript JSON gives the spoken language, the share of the runtime with speech and an excerpt.
- Rekognition's segments give the shot count and the technical cues (credits, black frames, ...).
- Rekognition's labels (aggregated by segment) give the objects and scenes with the most screen time.
- Rekognition's moderation labels give the top-level categories seen and the brand-safety tier they imply: by the
  most specific labels, with high and floor only for labels that are on screen for a while (:data:`SUSTAINED`).
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.understanding.record import Analysis, BrandSafety

EXCERPT_MAX_CHARS: Final = 8_000
"""Transcript characters the describe step reads: a long transcript is sampled at its start, middle and end."""
TOP_LABELS: Final = 25
LABEL_MIN_CONFIDENCE: Final = 80.0
MODERATION_MIN_CONFIDENCE: Final = 80.0
MODERATION_MIN_SIGHTINGS: Final = 2
"""A category counts only when seen in at least two frames at that confidence: one frame is too often a false
positive."""
SUSTAINED_FRAMES: Final = 10
"""Frames a label must be seen in, at :data:`SUSTAINED_CONFIDENCE` or more, to give its ``high`` or ``floor`` tier:
Rekognition samples about two frames a second, so about five seconds on screen. Below it, the label gives one tier
less: a moment of blood in an action film is not a graphic title."""
SUSTAINED_CONFIDENCE: Final = 90.0
SPEECH_GAP_S: Final = 1.0
"""Words closer than this belong to one stretch of speech."""

MODERATION_TIERS: Final[Mapping[str, BrandSafety]] = {
    # Rekognition's moderation taxonomy v7, by its most specific labels; a top-level category's own tier is what it
    # implies when no more specific label comes with it. The names of the v6 taxonomy that some calls still return
    # follow their v7 counterparts.
    "Explicit": BrandSafety.FLOOR,
    "Explicit Nudity": BrandSafety.FLOOR,
    "Exposed Male Genitalia": BrandSafety.FLOOR,
    "Exposed Female Genitalia": BrandSafety.FLOOR,
    "Exposed Buttocks or Anus": BrandSafety.FLOOR,
    "Exposed Female Nipple": BrandSafety.FLOOR,
    "Explicit Sexual Activity": BrandSafety.FLOOR,
    "Sex Toys": BrandSafety.FLOOR,
    "Hate Symbols": BrandSafety.FLOOR,
    "Nazi Party": BrandSafety.FLOOR,
    "White Supremacy": BrandSafety.FLOOR,
    "Extremist": BrandSafety.FLOOR,
    "Violence": BrandSafety.MEDIUM,
    "Weapons": BrandSafety.MEDIUM,
    "Graphic Violence": BrandSafety.MEDIUM,
    "Weapon Violence": BrandSafety.MEDIUM,
    "Physical Violence": BrandSafety.MEDIUM,
    "Explosions and Blasts": BrandSafety.MEDIUM,
    "Blood & Gore": BrandSafety.HIGH,
    "Self-Harm": BrandSafety.HIGH,
    "Visually Disturbing": BrandSafety.MEDIUM,
    "Death and Emaciation": BrandSafety.HIGH,
    "Emaciated Bodies": BrandSafety.HIGH,
    "Corpses": BrandSafety.HIGH,
    "Crashes": BrandSafety.MEDIUM,
    "Air Crash": BrandSafety.MEDIUM,
    "Drugs & Tobacco": BrandSafety.MEDIUM,
    "Drugs": BrandSafety.MEDIUM,
    "Tobacco": BrandSafety.MEDIUM,
    "Non-Explicit Nudity of Intimate parts and Kissing": BrandSafety.MEDIUM,
    "Suggestive": BrandSafety.MEDIUM,
    "Kissing on the Lips": BrandSafety.LOW,
    "Rude Gestures": BrandSafety.MEDIUM,
    "Gambling": BrandSafety.MEDIUM,
    "Alcohol": BrandSafety.LOW,
    "Swimwear or Underwear": BrandSafety.LOW,
}
"""The brand-safety tier each moderation label implies, before the exposure rule (:data:`SUSTAINED_FRAMES`). A label
that is not listed takes its top-level category's tier, and an unknown category counts as ``medium``. Weapons and
action violence are ``medium``; blood, self-harm, corpses and emaciation ``high``; explicit content and hate
symbols ``floor``."""


class Sighting(BaseModel):
    """How long one moderation label was on screen."""

    model_config = ConfigDict(frozen=True)

    category: str
    """Its top-level category."""
    frames: int
    """Frames it was seen in at :data:`MODERATION_MIN_CONFIDENCE` or more."""
    sustained: int = 0
    """Frames it was seen in at :data:`SUSTAINED_CONFIDENCE` or more."""
    confidence: float

    def tier(self, name: str) -> BrandSafety:
        """The tier this label gives: its own (or its category's), one less for a high or floor label that was not
        on screen for :data:`SUSTAINED_FRAMES` confident frames."""
        tier = MODERATION_TIERS.get(name) or MODERATION_TIERS.get(self.category, BrandSafety.MEDIUM)
        if tier.rank >= BrandSafety.HIGH.rank and self.sustained < SUSTAINED_FRAMES:
            return list(BrandSafety)[tier.rank - 1]
        return tier


class LabelTime(BaseModel):
    """A label and how long it is on screen."""

    model_config = ConfigDict(frozen=True)

    name: str
    seconds: float


class Signals(BaseModel):
    """What the analyses found, as the describe step reads it."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    duration_ms: int = Field(ge=0)
    analyses: tuple[Analysis, ...] = ()
    """The analyses whose results are here."""
    language: str | None = None
    speech_ratio: float | None = None
    transcript: str = ""
    """The transcript, or an excerpt of at most :data:`EXCERPT_MAX_CHARS`."""
    shots: int | None = None
    mean_shot_s: float | None = None
    cues: dict[str, int] = {}
    """Technical cues by type (``EndCredits``, ``BlackFrames``, ...)."""
    credits_at_s: float | None = None
    """When the end credits start."""
    labels: tuple[LabelTime, ...] = ()
    """The labels with the most screen time, most first."""
    moderation: dict[str, float] = {}
    """Top-level moderation categories seen, with their highest confidence."""
    moderation_labels: dict[str, Sighting] = {}
    """Every moderation label seen (each level of the taxonomy), by name: what the tier is computed from."""

    @property
    def moderation_tier(self) -> BrandSafety | None:
        """The tier the moderation labels imply, the riskiest label's (:meth:`Sighting.tier`); null when moderation
        did not run. Signals without labels (an older collector's) fall back to the top-level categories."""
        if Analysis.MODERATION not in self.analyses:
            return None
        tier = BrandSafety.LOW
        for name, sighting in self.moderation_labels.items():
            tier = tier.worse(sighting.tier(name))
        if not self.moderation_labels:
            for category in self.moderation:
                tier = tier.worse(MODERATION_TIERS.get(category, BrandSafety.MEDIUM))
        return tier

    def brief(self) -> dict[str, Any]:
        """The signals as the describe step's prompt carries them (JSON-ready, without the transcript)."""
        return {
            "duration_s": round(self.duration_ms / 1000),
            "language": self.language,
            "speech_ratio": self.speech_ratio,
            "shots": self.shots,
            "mean_shot_s": self.mean_shot_s,
            "technical_cues": self.cues,
            "credits_at_s": self.credits_at_s,
            "labels": [{"name": label.name, "seconds": label.seconds} for label in self.labels],
            "moderation": {
                name: {"category": seen.category, "frames": seen.frames, "confidence": seen.confidence}
                for name, seen in self.moderation_labels.items()
            }
            or self.moderation,
        }


def transcript_signals(document: Mapping[str, Any], duration_ms: int) -> dict[str, Any]:
    """Language, speech ratio and transcript (excerpt) from Transcribe's output JSON."""
    results = document.get("results") or {}
    text = " ".join(str(t.get("transcript", "")) for t in results.get("transcripts") or []).strip()
    code = str(results.get("language_code") or document.get("language_code") or "")
    words = [
        (float(item["start_time"]), float(item["end_time"]))
        for item in results.get("items") or []
        if item.get("type") == "pronunciation" and "start_time" in item and "end_time" in item
    ]
    return {
        # Transcribe names a language even for a title without speech: only a transcript makes it the title's.
        "language": code[:2].lower() if text and len(code) >= 2 and code[:2].isalpha() else None,
        "speech_ratio": _speech_ratio(words, duration_ms),
        "transcript": excerpt(text),
    }


def excerpt(text: str, limit: int = EXCERPT_MAX_CHARS) -> str:
    """``text``, or its start, middle and end when it is longer than ``limit``, joined by `` … ``."""
    if len(text) <= limit:
        return text
    part = (limit - 6) // 3
    middle = (len(text) - part) // 2
    return " … ".join((text[:part], text[middle : middle + part], text[-part:]))


def _speech_ratio(words: list[tuple[float, float]], duration_ms: int) -> float | None:
    if duration_ms <= 0:
        return None
    spoken = 0.0
    start = end = None
    for begin, finish in sorted(words):
        if end is not None and begin - end <= SPEECH_GAP_S:
            end = max(end, finish)
            continue
        if start is not None and end is not None:
            spoken += end - start
        start, end = begin, finish
    if start is not None and end is not None:
        spoken += end - start
    return round(min(1.0, spoken * 1000 / duration_ms), 3)


def segment_signals(segments: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Shot count, mean shot length and technical cues from ``GetSegmentDetection`` segments (every page)."""
    shots: list[int] = []
    cues: Counter[str] = Counter()
    credits: list[int] = []
    for segment in segments:
        match segment.get("Type"):
            case "SHOT":
                shots.append(int(segment.get("DurationMillis") or 0))
            case "TECHNICAL_CUE":
                kind = str((segment.get("TechnicalCueSegment") or {}).get("Type", "Unknown"))
                cues[kind] += 1
                if kind == "EndCredits":
                    credits.append(int(segment.get("StartTimestampMillis") or 0))
    return {
        "shots": len(shots) if shots else None,
        "mean_shot_s": round(sum(shots) / len(shots) / 1000, 1) if shots else None,
        "cues": dict(sorted(cues.items())),
        "credits_at_s": round(min(credits) / 1000, 1) if credits else None,
    }


def label_signals(labels: Iterable[Mapping[str, Any]], *, top: int = TOP_LABELS) -> tuple[LabelTime, ...]:
    """The labels with the most screen time, from ``GetLabelDetection`` with ``AggregateBy=SEGMENTS`` (every page)."""
    seconds: defaultdict[str, float] = defaultdict(float)
    for entry in labels:
        label = entry.get("Label") or {}
        if float(label.get("Confidence") or 0) < LABEL_MIN_CONFIDENCE or not label.get("Name"):
            continue
        duration = entry.get("DurationMillis")
        if duration is None and "StartTimestampMillis" in entry and "EndTimestampMillis" in entry:
            duration = int(entry["EndTimestampMillis"]) - int(entry["StartTimestampMillis"])
        seconds[str(label["Name"])] += max(0, int(duration or 0)) / 1000
    # A label seen in one frame only has no duration: it is not on screen long enough to describe the title.
    ranked = sorted(((n, s) for n, s in seconds.items() if s > 0), key=lambda item: (-item[1], item[0]))[:top]
    return tuple(LabelTime(name=name, seconds=round(time, 1)) for name, time in ranked)


def moderation_signals(labels: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Top-level moderation categories seen in at least :data:`MODERATION_MIN_SIGHTINGS` frames at
    :data:`MODERATION_MIN_CONFIDENCE` or more, with their highest confidence, from ``GetContentModeration``.

    A frame reports a label and each of its parents (``Explicit`` and ``Explicit Sexual Activity`` at one timestamp),
    so a sighting is a distinct timestamp, not an entry."""
    found = [(entry.get("Timestamp", index), entry.get("ModerationLabel") or {}) for index, entry in enumerate(labels)]
    parents = {str(e["Name"]): str(e.get("ParentName") or "") for _, e in found if e.get("Name")}
    frames: defaultdict[str, set[Any]] = defaultdict(set)
    confidence: dict[str, float] = {}
    for timestamp, entry in found:
        if not entry.get("Name") or float(entry.get("Confidence") or 0) < MODERATION_MIN_CONFIDENCE:
            continue
        top = _top_level(str(entry["Name"]), parents)
        frames[top].add(timestamp)
        confidence[top] = max(confidence.get(top, 0.0), round(float(entry["Confidence"]), 1))
    return {name: confidence[name] for name in sorted(confidence) if len(frames[name]) >= MODERATION_MIN_SIGHTINGS}


def moderation_labels(labels: Iterable[Mapping[str, Any]]) -> dict[str, Sighting]:
    """Every moderation label, at each level of the taxonomy, seen in at least :data:`MODERATION_MIN_SIGHTINGS`
    frames at :data:`MODERATION_MIN_CONFIDENCE` or more, from ``GetContentModeration``: its top-level category, its
    frames, its frames at :data:`SUSTAINED_CONFIDENCE` or more and its highest confidence."""
    found = [(entry.get("Timestamp", index), entry.get("ModerationLabel") or {}) for index, entry in enumerate(labels)]
    parents = {str(e["Name"]): str(e.get("ParentName") or "") for _, e in found if e.get("Name")}
    frames: defaultdict[str, set[Any]] = defaultdict(set)
    sustained: defaultdict[str, set[Any]] = defaultdict(set)
    confidence: dict[str, float] = {}
    for timestamp, entry in found:
        score = float(entry.get("Confidence") or 0)
        if not entry.get("Name") or score < MODERATION_MIN_CONFIDENCE:
            continue
        name = str(entry["Name"])
        frames[name].add(timestamp)
        if score >= SUSTAINED_CONFIDENCE:
            sustained[name].add(timestamp)
        confidence[name] = max(confidence.get(name, 0.0), round(score, 1))
    return {
        name: Sighting(
            category=_top_level(name, parents),
            frames=len(frames[name]),
            sustained=len(sustained[name]),
            confidence=confidence[name],
        )
        for name in sorted(frames)
        if len(frames[name]) >= MODERATION_MIN_SIGHTINGS
    }


def _top_level(name: str, parents: Mapping[str, str]) -> str:
    seen = {name}
    while (parent := parents.get(name)) and parent not in seen:
        seen.add(parent)
        name = parent
    return name
