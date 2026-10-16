import json
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import Stubber

from fast_channels.core import timeline
from fast_channels.core.ladder import Ladder
from fast_channels.functions import aws
from fast_channels.functions.channel_ops import SCHEDULE_MINUTES
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import library_store as store
from fast_channels.functions.tools import schedule
from fast_channels.functions.tools.registry import ToolContext, ToolError

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

TABLE, CATALOG, BUCKET = "library-test", "catalog-test", "media-test"
CHANNEL = "fast-demo"
ARN = f"arn:aws:mediatailor:us-east-1:111122223333:channel/{CHANNEL}"
STANDARD = Ladder.builtin("standard")
LV = STANDARD.version
NOW = 1_792_152_000_000
"""2026-10-16T05:20:00Z."""
BRIEF = timeline.ProgrammingBrief(ratings=["TV-G", "TV-PG"])
CTX = ToolContext(tool="schedule___plan", correlation_id="fast-console-session-1")
PLAN_ID = f"{CHANNEL}.cafe0001"
_typed = TypeSerializer().serialize


def decimals(value: Any) -> Any:
    """``value`` with floats as Decimal, the only number type DynamoDB serialises."""
    match value:
        case float():
            return Decimal(str(value))
        case dict():
            return {k: decimals(v) for k, v in value.items()}
        case list():
            return [decimals(v) for v in value]
        case _:
            return value


def dynamo(item: dict[str, Any]) -> dict[str, Any]:
    """An item as the low-level DynamoDB client returns it."""
    return {k: _typed(decimals(v)) for k, v in item.items()}


