"""The smoke test's verdict on a probe session's MediaTailor log events and its decision records, break by break."""

from types import SimpleNamespace

from fast_channels.core.monetization.smoke import request_timeouts, splice_event, timed_out, verdict

MARKERS = ("1793303950308-34-633AB926", "1793304462386-34-466E9F57", "1793304756447-34-1E49FCAA")
EVIDS = {"1664792870", "1181654871", "508165290"}


def logged(marker: str, n: int, *, request: bool = True, fill: bool = True) -> list[dict[str, str]]:
    """A break's events: its marker's first appearance (and what followed), then the marker found again with its
    duration (a new break id, nothing else)."""
    events = [{"eventType": "AD_MARKER_FOUND", "marker": marker, "breakId": f"b{n}"}]
    events += [{"eventType": "MAKING_ADS_REQUEST", "breakId": f"b{n}"}] if request else []
    events += [{"eventType": "FILLED_AVAIL", "breakId": f"b{n}"}] if fill else []
    return [
        *events,
        {"eventType": "AD_MARKER_FOUND", "marker": marker, "breakId": f"b{n}-done", "completed": ",DURATION="},
    ]


def session(*breaks: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{"eventType": "PRE_SESSION_INIT_HOOK_SUMMARY", "hookStatus": "SUCCESS"}, *(e for b in breaks for e in b)]


def test_the_splice_event_of_a_channel_assembly_marker() -> None:
    assert [splice_event(m) for m in MARKERS] == ["1664792870", "1181654871", "508165290"]
    assert splice_event("some-other-origin") is None


def test_a_healthy_session_passes() -> None:
    events = session(*(logged(m, n) for n, m in enumerate(MARKERS[:2])))
    result = verdict(sibling="ch-p-b", session="s", events=events, decisions=EVIDS, breaks=2, seen=MARKERS[:2])
    assert result.passed and (result.breaks, result.requests, result.filled, result.decisions) == (2, 2, 2, 2)


def test_markers_without_requests_and_unfilled_breaks_fail() -> None:
    events = session(logged(MARKERS[0], 0, request=False, fill=False), logged(MARKERS[1], 1, fill=False))
    result = verdict(sibling="ch-p-b", session="s", events=events, decisions=EVIDS, breaks=2)
    assert result.failures == ("1 breaks made no ad request", "1 breaks were not filled")


def test_a_break_left_out_of_the_logs_passes_on_its_decision_record() -> None:
    """fastm 2026-10-29: the break's first appearance, its request and its fill were not logged; only the marker found
    again with its duration was, and the PDS recorded the request."""
    events = session(logged(MARKERS[0], 0), logged(MARKERS[2], 2))
    events.append({"eventType": "AD_MARKER_FOUND", "marker": MARKERS[1], "breakId": "x", "completed": ",DURATION="})
    result = verdict(sibling="ch-p-b", session="s", events=events, decisions=EVIDS, breaks=3, seen=MARKERS)
    assert result.passed and (result.breaks, result.requests, result.filled, result.unlogged) == (3, 3, 2, 1)
    undecided = verdict(
        sibling="ch-p-b", session="s", events=events, decisions=EVIDS - {"1181654871"}, breaks=3, seen=MARKERS
    )
    assert undecided.failures == ("1 breaks made no ad request", "1 breaks without a decision record")


def test_a_session_of_unlogged_breaks_fails() -> None:
    result = verdict(sibling="ch-p-b", session="s", events=session(), decisions=EVIDS, breaks=2, seen=MARKERS[:2])
    assert result.failures == ("no break was logged",)


def test_too_few_breaks_function_errors_and_missing_decisions_fail() -> None:
    bad = session(logged(MARKERS[0], 0))
    bad += [{"eventType": "PRE_ADS_REQUEST_FUNCTION_ERROR"}, {"eventType": "PRE_ADS_REQUEST_HOOK_ERROR"}]
    bad.append({"eventType": "PRE_ADS_REQUEST_HOOK_SUMMARY", "hookStatus": "TIMEOUT"})
    result = verdict(sibling="ch-p-b", session="s", events=bad, decisions=set(), breaks=2)
    assert result.failures == ("saw 1 of 2 breaks", "3 Function errors", "1 breaks without a decision record")
    assert not result.passed
    events = session(logged(MARKERS[0], 0), logged(MARKERS[1], 1))
    probe = verdict(sibling="ch-p-z", session="s", events=events, decisions=set(), breaks=2, decisions_required=False)
    assert probe.passed


def test_markers_of_another_origin_count_decisions_by_number() -> None:
    events = session(logged("a", 0), logged("b", 1))
    assert verdict(sibling="x", session="s", events=events, decisions={"1", "2"}, breaks=2).passed
    assert not verdict(sibling="x", session="s", events=events, decisions={"1"}, breaks=2).passed


def test_the_request_timeouts_of_a_siblings_pre_ads_tree(kit: SimpleNamespace) -> None:
    bundle = kit.make_bundle(contributions=[kit.crawl()])
    assert request_timeouts(bundle, "fastm-demo-p-b") == {"fastm_pa_meta": 250}
    assert request_timeouts(bundle, "fastm-demo-p-z") == {}  # the probe maps no PRE_ADS tree
    assert request_timeouts(bundle, "fastm-other-p-a") == {}


def test_a_request_at_its_timeout_in_two_minutes_has_timed_out() -> None:
    timeouts = {"fastm_pa_lookup_r0004": 1000, "fastm_pa_meta_r0004": 250}
    # fastm 2026-10-30: the lookup to a 2,500 ms endpoint at 1,007 and 1,008 ms; the crawl once at 266 ms (cold)
    maxima = {"fastm_pa_lookup_r0004": [1007.0, 1008.0, 40.0], "fastm_pa_meta_r0004": [266.0, 77.0, 28.0]}
    assert timed_out(maxima, timeouts) == {"fastm_pa_lookup_r0004": 2}
    assert timed_out({}, timeouts) == {}
