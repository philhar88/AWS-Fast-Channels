"""Captions: the tracks a ladder carries, what an ingest request provides, and where each track of an asset comes from.

Channel Assembly re-hosts subtitle renditions by position. When two VOD sources of a channel differ in them it drops
ad breaks without an alert, answers 404 for a subtitle playlist or resets sequence numbers
(docs/captions.md). The caption tracks are therefore part of the ladder: every asset encoded with
it (content, filler, break slates and house ads) writes the same IMSC1 text tracks, and a track with nothing to
show is empty (``NULL_SOURCE``). IMSC1 is the only caption output MediaConvert writes in CMAF segmented on the video
grid, for HLS and DASH alike; its CMAF WebVTT is one unsegmented file, and CEA-608/708 cannot be carried in CMAF
(docs/captions.md).

The source of each track is resolved per asset, in this order (first match wins):

1. a **provided** file: ``captions.files`` of the request, or a drop-folder sibling ``<stem>.<language>.<ext>``;
2. captions **embedded** in the source: a QuickTime ``c608`` data track (MediaConvert Probe reports it), or
   CEA-608/708 in the video (SEI, MPEG-2 user data, MXF VANC), which Probe cannot see, so a caption scan job looks;
3. **generated** by the stage's captioner from the main audio, for the spoken language only;
4. **none**: an empty track. A stage that requires captions refuses the asset instead when this would leave its
   spoken-language track empty.
"""

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fast_channels.core.timeline import FrameRate  # noqa: TC001 - pydantic resolves field types at runtime

LANGUAGE_PATTERN: Final = r"^[a-z]{2}(-[A-Z]{2})?$"
"""BCP-47 language with an optional region (``en``, ``pt-BR``): HLS LANGUAGE and DASH ``@lang`` of a track."""
LABEL_PATTERN: Final = r"^[A-Za-z0-9 _]+$"
"""MediaConvert LanguageDescription (the HLS NAME and DASH Label of a track) allows these characters only."""

_ISO_639_2: Final = {
    "ara": "ar", "ces": "cs", "cze": "cs", "chi": "zh", "zho": "zh", "dan": "da", "deu": "de", "ger": "de",
    "ell": "el", "gre": "el", "eng": "en", "spa": "es", "fin": "fi", "fra": "fr", "fre": "fr", "heb": "he",
    "hin": "hi", "hun": "hu", "ind": "id", "ita": "it", "jpn": "ja", "kor": "ko", "msa": "ms", "may": "ms",
    "nld": "nl", "dut": "nl", "nor": "no", "nob": "nb", "pol": "pl", "por": "pt", "ron": "ro", "rum": "ro",
    "rus": "ru", "swe": "sv", "tha": "th", "tur": "tr", "ukr": "uk", "vie": "vi",
}  # fmt: skip
"""ISO 639-2 (MediaConvert Probe ``LanguageCode``) to ISO 639-1 for the languages a caption engine might speak."""


class CaptionFormat(StrEnum):
    """Sidecar caption formats MediaConvert reads (``SourceType``)."""

    SCC = "scc"
    SRT = "srt"
    WEBVTT = "webvtt"
    TTML = "ttml"
    SMPTE_TT = "smpte-tt"
    IMSC = "imsc"
    STL = "stl"
    SMI = "smi"

    @property
    def source_type(self) -> str:
        return _SOURCE_TYPES[self]

    @classmethod
    def of(cls, uri: str) -> CaptionFormat:
        """The format a file name implies (``.xml`` is read as TTML; declare ``imsc`` or ``smpte-tt`` explicitly)."""
        suffix = PurePosixPath(urlsplit(uri).path).suffix.lower()
        try:
            return _EXTENSIONS[suffix]
        except KeyError:
            raise ValueError(f"{uri}: unknown caption file extension {suffix!r}; set its format") from None


