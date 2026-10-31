"""The runbooks' function: facts from stand-ins, one ledger entry per execution, kill switches from AppConfig."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from aws_lambda_powertools.utilities import parameters
from botocore.exceptions import ClientError

from fast_channels.core.flags import OpsSwitches
from fast_channels.core.ledger import LedgerEntry, LedgerKind, entry_item
from fast_channels.functions import aws
from fast_channels.functions.ops import runbooks
from fast_channels.functions.ops.ledger import entry
from fast_channels.functions.settings import settings

NOW = datetime.now(UTC).replace(microsecond=0)


@dataclass
class Context:
    function_name: str = "runbooks"
    memory_limit_in_mb: int = 256
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:runbooks"
    aws_request_id: str = "request-1"


@dataclass
class Ledger:
    entries: list[LedgerEntry] = field(default_factory=list)

    def append(self, item: LedgerEntry, **_: Any) -> LedgerEntry:
        self.entries.append(item)
        return item


@pytest.fixture(autouse=True)
def stage(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "FAST_PREFIX": "fastm",
        "LEDGER_TABLE": "fastm-ledger",
        "LIBRARY_TABLE": "library",
        "CATALOG_TABLE": "catalog",
        "APPCONFIG_APPLICATION": "fastm-fast",
        "APPCONFIG_ENVIRONMENT": "fastm",
        "IAC_CHANNELS": "fastm-demo",
        "SOURCE_LOCATION": "fastm-media",
    }.items():
        monkeypatch.setenv(name, value)
    settings.cache_clear()


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> Ledger:
    fake = Ledger()
    monkeypatch.setattr(runbooks, "append", fake.append)
    monkeypatch.setattr(runbooks, "switches", OpsSwitches)
    monkeypatch.setattr(runbooks, "recent", lambda runbook: [])
    return fake


def alarm(name: str, state: str = "ALARM") -> dict[str, Any]:
    return {"detail-type": "CloudWatch Alarm State Change", "detail": {"alarmName": name, "state": {"value": state}}}


def run(runbook: str, trigger: dict[str, Any]) -> dict[str, Any]:
    return runbooks.handler({"runbook": runbook, "trigger": trigger, "execution": f"exec-{runbook}"}, Context())


def not_found(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "NotFoundException", "Message": "gone"}}, operation)


@pytest.mark.parametrize(
    ("runbook", "name", "actions"),
    [
        ("r7", "fastm-account-ad-insertion-requests", ["ops.freeze_expansion", "quota.draft_case"]),
        ("r9", "fastm-demo-getmanifest-errors", ["experiments.freeze", "release.freeze"]),
        ("r17", "fastm-cdn-5xx", ["page", "ops.shed"]),
    ],
)
def test_alarm_runbooks_ledger_their_dry_run(ledger: Ledger, runbook: str, name: str, actions: list[str]) -> None:
    result = run(runbook, alarm(name))

    [record] = ledger.entries
    assert (record.chain_id, record.kind, record.action) == (
        f"runbook/{runbook}",
        LedgerKind.RUNBOOK,
        "runbook.dry_run",
    )
    assert (record.subject, record.correlation_id, record.principal) == (name, f"exec-{runbook}", runbooks.PRINCIPAL)
    assert record.detail["actions"] == actions
    assert record.detail["facts"] == {"alarm": name, "state": "ALARM"}
    assert result["record_id"] == record.record_id
    assert result["mode"] == "dry_run"


def test_a_killed_runbook_ledgers_that_it_was_disabled(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runbooks, "switches", lambda: OpsSwitches(shed=False))

    run("r17", alarm("fastm-cdn-5xx"))

    [record] = ledger.entries
    assert (record.action, record.detail["mode"], record.detail["actions"]) == ("runbook.disabled", "disabled", [])


def test_r12_pages_when_no_always_on_channel_is_stopped(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    class MediaTailor:
        def describe_channel(self, *, ChannelName: str) -> dict[str, str]:  # noqa: N803
            return {"ChannelState": "STOPPED"}

    monkeypatch.setattr(aws, "client", lambda service: MediaTailor())

    run("r12", alarm("fastm-probe-off-air"))

    [record] = ledger.entries
    assert record.detail["actions"] == ["page.sev1"]
    assert record.detail["facts"]["channels"] == {"fastm-demo": "STOPPED"}
    assert record.subject == "fastm-demo"


def test_r12_would_start_an_always_on_channel(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    class MediaTailor:
        def describe_channel(self, *, ChannelName: str) -> dict[str, str]:  # noqa: N803
            if ChannelName == "fastm-gone":
                raise not_found("DescribeChannel")
            return {"ChannelState": "STOPPED"}

    monkeypatch.setenv("IAC_CHANNELS", "fastm-demo,fastm-gone")
    monkeypatch.setenv("ALWAYS_ON_CHANNELS", "fastm-demo")
    settings.cache_clear()
    monkeypatch.setattr(aws, "client", lambda service: MediaTailor())

    run("r12", alarm("fastm-probe-off-air"))
    run("r12", alarm("fastm-probe-off-air", state="OK"))

    started, ok = ledger.entries
    assert started.detail["actions"] == ["channel.start"]
    assert started.detail["facts"]["channels"] == {"fastm-demo": "STOPPED", "fastm-gone": "NotFoundException"}
    assert ok.detail["actions"] == ["none"]


def channel_alert(code: str = "SCHEDULING_ERROR", modified: str = "2026-10-29T12:00:00.000Z") -> dict[str, Any]:
    return {
        "detail-type": "Channel Alert",
        "detail": {
            "channel": "fastm-demo",
            "code": code,
            "category": "SCHEDULING_ERROR",
            "message": "The program has a missing source",
            "resource_arn": "arn:aws:mediatailor:us-east-1:123456789012:channel/fastm-demo",
            "last_modified": modified,
            "correlation_id": "2026-10-29T12:01:00Z",
        },
    }


def test_r13_pages_without_an_evergreen_block_and_on_repeat(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    run("r13", channel_alert())
    first = ledger.entries[-1].detail
    monkeypatch.setattr(runbooks, "recent", lambda runbook: [{"at": NOW, "detail": first}])
    run("r13", channel_alert(modified="2026-10-29T12:20:00.000Z"))  # the alert came back
    run("r13", channel_alert())  # the same change, delivered again: no repeat

    assert [e.detail["facts"]["repeated"] for e in ledger.entries] == [False, True, False]
    assert ledger.entries[0].detail["reason"] == "SCHEDULING_ERROR: no evergreen block configured"
    assert all(e.subject == "fastm-demo" for e in ledger.entries)


def test_r13_would_swap_in_the_evergreen_block(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVERGREEN_BLOCK", "fastm-evergreen")
    settings.cache_clear()

    run("r13", channel_alert())

    assert ledger.entries[0].detail["actions"] == ["program.swap_evergreen"]


def asset_failed(asset: str = "sintel-1a2b3c") -> dict[str, Any]:
    return {
        "detail-type": "Asset Failed",
        "detail": {
            "asset_id": asset,
            "kind": "content",
            "ladder_version": "standard@843ed1d05a",
            "error": "MediaConvertError",
            "cause": "x" * 900,
            "correlation_id": "seed-sintel",
        },
    }


def test_r14_retries_once_then_quarantines(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    run("r14", asset_failed())
    monkeypatch.setattr(runbooks, "recent", lambda runbook: [{"at": NOW, "detail": ledger.entries[0].detail}])
    run("r14", asset_failed())
    run("r14", asset_failed("other-asset"))

    assert [e.detail["actions"] for e in ledger.entries] == [
        ["ingest.retry"],
        ["asset.quarantine", "cms.notify"],
        ["ingest.retry"],
    ]
    assert len(str(ledger.entries[0].detail["facts"]["cause"])) == 500


def program(
    name: str, start: datetime, *, vod_source: str = "sintel-standard", content_id: str | None = "sintel"
) -> Any:
    start_ms = int(start.timestamp() * 1000)
    return SimpleNamespace(
        name=name,
        vod_source=vod_source,
        source_location=None,
        start_ms=start_ms,
        end_ms=start_ms + 3_600_000,
        content_id=content_id,
    )


def test_r15_finds_the_missing_vod_sources(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, str]] = []

    class MediaTailor:
        def describe_vod_source(self, **request: str) -> dict[str, Any]:
            calls.append(request)
            if request["VodSourceName"] == "gone":
                raise not_found("DescribeVodSource")
            return {}

    published = [
        program("p1", NOW + timedelta(hours=3), vod_source="gone"),
        program("p2", NOW + timedelta(minutes=30), vod_source="gone"),
        program("p3", NOW + timedelta(hours=4)),
    ]
    monkeypatch.setattr(aws, "client", lambda service: MediaTailor())
    monkeypatch.setattr(runbooks.library_store, "programs", lambda channel, start_ms, end_ms: published)
    trigger = {
        "detail-type": "Schedule Published",
        "detail": {
            "channel": "fastm-demo",
            "plan_id": "plan-1",
            "programs": 3,
            "start_ms": 0,
            "end_ms": 1,
            "correlation_id": "c",
        },
    }

    run("r15", trigger)

    [record] = ledger.entries
    assert record.detail["actions"] == ["program.replace_by_agent", "page"]
    assert [p["program"] for p in record.detail["facts"]["programs"]] == ["p1", "p2"]
    assert calls[0] == {"SourceLocationName": "fastm-media", "VodSourceName": "gone"}


def test_r15_raises_on_other_errors(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    class MediaTailor:
        def describe_vod_source(self, **request: str) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "DescribeVodSource")

    monkeypatch.setattr(aws, "client", lambda service: MediaTailor())
    monkeypatch.setattr(runbooks.library_store, "programs", lambda channel, start_ms, end_ms: [program("p", NOW)])
    trigger = {
        "detail": {"channel": "c", "plan_id": "p", "programs": 1, "start_ms": 0, "end_ms": 1, "correlation_id": "c"}
    }

    with pytest.raises(ClientError):
        run("r15", trigger)
    assert ledger.entries == []


def test_r16_checks_the_rights_of_the_next_six_hours(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    windows: list[tuple[int, int]] = []
    upcoming = [
        program("short", NOW + timedelta(hours=1)),
        program("fine", NOW + timedelta(hours=2), content_id="b"),
        program("unknown", NOW + timedelta(hours=3), content_id=None),
    ]

    def programs(channel: str, start_ms: int, end_ms: int) -> list[Any]:
        windows.append((start_ms, end_ms))
        return upcoming

    class Catalog:
        def get_item(self, *, Key: dict[str, str]) -> dict[str, Any]:  # noqa: N803
            until = NOW + (timedelta(hours=1, minutes=30) if Key["pk"] == "CONTENT#sintel" else timedelta(days=9))
            return {"Item": {"available_until": until.isoformat()}}

    monkeypatch.setattr(runbooks.library_store, "programs", programs)
    monkeypatch.setattr(aws, "table", lambda name: Catalog())

    run("r16", {"scheduled_time": NOW.isoformat()})

    [record] = ledger.entries
    assert record.detail["actions"] == ["program.replace_by_agent"]
    assert [p["program"] for p in record.detail["facts"]["programs"]] == ["short"]
    assert windows == [(int(NOW.timestamp() * 1000), int((NOW + timedelta(hours=6)).timestamp() * 1000))]
    assert record.subject == "fastm"


def test_r16_with_every_program_inside_its_rights(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runbooks.library_store, "programs", lambda channel, start_ms, end_ms: [])

    run("r16", {})

    assert ledger.entries[0].detail["actions"] == ["none"]


def test_switches_read_the_flags_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def get_app_config(**request: Any) -> dict[str, Any]:
        seen.update(request)
        return {"ops": {"enabled": True, "act": False, "freeze": False}}

    monkeypatch.setattr(parameters, "get_app_config", get_app_config)

    assert runbooks.switches() == OpsSwitches(freeze=False)
    assert (seen["name"], seen["environment"], seen["application"]) == ("flags", "fastm", "fastm-fast")


def test_unreadable_flags_leave_every_runbook_in_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**_: Any) -> None:
        raise parameters.exceptions.GetParameterError("no session")

    monkeypatch.setattr(parameters, "get_app_config", fail)

    assert runbooks.switches() == OpsSwitches()


def test_recent_reads_the_runbooks_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    good = entry(
        "runbook/r13",
        LedgerKind.RUNBOOK,
        principal="p",
        action="runbook.dry_run",
        subject="s",
        detail={"facts": {"code": "X"}},
    )
    queries: list[dict[str, Any]] = []

    class Table:
        def query(self, **request: Any) -> dict[str, Any]:
            queries.append(request)
            return {"Items": [entry_item(good), {"pk": "runbook/r13", "sk": "HEAD", "type": "head"}]}

    monkeypatch.setattr(aws, "table", lambda name: Table())

    [found] = runbooks.recent(runbooks.Runbook.R13)

    assert found == {"at": good.at, "detail": {"facts": {"code": "X"}}}
    assert queries[0]["ScanIndexForward"] is False