@dataclass
class Calls:
    """Parameters of every API call, recorded before serialisation."""

    params: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def __call__(self, params: dict[str, Any], event_name: str, **_: Any) -> None:
        self.params.append((event_name.rsplit(".", 1)[-1], params))

    def of(self, operation: str) -> list[dict[str, Any]]:
        return [p for name, p in self.params if name == operation]


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in {
        "LIBRARY_TABLE": TABLE,
        "CATALOG_TABLE": CATALOG,
        "MEDIA_BUCKET": BUCKET,
        "EVENT_BUS": "fast-fast-channels",
        "FAST_PREFIX": "fast",
        "SOURCE_LOCATION": "fast-origin",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(schedule, "now_ms", lambda: NOW)
    monkeypatch.setattr(schedule, "new_plan_id", lambda channel: f"{channel}.cafe0001")
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def aws_stubs() -> Iterator[SimpleNamespace]:
    clients = {
        "ddb": aws.table(TABLE).meta.client,
        "catalog": aws.table(CATALOG).meta.client,
        "mt": aws.client("mediatailor"),
        "s3": aws.client("s3"),
        "events": aws.client("events"),
    }
    calls = Calls()
    with ExitStack() as stack:
        stubs = {}
        for name, client in clients.items():
            client.meta.events.register("provide-client-params", calls)
            stack.callback(client.meta.events.unregister, "provide-client-params", calls)
            stubs[name] = stack.enter_context(Stubber(client))
        yield SimpleNamespace(**stubs, calls=calls)
        for stub in stubs.values():
            stub.assert_no_pending_responses()


# --- fixtures as records --------------------------------------------------------------------------------------------


def channel(**changes: Any) -> store.ChannelRecord:
    fields: dict[str, Any] = {
        "name": CHANNEL,
        "ladder_version": LV,
        "outputs": ["hls", "dash-avc"],
        "owner": "iac",
        "tier": "BASIC",
        "arn": ARN,
        "brief": BRIEF,
    }
    return store.ChannelRecord(**fields | changes)


def ladder() -> store.LadderRecord:
    kit = store.LadderKit(filler="fast-filler", break_slates={15: "fast-slate-15", 30: "fast-slate-30"})
    return store.LadderRecord(ladder_version=LV, status="READY", kit=kit)


def asset(asset_id: str, *, duration_ms: int = 1_201_200, **changes: Any) -> store.Asset:
    fields: dict[str, Any] = {
        "asset_id": asset_id,
        "ladder_version": LV,
        "kind": "content",
        "status": "READY",
        "content_id": f"c-{asset_id}",
        "duration_ms": duration_ms,
        "break_offsets_ms": [360_360, 720_720, 1_081_080],
        "vod_source": f"fast-{asset_id}",
    }
    return store.Asset(**fields | changes)


def program(name: str, start_ms: int, duration_ms: int = 1_200_000, plan_id: str = "old") -> store.ProgramRecord:
    return store.ProgramRecord(
        channel=CHANNEL,
        status="PUBLISHED",
        name=name,
        asset_id=f"asset-{name}",
        vod_source=f"fast-{name}",
        content_id=None,
        start_ms=start_ms,
        duration_ms=duration_ms,
        breaks=[],
        ladder_version=LV,
        plan_id=plan_id,
        title=name.upper(),
    )


def content(content_id: str, rating: str) -> dict[str, Any]:
    return {
        "pk": f"CONTENT#{content_id}",
        "sk": "META",
        "content_id": content_id,
        "title": content_id.title(),
        "rating": rating,
        "genres": ["animation"],
    }


# --- stubbing helpers -----------------------------------------------------------------------------------------------


def get_item(stub: Stubber, key: dict[str, str], item: dict[str, Any] | None = None, table: str = TABLE) -> None:
    stub.add_response("get_item", {"Item": dynamo(item)} if item else {}, {"TableName": table, "Key": key})


def query(stub: Stubber, *items: dict[str, Any]) -> None:
    stub.add_response("query", {"Items": [dynamo(i) for i in items], "Count": len(items), "ScannedCount": len(items)})


def live(*entries: tuple[str, int]) -> dict[str, Any]:
    return {
        "Items": [
            {
                "Arn": f"{ARN.replace(':channel/', ':program/')}/{name}",
                "ChannelName": CHANNEL,
                "ProgramName": name,
                "SourceLocationName": "fast-origin",
                "ScheduleEntryType": "PROGRAM",
                "ApproximateStartTime": datetime.fromtimestamp(start / 1000, UTC),
            }
            for name, start in entries
        ]
    }


def schedule_request() -> dict[str, Any]:
    return {"ChannelName": CHANNEL, "DurationMinutes": str(SCHEDULE_MINUTES)}


def plan_of(*assets: store.Asset, start_ms: int) -> timeline.Plan:
    """What the plan tool computes for ``assets`` (G-rated) on the demo channel."""
    inputs = [
        timeline.PlanInputAsset(
            asset_id=a.asset_id,
            vod_source=a.vod_source or "",
            content_id=a.content_id,
            duration_ms=a.duration_ms or 0,
            break_offsets_ms=a.break_offsets_ms,
            ladder_version=LV,
            rating="TV-G",
            genres=["animation"],
        )
        for a in assets
    ]
    return timeline.plan_block(
        channel=CHANNEL,
        ladder_version=LV,
        grid=STANDARD.grid,
        start_ms=start_ms,
        assets=inputs,
        brief=BRIEF,
        now_ms=NOW,
    )


def stored(plan: timeline.Plan, fingerprint: str) -> dict[str, Any]:
    metadata = {
        p.asset_id: {"title": p.asset_id.title(), "rating": "TV-G", "genres": ["animation"]} for p in plan.programs
    }
    record = schedule.StoredPlan(
        plan_id=PLAN_ID,
        plan=plan,
        metadata=metadata,
        fingerprint=fingerprint,
        correlation_id="fast-console-session-1",
        expires_at=int(time.time()) + 600,
    )
    return {**store.plan_key(CHANNEL, PLAN_ID), **record.model_dump(mode="json")}


# --- get_schedule ---------------------------------------------------------------------------------------------------


def test_get_schedule_lists_upcoming_programs_and_gaps(aws_stubs: SimpleNamespace) -> None:
    aired = program("aired", NOW - 3_600_000)
    first, second = program("first", NOW - 600_000), program("second", NOW + 1_200_000)
    get_item(aws_stubs.ddb, store.channel_key(CHANNEL), channel().to_item())
    query(aws_stubs.ddb, aired.to_item(), first.to_item(), second.to_item())

    result = schedule.get_schedule(schedule.ChannelInput(channel=CHANNEL), CTX)

    assert [p.name for p in result.programs] == ["first", "second"]
    assert result.programs[0].start == store.iso(NOW - 600_000) and result.programs[0].title == "FIRST"
    assert (result.end_ms, result.end) == (second.end_ms, store.iso(second.end_ms))
    [gap] = result.gaps
    assert (gap.start_ms, gap.end_ms, gap.duration_ms) == (first.end_ms, second.start_ms, 600_000)


def test_get_schedule_lists_skipped_programs_but_not_as_scheduled(aws_stubs: SimpleNamespace) -> None:
    skipped = program("skipped", NOW - 600_000).model_copy(update={"status": store.ProgramStatus.SKIPPED})
    get_item(aws_stubs.ddb, store.channel_key(CHANNEL), channel().to_item())
    query(aws_stubs.ddb, skipped.to_item())

    result = schedule.get_schedule(schedule.ChannelInput(channel=CHANNEL), CTX)

    assert [(p.name, p.status) for p in result.programs] == [("skipped", "SKIPPED")]
    assert (result.end_ms, result.end, result.gaps) == (None, None, [])  # filler airs: nothing is scheduled


def test_plan_ignores_skipped_programs(aws_stubs: SimpleNamespace) -> None:
    skipped = program("skipped", NOW + 300_000).model_copy(update={"status": store.ProgramStatus.SKIPPED})
    ddb = aws_stubs.ddb
    get_item(ddb, store.channel_key(CHANNEL), channel(brief=None).to_item())
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    query(ddb, skipped.to_item())
    get_item(ddb, store.asset_key("bbb", LV), asset("bbb", content_id=None).to_item())
    ddb.add_response("put_item", {})
    aws_stubs.mt.add_response("get_channel_schedule", {"Items": []}, schedule_request())

    result = schedule.plan(schedule.PlanInput(channel=CHANNEL, picks=["bbb"]), CTX)

    assert result.start_ms == NOW + timeline.LEAD_TIME_MS  # an empty schedule: it does not append to the skipped one
    assert result.publishable and not [v for v in result.violations if v.code == "OVERLAP"]


def test_unknown_channel_is_not_found(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.channel_key("nope"))
    with pytest.raises(ToolError) as raised:
        schedule.get_brief(schedule.ChannelInput(channel="nope"), CTX)
    assert raised.value.code == "NOT_FOUND"


# --- plan -----------------------------------------------------------------------------------------------------------


def test_plan_appends_to_the_schedule_and_reports_every_violation(aws_stubs: SimpleNamespace) -> None:
    previous = program("prev", NOW + 1_200_000)
    bbb, sintel = asset("bbb"), asset("sintel", duration_ms=600_600)
    ddb = aws_stubs.ddb
    get_item(ddb, store.channel_key(CHANNEL), channel().to_item())
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    query(ddb, previous.to_item())
    get_item(ddb, store.asset_key("bbb", LV), bbb.to_item())
    get_item(ddb, store.asset_key("nope", LV))
    query(ddb)  # nope has no encode at all
    get_item(ddb, store.asset_key("sintel", LV), sintel.to_item())
    ddb.add_response("put_item", {})
    get_item(aws_stubs.catalog, {"pk": "CONTENT#c-bbb", "sk": "META"}, content("c-bbb", "TV-G"), CATALOG)
    get_item(aws_stubs.catalog, {"pk": "CONTENT#c-sintel", "sk": "META"}, content("c-sintel", "TV-14"), CATALOG)
    aws_stubs.mt.add_response("get_channel_schedule", live(("prev", 0)), schedule_request())

    result = schedule.plan(schedule.PlanInput(channel=CHANNEL, picks=["bbb", "nope", "sintel"]), CTX)

    assert result.plan_id == PLAN_ID and result.channel == CHANNEL and result.ladder_version == LV
    assert result.start_ms == previous.end_ms and result.start == store.iso(previous.end_ms)
    expected = plan_of(bbb, sintel, start_ms=previous.end_ms)
    assert [p.model_dump(include=set(timeline.PlannedProgram.model_fields)) for p in result.programs] == [
        p.model_dump() for p in expected.programs
    ]
    first, second = result.programs
    assert (first.title, first.rating, first.start, first.end) == (
        "C-Bbb",
        "TV-G",
        store.iso(first.start_ms),
        store.iso(first.end_ms),
    )
    assert first.breaks and second.start_ms == first.end_ms
    codes = {(v.code, v.program) for v in result.violations}
    assert ("UNKNOWN_ASSET", "nope") in codes and ("RATING", second.name) in codes
    assert not result.publishable
    [item] = aws_stubs.calls.of("PutItem")
    saved = item["Item"]
    assert (saved["pk"], saved["sk"]) == (f"CHANNEL#{CHANNEL}", f"PLAN#{PLAN_ID}")
    assert saved["fingerprint"] == schedule.anchor([schedule.LiveProgram("prev", previous.start_ms)], 0).fingerprint
    assert saved["correlation_id"] == CTX.correlation_id
    assert abs(saved["expires_at"] - (time.time() + schedule.PLAN_TTL_S)) < 5


def test_plan_on_an_empty_schedule_starts_after_the_lead_time(aws_stubs: SimpleNamespace) -> None:
    ddb = aws_stubs.ddb
    get_item(ddb, store.channel_key(CHANNEL), channel(brief=None).to_item())
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    query(ddb)
    get_item(ddb, store.asset_key("bbb", LV), asset("bbb", content_id=None).to_item())
    ddb.add_response("put_item", {})
    aws_stubs.mt.add_response("get_channel_schedule", {"Items": []}, schedule_request())

    result = schedule.plan(schedule.PlanInput(channel=CHANNEL, picks=["bbb"]), CTX)

    assert result.start_ms == NOW + timeline.LEAD_TIME_MS
    assert result.programs[0].title == "bbb" and result.publishable


@pytest.mark.parametrize(("cue_points_ms", "breaks"), [([20_020], 1), ([], 0)])
def test_editorial_cue_points_place_breaks_where_the_policy_margins_would_not(
    aws_stubs: SimpleNamespace, cue_points_ms: list[int], breaks: int
) -> None:
    # A 2-minute short is under the policy's 3-minute minimum, and 20 s is inside its 60 s start margin: only an
    # editor's cue point (the asset's cue_points_ms) puts a break there.
    short = asset("short", duration_ms=120_120, break_offsets_ms=[20_020], cue_points_ms=cue_points_ms, content_id=None)
    ddb = aws_stubs.ddb
    get_item(ddb, store.channel_key(CHANNEL), channel(brief=None).to_item())
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    query(ddb)
    get_item(ddb, store.asset_key("short", LV), short.to_item())
    ddb.add_response("put_item", {})
    aws_stubs.mt.add_response("get_channel_schedule", {"Items": []}, schedule_request())

    policy = timeline.BreakPolicy(cadence_minutes=0.25)
    result = schedule.plan(schedule.PlanInput(channel=CHANNEL, picks=["short"], break_policy=policy), CTX)

    assert len(result.programs[0].breaks) == breaks


def test_plan_reports_assets_encoded_on_another_ladder(aws_stubs: SimpleNamespace) -> None:
    ddb = aws_stubs.ddb
    get_item(ddb, store.channel_key(CHANNEL), channel().to_item())
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    query(ddb)
    get_item(ddb, store.asset_key("bbb", LV))
    query(ddb, asset("bbb", ladder_version="uhd@5306b91dd0").to_item())
    ddb.add_response("put_item", {})
    aws_stubs.mt.add_response("get_channel_schedule", {"Items": []}, schedule_request())

    start = datetime.fromtimestamp((NOW + 3_600_000) / 1000, UTC)
    result = schedule.plan(schedule.PlanInput(channel=CHANNEL, picks=["bbb"], start=start), CTX)

    assert result.start_ms == NOW + 3_600_000
    assert {v.code for v in result.violations} >= {"LADDER_MISMATCH", "EMPTY"}


def test_plan_after_an_unknown_program_is_not_found(aws_stubs: SimpleNamespace) -> None:
    ddb = aws_stubs.ddb
    get_item(ddb, store.channel_key(CHANNEL), channel().to_item())
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    query(ddb, program("prev", NOW).to_item())
    with pytest.raises(ToolError) as raised:
        schedule.plan(schedule.PlanInput(channel=CHANNEL, picks=["bbb"], after="other"), CTX)
    assert raised.value.code == "NOT_FOUND"


def test_plan_takes_start_or_after() -> None:
    with pytest.raises(ValueError, match="start or after"):
        schedule.PlanInput(channel=CHANNEL, picks=["a"], start=datetime.now(UTC), after="p")


# --- publish --------------------------------------------------------------------------------------------------------


def test_anchor_is_the_program_preceding_the_block_in_time() -> None:
    live_schedule = [schedule.LiveProgram("a", 0), schedule.LiveProgram("b", 100), schedule.LiveProgram("c", 300)]
    position = schedule.anchor(live_schedule, 200)
    assert (position.after, position.before) == ("b", None)
    assert position.fingerprint == schedule.anchor(live_schedule[1:], 200).fingerprint  # aired programs may go
    assert position.fingerprint != schedule.anchor([*live_schedule, schedule.LiveProgram("d", 400)], 200).fingerprint
    own = schedule.LiveProgram("own", 250)
    assert schedule.anchor([*live_schedule, own], 200, exclude=["own"]) == position
    first = schedule.anchor(live_schedule, 0)
    assert (first.after, first.before) == (None, "a")
    assert (schedule.anchor([], 0).after, schedule.anchor([], 0).before) == (None, None)


def test_a_block_before_the_whole_schedule_goes_in_front_of_its_first_program() -> None:
    plan = plan_of(asset("bbb"), asset("sintel"), start_ms=NOW + 600_000)
    record = schedule.StoredPlan.model_validate(stored(plan, "f"))
    position = schedule.Anchor(after=None, before="later", fingerprint="f")
    first, second = schedule._requests(
        record, slates={30: "slate-30"}, source_location="fast-origin", position=position, live=["later"]
    )
    assert first["ScheduleConfiguration"]["Transition"] == {
        "Type": "ABSOLUTE",
        "RelativePosition": "BEFORE_PROGRAM",
        "RelativeProgram": "later",
        "ScheduledStartTimeMillis": plan.start_ms,
    }
    assert second["ScheduleConfiguration"]["Transition"] == {
        "Type": "ABSOLUTE",
        "RelativePosition": "AFTER_PROGRAM",
        "RelativeProgram": plan.programs[0].name,
        "ScheduledStartTimeMillis": plan.programs[1].start_ms,
    }
    resumed = schedule._requests(
        record, slates={30: "slate-30"}, source_location="fast-origin", position=position, live=[first["ProgramName"]]
    )
    assert [r["ProgramName"] for r in resumed] == [second["ProgramName"]]


def publish_stubs(
    aws_stubs: SimpleNamespace,
    plan: timeline.Plan,
    previous: store.ProgramRecord,
    *,
    assets: dict[str, store.Asset] | None = None,
) -> None:
    """The reads of a publish up to its readiness check: ``assets`` (asset id -> record) are what the library holds
    then, by default each planned asset READY; ``{}`` when the publish fails before the check."""
    fingerprint = schedule.anchor([schedule.LiveProgram(previous.name, previous.start_ms)], plan.start_ms).fingerprint
    ddb = aws_stubs.ddb
    get_item(ddb, store.plan_key(CHANNEL, PLAN_ID), stored(plan, fingerprint))
    get_item(ddb, store.channel_key(CHANNEL), channel().to_item())
    ddb.add_response("put_item", {})  # lease
    query(ddb, previous.to_item())
    ids = dict.fromkeys(p.asset_id for p in plan.programs)
    for asset_id, record in (assets if assets is not None else {a: asset(a) for a in ids}).items():
        get_item(ddb, store.asset_key(asset_id, LV), record.to_item())


def guide_written(aws_stubs: SimpleNamespace, *programs: store.ProgramRecord) -> None:
    query(aws_stubs.ddb, *(p.to_item() for p in programs))  # the guide is rendered from the table
    aws_stubs.s3.add_response("put_object", {})
    aws_stubs.s3.add_response("put_object", {})


def test_publish_links_the_block_after_the_preceding_program(aws_stubs: SimpleNamespace) -> None:
    previous = program("prev", NOW + 1_200_000)
    plan = plan_of(asset("bbb"), asset("sintel"), start_ms=previous.end_ms)
    publish_stubs(aws_stubs, plan, previous)
    ddb, mt = aws_stubs.ddb, aws_stubs.mt
    mt.add_response("get_channel_schedule", live(("prev", 0)), schedule_request())
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    for _ in plan.programs:
        mt.add_response("create_program", {})
        ddb.add_response("put_item", {})
    guide_written(aws_stubs, previous)
    ddb.add_response("delete_item", {})  # lease
    aws_stubs.events.add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "e"}]})
    noise = {"AlertCode": "PROGRAM_ALERT", "AlertMessage": "SEGMENT_DURATION_MISMATCH: harmless"}
    real = {"AlertCode": "PROGRAM_ALERT", "AlertMessage": "VOD_SOURCE_ALERT: UNREACHABLE"}
    alert_items = [
        {
            **a,
            "Category": "SCHEDULING_ERROR",
            "LastModifiedTime": datetime.now(UTC),
            "RelatedResourceArns": [],
            "ResourceArn": ARN,
        }
        for a in (noise, real)
    ]
    mt.add_response("list_alerts", {"Items": alert_items}, {"ResourceArn": ARN})

    result = schedule.publish(schedule.PublishInput(plan_id=PLAN_ID), CTX)

    first, second = aws_stubs.calls.of("CreateProgram")
    assert first["ScheduleConfiguration"]["Transition"] == {
        "Type": "ABSOLUTE",
        "RelativePosition": "AFTER_PROGRAM",
        "ScheduledStartTimeMillis": plan.start_ms,
        "RelativeProgram": "prev",
    }
    assert second["ScheduleConfiguration"]["Transition"] == {
        "Type": "ABSOLUTE",
        "RelativePosition": "AFTER_PROGRAM",
        "ScheduledStartTimeMillis": plan.programs[1].start_ms,
        "RelativeProgram": plan.programs[0].name,
    }
    assert first["SourceLocationName"] == "fast-origin" and first["VodSourceName"] == "fast-bbb"
    ad_break = first["AdBreaks"][0]
    assert ad_break["MessageType"] == "TIME_SIGNAL"
    assert ad_break["Slate"] == {"SourceLocationName": "fast-origin", "VodSourceName": "fast-slate-30"}
    descriptor = ad_break["TimeSignalMessage"]["SegmentationDescriptors"][0]
    assert (descriptor["SegmentationTypeId"], descriptor["SegmentationUpidType"]) == (0x34, 0x0C)
    lease, *programs = aws_stubs.calls.of("PutItem")
    assert lease["Item"]["sk"] == "LEASE" and "ConditionExpression" in lease
    assert [p["Item"]["status"] for p in programs] == ["PUBLISHED", "PUBLISHED"]
    assert programs[0]["Item"]["gsi1pk"] == f"PROGRAM#{plan.programs[0].name}"
    assert programs[0]["Item"]["expires_at"] == plan.programs[0].end_ms // 1000 + 30 * 86_400
    assert programs[0]["Item"]["plan_id"] == PLAN_ID and programs[0]["Item"]["title"] == "Bbb"
    assert [o["Key"] for o in aws_stubs.calls.of("PutObject")] == ["epg/fast-demo.json", "epg/fast-demo.xml"]
    [entry] = aws_stubs.calls.of("PutEvents")[0]["Entries"]
    assert entry["DetailType"] == "Schedule Published"
    assert json.loads(entry["Detail"]) == {
        "channel": CHANNEL,
        "plan_id": PLAN_ID,
        "programs": 2,
        "start_ms": plan.start_ms,
        "end_ms": plan.end_ms,
        "correlation_id": CTX.correlation_id,
    }
    assert result.plan_id == PLAN_ID and result.published == 2
    assert [p.name for p in result.programs] == [p.name for p in plan.programs]
    assert result.programs[0].status == "PUBLISHED" and result.end == store.iso(plan.end_ms)
    assert [a.message for a in result.alerts] == ["VOD_SOURCE_ALERT: UNREACHABLE"]


