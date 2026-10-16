"""AWS Elemental Inference smart subtitles for files: the proxy, the chunk plan, and captions from live roll-up TTML.

Smart subtitles is a live feature: a feed transcribes the CMAF Ingest segments a client pushes with ``PutMedia`` and
returns TTML for any time window with ``GetMetadata`` (docs/captions.md). For a file:

1. A MediaConvert job writes a **proxy** in 1 s CMAF segments: 1280x720 H.264 (the only size a feed accepts; the
   picture does not matter) and AAC-LC stereo 48 kHz, with :data:`PREROLL_MS` of silence before the programme (a
   fresh feed loses its first seconds) and :data:`POSTROLL_MS` after it (flushes the transcriber).
2. A feed accepts at most 1.5 requests per second per stream, so one relays about 1.4x real time; a long title is
   split into **chunks** of :data:`CHUNK_SEGMENTS`, each relayed to a fresh feed with :data:`OVERLAP_SEGMENTS` of
   context on both sides, and authoritative only for its own seconds.
3. The feed answers with **roll-up** snapshots: one ``<p>`` per newly recognised word, a growing two-line display
   whose first line scrolls away. :func:`words` turns them into timed words and :func:`captions` into pop-on
   captions, which :func:`ttml` writes as an IMSC1 (TTML) document for the encode.

Times are milliseconds on the proxy timeline unless a name says otherwise; source time is proxy time minus
:data:`PREROLL_MS`.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from fast_channels.core.captions import primary_language

INFERENCE_LANGUAGES: Final = {
    "en": "eng", "en-AU": "eng-au", "en-GB": "eng-gb", "en-US": "eng-us",
    "de": "deu", "es": "spa", "fr": "fra", "it": "ita", "pt": "por",
}  # fmt: skip
"""Caption track language (BCP-47) -> smart subtitles ``language``; other regions of a language use its base."""
PREROLL_MS: Final = 8_000
POSTROLL_MS: Final = 10_000
SEGMENT_MS: Final = 1_000
PROXY_FPS: Final = 25
"""A whole number of frames per 1 s segment and GOP."""
PROXY_VIDEO: Final = "_v"
PROXY_AUDIO: Final = "_a"
VIDEO_STREAM: Final = "Streams(default-video.cmfv)"
AUDIO_STREAM: Final = "Streams(default-audio.cmfa)"
CHUNK_SEGMENTS: Final = 600
"""10 minutes of proxy per feed: at 1.4x it relays in about 7.5 minutes, inside one Lambda invocation."""
OVERLAP_SEGMENTS: Final = 15
WINDOW_MS: Final = 30_000
"""The largest window GetMetadata answers (30.001 s is ``RESULT_SET_TOO_LARGE``)."""
LINE_CHARACTERS: Final = 37
"""Broadcast caption line length (CEA-608 rows are 32; 37 to 42 is common for IP delivery)."""
MAX_CAPTION_MS: Final = 6_000
PAUSE_MS: Final = 1_500
"""A pause between words this long starts a new caption (and, when the display cleared, a new roll-up line)."""
MIN_CAPTION_MS: Final = 1_000
LINGER_MS: Final = 2_000
"""How long a caption stays after its last word started, at most (roll-up displays linger much longer)."""
_TT: Final = "{http://www.w3.org/ns/ttml}"
_SENTENCE_END: Final = re.compile("[.?!\u2026]['\"\u201d\u2019)]*$")
_CLAUSE_END: Final = re.compile("[.?!,;:\u2026]['\"\u201d\u2019)]*$")
_NORMALIZE: Final = re.compile(r"[^\w']+")


def inference_language(language: str) -> str | None:
    """The smart subtitles language of a caption track language, or ``None`` when the feature has none."""
    return INFERENCE_LANGUAGES.get(language) or INFERENCE_LANGUAGES.get(primary_language(language))


def proxy_settings(*, source_uri: str, destination: str) -> dict[str, Any]:
    """MediaConvert job settings (no template) of the proxy a feed is fed with (see the module docstring).

    Outputs ``<destination>_v_<n>.cmfv`` and ``<destination>_a_<n>.cmfa`` (``n`` from 1, one moof each, the mfhd
    sequence number equal to ``n``), their init segments and an HLS playlist per output.
    """
    source = {
        "FileInput": source_uri,
        "TimecodeSource": "ZEROBASED",
        "VideoSelector": {"Rotate": "AUTO"},
        "AudioSelectors": {"Audio Selector 1": {"DefaultSelection": "DEFAULT"}},
    }
    h264 = {
        "CodecProfile": "MAIN",
        "CodecLevel": "AUTO",
        "RateControlMode": "QVBR",
        "QvbrSettings": {"QvbrQualityLevel": 1},
        "MaxBitrate": 200_000,
        "GopSize": PROXY_FPS,
        "GopSizeUnits": "FRAMES",
        "GopClosedCadence": 1,
        "SceneChangeDetect": "DISABLED",
        "FramerateControl": "SPECIFIED",
        "FramerateNumerator": PROXY_FPS,
        "FramerateDenominator": 1,
        "FramerateConversionAlgorithm": "DUPLICATE_DROP",
    }
    aac = {"CodecProfile": "LC", "CodingMode": "CODING_MODE_2_0", "SampleRate": 48_000, "Bitrate": 96_000,
           "RateControlMode": "CBR"}  # fmt: skip
    return {
        "TimecodeConfig": {"Source": "ZEROBASED"},
        "Inputs": [_silence(PREROLL_MS), source, _silence(POSTROLL_MS)],
        "OutputGroups": [
            {
                "Name": "CMAF",
                "CustomName": "inference_proxy",
                "OutputGroupSettings": {
                    "Type": "CMAF_GROUP_SETTINGS",
                    "CmafGroupSettings": {
                        "Destination": destination,
                        "SegmentControl": "SEGMENTED_FILES",
                        "SegmentLength": SEGMENT_MS // 1000,
                        "SegmentLengthControl": "EXACT",
                        "FragmentLength": SEGMENT_MS // 1000,
                        "WriteHlsManifest": "ENABLED",
                        "WriteDashManifest": "DISABLED",
                        "ManifestDurationFormat": "FLOATING_POINT",
                    },
                },
                "Outputs": [
                    {
                        "NameModifier": PROXY_VIDEO,
                        "ContainerSettings": {"Container": "CMFC"},
                        "VideoDescription": {
                            "Width": 1280,
                            "Height": 720,
                            "ScalingBehavior": "DEFAULT",
                            "CodecSettings": {"Codec": "H_264", "H264Settings": h264},
                        },
                    },
                    {
                        "NameModifier": PROXY_AUDIO,
                        "ContainerSettings": {"Container": "CMFC"},
                        "AudioDescriptions": [
                            {
                                "AudioSourceName": "Audio Selector 1",
                                "CodecSettings": {"Codec": "AAC", "AacSettings": aac},
                            }
                        ],
                    },
                ],
            }
        ],
    }


def _silence(duration_ms: int) -> dict[str, Any]:
    """A black, silent generated input of ``duration_ms``."""
    generator = {"Width": 1280, "Height": 720, "FramerateNumerator": PROXY_FPS, "FramerateDenominator": 1,
                 "Channels": 2, "SampleRate": 48_000, "Duration": duration_ms}  # fmt: skip
    return {
        "VideoGenerator": generator,
        "TimecodeSource": "ZEROBASED",
        "AudioSelectors": {"Audio Selector 1": {"DefaultSelection": "DEFAULT"}},
    }


@dataclass(frozen=True, slots=True)
class Chunk:
    """Segments ``first`` to ``last`` (numbers from 1) go to one feed, which is authoritative for words that start
    in ``[start_ms, end_ms)``."""

    index: int
    first: int
    last: int
    start_ms: int
    end_ms: int


def plan_chunks(
    segments: int, *, chunk_segments: int = CHUNK_SEGMENTS, overlap_segments: int = OVERLAP_SEGMENTS
) -> list[Chunk]:
    """Chunks covering ``segments`` 1 s proxy segments, each with ``overlap_segments`` of context on both sides."""
    if segments < 1:
        raise ValueError("the proxy has no segments")
    chunks = []
    for index, start in enumerate(range(0, segments, chunk_segments)):
        end = min(start + chunk_segments, segments)
        chunks.append(
            Chunk(
                index=index,
                first=max(0, start - overlap_segments) + 1,
                last=min(segments, end + overlap_segments),
                start_ms=start * SEGMENT_MS,
                end_ms=end * SEGMENT_MS if end < segments else (segments + 1) * SEGMENT_MS,
            )
        )
    return chunks


def windows(start_ms: int, end_ms: int, *, size_ms: int = WINDOW_MS) -> list[tuple[int, int]]:
    """The GetMetadata windows that cover ``[start_ms, end_ms)``."""
    return [(at, min(at + size_ms, end_ms)) for at in range(start_ms, end_ms, size_ms)]


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One roll-up ``<p>``: the lines on screen from ``begin_ms`` to ``end_ms``."""

    begin_ms: int
    end_ms: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Word:
    """A recognised word: shown from ``start_ms`` (when the transcriber emitted it) to ``end_ms``."""

    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class Caption:
    begin_ms: int
    end_ms: int
    lines: tuple[str, ...]


