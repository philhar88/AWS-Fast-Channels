from typing import Any

import pytest
from pydantic import ValidationError

from fast_channels.core.captions import (
    CaptionFile,
    CaptionFormat,
    CaptionMode,
    CaptionOrigin,
    CaptionPlan,
    CaptionPolicy,
    CaptionSource,
    CaptionsRefusedError,
    CaptionsRequest,
    CaptionTrack,
    Carriage,
    SourceCaptions,
    after_generation,
    after_scan,
    generation_failed,
    iso_639_1,
    plan_captions,
    scc_offset_ms,
)
from fast_channels.core.timeline import FrameRate

EN = CaptionTrack(language="en", label="English")
ES = CaptionTrack(language="es", label="Spanish")
TRACKS = (EN, ES)
SOURCE = "s3://ingest/films/movie.mov"
PLAIN = SourceCaptions(audio_language="en")
GENERATE = CaptionPolicy(can_generate=True)
NO_ENGINE = CaptionPolicy()
REQUIRED = CaptionPolicy(required=True, can_generate=True)


def request(*files: CaptionFile, **fields: Any) -> CaptionsRequest:
    return CaptionsRequest(files=list(files), **fields)


def origins(plan: CaptionPlan) -> list[str]:
    return [s.origin.value for s in plan.sources]


@pytest.mark.parametrize(
    ("uri", "fmt"),
    [
        ("movie.en.SCC", CaptionFormat.SCC),
        ("s3://b/movie.srt", CaptionFormat.SRT),
        ("https://cms/x.vtt?sig=1", CaptionFormat.WEBVTT),
        ("movie.dfxp", CaptionFormat.TTML),
        ("movie.xml", CaptionFormat.TTML),
        ("movie.stl", CaptionFormat.STL),
        ("movie.sami", CaptionFormat.SMI),
    ],
)
def test_formats_follow_the_extension(uri: str, fmt: CaptionFormat) -> None:
    assert CaptionFormat.of(uri) is fmt
    assert CaptionFile(uri=uri, language="en").resolved_format is fmt


def test_file_rules() -> None:
    with pytest.raises(ValidationError, match="unknown caption file extension"):
        CaptionFile(uri="movie.txt", language="en")
    with pytest.raises(ValidationError, match="SCC files only"):
        CaptionFile(uri="movie.srt", language="en", frame_rate="29.97")
    with pytest.raises(ValidationError, match=r"only from \.xml"):
        CaptionFile(uri="movie.ttml", language="en", format=CaptionFormat.IMSC)
    with pytest.raises(ValidationError):
        CaptionFile(uri="movie.srt", language="english")
    with pytest.raises(ValidationError, match=r"more than one caption file for \['en'\]"):
        request(CaptionFile(uri="a.srt", language="en"), CaptionFile(uri="b.vtt", language="en-GB"))


def test_relative_files_resolve_against_the_source_folder() -> None:
    relative = CaptionFile(uri="movie.en.scc", language="en")
    assert relative.absolute(SOURCE).uri == "s3://ingest/films/movie.en.scc"
    absolute = CaptionFile(uri="https://cms/movie.vtt", language="en")
    assert absolute.absolute(SOURCE) is absolute


def test_selectors_read_each_origin() -> None:
    scc = CaptionFile(uri="s3://b/m.scc", language="en", offset_ms=-3_600_000, frame_rate="29.97")
    assert CaptionSource.provided(scc, language="en").selector() == {
        "SourceSettings": {
            "SourceType": "SCC",
            "FileSourceSettings": {
                "SourceFile": "s3://b/m.scc",
                "TimeDelta": -3_600_000,
                "TimeDeltaUnits": "MILLISECONDS",
                "Framerate": {"FramerateNumerator": 30000, "FramerateDenominator": 1001},
            },
        }
    }
    srt = CaptionSource.provided(CaptionFile(uri="s3://b/m.srt", language="en"), language="en")
    assert srt.selector() == {
        "SourceSettings": {"SourceType": "SRT", "FileSourceSettings": {"SourceFile": "s3://b/m.srt"}}
    }
    embedded = CaptionSource(language="en", origin=CaptionOrigin.EMBEDDED, carriage=Carriage.EMBEDDED)
    assert embedded.selector()["SourceSettings"] == {
        "SourceType": "EMBEDDED",
        "EmbeddedSourceSettings": {"Source608ChannelNumber": 1},
    }
    ancillary = embedded.model_copy(update={"carriage": Carriage.ANCILLARY})
    assert ancillary.selector()["SourceSettings"]["SourceType"] == "ANCILLARY"
    assert CaptionSource.none("en").selector() == {"SourceSettings": {"SourceType": "NULL_SOURCE"}}