def test_a_retried_publish_creates_only_the_missing_programs(aws_stubs: SimpleNamespace) -> None:
    previous = program("prev", NOW + 1_200_000)
    plan = plan_of(asset("bbb"), asset("sintel"), start_ms=previous.end_ms)
    publish_stubs(aws_stubs, plan, previous)
    ddb, mt = aws_stubs.ddb, aws_stubs.mt
    mt.add_response("get_channel_schedule", live(("prev", 0), (plan.programs[0].name, 0)), schedule_request())
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    ddb.add_response("put_item", {})  # the program already on air is recorded first
    mt.add_response("create_program", {})
    ddb.add_response("put_item", {})
    guide_written(aws_stubs)
    ddb.add_response("delete_item", {})
    aws_stubs.events.add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "e"}]})
    mt.add_response("list_alerts", {"Items": []}, {"ResourceArn": ARN})

    result = schedule.publish(schedule.PublishInput(plan_id=PLAN_ID), CTX)

    [created] = aws_stubs.calls.of("CreateProgram")
    assert created["ProgramName"] == plan.programs[1].name
    assert created["ScheduleConfiguration"]["Transition"]["RelativeProgram"] == plan.programs[0].name
    assert result.published == 2


def test_publish_is_stale_when_the_schedule_moved(aws_stubs: SimpleNamespace) -> None:
    previous = program("prev", NOW + 1_200_000)
    plan = plan_of(asset("bbb"), start_ms=previous.end_ms)
    publish_stubs(aws_stubs, plan, previous)
    intruder = ("someone-else", previous.end_ms)
    aws_stubs.mt.add_response("get_channel_schedule", live(("prev", 0), intruder), schedule_request())
    aws_stubs.ddb.add_response("delete_item", {})  # the lease is released on failure too

    with pytest.raises(ToolError) as raised:
        schedule.publish(schedule.PublishInput(plan_id=PLAN_ID), CTX)
    assert raised.value.code == "PLAN_STALE"


