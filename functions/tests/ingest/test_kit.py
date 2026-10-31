import json
import xml.etree.ElementTree as ET
from typing import Any

import pytest
from botocore.stub import ANY

from fast_channels.core.demo_demand import DEMO_CREATIVES, DemoIndex, DemoRole, demo_index_key
from fast_channels.core.ladder import Ladder
from fast_channels.core.specs import vast_key
from fast_channels.core.timeline import BREAK_SLATE_GOPS, DEMO_AD_GOPS, HOUSE_AD_GOPS
from fast_channels.functions.ingest import kit
from fast_channels.functions.ingest.kit import KitImages, child_name, kit_requests
from fast_channels.functions.ingest.notify import EventDeliveryError

STANDARD = Ladder.builtin("standard")
IMAGES = KitImages(
    filler="s3://ingest/brand/d/filler.png",
    break_slate="s3://ingest/brand/d/break.png",
    house_ads=("s3://ingest/brand/d/house-ad-1.png", "s3://ingest/brand/d/house-ad-2.png"),
)
DEMO = IMAGES.model_copy(update={"demo": {c.crid: f"s3://cdk-assets/{c.crid}-hash.png" for c in DEMO_CREATIVES}})
VAST = "{http://www.iab.com/VAST}"


def test_child_names_fit_execution_names() -> None:
    assert child_name("kit-standard_86bf8088e0-1a2b3c", "break-15") == "kit-standard_86bf8088e0-1a2b3c-break-15"
    long = child_name("fast-console-" + "0" * 70, "house-ad-2")
    assert len(long) == 80 and long.endswith("-house-ad-2")
    assert child_name("a:b/c", "filler") == "a-b-c-filler"


def test_kit_requests() -> None:
    requests = kit_requests(STANDARD.version, IMAGES, "kit-1")

    assert [(r.kind.value, r.generator.label, r.generator.duration_gops) for r in requests if r.generator] == [
        ("slate", "filler", kit.FILLER_GOPS),
        *(("slate", f"break-{gops}", gops) for gops in BREAK_SLATE_GOPS),
        *(("ad", f"house-ad-{n}", gops) for n, gops in enumerate(HOUSE_AD_GOPS, start=1)),
    ]
    assert {r.ladder_version for r in requests} == {STANDARD.version}
    assert requests[0].correlation_id == "kit-1-filler"
    assert requests[-1].generator is not None and requests[-1].generator.image_s3_uri == IMAGES.house_ads[1]
    with pytest.raises(ValueError, match="house-ad images"):
        kit_requests(STANDARD.version, IMAGES.model_copy(update={"house_ads": ()}), "kit-1")


def test_demo_kits_also_encode_the_demo_creatives() -> None:
    requests = kit_requests(STANDARD.version, DEMO, "kit-1")

    demo = [r for r in requests if r.generator and r.generator.label.startswith("demo-")]
    assert [r.generator.label for r in demo if r.generator] == [f"demo-{c.crid}" for c in DEMO_CREATIVES]
    assert {(r.kind.value, r.generator.duration_gops) for r in demo if r.generator} == {("ad", DEMO_AD_GOPS)}
    assert len(requests) == 1 + len(BREAK_SLATE_GOPS) + len(HOUSE_AD_GOPS) + len(DEMO_CREATIVES)
    with pytest.raises(ValueError, match="demo stills missing"):
        kit_requests(STANDARD.version, IMAGES.model_copy(update={"demo": {"sim1": "s3://b/sim1.png"}}), "kit-1")


def expect_ladder_update(stubs: Any) -> None:
    stubs["dynamodb"].add_response(
        "update_item",
        {},
        {
            "TableName": "library",
            "Key": {"pk": "LADDER#standard", "sk": f"V#{STANDARD.version}"},
            "UpdateExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ExpressionAttributeValues": ANY,
        },
    )


def test_begin_records_the_ladder_and_lists_its_kit(stubs: Any, lambda_context: Any) -> None:
    expect_ladder_update(stubs)
    event = {
        "step": "begin",
        "input": {"ladder": STANDARD.model_dump(mode="json"), "job_template": "fast-standard_x"},
        "execution": "kit-standard-1",
        "images": IMAGES.model_dump(mode="json"),
    }

    result = kit.handler(event, lambda_context)

    assert result["ladder_version"] == STANDARD.version
    assert result["kit_fingerprint"] == STANDARD.version
    assert result["correlation_id"] == "kit-standard-1"
    assert (result["width"], result["height"]) == (1920, 1080)
    assert len(result["requests"]) == 1 + len(BREAK_SLATE_GOPS) + len(HOUSE_AD_GOPS)
    assert result["requests"][1] == {
        "generator": {"image_s3_uri": IMAGES.break_slate, "duration_gops": 15, "label": "break-15"},
        "kind": "slate",
        "cue_points_ms": [],
        "ladder_version": STANDARD.version,
        "correlation_id": "kit-standard-1-break-15",
        "metadata": {},
    }


def child(label: str, duration_ms: int, **extra: Any) -> dict[str, Any]:
    return {"label": label, "duration_ms": duration_ms, "vod_source": f"{label}-vod", **extra}


