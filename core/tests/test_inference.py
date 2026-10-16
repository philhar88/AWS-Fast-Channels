"""Elemental Inference for files: proxy settings, chunks, and captions from live roll-up TTML (wire captures of a
Tears of Steel run in ``data/inference``)."""

import json
import re
from itertools import pairwise
from pathlib import Path

import pytest

from fast_channels.core import inference as ei
from fast_channels.core.inference import Caption, Chunk, Snapshot, Word
from fast_channels.core.mp4 import iter_boxes, sample_entries, with_compatible_brand

DATA = Path(__file__).parent / "data"
WINDOWS = json.loads((DATA / "inference" / "tears-of-steel-windows.json").read_text())


def shots() -> list[Snapshot]:
    return [s for w in WINDOWS for s in ei.snapshots(w["ttml"], offset_ms=w["startPts"])]


def test_languages() -> None:
    assert [ei.inference_language(lang) for lang in ("en", "en-GB", "en-NZ", "pt-BR", "fr-CA", "ja")] == [
        "eng",
        "eng-gb",
        "eng",
        "por",
        "fra",
        None,
    ]


def test_the_proxy_is_one_second_cmaf_with_silence_around_the_programme() -> None:
    settings = ei.proxy_settings(source_uri="s3://in/movie.mov", destination="s3://in/work/a/c/inference/proxy")
    before, source, after = settings["Inputs"]
    assert source["FileInput"] == "s3://in/movie.mov"
    assert (before["VideoGenerator"]["Duration"], after["VideoGenerator"]["Duration"]) == (8_000, 10_000)
    assert all(g["VideoGenerator"]["Width"] == 1280 and g["VideoGenerator"]["Height"] == 720 for g in (before, after))
    (group,) = settings["OutputGroups"]
    cmaf = group["OutputGroupSettings"]["CmafGroupSettings"]
    assert (cmaf["SegmentLength"], cmaf["FragmentLength"], cmaf["SegmentLengthControl"]) == (1, 1, "EXACT")
    video, audio = group["Outputs"]
    h264 = video["VideoDescription"]["CodecSettings"]["H264Settings"]
    assert (video["VideoDescription"]["Width"], video["VideoDescription"]["Height"]) == (1280, 720)
    assert h264["GopSize"] == h264["FramerateNumerator"] == ei.PROXY_FPS and h264["GopClosedCadence"] == 1
    aac = audio["AudioDescriptions"][0]["CodecSettings"]["AacSettings"]
    assert (aac["SampleRate"], aac["CodingMode"]) == (48_000, "CODING_MODE_2_0")
    assert (video["NameModifier"], audio["NameModifier"]) == (ei.PROXY_VIDEO, ei.PROXY_AUDIO)


def test_chunks_overlap_and_own_their_seconds() -> None:
    assert ei.plan_chunks(169, chunk_segments=60) == [
        Chunk(0, 1, 75, 0, 60_000),
        Chunk(1, 46, 135, 60_000, 120_000),
        Chunk(2, 106, 169, 120_000, 170_000),
    ]
    [one] = ei.plan_chunks(42)
    assert (one.first, one.last, one.start_ms, one.end_ms) == (1, 42, 0, 43_000)
    with pytest.raises(ValueError, match="no segments"):
        ei.plan_chunks(0)
    assert ei.windows(10_000, 75_000) == [(10_000, 40_000), (40_000, 70_000), (70_000, 75_000)]


def test_snapshots_carry_their_window_offset_and_lines() -> None:
    snaps = ei.snapshots(WINDOWS[1]["ttml"], offset_ms=WINDOWS[1]["startPts"])
    first = snaps[0]
    assert first.begin_ms == 25_000 and first.lines == ("You're a jerk, Dom.", "Look, Celia,")
    assert all(s.begin_ms >= 25_000 for s in snaps)
    assert ei.snapshots("", offset_ms=0) == []


def test_roll_up_becomes_one_word_stream() -> None:
    stream = ei.words(shots())
    text = " ".join(w.text for w in stream)
    assert "You're a jerk, Dom. Look, Celia, we have to follow our passions. You have your robotics," in text
    assert text.count("You're a jerk") == 1  # the window edge repeats the clipped snapshot
    starts = [w.start_ms for w in stream]
    assert starts == sorted(starts)
    assert "Robots. Memory synced and locked." in text  # a cleared display starts afresh


