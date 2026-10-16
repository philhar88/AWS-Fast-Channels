import json
from collections.abc import Callable
from typing import Any

import pytest
from aws_cdk.assertions import Template


@pytest.fixture(scope="module")
def baseline(deployment: Callable[..., Any]) -> Template:
    return Template.from_stack(deployment({"observability.transaction_search": "enable"}).stage.baseline)


def test_transaction_search_indexes_one_percent_after_the_spans_policy(baseline: Template) -> None:
    [(policy_id, _)] = baseline.find_resources("AWS::Logs::ResourcePolicy").items()

    baseline.has_resource(
        "AWS::XRay::TransactionSearchConfig",
        {
            "Properties": {"IndexingPercentage": 1},
            "DependsOn": [policy_id],
        },
    )


def test_xray_may_write_spans_to_cloudwatch_logs(baseline: Template) -> None:
    [policy] = baseline.find_resources("AWS::Logs::ResourcePolicy").values()
    document = json.loads(policy["Properties"]["PolicyDocument"])

    assert policy["Properties"]["PolicyName"] == "FastChannels-TransactionSearch"
    assert document["Statement"] == [
        {
            "Sid": "TransactionSearchXRayAccess",
            "Effect": "Allow",
            "Principal": {"Service": "xray.amazonaws.com"},
            "Action": "logs:PutLogEvents",
            "Resource": [
                "arn:aws:logs:us-east-1:123456789012:log-group:aws/spans:*",
                "arn:aws:logs:us-east-1:123456789012:log-group:/aws/application-signals/data:*",
            ],
            "Condition": {
                "ArnLike": {"aws:SourceArn": "arn:aws:xray:us-east-1:123456789012:*"},
                "StringEquals": {"aws:SourceAccount": "123456789012"},
            },
        }
    ]


def test_baseline_survives_deletion(baseline: Template) -> None:
    resources = baseline.to_json()["Resources"].values()

    assert {(r["Type"], r["DeletionPolicy"], r["UpdateReplacePolicy"]) for r in resources} == {
        ("AWS::Logs::ResourcePolicy", "Retain", "Retain"),
        ("AWS::XRay::TransactionSearchConfig", "Retain", "Retain"),
    }