def test_sources_are_well_formed() -> None:
    with pytest.raises(ValidationError, match="need a file"):
        CaptionSource(language="en", origin=CaptionOrigin.GENERATED, engine="elemental-inference")
    with pytest.raises(ValidationError, match="carriage"):
        CaptionSource(language="en", origin=CaptionOrigin.EMBEDDED)


def test_stills_and_caption_less_ladders_have_empty_tracks() -> None:
    assert plan_captions((), request(), PLAIN, REQUIRED) == CaptionPlan()
    still = plan_captions(TRACKS, request(), None, REQUIRED)
    assert origins(still) == ["none", "none"] and still.awaiting is None
    assert still.selectors(TRACKS) == {
        "Captions en": {"SourceSettings": {"SourceType": "NULL_SOURCE"}},
        "Captions es": {"SourceSettings": {"SourceType": "NULL_SOURCE"}},
    }


def test_provided_files_come_first() -> None:
    plan = plan_captions(
        TRACKS, request(CaptionFile(uri="s3://b/m.vtt", language="en-US")), SourceCaptions(c608_language="en"), GENERATE
    )
    assert origins(plan) == ["provided", "none"] and plan.awaiting is None
    assert plan.sources[0].language == "en"  # the ladder's language, whatever region the file declared
    assert plan.spoken_language == "en"


def test_a_c608_track_in_the_spoken_language_is_used() -> None:
    plan = plan_captions(TRACKS, request(), SourceCaptions(audio_language="en", c608_language="und"), GENERATE)
    assert plan.sources[0].carriage is Carriage.ANCILLARY and plan.awaiting is None
    spanish = plan_captions(TRACKS, request(), SourceCaptions(audio_language="en", c608_language="es"), GENERATE)
    assert spanish.awaiting == "scan"


def test_otherwise_the_spoken_language_awaits_a_scan_then_generation() -> None:
    plan = plan_captions(TRACKS, request(), PLAIN, GENERATE)
    assert plan.awaiting == "scan"
    with pytest.raises(ValueError, match="still await their scan"):
        plan.selectors(TRACKS)

    embedded = after_scan(plan, 12, GENERATE)
    assert embedded.sources[0].carriage is Carriage.EMBEDDED and embedded.awaiting is None
    assert embedded.notes == ("captions embedded in the video (12 cues found)",)

    waiting = after_scan(plan, 0, GENERATE)
    assert waiting.awaiting == "generation"
    done = after_generation(
        waiting,
        uri="s3://i/captions/a/en.vtt",
        format=CaptionFormat.WEBVTT,
        engine="elemental-inference",
        policy=GENERATE,
    )
    assert done.awaiting is None and done.sources[0].origin is CaptionOrigin.GENERATED
    assert done.selectors(TRACKS)["Captions en"]["SourceSettings"]["SourceType"] == "WEBVTT"
    assert [p.model_dump(exclude_none=True) for p in done.provenance()] == [
        {"language": "en", "origin": "generated", "uri": "s3://i/captions/a/en.vtt", "engine": "elemental-inference"},
        {"language": "es", "origin": "none"},
    ]

    failed = generation_failed(waiting, "States.Timeout: 6 hours", GENERATE)
    assert origins(failed) == ["none", "none"] and failed.notes[-1].startswith("caption generation failed")
    with pytest.raises(CaptionsRefusedError, match=r"States\.Timeout"):
        generation_failed(waiting, "States.Timeout: 6 hours", REQUIRED)
    with pytest.raises(ValueError, match="do not await a scan"):
        after_scan(done, 1, GENERATE)