def snapshots(document: str, *, offset_ms: int) -> list[Snapshot]:
    """The ``<p>`` elements of a GetMetadata TTML document; its times are relative to the window, at ``offset_ms``."""
    if not document.strip():
        return []
    found = []
    # S314: the document is an Elemental Inference API response (expat resolves no external entities).
    for p in ET.fromstring(document).iter(f"{_TT}p"):  # noqa: S314
        lines = [""]
        for node in p.iter():
            if node.tag == f"{_TT}br":
                lines.append("")
            elif node is not p and node.text:
                lines[-1] += node.text
            if node.tag == f"{_TT}br" and node.tail:
                lines[-1] += node.tail
        text = tuple(" ".join(line.split()) for line in lines if line.strip())
        if text:
            found.append(
                Snapshot(offset_ms + _clock_ms(p.get("begin", "0")), offset_ms + _clock_ms(p.get("end", "0")), text)
            )
    return found


def words(shots: Iterable[Snapshot]) -> list[Word]:
    """The words a sequence of roll-up snapshots adds, each timed by the snapshot that first shows it.

    Each snapshot repeats what is still on screen; the words it shares with the end of the transcript so far are
    found by the longest overlap of normalised words (case and punctuation ignored, so a word that gains its full
    stop is matched, and takes the later form). Window edges repeat the clipped snapshot at the next window's start,
    which the overlap absorbs. A snapshot of one line after a pause starts afresh: the display cleared.
    """
    transcript: list[Word] = []
    last_end: int | None = None
    for shot in sorted(shots, key=lambda s: (s.begin_ms, s.end_ms)):
        tokens = " ".join(shot.lines).split()
        cleared = last_end is not None and shot.begin_ms - last_end >= PAUSE_MS and len(shot.lines) == 1
        shared = 0 if cleared else _overlap([w.text for w in transcript], tokens)
        for i, token in enumerate(tokens[:shared], start=len(transcript) - shared):
            transcript[i] = replace(transcript[i], text=token, end_ms=max(transcript[i].end_ms, shot.end_ms))
        transcript += [Word(shot.begin_ms, shot.end_ms, token) for token in tokens[shared:]]
        last_end = shot.end_ms
    return transcript


