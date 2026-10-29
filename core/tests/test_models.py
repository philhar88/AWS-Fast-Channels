"""Catalog, ingest and event contracts."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from fast_channels.core.catalog import ContentItem, ContentPage, ContentQuery, ContentType, is_domain
from fast_channels.core.events import (
    DETAIL_TYPES,
    AssetFailed,
    AssetReady,
    AutopilotRun,
    CatalogItemChanged,
    CensusChanged,
    ChannelAlert,
    LadderFailed,
    LadderReady,
    SchedulePublished,
    event_source,
    put_events_entry,
)
from fast_channels.core.ingest import AssetKind, IngestRequest, asset_id_for
from fast_channels.core.mediaconvert import GeneratorInput

LV = "standard@0123abcd45"


def test_content_item() -> None:
    item = ContentItem(content_id="bbb", title="Big Buck Bunny", genres=["animation"], extra="ignored")  # type: ignore[call-arg]
    assert item.rating is None and item.cue_points_ms == []
    with pytest.raises(ValidationError, match="available_until"):
        ContentItem(
            content_id="x",
            title="t",
            available_from=datetime(2026, 2, 1, tzinfo=UTC),
            available_until=datetime(2026, 1, 1, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        ContentItem(content_id="-bad", title="t")
    page = ContentPage(items=[item], next_token="t2")
    assert ContentPage.model_validate_json(page.model_dump_json()) == page
    assert ContentQuery().limit == 20
    with pytest.raises(ValidationError):
        ContentQuery(limit=0)


def test_show_level_fields_are_optional_and_leave_v3_items_unchanged() -> None:
    v3 = {"content_id": "bbb", "title": "Big Buck Bunny", "synopsis": "", "genres": [], "cue_points_ms": []}
    assert ContentItem.model_validate(v3).model_dump(mode="json", exclude_none=True) == v3 | {"asset_ids": []}
    item = ContentItem(
        content_id="ep-1",
        title="Pilot",
        series_title="A Show",
        season="Season 1",
        episode=1,
        content_type=ContentType.EPISODE,
        language="en",
        producer_name="A Studio",
        producer_domain="studio.example",
        keywords=["pilot"],
        prodq=1,
        external_ids={"metadata.example.org": "EP1"},
    )
    assert ContentItem.model_validate_json(item.model_dump_json()) == item
    assert item.model_dump(mode="json")["content_type"] == "episode"


@pytest.mark.parametrize(
    "bad",
    [
        {"language": "EN"},
        {"language": "eng"},
        {"prodq": 4},
        {"episode": -1},
        {"content_type": "series"},
        {"producer_domain": "localhost"},
        {"producer_domain": f"{'a' * 250}.com"},
        {"series_title": ""},
        {"external_ids": {"not a domain": "1"}},
        {"external_ids": {"metadata.example.org": ""}},
    ],
)
def test_show_level_fields_are_validated(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ContentItem.model_validate({"content_id": "x", "title": "t", **bad})


def test_is_domain() -> None:
    assert is_domain("blender.org") and is_domain("a-b.example.co.uk")
    assert not is_domain("blender") and not is_domain("-a.org") and not is_domain("blender.org\n")


def test_asset_id_for() -> None:
    a = asset_id_for("s3://ingest/library/Big Buck Bunny (2008).mp4")
    assert a.startswith("big-buck-bunny-2008-") and len(a) == len("big-buck-bunny-2008-") + 6
    assert a == asset_id_for("s3://ingest/library/Big Buck Bunny (2008).mp4")
    assert a != asset_id_for("s3://ingest/other/Big Buck Bunny (2008).mp4")
    assert asset_id_for("https://x/%%%.mov?sig=1").startswith("asset-")
    assert len(asset_id_for(f"s3://b/{'x' * 200}.mp4")) <= 48


def test_ingest_request() -> None:
    file = IngestRequest(source_uri="s3://ingest/a.mp4", ladder_version=LV, cue_points_ms=[600_000])
    assert file.kind is AssetKind.CONTENT and file.resolved_asset_id == asset_id_for("s3://ingest/a.mp4")
    assert IngestRequest(source_uri="s3://i/a.mp4", asset_id="mine", ladder_version=LV).resolved_asset_id == "mine"
    slate = IngestRequest(
        generator=GeneratorInput(image_s3_uri="s3://kit/slate.png", duration_gops=30, label="break-slate-30"),
        kind=AssetKind.SLATE,
        ladder_version=LV,
    )
    assert slate.resolved_asset_id.startswith("break-slate-30-")
    for bad in (
        {},
        {"source_uri": "s3://i/a.mp4", "generator": slate.generator},
        {"generator": slate.generator, "cue_points_ms": [2002]},
        {"source_uri": "s3://i/a.mp4", "cue_points_ms": [-1]},
        {"source_uri": "ftp://i/a.mp4"},
        {"source_uri": "s3://i/a.mp4", "correlation_id": "a b"},
    ):
        with pytest.raises(ValidationError):
            IngestRequest.model_validate({"ladder_version": LV} | bad)
    with pytest.raises(ValidationError):
        IngestRequest(source_uri="s3://i/a.mp4", ladder_version="standard")
    with pytest.raises(ValidationError, match="Extra inputs"):  # no caption files or caption mode any more
        IngestRequest.model_validate({"source_uri": "s3://i/a.mp4", "ladder_version": LV, "captions": {"mode": "auto"}})
    assert "nearest frame" in (IngestRequest.model_fields["cue_points_ms"].description or "")
    with pytest.raises(ValidationError):
        GeneratorInput(image_s3_uri="s3://kit/slate.jpg", duration_gops=1, label="x")


def test_events() -> None:
    details = [
        AssetReady(
            asset_id="a",
            kind=AssetKind.CONTENT,
            ladder_version=LV,
            vod_source="fast-a",
            duration_ms=1,
            correlation_id="c",
        ),
        AssetFailed(asset_id="a", kind=AssetKind.AD, ladder_version=LV, error="E", cause="C", correlation_id="c"),
        LadderReady(ladder_version=LV, correlation_id="c"),
        LadderFailed(ladder_version=LV, correlation_id="c", error="E", cause="C"),
        SchedulePublished(channel="ch", plan_id="p", programs=2, start_ms=0, end_ms=1, correlation_id="c"),
        AutopilotRun(channel="ch", status="SUCCEEDED", session_id="s", summary="ok"),
        CensusChanged(running=1, previous=0, correlation_id="c"),
        ChannelAlert(
            channel="ch",
            code="SCHEDULING_ERROR",
            category="SCHEDULING_ERROR",
            message="m",
            resource_arn="arn",
            last_modified="2026-10-29T03:00:00Z",
            correlation_id="c",
        ),
        IngestRequest(source_uri="s3://i/a.mp4", ladder_version=LV),
        CatalogItemChanged(content_id="sintel"),
    ]
    assert {type(d) for d in details} == set(DETAIL_TYPES)
    assert put_events_entry(details[-1], prefix="fast", event_bus="b")["DetailType"] == "Catalog Item Changed"
    with pytest.raises(ValidationError):
        CatalogItemChanged(content_id="-bad")
    assert "captions" not in AssetReady.model_fields
    entry = put_events_entry(details[0], prefix="fast", event_bus="fast-channels")
    assert entry == {
        "Source": "fast-channels.fast",
        "DetailType": "Asset Ready",
        "Detail": details[0].model_dump_json(),
        "EventBusName": "fast-channels",
    }
    assert json.loads(entry["Detail"])["kind"] == "content"
    assert event_source("fast") == "fast-channels.fast"
    with pytest.raises(KeyError):
        put_events_entry(ContentQuery(), prefix="fast", event_bus="b")


def test_census_changed_counts_are_never_negative() -> None:
    with pytest.raises(ValidationError):
        CensusChanged(running=-1, previous=0, correlation_id="c")