def test_the_spoken_language_defaults_to_the_audio_then_the_first_track() -> None:
    assert plan_captions(TRACKS, request(), SourceCaptions(audio_language="es"), GENERATE).spoken == CaptionSource.none(
        "es"
    )
    assert plan_captions(TRACKS, request(), SourceCaptions(), GENERATE).spoken_language == "en"
    told = plan_captions(TRACKS, request(spoken_language="es"), PLAIN, GENERATE)
    assert told.spoken_language == "es" and told.awaiting == "scan"


def test_generate_mode_skips_the_scan() -> None:
    plan = plan_captions(TRACKS, request(mode=CaptionMode.GENERATE), SourceCaptions(c608_language="en"), GENERATE)
    assert plan.awaiting == "generation"


def test_without_a_captioner_nothing_is_generated() -> None:
    scanned = after_scan(plan_captions(TRACKS, request(), PLAIN, NO_ENGINE), 0, NO_ENGINE)
    assert origins(scanned) == ["none", "none"] and scanned.notes == ("no captioner",)
    generate = plan_captions(TRACKS, request(mode=CaptionMode.GENERATE), PLAIN, NO_ENGINE)
    assert generate.awaiting is None and origins(generate) == ["none", "none"]


def test_required_captions() -> None:
    required_without_engine = CaptionPolicy(required=True)
    with pytest.raises(CaptionsRefusedError, match="en track would be empty"):
        after_scan(plan_captions(TRACKS, request(), PLAIN, required_without_engine), 0, required_without_engine)
    with pytest.raises(CaptionsRefusedError, match="captions are off"):
        plan_captions(TRACKS, request(mode=CaptionMode.OFF), PLAIN, REQUIRED)
    with pytest.raises(CaptionsRefusedError, match="no caption track for the spoken language fr"):
        plan_captions(TRACKS, request(spoken_language="fr"), PLAIN, REQUIRED)
    # Other languages are best effort: an empty es track is fine.
    provided = plan_captions(TRACKS, request(CaptionFile(uri="s3://b/m.srt", language="en")), PLAIN, REQUIRED)
    assert origins(provided) == ["provided", "none"]


def test_off_mode_writes_empty_tracks() -> None:
    plan = plan_captions(
        TRACKS, request(CaptionFile(uri="s3://b/m.srt", language="en"), mode=CaptionMode.OFF), PLAIN, GENERATE
    )
    assert origins(plan) == ["none", "none"] and plan.awaiting is None


@pytest.mark.parametrize(
    ("scc", "start_ms", "offset"),
    [
        ("Scenarist_SCC V1.0\n\n00:00:01;02\t9420 9420\n", 0, 0),
        ("Scenarist_SCC V1.0\n\n01:00:00;12\t9420\n", 0, -3_600_000),
        ("Scenarist_SCC V1.0\n\n00:59:58:00\t9420\n", 0, 0),
        ("Scenarist_SCC V1.0\n\n10:00:05;00\t9420\n", 36_000_000, -36_000_000),
        ("Scenarist_SCC V1.0\n\n00:00:05;00\t9420\n", 3_600_000, 0),
        ("not an scc file", 0, 0),
    ],
)
def test_scc_offsets_line_up_with_the_zero_based_source(scc: str, start_ms: int, offset: int) -> None:
    assert scc_offset_ms(scc, start_timecode_ms=start_ms) == offset


def test_iso_639_1() -> None:
    assert [iso_639_1(code) for code in ("eng", "FRE", "deu", "en", "und", None, "")] == [
        "en",
        "fr",
        "de",
        "en",
        None,
        None,
        None,
    ]


def test_frame_rates_parse_like_ladders() -> None:
    assert CaptionFile(uri="m.scc", language="en", frame_rate="23.976").frame_rate == FrameRate.parse("23.976")
