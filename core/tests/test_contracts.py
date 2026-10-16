"""contracts/ is generated from the core models and must stay in sync with them."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from fast_channels.core.catalog import ContentItem, ContentQuery
from fast_channels.core.events import DETAIL_TYPES

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_contracts", REPO / "scripts/generate_contracts.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_contracts_are_current(generator: ModuleType) -> None:
    for name, text in generator.render().items():
        assert (REPO / "contracts" / name).read_text() == text, f"run scripts/generate_contracts.py ({name})"
    assert generator.main(["--check"]) == 0


def test_check_mode_reports_stale_files(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(generator, "CONTRACTS", tmp_path)
    assert generator.main(["--check"]) == 1
    assert "out of date" in capsys.readouterr().err
    assert generator.main([]) == 0
    assert generator.main(["--check"]) == 0


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def test_openapi_is_gateway_compatible(generator: ModuleType) -> None:
    document = generator.catalog_openapi()
    assert not _keys(document) & {"$ref", "anyOf", "oneOf", "allOf", "$defs"}
    operations = [op["operationId"] for path in document["paths"].values() for op in path.values()]
    assert operations == ["search_content", "get_content"]
    search = document["paths"]["/content/search"]["post"]
    query = search["requestBody"]["content"]["application/json"]["schema"]
    assert set(query["properties"]) == set(ContentQuery.model_fields)
    page = search["responses"]["200"]["content"]["application/json"]["schema"]
    assert set(page["properties"]["items"]["items"]["properties"]) == set(ContentItem.model_fields)
    assert page["properties"]["items"]["items"]["properties"]["rating"] == {
        "type": "string",
        "description": "Parental rating, e.g. TV-G.",
    }


def test_yaml_emitter_round_trips(generator: ModuleType) -> None:
    yaml = pytest.importorskip("yaml")
    text = generator.render()["catalog.openapi.yaml"]
    assert yaml.safe_load(text) == json.loads(json.dumps(generator.catalog_openapi()))
    sample: dict[str, Any] = {"a": [{"b": 1, "c": [1, [2, 3]]}, []], "path/x": {}, "n": None, "t": True}
    assert yaml.safe_load("\n".join(generator._yaml(sample))) == sample


def test_json_schemas(generator: ModuleType) -> None:
    events = generator.events_schema()
    assert [e["properties"]["detail-type"]["const"] for e in events["oneOf"]] == list(DETAIL_TYPES.values())
    ingest = generator.ingest_request_schema()
    assert ingest["title"] == "IngestRequest" and "ladder_version" in ingest["required"]
