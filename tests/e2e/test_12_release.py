"""The release module (``modules.release``) on a deployed stage: the Release Controller's first release of every
monetized channel's siblings (BUILD-PLAN M3-03).

Runs only when the stage has the Release stack (``deploy-demand`` deploys it beside the guidance); skipped
otherwise. It reads the siblings, their Functions and vended-log deliveries, the playback plan through the CDN and
the ledger, and invokes the drift check once. It changes nothing.
"""

import json
from typing import Any, Final

import httpx
import pytest
from _stack import Module, Stage
from boto3.dynamodb.conditions import Key

from fast_channels.core.mediatailor_logs import EXCLUDED_ADS_LOG_FIELDS, EXCLUDED_MANIFEST_LOG_FIELDS
from fast_channels.core.monetization import names
from fast_channels.core.monetization.playback import PERSONALIZATION_THRESHOLD_SECONDS, RELEASE_OWNER

pytestmark = pytest.mark.e2e

DELIVERIES_PER_SIBLING: Final = 5
"""Ads and manifest logs to the module's log group and the lake, transcode logs to the log group."""


@pytest.fixture(scope="module")
def release(stage: Stage) -> Stage:
    if stage.status(Module.RELEASE) is None:
        pytest.skip(f"stage {stage.prefix} has no Release stack (modules.release is off, or deploy-demand not run)")
    return stage


@pytest.fixture(scope="module")
def siblings(release: Stage) -> dict[str, dict[str, Any]]:
    """GetPlaybackConfiguration of every sibling the stack names."""
    mediatailor = release.session.client("mediatailor", region_name=release.region)
    return {
        name: dict(mediatailor.get_playback_configuration(Name=name))
        for name in release.json_output(Module.RELEASE, "Siblings")
    }


def test_every_sibling_posts_to_prebid_server_with_the_monetized_settings(siblings: dict[str, Any]) -> None:
    for name, configuration in siblings.items():
        server = configuration["AdDecisionServerConfiguration"]
        assert server["HttpRequest"]["Method"] == "POST" and server["HttpRequest"]["Body"], name
        assert "X-Fast-Origin" in server["HttpRequest"]["Headers"], name  # the value is never read here
        assert server["VastResponse"]["AdSequencingMode"] == "FOLLOW_AD_SEQUENCE", name
        assert configuration["PersonalizationThresholdSeconds"] == PERSONALIZATION_THRESHOLD_SECONDS, name
        assert configuration["AvailSuppression"]["Mode"] == "BEHIND_LIVE_EDGE", name
        assert configuration["Tags"]["fast:owner"] == RELEASE_OWNER, name
        roots = configuration["FunctionMapping"]  # one root per hook (PRE_SESSION, and PRE_ADS with the crawl)
        assert roots and all(r.endswith(configuration["Tags"]["fast:release"]) for r in roots.values()), (name, roots)


def test_the_mapped_functions_exist(release: Stage, siblings: dict[str, Any]) -> None:
    mediatailor = release.session.client("mediatailor", region_name=release.region)
    for configuration in siblings.values():
        for function_id in configuration["FunctionMapping"].values():
            found = mediatailor.get_function(FunctionId=function_id)
            assert found["Tags"]["fast:owner"] == RELEASE_OWNER
            for step in found.get("SequentialExecutorConfiguration", {}).get("FunctionList", []):
                mediatailor.get_function(FunctionId=step["FunctionId"])


def test_the_siblings_logs_leave_out_the_fields_that_may_hold_a_secret(release: Stage) -> None:
    logs = release.session.client("logs", region_name=release.region)
    names_ = release.json_output(Module.RELEASE, "Siblings")
    deliveries = [
        d
        for page in logs.get_paginator("describe_deliveries").paginate()
        for d in page["deliveries"]
        if d["deliverySourceName"].rsplit("-", 1)[0] in names_
    ]
    assert len(deliveries) == DELIVERIES_PER_SIBLING * len(names_)
    for delivery in deliveries:
        fields = set(delivery.get("recordFields", []))
        if delivery["deliverySourceName"].endswith("-ads"):
            assert fields and not fields & EXCLUDED_ADS_LOG_FIELDS, delivery["deliverySourceName"]
        if delivery["deliverySourceName"].endswith("-manifest"):
            assert fields and not fields & EXCLUDED_MANIFEST_LOG_FIELDS, delivery["deliverySourceName"]
    group = release.output(Module.RELEASE, "SiblingLogGroup")
    [described] = logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]
    assert described.get("kmsKeyId"), "the siblings' log group is encrypted with the module's key"


def test_the_plan_names_each_channels_stable_sibling(release: Stage, siblings: dict[str, Any]) -> None:
    ssm = release.session.client("ssm", region_name=release.region)
    cdn = ssm.get_parameter(Name=names.parameter_name(release.prefix, names.Parameter.CDN_BASE_URL))["Parameter"]
    for channel in sorted({s.rsplit("-p-", 1)[0] for s in siblings}):
        response = httpx.get(f"{cdn['Value'].rstrip('/')}/{names.plan_key(channel)}", timeout=10)
        response.raise_for_status()
        plan = response.json()
        assert plan["configuration"] in siblings, plan
        assert response.headers.get("Cache-Control") == names.PLAN_CACHE_CONTROL
        assert all(url.startswith(cdn["Value"].rstrip("/")) for url in plan["playback"].values()), plan


def test_nothing_drifted(release: Stage, siblings: dict[str, Any]) -> None:
    client = release.session.client("lambda", region_name=release.region)
    response = client.invoke(
        FunctionName=release.output(Module.RELEASE, "DriftFunction"), Payload=b'{"change_id": "e2e"}'
    )
    result = json.loads(response["Payload"].read())
    assert result == {"siblings": len(siblings), "drift": []}, result


def test_the_ledger_records_the_release(release: Stage, siblings: dict[str, Any]) -> None:
    table = release.session.resource("dynamodb", region_name=release.region).Table(f"{release.prefix}-ledger")

    def actions(chain: str) -> set[str]:
        items = table.query(KeyConditionExpression=Key("pk").eq(chain))["Items"]
        return {str(item["action"]) for item in items if "action" in item}

    assert {"release.accept"} <= actions(names.BUNDLE_CHAIN)
    for channel in {s.rsplit("-p-", 1)[0] for s in siblings}:
        assert "release.apply" in actions(names.channel_chain(channel))
