import json
import secrets
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from fast_channels.core.cost import encode_cost_per_minute
from fast_channels.core.ladder import Ladder, LadderOps, propose
from fast_channels.functions import aws
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import encoding
from fast_channels.functions.tools import library_store as store
from fast_channels.functions.tools.registry import ToolContext, ToolError

TABLE = "library-test"
QUEUE = "arn:aws:mediaconvert:us-east-1:111122223333:queues/fast"
PROVISIONER = "arn:aws:states:us-east-1:111122223333:stateMachine:fast-ladder-provisioner"
STANDARD = Ladder.builtin("standard")
UP_TO_720 = LadderOps(max_height=720)
NOTHING_LEFT = LadderOps(max_height=200)
CTX = ToolContext(tool="encoding___provision_ladder", correlation_id="fast-console-session-1")
READ_CTX = ToolContext(tool="encoding___list_ladders", correlation_id="fast-console-session-1")
_typed = TypeSerializer().serialize


def decimals(value: Any) -> Any:
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
    return {k: _typed(decimals(v)) for k, v in item.items()}


@dataclass
class Calls:
    params: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def __call__(self, params: dict[str, Any], event_name: str, **_: Any) -> None:
        self.params.append((event_name.rsplit(".", 1)[-1], params))

    def of(self, operation: str) -> list[dict[str, Any]]:
        return [p for name, p in self.params if name == operation]


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in {
        "FAST_PREFIX": "fast",
        "LIBRARY_TABLE": TABLE,
        "MEDIACONVERT_QUEUE_ARN": QUEUE,
        "PROVISION_STATE_MACHINE_ARN": PROVISIONER,
    }.items():
        monkeypatch.setenv(name, value)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def aws_stubs() -> Iterator[SimpleNamespace]:
    clients = {
        "ddb": aws.table(TABLE).meta.client,
        "mediaconvert": aws.client("mediaconvert"),
        "sfn": aws.client("stepfunctions"),
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


def get_item(stub: Stubber, key: dict[str, str], item: dict[str, Any] | None = None) -> None:
    stub.add_response("get_item", {"Item": dynamo(item)} if item else {}, {"TableName": TABLE, "Key": key})


def standard(**changes: Any) -> store.LadderRecord:
    kit = store.LadderKit(
        filler="fast-filler", break_slates={30: "fast-slate-30", 15: "fast-slate-15"}, house_ads=["h1"]
    )
    return store.LadderRecord(**{"ladder_version": STANDARD.version, "status": "READY", "kit": kit} | changes)


def proposal_item(ops: LadderOps = UP_TO_720, *, expires_in: int = 600) -> tuple[dict[str, Any], Ladder | None]:
    proposal = propose(STANDARD, ops)
    stored = encoding.StoredProposal(
        proposal_id="proposal-1",
        base_version=STANDARD.version,
        ladder=proposal.ladder,
        ops=ops,
        errors=proposal.errors,
        correlation_id="s",
        expires_at=int(time.time()) + expires_in,
    )
    return {**store.proposal_key("proposal-1"), **stored.model_dump(mode="json")}, proposal.ladder


# --- list / get -----------------------------------------------------------------------------------------------------


def test_list_ladders_summarises_each_version_and_its_kit(aws_stubs: SimpleNamespace) -> None:
    derived = propose(STANDARD, UP_TO_720).ladder
    assert derived is not None
    records = [
        standard(),
        store.LadderRecord(ladder_version=derived.version, status="PROVISIONING", ladder=derived, owner="agent"),
        store.LadderRecord(ladder_version="gone@0123456789", status="FAILED"),
    ]
    aws_stubs.ddb.add_response("query", {"Items": [dynamo(r.to_item()) for r in records]})

    result = encoding.list_ladders(encoding.ListLaddersInput(), READ_CTX)

    first, second, broken = result.model_dump(mode="json")["ladders"]
    assert first["ladder_version"] == STANDARD.version and first["status"] == "READY"
    assert first["max_height"] == 1080
    assert first["renditions"] == len(STANDARD.video)
    assert first["cost_per_minute_usd"] == encode_cost_per_minute(STANDARD)
    assert first["kit"] == {
        "filler": "fast-filler",
        "break_slates": ["fast-slate-15", "fast-slate-30"],
        "house_ads": ["h1"],
    }
    assert second["max_height"] == 720 and second["owner"] == "agent"
    assert broken["max_height"] is None and broken["cost_per_minute_usd"] is None
    assert "codecs" not in first


def test_get_ladder_returns_renditions_grid_and_profiles(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.ladder_key(STANDARD.version), standard().to_item())

    result = encoding.get_ladder(encoding.LadderInput(ladder_version=STANDARD.version), READ_CTX)

    assert (result.frame_rate, result.gop_ms, result.segment_ms) == ("30000/1001", 2002, 6006)
    assert [r.name for r in result.video] == [r.name for r in STANDARD.video]
    assert result.audio == STANDARD.audio and result.audio.kbps == 128
    assert result.profiles == ["hls"]
    assert result.break_slate_gops == [15, 30]


def test_get_unknown_ladder_is_not_found(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.ladder_key("standard@0000000000"))
    with pytest.raises(ToolError) as raised:
        encoding.get_ladder(encoding.LadderInput(ladder_version="standard@0000000000"), READ_CTX)
    assert raised.value.code == "NOT_FOUND"


# --- propose --------------------------------------------------------------------------------------------------------


def test_propose_ladder_stores_a_valid_proposal(aws_stubs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets, "token_hex", lambda n: "a1b2c3d4e5")
    derived = propose(STANDARD, UP_TO_720).ladder
    assert derived is not None
    get_item(aws_stubs.ddb, store.ladder_key(STANDARD.version), standard().to_item())
    get_item(aws_stubs.ddb, store.ladder_key(derived.version))
    aws_stubs.ddb.add_response("put_item", {})

    result = encoding.propose_ladder(encoding.ProposeLadderInput(base=STANDARD.version, ops=UP_TO_720), READ_CTX)

    assert result.proposal_id == "proposal-a1b2c3d4e5" and result.valid
    assert result.ladder_version == derived.version and result.existing_status is None
    assert result.video == ["avc-720p", "avc-360p"]
    assert result.removed == ["avc-1080p"]
    assert result.cost_per_minute_usd < result.base_cost_per_minute_usd
    assert result.kit_cost_usd is not None and result.kit_cost_usd > 0
    [put] = aws_stubs.calls.of("PutItem")
    assert (put["Item"]["pk"], put["Item"]["sk"]) == ("PROPOSAL#proposal-a1b2c3d4e5", "META")
    assert put["Item"]["ladder"]["video"][0]["name"] == "avc-720p"
    assert abs(put["Item"]["expires_at"] - time.time() - encoding.PROPOSAL_TTL_S) < 5


def test_propose_reports_an_invalid_ladder(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.ladder_key(STANDARD.version), standard().to_item())
    aws_stubs.ddb.add_response("put_item", {})
    ops = NOTHING_LEFT

    result = encoding.propose_ladder(encoding.ProposeLadderInput(base=STANDARD.version, ops=ops), READ_CTX)

    assert not result.valid and result.ladder_version is None and result.kit_cost_usd is None
    assert result.errors == ["no video renditions left"]


# --- provision ------------------------------------------------------------------------------------------------------


def test_provision_ladder_creates_the_template_and_starts_the_kit(aws_stubs: SimpleNamespace) -> None:
    item, ladder = proposal_item()
    assert ladder is not None
    get_item(aws_stubs.ddb, store.proposal_key("proposal-1"), item)
    get_item(aws_stubs.ddb, store.ladder_key(ladder.version))
    aws_stubs.mediaconvert.add_client_error("create_job_template", "ConflictException", "already exists")
    aws_stubs.ddb.add_response("put_item", {})
    started = {"executionArn": f"{PROVISIONER}:x".replace(":stateMachine:", ":execution:"), "startDate": 0}
    aws_stubs.sfn.add_response("start_execution", started)

    result = encoding.provision_ladder(encoding.ProvisionLadderInput(proposal_id="proposal-1"), CTX)

    name = f"fast-{ladder.version.replace('@', '_')}"
    [template] = aws_stubs.calls.of("CreateJobTemplate")
    assert (template["Name"], template["Category"], template["Queue"]) == (name, "fast", QUEUE)
    assert template["Tags"] == {
        "fast:app": "fast",
        "fast:ladder": ladder.version,
        "fast:owner": "agent",
        "fast:correlation-id": CTX.correlation_id,
    }
    [record] = aws_stubs.calls.of("PutItem")
    assert record["Item"]["status"] == "PROVISIONING" and record["Item"]["job_template"] == name
    assert record["Item"]["owner"] == "agent" and record["Item"]["gsi1sk"] == f"PROVISIONING#{ladder.version}"
    assert Ladder.model_validate(store.plain(record["Item"]["ladder"])).version == ladder.version
    [execution] = aws_stubs.calls.of("StartExecution")
    assert execution["stateMachineArn"] == PROVISIONER
    assert json.loads(execution["input"]) == {
        "ladder": ladder.model_dump(mode="json"),
        "job_template": name,
        "owner": "agent",
        "correlation_id": CTX.correlation_id,
    }
    assert execution["name"].startswith(f"kit-{ladder.version.replace('@', '_')}-")
    assert (result.status, result.job_template, result.execution_arn) == ("PROVISIONING", name, started["executionArn"])


def test_a_concurrent_provision_of_the_same_version_starts_nothing(aws_stubs: SimpleNamespace) -> None:
    # An agent retry after a Gateway timeout: the first call claimed the version between the read and the write.
    item, ladder = proposal_item()
    assert ladder is not None
    get_item(aws_stubs.ddb, store.proposal_key("proposal-1"), item)
    get_item(aws_stubs.ddb, store.ladder_key(ladder.version))
    aws_stubs.mediaconvert.add_client_error("create_job_template", "ConflictException", "already exists")
    aws_stubs.ddb.add_client_error("put_item", "ConditionalCheckFailedException")
    claimed = store.LadderRecord(ladder_version=ladder.version, status="PROVISIONING", job_template="t", ladder=ladder)
    get_item(aws_stubs.ddb, store.ladder_key(ladder.version), claimed.to_item())

    result = encoding.provision_ladder(encoding.ProvisionLadderInput(proposal_id="proposal-1"), CTX)

    assert (result.status, result.execution_arn) == ("PROVISIONING", None)
    assert not aws_stubs.calls.of("StartExecution")
    [claim] = aws_stubs.calls.of("PutItem")
    assert "ConditionExpression" in claim


def test_a_provisioner_that_does_not_start_releases_the_version(aws_stubs: SimpleNamespace) -> None:
    item, ladder = proposal_item()
    assert ladder is not None
    get_item(aws_stubs.ddb, store.proposal_key("proposal-1"), item)
    get_item(aws_stubs.ddb, store.ladder_key(ladder.version))
    aws_stubs.mediaconvert.add_client_error("create_job_template", "ConflictException", "already exists")
    aws_stubs.ddb.add_response("put_item", {})  # PROVISIONING
    aws_stubs.sfn.add_client_error("start_execution", "ExecutionLimitExceeded", "too many executions")
    aws_stubs.ddb.add_response("put_item", {})  # FAILED again: provision_ladder may be called again

    with pytest.raises(ClientError, match="ExecutionLimitExceeded"):
        encoding.provision_ladder(encoding.ProvisionLadderInput(proposal_id="proposal-1"), CTX)

    claim, release = aws_stubs.calls.of("PutItem")
    assert (claim["Item"]["status"], release["Item"]["status"]) == ("PROVISIONING", "FAILED")
    assert release["Item"]["error"].startswith("the ladder provisioner did not start: ExecutionLimitExceeded")
    assert release["Item"]["gsi1sk"] == f"FAILED#{ladder.version}"


def test_provisioning_an_existing_version_starts_nothing(aws_stubs: SimpleNamespace) -> None:
    item, ladder = proposal_item()
    assert ladder is not None
    get_item(aws_stubs.ddb, store.proposal_key("proposal-1"), item)
    existing = store.LadderRecord(ladder_version=ladder.version, status="PROVISIONING", job_template="t", ladder=ladder)
    get_item(aws_stubs.ddb, store.ladder_key(ladder.version), existing.to_item())

    result = encoding.provision_ladder(encoding.ProvisionLadderInput(proposal_id="proposal-1"), CTX)

    assert (result.status, result.execution_arn, result.job_template) == ("PROVISIONING", None, "t")


def test_invalid_or_expired_proposals_are_refused(aws_stubs: SimpleNamespace) -> None:
    invalid, _ = proposal_item(NOTHING_LEFT)
    get_item(aws_stubs.ddb, store.proposal_key("proposal-1"), invalid)
    with pytest.raises(ToolError) as raised:
        encoding.provision_ladder(encoding.ProvisionLadderInput(proposal_id="proposal-1"), CTX)
    assert raised.value.code == "VALIDATION"

    expired, _ = proposal_item(expires_in=-1)
    get_item(aws_stubs.ddb, store.proposal_key("proposal-1"), expired)
    with pytest.raises(ToolError) as raised:
        encoding.provision_ladder(encoding.ProvisionLadderInput(proposal_id="proposal-1"), CTX)
    assert raised.value.code == "NOT_FOUND"
