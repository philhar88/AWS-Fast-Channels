"""Content understanding (M12): the item contract, the signal reducers, the model call and its checks, the proxy job
and the cost model."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from fast_channels.core.understanding import MOODS, Analysis, BrandSafety, Mood, Status, Understanding, item_key
from fast_channels.core.understanding.cost import MAX_FRAMES, estimate, per_hour, video_tokens
from fast_channels.core.understanding.prompt import (
    TOOL,
    answer,
    cut,
    description,
    genres,
    request,
    system_prompt,
    tool_spec,
)
from fast_channels.core.understanding.proxy import analysis_variant, proxy_job
from fast_channels.core.understanding.record import categories
from fast_channels.core.understanding.signals import (
    EXCERPT_MAX_CHARS,
    Sighting,
    Signals,
    excerpt,
    label_signals,
    moderation_labels,
    moderation_signals,
    segment_signals,
    transcript_signals,
)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def understanding(**fields: Any) -> Understanding:
    return Understanding.model_validate(
        {
            "content_id": "sintel",
            "asset_id": "sintel-20261107",
            "status": "complete",
            "analyses": ["transcript", "moderation"],
            "duration_ms": 888_000,
            "analyzed_at": NOW,
            **fields,
        }
    )


# --- the item --------------------------------------------------------------------------------------------------------


def test_the_item_holds_ct31_categories_lower_case_keywords_and_vocabulary_moods() -> None:
    item = understanding(cat=["641", "335"], keywords=["dragon", "quest"], moods=["epic", "sad"], brand_safety="medium")

    assert item.v == 1
    assert item.moods == (Mood.EPIC, Mood.SAD)
    assert item_key("sintel") == {"pk": "CONTENT#sintel", "sk": "UNDERSTANDING"}
    with pytest.raises(ValidationError, match="not Content Taxonomy 3\\.1 categories"):
        understanding(cat=["641", "nope"])
    with pytest.raises(ValidationError, match="not Content Taxonomy 3\\.1 categories"):
        understanding(cat=["1000"])  # a descriptive vector, not a category
    with pytest.raises(ValidationError, match="repeat"):
        understanding(cat=["641", "641"])
    with pytest.raises(ValidationError, match="lower case"):
        understanding(keywords=["Dragon"])
    with pytest.raises(ValidationError):
        understanding(moods=["gloomy"])
    with pytest.raises(ValidationError):
        understanding(cat=["641", "335", "325", "646", "652", "332"])  # more than five


def test_the_mood_vocabulary_is_fixed_and_ordered() -> None:
    """The recommender imports it (lane J): the words and their order are part of the contract."""
    assert MOODS == (
        "uplifting",
        "tense",
        "dark",
        "humorous",
        "romantic",
        "calm",
        "energetic",
        "sad",
        "suspenseful",
        "whimsical",
        "epic",
        "informative",
    )


def test_the_riskier_brand_safety_tier_wins() -> None:
    assert BrandSafety.LOW.worse(BrandSafety.HIGH) is BrandSafety.HIGH
    assert BrandSafety.FLOOR.worse(BrandSafety.MEDIUM) is BrandSafety.FLOOR
    assert BrandSafety.MEDIUM.worse(BrandSafety.MEDIUM) is BrandSafety.MEDIUM


def test_the_categories_are_the_taxonomys_categories_not_its_vectors() -> None:
    found = categories()

    assert len(found) == 704
    assert found["641"] == "Animation & Anime"
    assert not any(id_.isdigit() and int(id_) >= 1000 for id_ in found)


# --- the signals ---------------------------------------------------------------------------------------------------


def test_the_transcript_gives_the_language_the_speech_ratio_and_an_excerpt() -> None:
    document = {
        "results": {
            "language_code": "en-US",
            "transcripts": [{"transcript": "Hello there. Who goes there?"}],
            "items": [
                {"type": "pronunciation", "start_time": "1.0", "end_time": "1.5"},
                {"type": "pronunciation", "start_time": "1.9", "end_time": "2.4"},  # within a second: one stretch
                {"type": "punctuation"},
                {"type": "pronunciation", "start_time": "10.0", "end_time": "11.0"},
            ],
        }
    }

    found = transcript_signals(document, 100_000)

    assert found == {"language": "en", "speech_ratio": 0.024, "transcript": "Hello there. Who goes there?"}
    assert transcript_signals({}, 100_000) == {"language": None, "speech_ratio": 0.0, "transcript": ""}
    silent = {"results": {"language_code": "en-US", "transcripts": [{"transcript": ""}], "items": []}}
    assert transcript_signals(silent, 70_000)["language"] is None  # a tone is not English (live, 2026-11-07)


def test_a_long_transcript_is_sampled_at_its_start_middle_and_end() -> None:
    text = "".join(str(i % 10) for i in range(30_000))

    sampled = excerpt(text)

    assert len(sampled) <= EXCERPT_MAX_CHARS
    assert sampled.startswith(text[:100]) and sampled.endswith(text[-100:])
    assert sampled.count(" … ") == 2


def test_segments_give_the_shots_and_the_technical_cues() -> None:
    segments = [
        {"Type": "SHOT", "DurationMillis": 4000, "ShotSegment": {"Index": 0}},
        {"Type": "SHOT", "DurationMillis": 6000, "ShotSegment": {"Index": 1}},
        {"Type": "TECHNICAL_CUE", "TechnicalCueSegment": {"Type": "BlackFrames"}, "StartTimestampMillis": 100},
        {"Type": "TECHNICAL_CUE", "TechnicalCueSegment": {"Type": "EndCredits"}, "StartTimestampMillis": 800_000},
    ]

    assert segment_signals(segments) == {
        "shots": 2,
        "mean_shot_s": 5.0,
        "cues": {"BlackFrames": 1, "EndCredits": 1},
        "credits_at_s": 800.0,
    }
    assert segment_signals([]) == {"shots": None, "mean_shot_s": None, "cues": {}, "credits_at_s": None}


def test_labels_are_ranked_by_screen_time_above_the_confidence_floor() -> None:
    labels = [
        {"Label": {"Name": "Dragon", "Confidence": 95.0}, "DurationMillis": 30_000},
        {"Label": {"Name": "Dragon", "Confidence": 91.0}, "StartTimestampMillis": 0, "EndTimestampMillis": 10_000},
        {"Label": {"Name": "Snow", "Confidence": 88.0}, "DurationMillis": 50_000},
        {"Label": {"Name": "Cat", "Confidence": 60.0}, "DurationMillis": 90_000},  # below the floor
        {"Label": {"Name": "Purple", "Confidence": 90.0}, "DurationMillis": 0},  # one frame (live, 2026-11-07)
    ]

    ranked = label_signals(labels)

    assert [(label.name, label.seconds) for label in ranked] == [("Snow", 50.0), ("Dragon", 40.0)]
    assert len(label_signals(labels, top=1)) == 1


def test_moderation_counts_top_level_categories_seen_in_two_frames() -> None:
    def seen(at: int, name: str, parent: str, confidence: float = 90.0) -> dict[str, Any]:
        return {"Timestamp": at, "ModerationLabel": {"Name": name, "ParentName": parent, "Confidence": confidence}}

    labels = [
        seen(0, "Violence", ""),
        seen(0, "Weapons", "Violence"),
        seen(500, "Weapon Violence", "Weapons"),  # level 3, another frame: rolled up to Violence
        seen(0, "Alcohol", ""),  # one frame only
        seen(0, "Smoking", "Drugs & Tobacco", 70.0),  # below the confidence floor
        seen(500, "Smoking", "Drugs & Tobacco", 72.0),
        # One frame reports a label and its parent (as Rekognition does): still one sighting.
        seen(1000, "Explicit", "", 83.5),
        seen(1000, "Explicit Sexual Activity", "Explicit", 83.5),
    ]

    assert moderation_signals(labels) == {"Violence": 90.0}


def test_the_moderation_tier_needs_the_analysis() -> None:
    ran = (Analysis.MODERATION,)

    assert Signals(duration_ms=1, analyses=()).moderation_tier is None
    assert Signals(duration_ms=1, analyses=ran).moderation_tier is BrandSafety.LOW
    # Signals of an older collector carry the top-level categories only.
    assert Signals(duration_ms=1, analyses=ran, moderation={"Violence": 90}).moderation_tier is BrandSafety.MEDIUM
    assert Signals(duration_ms=1, analyses=ran, moderation={"Explicit": 99}).moderation_tier is BrandSafety.FLOOR
    assert Signals(duration_ms=1, analyses=ran, moderation={"Something New": 99}).moderation_tier is BrandSafety.MEDIUM


def frames(name: str, parent: str, top: str, *, count: int, confidence: float, start: int = 0) -> list[dict[str, Any]]:
    """Rekognition's entries for ``name`` seen in ``count`` frames half a second apart: each frame also reports the
    label's parent and its top-level category (as GetContentModeration does)."""
    entries = []
    for n in range(count):
        at = start + 500 * n
        for label, its_parent in {name: parent, parent: top, top: ""}.items():
            if label:
                entries.append(
                    {
                        "Timestamp": at,
                        "ModerationLabel": {"Name": label, "ParentName": its_parent, "Confidence": confidence},
                    }
                )
    return entries