def test_the_overlap_takes_the_later_punctuation() -> None:
    stream = ei.words(
        [
            Snapshot(0, 400, ("We're",)),
            Snapshot(400, 900, ("We're done",)),
            Snapshot(900, 2000, ("We're done.",)),
            Snapshot(5000, 5400, ("Robots.",)),
        ]
    )
    assert [(w.start_ms, w.text) for w in stream] == [(0, "We're"), (400, "done."), (5000, "Robots.")]
    assert stream[1].end_ms == 2000


def test_captions_are_two_lines_broken_at_sentences_and_pauses() -> None:
    entries = ei.captions(ei.words(shots()))
    assert all(len(c.lines) <= 2 and all(len(line) <= ei.LINE_CHARACTERS for line in c.lines) for c in entries)
    assert all(a.end_ms <= b.begin_ms for a, b in pairwise(entries))
    assert all(c.end_ms - c.begin_ms >= ei.MIN_CAPTION_MS or c.end_ms == n.begin_ms for c, n in pairwise(entries))
    by_text = {" ".join(c.lines): c for c in entries}
    claw = by_text["I'm having nightmares that I'm being chased by these giant robotic claws."]
    assert claw.lines == ("I'm having nightmares that I'm being", "chased by these giant robotic claws.")
    assert by_text["Robots. Memory synced and locked."].begin_ms == 50_120


def test_captions_move_to_the_source_timeline() -> None:
    stream = [Word(7_000, 7_500, "lost"), Word(9_000, 9_400, "Hello"), Word(9_400, 10_000, "there.")]
    [caption] = ei.captions(stream, offset_ms=8_000)
    assert caption == Caption(1_000, 2_000, ("Hello there.",))  # until the word left the screen, at 10 s


def test_lines_prefer_a_clause_end() -> None:
    assert ei.lines("Are you ready? Of course you're ready.") == ("Are you ready?", "Of course you're ready.")
    assert ei.lines("short") == ("short",)
    long = " ".join(["word"] * 30)
    assert all(len(line) <= ei.LINE_CHARACTERS for line in ei.lines(long))


def test_ttml_is_an_imsc1_text_document() -> None:
    document = ei.ttml([Caption(1_000, 2_500, ("Fish & chips", "<now>"))], language="en")
    assert 'ttp:profile="http://www.w3.org/ns/ttml/profile/imsc1/text"' in document and 'xml:lang="en"' in document
    assert re.search(r'<p begin="00:00:01.000" end="00:00:02.500" region="bottom">', document)
    assert "Fish &amp; chips<br/>&lt;now&gt;" in document
    assert [s.lines for s in ei.snapshots(document, offset_ms=0)] == [("Fish & chips", "<now>")]


def test_lmsg_marks_the_last_segment(cmaf_segment: bytes) -> None:
    marked = with_compatible_brand(cmaf_segment, "lmsg")
    styp = next(iter_boxes(marked))
    assert styp.type == "styp" and marked[styp.end - 4 : styp.end] == b"lmsg"
    assert len(marked) == len(cmaf_segment) + 4
    assert marked[styp.end :] == cmaf_segment[next(iter_boxes(cmaf_segment)).end :]  # nothing after styp moves
    assert with_compatible_brand(marked, "lmsg") == marked
    with pytest.raises(ValueError, match="styp"):
        with_compatible_brand(b"\x00\x00\x00\x08moof", "lmsg")


@pytest.fixture
def cmaf_segment() -> bytes:
    styp = b"\x00\x00\x00\x18styp" + b"msdh\x00\x00\x00\x00msdhmsix"
    moof = b"\x00\x00\x00\x08moof"
    return styp + moof


def test_sample_entries_of_cmaf_tracks() -> None:
    init = next(DATA.glob("**/*init*.cmf[va]"), None)
    if init is None:
        pytest.skip("no init segment in the test data")
    assert len(sample_entries(init.read_bytes())) == 1