_SOURCE_TYPES: Final = {
    CaptionFormat.SCC: "SCC",
    CaptionFormat.SRT: "SRT",
    CaptionFormat.WEBVTT: "WEBVTT",
    CaptionFormat.TTML: "TTML",
    CaptionFormat.SMPTE_TT: "SMPTE_TT",
    CaptionFormat.IMSC: "IMSC",
    CaptionFormat.STL: "STL",
    CaptionFormat.SMI: "SMI",
}
_EXTENSIONS: Final = {
    ".scc": CaptionFormat.SCC,
    ".srt": CaptionFormat.SRT,
    ".vtt": CaptionFormat.WEBVTT,
    ".webvtt": CaptionFormat.WEBVTT,
    ".ttml": CaptionFormat.TTML,
    ".dfxp": CaptionFormat.TTML,
    ".xml": CaptionFormat.TTML,
    ".stl": CaptionFormat.STL,
    ".smi": CaptionFormat.SMI,
    ".sami": CaptionFormat.SMI,
}
CAPTION_EXTENSIONS: Final = frozenset(_EXTENSIONS)
"""File extensions of sidecar caption files (drop-folder discovery)."""


def primary_language(language: str) -> str:
    """``pt-BR`` -> ``pt``: tracks, sources and engines match on the primary language subtag."""
    return language.partition("-")[0]


def iso_639_1(code: str | None) -> str | None:
    """A Probe language code (ISO 639-2, ``eng``) as ISO 639-1 (``en``); ``None`` when unknown or undetermined."""
    if not code:
        return None
    code = code.lower()
    return code if re.fullmatch(r"[a-z]{2}", code) else _ISO_639_2.get(code)


class CaptionKind(StrEnum):
    """What a track transcribes: ``captions`` (closed captions, SDH) also describe sound and music; ``subtitles``
    only the dialogue. Sets the HLS CHARACTERISTICS and the DASH accessibility role of the track."""

    CAPTIONS = "captions"
    SUBTITLES = "subtitles"


class CaptionTrack(BaseModel):
    """One text rendition of a ladder: an IMSC1 track every asset of the ladder carries, in ladder order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    language: str = Field(pattern=LANGUAGE_PATTERN, description="BCP-47 language of the track, e.g. en.")
    label: str = Field(pattern=LABEL_PATTERN, max_length=32, description="Display name (HLS NAME), e.g. English.")
    kind: CaptionKind = Field(
        CaptionKind.CAPTIONS, description="captions (closed captions, SDH) or subtitles (dialogue only)."
    )

    @property
    def name_modifier(self) -> str:
        """MediaConvert output NameModifier (``_cc-en``)."""
        return f"_cc-{self.language.lower()}"

    @property
    def selector(self) -> str:
        """Name of the job's caption selector that feeds this track (``Captions en``)."""
        return f"Captions {self.language}"


class CaptionFile(BaseModel):
    """A caption file provided with a source, for one language."""

    model_config = ConfigDict(extra="forbid")

    uri: str = Field(
        min_length=1,
        max_length=2048,
        description="s3:// or https:// URI, or a key relative to the source file's folder (movie.en.scc).",
    )
    language: str = Field(pattern=LANGUAGE_PATTERN, description="BCP-47 language of the captions, e.g. en.")
    format: CaptionFormat | None = Field(None, description="Defaults to the format the file extension implies.")
    offset_ms: int | None = Field(
        None,
        description="Shift added to every cue time (negative is earlier). Defaults to 0, and for SCC files to "
        "minus the hour (or source timecode) they were authored from, e.g. -3600000 for 01:00:00;00.",
    )
    frame_rate: FrameRate | None = Field(None, description="Timecode rate of an SCC file; MediaConvert's default.")

    @property
    def resolved_format(self) -> CaptionFormat:
        return self.format or CaptionFormat.of(self.uri)

    @model_validator(mode="after")
    def _checks(self) -> Self:
        fmt = self.resolved_format
        if self.frame_rate is not None and fmt is not CaptionFormat.SCC:
            raise ValueError("frame_rate applies to SCC files only")
        if fmt is CaptionFormat.IMSC and not self.uri.lower().endswith(".xml"):
            raise ValueError("MediaConvert reads IMSC sidecars only from .xml files")
        return self

    def absolute(self, source_uri: str) -> Self:
        """This file with ``uri`` resolved against the folder of ``source_uri``."""
        if re.match(r"^(s3|https)://", self.uri):
            return self
        folder = source_uri.rsplit("/", 1)[0]
        return self.model_copy(update={"uri": f"{folder}/{self.uri.lstrip('/')}"})


class CaptionMode(StrEnum):
    """How an ingest finds captions: ``auto`` uses provided files, then captions in the source, then generates;
    ``generate`` ignores captions in the source; ``off`` writes empty tracks."""

    AUTO = "auto"
    GENERATE = "generate"
    OFF = "off"


