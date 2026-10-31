"""The crawl module in the release bundle: the Release stack renders the PRE_ADS content lookup when ``modules.crawl``
and its ``pre_ads`` are on, and leaves PRE_ADS unmapped otherwise."""

from collections.abc import Callable
from typing import Any

import pytest

from fast_channels.core.demand import NETWORK_CONTEXT
from fast_channels.core.monetization.functions import Hook
from fast_channels.core.monetization.validators import validate_bundle

NETWORK: dict[str, Any] = {
    "vpc_id": "vpc-0123456789abcdef0",
    "private_subnets": [
        {"id": "subnet-0aaaaaaaaaaaaaaa1", "az": "us-east-1a", "route_table": "rtb-0aaaaaaaaaaaaaaa1"},
        {"id": "subnet-0bbbbbbbbbbbbbbb2", "az": "us-east-1b", "route_table": "rtb-0bbbbbbbbbbbbbbb2"},
    ],
    "task_security_group_id": "sg-0123456789abcdef0",
    "cluster_arn": "arn:aws:ecs:us-east-1:123456789012:cluster/fastm-pbs-PrebidCluster",
    "service_arn": "arn:aws:ecs:us-east-1:123456789012:service/fastm-pbs-PrebidCluster/fastm-pbs-Service",
    "cloudfront_domain": "d111111abcdef8.cloudfront.net",
    "target_group_arn": "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/fastm-pbs/0123456789abcd",
    "simulator_code_key": "simulator/0123456789abcdef.zip",
}
"""The guidance's network as ``scripts/prebid_guidance.py`` passes it (the Release stack deploys beside it)."""
RELEASE: dict[str, Any] = {
    "prefix": "fastm",
    "modules.ops.enabled": True,
    "modules.demand.enabled": True,
    "modules.demand.simulator": "lambda",
    "modules.pds.enabled": True,
    "modules.release.enabled": True,
    "modules.release.channels": ["{prefix}-demo"],
}


def bundle(deployment: Callable[..., Any], **crawl: Any) -> Any:
    overrides = RELEASE | {f"modules.crawl.{key}": value for key, value in crawl.items()}
    release = deployment(overrides, extra={NETWORK_CONTEXT: NETWORK}).stage.release
    assert release is not None
    return release.rendered.bundle


def test_the_lookup_is_mapped_on_the_general_siblings(deployment: Callable[..., Any]) -> None:
    rendered = bundle(deployment, enabled=True)
    assert validate_bundle(rendered) == []
    mapped = {s.name: (s.playback.function_mapping or {}).get(Hook.PRE_ADS) for s in rendered.siblings}
    assert mapped == {  # the monetized channel's siblings and the lab channel's (M3-04)
        f"fastm-{c}-p-{s}": "fastm_pa_root" if s != "z" else None for c in ("demo", "lab") for s in ("a", "b", "z")
    }
    meta = rendered.function("fastm_pa_meta")
    assert meta is not None
    assert "/catalog/ctx/v1/' & $encodeUrlComponent($id)" in meta.http_request_configuration.url
    # The content base URL is the Media stack's SSM parameter, a deploy-time token, placeholdered while validating.
    assert "https://cdk-token-" in meta.http_request_configuration.url


@pytest.mark.parametrize("crawl", [{"enabled": False}, {"enabled": True, "pre_ads": False}])
def test_no_lookup_without_the_module_or_with_pre_ads_off(deployment: Callable[..., Any], crawl: Any) -> None:
    rendered = bundle(deployment, **crawl)
    assert rendered.function("fastm_pa_meta") is None
    assert all(Hook.PRE_ADS not in (s.playback.function_mapping or {}) for s in rendered.siblings)
