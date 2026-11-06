"""The identity module's edge rebuild (``constructs/identity/edge_rebuild.js``) on crafted viewer-request events:
P0-05's 13 ``TestFunction`` cases and the error path, run in Node.js (the e2e runs the same cases through CloudFront's
``TestFunction`` on the deployed function, ``tests/e2e/test_17_identity.py``)."""

import json
import shutil
import subprocess
from typing import Any, Final

import pytest

from fast_channels.infra.constructs.identity.edge import CODE

HASH: Final = "1bab6b4238868b752a9e9daae3e2a4911f2a2fc9"
PC: Final = "fastm-demo-p-a"
INIT: Final = f"/v1/master/{HASH}/{PC}/hls.m3u8"
T1, T2 = "A" * 11 + "b" * 11, "C" * 11 + "d" * 11
NODE: Final = shutil.which("node")
RUN: Final = """
const vm = require('vm');
const events = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const context = {};
vm.createContext(context);
vm.runInContext(__CODE__, context);
process.stdout.write(JSON.stringify(events.map((event) => {
  try { return {result: context.handler(event)}; } catch (error) { return {thrown: String(error)}; }
})));
"""


def event(uri: str, query: dict[str, Any], method: str = "GET") -> dict[str, Any]:
    """A cloudfront-js-2.0 viewer-request event."""
    return {
        "version": "1.0",
        "context": {"eventType": "viewer-request"},
        "viewer": {"ip": "198.51.100.10"},
        "request": {"method": method, "uri": uri, "querystring": query, "headers": {}, "cookies": {}},
    }


def run(*events: dict[str, Any], code: str | None = None) -> list[dict[str, Any]]:
    """The function's answer to each event (``code``: a variant of its source)."""
    if NODE is None:
        pytest.skip("Node.js is not installed")
    script = RUN.replace("__CODE__", json.dumps(CODE.read_text() if code is None else code))
    answer = subprocess.run([NODE, "-e", script], input=json.dumps(events), capture_output=True, text=True, check=True)
    results: list[dict[str, Any]] = json.loads(answer.stdout)
    return results


def query_of(result: dict[str, Any]) -> dict[str, Any]:
    assert "thrown" not in result, result
    return dict(result["result"]["querystring"])


def test_a_valid_ticket_is_all_that_is_left_of_the_query() -> None:
    junk = {
        "ads.t": {"value": T1},
        "ads.x": {"value": "junk"},
        "aws.logMode": {"value": "DEBUG"},
        "aws.availSuppressionMode": {"value": "OFF"},
        "manifest.foo": {"value": "bar"},
    }
    [result] = run(event(INIT, junk))
    assert query_of(result) == {"ads.t": {"value": T1}}
    assert result["result"]["uri"] == INIT


@pytest.mark.parametrize(
    "ticket",
    [
        {"value": T1, "multiValue": [{"value": T1}, {"value": T2}]},  # two tickets
        {"value": T1[:21]},
        {"value": T1 + "x"},
        {"value": T1[:20] + "%2D"},  # values are not decoded: never a ticket
    ],
    ids=["duplicate", "21 characters", "23 characters", "percent-encoded"],
)
def test_anything_but_one_well_formed_ticket_leaves_an_empty_query(ticket: dict[str, Any]) -> None:
    [result] = run(event(INIT, {"ads.t": ticket, "aws.logMode": {"value": "DEBUG"}}))
    assert query_of(result) == {}, "an anonymous session"


def test_a_ticket_with_doubled_characters_is_kept() -> None:
    """MediaTailor passes ``--`` and ``__`` in ``ads.*`` values unchanged (P0-05)."""
    ticket = T1[:9] + "--" + T1[11:]
    [result] = run(event(INIT, {"ads.t": {"value": ticket}}))
    assert query_of(result) == {"ads.t": {"value": ticket}}


def test_no_query_and_overrides_only_leave_an_empty_query() -> None:
    overrides = {
        "aws.logMode": {"value": "DEBUG"},
        "aws.availSuppressionMode": {"value": "AFTER_LIVE_EDGE"},
        "aws.availSuppressionValue": {"value": "12:00:00"},
    }
    assert [query_of(r) for r in run(event(INIT, {}), event(INIT, overrides))] == [{}, {}]


@pytest.mark.parametrize(
    "uri",
    [
        f"/v1/master/{HASH}/{PC}/extra/hls.m3u8",
        f"/v1/master/{HASH.upper()}/{PC}/hls.m3u8",
        f"/v1/master/{HASH[:39]}/{PC}/hls.m3u8",
        f"/v1/master/{HASH}/{PC}/hls.mpd",
        f"/v1/dash/{HASH}/{PC}/index.m3u8",
    ],
    ids=["extra segment", "uppercase hash", "short hash", "m3u8 path with mpd", "dash path with m3u8"],
)
def test_a_non_canonical_init_path_is_a_400(uri: str) -> None:
    [result] = run(event(uri, {"ads.t": {"value": T1}}))
    assert result["result"] == {"statusCode": 400, "statusDescription": "Bad Request"}


def test_a_dash_init_is_rebuilt_too() -> None:
    [result] = run(event(f"/v1/dash/{HASH}/{PC}/index.mpd", {"ads.t": {"value": T1}, "ads.x": {"value": "junk"}}))
    assert query_of(result) == {"ads.t": {"value": T1}}


@pytest.mark.parametrize("uri", [f"/v1/session/{HASH}/{PC}/hls.m3u8", "/v1/session"])
def test_explicit_session_initialization_is_a_403(uri: str) -> None:
    [result] = run(event(uri, {"ads.t": {"value": T1}}))
    assert result["result"] == {"statusCode": 403, "statusDescription": "Forbidden"}


def test_other_mediatailor_paths_keep_their_query() -> None:
    cmcd = {"CMCD": {"value": "ot%3Dm%2Csid%3D%22x%22"}, "aws.sessionId": {"value": "s"}}
    child = f"/v1/manifest/{HASH}/{PC}/00000000-0000-0000-0000-000000000000/0.m3u8"
    [result] = run(event(child, cmcd))
    assert query_of(result) == cmcd


def test_an_error_leaves_the_rebuilt_request_never_a_throw() -> None:
    """A throw is CloudFront's 503 (P0-05): the function catches it and passes the emptied request on."""
    broken = CODE.read_text().replace(
        "if (INIT_PREFIX.test(request.uri)) {", "if (INIT_PREFIX.test(request.uri)) { null.x;"
    )
    assert broken != CODE.read_text()
    [result] = run(event(INIT, {"ads.t": {"value": T1}, "aws.logMode": {"value": "DEBUG"}}), code=broken)
    assert query_of(result) == {}
