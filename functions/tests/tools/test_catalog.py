import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import Stubber

from fast_channels.core.catalog import ContentItem, ContentQuery
from fast_channels.functions import aws
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import _library_store as store
from fast_channels.functions.tools import catalog, registry
from fast_channels.functions.tools.registry import ToolContext, ToolError, gateway_tool_schema

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

REPO = Path(__file__).resolve().parents[3]
TABLE = "catalog-test"
CTX = ToolContext(tool="catalog___search_content", correlation_id="session-1")
_serialize = TypeSerializer().serialize

TITLES = [
    ContentItem(content_id="bbb", title="Big Buck Bunny", synopsis="A rabbit's revenge.", genres=["animation"]),
    ContentItem(content_id="sintel", title="Sintel", synopsis="A girl searches for her DRAGON.", rating="TV-14"),
    ContentItem(content_id="tos", title="Tears of Steel", genres=["sci-fi"], cue_points_ms=[300_000]),
]


def dynamo(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _serialize(v) for k, v in item.items()}


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CATALOG_TABLE", TABLE)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def ddb() -> Iterator[Stubber]:
    with Stubber(aws.table(TABLE).meta.client) as stub:
        yield stub
        stub.assert_no_pending_responses()


# --- contract: identical to contracts/catalog.openapi.yaml ----------------------------------------------------------


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    """The catalog contract as scripts/generate_contracts.py renders it (core/tests/test_contracts.py asserts that
    the committed YAML equals this rendering)."""
    spec = importlib.util.spec_from_file_location("generate_contracts", REPO / "scripts/generate_contracts.py")
    assert spec and spec.loader
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    return {"document": generator.catalog_openapi(), "schema": generator._gateway_schema}


def _operations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {op["operationId"]: op for path in document["paths"].values() for op in path.values()}


def _spec(name: str) -> registry.ToolSpec[Any, Any]:
    return registry.load("catalog")[name]


def _words(text: str) -> str:
    return " ".join(text.split())


def test_tools_are_exactly_the_contract_operations(openapi: dict[str, Any]) -> None:
    operations = _operations(openapi["document"])
    assert set(registry.load("catalog")) == set(operations)
    for name, operation in operations.items():
        assert _words(_spec(name).description) == operation["description"]
        assert _spec(name).access is registry.Access.READ


def test_search_content_schemas_match_the_contract(openapi: dict[str, Any]) -> None:
    operation = _operations(openapi["document"])["search_content"]
    spec, flatten = _spec("search_content"), openapi["schema"]
    assert spec.input is ContentQuery
    assert flatten(spec.input) == operation["requestBody"]["content"]["application/json"]["schema"]
    assert flatten(spec.output) == operation["responses"]["200"]["content"]["application/json"]["schema"]


def test_get_content_schemas_match_the_contract(openapi: dict[str, Any]) -> None:
    operation = _operations(openapi["document"])["get_content"]
    spec, flatten = _spec("get_content"), openapi["schema"]
    (parameter,) = operation["parameters"]
    schema = flatten(spec.input)
    assert schema["required"] == [parameter["name"]]
    assert schema["properties"][parameter["name"]] == {**parameter["schema"], "description": parameter["description"]}
    assert flatten(spec.output) == operation["responses"]["200"]["content"]["application/json"]["schema"]


def test_gateway_schemas_keep_the_contract_types(openapi: dict[str, Any]) -> None:
    body = _operations(openapi["document"])["search_content"]["requestBody"]["content"]["application/json"]["schema"]
    gateway = gateway_tool_schema(_spec("search_content"))["inputSchema"]["properties"]
    assert set(gateway) - {"correlation_id"} == set(body["properties"])
    for name, prop in body["properties"].items():
        assert gateway[name]["type"] == prop["type"]
        assert gateway[name].get("items", {}).get("type") == prop.get("items", {}).get("type")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_catalog_item_is_indexed_by_its_first_genre() -> None:
    item = catalog.catalog_item(TITLES[0])
    assert item["pk"] == "CONTENT#bbb" and item["sk"] == "META"
    assert (item["gsi1pk"], item["gsi1sk"]) == ("GENRE#animation", "Big Buck Bunny")
    assert "gsi1pk" not in catalog.catalog_item(TITLES[1])
    assert ContentItem.model_validate(item) == TITLES[0]


