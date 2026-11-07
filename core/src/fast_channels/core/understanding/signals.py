"""The analyses' results, reduced to what the describe step reads: a compact summary of a title's speech and
pictures. Pure: the workflow's collector pages the AWS results and passes them in.

- Transcribe's transcript JSON gives the spoken language, the share of the runtime with speech and an excerpt.
- Rekognition's segments give the shot count and the technical cues (credits, black frames, ...).
- Rekognition's labels (aggregated by segment) give the objects and scenes with the most screen time.
- Rekognition's moderation labels give the top-level categories seen and the brand-safety tier they imply.
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
"""A category counts only when seen at least twice at that confidence: one frame is too often a false positive."""
SPEECH_GAP_S: Final = 1.0
"""Words closer than this belong to one stretch of speech."""

MODERATION_TIERS: Final[Mapping[str, BrandSafety]] = {
    # Rekognition's moderation taxonomy, top level (v7), and the names of the v6 taxonomy still returned by some calls.
    "Explicit": BrandSafety.FLOOR,
    "Explicit Nudity": BrandSafety.FLOOR,
    "Hate Symbols": BrandSafety.FLOOR,
    "Violence": BrandSafety.HIGH,
    "Visually Disturbing": BrandSafety.HIGH,
    "Drugs & Tobacco": BrandSafety.MEDIUM,
    "Drugs": BrandSafety.MEDIUM,
    "Tobacco": BrandSafety.MEDIUM,
    "Non-Explicit Nudity of Intimate parts and Kissing": BrandSafety.MEDIUM,
    "Suggestive": BrandSafety.MEDIUM,
    "Rude Gestures": BrandSafety.MEDIUM,
    "Gambling": BrandSafety.MEDIUM,
    "Alcohol": BrandSafety.LOW,
    "Swimwear or Underwear": BrandSafety.LOW,
}
"""The brand-safety tier each top-level moderation category implies; an unknown category counts as ``medium``."""


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

    @property
    def moderation_tier(self) -> BrandSafety | None:
        """The tier the moderation labels imply; null when moderation did not run."""
        if Analysis.MODERATION not in self.analyses:
            return None
        tier = BrandSafety.LOW
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
            "moderation": self.moderation,
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
    """Top-level moderation categories seen at least :data:`MODERATION_MIN_SIGHTINGS` times at
    :data:`MODERATION_MIN_CONFIDENCE` or more, with their highest confidence, from ``GetContentModeration``."""
    entries = [entry.get("ModerationLabel") or {} for entry in labels]
    parents = {str(e["Name"]): str(e.get("ParentName") or "") for e in entries if e.get("Name")}
    sightings: Counter[str] = Counter()
    confidence: dict[str, float] = {}
    for entry in entries:
        if not entry.get("Name") or float(entry.get("Confidence") or 0) < MODERATION_MIN_CONFIDENCE:
            continue
        top = _top_level(str(entry["Name"]), parents)
        sightings[top] += 1
        confidence[top] = max(confidence.get(top, 0.0), round(float(entry["Confidence"]), 1))
    return {name: confidence[name] for name in sorted(confidence) if sightings[name] >= MODERATION_MIN_SIGHTINGS}


def _top_level(name: str, parents: Mapping[str, str]) -> str:
    seen = {name}
    while (parent := parents.get(name)) and parent not in seen:
        seen.add(parent)
        name = parent
    return name