def _overlap(transcript: Sequence[str], tokens: Sequence[str], *, limit: int = 40) -> int:
    """The largest ``k`` such that the last ``k`` transcript words are the first ``k`` tokens (normalised)."""
    tail = [_normalized(t) for t in transcript[-limit:]]
    head = [_normalized(t) for t in tokens[:limit]]
    return next((k for k in range(min(len(tail), len(head)), 0, -1) if tail[-k:] == head[:k]), 0)


def _normalized(token: str) -> str:
    return _NORMALIZE.sub("", token.lower().replace("\u2019", "'"))


def captions(stream: Sequence[Word], *, offset_ms: int = 0) -> list[Caption]:
    """Pop-on captions of a word stream: at most two lines of :data:`LINE_CHARACTERS`, broken at sentence ends,
    at pauses of :data:`PAUSE_MS` and after :data:`MAX_CAPTION_MS`; times shifted by ``-offset_ms`` (words before
    the offset are dropped)."""
    groups: list[list[Word]] = []
    for word in (w for w in stream if w.start_ms >= offset_ms):
        if groups and not _breaks_before(groups[-1], word):
            groups[-1].append(word)
        else:
            groups.append([word])
    result = []
    for n, group in enumerate(groups):
        begin = group[0].start_ms
        following = groups[n + 1][0].start_ms if n + 1 < len(groups) else None
        last = group[-1]
        end = max(min(last.end_ms, last.start_ms + LINGER_MS), begin + MIN_CAPTION_MS)
        if following is not None:
            end = min(end, following)
        result.append(
            Caption(begin - offset_ms, max(end, begin + 1) - offset_ms, lines(" ".join(w.text for w in group)))
        )
    return result


