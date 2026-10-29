"""The ops module (``modules.ops``): the lake bucket in the Data stack, the Ops stack (lake tables, views, workgroup,
log copies, AppConfig application) and the census's ``Census Changed`` events in the Media stack."""

import base64
import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from aws_cdk.assertions import Match, Template

from fast_channels.infra.constructs.delivery import ACCESS_LOG_FIELDS
from fast_channels.infra.constructs.ops import (
    ADS_LOG_FIELDS,
    EXCLUDED_ACCESS_LOG_FIELDS,
    EXCLUDED_ADS_LOG_FIELDS,
    EXCLUDED_MANIFEST_LOG_FIELDS,
    MANIFEST_LOG_FIELDS,
    lake_database_name,
)
from fast_channels.infra.constructs.ops.lake import QUERY_SCAN_LIMIT_BYTES, VIEWS

type GetDeployment = Callable[..., Any]

OPS = {"modules.ops.enabled": True}


@pytest.fixture(scope="module")
def ops(deployment: GetDeployment) -> Any:
    return deployment(OPS)


def resources(template: Template, kind: str) -> dict[str, Any]:
    found: dict[str, Any] = template.find_resources(kind)
    return found


def by_name(template: Template, kind: str) -> dict[str, dict[str, Any]]:
    """Glue tables by name, deliveries by source name, ..."""
    key = {"AWS::Glue::Table": "TableInput", "AWS::Logs::Delivery": "DeliverySourceName"}[kind]
    found = resources(template, kind).values()
    if kind == "AWS::Glue::Table":
        return {r["Properties"][key]["Name"]: r["Properties"] for r in found}
    return {r["Properties"][key]: r["Properties"] for r in found}


def buckets_logging_to(template: Template, prefix: str) -> list[dict[str, Any]]:
    """The buckets whose access logs go to ``prefix`` in the logs bucket."""
    return [
        r
        for r in resources(template, "AWS::S3::Bucket").values()
        if r["Properties"].get("LoggingConfiguration", {}).get("LogFilePrefix") == prefix
    ]


def test_ops_off_deploys_no_ops_stack_and_no_lake(default: Any) -> None:
    assert default.stage.ops is None
    assert default.stage.data.lake is None
    assert default.stage.data.exports.lake_bucket is None
    assert default.stage.media.observability.census.last_count is None


def test_the_ops_stack_follows_data_and_media(ops: Any) -> None:
    stage = ops.stage

    assert stage.ops.stack_name == "FastChannels-Ops"
    assert {d.node.id for d in stage.ops.dependencies} == {"Data", "Media"}
    assert stage.ops.termination_protection is False


def test_lake_bucket(ops: Any) -> None:
    data = ops.template(ops.stage.data)
    [lake] = buckets_logging_to(data, "s3/lake/")
    props = lake["Properties"]

    assert props["VersioningConfiguration"] == {"Status": "Enabled"}
    assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"] == [
        {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
    ]
    assert props["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert props["LoggingConfiguration"]["LogFilePrefix"] == "s3/lake/"
    rules = {rule["Id"]: rule for rule in props["LifecycleConfiguration"]["Rules"]}
    assert rules["expire-lake-objects"]["ExpirationInDays"] == 30
    assert rules["expire-lake-objects"]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 1}
    assert lake["DeletionPolicy"] == "Delete"
    data.has_output("LakeBucketName", {})


def test_lake_retention_follows_the_configuration(deployment: GetDeployment) -> None:
    d = deployment({**OPS, "modules.ops.lake_retention_days": 7})
    data = d.template(d.stage.data)
    [lake] = buckets_logging_to(data, "s3/lake/")
    rules = {rule["Id"]: rule for rule in lake["Properties"]["LifecycleConfiguration"]["Rules"]}
    tables = by_name(d.template(d.stage.ops), "AWS::Glue::Table")

    assert rules["expire-lake-objects"]["ExpirationInDays"] == 7
    assert tables["mediatailor_ads"]["TableInput"]["Parameters"]["projection.datehour.range"] == "NOW-7DAYS,NOW+1HOURS"


def test_only_this_deployments_log_deliveries_may_write_to_the_lake(ops: Any) -> None:
    data = ops.template(ops.stage.data)
    policies = [
        p["Properties"]["PolicyDocument"]["Statement"]
        for p in resources(data, "AWS::S3::BucketPolicy").values()
        if "LogDeliveryWrite" in json.dumps(p) and "mediatailor/*" in json.dumps(p)
    ]
    [statements] = policies
    write = next(s for s in statements if s.get("Sid") == "LogDeliveryWrite")

    assert write["Principal"] == {"Service": "delivery.logs.amazonaws.com"}
    assert write["Action"] == "s3:PutObject"
    assert write["Condition"]["ArnLike"] == {
        "aws:SourceArn": "arn:aws:logs:us-east-1:123456789012:delivery-source:fast-*"
    }
    assert write["Condition"]["StringEquals"]["aws:SourceAccount"] == "123456789012"
    assert json.dumps(write["Resource"]).endswith('"/mediatailor/*"]]}')


