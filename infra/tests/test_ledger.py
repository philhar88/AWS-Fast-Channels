"""The ops module's ledger: the table and archive in the Data stack, the writer and Firehose stream in the Ops stack."""

import json
from collections.abc import Callable
from typing import Any

import pytest
from aws_cdk import App, Environment, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Template

from fast_channels.infra.config import LedgerLock
from fast_channels.infra.constructs.ops import ARCHIVE_TAG, LedgerStorage

type GetDeployment = Callable[..., Any]

OPS = {"modules.ops.enabled": True}


def resources(template: Template, kind: str) -> dict[str, Any]:
    found: dict[str, Any] = template.find_resources(kind)
    return found


def archive_of(template: Template) -> dict[str, Any]:
    [archive] = [
        r
        for r in resources(template, "AWS::S3::Bucket").values()
        if {"Key": ARCHIVE_TAG, "Value": "true"} in r["Properties"].get("Tags", [])
    ]
    return archive


@pytest.fixture(scope="module")
def ops(deployment: GetDeployment) -> Any:
    return deployment(OPS)


def test_ledger_table(ops: Any) -> None:
    data = ops.template(ops.stage.data)
    [table] = [
        r["Properties"]
        for r in resources(data, "AWS::DynamoDB::GlobalTable").values()
        if r["Properties"].get("TableName") == "fast-ledger"
    ]

    assert table["KeySchema"] == [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ]
    assert table["StreamSpecification"] == {"StreamViewType": "NEW_AND_OLD_IMAGES"}
    assert table["BillingMode"] == "PAY_PER_REQUEST"
    assert table["Replicas"][0]["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    for output in ("LedgerTableName", "LedgerArchiveBucketName"):
        data.has_output(output, {})


def test_the_default_archive_is_governance_locked_for_30_days_and_retained(ops: Any) -> None:
    archive = archive_of(ops.template(ops.stage.data))
    props = archive["Properties"]

    assert props["ObjectLockEnabled"] is True
    assert props["ObjectLockConfiguration"] == {
        "ObjectLockEnabled": "Enabled",
        "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 30}},
    }
    assert props["VersioningConfiguration"] == {"Status": "Enabled"}
    assert props["LoggingConfiguration"]["LogFilePrefix"] == "s3/ledger/"
    assert (archive["DeletionPolicy"], archive["UpdateReplacePolicy"]) == ("Retain", "Retain")
    cleaners = resources(ops.template(ops.stage.data), "Custom::S3AutoDeleteObjects").values()
    assert not [c for c in cleaners if "LedgerArchive" in json.dumps(c["Properties"])]  # it keeps its records


@pytest.mark.parametrize(("lock", "mode"), [("governance", "GOVERNANCE"), ("compliance", "COMPLIANCE")])
def test_the_lock_follows_the_configuration(deployment: GetDeployment, lock: str, mode: str) -> None:
    d = deployment(
        {**OPS, "removal": "retain", "modules.ops.ledger_lock": lock, "modules.ops.ledger_retention_days": 1}
    )
    props = archive_of(d.template(d.stage.data))["Properties"]

    assert props["ObjectLockConfiguration"]["Rule"] == {"DefaultRetention": {"Mode": mode, "Days": 1}}


def test_an_unlocked_archive_follows_removal(deployment: GetDeployment) -> None:
    d = deployment({**OPS, "modules.ops.ledger_lock": "off"})
    archive = archive_of(d.template(d.stage.data))

    assert "ObjectLockEnabled" not in archive["Properties"]
    assert archive["DeletionPolicy"] == "Delete"


def test_producers_may_only_put(ops: Any) -> None:
    stack = Stack(App(), "Test", env=Environment(account="123456789012", region="us-east-1"))
    logs = s3.Bucket(stack, "Logs")
    storage = LedgerStorage(stack, "Ledger", prefix="t", logs_bucket=logs, lock=LedgerLock.OFF, retention_days=1)
    role = iam.Role(stack, "Producer", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    storage.grant_append(role)
    [policy] = resources(Template.from_stack(stack), "AWS::IAM::Policy").values()

    assert [s["Action"] for s in policy["Properties"]["PolicyDocument"]["Statement"]] == ["dynamodb:PutItem"]


def test_the_writer_reads_entries_from_the_stream(ops: Any) -> None:
    template = ops.template(ops.stage.ops)
    [mapping] = resources(template, "AWS::Lambda::EventSourceMapping").values()
    props = mapping["Properties"]

    assert props["StartingPosition"] == "TRIM_HORIZON"
    assert props["BatchSize"] == 100
    assert props["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert [json.loads(f["Pattern"]) for f in props["FilterCriteria"]["Filters"]] == [
        {"dynamodb": {"NewImage": {"type": {"S": ["entry"]}}}},
        {"dynamodb": {"OldImage": {"type": {"S": ["entry"]}}}},
    ]
    assert "Fn::GetAtt" in json.dumps(props["DestinationConfig"]["OnFailure"]["Destination"])
    assert "PublishOutputFnGetAttLedgerTable" in json.dumps(props["EventSourceArn"])


def test_the_writer_may_get_and_put_ledger_items_and_put_to_the_stream(ops: Any) -> None:
    template = ops.template(ops.stage.ops)
    [function] = [
        r["Properties"]
        for r in resources(template, "AWS::Lambda::Function").values()
        if r["Properties"]["Handler"] == "fast_channels.functions.ops.ledger_writer.handler"
    ]
    policies = json.dumps(resources(template, "AWS::IAM::Policy"))

    assert function["Environment"]["Variables"]["LEDGER_STREAM"] == {"Ref": "LedgerStream957C9527"}
    assert "LEDGER_TABLE" in function["Environment"]["Variables"]
    assert '["dynamodb:GetItem", "dynamodb:PutItem"]' in policies
    assert "firehose:PutRecord" in policies
    assert "dynamodb:DeleteItem" not in policies
    assert "dynamodb:UpdateItem" not in policies


def test_firehose_writes_gzip_lines_and_may_never_delete(ops: Any) -> None:
    template = ops.template(ops.stage.ops)
    [stream] = resources(template, "AWS::KinesisFirehose::DeliveryStream").values()
    config = stream["Properties"]["ExtendedS3DestinationConfiguration"]
    [role_policy] = [
        p["Properties"]["PolicyDocument"]["Statement"]
        for p in resources(template, "AWS::IAM::Policy").values()
        if "StreamRole" in json.dumps(p["Properties"]["Roles"])
    ]
    [deny] = [s for s in role_policy if s["Effect"] == "Deny"]

    assert stream["Properties"]["DeliveryStreamName"] == "fast-ledger"
    assert config["Prefix"] == "ledger/!{timestamp:yyyy/MM/dd}/"
    assert config["ErrorOutputPrefix"].startswith("ledger-errors/")
    assert config["CompressionFormat"] == "GZIP"
    assert config["BufferingHints"] == {"IntervalInSeconds": 60, "SizeInMBs": 1}
    assert {"s3:DeleteObject*", "s3:PutObjectRetention", "s3:PutObjectLegalHold"} <= set(deny["Action"])


def test_ledger_alarms(ops: Any) -> None:
    alarms = {
        r["Properties"]["AlarmName"]: r["Properties"]
        for r in resources(ops.template(ops.stage.ops), "AWS::CloudWatch::Alarm").values()
    }

    assert {
        "fast-ledger-dead-letters",
        "fast-ledger-entries-changed",
        "fast-ledger-entries-rejected",
        "fast-ledger-lag",
    } <= set(alarms)
    assert all(a["TreatMissingData"] == "notBreaching" for a in alarms.values())
    changed = alarms["fast-ledger-entries-changed"]
    assert (changed["Namespace"], changed["MetricName"]) == ("FastChannels", "LedgerEntriesChanged")
    assert changed["Dimensions"] == [{"Name": "service", "Value": "ops.ledger_writer"}]
    assert alarms["fast-ledger-lag"]["Threshold"] == 600_000


def test_the_lake_reads_the_archive(ops: Any) -> None:
    tables = {
        r["Properties"]["TableInput"]["Name"]: r["Properties"]["TableInput"]
        for r in resources(ops.template(ops.stage.ops), "AWS::Glue::Table").values()
    }
    ledger = tables["ledger"]

    assert [c["Name"] for c in ledger["StorageDescriptor"]["Columns"]][-3:] == ["seq", "prev_hash", "hash"]
    assert ledger["Parameters"]["projection.day.format"] == "yyyy/MM/dd"
    assert json.dumps(ledger["Parameters"]["storage.location.template"]).endswith('/ledger/${day}/"]]}')


def test_the_stream_output(ops: Any) -> None:
    ops.template(ops.stage.ops).has_output("LedgerStreamName", {})