def test_publish_refuses_a_plan_with_errors(aws_stubs: SimpleNamespace) -> None:
    previous = program("prev", NOW + 1_200_000)
    rating = timeline.Violation(code="RATING", severity="error", message="TV-14 is not allowed")
    plan = plan_of(asset("bbb"), start_ms=previous.end_ms).model_copy(update={"violations": [rating]})
    publish_stubs(aws_stubs, plan, previous, assets={})
    aws_stubs.ddb.add_response("delete_item", {})
    with pytest.raises(ToolError) as raised:
        schedule.publish(schedule.PublishInput(plan_id=PLAN_ID), CTX)
    assert raised.value.code == "VALIDATION"


def test_publish_revalidates_against_the_current_schedule(aws_stubs: SimpleNamespace) -> None:
    previous = program("prev", NOW + 1_200_000)  # published after the plan was made
    publish_stubs(aws_stubs, plan_of(asset("bbb"), start_ms=previous.start_ms), previous, assets={})
    aws_stubs.ddb.add_response("delete_item", {})
    with pytest.raises(ToolError) as raised:
        schedule.publish(schedule.PublishInput(plan_id=PLAN_ID), CTX)
    assert raised.value.code == "PLAN_STALE" and "OVERLAP" in raised.value.message


def test_publish_while_another_writer_holds_the_lease(aws_stubs: SimpleNamespace) -> None:
    previous = program("prev", NOW + 1_200_000)
    plan = plan_of(asset("bbb"), start_ms=previous.end_ms)
    ddb = aws_stubs.ddb
    get_item(ddb, store.plan_key(CHANNEL, PLAN_ID), stored(plan, "f"))
    get_item(ddb, store.channel_key(CHANNEL), channel().to_item())
    ddb.add_client_error("put_item", service_error_code="ConditionalCheckFailedException")
    with pytest.raises(ToolError) as raised:
        schedule.publish(schedule.PublishInput(plan_id=PLAN_ID), CTX)
    assert (raised.value.code, raised.value.retryable) == ("CONFLICT", True)


