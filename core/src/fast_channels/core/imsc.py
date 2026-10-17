"""IMSC1 documents of CMAF caption segments, re-cut on another track's segment boundaries.

MediaConvert writes each caption segment as one IMSC1 document whose ``<p>`` cues carry absolute ``begin`` and
``end`` times, clipped to the segment: a cue that crosses a boundary appears in both segments. :func:`recut` merges
those pieces back and clips them to new segments. Every document keeps MediaConvert's head (styling, regions) and
every cue its markup; only the times change.

Only the timing MediaConvert writes is accepted: ``begin`` and ``end`` on ``<p>``, in clock time (``00:01:02.345``)
or offset time (``62.345s``), on the media time base. Anything else (timed ``<div>`` or ``<span>``, ``dur``, frames,
ticks, another time base) raises ValueError, so an encode is refused rather than mistimed.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from itertools import pairwise
from typing import Final

_CUE: Final = re.compile(
    r"<(?P<tag>(?:[\w.-]+:)?p)(?=[\s/>])(?P<attributes>[^>]*?)(?P<rest>/>|>.*?</(?P=tag)\s*>)", re.DOTALL
)
_TIMING: Final = re.compile(r"""\s(begin|end)\s*=\s*(["'])(.*?)\2""")
_TIMED: Final = re.compile(r"""<[^!?][^>]*\s(?:begin|end|dur)\s*=""")
_UNTIMED: Final = re.compile(r"""\s(?:begin|end|dur)\s*=""")
_TIME_BASE: Final = re.compile(r"""\sttp:timeBase\s*=\s*(["'])(?!media\1)""")
_CONTAINER_END: Final = re.compile(r"</(?:[\w.-]+:)?div\s*>")
_CLOCK: Final = re.compile(r"(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_OFFSET: Final = re.compile(r"(\d+(?:\.\d+)?)(h|m|s|ms)")
_UNITS: Final = {"h": Fraction(3600), "m": Fraction(60), "s": Fraction(1), "ms": Fraction(1, 1000)}


def seconds(expression: str) -> Fraction:
    """A TTML clock-time or offset-time expression in seconds."""
    if clock := _CLOCK.fullmatch(expression):
        hours, minutes, rest = clock.groups()
        return int(hours) * 3600 + int(minutes) * 60 + Fraction(rest)
    if offset := _OFFSET.fullmatch(expression):
        value, unit = offset.groups()
        return Fraction(value) * _UNITS[unit]
    raise ValueError(f"unsupported TTML time expression {expression!r}")


def clock_time(t: Fraction) -> str:
    """``t`` seconds as a TTML clock time to the millisecond (``HH:MM:SS.mmm``)."""
    ms = round(t * 1000)
    minutes, ms = divmod(ms, 60_000)
    return f"{minutes // 60:02d}:{minutes % 60:02d}:{ms // 1000:02d}.{ms % 1000:03d}"


@dataclass(frozen=True, slots=True)
class Cue:
    """A ``<p>`` element active from ``begin`` to ``end`` (seconds on the track timeline)."""

    begin: Fraction
    end: Fraction
    opening: str
    """The start tag up to its closing ``>`` or ``/>``, without ``begin`` and ``end``."""
    rest: str
    """``/>``, or ``>``, the content and the end tag."""

    @property
    def markup(self) -> str:
        times = f' begin="{clock_time(self.begin)}" end="{clock_time(self.end)}"'
        return f"{self.opening}{times}{self.rest}"


@dataclass(frozen=True, slots=True)
class Document:
    """An IMSC1 document split around its cues: ``head`` + cues + ``tail``."""

    head: str
    tail: str
    cues: tuple[Cue, ...]

    def render(self, cues: Iterable[Cue]) -> str:
        """This document with ``cues`` instead of its own (cues shorter than a millisecond are dropped)."""
        kept = (c.markup for c in cues if round(c.end * 1000) > round(c.begin * 1000))
        return self.head + "".join(f"{markup}\n" for markup in kept) + self.tail


def parse(document: str) -> Document:
    """Split an IMSC1 document as MediaConvert writes it (see the module docstring)."""
    if _TIME_BASE.search(document):
        raise ValueError("only the media time base is supported")
    matches = list(_CUE.finditer(document))
    if matches:
        head, tail = document[: matches[0].start()], document[matches[-1].end() :]
        between = (document[a.end() : b.start()] for a, b in pairwise(matches))
        if any(text.strip() for text in between):
            raise ValueError("cues are not all in one container")
    elif container_end := list(_CONTAINER_END.finditer(document)):
        at = container_end[-1].start()
        head, tail = document[:at], document[at:]
    else:
        raise ValueError("the document has no <div> for cues")
    if _TIMED.search(head) or _TIMED.search(tail):
        raise ValueError("only <p> elements may be timed")
    return Document(head=head, tail=tail, cues=tuple(_cue(match) for match in matches))


def _cue(match: re.Match[str]) -> Cue:
    attributes, rest = match["attributes"], match["rest"]
    timing = _TIMING.findall(attributes)
    untimed = _TIMING.sub("", attributes)
    if sorted(name for name, _, _ in timing) != ["begin", "end"] or _UNTIMED.search(untimed) or _TIMED.search(rest):
        raise ValueError(f"a cue needs one begin and one end, and nothing in it may be timed: {match[0][:120]!r}")
    times = {name: seconds(value.strip()) for name, _, value in timing}
    return Cue(times["begin"], times["end"], opening=f"<{match['tag']}{untimed}", rest=rest)


def merged(cues: Iterable[Cue]) -> list[Cue]:
    """Cues in begin order, with the pieces of a cue split at segment boundaries joined (same markup, one ending
    where the next begins) and repeated pieces dropped."""
    out: list[Cue] = []
    last: dict[tuple[str, str], int] = {}
    for cue in sorted(cues, key=lambda c: (c.begin, c.end)):
        key = (cue.opening, cue.rest)
        if (i := last.get(key)) is not None and out[i].begin <= cue.begin <= out[i].end:
            out[i] = replace(out[i], end=max(out[i].end, cue.end))
            continue
        last[key] = len(out)
        out.append(cue)
    return out


def recut(documents: Sequence[str], boundaries: Sequence[Fraction]) -> list[str]:
    """One document per segment ``[boundaries[i], boundaries[i + 1])`` (seconds on the track timeline), holding the
    cues of ``documents`` that overlap it, clipped to it. The first document provides the head and tail."""
    if not documents:
        raise ValueError("nothing to re-cut")
    parsed = [parse(d) for d in documents]
    cues = merged(c for d in parsed for c in d.cues)
    return [
        parsed[0].render(
            replace(c, begin=max(c.begin, start), end=min(c.end, end)) for c in cues if c.begin < end and c.end > start
        )
        for start, end in pairwise(boundaries)
    ]