def test_lake_access_logs_go_to_the_logs_bucket(ops: Any) -> None:
    data = ops.template(ops.stage.data)
    logs_policy = json.dumps(resources(data, "AWS::S3::BucketPolicy"))

    assert "/s3/lake/*" in logs_policy
    assert "logging.s3.amazonaws.com" in logs_policy


def test_field_lists_drop_the_design_exclusions() -> None:
    assert {"requestHeaders", "rawAdsRequest"} == EXCLUDED_ADS_LOG_FIELDS
    assert {"mediaTailorPath", "input", "http"} == EXCLUDED_MANIFEST_LOG_FIELDS
    assert {"c-ip", "cs-uri-query", "x-forwarded-for"} == EXCLUDED_ACCESS_LOG_FIELDS
    assert not EXCLUDED_ADS_LOG_FIELDS & set(ADS_LOG_FIELDS)
    assert not EXCLUDED_MANIFEST_LOG_FIELDS & set(MANIFEST_LOG_FIELDS)
    assert set(ACCESS_LOG_FIELDS) >= EXCLUDED_ACCESS_LOG_FIELDS  # the v3.0 delivery records them
    for required in ("eventTimestamp", "awsAccountId", "eventType", "sessionId", "adsRequestUrl", "avail"):
        assert required in ADS_LOG_FIELDS
    assert {"eventTimestamp", "awsAccountId", "originId"} <= set(MANIFEST_LOG_FIELDS)  # mandatory fields


def test_log_copies_of_every_iac_playback_configuration_exclude_the_fields(ops: Any) -> None:
    template = ops.template(ops.stage.ops)
    deliveries = by_name(template, "AWS::Logs::Delivery")
    destinations = {
        r["Properties"]["Name"]: r["Properties"] for r in resources(template, "AWS::Logs::DeliveryDestination").values()
    }

    assert set(deliveries) == {"fast-demo-ads", "fast-demo-manifest"}
    assert deliveries["fast-demo-ads"]["RecordFields"] == list(ADS_LOG_FIELDS)
    assert deliveries["fast-demo-manifest"]["RecordFields"] == list(MANIFEST_LOG_FIELDS)
    for delivery in deliveries.values():
        assert delivery["S3SuffixPath"] == "{yyyy}/{MM}/{dd}/{HH}"
        assert delivery["S3EnableHiveCompatiblePath"] is False
    assert set(destinations) == {"fast-lake-ads", "fast-lake-manifest"}
    for suffix in ("ads", "manifest"):
        assert destinations[f"fast-lake-{suffix}"]["OutputFormat"] == "json"
        assert json.dumps(destinations[f"fast-lake-{suffix}"]["DestinationResourceArn"]).endswith(
            f'"/mediatailor/{suffix}"]]}}'
        )


def test_log_copies_are_created_one_after_another(ops: Any) -> None:
    template = ops.template(ops.stage.ops)
    deliveries = resources(template, "AWS::Logs::Delivery")
    chained = [r.get("DependsOn", []) for r in deliveries.values()]

    assert sorted(len(d) for d in chained) == [0, 1]


def test_every_iac_channel_gets_its_log_copies(deployment: GetDeployment) -> None:
    d = deployment({**OPS, "channels.iac": [{"name": "fast-demo"}, {"name": "fast-news"}]})

    assert set(by_name(d.template(d.stage.ops), "AWS::Logs::Delivery")) == {
        f"{channel}-{suffix}" for channel in ("fast-demo", "fast-news") for suffix in ("ads", "manifest")
    }


