"""Generate ``contracts/`` from the pydantic models in ``fast_channels.core``.

    uv run python scripts/generate_contracts.py          # write the files
    uv run python scripts/generate_contracts.py --check  # exit 1 if they are out of date

Outputs: ``ingest-request.schema.json``, ``events.schema.json``, ``model-registry.schema.json`` and
``catalog-context.schema.json`` (JSON Schema 2020-12), ``catalog.openapi.yaml`` (OpenAPI 3.1, the bring-your-own-CMS
contract) and ``catalog.gateway.openapi.json`` (the same contract as an AgentCore Gateway OpenAPI target reads it).
The OpenAPI documents are flattened for Gateway targets: every schema is inline, optional fields drop their ``null``
branch, and no ``$ref``/``anyOf``/``oneOf``/``allOf`` remains (agentcore.md §5.2); operationIds become the tool
names.

``pds-policy.schema.json`` and ``pds-select.schema.json`` are the Pod Decision Service contract with the fast-ctv
Prebid Server module (requests as the PDS accepts them, responses as it writes them). The examples under
``fixtures/pds/<call>/`` are shared with the module's tests: their ``description``, ``context`` and ``request`` are
written by hand, and their ``response`` is what the PDS rules answer, regenerated here.
"""

import argparse
import json
import re
import sys
from collections.abc import Iterator
from ipaddress import ip_network
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic.json_schema import models_json_schema

from fast_channels.core.catalog import CONTENT_ID_PATTERN, ContentItem, ContentPage, ContentQuery
from fast_channels.core.crawl.catalog_context import CatalogContext
from fast_channels.core.events import DETAIL_TYPES
from fast_channels.core.ingest import IngestRequest
from fast_channels.core.model_registry import SCHEDULER_TASK, TaskEntry
from fast_channels.core.pds.models import PolicyRequest, PolicyResponse, SelectRequest, SelectResponse
from fast_channels.core.pds.pod import PodContext, select_pod, select_response
from fast_channels.core.pds.policy import Demand, decide

if TYPE_CHECKING:
    from pydantic import BaseModel

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
FIXTURES = CONTRACTS / "fixtures" / "pds"
"""Hand-written inputs of the PDS examples (read from the repository even when ``CONTRACTS`` points elsewhere)."""
JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
VERSION = "3.0.0"


def ingest_request_schema() -> dict[str, Any]:
    """JSON Schema of the IngestPipeline input (S3 sidecar, ``Ingest Requested`` event or StartExecution)."""
    return {"$schema": JSON_SCHEMA, **IngestRequest.model_json_schema()}


def catalog_context_schema() -> dict[str, Any]:
    """JSON Schema of the catalog context object ``catalog/ctx/v1/<content_id>.json`` (immutable: v2 is a new file)."""
    return {"$schema": JSON_SCHEMA, **CatalogContext.model_json_schema(mode="serialization")}


def events_schema() -> dict[str, Any]:
    """JSON Schema of every EventBridge event the solution publishes, keyed by detail-type."""
    models: list[type[BaseModel]] = list(DETAIL_TYPES)
    refs, definitions = models_json_schema([(m, "validation") for m in models], ref_template="#/$defs/{model}")
    events = [
        {
            "type": "object",
            "required": ["source", "detail-type", "detail"],
            "properties": {
                "source": {"type": "string", "pattern": r"^fast-channels\..+$"},
                "detail-type": {"const": DETAIL_TYPES[model]},
                "detail": refs[model, "validation"],
            },
        }
        for model in models
    ]
    return {
        "$schema": JSON_SCHEMA,
        "title": "FAST Channels events",
        "description": "EventBridge events on the fast-channels bus; source is fast-channels.<prefix>.",
        "oneOf": events,
        **definitions,
    }


