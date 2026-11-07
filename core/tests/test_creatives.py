"""The creatives module (M5): registry states, ladder conformance, and the mezzanine of a VAST registration."""

import re

import pytest
from pydantic import ValidationError

from fast_channels.core.creatives import names
from fast_channels.core.creatives.conformance import check, frame_ms, slot_ms
from fast_channels.core.creatives.registry import (
    DECIDED,
    TRANSITIONS,
    Action,
    Decision,
    ReadyCreatives,
    RegisteredCreative,
    Registration,
    State,
    TransitionError,
    check_transition,
)
from fast_channels.core.creatives.vast import VastError, mezzanine, progressive_files
from fast_channels.core.ladder import Ladder
from fast_channels.core.manifests import AUDIO_CODEC

LADDER = Ladder.builtin("standard")
CDN = "https://d111111abcdef8.cloudfront.net"


def creative(crid: str, adomain: str = "acme-outdoor.example") -> RegisteredCreative:
    return RegisteredCreative(
        crid=crid,
        creative_id=f"{crid}-standard_843ed1d05a-20d4b4",
        hls_uri=f"{CDN}/ads/standard_843ed1d05a/{crid}/main.m3u8",
        duration_ms=30030,
        width=1920,
        height=1080,
        advertiser="Acme Outdoor",
        adomain=adomain,
    )


# --- names and states ------------------------------------------------------------------------------------------------


def test_a_creatives_id_is_stable_and_never_a_demo_creatives() -> None:
    crid = names.crid_for("acme-outdoor.example", "https://ads.example/spot.mp4")
    assert crid == names.crid_for("acme-outdoor.example", "https://ads.example/spot.mp4")
    assert crid != names.crid_for("other.example", "https://ads.example/spot.mp4")
    assert re.fullmatch(names.CRID_PATTERN, crid)
    assert names.source_key(crid) == f"incoming/{crid}/source.mp4"
    assert names.quarantine_bucket("fastm", "123456789012", "us-east-1") == "fastm-creatives-123456789012-us-east-1"


def test_every_state_has_its_transitions_and_a_creative_starts_received() -> None:
    assert set(TRANSITIONS) == {None, *State}
    check_transition(None, State.RECEIVED)
    check_transition(State.READY, State.SUSPENDED)
    check_transition(State.FAILED, State.RECEIVED)  # a failed creative is registered again
    for current, wanted in ((None, State.READY), (State.REJECTED, State.RECEIVED), (State.REVIEW, State.READY)):
        with pytest.raises(TransitionError):
            check_transition(current, wanted)


def test_each_decision_is_an_allowed_transition() -> None:
    assert set(DECIDED) == set(Action) - {Action.REGISTER}
    for current, wanted in DECIDED.values():
        check_transition(current, wanted)


def test_registrations_and_decisions_are_validated() -> None:
    crid = names.crid_for("acme-outdoor.example", "s3://bucket/incoming/x/source.mp4")
    Registration(crid=crid, advertiser="Acme", adomain="acme-outdoor.example", source="s3://b/incoming/x/source.mp4")
    with pytest.raises(ValidationError):
        Registration(crid="direct1", advertiser="Acme", adomain="acme-outdoor.example", source="https://x.example/a")
    with pytest.raises(ValidationError):
        Registration(crid=crid, advertiser="Acme", adomain="acme-outdoor.example", source="file:///spot.mp4")
    assert Decision.model_validate({"action": "approve", "crid": crid, "by": "ops"}).action is Action.APPROVE
    with pytest.raises(ValidationError):
        Decision.model_validate({"action": "register", "crid": crid, "by": "ops"})


def test_ready_creatives_by_brand_keep_their_order() -> None:
    ready = ReadyCreatives(
        ladder_version=LADDER.version,
        creatives=(creative("cr-000000000001"), creative("cr-000000000002", "b.example"), creative("cr-000000000003")),
    )
    assert ready.by_brand() == {
        "acme-outdoor.example": ("cr-000000000001", "cr-000000000003"),
        "b.example": ("cr-000000000002",),
    }


# --- ladder conformance ----------------------------------------------------------------------------------------------


