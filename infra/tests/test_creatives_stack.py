"""The creatives module (M5): the registry, the quarantine bucket and the readiness state machine, beside the
guidance; the ingest pipeline's read of the quarantine; and the own book and selling hooks."""

import json
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.creatives import names
from fast_channels.core.ladder import Ladder

from .test_selling_stack import EXTRA, SELLING, resources

if TYPE_CHECKING:
    from aws_cdk.assertions import Template
    from syrupy.assertion import SnapshotAssertion

type GetDeployment = Callable[..., Any]

CREATIVES: dict[str, Any] = {
    "prefix": "fastm",
    "modules.ops.enabled": True,
    "modules.demand.enabled": True,
    "modules.demand.simulator": "lambda",
    "modules.pds.enabled": True,
    "modules.creatives.enabled": True,
}
EVERY: dict[str, Any] = {**SELLING, "modules.crawl.enabled": True, "modules.creatives.enabled": True}
"""Every module beside the guidance (M2, M3, M5, M7, M8, M17)."""
WRITES: frozenset[str] = frozenset({"s3:PutObject", "s3:Put*", "s3:*", "*"})


def readiness_function(template: Template) -> dict[str, Any]:
    [function] = [
        f
        for f in resources(template, "AWS::Lambda::Function").values()
        if f.get("Handler") == "fast_channels.functions.creatives.readiness.handler"
    ]
    return function


def test_the_creatives_stack_exists_only_with_the_module_beside_the_guidance(deployment: GetDeployment) -> None:
    assert deployment(CREATIVES, extra=EXTRA).stage.creatives is not None
    assert deployment(CREATIVES).stage.creatives is None  # synthesized only with the guidance's network
    assert deployment({**CREATIVES, "modules.creatives.enabled": False}, extra=EXTRA).stage.creatives is None


def test_the_registry_and_the_quarantine_are_the_modules(deployment: GetDeployment) -> None:
    d = deployment(CREATIVES, extra=EXTRA)
    template = d.template(d.stage.creatives)
    [table] = resources(template, "AWS::DynamoDB::GlobalTable").values()
    assert table["TableName"] == "fastm-creatives"
    assert [index["IndexName"] for index in table["GlobalSecondaryIndexes"]] == [names.STATE_INDEX]
    [bucket] = resources(template, "AWS::S3::Bucket").values()
    assert bucket["BucketName"] == names.quarantine_bucket("fastm", "123456789012", "us-east-1")
    rules = {rule["Id"]: rule for rule in bucket["LifecycleConfiguration"]["Rules"]}
    assert rules["sources"]["Prefix"] == names.INCOMING and rules["sources"]["ExpirationInDays"] == 30


def test_the_readiness_function_conditions_to_the_default_ladder(deployment: GetDeployment) -> None:
    d = deployment({**CREATIVES, "modules.creatives.min_confidence": 90}, extra=EXTRA)
    environment = readiness_function(d.template(d.stage.creatives))["Environment"]["Variables"]
    assert Ladder.model_validate_json(environment["CREATIVES_LADDER"]) == Ladder.builtin("standard")
    assert (environment["CREATIVES_MODERATION"], environment["CREATIVES_MIN_CONFIDENCE"]) == ("true", "90")
    assert environment["PDS_CREATIVES_KEY"] == names.READY_KEY


def test_the_state_machine_conditions_with_the_stages_ingest_pipeline(deployment: GetDeployment) -> None:
    d = deployment(CREATIVES, extra=EXTRA)
    template = d.template(d.stage.creatives)
    [machine] = resources(template, "AWS::StepFunctions::StateMachine").values()
    assert machine["StateMachineName"] == "fastm-creatives"
    definition = json.dumps(machine["DefinitionString"])
    assert "states:::states:startExecution.sync:2" in definition
    assert "stateMachine:fastm-ingest" in definition
    policies = json.dumps(resources(template, "AWS::IAM::Policy"))
    assert "rekognition:StartContentModeration" in policies


def test_the_ingest_pipeline_reads_the_quarantine_only_with_the_module(deployment: GetDeployment) -> None:
    def media_policies(overrides: dict[str, Any]) -> str:
        d = deployment(overrides, extra=EXTRA)
        return json.dumps(resources(d.template(d.stage.media), "AWS::IAM::Policy"))

    assert "fastm-creatives-" in media_policies(CREATIVES)
    assert "fastm-creatives-" not in media_policies({**CREATIVES, "modules.creatives.enabled": False})


def test_the_own_book_and_the_sync_read_the_ready_creatives_only_with_the_module(deployment: GetDeployment) -> None:
    def environment(overrides: dict[str, Any], stack: str, handler: str) -> dict[str, str]:
        d = deployment(overrides, extra=EXTRA)
        functions = resources(d.template(getattr(d.stage, stack)), "AWS::Lambda::Function").values()
        [f] = [f for f in functions if f.get("Handler") == handler]
        return dict(f["Environment"]["Variables"])

    publisher, sync = "fast_channels.functions.pds.publisher.handler", "fast_channels.functions.selling.sync.handler"
    with_both = {**SELLING, "modules.creatives.enabled": True}
    assert environment(with_both, "pds", publisher)["PDS_CREATIVES_KEY"] == names.READY_KEY
    assert environment(with_both, "selling", sync)["PDS_CREATIVES_KEY"] == names.READY_KEY
    assert "PDS_CREATIVES_KEY" not in environment(SELLING, "pds", publisher)
    assert "PDS_CREATIVES_KEY" not in environment(SELLING, "selling", sync)
    d = deployment(with_both, extra=EXTRA)
    assert d.stage.creatives in d.stage.selling.dependencies


def _statements(template: Template) -> Iterator[dict[str, Any]]:
    for kind in ("AWS::IAM::Policy", "AWS::IAM::Role"):
        for properties in resources(template, kind).values():
            documents = [properties.get("PolicyDocument")] + [
                p["PolicyDocument"] for p in properties.get("Policies", [])
            ]
            for document in filter(None, documents):
                yield from document["Statement"]


@pytest.mark.parametrize(
    "stack", ["creatives", "demand", "demand_build", "demand_edge", "pds", "release", "selling", "crawl"]
)
def test_no_role_of_the_modules_may_write_to_the_ingest_bucket(deployment: GetDeployment, stack: str) -> None:
    """The ingest bucket's upload rule ingests any video under any key (a probe once created 63 stray assets), so
    generated, simulator and buyer video goes to the quarantine or the media bucket's ads/ (DESIGN-v2.2 §11)."""
    d = deployment(EVERY, extra=EXTRA)
    ingest = d.template(d.stage.data).find_resources("AWS::S3::Bucket")
    [ingest_id] = [name for name in ingest if "Ingest" in name]
    for statement in _statements(d.template(getattr(d.stage, stack))):
        actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        if statement["Effect"] == "Allow" and WRITES & set(actions):
            assert ingest_id not in json.dumps(statement.get("Resource")), (stack, statement)
            assert "IngestBucket" not in json.dumps(statement.get("Resource")), (stack, statement)


def test_template_snapshot(
    deployment: GetDeployment, normalized: Callable[..., Any], snapshot_json: SnapshotAssertion
) -> None:
    """What the module adds: the Creatives stack."""
    d = deployment(CREATIVES, extra=EXTRA)

    assert normalized(d.template(d.stage.creatives)) == snapshot_json