class CaptionsRequest(BaseModel):
    """The captions part of an ingest request."""

    model_config = ConfigDict(extra="forbid")

    mode: CaptionMode = CaptionMode.AUTO
    files: list[CaptionFile] = Field([], description="Provided caption files, at most one per language.")
    spoken_language: str | None = Field(
        None,
        pattern=LANGUAGE_PATTERN,
        description="Language of the main audio: what embedded and generated captions are in. Defaults to the "
        "language of the source's first audio track, then the ladder's first caption track.",
    )

    @model_validator(mode="after")
    def _one_file_per_language(self) -> Self:
        languages = [primary_language(f.language) for f in self.files]
        if duplicates := sorted({lang for lang in languages if languages.count(lang) > 1}):
            raise ValueError(f"more than one caption file for {duplicates}")
        return self


class CaptionOrigin(StrEnum):
    PROVIDED = "provided"
    EMBEDDED = "embedded"
    GENERATED = "generated"
    NONE = "none"


class Carriage(StrEnum):
    """Where embedded captions are: CEA-608/708 in the video (``embedded``) or a QuickTime ``c608`` / MXF ANC track
    (``ancillary``)."""

    EMBEDDED = "embedded"
    ANCILLARY = "ancillary"


class CaptionSource(BaseModel):
    """Where one caption track of an asset comes from; :meth:`selector` is its MediaConvert CaptionSelector."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    language: str = Field(pattern=LANGUAGE_PATTERN)
    origin: CaptionOrigin
    uri: str | None = None
    format: CaptionFormat | None = None
    offset_ms: int = 0
    frame_rate: FrameRate | None = None
    carriage: Carriage | None = None
    engine: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        has_file = self.uri is not None and self.format is not None
        match self.origin:
            case CaptionOrigin.PROVIDED | CaptionOrigin.GENERATED if not has_file:
                raise ValueError(f"{self.origin} captions need a file and its format")
            case CaptionOrigin.EMBEDDED if self.carriage is None:
                raise ValueError("embedded captions need their carriage")
        return self

    @classmethod
    def none(cls, language: str) -> CaptionSource:
        return cls(language=language, origin=CaptionOrigin.NONE)

    @classmethod
    def provided(cls, file: CaptionFile, *, language: str) -> CaptionSource:
        return cls(
            language=language,
            origin=CaptionOrigin.PROVIDED,
            uri=file.uri,
            format=file.resolved_format,
            offset_ms=file.offset_ms or 0,
            frame_rate=file.frame_rate,
        )

    @property
    def empty(self) -> bool:
        return self.origin is CaptionOrigin.NONE

    def selector(self) -> dict[str, Any]:
        """The ``CaptionSelectors`` entry (MediaConvert API shape) that reads this source."""
        settings: dict[str, Any]
        match self.origin, self.carriage, self.format:
            case CaptionOrigin.NONE, _, _:
                settings = {"SourceType": "NULL_SOURCE"}
            case CaptionOrigin.EMBEDDED, Carriage.ANCILLARY, _:
                settings = {"SourceType": "ANCILLARY", "AncillarySourceSettings": {"SourceAncillaryChannelNumber": 1}}
            case CaptionOrigin.EMBEDDED, _, _:
                settings = {"SourceType": "EMBEDDED", "EmbeddedSourceSettings": {"Source608ChannelNumber": 1}}
            case _, _, CaptionFormat() as fmt:
                settings = {"SourceType": fmt.source_type, "FileSourceSettings": self._file_settings()}
            case _:
                raise ValueError(f"{self.origin} captions need a file")
        return {"SourceSettings": settings}

    def _file_settings(self) -> dict[str, Any]:
        file: dict[str, Any] = {"SourceFile": self.uri}
        if self.offset_ms:
            file |= {"TimeDelta": self.offset_ms, "TimeDeltaUnits": "MILLISECONDS"}
        if self.frame_rate is not None:
            file["Framerate"] = {
                "FramerateNumerator": self.frame_rate.numerator,
                "FramerateDenominator": self.frame_rate.denominator,
            }
        return file


class CaptionProvenance(BaseModel):
    """Where an encoded caption track came from: asset items, ``Asset Ready`` and the agent's asset details."""

    model_config = ConfigDict(frozen=True)

    language: str
    origin: CaptionOrigin
    uri: str | None = Field(None, description="The file the track was encoded from (provided or generated).")
    engine: str | None = Field(None, description="The captioner engine, for generated captions.")