def tier(entries: list[dict[str, Any]]) -> BrandSafety | None:
    signals = Signals(
        duration_ms=600_000,
        analyses=(Analysis.MODERATION,),
        moderation=moderation_signals(entries),
        moderation_labels=moderation_labels(entries),
    )
    return signals.moderation_tier


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        # The sample titles' patterns (live, 2026-11-07): each is medium.
        (frames("Blood & Gore", "Graphic Violence", "Violence", count=2, confidence=88.3), BrandSafety.MEDIUM),
        (frames("Weapon Violence", "Graphic Violence", "Violence", count=72, confidence=92.1), BrandSafety.MEDIUM),
        (
            frames("Emaciated Bodies", "Death and Emaciation", "Visually Disturbing", count=3, confidence=87.2),
            BrandSafety.MEDIUM,
        ),
        # Synthetic: sustained graphic content is high, sustained explicit content floor; briefly, one tier less.
        (frames("Blood & Gore", "Graphic Violence", "Violence", count=12, confidence=96.0), BrandSafety.HIGH),
        (frames("Blood & Gore", "Graphic Violence", "Violence", count=12, confidence=85.0), BrandSafety.MEDIUM),
        (frames("Corpses", "Death and Emaciation", "Visually Disturbing", count=10, confidence=91.0), BrandSafety.HIGH),
        (frames("Exposed Female Nipple", "Explicit Nudity", "Explicit", count=12, confidence=99.0), BrandSafety.FLOOR),
        (frames("Exposed Female Nipple", "Explicit Nudity", "Explicit", count=3, confidence=99.0), BrandSafety.HIGH),
        (frames("Nazi Party", "Hate Symbols", "", count=10, confidence=95.0), BrandSafety.FLOOR),
        (
            frames("Smoking", "Drugs & Tobacco Paraphernalia & Use", "Drugs & Tobacco", count=4, confidence=95.0),
            BrandSafety.MEDIUM,
        ),
        (frames("Drinking", "Alcohol Use", "Alcohol", count=20, confidence=99.0), BrandSafety.LOW),
        (frames("Explicit Sexual Activity", "", "Explicit", count=1, confidence=83.5), BrandSafety.LOW),  # one frame
    ],
)
def test_the_tier_is_the_most_specific_labels_and_high_needs_them_sustained(
    entries: list[dict[str, Any]], expected: BrandSafety
) -> None:
    assert tier(entries) is expected


