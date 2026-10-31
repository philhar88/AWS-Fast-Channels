"""pbs_config in the ad-server mode (M8-09): the stored requests that ask the fast-ctv module to cache the pod, Prebid
Cache's write endpoint inside the VPC, and the exitpoint's budget for the write. With the mode off nothing changes
(``test_pbs_config``'s golden files)."""

import json
from typing import TYPE_CHECKING

import pytest
from syrupy.extensions.json import JSONSnapshotExtension
from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode

from fast_channels.core.pbs_config import (
    POD_CACHE_GROUP_MS,
    PbsSettings,
    account,
    prebid_config,
    render,
    stored_request,
)

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

CDN = "https://d111111abcdef8.cloudfront.net"
SETTINGS = PbsSettings(
    prefix="fastm",
    simulator=True,
    server_instances=2,
    tracker_base_url=CDN,
    publisher_domain="d111111abcdef8.cloudfront.net",
    ad_server=True,
)


class YamlSnapshot(SingleFileSnapshotExtension):
    file_extension = "yaml"
    _write_mode = WriteMode.TEXT


def test_the_mode_adds_its_stored_requests() -> None:
    added = sorted(set(render(SETTINGS)) - set(render(SETTINGS.model_copy(update={"ad_server": False}))))
    assert added == ["stored_requests/fast-adsrv-pod-kids.json", "stored_requests/fast-adsrv-pod.json"]


@pytest.mark.parametrize("kids", [False, True])
def test_a_stored_request_is_the_pod_with_the_pod_cache_ask(kids: bool) -> None:
    direct, cached = stored_request(SETTINGS, kids=kids), stored_request(SETTINGS, kids=kids, ad_server=True)
    assert cached.pop("id") == f"fast-adsrv-pod{'-kids' if kids else ''}"
    direct.pop("id")
    assert cached["ext"]["fast"].pop("pod_cache") == {"ttlseconds": 300}
    assert cached["ext"]["prebid"].pop("targeting") == {
        "pricegranularity": {
            "precision": 2,
            "ranges": [
                {"min": 0, "max": 20, "increment": 0.10},
                {"min": 20, "max": 50, "increment": 0.50},
                {"min": 50, "max": 100, "increment": 1.00},
            ],
        },
        "includewinners": True,
        "includebidderkeys": False,
    }
    assert cached == direct
    assert "cache" not in cached["ext"]["prebid"]  # Prebid Server's core never caches single bids


@pytest.mark.parametrize("path", ["stored_requests/fast-adsrv-pod.json", "stored_accounts/fastm.json"])
def test_golden_json(path: str, snapshot: SnapshotAssertion) -> None:
    assert json.loads(render(SETTINGS)[path]) == snapshot.use_extension(JSONSnapshotExtension)


def test_golden_config(snapshot: SnapshotAssertion) -> None:
    assert render(SETTINGS)["current-config/prebid-config.yaml"] == snapshot.use_extension(YamlSnapshot)


def test_writes_stay_inside_the_vpc_and_the_exitpoint_waits_for_them() -> None:
    config = prebid_config(SETTINGS)
    assert config["cache"]["internal"] == {"scheme": "http", "host": "cache.fastm.fast.internal", "path": "/cache"}
    assert config["cache"]["host"] == "${CACHE_HOST}"  # the targeting names the public host
    assert "internal" not in prebid_config(SETTINGS.model_copy(update={"ad_server": False}))["cache"]
    stages = account(SETTINGS)["hooks"]["execution-plan"]["endpoints"]["/openrtb2/auction"]["stages"]
    assert stages["exitpoint"]["groups"][0]["timeout"] == SETTINGS.select_group_timeout_ms + POD_CACHE_GROUP_MS