def test_expired_or_unknown_plans_are_not_found(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.plan_key(CHANNEL, PLAN_ID))
    for plan_id in (PLAN_ID, "no-channel-part"):
        with pytest.raises(ToolError) as raised:
            schedule.publish(schedule.PublishInput(plan_id=plan_id), CTX)
        assert raised.value.code == "NOT_FOUND"


def test_publish_is_stale_when_an_asset_is_no_longer_ready(aws_stubs: SimpleNamespace) -> None:
    previous = program("prev", NOW + 1_200_000)
    plan = plan_of(asset("bbb"), asset("sintel"), start_ms=previous.end_ms)
    failed = asset("sintel", status="FAILED")
    publish_stubs(aws_stubs, plan, previous, assets={"bbb": asset("bbb"), "sintel": failed})
    aws_stubs.ddb.add_response("delete_item", {})

    with pytest.raises(ToolError) as raised:
        schedule.publish(schedule.PublishInput(plan_id=PLAN_ID), CTX)

    assert raised.value.code == "PLAN_STALE"
    assert raised.value.message == "NOT_READY: sintel is FAILED with VOD source fast-sintel, planned with fast-sintel"
    assert not aws_stubs.calls.of("CreateProgram")


def test_a_publish_that_fails_part_way_records_what_airs_and_says_how_to_resume(aws_stubs: SimpleNamespace) -> None:
    previous = program("prev", NOW + 1_200_000)
    plan = plan_of(asset("bbb"), asset("sintel"), start_ms=previous.end_ms)
    publish_stubs(aws_stubs, plan, previous)
    ddb, mt = aws_stubs.ddb, aws_stubs.mt
    mt.add_response("get_channel_schedule", live(("prev", 0)), schedule_request())
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    mt.add_response("create_program", {})
    ddb.add_response("put_item", {})  # the first program, recorded as soon as it exists
    mt.add_client_error("create_program", "BadRequestException", "VodSource fast-sintel does not exist")
    guide_written(aws_stubs, previous)
    ddb.add_response("delete_item", {})  # lease

    with pytest.raises(ToolError) as raised:
        schedule.publish(schedule.PublishInput(plan_id=PLAN_ID), CTX)

    error = raised.value
    assert (error.code, error.retryable) == ("VALIDATION", False)
    assert error.message.startswith(f"published 1 of 2 programs of {PLAN_ID}, then: BadRequestException")
    assert error.hint == schedule.RESUME_HINT
    _, recorded = aws_stubs.calls.of("PutItem")
    assert recorded["Item"]["name"] == plan.programs[0].name
    assert [o["Key"] for o in aws_stubs.calls.of("PutObject")] == ["epg/fast-demo.json", "epg/fast-demo.xml"]