def test_cloudfront_table_reads_the_v3_parquet_without_viewer_fields(ops: Any) -> None:
    table = by_name(ops.template(ops.stage.ops), "AWS::Glue::Table")["cloudfront_access"]
    columns = [c["Name"] for c in table["TableInput"]["StorageDescriptor"]["Columns"]]
    parameters = table["TableInput"]["Parameters"]

    assert {"c_ip", "cs_uri_query", "x_forwarded_for"}.isdisjoint(columns)
    assert len(columns) == len(ACCESS_LOG_FIELDS) - 3
    assert {"timestamp_ms", "cs_uri_stem", "cs_user_agent", "cmcd_session_id", "sc_status"} <= set(columns)
    assert all(re.fullmatch(r"[a-z0-9_]+", c) for c in columns)
    assert table["TableInput"]["StorageDescriptor"]["SerdeInfo"]["SerializationLibrary"] == (
        "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    )
    assert [k["Name"] for k in table["TableInput"]["PartitionKeys"]] == ["year", "month", "day", "hour"]
    assert parameters["projection.enabled"] == "true"
    assert parameters["projection.month.digits"] == "2"
    location = json.dumps(parameters["storage.location.template"])
    assert "/AWSLogs/aws-account-id=123456789012/CloudFront/DistributionId=" in location
    assert "/year=${year}/month=${month}/day=${day}/hour=${hour}/" in location


@pytest.mark.parametrize(
    ("table", "fields", "location"),
    [
        (
            "mediatailor_ads",
            ADS_LOG_FIELDS,
            "/mediatailor/ads/AWSLogs/123456789012/mediatailoraddecisionserverinteractionlogs/",
        ),
        (
            "mediatailor_manifest",
            MANIFEST_LOG_FIELDS,
            "/mediatailor/manifest/AWSLogs/123456789012/mediatailormanifestservicelogs/",
        ),
    ],
)
def test_mediatailor_tables(ops: Any, table: str, fields: tuple[str, ...], location: str) -> None:
    """The location is where MediaTailor's S3 deliveries write (observed on fastm, 2026-10-29)."""
    props = by_name(ops.template(ops.stage.ops), "AWS::Glue::Table")[table]["TableInput"]
    parameters = props["Parameters"]

    assert [c["Name"] for c in props["StorageDescriptor"]["Columns"]] == [f.lower() for f in fields]
    assert props["StorageDescriptor"]["SerdeInfo"]["SerializationLibrary"] == "org.openx.data.jsonserde.JsonSerDe"
    assert [k["Name"] for k in props["PartitionKeys"]] == ["datehour"]
    assert parameters["projection.datehour.format"] == "yyyy/MM/dd/HH"
    assert parameters["projection.datehour.interval.unit"] == "HOURS"
    assert json.dumps(parameters["storage.location.template"]).endswith(f'{location}${{datehour}}/"]]}}')


def test_workgroup_caps_every_query(ops: Any) -> None:
    [workgroup] = resources(ops.template(ops.stage.ops), "AWS::Athena::WorkGroup").values()
    props = workgroup["Properties"]

    assert props["Name"] == "fast-lake"
    assert props["RecursiveDeleteOption"] is True
    assert props["WorkGroupConfiguration"] == {
        "BytesScannedCutoffPerQuery": QUERY_SCAN_LIMIT_BYTES,
        "EnforceWorkGroupConfiguration": True,
        "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"},
        "ManagedQueryResultsConfiguration": {"Enabled": True},
        "PublishCloudWatchMetricsEnabled": True,
    }


@pytest.mark.parametrize("view", VIEWS, ids=[v.name for v in VIEWS])
def test_views_are_athena_presto_views(ops: Any, view: Any) -> None:
    props = by_name(ops.template(ops.stage.ops), "AWS::Glue::Table")[view.name]
    table = props["TableInput"]
    encoded = re.fullmatch(r"/\* Presto View: (\S+) \*/", table["ViewOriginalText"])
    assert encoded
    definition = json.loads(base64.b64decode(encoded.group(1)))

    assert props["DatabaseName"] == "fast_lake"
    assert table["TableType"] == "VIRTUAL_VIEW"
    assert table["Parameters"] == {"presto_view": "true", "comment": "Presto View"}
    assert table["ViewExpandedText"] == "/* Presto View */"
    assert (definition["catalog"], definition["schema"]) == ("awsdatacatalog", "fast_lake")
    assert [c["name"] for c in definition["columns"]] == [c["Name"] for c in table["StorageDescriptor"]["Columns"]]
    assert definition["originalSql"].startswith("WITH requests AS")
    assert '"fast_lake"."mediatailor_ads"' in definition["originalSql"]
    assert "{db}" not in definition["originalSql"]


def test_views_never_read_excluded_fields() -> None:
    for view in VIEWS:
        sql = view.sql.lower()
        for field in (*EXCLUDED_ADS_LOG_FIELDS, *EXCLUDED_MANIFEST_LOG_FIELDS):
            assert field.lower() not in re.findall(r"[a-z_]+", sql), (view.name, field)


