from xml.etree.ElementTree import fromstring

import pytest
from botocore.stub import Stubber

from fast_channels.functions import aws, epg


def _program(name: str, start: str, end: str, **extra: object) -> epg.EpgProgram:
    fields: dict[str, object] = {
        "name": name,
        "title": f"Title {name}",
        "content_id": f"c-{name}",
        "asset_id": f"a-{name}",
        "start": start,
        "end": end,
        "rating": "TV-G",
        "genres": ["animation", "family"],
        "breaks": [{"offset_ms": 300_300, "duration_ms": 60_060}],
        "status": "PUBLISHED",
    }
    return epg.EpgProgram.model_validate(fields | extra)


@pytest.fixture
def guide() -> epg.Epg:
    return epg.feed(
        channel="fast-demo",
        ladder_version="standard@86bf8088e0",
        programs=[
            _program("b", "2026-10-16T12:09:56.596Z", "2026-10-16T12:30:00Z", rating=None, content_id=None),
            _program("a", "2026-10-16T12:00:00Z", "2026-10-16T12:09:56.596Z"),
        ],
        generated_at="2026-10-16T11:58:00.000Z",
    )


def test_feed_orders_programs_by_air_time(guide: epg.Epg) -> None:
    assert [p.name for p in guide.programs] == ["a", "b"]
    assert guide.model_dump()["programs"][0]["breaks"] == [{"offset_ms": 300_300, "duration_ms": 60_060}]


def test_xmltv_follows_the_dtd(guide: epg.Epg) -> None:
    document = epg.xmltv(guide)
    assert document.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n')
    tv = fromstring(document.split("\n", 2)[2])
    assert tv.get("date") == "20261016115800 +0000"
    assert tv.find("channel").get("id") == "fast-demo"  # type: ignore[union-attr]
    first, second = tv.findall("programme")
    assert (first.get("start"), first.get("stop"), first.get("channel")) == (
        "20261016120000 +0000",
        "20261016120956 +0000",
        "fast-demo",
    )
    assert [child.tag for child in first] == ["title", "category", "category", "episode-num", "rating"]
    assert first.findtext("rating/value") == "TV-G"
    assert first.find("episode-num").text == "c-a"  # type: ignore[union-attr]
    assert [child.tag for child in second] == ["title", "category", "category"]


def test_skipped_programs_stay_in_the_json_feed_and_leave_xmltv(guide: epg.Epg) -> None:
    skipped = _program("s", "2026-10-16T11:30:00Z", "2026-10-16T12:00:00Z", status="SKIPPED")
    with_skipped = epg.feed(
        channel=guide.channel,
        ladder_version=guide.ladder_version,
        programs=[*guide.programs, skipped],
        generated_at=guide.generated_at,
    )
    assert [(p.name, p.status) for p in with_skipped.programs] == [
        ("s", "SKIPPED"),
        ("a", "PUBLISHED"),
        ("b", "PUBLISHED"),
    ]
    assert epg.xmltv(with_skipped) == epg.xmltv(guide)


def test_write_uploads_both_feeds_with_short_caching(guide: epg.Epg) -> None:
    s3 = aws.client("s3")
    with Stubber(s3) as stub:
        for key, body, content_type in (
            ("epg/fast-demo.json", guide.model_dump_json().encode(), "application/json"),
            ("epg/fast-demo.xml", epg.xmltv(guide).encode(), "application/xml"),
        ):
            stub.add_response(
                "put_object",
                {},
                {
                    "Bucket": "media",
                    "Key": key,
                    "Body": body,
                    "ContentType": content_type,
                    "CacheControl": "public, max-age=30",
                },
            )
        assert epg.write(guide, bucket="media") == ("epg/fast-demo.json", "epg/fast-demo.xml")
        stub.assert_no_pending_responses()


def test_delete_removes_both_feeds() -> None:
    with Stubber(aws.client("s3")) as stub:
        stub.add_response(
            "delete_objects",
            {},
            {
                "Bucket": "media",
                "Delete": {"Objects": [{"Key": "epg/c.json"}, {"Key": "epg/c.xml"}], "Quiet": True},
            },
        )
        epg.delete("c", bucket="media")
        stub.assert_no_pending_responses()


def test_breaks_carry_their_ids_only_when_given(guide: epg.Epg) -> None:
    plain = epg.EpgBreak(offset_ms=1, duration_ms=2)
    with_ids = epg.EpgBreak(offset_ms=1, duration_ms=2, event_id=1817010633, upid="FST3sintel")

    assert plain.model_dump_json() == '{"offset_ms":1,"duration_ms":2}'  # the v3.0 feed, byte for byte
    assert with_ids.model_dump_json() == '{"offset_ms":1,"duration_ms":2,"event_id":1817010633,"upid":"FST3sintel"}'
    assert '"event_id"' not in guide.model_dump_json()