def test_a_publish_running_out_of_time_stops_resumably(aws_stubs: SimpleNamespace) -> None:
    previous = program("prev", NOW + 1_200_000)
    plan = plan_of(asset("bbb"), asset("sintel"), start_ms=previous.end_ms)
    publish_stubs(aws_stubs, plan, previous)
    ddb, mt = aws_stubs.ddb, aws_stubs.mt
    mt.add_response("get_channel_schedule", live(("prev", 0)), schedule_request())
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    mt.add_response("create_program", {})
    ddb.add_response("put_item", {})
    guide_written(aws_stubs, previous)
    ddb.add_response("delete_item", {})
    ending = ToolContext(
        tool="schedule___publish",
        correlation_id="s",
        invocation=SimpleNamespace(get_remaining_time_in_millis=lambda: schedule.PUBLISH_TIME_MARGIN_MS - 1),
    )

    with pytest.raises(ToolError) as raised:
        schedule.publish(schedule.PublishInput(plan_id=PLAN_ID), ending)

    assert (raised.value.code, raised.value.retryable) == ("UPSTREAM", True)
    assert "published 1 of 2 programs" in raised.value.message and "running out of time" in raised.value.message
    assert len(aws_stubs.calls.of("CreateProgram")) == 1


def test_publish_succeeds_when_its_follow_up_calls_fail(aws_stubs: SimpleNamespace) -> None:
    previous = program("prev", NOW + 1_200_000)
    plan = plan_of(asset("bbb"), start_ms=previous.end_ms)
    publish_stubs(aws_stubs, plan, previous)
    ddb, mt = aws_stubs.ddb, aws_stubs.mt
    mt.add_response("get_channel_schedule", live(("prev", 0)), schedule_request())
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    mt.add_response("create_program", {})
    ddb.add_response("put_item", {})
    guide_written(aws_stubs, previous)
    ddb.add_response("delete_item", {})
    aws_stubs.events.add_client_error("put_events", "ThrottlingException")
    mt.add_client_error("list_alerts", "TooManyRequestsException", http_status_code=429)

    result = schedule.publish(schedule.PublishInput(plan_id=PLAN_ID), CTX)

    assert result.published == 1
    [alert] = result.alerts
    assert (alert.code, alert.category) == ("ALERTS_UNAVAILABLE", "INFO")
    assert "origins___list_alerts" in alert.message