def test_search_filters_in_dynamodb_and_matches_words_case_insensitively(ddb: Stubber) -> None:
    expected_filter = (
        Attr("sk").eq("META")
        & (Attr("genres").contains("animation") | Attr("genres").contains("fantasy"))
        & Attr("rating").is_in(["TV-14"])
        & Attr("duration_ms").lte(600_000)
    )
    items = [dynamo(catalog.catalog_item(t)) for t in TITLES]
    ddb.add_response("scan", {"Items": items}, {"TableName": TABLE, "FilterExpression": expected_filter})
    query = ContentQuery(
        text="dragon Girl", genres=["animation", "fantasy"], ratings=["TV-14"], max_duration_minutes=10
    )
    page = catalog.search_content(query, CTX)
    assert [i.content_id for i in page.items] == ["sintel"] and page.next_token is None


def test_search_pages_with_next_token(ddb: Stubber) -> None:
    items = [dynamo(catalog.catalog_item(t)) for t in TITLES]
    ddb.add_response("scan", {"Items": items}, {"TableName": TABLE, "FilterExpression": Attr("sk").eq("META")})
    first = catalog.search_content(ContentQuery(limit=2), CTX)
    assert [i.content_id for i in first.items] == ["bbb", "sintel"]
    assert store.decode_token(first.next_token, store.TABLE_KEYS) == {"pk": "CONTENT#sintel", "sk": "META"}

    resumed = {
        "TableName": TABLE,
        "FilterExpression": Attr("sk").eq("META"),
        "ExclusiveStartKey": {"pk": "CONTENT#sintel", "sk": "META"},
    }
    ddb.add_response("scan", {"Items": [dynamo(catalog.catalog_item(TITLES[2]))]}, resumed)
    second = catalog.search_content(ContentQuery(limit=2, next_token=first.next_token), CTX)
    assert [i.content_id for i in second.items] == ["tos"] and second.next_token is None


def test_get_content(ddb: Stubber) -> None:
    key = {"pk": "CONTENT#tos", "sk": "META"}
    ddb.add_response("get_item", {"Item": dynamo(catalog.catalog_item(TITLES[2]))}, {"TableName": TABLE, "Key": key})
    assert catalog.get_content(catalog.GetContentInput(content_id="tos"), CTX) == TITLES[2]


def test_get_content_reports_unknown_titles(ddb: Stubber) -> None:
    ddb.add_response("get_item", {}, {"TableName": TABLE, "Key": {"pk": "CONTENT#nope", "sk": "META"}})
    with pytest.raises(ToolError) as raised:
        catalog.get_content(catalog.GetContentInput(content_id="nope"), CTX)
    assert raised.value.code == "NOT_FOUND"


def _lambda_context(tool: str) -> LambdaContext:
    fake = SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": tool}),
        aws_request_id="req-1",
        function_name="catalog",
        function_version="1",
        invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:catalog",
        memory_limit_in_mb=512,
        log_group_name="g",
        log_stream_name="s",
        get_remaining_time_in_millis=lambda: 1000,
    )
    return cast("LambdaContext", fake)


def test_lambda_handler_wraps_results_and_errors(ddb: Stubber) -> None:
    key = {"pk": "CONTENT#bbb", "sk": "META"}
    ddb.add_response("get_item", {"Item": dynamo(catalog.catalog_item(TITLES[0]))}, {"TableName": TABLE, "Key": key})
    ok = catalog.handler({"content_id": "bbb", "correlation_id": "s1"}, _lambda_context("catalog___get_content"))
    assert ok == {"ok": True, **TITLES[0].model_dump(mode="json", exclude_none=True)}

    invalid = catalog.handler({"content_id": "bad id"}, _lambda_context("catalog___get_content"))
    assert invalid["error"]["code"] == "VALIDATION"
    bad_token = catalog.handler({"next_token": "@@"}, _lambda_context("catalog___search_content"))
    assert bad_token["error"]["code"] == "VALIDATION"


def test_dynamodb_throttling_is_retryable(ddb: Stubber) -> None:
    ddb.add_client_error("get_item", "ThrottlingException", "slow down")
    result = catalog.handler({"content_id": "bbb"}, _lambda_context("catalog___get_content"))
    assert result["error"]["code"] == "THROTTLED" and result["error"]["retryable"] is True