def test_moderation_labels_count_frames_and_confident_frames() -> None:
    entries = frames("Blood & Gore", "Graphic Violence", "Violence", count=4, confidence=95.0)
    entries += frames("Blood & Gore", "Graphic Violence", "Violence", count=3, confidence=85.0, start=10_000)

    found = moderation_labels(entries)

    assert set(found) == {"Blood & Gore", "Graphic Violence", "Violence"}
    assert found["Blood & Gore"] == Sighting(category="Violence", frames=7, sustained=4, confidence=95.0)
    assert found["Violence"].category == "Violence"
    brief = Signals(duration_ms=1, analyses=(Analysis.MODERATION,), moderation_labels=found).brief()["moderation"]
    assert brief["Blood & Gore"] == {"category": "Violence", "frames": 7, "confidence": 95.0}


# --- the model call --------------------------------------------------------------------------------------------------


def signals() -> Signals:
    return Signals(
        duration_ms=888_000,
        analyses=(Analysis.TRANSCRIPT, Analysis.MODERATION),
        language="en",
        transcript="Sintel searches for the dragon she raised.",
        moderation={"Violence": 92.0},
    )


def test_the_request_sends_the_proxy_the_signals_and_a_cached_system_prompt() -> None:
    sent = request(
        model_id="arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc",
        video_uri="s3://bucket/proxies/sintel/a/proxy.mp4",
        bucket_owner="123456789012",
        signals=signals(),
        max_tokens=4000,
    )

    [video, text] = sent["messages"][0]["content"]
    assert video == {
        "video": {
            "format": "mp4",
            "source": {"s3Location": {"uri": "s3://bucket/proxies/sintel/a/proxy.mp4", "bucketOwner": "123456789012"}},
        }
    }
    assert "Sintel searches for the dragon" in text["text"] and '"moderation":{"Violence":92.0}' in text["text"]
    # No cache point: with one, Nova 2 Lite reported most of a title's video tokens as cache reads or writes.
    assert sent["system"] == [{"text": system_prompt()}]
    assert sent["toolConfig"]["toolChoice"] == {"tool": {"name": TOOL}}
    assert sent["inferenceConfig"] == {"maxTokens": 4000, "temperature": 0.0}
    schema = tool_spec()["toolSpec"]["inputSchema"]["json"]
    assert set(schema["required"]) == {"genres", "keywords", "moods", "brand_safety", "synopsis"}
    assert schema["properties"]["genres"]["items"]["enum"] == genres()
    assert "641" not in system_prompt()  # no example id, no taxonomy list: the model copied the list's order
    assert "- high: graphic and sustained" in system_prompt()
    assert "Corpses, when seen in 10 frames or more)" in system_prompt()  # as MODERATION_TIERS and SUSTAINED_FRAMES
    assert "- medium: action, fantasy or brief violence, weapons" in system_prompt()
    text_only = request(model_id="m", video_uri=None, bucket_owner=None, signals=signals(), max_tokens=10)
    assert [list(block) for block in text_only["messages"][0]["content"]] == [["text"]]


