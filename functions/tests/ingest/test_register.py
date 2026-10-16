from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError

from fast_channels.core.ladder import Ladder
from fast_channels.functions.ingest import register
from fast_channels.functions.ingest.register import VodSourceAlertError, VodSourceBusyError

STANDARD = Ladder.builtin("standard")
PREFIX = f"assets/filler-1a2b3c/standard_{STANDARD.fingerprint}/"
NAME = f"filler-1a2b3c-standard_{STANDARD.fingerprint}"
ARN = f"arn:aws:mediatailor:us-east-1:123456789012:vodSource/fast-origin/{NAME}"
EVENT = {
    "asset_id": "filler-1a2b3c",
    "ladder_version": STANDARD.version,
    "kind": "slate",
    "vod_source": NAME,
    "output_prefix": PREFIX,
    "profiles": ["hls", "hls-avc", "dash-avc"],
    "correlation_id": "kit-1-filler",
}
PACKAGES = [
    {"Path": f"/{PREFIX}main.m3u8", "SourceGroup": "hls", "Type": "HLS"},
    {"Path": f"/{PREFIX}main_avc.m3u8", "SourceGroup": "hls-avc", "Type": "HLS"},
    {"Path": f"/{PREFIX}main_avc.mpd", "SourceGroup": "dash-avc", "Type": "DASH"},
]
TAGS = {
    "fast:app": "fast",
    "fast:owner": "pipeline",
    "fast:ladder": STANDARD.version,
    "fast:correlation-id": "kit-1-filler",
    "fast:asset": "filler-1a2b3c",
}
SOURCE = {
    "Arn": ARN,
    "VodSourceName": NAME,
    "SourceLocationName": "fast-origin",
    "HttpPackageConfigurations": PACKAGES,
    "CreationTime": datetime.now(UTC),
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fast_channels.functions.ingest.register.time.sleep", lambda seconds: None)


def alerts(*codes: tuple[str, str]) -> dict[str, Any]:
    return {
        "Items": [
            {
                "AlertCode": code,
                "AlertMessage": f"{code} message",
                "Category": category,
                "LastModifiedTime": datetime.now(UTC),
                "RelatedResourceArns": [],
                "ResourceArn": ARN,
            }
            for code, category in codes
        ]
    }


def test_creates_a_new_vod_source(stubs: Any, lambda_context: Any) -> None:
    mt = stubs["mediatailor"]
    mt.add_client_error("describe_vod_source", "NotFoundException", http_status_code=404)
    mt.add_response(
        "create_vod_source",
        SOURCE,
        {
            "SourceLocationName": "fast-origin",
            "VodSourceName": NAME,
            "HttpPackageConfigurations": PACKAGES,
            "Tags": TAGS,
        },
    )
    mt.add_response("list_alerts", alerts(("NOT_PROCESSED", "INFO")), {"ResourceArn": ARN})

    assert register.handler(EVENT, lambda_context) == {
        "vod_source": NAME,
        "vod_source_arn": ARN,
        "source_location": "fast-origin",
        "packages": {
            "hls": f"/{PREFIX}main.m3u8",
            "hls-avc": f"/{PREFIX}main_avc.m3u8",
            "dash-avc": f"/{PREFIX}main_avc.mpd",
        },
        "alerts": ["NOT_PROCESSED"],
    }


def test_updates_an_existing_vod_source(stubs: Any, lambda_context: Any) -> None:
    mt = stubs["mediatailor"]
    mt.add_response("describe_vod_source", SOURCE, {"SourceLocationName": "fast-origin", "VodSourceName": NAME})
    mt.add_response(
        "update_vod_source",
        SOURCE,
        {"SourceLocationName": "fast-origin", "VodSourceName": NAME, "HttpPackageConfigurations": PACKAGES},
    )
    mt.add_response("tag_resource", {}, {"ResourceArn": ARN, "Tags": TAGS})
    mt.add_response("list_alerts", alerts(), {"ResourceArn": ARN})

    assert register.handler(EVENT, lambda_context)["alerts"] == []


def test_waits_for_scheduling_errors_to_clear(stubs: Any) -> None:
    mt = stubs["mediatailor"]
    mt.add_response("list_alerts", alerts(("UNREACHABLE", "SCHEDULING_ERROR")), {"ResourceArn": ARN})
    mt.add_response("list_alerts", alerts(("BANDWIDTH_MISMATCH", "PLAYBACK_WARNING")), {"ResourceArn": ARN})

    assert register.wait_for_alerts(ARN) == ["BANDWIDTH_MISMATCH"]


def test_persistent_scheduling_errors_fail(stubs: Any) -> None:
    for _ in range(register.ALERT_POLLS):
        stubs["mediatailor"].add_response(
            "list_alerts", alerts(("UNAUTHORIZED", "SCHEDULING_ERROR")), {"ResourceArn": ARN}
        )

    with pytest.raises(VodSourceAlertError, match="UNAUTHORIZED: UNAUTHORIZED message"):
        register.wait_for_alerts(ARN)


def test_conflicts_are_retryable(stubs: Any, lambda_context: Any) -> None:
    stubs["mediatailor"].add_client_error("describe_vod_source", "ConflictException", http_status_code=409)

    with pytest.raises(VodSourceBusyError):
        register.handler(EVENT, lambda_context)


def test_a_ladder_without_registered_profiles_fails(lambda_context: Any) -> None:
    with pytest.raises(VodSourceAlertError, match="none of the deployment's package profiles"):
        register.handler({**EVENT, "profiles": []}, lambda_context)


def test_other_errors_propagate(stubs: Any, lambda_context: Any) -> None:
    stubs["mediatailor"].add_client_error("describe_vod_source", "AccessDeniedException", http_status_code=403)

    with pytest.raises(ClientError):
        register.handler(EVENT, lambda_context)
