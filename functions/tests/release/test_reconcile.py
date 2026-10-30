"""The siblings' ADS log against MediaTailor's counts: filled ad time it served but did not log."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from botocore.stub import ANY, Stubber

from fast_channels.functions.release import reconcile

if TYPE_CHECKING:
    import pytest

type Stub = Callable[[str], Stubber]
SIBLINGS = ["fastm-demo-p-a", "fastm-demo-p-b"]
GROUP = "/aws/vendedlogs/mediatailor/fastm-release"
AT = datetime(2026, 10, 29, 21, 5, 30, tzinfo=UTC)


def metric_data(values: dict[str, list[float]]) -> dict[str, Any]:
    return {"MetricDataResults": [{"Id": k, "Values": v, "StatusCode": "Complete"} for k, v in values.items()]}


def test_the_window_is_a_whole_hour_that_ended_twenty_minutes_ago() -> None:
    start, end = reconcile.window(AT)
    assert (start, end) == (datetime(2026, 10, 29, 19, 45, tzinfo=UTC), datetime(2026, 10, 29, 20, 45, tzinfo=UTC))


def log_group(stub: Stub, created: datetime | None) -> None:
    groups = [{"logGroupName": GROUP, "creationTime": int(created.timestamp() * 1000)}] if created else []
    stub("logs").add_response("describe_log_groups", {"logGroups": groups}, {"logGroupNamePrefix": GROUP})


def test_a_filled_break_the_log_left_out_is_counted_and_alarmed(
    stub: Stub, monkeypatch: pytest.MonkeyPatch, context: Any
) -> None:
    """fastm, 20:00-21:00Z: p-b served 5 filled 60.06 s avails, its log has 4 FILLED_AVAIL (a break left out)."""
    monkeypatch.setenv("RELEASE_SIBLINGS", ",".join(SIBLINGS))
    reconcile.settings.cache_clear()
    log_group(stub, datetime(2026, 10, 29, 12, tzinfo=UTC))
    stub("cloudwatch").add_response(
        "get_metric_data",
        metric_data(
            {
                "m0_avails": [4.0],
                "m0_filled_ms": [3 * 60060.0],  # one avail unfilled: a sample of 0, no FILLED_AVAIL
                "m1_avails": [5.0],
                "m1_filled_ms": [5 * 60060.0],
            }
        ),
        {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY},
    )
    rows = [
        {"originId": "fastm-demo-p-a", "avails": "3", "filled_s": "180.18"},
        {"originId": "fastm-demo-p-b", "avails": "4", "filled_s": "240.24"},
    ]
    windows: list[tuple[float, float]] = []

    def insights(group: str, query: str, start: float, end: float) -> list[dict[str, str]]:
        windows.append((start, end))
        return rows

    monkeypatch.setattr(reconcile.smoke, "insights", insights)
    added: list[Any] = []
    monkeypatch.setattr(reconcile.metrics, "add_metric", lambda **kw: added.append(kw))
    result = reconcile.handler({"scheduled_time": "2026-10-29T21:20:00Z"}, context)
    assert result["unlogged_filled_s"] == 60.06
    assert result["siblings"]["fastm-demo-p-b"]["unlogged_filled_s"] == 60.06
    assert result["siblings"]["fastm-demo-p-a"] == {
        "counted": {"avails": 4, "filled_ms": 180180},
        "logged": {"avails": 3, "filled_ms": 180180},
        "unlogged_filled_s": 0.0,
    }, "an unfilled avail is a sample and no FILLED_AVAIL, and no shortfall"
    assert [(m["name"], m["value"]) for m in added] == [("UnloggedFilledSeconds", 60.06)]
    start, end = reconcile.window(datetime(2026, 10, 29, 21, 20, tzinfo=UTC))
    assert windows == [((start - reconcile.EDGE).timestamp(), (end + reconcile.EDGE).timestamp())]


def test_the_ad_server_route_and_prefetch_reconcile_to_the_millisecond(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fastcih-demo-p-a, 13:40-14:40Z 2026-10-30 (ad-server route, prefetch): 1,002 MAKING_ADS_REQUEST (retrievals and
    VAST Wrapper hops) against 400 AdDecisionServer.Ads samples, but 46,726,680 ms of Avail.FilledDuration against
    46,726.68 s in 389 FILLED_AVAIL (11 avails unfilled). Requests do not reconcile; filled time does."""
    log_group(stub, datetime(2026, 10, 29, 12, tzinfo=UTC))
    stub("cloudwatch").add_response(
        "get_metric_data", metric_data({"m0_avails": [400.0], "m0_filled_ms": [46_726_680.0]}), None
    )
    rows = [{"originId": "fastm-demo-p-a", "avails": "389", "filled_s": "46726.68"}]
    monkeypatch.setattr(reconcile.smoke, "insights", lambda *args: rows)
    result = reconcile.reconcile(SIBLINGS[:1], AT)
    assert result["unlogged_filled_s"] == 0.0


def test_a_sibling_with_no_traffic_has_nothing_missing(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    log_group(stub, datetime(2026, 10, 29, 12, tzinfo=UTC))
    stub("cloudwatch").add_response("get_metric_data", metric_data({"m0_avails": [], "m0_filled_ms": []}), None)
    monkeypatch.setattr(reconcile.smoke, "insights", lambda *args: [])
    result = reconcile.reconcile(SIBLINGS[:1], AT)
    assert result["unlogged_filled_s"] == 0.0
    assert result["siblings"]["fastm-demo-p-a"]["logged"] == {"avails": 0, "filled_ms": 0}


def test_an_hour_before_the_siblings_log_group_existed_is_skipped(
    stub: Stub, monkeypatch: pytest.MonkeyPatch, context: Any
) -> None:
    """fastcih, 2026-10-30: the stack was a minute old, so the hour ended before its log group existed, and Logs
    Insights refused the query (MalformedQueryException). No metric is put for an hour not measured."""
    monkeypatch.setenv("RELEASE_SIBLINGS", ",".join(SIBLINGS))
    reconcile.settings.cache_clear()
    monkeypatch.setattr(reconcile.smoke, "insights", lambda *args: (_ for _ in ()).throw(AssertionError("queried")))
    added: list[Any] = []
    monkeypatch.setattr(reconcile.metrics, "add_metric", lambda **kw: added.append(kw))
    log_group(stub, datetime(2026, 10, 29, 20, 58, tzinfo=UTC))  # created inside the hour 20:00-21:00Z
    result = reconcile.handler({"scheduled_time": "2026-10-29T21:20:00Z"}, context)
    assert result["unlogged_filled_s"] == 0.0 and "created" in result["skipped"]
    log_group(stub, None)
    assert "no siblings' log group" in reconcile.reconcile(SIBLINGS, AT)["skipped"]
    assert added == []