def test_the_genres_are_the_ctv_genres_with_a_category_id() -> None:
    vocabulary = genres()

    assert {"Action", "Animation", "Drama", "Fantasy", "Sci-Fi", "Thriller"} <= set(vocabulary)
    assert "News" not in vocabulary  # its id, 1020, is a descriptive vector, not a category
    assert vocabulary == sorted(vocabulary)


def test_the_answer_is_checked_and_the_riskier_tier_wins() -> None:
    raw = {
        "genres": ["animation", "Invented", "Action", "Adventure", "Fantasy"],
        "keywords": ["  Dragon ", "dragon", "Coming of Age"],
        "moods": ["Epic", "gloomy", "sad"],
        "brand_safety": "low",
        "synopsis": "A girl " + "searches far and wide " * 30,
    }

    checked = description(raw, moderation_tier=BrandSafety.HIGH)

    assert checked.cat == ("641", "325")  # three genres at most; Action and Adventure are both 325
    assert checked.keywords == ("dragon", "coming of age")
    assert checked.moods == (Mood.EPIC, Mood.SAD)
    assert checked.brand_safety is BrandSafety.HIGH  # moderation saw violence the model did not rate
    assert len(checked.synopsis) <= 300 and checked.synopsis.endswith("…")
    assert checked.dropped == ("genre:Invented", "mood:gloomy")
    assert description({"brand_safety": "floor"}, moderation_tier=BrandSafety.LOW).brand_safety is BrandSafety.FLOOR
    assert description({"brand_safety": "weird"}, moderation_tier=None).brand_safety is None


