from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.stub import Stubber

from fast_channels.functions import aws
from fast_channels.functions.tools import _alerts
from fast_channels.functions.tools._alerts import MAX_ALERTS, alerts_of, is_noise, relevant

ARN = "arn:aws:mediatailor:us-east-1:111122223333:channel/fast-demo"


@pytest.fixture
def mt() -> Iterator[Stubber]:
    with Stubber(aws.client("mediatailor")) as stub:
        yield stub
        stub.assert_no_pending_responses()


def alert(code: str, category: str = "PLAYBACK_WARNING", minute: int = 0, message: str = "") -> dict[str, Any]:
    return {
        "AlertCode": code,
        "AlertMessage": message or f"{code} on a program",
        "Category": category,
        "LastModifiedTime": datetime(2026, 10, 16, 12, minute, tzinfo=UTC),
        "RelatedResourceArns": [],
        "ResourceArn": ARN,
    }


@pytest.mark.parametrize(
    ("item", "noise"),
    [
        (alert("SEGMENT_DURATION_MISMATCH"), True),
        (alert("BANDWIDTH_MISMATCH"), True),
        (alert("PROGRAM_ALERT", message="SEGMENT_DURATION_MISMATCH: 2.000 s next to 6.000 s"), True),
        (alert("PROGRAM_ALERT", message="VOD_SOURCE_ALERT: UNREACHABLE"), False),
        (alert("CODEC_MISMATCH", message="mentions BANDWIDTH_MISMATCH but is no program alert"), False),
    ],
)
def test_noise_is_recognised_directly_and_wrapped_in_program_alerts(item: dict[str, Any], noise: bool) -> None:
    assert is_noise(item) is noise


def test_alerts_are_most_severe_then_newest_first_with_noise_counted(mt: Stubber) -> None:
    items = [
        alert("NOT_PROCESSED", "INFO", 9),
        alert("BANDWIDTH_MISMATCH", minute=8),
        alert("PROGRAM_ALERT", minute=7, message="SEGMENT_DURATION_MISMATCH: harmless"),
        alert("CODEC_MISMATCH", "SCHEDULING_ERROR", 1),
        alert("PROGRAM_ALERT", minute=6, message="x" * 1000),
        alert("VOD_SOURCE_ALERT", "SCHEDULING_ERROR", 5),
    ]
    mt.add_response("list_alerts", {"Items": items[:3], "NextToken": "t"}, {"ResourceArn": ARN})
    mt.add_response("list_alerts", {"Items": items[3:]}, {"ResourceArn": ARN, "NextToken": "t"})

    found, suppressed = alerts_of(ARN)

    assert [(a.code, a.category) for a in found] == [
        ("VOD_SOURCE_ALERT", "SCHEDULING_ERROR"),
        ("CODEC_MISMATCH", "SCHEDULING_ERROR"),
        ("PROGRAM_ALERT", "PLAYBACK_WARNING"),
        ("NOT_PROCESSED", "INFO"),
    ]
    assert suppressed == 2
    assert found[0].last_modified == "2026-10-16T12:05:00.000Z"
    assert len(found[2].message) == 400


def test_noise_is_kept_on_request(mt: Stubber) -> None:
    mt.add_response("list_alerts", {"Items": [alert("BANDWIDTH_MISMATCH")]}, {"ResourceArn": ARN})
    found, suppressed = alerts_of(ARN, include_noise=True)
    assert ([a.code for a in found], suppressed) == (["BANDWIDTH_MISMATCH"], 0)


def test_relevant_alerts_are_capped(mt: Stubber, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_alerts, "MAX_ALERTS", 2)
    items = [alert("NOT_PROCESSED", "INFO", minute) for minute in range(MAX_ALERTS)]
    mt.add_response("list_alerts", {"Items": items}, {"ResourceArn": ARN})
    assert [a.last_modified[11:16] for a in relevant(ARN)] == ["12:19", "12:18"]