def test_plan_after_a_program_with_its_own_break_policy(aws_stubs: SimpleNamespace) -> None:
    previous, later = program("prev", NOW + 1_200_000), program("later", NOW + 7_200_000)
    ddb = aws_stubs.ddb
    get_item(ddb, store.channel_key(CHANNEL), channel(brief=None).to_item())
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    query(ddb, previous.to_item(), later.to_item())
    get_item(ddb, store.asset_key("bbb", LV), asset("bbb", content_id=None).to_item())
    ddb.add_response("put_item", {})
    aws_stubs.mt.add_response("get_channel_schedule", live(("prev", 0), ("later", 0)), schedule_request())

    policy = timeline.BreakPolicy(cadence_minutes=5, slate_gops=15)
    payload = schedule.PlanInput(channel=CHANNEL, picks=["bbb"], after="prev", break_policy=policy)
    result = schedule.plan(payload, CTX)

    assert result.start_ms == previous.end_ms
    assert {b.slate_gops for b in result.programs[0].breaks} == {15}
    saved = aws_stubs.calls.of("PutItem")[0]["Item"]
    expected = schedule.anchor(
        [schedule.LiveProgram("prev", previous.start_ms), schedule.LiveProgram("later", later.start_ms)],
        previous.end_ms,
    )
    assert saved["fingerprint"] == expected.fingerprint


def test_unschedulable_picks_become_violations(aws_stubs: SimpleNamespace) -> None:
    ddb = aws_stubs.ddb
    for asset_id, record in (
        ("slate", asset("slate", kind="slate")),
        ("busy", asset("busy", status="INGESTING", vod_source=None)),
        ("bbb", asset("bbb")),
    ):
        get_item(ddb, store.asset_key(asset_id, LV), record.to_item())
    get_item(aws_stubs.catalog, {"pk": "CONTENT#c-bbb", "sk": "META"}, {"pk": "CONTENT#c-bbb", "sk": "META"}, CATALOG)

    assets, metadata, problems = schedule.resolve_picks(["slate", "busy", "bbb", "bbb"], LV)

    assert [a.asset_id for a in assets] == ["bbb", "bbb"]
    assert metadata["bbb"].title == "bbb" and metadata["bbb"].rating is None  # unreadable catalog item
    assert [(v.code, v.program) for v in problems] == [("NOT_CONTENT", "slate"), ("NOT_READY", "busy")]