def test_v_breaks_keys_breaks_by_scte_event_id() -> None:
    [breaks] = [v for v in VIEWS if v.name == "v_breaks"]

    assert breaks.columns[0] == ("evid", "bigint")
    assert "url_extract_parameter(adsrequesturl, 'evid')" in breaks.sql
    # Each source is aggregated by evid before the join, so fills are never multiplied by marker sightings.
    assert breaks.sql.count("GROUP BY") == 3
    assert "FROM seen s LEFT JOIN marked m ON m.evid = s.evid LEFT JOIN requested q ON q.evid = s.evid" in breaks.sql


def test_appconfig_application_and_environment(ops: Any) -> None:
    template = ops.template(ops.stage.ops)

    template.has_resource_properties("AWS::AppConfig::Application", {"Name": "fast-fast"})
    template.has_resource_properties(
        "AWS::AppConfig::Environment",
        {"Name": "fast", "ApplicationId": {"Ref": Match.string_like_regexp("AppConfigApplication")}},
    )
    exports = ops.stage.ops.exports
    assert (exports.lake_database, exports.lake_workgroup) == ("fast_lake", "fast-lake")
    for name in ("LakeDatabase", "LakeWorkGroup", "AppConfigApplicationId", "AppConfigEnvironmentId"):
        template.has_output(name, {})


def test_lake_database_names_have_no_hyphens() -> None:
    assert lake_database_name("fast") == "fast_lake"
    assert lake_database_name("fastci-pr7") == "fastci_pr7_lake"


def test_census_announces_changes_with_ops_on(ops: Any, default: Any) -> None:
    census = ops.stage.media.observability.census
    media = ops.template(ops.stage.media)

    assert census.last_count is not None
    media.has_resource_properties("AWS::SSM::Parameter", {"Name": "/fast/ops/running-channels", "Value": "0"})
    [machine] = [
        r
        for r in resources(media, "AWS::StepFunctions::StateMachine").values()
        if "Census Changed" in json.dumps(r["Properties"]["DefinitionString"])
    ]
    assert "fast-channels.fast" in json.dumps(machine["Properties"]["DefinitionString"])
    assert not [
        r
        for r in resources(default.template(default.stage.media), "AWS::StepFunctions::StateMachine").values()
        if "Census Changed" in json.dumps(r["Properties"]["DefinitionString"])
    ]


def _without_epg_flag(template: dict[str, Any]) -> dict[str, Any]:
    for resource in template["Resources"].values():
        if resource["Type"] == "AWS::Lambda::Function":
            resource["Properties"].get("Environment", {}).get("Variables", {}).pop("EPG_BREAK_IDS", None)
    return template


def test_ops_on_changes_only_the_guide_writers_environment_in_the_operator_stack(
    ops: Any, default: Any, normalized: Any
) -> None:
    template = normalized(ops.template(ops.stage.operator))
    flagged = {
        r["Properties"]["Handler"]
        for r in template["Resources"].values()
        if r["Type"] == "AWS::Lambda::Function"
        and r["Properties"].get("Environment", {}).get("Variables", {}).get("EPG_BREAK_IDS") == "true"
    }

    assert flagged == {
        "fast_channels.functions.tools.schedule.handler",
        "fast_channels.functions.tools.channels.handler",
    }
    assert _without_epg_flag(template) == normalized(default.template(default.stage.operator))


def test_with_ops_the_as_run_log_feeds_break_starts_and_a_sweep_decides_unaired_breaks(ops: Any, default: Any) -> None:
    media = ops.template(ops.stage.media)
    [subscription] = resources(media, "AWS::Logs::SubscriptionFilter").values()
    [v3_subscription] = resources(default.template(default.stage.media), "AWS::Logs::SubscriptionFilter").values()
    reconcilers = {
        r["Properties"]["Handler"]: r["Properties"]["Environment"]["Variables"]
        for r in resources(media, "AWS::Lambda::Function").values()
        if r["Properties"]["Handler"].startswith("fast_channels.functions.as_run_reconciler.")
    }
    [schedule] = [
        r["Properties"]
        for r in resources(media, "AWS::Scheduler::Schedule").values()
        if "never aired" in r["Properties"].get("Description", "")
    ]

    assert subscription["Properties"]["FilterPattern"] == (
        '{ ($.eventType = "VodProgramStart" || $.eventType = "AdBreakStart") && ($.channelName = "fast-*") }'
    )
    assert v3_subscription["Properties"]["FilterPattern"] == (
        '{ ($.eventType = "VodProgramStart") && ($.channelName = "fast-*") }'
    )
    assert set(reconcilers) == {
        "fast_channels.functions.as_run_reconciler.handler",
        "fast_channels.functions.as_run_reconciler.sweep",
    }
    assert all(env["AS_RUN_BREAKS"] == "true" for env in reconcilers.values())
    assert schedule["ScheduleExpression"] == "rate(15 minutes)"
    assert default.stage.media.as_run.sweeper is None
