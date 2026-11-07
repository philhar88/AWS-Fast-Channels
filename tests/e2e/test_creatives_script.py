"""The operator script of the creatives module (``scripts/creatives.py``): registrations and decisions as readiness
executions, uploads only to the quarantine bucket. Offline (not marked ``e2e``)."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber
from creatives import Creatives, summary

from fast_channels.core.creatives.names import CRID_PATTERN, crid_for, source_key
from fast_channels.core.creatives.registry import Decision, Registration

MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:fastcii-creatives"
BUCKET = "fastcii-creatives-123456789012-us-east-1"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
EXECUTION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


@dataclass
class FakeStage:
    outputs: dict[str, str] = field(
        default_factory=lambda: {
            "StateMachineArn": MACHINE,
            "RegistryTable": "fastcii-creatives",
            "QuarantineBucket": BUCKET,
        }
    )
    session: Any = field(default_factory=lambda: boto3.Session(region_name="us-east-1"))
    region: str = "us-east-1"

    def output(self, plane: object, key: str) -> str:
        return self.outputs[key]


@pytest.fixture
def creatives(monkeypatch: pytest.MonkeyPatch) -> Creatives:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    return Creatives(FakeStage())  # type: ignore[arg-type]


class Started:
    """Matches a StartExecution input: the JSON of ``model`` with ``fields``."""

    __hash__ = None  # type: ignore[assignment]

    def __init__(self, model: type[Registration] | type[Decision], **fields: str) -> None:
        self.model, self.fields = model, fields

    def __eq__(self, other: object) -> bool:
        document = self.model.model_validate_json(str(other))
        return all(getattr(document, key) == value for key, value in self.fields.items())


def test_a_local_file_goes_to_the_quarantine_and_is_registered(creatives: Creatives, tmp_path: Path) -> None:
    spot = tmp_path / "spot.mp4"
    spot.write_bytes(MP4)
    crid = crid_for("acme-outdoor.example", hashlib.sha256(MP4).hexdigest())
    s3, states = Stubber(creatives.s3), Stubber(creatives.states)
    s3.add_response(
        "put_object", {}, {"Bucket": BUCKET, "Key": source_key(crid), "Body": MP4, "ContentType": "video/mp4"}
    )
    source = f"s3://{BUCKET}/{source_key(crid)}"
    states.add_response(
        "start_execution",
        {"executionArn": f"{MACHINE}:x", "startDate": "2026-11-07T00:00:00Z"},
        {"stateMachineArn": MACHINE, "name": ANY, "input": Started(Registration, crid=crid, source=source, by="ops")},
    )
    with s3, states:
        assert creatives.register_file(spot, advertiser="Acme", adomain="acme-outdoor.example", by="ops") == (
            crid,
            f"{MACHINE}:x",
        )
    assert re.fullmatch(CRID_PATTERN, crid)


def test_a_file_that_is_not_an_mp4_is_refused(creatives: Creatives, tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_bytes(b"<html></html>" * 4)
    with pytest.raises(SystemExit, match="not an MP4"):
        creatives.register_file(page, advertiser="Acme", adomain="acme-outdoor.example", by="ops")


def test_a_url_and_a_decision_start_executions_with_valid_names(creatives: Creatives) -> None:
    states = Stubber(creatives.states)
    names: list[str] = []
    url = "https://ads.example/vast.xml"
    crid = crid_for("acme-outdoor.example", url)
    for model, fields in ((Registration, {"source": url}), (Decision, {"action": "suspend"})):
        states.add_response(
            "start_execution",
            {"executionArn": f"{MACHINE}:y", "startDate": "2026-11-07T00:00:00Z"},
            {"stateMachineArn": MACHINE, "name": ANY, "input": Started(model, crid=crid, **fields)},
        )
    states.activate()
    creatives.states.meta.events.register(
        "before-parameter-build.states.StartExecution", lambda params, **_: names.append(params["name"])
    )
    creatives.register_url(url, advertiser="Acme", adomain="acme-outdoor.example", by="ops")
    creatives.start(Decision(action="suspend", crid=crid, by="ops"))
    states.assert_no_pending_responses()
    assert all(EXECUTION_NAME.fullmatch(name) for name in names), names


def test_status_shows_the_state_and_its_history() -> None:
    record = {
        "crid": "cr-0123456789ab",
        "state": "READY",
        "duration_ms": Decimal(30030),
        "history": [
            {"state": "RECEIVED", "at": "t0", "by": "ops"},
            {"state": "REVIEW", "at": "t1", "by": "moderation", "note": "Alcohol 91%"},
        ],
    }
    shown = json.loads(json.dumps(summary(record), default=str))
    assert shown["state"] == "READY" and shown["duration_ms"] == 30030
    assert shown["history"] == ["t0 RECEIVED by ops", "t1 REVIEW by moderation: Alcohol 91%"]
    assert summary(None) == {"state": None}