def multivariant(ladder: Ladder = LADDER, *, drop: int | None = None, **overrides: tuple[int, int] | int) -> str:
    """An encode's multivariant playlist for ``ladder``: rendition ``n`` gets a ``resolution`` or ``bandwidth``
    override with keys ``resolution<n>`` / ``bandwidth<n>``."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for n, rendition in enumerate(ladder.video, start=1):
        if n == drop:
            continue
        peak, average = ladder.nominal_bandwidth(rendition)
        width, height = overrides.get(f"resolution{n}", (rendition.width, rendition.height))  # type: ignore[misc]
        bandwidth = overrides.get(f"bandwidth{n}", peak)
        lines += [
            f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},AVERAGE-BANDWIDTH={average},"
            f'CODECS="avc1.64001f,{AUDIO_CODEC}",RESOLUTION={width}x{height},FRAME-RATE=29.970',
            f"main_{n}.m3u8",
        ]
    return "\n".join(lines) + "\n"


def media(seconds: float, segment: float = 2.002) -> str:
    """A media playlist of ``seconds`` cut into ``segment``-second segments (the last one shorter)."""
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:3", "#EXT-X-PLAYLIST-TYPE:VOD"]
    left, n = seconds, 0
    while left > 1e-9:
        length = min(segment, left)
        lines += [f"#EXTINF:{length:.3f},", f"seg_{n}.ts"]
        left, n = left - length, n + 1
    return "\n".join([*lines, "#EXT-X-ENDLIST"]) + "\n"


def playlists(seconds: float = 30.03, ladder: Ladder = LADDER) -> dict[str, str]:
    return {f"main_{n}.m3u8": media(seconds) for n in range(1, len(ladder.video) + 1)}


def test_the_pipelines_encode_of_a_30_s_ad_conforms() -> None:
    result = check(multivariant(), playlists(), LADDER)
    assert result.conformant, result.problems
    assert (result.variants, result.duration_ms, result.slots) == (len(LADDER.video), 30030, 1)
    assert slot_ms(LADDER) == 30030 and round(frame_ms(LADDER), 1) == 33.4


def test_two_slots_conform_and_one_frame_off_is_tolerated() -> None:
    assert check(multivariant(), playlists(60.06), LADDER).slots == 2
    assert check(multivariant(), playlists(30.03 + 0.03), LADDER).conformant
    assert check(multivariant(), playlists(29.997), LADDER).conformant  # 899 frames: one short


@pytest.mark.parametrize(
    ("main", "seconds", "problem"),
    [
        (multivariant(drop=1), 30.03, "variants, the ladder has"),
        (multivariant(resolution1=(960, 540)), 30.03, "variant 1 is avc/960x540"),
        (multivariant(bandwidth1=2_100_000_000), 30.03, "variant 1 advertises 2100000000 bit/s"),
        (multivariant(), 29.9, "is not a whole number of 30030 ms ad slots"),
        (multivariant(), 15.015, "is not a whole number of 30030 ms ad slots"),
    ],
)
def test_an_off_ladder_or_off_grid_creative_does_not_conform(main: str, seconds: float, problem: str) -> None:
    result = check(main, playlists(seconds), LADDER)
    assert not result.conformant
    assert any(problem in p for p in result.problems), result.problems


def test_variants_that_disagree_or_are_missing_do_not_conform() -> None:
    uneven = playlists() | {"main_1.m3u8": media(60.06)}
    assert any("not one duration" in p for p in check(multivariant(), uneven, LADDER).problems)
    missing = {k: v for k, v in playlists().items() if k != "main_2.m3u8"}
    assert any("main_2.m3u8 is missing" in p for p in check(multivariant(), missing, LADDER).problems)
    assert check("#EXTM3U\n", {}, LADDER).problems


# --- the mezzanine of a VAST registration ----------------------------------------------------------------------------

VAST = """<VAST version="4.2" xmlns="http://www.iab.com/VAST"><Ad id="a1"><InLine><AdSystem>x</AdSystem>
<AdTitle>spot</AdTitle><Creatives><Creative id="c1"><Linear><Duration>00:00:30.030</Duration><MediaFiles>
<MediaFile delivery="progressive" type="video/mp4" width="1280" height="720" bitrate="3000">
<![CDATA[https://ads.example/720.mp4]]></MediaFile>
<MediaFile delivery="progressive" type="video/mp4" width="1920" height="1080" bitrate="5000">
<![CDATA[https://ads.example/1080.mp4]]></MediaFile>
<MediaFile delivery="progressive" type="video/mp4" width="3840" height="2160" bitrate="20000">
<![CDATA[https://ads.example/2160.mp4]]></MediaFile>
<MediaFile delivery="streaming" type="application/x-mpegURL" width="1920" height="1080">
<![CDATA[https://ads.example/main.m3u8]]></MediaFile>
<MediaFile delivery="progressive" type="video/webm" width="1920" height="1080">
<![CDATA[https://ads.example/1080.webm]]></MediaFile>
</MediaFiles></Linear></Creative></Creatives></InLine></Ad></VAST>"""


def test_the_mezzanine_is_the_largest_progressive_mp4_up_to_1080_lines() -> None:
    assert mezzanine(VAST).url == "https://ads.example/1080.mp4"
    assert [f.height for f in progressive_files(VAST)] == [1080, 720]
    no_namespace = VAST.replace(' xmlns="http://www.iab.com/VAST"', "").replace('version="4.2"', 'version="3.0"')
    assert mezzanine(no_namespace).url == "https://ads.example/1080.mp4"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("<VAST><Ad><Wrapper><VASTAdTagURI>https://x.example</VASTAdTagURI></Wrapper></Ad></VAST>", "Wrapper"),
        ('<!DOCTYPE VAST [<!ENTITY a "b">]><VAST/>', "DOCTYPE"),
        ("<html/>", "not a VAST document"),
        ("<VAST", "not XML"),
        ("<VAST/>", "no progressive https MP4"),
        ("<VAST>" + " " * 1_000_001 + "</VAST>", "over 1000000 bytes"),
    ],
)
def test_an_unusable_vast_document_is_refused(document: str, message: str) -> None:
    with pytest.raises(VastError, match=message):
        mezzanine(document)