def test_the_answer_is_the_forced_tool_call() -> None:
    response = {
        "output": {"message": {"content": [{"text": "x"}, {"toolUse": {"name": TOOL, "input": {"genres": []}}}]}},
        "stopReason": "tool_use",
    }

    assert answer(response) == {"genres": []}
    with pytest.raises(ValueError, match="did not call"):
        answer({"output": {"message": {"content": [{"text": "no"}]}}, "stopReason": "end_turn"})


def test_a_synopsis_is_cut_at_a_word() -> None:
    assert cut("one  two\nthree") == "one two three"
    assert cut("alpha beta gamma", limit=12) == "alpha beta…"


# --- the proxy -------------------------------------------------------------------------------------------------------

MULTIVARIANT = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
hls_1080p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2"
hls_360p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"
hls_720p.m3u8
"""


def test_the_proxy_reads_the_smallest_variant_at_least_360_tall() -> None:
    assert analysis_variant(MULTIVARIANT) == "hls_360p.m3u8"
    small = MULTIVARIANT.replace("640x360", "426x240")
    assert analysis_variant(small) == "hls_720p.m3u8"
    only_small = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=426x240\nlow.m3u8\n"
    assert analysis_variant(only_small) == "low.m3u8"
    with pytest.raises(ValueError, match="no variant"):
        analysis_variant("#EXTM3U\n")


def test_the_proxy_job_is_one_small_mp4() -> None:
    job = proxy_job(
        input_uri="s3://media/assets/a/standard/hls_360p.m3u8",
        destination="s3://understanding/proxies/sintel/a/",
        metadata={"app": "fastm"},
    )

    settings = job["Settings"]
    [group] = settings["OutputGroups"]
    [output] = group["Outputs"]
    assert settings["Inputs"][0]["FileInput"] == "s3://media/assets/a/standard/hls_360p.m3u8"
    assert (
        group["OutputGroupSettings"]["FileGroupSettings"]["Destination"] == "s3://understanding/proxies/sintel/a/proxy"
    )
    assert output["ContainerSettings"]["Container"] == "MP4"
    assert (output["VideoDescription"]["Width"], output["VideoDescription"]["Height"]) == (640, 360)
    assert job["UserMetadata"] == {"app": "fastm"}


# --- cost ------------------------------------------------------------------------------------------------------------


def test_the_cost_per_content_hour() -> None:
    assert per_hour(tuple(Analysis)) == Decimal("18.89")  # an hour-long title (the short sample titles: 19.12)
    assert per_hour((Analysis.TRANSCRIPT, Analysis.SHOTS, Analysis.MODERATION)) == Decimal("9.89")
    assert per_hour(()) == Decimal("0.53")  # the proxy and the model call always run
    assert video_tokens(10_000) == 2_560  # 10 s, 10 frames of 256 tokens at 640 by 360 (measured)
    assert video_tokens(7_200_000) == MAX_FRAMES * 256
    assert estimate(0, ()) > 0


def test_a_partial_item_needs_no_description() -> None:
    item = understanding(status=Status.PARTIAL, analyses=["moderation"], brand_safety="high", moderation=["Violence"])

    assert (item.cat, item.moods, item.synopsis, item.model) == ((), (), "", None)
