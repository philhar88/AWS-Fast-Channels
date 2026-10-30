"""The siblings' ADS log against MediaTailor's counts: requests it made but did not log."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from botocore.stub import ANY, Stubber

from fast_channels.functions.release import reconcile

if TYPE_CHECKING:
    import pytest

type Stub = Callable[[str], Stubber]
SIBLINGS = ["fastm-demo-p-a", "fastm-demo-p-b"]
AT = datetime(2026, 10, 29, 21, 5, 30, tzinfo=UTC)


def metric_data(values: dict[str, list[float]]) -> dict[str, Any]:
    return {"MetricDataResults": [{"Id": k, "Values": v, "StatusCode": "Complete"} for k, v in values.items()]}


def test_the_window_is_a_whole_hour_that_ended_twenty_minutes_ago() -> None:
    start, end = reconcile.window(AT)
    assert (start, end) == (datetime(2026, 10, 29, 19, 45, tzinfo=UTC), datetime(2026, 10, 29, 20, 45, tzinfo=UTC))


def test_a_request_the_log_left_out_is_counted_and_alarmed(
    stub: Stub, monkeypatch: pytest.MonkeyPatch, context: Any
) -> None:
    """fastm, 20:00-21:00Z: 5 ADS responses on p-b, 4 MAKING_ADS_REQUEST in its log."""
    monkeypatch.setenv("RELEASE_SIBLINGS", ",".join(SIBLINGS))
    reconcile.settings.cache_clear()
    stub("cloudwatch").add_response(
        "get_metric_data",
        metric_data({"m0_requests": [3.0], "m0_avails": [3.0], "m1_requests": [5.0], "m1_avails": [5.0]}),
        {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY},
    )
    rows = [
        {"originId": "fastm-demo-p-a", "eventType": "MAKING_ADS_REQUEST", "n": "3"},
        {"originId": "fastm-demo-p-a", "eventType": "FILLED_AVAIL", "n": "3"},
        {"originId": "fastm-demo-p-b", "eventType": "MAKING_ADS_REQUEST", "n": "4"},
        {"originId": "fastm-demo-p-b", "eventType": "FILLED_AVAIL", "n": "4"},
    ]
    windows: list[tuple[float, float]] = []

    def insights(group: str, query: str, start: float, end: float) -> list[dict[str, str]]:
        windows.append((start, end))
        return rows

    monkeypatch.setattr(reconcile.smoke, "insights", insights)
    result = reconcile.handler({"scheduled_time": "2026-10-29T21:20:00Z"}, context)
    assert result["unlogged_requests"] == 1
    assert result["siblings"]["fastm-demo-p-b"]["unlogged_requests"] == 1
    assert result["siblings"]["fastm-demo-p-a"] == {
        "counted": {"requests": 3, "avails": 3},
        "logged": {"requests": 3, "avails": 3},
        "unlogged_requests": 0,
    }
    start, end = reconcile.window(datetime(2026, 10, 29, 21, 20, tzinfo=UTC))
    assert windows == [((start - reconcile.EDGE).timestamp(), (end + reconcile.EDGE).timestamp())]


def test_a_sibling_with_no_traffic_has_nothing_missing(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    stub("cloudwatch").add_response("get_metric_data", metric_data({"m0_requests": [], "m0_avails": []}), None)
    monkeypatch.setattr(reconcile.smoke, "insights", lambda *args: [])
    result = reconcile.reconcile(SIBLINGS[:1], AT)
    assert result["unlogged_requests"] == 0
    assert result["siblings"]["fastm-demo-p-a"]["logged"] == {"requests": 0, "avails": 0}
