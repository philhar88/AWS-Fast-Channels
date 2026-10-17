"""Re-cutting IMSC1 caption documents, on two real MediaConvert segments of Tears of Steel: the cue "We have main
engine start 4321." runs from 4.669 s to 8.909 s, so MediaConvert split it at the 6.006 s segment boundary."""

import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from fast_channels.core import imsc, mp4

CAPTIONS = Path(__file__).parent / "data/captions"
DOCUMENTS = [mp4.read_fragment((CAPTIONS / f"tos-cc-en_00000000{n}.cmft").read_bytes()).sample.decode() for n in (1, 2)]
TEXT = '<span tts:backgroundColor="black">We have main engine start 4321.</span>'


def times(document: str) -> list[tuple[str, str]]:
    return [(imsc.clock_time(c.begin), imsc.clock_time(c.end)) for c in imsc.parse(document).cues]


def test_mediaconvert_documents_split_a_cue_at_the_segment_boundary() -> None:
    assert times(DOCUMENTS[0]) == [("00:00:04.669", "00:00:06.006")]
    assert times(DOCUMENTS[1]) == [("00:00:06.006", "00:00:08.909")]
    [cue] = imsc.merged(c for d in DOCUMENTS for c in imsc.parse(d).cues)
    assert (cue.begin, cue.end) == (Fraction("4.669"), Fraction("8.909"))
    assert cue.markup == f'<p region="bottom" begin="00:00:04.669" end="00:00:08.909">{TEXT}</p>'


def test_recut_clips_the_cues_to_the_new_segments() -> None:
    boundaries = [Fraction(0), Fraction("4.004"), Fraction("6.006"), Fraction("8.008"), Fraction("12.012")]

    documents = imsc.recut(DOCUMENTS, boundaries)

    assert [times(d) for d in documents] == [
        [],
        [("00:00:04.669", "00:00:06.006")],
        [("00:00:06.006", "00:00:08.008")],
        [("00:00:08.008", "00:00:08.909")],
    ]
    for document in documents:
        root = ET.fromstring(document.encode())
        assert root.tag == "{http://www.w3.org/ns/ttml}tt"
        assert document.startswith(DOCUMENTS[0][: DOCUMENTS[0].index("<body>")])  # MediaConvert's head, unchanged
    assert TEXT in documents[1] and 'region="bottom"' in documents[1]


def test_an_empty_document_takes_cues_inside_its_div() -> None:
    [empty] = imsc.recut(DOCUMENTS, [Fraction(0), Fraction(1)])
    [moved] = imsc.recut([empty], [Fraction(0), Fraction(1)])
    assert times(moved) == [] and moved == empty
    document = imsc.parse(empty)
    filled = document.render(imsc.parse(DOCUMENTS[0]).cues)
    assert times(filled) == [("00:00:04.669", "00:00:06.006")]
    assert filled.endswith("</p>\n</div></body></tt>\n")


def test_cues_shorter_than_a_millisecond_are_dropped() -> None:
    [document] = imsc.recut(DOCUMENTS, [Fraction("8.9088"), Fraction(10)])
    assert times(document) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (('begin="00:00:04.669"', 'begin="00:00:04:20"'), "unsupported TTML time"),
        ((' end="00:00:06.006"', ' dur="1.337s"'), "one begin and one end"),
        (("<span ", '<span begin="1s" '), "nothing in it may be timed"),
        (("<div>", '<div begin="10s">'), "only <p> elements may be timed"),
        (('xml:lang="en"', 'xml:lang="en" ttp:timeBase="smpte"'), "media time base"),
    ],
)
def test_timing_mediaconvert_does_not_write_is_refused(change: tuple[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        imsc.parse(DOCUMENTS[0].replace(*change))


def test_time_expressions() -> None:
    assert imsc.seconds("01:02:03.5") == 3723.5
    assert imsc.seconds("1500ms") == 1.5
    assert imsc.seconds("0.25h") == 900
    assert imsc.clock_time(Fraction("3723.5006")) == "01:02:03.501"