class SourceCaptions(BaseModel):
    """What MediaConvert Probe tells about a source's captions and its spoken language."""

    model_config = ConfigDict(frozen=True)

    audio_language: str | None = None
    """ISO 639-1 language of the first audio track, if the container declares one."""
    c608_language: str | None = None
    """ISO 639-1 language of a QuickTime ``c608`` data track, ``"und"`` when it declares none; ``None``: no track."""


type Awaiting = Literal["scan", "generation"]


class CaptionPlan(BaseModel):
    """The source of every caption track of an asset, in ladder order, while it is being resolved.

    ``awaiting`` names the step the spoken-language track still waits for: the caption ``scan`` of the source, or its
    ``generation``. Every other track is final.
    """

    model_config = ConfigDict(frozen=True)

    sources: tuple[CaptionSource, ...] = ()
    spoken_language: str | None = None
    awaiting: Awaiting | None = None
    notes: tuple[str, ...] = ()

    @property
    def spoken(self) -> CaptionSource | None:
        """The track of the spoken language, if the ladder has one."""
        if self.spoken_language is None:
            return None
        target = primary_language(self.spoken_language)
        return next((s for s in self.sources if primary_language(s.language) == target), None)

    def replace_spoken(self, source: CaptionSource, *, note: str | None = None) -> CaptionPlan:
        """This plan with the spoken-language track set to ``source`` and nothing awaited any more."""
        spoken = self.spoken
        sources = tuple(source if s is spoken else s for s in self.sources)
        return self.model_copy(
            update={"sources": sources, "awaiting": None, "notes": (*self.notes, note) if note else self.notes}
        )

    def provenance(self) -> list[CaptionProvenance]:
        """Where each track comes from, in ladder order."""
        return [
            CaptionProvenance(language=s.language, origin=s.origin, uri=s.uri, engine=s.engine) for s in self.sources
        ]

    def selectors(self, tracks: tuple[CaptionTrack, ...]) -> dict[str, dict[str, Any]]:
        """``Inputs[0].CaptionSelectors`` for a ladder's ``tracks`` (every track: the template references them all)."""
        if self.awaiting is not None:
            raise ValueError(f"the captions still await their {self.awaiting}")
        by_language = {s.language: s for s in self.sources}
        return {t.selector: by_language.get(t.language, CaptionSource.none(t.language)).selector() for t in tracks}


class CaptionPolicy(BaseModel):
    """The stage's caption settings that resolution needs."""

    model_config = ConfigDict(frozen=True)

    required: bool = False
    """Refuse an asset whose spoken-language track would be empty."""
    can_generate: bool = False
    """A captioner is deployed."""


class CaptionsRefusedError(ValueError):
    """The stage requires captions and this asset cannot get them."""


def plan_captions(
    tracks: tuple[CaptionTrack, ...],
    request: CaptionsRequest,
    probed: SourceCaptions | None,
    policy: CaptionPolicy,
) -> CaptionPlan:
    """First resolution, at Prepare: provided files and Probe-visible tracks; what the spoken language awaits.

    ``probed`` is ``None`` for generated stills (slates and house ads), whose tracks are always empty.
    """
    empty = tuple(CaptionSource.none(t.language) for t in tracks)
    if not tracks or probed is None:
        return CaptionPlan(sources=empty)
    spoken = request.spoken_language or probed.audio_language or tracks[0].language
    if request.mode is CaptionMode.OFF:
        plan = CaptionPlan(sources=empty, spoken_language=spoken, notes=("captions are off for this asset",))
        return check_required(plan, policy)
    files = {primary_language(f.language): f for f in request.files}
    plan = CaptionPlan(
        sources=tuple(
            CaptionSource.provided(file, language=t.language)
            if (file := files.get(primary_language(t.language)))
            else CaptionSource.none(t.language)
            for t in tracks
        ),
        spoken_language=spoken,
    )
    if plan.spoken is None or not plan.spoken.empty:
        return check_required(plan, policy)
    c608 = probed.c608_language
    if request.mode is CaptionMode.AUTO and c608 is not None and c608 in {"und", primary_language(spoken)}:
        embedded = CaptionSource(
            language=plan.spoken.language, origin=CaptionOrigin.EMBEDDED, carriage=Carriage.ANCILLARY
        )
        return plan.replace_spoken(embedded, note="captions from the source's c608 track")
    if request.mode is CaptionMode.AUTO:
        return plan.model_copy(update={"awaiting": "scan"})
    return awaiting_generation(plan, policy)


