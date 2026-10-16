import json
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from aws_cdk.assertions import Match, Template

type GetDeployment = Callable[..., Any]

BUCKETS = ("StorageIngest", "StorageMedia", "StorageLogs")


@pytest.fixture(scope="module")
def data(deployment: GetDeployment) -> Template:
    return Template.from_stack(deployment().stage.data)


@pytest.fixture(scope="module")
def retained(deployment: GetDeployment) -> Template:
    return Template.from_stack(deployment({"removal": "retain"}).stage.data)


def resource(template: Template, type_: str, logical_prefix: str) -> Mapping[str, Any]:
    [(_, found)] = [(k, v) for k, v in template.find_resources(type_).items() if k.startswith(logical_prefix)]
    return found


def policy_statements(template: Template, bucket: str) -> list[dict[str, Any]]:
    policy = resource(template, "AWS::S3::BucketPolicy", f"{bucket}Policy")
    return list(policy["Properties"]["PolicyDocument"]["Statement"])


def statements_by_sid(template: Template, bucket: str) -> dict[str, dict[str, Any]]:
    return {s["Sid"]: s for s in policy_statements(template, bucket) if "Sid" in s}


@pytest.mark.parametrize("bucket", BUCKETS)
def test_buckets_are_private_sse_s3_bucket_owner_enforced_and_tls_only(data: Template, bucket: str) -> None:
    props = resource(data, "AWS::S3::Bucket", bucket)["Properties"]

    # Channel Assembly cannot read SSE-KMS manifests (channel-assembly.md §2.4).
    assert props["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [{"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
    }
    assert props["OwnershipControls"] == {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    assert set(props["PublicAccessBlockConfiguration"].values()) == {True}
    denies = [
        s["Condition"]
        for s in policy_statements(data, bucket)
        if s["Effect"] == "Deny" and s["Principal"] == {"AWS": "*"}
    ]
    assert {"Bool": {"aws:SecureTransport": "false"}} in denies
    assert {"NumericLessThan": {"s3:TlsVersion": 1.2}} in denies
    assert {
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        "Id": "abort-incomplete-uploads",
        "Status": "Enabled",
    } in props["LifecycleConfiguration"]["Rules"]


def test_one_bucket_policy_per_bucket(data: Template) -> None:
    policies = data.find_resources("AWS::S3::BucketPolicy")

    assert sorted(p["Properties"]["Bucket"]["Ref"][:-8] for p in policies.values()) == sorted(BUCKETS)


def test_ingest_bucket_emits_eventbridge_events_and_tiers_mezzanines(data: Template) -> None:
    props = resource(data, "AWS::S3::Bucket", "StorageIngest")["Properties"]

    assert props["NotificationConfiguration"] == {"EventBridgeConfiguration": {"EventBridgeEnabled": True}}
    assert {
        "Id": "mezzanines-intelligent-tiering",
        "Status": "Enabled",
        "Transitions": [{"StorageClass": "INTELLIGENT_TIERING", "TransitionInDays": 0}],
    } in (props["LifecycleConfiguration"]["Rules"])
    data.resource_count_is("Custom::S3BucketNotifications", 0)


@pytest.mark.parametrize(("bucket", "prefix"), [("StorageIngest", "s3/ingest/"), ("StorageMedia", "s3/media/")])
def test_server_access_logs_go_to_the_logs_bucket(data: Template, bucket: str, prefix: str) -> None:
    logging = resource(data, "AWS::S3::Bucket", bucket)["Properties"]["LoggingConfiguration"]

    assert logging["LogFilePrefix"] == prefix
    assert logging["DestinationBucketName"]["Ref"].startswith("StorageLogs")
    assert logging["TargetObjectKeyFormat"] == {"PartitionedPrefix": {"PartitionDateSource": "EventTime"}}


def test_logs_expire_with_the_configured_retention(data: Template) -> None:
    rules = resource(data, "AWS::S3::Bucket", "StorageLogs")["Properties"]["LifecycleConfiguration"]["Rules"]

    assert {"ExpirationInDays": 7, "Id": "expire-logs", "Status": "Enabled"} in rules


def test_media_bucket_cors_allows_players(data: Template) -> None:
    props = resource(data, "AWS::S3::Bucket", "StorageMedia")["Properties"]

    assert props["CorsConfiguration"] == {
        "CorsRules": [
            {"AllowedHeaders": ["*"], "AllowedMethods": ["GET", "HEAD"], "AllowedOrigins": ["*"], "MaxAge": 86400}
        ]
    }


def test_cloudfront_reads_media_but_never_content_manifests(data: Template) -> None:
    statements = statements_by_sid(data, "StorageMedia")

    allow, deny = statements["CloudFrontRead"], statements["CloudFrontDenyContentManifests"]
    assert allow["Principal"] == deny["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    assert (allow["Effect"], allow["Action"]) == ("Allow", "s3:GetObject")
    assert allow["Condition"] == {
        "StringEquals": {"AWS:SourceAccount": "123456789012"},
        "ArnLike": {"AWS:SourceArn": "arn:aws:cloudfront::123456789012:distribution/*"},
    }
    assert (deny["Effect"], deny["Action"], deny.get("Condition")) == ("Deny", "s3:GetObject", None)
    for arn, suffix in zip(deny["Resource"], ["/assets/*.m3u8", "/assets/*.mpd"], strict=True):
        [bucket_arn, key] = arn["Fn::Join"][1]
        assert (bucket_arn["Fn::GetAtt"][0][:12], key) == ("StorageMedia", suffix)


def test_channel_assembly_reads_asset_manifests_of_prefixed_source_locations(data: Template) -> None:
    statement = statements_by_sid(data, "StorageMedia")["ChannelAssemblyRead"]

    assert statement["Principal"] == {"Service": "mediatailor.amazonaws.com"}
    assert statement["Action"] == "s3:GetObject"
    assert statement["Resource"]["Fn::Join"][1][1] == "/assets/*"
    assert statement["Condition"] == {
        "StringEquals": {"aws:SourceAccount": "123456789012"},
        "ArnLike": {"aws:SourceArn": "arn:aws:mediatailor:us-east-1:123456789012:sourceLocation/fast*"},
    }


def test_log_deliveries_may_write_to_the_logs_bucket(data: Template) -> None:
    statements = statements_by_sid(data, "StorageLogs")
    source = {"ArnLike": {"aws:SourceArn": "arn:aws:logs:us-east-1:123456789012:*"}}

    write, acl = statements["LogDeliveryWrite"], statements["LogDeliveryAclCheck"]
    assert write["Principal"] == acl["Principal"] == {"Service": "delivery.logs.amazonaws.com"}
    assert (write["Action"], acl["Action"]) == ("s3:PutObject", "s3:GetBucketAcl")
    assert write["Condition"] == {
        "StringEquals": {"aws:SourceAccount": "123456789012", "s3:x-amz-acl": "bucket-owner-full-control"},
        **source,
    }
    assert acl["Condition"] == {"StringEquals": {"aws:SourceAccount": "123456789012"}, **source}
    principals = [s["Principal"] for s in policy_statements(data, "StorageLogs")]
    assert {"Service": "logging.s3.amazonaws.com"} in principals


def test_data_stack_references_no_other_stack(data: Template) -> None:
    template = json.dumps(data.to_json())

    assert "Fn::GetStackOutput" not in template and "Fn::ImportValue" not in template


@pytest.mark.parametrize(("table", "ttl"), [("Library", "expires_at"), ("Catalog", None)])
def test_tables(data: Template, table: str, ttl: str | None) -> None:
    props = resource(data, "AWS::DynamoDB::GlobalTable", table)["Properties"]

    assert props["KeySchema"] == [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ]
    assert props["GlobalSecondaryIndexes"] == [
        {
            "IndexName": "GSI1",
            "KeySchema": [
                {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
    ]
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert "SSESpecification" not in props  # AWS-owned key
    [replica] = props["Replicas"]
    assert replica["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    assert replica["DeletionProtectionEnabled"] is False
    assert props.get("TimeToLiveSpecification") == ({"AttributeName": ttl, "Enabled": True} if ttl else None)


def test_agent_memory_keeps_events_30_days_and_only_user_preferences(data: Template) -> None:
    data.has_resource_properties(
        "AWS::BedrockAgentCore::Memory",
        {
            "Name": "fast_memory",
            "EventExpiryDuration": 30,
            "MemoryStrategies": [{"UserPreferenceMemoryStrategy": Match.any_value()}],
        },
    )


def test_no_memory_without_the_agent(deployment: GetDeployment) -> None:
    d = deployment({"agent.enabled": False})

    d.template(d.stage.data).resource_count_is("AWS::BedrockAgentCore::Memory", 0)
    assert d.stage.data.exports.memory is None


def test_destroy_empties_and_deletes_everything(data: Template) -> None:
    for type_ in ("AWS::S3::Bucket", "AWS::DynamoDB::GlobalTable", "AWS::BedrockAgentCore::Memory"):
        assert {r["DeletionPolicy"] for r in data.find_resources(type_).values()} == {"Delete"}, type_
    data.resource_count_is("Custom::S3AutoDeleteObjects", 3)


def test_retain_keeps_data_and_protects_tables(retained: Template) -> None:
    for type_ in ("AWS::S3::Bucket", "AWS::DynamoDB::GlobalTable", "AWS::BedrockAgentCore::Memory"):
        assert {r["DeletionPolicy"] for r in retained.find_resources(type_).values()} == {"Retain"}, type_
    retained.resource_count_is("Custom::S3AutoDeleteObjects", 0)
    for table in retained.find_resources("AWS::DynamoDB::GlobalTable").values():
        assert table["Properties"]["Replicas"][0]["DeletionProtectionEnabled"] is True


def test_outputs_for_scripts(data: Template) -> None:
    assert set(data.find_outputs("*")) == {
        "IngestBucketName",
        "MediaBucketName",
        "LogsBucketName",
        "LibraryTableName",
        "CatalogTableName",
        "MemoryId",
    }