def model_registry_schema() -> dict[str, Any]:
    """JSON Schema of a model-registry entry, the SSM document ``/<prefix>/models/tasks/<task>`` that the agent and
    other model callers read; the examples are the shipped ``scheduler.plan`` entry (us-east-1) and a viewer-data
    task."""
    examples = [
        TaskEntry(model="us.anthropic.claude-haiku-5-5"),
        TaskEntry.model_validate(
            {
                "model": "us.anthropic.claude-haiku-5-5",
                "effort": "low",
                "max_tokens": 8000,
                "data_class": "viewer",
                "daily_cap_usd": 30,
            }
        ),
    ]
    return {
        "$schema": JSON_SCHEMA,
        **TaskEntry.model_json_schema(),
        "title": "FAST Channels model-registry entry",
        "description": (
            "SSM parameter /<prefix>/models/tasks/<task> (String, JSON), one per LLM task; "
            f"{SCHEDULER_TASK} is the scheduling agent's. The model must be one of the deployment's allowed models; "
            "readers refuse an entry that is invalid or names any other model."
        ),
        "examples": [entry.model_dump(mode="json") for entry in examples],
    }


def pds_schema(call: str, description: str, request: type[BaseModel], response: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema of one PDS call: the request as the PDS validates it, the response as it serializes it."""
    refs, definitions = models_json_schema(
        [(request, "validation"), (response, "serialization")], ref_template="#/$defs/{model}"
    )
    return {
        "$schema": JSON_SCHEMA,
        "title": f"Pod Decision Service {call}, contract version 1",
        "description": description,
        "oneOf": [refs[request, "validation"], refs[response, "serialization"]],
        **definitions,
    }


def pds_policy_schema() -> dict[str, Any]:
    """``POST /policy``: the module's request and the patch the PDS answers with."""
    return pds_schema(
        "/policy",
        "The fast-ctv module asks before each auction; it applies the answer verbatim to the bid request.",
        PolicyRequest,
        PolicyResponse,
    )


def pds_select_schema() -> dict[str, Any]:
    """``POST /select``: every bid of the auction and the pod the PDS selects."""
    return pds_schema(
        "/select",
        "The fast-ctv module sends every bid after the auction and renders exactly the pod the answer names.",
        SelectRequest,
        SelectResponse,
    )


def pds_fixture_response(call: str, document: dict[str, Any]) -> dict[str, Any]:
    """The PDS answer to a fixture's request, given the fixture's PDS-side context."""
    context = document["context"]
    match call:
        case "policy":
            decision = decide(
                PolicyRequest.model_validate(document["request"]),
                Demand(context["demand"]),
                origin_facing=[ip_network(network) for network in context["origin_facing"]],
                kids_channel=context["kids_channel"],
            )
            return decision.response.model_dump(mode="json")
        case "select":
            request = SelectRequest.model_validate(document["request"])
            pod = select_pod(request, PodContext.model_validate(context["pod"]))
            response = select_response(request, pod, tracker_base=context["tracker_base"], enr=context["enr"])
            return response.model_dump(mode="json")
        case _:
            raise ValueError(f"no PDS call {call!r} (fixtures/pds/<policy|select>/*.json)")


def pds_fixtures() -> dict[str, dict[str, Any]]:
    """Every PDS example with its response regenerated, by path under ``contracts/``."""
    fixtures = {}
    for path in sorted(FIXTURES.glob("*/*.json")):
        document = json.loads(path.read_text())
        document["response"] = pds_fixture_response(path.parent.name, document)
        fixtures[path.relative_to(FIXTURES.parents[1]).as_posix()] = document
    return fixtures


def _flatten(schema: Any, definitions: dict[str, Any]) -> Any:
    """Inline ``$ref``s, collapse ``anyOf: [X, null]`` to ``X`` and drop pydantic's ``title`` noise."""
    if isinstance(schema, list):
        return [_flatten(item, definitions) for item in schema]
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:  # the referencing schema's own keywords (a property's description) win
        siblings = {k: v for k, v in schema.items() if k != "$ref"}
        return _flatten(definitions[schema["$ref"].rsplit("/", 1)[-1]] | siblings, definitions)
    if "anyOf" in schema:  # only ``X | None`` unions exist in the catalog models
        (branch,) = [s for s in schema["anyOf"] if s != {"type": "null"}]
        return _flatten(branch | {k: v for k, v in schema.items() if k != "anyOf"}, definitions)
    flat: dict[str, Any] = {}
    for key, value in schema.items():
        if key in {"title", "$defs"} or (key == "default" and value is None):
            continue
        if key == "properties":  # property names are data, not schema keywords: keep every one
            flat[key] = {name: _flatten(prop, definitions) for name, prop in value.items()}
        else:
            flat[key] = _flatten(value, definitions)
    return flat


def _gateway_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    flat: dict[str, Any] = _flatten(schema, schema.get("$defs", {}))
    return flat


def _json(model: type[BaseModel], description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": _gateway_schema(model)}}}


def catalog_openapi() -> dict[str, Any]:
    """OpenAPI 3.1 contract a CMS implements to replace the reference catalog (same tool names)."""
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "FAST Channels catalog",
            "version": VERSION,
            "description": (
                "Bring-your-own-CMS contract. Implement these two operations and register the API as "
                "the AgentCore Gateway 'catalog' target; the scheduling agent then searches your "
                "catalog with the same tools it uses for the reference catalog."
            ),
        },
        "servers": [{"url": "https://cms.example.com/fast/v1", "description": "Replace with your CMS API."}],
        "security": [{"apiKey": []}],
        "paths": {
            "/content/search": {
                "post": {
                    "operationId": "search_content",
                    "summary": "Search the catalog",
                    "description": (
                        "Find titles to schedule. Filters combine with AND; genres and ratings match any "
                        "listed value. Results are paged with next_token."
                    ),
                    "requestBody": {"required": True, **_json(ContentQuery, "Search criteria.")},
                    "responses": {"200": _json(ContentPage, "One page of matching titles.")},
                }
            },
            "/content/{content_id}": {
                "get": {
                    "operationId": "get_content",
                    "summary": "Get one title",
                    "description": "Full metadata of one title, including cue points, rights window and assets.",
                    "parameters": [
                        {
                            "name": "content_id",
                            "in": "path",
                            "required": True,
                            "description": "Catalog content id.",
                            "schema": {"type": "string", "pattern": CONTENT_ID_PATTERN},
                        }
                    ],
                    "responses": {"200": _json(ContentItem, "The title."), "404": {"description": "No such title."}},
                }
            },
        },
        "components": {"securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "x-api-key"}}},
    }


def catalog_gateway_openapi() -> dict[str, Any]:
    """:func:`catalog_openapi` as a Gateway OpenAPI target takes it: JSON, with no specification-level security.

    The CDK L2 (``ApiSchema.from_local_asset``) rejects YAML and a top-level ``security``; the Gateway's outbound
    credential provider sends the API key instead. Only ``servers`` needs editing before use.
    """
    document = catalog_openapi()
    del document["security"], document["components"]
    return document


_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _yaml(value: Any, indent: int = 0) -> Iterator[str]:
    """Block-style YAML; scalars and unusual keys are JSON-quoted, which YAML 1.2 reads verbatim."""
    pad = "  " * indent
    items = value.items() if isinstance(value, dict) else ((None, v) for v in value)
    for key, item in items:
        head = f"{pad}- " if key is None else f"{pad}{key if _PLAIN_KEY.match(key) else json.dumps(key)}:"
        if isinstance(item, dict | list) and item:
            nested = list(_yaml(item, indent + 1))
            if key is None:
                yield head + nested[0].lstrip()
                yield from nested[1:]
            else:
                yield head
                yield from nested
        else:
            yield f"{head}{'' if key is None else ' '}{json.dumps(item)}"


def render() -> dict[str, str]:
    """Contents of every contract file, by file name."""

    def as_json(document: dict[str, Any]) -> str:
        return json.dumps(document, indent=2) + "\n"

    header = "# Generated by scripts/generate_contracts.py from fast_channels.core.catalog. Do not edit.\n"
    return {
        "ingest-request.schema.json": as_json(ingest_request_schema()),
        "events.schema.json": as_json(events_schema()),
        "model-registry.schema.json": as_json(model_registry_schema()),
        "catalog-context.schema.json": as_json(catalog_context_schema()),
        "catalog.openapi.yaml": header + "\n".join(_yaml(catalog_openapi())) + "\n",
        "catalog.gateway.openapi.json": as_json(catalog_gateway_openapi()),
        "pds-policy.schema.json": as_json(pds_policy_schema()),
        "pds-select.schema.json": as_json(pds_select_schema()),
        **{name: as_json(document) for name, document in pds_fixtures().items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed files are out of date")
    args = parser.parse_args(argv)
    stale = []
    for name, text in render().items():
        path = CONTRACTS / name
        if args.check:
            if not path.exists() or path.read_text() != text:
                stale.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    if stale:
        print(f"out of date: {', '.join(stale)}; run scripts/generate_contracts.py", file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