def _breaks_before(group: Sequence[Word], word: Word) -> bool:
    previous = group[-1]
    return (
        word.start_ms - previous.start_ms >= PAUSE_MS
        or word.start_ms - group[0].start_ms >= MAX_CAPTION_MS
        or (bool(_SENTENCE_END.search(previous.text)) and len(" ".join(w.text for w in group)) >= LINE_CHARACTERS // 2)
        or not _fits(" ".join([*(w.text for w in group), word.text]))
    )


def _fits(text: str) -> bool:
    return all(len(line) <= LINE_CHARACTERS for line in lines(text)) and len(lines(text)) <= 2


def lines(text: str) -> tuple[str, ...]:
    """``text`` on one line, or on two lines as even as possible (a longer text is returned on more lines)."""
    if len(text) <= LINE_CHARACTERS:
        return (text,)
    words_ = text.split()
    splits = [(" ".join(words_[:i]), " ".join(words_[i:])) for i in range(1, len(words_))]
    fitting = [s for s in splits if max(len(s[0]), len(s[1])) <= LINE_CHARACTERS]
    if fitting:  # prefer a break after a clause, then the most even lines
        return min(fitting, key=lambda s: (not _CLAUSE_END.search(s[0]), abs(len(s[0]) - len(s[1]))))
    first, rest = max((s for s in splits if len(s[0]) <= LINE_CHARACTERS), key=lambda s: len(s[0]), default=(text, ""))
    return (first, *lines(rest)) if rest else (first,)


def ttml(entries: Iterable[Caption], *, language: str) -> str:
    """An IMSC1 Text Profile document of ``entries``: white on black, bottom region, centred."""
    body = "\n".join(
        f'      <p begin="{_timestamp(c.begin_ms)}" end="{_timestamp(c.end_ms)}" region="bottom">'
        f'<span tts:backgroundColor="black">{"<br/>".join(escape(line) for line in c.lines)}</span></p>'
        for c in entries
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<tt xmlns="http://www.w3.org/ns/ttml" xmlns:tts="http://www.w3.org/ns/ttml#styling"
    xmlns:ttp="http://www.w3.org/ns/ttml#parameter"
    ttp:profile="http://www.w3.org/ns/ttml/profile/imsc1/text" ttp:timeBase="media"
    ttp:cellResolution="32 15" xml:lang="{escape(language)}">
  <head>
    <styling>
      <style xml:id="caption" tts:fontFamily="proportionalSansSerif" tts:fontSize="100%" tts:color="white"
          tts:textAlign="center" tts:lineHeight="125%"/>
    </styling>
    <layout>
      <region xml:id="bottom" style="caption" tts:origin="10% 75%" tts:extent="80% 20%" tts:displayAlign="after"/>
    </layout>
  </head>
  <body>
    <div>
{body}
    </div>
  </body>
</tt>
"""


def _clock_ms(value: str) -> int:
    hours, minutes, seconds = value.split(":") if value.count(":") == 2 else ("0", "0", value.rstrip("s"))
    return round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)


def _timestamp(ms: int) -> str:
    hours, rest = divmod(max(ms, 0), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    return f"{hours:02d}:{minutes:02d}:{rest / 1000:06.3f}"
