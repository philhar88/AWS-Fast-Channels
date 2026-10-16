"""Offline tests of the operator scripts (not marked e2e): stack lookup, seeding, tracing and cleanup, with botocore
Stubber standing in for AWS."""

import argparse
import importlib.util
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import _stack
import boto3
import httpx
import pytest
from _stack import Plane, StackError, Stage
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from fast_channels.core.cost import encode_cost
from fast_channels.core.ingest import IngestRequest
from fast_channels.core.ladder import Ladder
from fast_channels.functions.tools.catalog import catalog_item
from fast_channels.functions.tools.channels import STOP_CHANNEL_TARGET
from fast_channels.infra import stage as infra_stage
from fast_channels.infra.constructs.channel_assembly import AS_RUN_LOG_GROUP

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REGION = "us-east-1"
STANDARD = Ladder.builtin("standard")


def script(name: str) -> ModuleType:
    """Load ``scripts/<name>.py`` under a private name (``trace`` would shadow the standard library's module)."""
    spec = importlib.util.spec_from_file_location(f"fast_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


trace, seed, cleanup = script("trace"), script("seed_sample_content"), script("cleanup")


def session() -> boto3.Session:
    return boto3.Session(region_name=REGION, aws_access_key_id="test", aws_secret_access_key="test")


@pytest.fixture
def stubbed() -> Iterator[Any]:
    """``stubbed(client)`` -> an active Stubber whose responses must all be used."""
    stubbers: list[Stubber] = []

    def stub(client: Any) -> Stubber:
        stubber = Stubber(client)
        stubber.activate()
        stubbers.append(stubber)
        return stubber

    yield stub
    for stubber in stubbers:
        stubber.assert_no_pending_responses()


def stack(name: str, status: str = "CREATE_COMPLETE", **outputs: str) -> dict[str, Any]:
    entries = [{"OutputKey": key, "OutputValue": value} for key, value in outputs.items()]
    return {
        "Stacks": [
            {
                "StackName": name,
                "CreationTime": datetime(2026, 10, 16, tzinfo=UTC),
                "StackStatus": status,
                "Outputs": entries,
            }
        ]
    }


def missing(stubber: Stubber, name: str) -> None:
    stubber.add_client_error(
        "describe_stacks",
        "ValidationError",
        f"Stack with id {name} does not exist",
        400,
        expected_params={"StackName": name},
    )


# --- _stack --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["fast", "fastci", "fast-two"])
def test_stack_names_match_the_cdk_stage(prefix: str) -> None:
    assert _stack.stage_id(prefix) == infra_stage.stage_id(prefix)
    assert _stack.DEFAULT_PREFIX == infra_stage.DEFAULT_PREFIX


def test_configured_prefix(tmp_path: Path) -> None:
    cdk = tmp_path / "cdk.json"
    cdk.write_text(json.dumps({"context": {"fast-channels": {"prefix": "demo"}}}))
    assert _stack.configured_prefix(cdk) == "demo"
    cdk.write_text(json.dumps({"context": {}}))
    assert _stack.configured_prefix(cdk) == "fast"
    assert _stack.configured_prefix() == json.loads(_stack.CDK_JSON.read_text())["context"]["fast-channels"]["prefix"]


def test_region_resolves_like_the_cdk_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    assert _stack.cdk_session().region_name == "eu-west-1"
    monkeypatch.delenv("AWS_REGION")
    assert _stack.cdk_session().region_name == "us-west-2"


def test_outputs_are_read_once_and_explained_when_missing(stubbed: Any) -> None:
    stage = Stage("fastci", session())
    stubber = stubbed(stage.cloudformation)
    name = "FastChannels-fastci-Media"
    stubber.add_response(
        "describe_stacks", stack(name, Channels='[{"name": "fastci-demo"}]', Broken="{"), {"StackName": name}
    )
    assert stage.json_output(Plane.MEDIA, "Channels") == [{"name": "fastci-demo"}]
    with pytest.raises(StackError, match="has no output EventBusName \\(it has: Broken, Channels\\)"):
        stage.output(Plane.MEDIA, "EventBusName")
    with pytest.raises(StackError, match="output Broken of FastChannels-fastci-Media is not JSON"):
        stage.json_output(Plane.MEDIA, "Broken")


def test_a_missing_stack(stubbed: Any) -> None:
    stage = Stage("fastci", session())
    stubber = stubbed(stage.cloudformation)
    missing(stubber, "FastChannels-fastci-Data")
    missing(stubber, "FastChannels-fastci-Data")
    assert stage.status(Plane.DATA) is None
    with pytest.raises(
        StackError,
        match="FastChannels-fastci-Data does not exist in us-east-1: deploy the stage, or pass the --prefix",
    ):
        stage.outputs(Plane.DATA)


def test_other_cloudformation_errors_are_raised(stubbed: Any) -> None:
    stage = Stage("fastci", session())
    stubbed(stage.cloudformation).add_client_error("describe_stacks", "AccessDenied", "not allowed", 403)
    with pytest.raises(ClientError, match="not allowed"):
        stage.status(Plane.OPERATOR)


def test_run_turns_stack_errors_into_a_message() -> None:
    def broken() -> int:
        raise StackError("stack FastChannels-x-Data does not exist")

    with pytest.raises(SystemExit) as failed:
        _stack.run(broken)
    assert failed.value.code == "error: stack FastChannels-x-Data does not exist"
    with pytest.raises(SystemExit) as passed:
        _stack.run(lambda: 0)
    assert passed.value.code == 0


def test_prefix_argument() -> None:
    parser = argparse.ArgumentParser()
    _stack.add_prefix_argument(parser)
    assert parser.parse_args(["--prefix", "fastci"]).prefix == "fastci"
    assert parser.parse_args([]).prefix is None


# --- seed_sample_content -------------------------------------------------------------------------------------------


def library_table() -> Any:
    return session().resource("dynamodb").Table("library")


def test_the_sample_catalog_is_valid_and_can_be_subset() -> None:
    titles = seed.sample_titles(seed.CATALOG, [])
    assert [t.content_id for t in titles] == [
        "sintel",
        "tears-of-steel",
        "elephants-dream",
        "sintel-trailer",
    ]
    assert all(t.mezzanine_uri and t.duration_ms for t in titles)
    subset = seed.sample_titles(seed.CATALOG, ["sintel-trailer", "sintel"])
    assert [t.content_id for t in subset] == ["sintel", "sintel-trailer"]
    with pytest.raises(SystemExit, match="unknown titles nope"):
        seed.sample_titles(seed.CATALOG, ["nope"])


def test_titles_carry_an_ingest_request_and_their_library_status(stubbed: Any) -> None:
    table = library_table()
    stubber = stubbed(table.meta.client)
    [content] = seed.sample_titles(seed.CATALOG, ["sintel-trailer"])
    asset_id = content.content_id  # sample titles keep their content id as asset id
    item = {
        "pk": {"S": f"ASSET#{asset_id}"},
        "sk": {"S": f"LADDER#{STANDARD.version}"},
        "kind": {"S": "content"},
        "status": {"S": "READY"},
    }
    stubber.add_response("get_item", {"Item": item})
    stubber.add_response("get_item", {})
    ready = seed.title_of(table, content, STANDARD.version)
    fresh = seed.title_of(table, content, STANDARD.version)
    assert (ready.asset_id, ready.needs_ingest, fresh.needs_ingest) == (asset_id, False, True)
    request = fresh.request
    assert IngestRequest.model_validate(request.model_dump()) == request
    assert str(request.correlation_id).startswith("seed-sintel-trailer-")
    assert (request.cue_points_ms, request.metadata) == ([], {"title": "Sintel (trailer)"})


def test_the_plan_prices_only_what_is_encoded(stubbed: Any, capsys: pytest.CaptureFixture[str]) -> None:
    table = library_table()
    stubber = stubbed(table.meta.client)
    stubber.add_response(
        "get_item",
        {"Item": {"pk": {"S": "ASSET#x"}, "sk": {"S": "LADDER#y"}, "kind": {"S": "content"}, "status": {"S": "READY"}}},
    )
    stubber.add_response("get_item", {})
    contents = seed.sample_titles(seed.CATALOG, ["sintel", "sintel-trailer"])
    titles = [seed.title_of(table, content, STANDARD.version) for content in contents]
    total = seed.print_plan(titles, STANDARD)
    assert total == pytest.approx(encode_cost(STANDARD, 52_000))
    lines = capsys.readouterr().out.splitlines()
    assert "skipped" in lines[1] and lines[1].startswith("sintel ")
    assert lines[2].split()[1:] == ["0:52", "-", f"${total:.2f}"]


def test_mezzanines_are_uploaded_after_their_sidecar(stubbed: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sidecar (the whole request) lands first, then the streamed mezzanine: the upload starts the ingest."""
    s3 = session().client("s3")
    stubber = stubbed(s3)
    [content] = seed.sample_titles(seed.CATALOG, ["sintel-trailer"])
    request = IngestRequest(
        source_uri=str(content.mezzanine_uri),
        asset_id=content.content_id,
        content_id=content.content_id,
        ladder_version=STANDARD.version,
        correlation_id="seed-x",
    )
    sidecar = request.model_dump(mode="json", exclude={"source_uri", "generator"}, exclude_none=True)
    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": "ingest",
            "Key": "samples/sintel-trailer.mp4.json",
            "Body": json.dumps(sidecar).encode(),
            "ContentType": "application/json",
        },
    )
    uploaded: dict[str, Any] = {}

    def upload_fileobj(body: Any, bucket: str, key: str, **kwargs: Any) -> None:
        uploaded.update(bucket=bucket, key=key, body=body.read(), content_type=kwargs["ExtraArgs"]["ContentType"])

    monkeypatch.setattr(s3, "upload_fileobj", upload_fileobj)
    payload = b"\x00\x00\x00\x18ftypmp42" * 1000
    publisher = httpx.MockTransport(
        lambda _: httpx.Response(200, content=payload, headers={"content-type": "video/mp4"})
    )
    with httpx.Client(transport=publisher) as http:
        key = seed.upload(s3, http, "ingest", seed.Title(content, request, None))
    assert key == "samples/sintel-trailer.mp4"
    assert uploaded == {"bucket": "ingest", "key": key, "body": payload, "content_type": "video/mp4"}


def test_catalog_items_are_the_catalog_tools_own(stubbed: Any) -> None:
    table = session().resource("dynamodb").Table("catalog")
    stubber = stubbed(table.meta.client)
    [content] = seed.sample_titles(seed.CATALOG, ["sintel-trailer"])
    request = IngestRequest(source_uri=str(content.mezzanine_uri), ladder_version=STANDARD.version)
    linked = content.model_copy(update={"asset_ids": [request.resolved_asset_id]})
    item = catalog_item(linked)  # the resource API serialises after the stub sees the request
    stubber.add_response(
        "batch_write_item", {"UnprocessedItems": {}}, {"RequestItems": {"catalog": [{"PutRequest": {"Item": item}}]}}
    )
    seed.write_catalog(table, [seed.Title(content, request, None)])


def test_the_ladder_must_be_provisioned(stubbed: Any) -> None:
    table = library_table()
    stubber = stubbed(table.meta.client)
    key = {"pk": {"S": "LADDER#standard"}, "sk": {"S": f"V#{STANDARD.version}"}}
    stubber.add_response("get_item", {"Item": {**key, "status": {"S": "READY"}}})
    stubber.add_response("get_item", {"Item": {**key, "status": {"S": "FAILED"}}})
    stubber.add_response("get_item", {})
    assert seed.provisioned_ladder(table, STANDARD.version) == STANDARD
    with pytest.raises(SystemExit, match="is FAILED"):
        seed.provisioned_ladder(table, STANDARD.version)
    with pytest.raises(SystemExit, match="has the Media stack finished deploying"):
        seed.provisioned_ladder(table, STANDARD.version)


def test_progress_follows_the_execution_until_the_library_has_the_encode(stubbed: Any) -> None:
    table, sfn = library_table(), session().client("stepfunctions")
    dynamo, steps = stubbed(table.meta.client), stubbed(sfn)
    request = IngestRequest(
        source_uri="https://example.com/a.mp4", ladder_version=STANDARD.version, correlation_id="seed-a-1"
    )
    title = seed.Title(seed.sample_titles(seed.CATALOG, ["sintel"])[0], request, None)
    machine = "arn:aws:states:us-east-1:123456789012:stateMachine:fastci-ingest"
    started = {"executionArn": "arn", "stateMachineArn": machine, "startDate": datetime(2026, 10, 16, tzinfo=UTC)}

    def item(status: str, correlation_id: str = "seed-a-1", **extra: str) -> dict[str, Any]:
        attributes = {"kind": "content", "status": status, "correlation_id": correlation_id, **extra}
        return {
            "Item": {"pk": {"S": "ASSET#a"}, "sk": {"S": "LADDER#x"}, **{k: {"S": v} for k, v in attributes.items()}}
        }

    dynamo.add_response("get_item", {})
    steps.add_client_error("describe_execution", "ExecutionDoesNotExist")
    dynamo.add_response("get_item", item("READY", correlation_id="seed-older"))
    steps.add_client_error("describe_execution", "ExecutionDoesNotExist")
    dynamo.add_response("get_item", {})
    steps.add_response("describe_execution", {**started, "status": "RUNNING"})
    dynamo.add_response("get_item", {})
    steps.add_response("describe_execution", {**started, "status": "FAILED"})
    dynamo.add_response("get_item", item("INGESTING"))
    dynamo.add_response("get_item", item("FAILED", error="IngestRefusedError"))
    dynamo.add_response("get_item", item("READY"))
    progress = [
        seed.progress(table, sfn, machine, title, late=late)
        for late in (False, True, False, False, False, False, False)
    ]
    assert [(p.state, p.done, p.ready) for p in progress] == [
        ("requested", False, False),
        ("REJECTED: no execution started (scripts/trace.py seed-a-1)", True, False),
        ("execution RUNNING", False, False),
        ("execution FAILED", True, False),
        ("INGESTING", False, False),
        ("FAILED: IngestRefusedError", True, False),
        ("READY", True, True),
    ]


# --- trace ---------------------------------------------------------------------------------------------------------


def test_since() -> None:
    assert trace.since("90s") == timedelta(seconds=90)
    assert trace.since("6h") == timedelta(hours=6)
    assert trace.since("2d") == timedelta(days=2)
    with pytest.raises(argparse.ArgumentTypeError, match="not a duration"):
        trace.since("6 hours")


def test_query_string_matches_any_term() -> None:
    query = trace.query_string(["e2e-pattern", "evid=12"])
    assert '| filter @message like "e2e-pattern" or @message like "evid=12"\n' in query
    assert query.startswith("fields @timestamp, @log, @message") and query.endswith("| limit 10000")


def test_name_prefixes_of_a_deployment() -> None:
    prefixes = trace.name_prefixes(Stage("fast", session()))
    assert prefixes[:3] == ("FastChannels-Data-", "FastChannels-Media-", "FastChannels-Operator-")
    assert "/aws/vendedlogs/states/fast-" in prefixes
    assert "/aws/bedrock-agentcore/runtimes/fast_scheduler-" in prefixes
    assert trace.AS_RUN_LOG_GROUP == AS_RUN_LOG_GROUP in prefixes


def test_log_groups_by_name_and_tag(stubbed: Any) -> None:
    stage = Stage("fastci", session())
    logs, tagging = session().client("logs"), session().client("resourcegroupstaggingapi")
    by_name, by_tag = stubbed(logs), stubbed(tagging)
    for prefix in trace.name_prefixes(stage):
        groups = [{"logGroupName": f"{prefix}x"}] if prefix.startswith("/aws/vendedlogs/states/") else []
        by_name.add_response("describe_log_groups", {"logGroups": groups}, {"logGroupNamePrefix": prefix})
    arn = "arn:aws:logs:us-east-1:123456789012:log-group:FastChannels-fastci-Media-IngestPrepareLogs1A2B-xyz"
    by_tag.add_response("get_resources", {"ResourceTagMappingList": [{"ResourceARN": arn}]})
    assert trace.log_groups(stage, logs, tagging) == [
        "/aws/vendedlogs/states/fastci-x",
        "FastChannels-fastci-Media-IngestPrepareLogs1A2B-xyz",
    ]
    by_tag.add_client_error("get_resources", "AccessDeniedException")
    assert trace.tagged_log_groups(tagging, "fastci") == []


def test_search_merges_the_queries_in_time_order(stubbed: Any) -> None:
    logs = session().client("logs")
    stubber = stubbed(logs)
    start, end = datetime(2026, 10, 16, 11, tzinfo=UTC), datetime(2026, 10, 16, 12, tzinfo=UTC)
    groups = [f"group-{n:02d}" for n in range(60)]
    query = trace.query_string(["x"])
    for name, batch in (("q1", groups[:50]), ("q2", groups[50:])):
        request = {
            "logGroupNames": batch,
            "startTime": int(start.timestamp()),
            "endTime": int(end.timestamp()),
            "queryString": query,
            "limit": 10000,
        }
        stubber.add_response("start_query", {"queryId": name}, request)

    def row(when: str, group: str, message: str) -> list[dict[str, str]]:
        return [
            {"field": "@timestamp", "value": when},
            {"field": "@log", "value": f"123:{group}"},
            {"field": "@message", "value": message},
        ]

    stubber.add_response("get_query_results", {"status": "Running", "results": []}, {"queryId": "q1"})
    stubber.add_response(
        "get_query_results",
        {"status": "Complete", "results": [row("2026-10-16 11:30:00.500", "group-01", "late")]},
        {"queryId": "q1"},
    )
    stubber.add_response(
        "get_query_results",
        {"status": "Complete", "results": [row("2026-10-16 11:10:00.000", "group-55", "early")]},
        {"queryId": "q2"},
    )
    records = trace.search(logs, groups, ["x"], start=start, end=end)
    assert [(r.log_group, r.message) for r in records] == [("group-55", "early"), ("group-01", "late")]
    assert records[1].timestamp == datetime(2026, 10, 16, 11, 30, 0, 500000, UTC)


def test_lines_summarise_each_kind_of_log() -> None:
    when = datetime(2026, 10, 16, 12, 0, 0, 123000, UTC)
    powertools = json.dumps(
        {"level": "INFO", "message": "tool succeeded", "service": "tools.schedule", "correlation_id": "x"}
    )
    states = json.dumps({"type": "TaskStateEntered", "details": {"name": "Prepare", "input": "{}"}})
    ads = json.dumps({"eventType": "MAKING_ADS_REQUEST", "adsRequestUrl": "https://cdn/ads/x/vast.xml?evid=12"})
    assert trace.line(trace.Record(when, "FastChannels-x-Operator-ScheduleLogs-1", powertools), full=False) == (
        f"2026-10-16T12:00:00.123Z  {'tools.schedule':<34}  INFO | tool succeeded"
    )
    assert trace.line(trace.Record(when, "/aws/vendedlogs/states/x-ingest", states), full=False).endswith(
        f"{'states/x-ingest':<34}  TaskStateEntered | Prepare"
    )
    assert "MAKING_ADS_REQUEST | https://cdn/ads/x/vast.xml?evid=12" in trace.line(
        trace.Record(when, "g", ads), full=False
    )
    assert trace.line(trace.Record(when, "g", "START RequestId: 1\n  Version: $LATEST"), full=False).endswith(
        "START RequestId: 1 Version: $LATEST"
    )
    long = trace.summary({}, "x" * 500)
    assert len(long) == trace.SUMMARY_WIDTH and long.endswith("…")
    assert trace.line(trace.Record(when, "g", powertools), full=True).endswith(powertools)


def test_library_links_follow_assets_and_programs(stubbed: Any) -> None:
    table = library_table()
    stubber = stubbed(table.meta.client)
    encodes = [
        {
            "pk": {"S": "ASSET#a"},
            "sk": {"S": f"LADDER#{STANDARD.version}"},
            "kind": {"S": "content"},
            "status": {"S": "READY"},
            "correlation_id": {"S": "seed-a-1"},
        },
    ]
    program = {
        "pk": {"S": "CHANNEL#fast-demo"},
        "sk": {"S": "PROGRAM#1792120000000"},
        "name": {"S": "20261016T120000-a"},
        "asset_id": {"S": "a"},
        "vod_source": {"S": "a-standard"},
        "start_ms": {"N": "1792120000000"},
        "duration_ms": {"N": "100100"},
        "ladder_version": {"S": STANDARD.version},
        "status": {"S": "PUBLISHED"},
        "breaks": {
            "L": [
                {
                    "M": {
                        "offset_ms": {"N": "30030"},
                        "duration_ms": {"N": "30030"},
                        "slate_gops": {"N": "15"},
                        "event_id": {"N": "12"},
                        "upid": {"S": "FST3a"},
                    }
                }
            ]
        },
    }
    stubber.add_response("query", {"Items": encodes})
    stubber.add_response("query", {"Items": []})
    assert trace.library_links(table, "a") == ["seed-a-1"]
    stubber.add_response("query", {"Items": []})
    stubber.add_response("query", {"Items": [program]})
    assert trace.library_links(table, "20261016T120000-a") == ["evid=12"]


# --- cleanup -------------------------------------------------------------------------------------------------------


def arn(kind: str, name: str) -> str:
    return f"arn:aws:mediatailor:us-east-1:123456789012:{kind}/{name}"


def channel(name: str, app: str) -> dict[str, Any]:
    return {
        "Arn": arn("channel", name),
        "ChannelName": name,
        "ChannelState": "STOPPED",
        "Outputs": [],
        "PlaybackMode": "LINEAR",
        "Tier": "BASIC",
        "LogConfiguration": {"LogTypes": []},
        "Tags": {"fast:app": app},
    }


def location(name: str, app: str, owner: str) -> dict[str, Any]:
    tags = {"fast:app": app, "fast:owner": owner}
    return {
        "Arn": arn("sourceLocation", name),
        "HttpConfiguration": {"BaseUrl": "https://b.s3.amazonaws.com"},
        "SourceLocationName": name,
        "Tags": tags,
    }


def vod_source(location_name: str, name: str) -> dict[str, Any]:
    return {
        "Arn": arn("vodSource", f"{location_name}/{name}"),
        "HttpPackageConfigurations": [],
        "SourceLocationName": location_name,
        "VodSourceName": name,
    }


def test_inventory_lists_what_the_reaper_deletes_in_its_order(stubbed: Any) -> None:
    mediatailor, mediaconvert = session().client("mediatailor"), session().client("mediaconvert")
    mt, mc = stubbed(mediatailor), stubbed(mediaconvert)
    mt.add_response(
        "list_channels",
        {
            "Items": [
                channel("fastci-agent-kids", "fastci"),
                channel("fastci-agent-x", "other"),
                channel("fastci-demo", "fastci"),
            ]
        },
    )
    mt.add_response(
        "list_playback_configurations",
        {
            "Items": [
                {"Name": "fastci-agent-kids", "Tags": {"fast:app": "fastci"}},
                {"Name": "fastci-demo", "Tags": {"fast:app": "fastci"}},
            ]
        },
    )
    locations = [
        location("fastci-media", "fastci", "iac"),
        location("fastci-agent-byo", "fastci", "agent"),
        location("other-agent-byo", "other", "agent"),
    ]
    mt.add_response("list_source_locations", {"Items": locations})
    mt.add_response(
        "list_vod_sources", {"Items": [vod_source("fastci-media", "slate-15")]}, {"SourceLocationName": "fastci-media"}
    )
    mt.add_client_error(
        "list_vod_sources", "NotFoundException", expected_params={"SourceLocationName": "fastci-agent-byo"}
    )
    templates = [
        {"Name": "fastci-tiny_1a2b3c4d5e", "Arn": "arn:jt/1", "Settings": {}},
        {"Name": "fastci-standard_86bf8088e0", "Arn": "arn:jt/2", "Settings": {}},
    ]
    mc.add_response("list_job_templates", {"JobTemplates": templates}, {"Category": "fastci"})
    mc.add_response(
        "list_tags_for_resource",
        {"ResourceTags": {"Tags": {"fast:app": "fastci", "fast:owner": "agent"}}},
        {"Arn": "arn:jt/1"},
    )
    mc.add_response(
        "list_tags_for_resource",
        {"ResourceTags": {"Tags": {"fast:app": "fastci", "fast:owner": "iac"}}},
        {"Arn": "arn:jt/2"},
    )
    deployment = cleanup.reaper.Deployment(
        prefix="fastci", source_location="fastci-media", job_template_category="fastci", table_name=""
    )
    assert [(item.kind, item.name) for item in cleanup.inventory(deployment, mediatailor, mediaconvert)] == [
        ("agent channel", "fastci-agent-kids"),
        ("playback configuration", "fastci-agent-kids"),
        ("VOD source", "fastci-media/slate-15"),
        ("source location", "fastci-agent-byo"),
        ("job template", "fastci-tiny_1a2b3c4d5e"),
    ]


def test_the_iac_source_location_is_found_by_its_tags(stubbed: Any) -> None:
    mediatailor = session().client("mediatailor")
    stubber = stubbed(mediatailor)
    stubber.add_response(
        "list_source_locations",
        {"Items": [location("fastci-agent-byo", "fastci", "agent"), location("fastci-media", "fastci", "iac")]},
    )
    stubber.add_response("list_source_locations", {"Items": []})
    assert cleanup.iac_source_location(mediatailor, "fastci") == "fastci-media"
    assert cleanup.iac_source_location(mediatailor, "fastci") == "fastci-media"  # gone: the name the stack gave it


def test_auto_stop_schedules_of_the_deployment(stubbed: Any) -> None:
    scheduler = session().client("scheduler")
    stubber = stubbed(scheduler)
    groups = [
        {"Name": "fastci-auto-stop", "Arn": "arn:group/ours"},
        {"Name": "other-auto-stop", "Arn": "arn:group/theirs"},
    ]
    stubber.add_response("list_schedule_groups", {"ScheduleGroups": groups})
    stubber.add_response(
        "list_tags_for_resource", {"Tags": [{"Key": "fast:app", "Value": "fastci"}]}, {"ResourceArn": "arn:group/ours"}
    )
    schedules = [
        {"Name": "fastci-demo-auto-stop", "Target": {"Arn": STOP_CHANNEL_TARGET}},
        {
            "Name": "fastci-demo-autopilot",
            "Target": {"Arn": "arn:aws:lambda:us-east-1:123456789012:function:autopilot"},
        },
    ]
    stubber.add_response("list_schedules", {"Schedules": schedules}, {"GroupName": "fastci-auto-stop"})
    stubber.add_response(
        "list_tags_for_resource", {"Tags": [{"Key": "fast:app", "Value": "other"}]}, {"ResourceArn": "arn:group/theirs"}
    )
    found = list(cleanup.auto_stops(Stage("fastci", session()), scheduler))
    assert found == [cleanup.Leftover(cleanup.AUTO_STOP, "fastci-auto-stop/fastci-demo-auto-stop")]
    stubber.add_response("delete_schedule", {}, {"GroupName": "fastci-auto-stop", "Name": "fastci-demo-auto-stop"})
    stubber.add_client_error("delete_schedule", "ResourceNotFoundException")
    cleanup.delete_schedules(scheduler, found * 2)


def test_reap_repeats_the_reaper_until_nothing_is_left(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    passes = iter([["vodSource/fastci-media/a"], []])

    def reap(deployment: Any, budget: Any) -> list[str]:
        assert budget.context.get_remaining_time_in_millis() > 0
        budget.count("VodSources")
        return next(passes)

    monkeypatch.setattr(cleanup.reaper, "reap", reap)
    monkeypatch.setattr(cleanup.time, "sleep", lambda _: None)
    deployment = cleanup.reaper.Deployment(
        prefix="fastci", source_location="fastci-media", job_template_category="fastci", table_name=""
    )
    assert cleanup.reap(deployment) == []
    assert capsys.readouterr().out.splitlines() == [
        "deleted 1 VodSources; 1 pending",
        "deleted 1 VodSources; 0 pending",
    ]


def test_apply_is_refused_while_the_stage_serves(stubbed: Any) -> None:
    stage = Stage("fastci", session())
    stubber = stubbed(stage.cloudformation)
    stubber.add_response("describe_stacks", stack("FastChannels-fastci-Media", "UPDATE_COMPLETE"))
    stubber.add_response("describe_stacks", stack("FastChannels-fastci-Operator", "DELETE_FAILED"))
    assert cleanup.deployed_stacks(stage) == ["FastChannels-fastci-Media"]
    missing(stubber, "FastChannels-fastci-Media")
    missing(stubber, "FastChannels-fastci-Operator")
    assert cleanup.deployed_stacks(stage) == []


def test_the_deadline_counts_down() -> None:
    deadline = cleanup.Deadline(60)
    assert 0 < deadline.get_remaining_time_in_millis() <= 60_000
    assert cleanup.Deadline(-1).get_remaining_time_in_millis() == 0