def after_scan(plan: CaptionPlan, cues: int, policy: CaptionPolicy) -> CaptionPlan:
    """Second resolution: the caption scan found ``cues`` cues of embedded CEA-608/708 in the source."""
    if plan.awaiting != "scan" or plan.spoken is None:
        raise ValueError("the captions do not await a scan")
    if cues > 0:
        embedded = CaptionSource(
            language=plan.spoken.language, origin=CaptionOrigin.EMBEDDED, carriage=Carriage.EMBEDDED
        )
        return plan.replace_spoken(embedded, note=f"captions embedded in the video ({cues} cues found)")
    return awaiting_generation(plan.model_copy(update={"awaiting": None}), policy)


def awaiting_generation(plan: CaptionPlan, policy: CaptionPolicy) -> CaptionPlan:
    """The plan waiting for its spoken-language track to be generated, or final when no captioner is deployed."""
    if policy.can_generate:
        return plan.model_copy(update={"awaiting": "generation"})
    return check_required(plan.replace_spoken(CaptionSource.none(_spoken_language(plan)), note="no captioner"), policy)


def after_generation(
    plan: CaptionPlan, *, uri: str, format: CaptionFormat, engine: str, policy: CaptionPolicy
) -> CaptionPlan:
    """Third resolution: the captioner wrote the spoken language's captions to ``uri``."""
    if plan.awaiting != "generation":
        raise ValueError("the captions do not await generation")
    generated = CaptionSource(
        language=_spoken_language(plan), origin=CaptionOrigin.GENERATED, uri=uri, format=format, engine=engine
    )
    return check_required(plan.replace_spoken(generated, note=f"captions generated by {engine}"), policy)


def generation_failed(plan: CaptionPlan, error: str, policy: CaptionPolicy) -> CaptionPlan:
    """The captioner failed (``error`` says why): the spoken-language track stays empty, unless that is refused."""
    if plan.awaiting != "generation":
        raise ValueError("the captions do not await generation")
    empty = CaptionSource.none(_spoken_language(plan))
    return check_required(plan.replace_spoken(empty, note=f"caption generation failed: {error}"), policy)


def check_required(plan: CaptionPlan, policy: CaptionPolicy) -> CaptionPlan:
    """``plan`` unchanged, or :class:`CaptionsRefusedError` when the stage requires captions it cannot have."""
    if not policy.required or plan.awaiting is not None:
        return plan
    if plan.spoken_language is not None and plan.spoken is None:
        raise CaptionsRefusedError(f"the ladder has no caption track for the spoken language {plan.spoken_language}")
    if plan.spoken is not None and plan.spoken.empty:
        reason = "; ".join(plan.notes) or "no captions were provided or found"
        raise CaptionsRefusedError(
            f"captions are required and the {plan.spoken.language} track would be empty ({reason})"
        )
    return plan


_SCC_TIMECODE: Final = re.compile(r"^(\d{2}):(\d{2}):(\d{2})[:;](\d{2})\s", re.MULTILINE)


def scc_offset_ms(scc: str, *, start_timecode_ms: int = 0) -> int:
    """The ``offset_ms`` that lines an SCC file up with a zero-based source (the pipeline's input timecode).

    SCC cues are timecodes, conventionally authored against the programme's start timecode (often 01:00:00;00).
    With a source timecode at or before the first cue, the shift is minus that timecode; without one, minus the
    whole hours of the first cue. A file whose first cue is in hour 0 needs no shift.
    """
    match = _SCC_TIMECODE.search(scc)
    if match is None:
        return 0
    hours, minutes, seconds = (int(g) for g in match.groups()[:3])
    first_ms = ((hours * 60 + minutes) * 60 + seconds) * 1000
    if 0 < start_timecode_ms <= first_ms:
        return -start_timecode_ms
    return -(first_ms // 3_600_000) * 3_600_000


def _spoken_language(plan: CaptionPlan) -> str:
    if plan.spoken is None:
        raise ValueError("the ladder has no caption track for the spoken language")
    return plan.spoken.language
