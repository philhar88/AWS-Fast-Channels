"""The ad-server leg's log readers (``harness_ad_server``), offline."""

import json
from typing import Any

from harness_ad_server import Auction, auctions, breaks, decisions, json_line, per_break, pod_media

UUID = "0b9a7c3e-1111-4222-8333-444455556666"


def test_prebid_servers_unescaped_message_is_read() -> None:
    line = {"evt": "fast-ctv-pod", "sid": "s", "evid": "7", "cached": True, "hb_pb": "35.00", "uuid8": "0b9a7c3e"}
    layout = '{"timestamp":"x","level":"INFO","message":"' + json.dumps(line) + '"}'
    assert json_line(layout) == line
    assert json_line(json.dumps(line)) == line
    assert json_line("not json") is None


def test_auctions_are_the_pod_cache_lines() -> None:
    lines = [
        json.dumps(
            {
                "evt": "fast-ctv-pod",
                "sid": "s",
                "evid": "7",
                "cached": True,
                "hb_pb": "35.00",
                "uuid8": "0b9a7c3e",
                "winners": ["amt:sim1"],
            }
        ),
        json.dumps({"evt": "fast-ctv-pod", "sid": "s", "evid": "7", "cached": False}),
        json.dumps({"evt": "fast-ctv-pod", "sid": "s", "evid": "8"}),  # the direct route: no "cached"
        json.dumps({"evt": "fast-ctv-request"}),
    ]
    found = auctions(lines)
    assert found[0] == Auction("s", "7", True, "35.00", "0b9a7c3e", ("amt:sim1",))
    assert per_break(found) == {("s", "7"): 2}


def test_decisions_are_the_mock_records() -> None:
    record = {
        "level": "INFO",
        "message": "ad decision",
        "sid": "s",
        "evid": "7",
        "line": "prebid-35.00",
        "uuid8": "0b9a7c3e",
    }
    found = decisions([json.dumps(record), json.dumps({"message": "other"})])
    assert [(d.sid, d.evid, d.line, d.uuid8) for d in found] == [("s", "7", "prebid-35.00", "0b9a7c3e")]


def test_breaks_join_the_request_the_wrapper_and_the_fill() -> None:
    """Per session and correlation id (every session of a break shares it); the Wrapper's own request has no
    ``evid``, and a marker alone is not a break."""

    def event(kind: str, sid: str, correlation: str, **fields: Any) -> dict[str, Any]:
        return {"eventType": kind, "adBreakCorrelationId": correlation, "sessionId": sid, **fields}

    cache = f"https://d2.cloudfront.net/cache?uuid={UUID}"
    events: list[dict[str, Any]] = [
        event("MAKING_ADS_REQUEST", "s", "c1", adsRequestUrl="https://mock.example/ads?pc=p&sid=s&evid=7&hb_pb=35.00"),
        event("VAST_REDIRECT", "s", "c1", vastAd={"vastAdTagUri": cache}),
        event("MAKING_ADS_REQUEST", "s", "c1", adsRequestUrl=cache),
        event(
            "FILLED_AVAIL",
            "s",
            "c1",
            avail={
                "filledDuration": 60.06,
                "creativeAds": [{"uri": "https://cdn.example/sim1/m.m3u8"}, {"uri": "https://cdn.example/sim2/m.m3u8"}],
                "skippedAds": [{"skippedReason": "AVAIL_DURATION_EXCEEDED"}],
            },
        ),
        event("MAKING_ADS_REQUEST", "t", "c1", adsRequestUrl="https://mock.example/ads?pc=p&sid=t&evid=7&hb_pb="),
        event("VAST_REDIRECT", "t", "c1", vastAd={"vastAdTagUri": "https://cdn.example/ads/standard/vast.xml"}),
        event("AD_MARKER_FOUND", "s", "m1"),  # a marker's own correlation id: not a request
        {"eventType": "AD_MARKER_FOUND", "sessionId": "s"},
    ]
    found = breaks(events)
    assert set(found) == {("s", "c1"), ("t", "c1")}
    first = found[("s", "c1")]
    assert (first.sid, first.evid, first.uuid, first.filled_seconds) == ("s", "7", UUID, 60.06)
    assert first.creatives == ("https://cdn.example/sim1/m.m3u8", "https://cdn.example/sim2/m.m3u8")
    assert first.skipped == ("AVAIL_DURATION_EXCEEDED",)
    other = found[("t", "c1")]
    assert (other.evid, other.uuid) == ("7", "")  # the house line's Wrapper


def test_a_pods_media_files() -> None:
    media = "<MediaFiles><MediaFile><![CDATA[ https://cdn.example/{}/m.m3u8 ]]></MediaFile></MediaFiles>"
    vast = (
        '<VAST version="4.2"><Ad id="a" sequence="1"><InLine><Creatives><Creative><Linear>'
        + media.format("sim1")
        + "</Linear></Creative></Creatives></InLine></Ad><Ad id='b'><InLine><Creatives><Creative><Linear>"
        + media.format("house1")
        + "</Linear></Creative></Creatives></InLine></Ad></VAST>"
    )
    assert pod_media(vast) == ["https://cdn.example/sim1/m.m3u8", "https://cdn.example/house1/m.m3u8"]
