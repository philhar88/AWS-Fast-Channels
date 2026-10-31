"""The PDS sidecar as the guidance wrapper adds it: its environment, its grants and its sizes (ADR-0036)."""

import json

from fast_channels.core.pbs_config import PDS_BASE_URL
from fast_channels.core.pds.names import PORT, sidecar_log_group
from fast_channels.core.pds.sidecar import HEALTH_COMMAND, TASK_CPU, TASK_MEMORY_MIB, Sidecar, sidecar

ARGS = {
    "prefix": "fastm",
    "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/fastm-pds:0123456789abcdef",
    "partition": "aws",
    "region": "us-east-1",
    "account": "123456789012",
    "config_bucket": "fastm-pds-config-0123",
    "content_base_url": "https://d111111abcdef8.cloudfront.net",
    "log_retention_days": 30,
}
TABLE = "arn:aws:dynamodb:us-east-1:123456789012:table/fastm-pds"


def by_sid(spec: Sidecar) -> dict[str, dict[str, object]]:
    return {str(s["Sid"]): s for s in spec.statements}


def test_the_grants_are_the_hot_path_functions_and_no_more() -> None:
    """The snapshot object; the budget shards' reads; the reservations' writes; the ledger's pds/* chains."""
    granted = by_sid(sidecar(**ARGS))

    assert granted == {
        "PdsSnapshot": {
            "Sid": "PdsSnapshot",
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::fastm-pds-config-0123/snapshot/current.json",
        },
        "PdsBudgetReads": {
            "Sid": "PdsBudgetReads",
            "Effect": "Allow",
            "Action": "dynamodb:BatchGetItem",
            "Resource": TABLE,
            "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["BUDGET#*"]}},
        },
        "PdsReservations": {
            "Sid": "PdsReservations",
            "Effect": "Allow",
            "Action": "dynamodb:UpdateItem",
            "Resource": TABLE,
            "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["BUDGET#*", "CAP#*"]}},
        },
        "PdsReservationItems": {
            "Sid": "PdsReservationItems",
            "Effect": "Allow",
            "Action": "dynamodb:PutItem",
            "Resource": TABLE,
            "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["RES#*"]}},
        },
        "PdsLedger": {
            "Sid": "PdsLedger",
            "Effect": "Allow",
            "Action": "dynamodb:PutItem",
            "Resource": "arn:aws:dynamodb:us-east-1:123456789012:table/fastm-ledger",
            "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["pds/*"]}},
        },
    }
    assert "*" not in json.dumps([s["Action"] for s in granted.values()])


def test_the_s3_reads_join_the_guidance_vpcs_endpoint_policy() -> None:
    """The guidance's S3 gateway endpoint admits only its own buckets; the sidecar's reads join its policy, for the
    account's objects only."""
    own = {"StringEquals": {"aws:ResourceAccount": "123456789012"}}

    assert sidecar(**ARGS).endpoint_statements == (
        {
            "Sid": "PdsSnapshot",
            "Effect": "Allow",
            "Principal": {"AWS": "*"},
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::fastm-pds-config-0123/snapshot/current.json",
            "Condition": own,
        },
    )
    [_, contexts] = sidecar(**ARGS, catalog_context_bucket="fastm-catalog-context").endpoint_statements
    assert contexts["Resource"] == [
        "arn:aws:s3:::fastm-catalog-context",
        "arn:aws:s3:::fastm-catalog-context/catalog/ctx/v1/*",
    ]
    assert (contexts["Action"], contexts["Condition"]) == (["s3:ListBucket", "s3:GetObject"], own)


def test_the_dynamodb_endpoint_admits_only_item_calls_on_the_sidecars_tables() -> None:
    [statement] = sidecar(**ARGS).dynamodb_endpoint_statements

    assert statement["Resource"] == [TABLE, "arn:aws:dynamodb:us-east-1:123456789012:table/fastm-ledger"]
    assert statement["Action"] == [
        "dynamodb:BatchGetItem",
        "dynamodb:ConditionCheckItem",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
    ]
    assert statement["Condition"] == {"StringEquals": {"aws:ResourceAccount": "123456789012"}}


def test_with_the_crawl_module_it_lists_and_reads_only_the_context_objects() -> None:
    spec = sidecar(
        **ARGS,
        crawl={"CRAWL_HOLDOUT_PERCENT": "20"},
        catalog_context_bucket="fastm-catalog-context",
    )
    granted = by_sid(spec)

    assert granted["PdsCatalogContextList"]["Condition"] == {"StringEquals": {"s3:prefix": "catalog/ctx/v1/"}}
    assert granted["PdsCatalogContexts"]["Resource"] == "arn:aws:s3:::fastm-catalog-context/catalog/ctx/v1/*"
    assert spec.environment["CATALOG_CONTEXT_BUCKET"] == "fastm-catalog-context"
    assert spec.environment["CRAWL_HOLDOUT_PERCENT"] == "20"


def test_the_environment_names_the_stages_state_and_cdn() -> None:
    spec = sidecar(**ARGS)

    assert spec.environment == {
        "FAST_PREFIX": "fastm",
        "POWERTOOLS_SERVICE_NAME": "pds",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "PDS_CONFIG_BUCKET": "fastm-pds-config-0123",
        "PDS_TABLE": "fastm-pds",
        "LEDGER_TABLE": "fastm-ledger",
        "CONTENT_BASE_URL": "https://d111111abcdef8.cloudfront.net",
        "PDS_POLICY_TIMEOUT_MS": "50",
        "PDS_SELECT_TIMEOUT_MS": "200",
    }
    assert (spec.log_group, spec.log_retention_days) == (sidecar_log_group("fastm"), 30)
    assert (spec.container, spec.port, spec.task_cpu, spec.task_memory_mib) == (
        "Pds-Container",
        PORT,
        TASK_CPU,
        TASK_MEMORY_MIB,
    )


def test_prebid_server_and_the_health_check_reach_the_same_port_on_the_loopback() -> None:
    assert f"http://127.0.0.1:{PORT}" == PDS_BASE_URL
    assert f"('127.0.0.1', {PORT})" in HEALTH_COMMAND[-1]
    assert HEALTH_COMMAND[:3] == ("CMD", "python", "-S"), "no site: a cheap interpreter every 5 s"
    assert Sidecar.model_validate_json(sidecar(**ARGS).model_dump_json()) == sidecar(**ARGS)