def test_publish_needs_the_ladder_kit_to_have_the_break_slate(aws_stubs: SimpleNamespace) -> None:
    previous = program("prev", NOW + 1_200_000)
    brief = BRIEF.model_copy(update={"break_policy": timeline.BreakPolicy(slate_gops=45)})
    inputs = [
        timeline.PlanInputAsset(
            asset_id="bbb",
            vod_source="fast-bbb",
            duration_ms=1_201_200,
            break_offsets_ms=[720_720],
            ladder_version=LV,
            rating="TV-G",
        )
    ]
    plan = timeline.plan_block(
        channel=CHANNEL,
        ladder_version=LV,
        grid=STANDARD.grid,
        start_ms=previous.end_ms,
        assets=inputs,
        brief=brief,
        now_ms=NOW,
    )
    publish_stubs(aws_stubs, plan, previous)
    aws_stubs.mt.add_response("get_channel_schedule", live(("prev", 0)), schedule_request())
    get_item(aws_stubs.ddb, store.ladder_key(LV), ladder().to_item())
    aws_stubs.ddb.add_response("delete_item", {})

    with pytest.raises(ToolError, match=r"\[45\] GOPs") as raised:
        schedule.publish(schedule.PublishInput(plan_id=PLAN_ID), CTX)
    assert raised.value.code == "NOT_FOUND"


def test_a_lease_taken_over_after_expiry_is_not_released(aws_stubs: SimpleNamespace) -> None:
    aws_stubs.ddb.add_response("put_item", {})
    aws_stubs.ddb.add_client_error("delete_item", "ConditionalCheckFailedException")
    with schedule.lease(CHANNEL, "me"):
        pass
    [put] = aws_stubs.calls.of("PutItem")
    assert put["Item"]["owner"] == "me" and put["Item"]["expires_at"] > time.time()


# --- remove_programs ------------------------------------------------------------------------------------------------


def test_remove_programs_leaves_every_other_program_at_its_start(aws_stubs: SimpleNamespace) -> None:
    a, b, c = (program(name, NOW + 600_000 + i * 1_200_000, plan_id="blk") for i, name in enumerate("abc"))
    on_air = program("on-air", NOW - 60_000)
    ddb = aws_stubs.ddb
    get_item(ddb, store.channel_key(CHANNEL), channel().to_item())
    ddb.add_response("put_item", {})  # lease
    query(ddb, on_air.to_item(), a.to_item(), b.to_item(), c.to_item())
    aws_stubs.mt.add_response("delete_program", {}, {"ChannelName": CHANNEL, "ProgramName": "b"})
    ddb.add_response("delete_item", {}, {"TableName": TABLE, "Key": b.key()})
    guide_written(aws_stubs, on_air, a, c)
    ddb.add_response("delete_item", {})  # lease

    payload = schedule.RemoveProgramsInput(channel=CHANNEL, programs=["b", "on-air", "zz", "b"])
    result = schedule.remove_programs(payload, CTX)

    assert result.removed == ["b"] and result.not_found == ["on-air", "zz"]
    assert [put["Item"]["sk"] for put in aws_stubs.calls.of("PutItem")] == ["LEASE"]  # c keeps its ABSOLUTE start
    assert "moved" not in result.model_dump()


# --- briefs ---------------------------------------------------------------------------------------------------------


def test_get_brief_falls_back_to_the_defaults(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.channel_key(CHANNEL), channel(brief=None).to_item())
    result = schedule.get_brief(schedule.ChannelInput(channel=CHANNEL), CTX)
    assert result.default and result.brief == timeline.ProgrammingBrief()


def test_update_brief_replaces_the_brief(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.channel_key(CHANNEL), channel().to_item())
    aws_stubs.ddb.add_response("put_item", {})
    brief = timeline.ProgrammingBrief(genres_deny=["horror"], break_policy=timeline.BreakPolicy(slate_gops=15))
    result = schedule.update_brief(schedule.UpdateBriefInput(channel=CHANNEL, brief=brief), CTX)
    assert result.brief == brief and not result.default
    [put] = aws_stubs.calls.of("PutItem")
    assert put["Item"]["brief"]["genres_deny"] == ["horror"] and "ConditionExpression" in put


def test_gateway_handler_serves_the_schedule_target(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.channel_key(CHANNEL), channel().to_item())
    context = SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "schedule___get_brief"}),
        aws_request_id="req-1",
        function_name="schedule",
        function_version="1",
        invoked_function_arn="arn:aws:lambda:us-east-1:111122223333:function:schedule",
        memory_limit_in_mb=512,
        log_group_name="g",
        log_stream_name="s",
        get_remaining_time_in_millis=lambda: 30_000,
    )
    response = schedule.handler({"channel": CHANNEL, "correlation_id": "session-1"}, cast("LambdaContext", context))
    assert response["ok"] is True and response["brief"]["ratings"] == ["TV-G", "TV-PG"]