def test_ready_writes_the_vast_pod_and_marks_the_ladder(
    store: Any, stubs: Any, lambda_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = store()
    monkeypatch.setattr(kit, "S3Store", lambda bucket: objects)
    expect_ladder_update(stubs)
    stubs["events"].add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]}, None)
    ads = [
        child(
            f"house-ad-{n}",
            d,
            vod_source=None,
            creative={"creative_id": f"c{n}", "hls_uri": f"h{n}"},
        )
        for n, d in ((1, 30030), (2, 30030))
    ]
    assets = [child("filler", 30030), *(child(f"break-{g}", g * 2002) for g in BREAK_SLATE_GOPS), *ads]
    context = {
        "ladder_version": STANDARD.version,
        "job_template": "fast-standard_x",
        "kit_fingerprint": "fp",
        "correlation_id": "kit-1",
        "width": 1920,
        "height": 1080,
        "pod_fill_ms": 120120,
    }

    result = kit.handler({"step": "ready", "kit": context, "assets": assets}, lambda_context)

    assert result == {
        "ladder_version": STANDARD.version,
        "kit_fingerprint": "fp",
        "filler": "filler-vod",
        "break_slates": {str(g): f"break-{g}-vod" for g in BREAK_SLATE_GOPS},
        "house_ads": ["c1", "c2"],
        "vast_key": vast_key(STANDARD.version),
        "vast_url": f"https://media.example.com/{vast_key(STANDARD.version)}",
    }
    vast = ET.fromstring(objects.objects[vast_key(STANDARD.version)])
    # The pod cycles through the house ads until it covers the longest break slate (4 x 30.03 s = 120.12 s).
    assert [ad.get("sequence") for ad in vast.iter(f"{VAST}Ad")] == ["1", "2", "3", "4"]
    assert [d.text for d in vast.iter(f"{VAST}Duration")] == ["00:00:30.030"] * 4
    assert objects.objects[kit.BEACON_KEY] == kit.BEACON_GIF
    assert objects.cache_control[kit.BEACON_KEY] == "no-store"


def test_failed_marks_the_ladder(stubs: Any, lambda_context: Any) -> None:
    expect_ladder_update(stubs)
    stubs["events"].add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]}, None)
    error = {"Error": "States.TaskFailed", "Cause": json.dumps({"Error": "IngestRefusedError", "Cause": "x"})}

    result = kit.handler(
        {"step": "failed", "input": {"ladder": STANDARD.model_dump(mode="json")}, "execution": "kit-1", "error": error},
        lambda_context,
    )

    assert result == {"error": "States.TaskFailed", "cause": "IngestRefusedError: x"}


def test_failed_without_a_readable_ladder_only_publishes(stubs: Any, lambda_context: Any) -> None:
    stubs["events"].add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]}, None)

    kit.handler({"step": "failed", "input": {}, "execution": "kit-1", "error": {}}, lambda_context)


def test_undelivered_events_raise(stubs: Any, lambda_context: Any) -> None:
    stubs["events"].add_response(
        "put_events", {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalFailure", "ErrorMessage": "x"}]}, None
    )

    with pytest.raises(EventDeliveryError, match="InternalFailure"):
        kit.handler({"step": "failed", "input": {}, "execution": "kit-1", "error": {}}, lambda_context)


def test_unknown_step(lambda_context: Any) -> None:
    with pytest.raises(ValueError, match="unknown step"):
        kit.handler({"step": "later"}, lambda_context)


def test_ready_writes_the_demo_index_beside_the_vast_pod(
    store: Any, stubs: Any, lambda_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = store()
    monkeypatch.setattr(kit, "S3Store", lambda bucket: objects)
    expect_ladder_update(stubs)
    stubs["events"].add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]}, None)
    house = [
        child(f"house-ad-{n}", 30030, vod_source=None, creative={"creative_id": f"c{n}", "hls_uri": f"h{n}"})
        for n in (1, 2)
    ]
    demo = [
        child(
            f"demo-{c.crid}",
            30030,
            vod_source=None,
            creative={
                "creative_id": f"demo-{c.crid}-x",
                "hls_uri": f"https://media.example.com/ads/s/{c.crid}/main.m3u8",
            },
        )
        for c in DEMO_CREATIVES
    ]
    assets = [child("filler", 30030), *(child(f"break-{g}", g * 2002) for g in BREAK_SLATE_GOPS), *house, *demo]
    context = {
        "ladder_version": STANDARD.version,
        "job_template": "fast-standard_x",
        "kit_fingerprint": "fp",
        "correlation_id": "kit-1",
        "width": 1920,
        "height": 1080,
        "pod_fill_ms": 120120,
    }

    result = kit.handler({"step": "ready", "kit": context, "assets": assets}, lambda_context)

    key = demo_index_key(STANDARD.version)
    assert result["demo_key"] == key
    assert result["house_ads"] == ["c1", "c2"], "demo creatives never join the house pod"
    index = DemoIndex.model_validate_json(objects.objects[key])
    assert [c.crid for c in index.creatives] == [c.crid for c in DEMO_CREATIVES]
    assert {c.creative_id for c in index.creatives if c.crid == "sim3"} == {"demo-sim3-x"}
    assert [c.adomain for c in index.creatives if c.crid in ("sim3", "sim4")] == ["brand-c.example"] * 2
    assert len({c.adomain for c in index.creatives if c.role is DemoRole.SIMULATOR}) == 7
    assert {(c.duration_ms, c.width, c.height) for c in index.creatives} == {(30030, 1920, 1080)}
    vast = ET.fromstring(objects.objects[vast_key(STANDARD.version)])
    assert len(list(vast.iter(f"{VAST}Ad"))) == 4
