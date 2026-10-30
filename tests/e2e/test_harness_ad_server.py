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
    events: list[dict[str, Any]] = [
        {
            "eventType": "MAKING_ADS_REQUEST",
            "adBreakCorrelationId": "c1",
            "sessionId": "s",
            "adsRequestUrl": "https://mock.example/ads?pc=p&sid=s&evid=7&hb_pb=35.00",
        },
        {
            "eventType": "VAST_REDIRECT",
            "adBreakCorrelationId": "c1",
            "sessionId": "s",
            "vastAd": {"vastAdTagUri": f"https://d2.cloudfront.net/cache?uuid={UUID}"},
        },
        {
            "eventType": "FILLED_AVAIL",
            "adBreakCorrelationId": "c1",
            "sessionId": "s",
            "avail": {
                "filledDuration": 60.06,
                "creativeAds": [{"uri": "https://cdn.example/sim1/m.m3u8"}, {"uri": "https://cdn.example/sim2/m.m3u8"}],
                "skippedAds": [{"skippedReason": "AVAIL_DURATION_EXCEEDED"}],
            },
        },
        {
            "eventType": "VAST_REDIRECT",
            "adBreakCorrelationId": "c2",
            "sessionId": "s",
            "vastAd": {"vastAdTagUri": "https://cdn.example/ads/standard/vast.xml"},
        },
        {"eventType": "AD_MARKER_FOUND", "sessionId": "s"},  # no correlation id: not a request
    ]
    found = breaks(events)
    assert set(found) == {"c1", "c2"}
    first = found["c1"]
    assert (first.sid, first.evid, first.uuid, first.filled_seconds) == ("s", "7", UUID, 60.06)
    assert first.creatives == ("https://cdn.example/sim1/m.m3u8", "https://cdn.example/sim2/m.m3u8")
    assert first.skipped == ("AVAIL_DURATION_EXCEEDED",)
    assert found["c2"].uuid == ""  # the house line's Wrapper


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
