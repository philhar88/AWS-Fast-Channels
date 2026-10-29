"""Probe sessions against a candidate sibling: the smoke test (two breaks) and the synthetic soak (three more) of
DESIGN §8.3, steps 3 and 4.

One session is opened on the sibling (an implicit HLS session through MediaTailor) and its media playlist polled
like a player's until it has seen ``breaks`` new ad markers or the invocation's time runs out; the state machine then
calls ``watch`` again with the same session (a session left unpolled for a few segments would expire). The markers
already in the session's first playlist are left out: a viewer who joins during a break gets no ads for it (avail
suppression ``BEHIND_LIVE_EDGE``). When the session has seen its breaks, ``judge`` reads the session's MediaTailor
log events from the module's log group (Logs Insights, after the vended logs' delivery delay) and returns
:class:`~fast_channels.core.monetization.smoke.SmokeVerdict`.
"""

import base64
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urljoin, urlsplit

import httpx
from aws_lambda_powertools import Logger

from fast_channels.core.manifests import attributes, parse_multivariant
from fast_channels.core.monetization import names
from fast_channels.core.monetization.smoke import verdict
from fast_channels.functions import aws
from fast_channels.functions.release import promote
from fast_channels.functions.settings import settings
from fast_channels.functions.tools.registry import CORRELATION_FIELD, SEPARATOR

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
logging.getLogger("httpx").setLevel(logging.WARNING)  # not a line per playlist poll
POLL_SECONDS: Final = 4.0
"""Playlist polls, about one per segment like a player."""
WATCH_SECONDS: Final = 780
"""One invocation's share of a session (its function times out at 15 minutes)."""
QUERY_SECONDS: Final = 240
_SESSION: Final = re.compile(r"/v1/manifest/[^/]+/[^/]+/(?P<session>[^/]+)/")
QUERY: Final = (
    "fields eventType, adBreakCorrelationId as breakId, status as hookStatus\n"
    "| filter sessionId = '{session}'\n"
    '| parse adMarkers /ID="(?<marker>[^"]+)"/\n'
    "| parse adMarkers /(?<completed>,DURATION=)/\n"
    "| limit 10000"
)
"""The session's events: a marker's ``EXT-X-DATERANGE`` id (a VOD ``EXT-X-CUE-OUT`` has none: its break id) and
whether it carries its duration (found again at the break's end), and the hook summaries' ``status``."""
DECISIONS: Final = "filter record = 'pds.policy' and sid = '{session}'\n| fields evid\n| limit 1000"
"""The breaks (``evid``) of the session's ``/policy`` decision records (``core.pds.records.PolicyRecord``)."""


LAB_LEAD: Final = timedelta(minutes=3)
"""From planning the lab's block to its first program: publishing and starting the channel fit in it."""
LAB_BREAKS: Final = {"cadence_minutes": 1, "min_gap_minutes": 0.5, "slate_gops": 30}
"""A break a minute on the lab channel, so a smoke and a soak take minutes."""
LAB_PICKS: Final = 2


class LabError(RuntimeError):
    """An agent tool the lab channel's smoke test uses failed."""


def tool(name: str, correlation_id: str, **arguments: Any) -> dict[str, Any]:
    """An agent tool, invoked as AgentCore Gateway invokes it (the target's function, the tool in the client
    context)."""
    config = settings()
    config.require("tool_functions")
    function = json.loads(config.tool_functions)[name.partition(SEPARATOR)[0]]
    context = base64.b64encode(json.dumps({"custom": {"bedrockAgentCoreToolName": name}}).encode()).decode()
    response = aws.client("lambda").invoke(
        FunctionName=function,
        Payload=json.dumps({**arguments, CORRELATION_FIELD: correlation_id}).encode(),
        ClientContext=context,
    )
    body: dict[str, Any] = json.loads(response["Payload"].read())
    if "FunctionError" in response or body.get("ok") is not True:
        raise LabError(f"{name}: {body.get('error', body)}")
    return body


def lab_start(lab: str, correlation_id: str) -> dict[str, Any]:
    """Schedule a block with a break a minute on the lab channel and start it."""
    channel = tool("channels___get_channel", correlation_id, channel=lab)
    assets = tool(
        "library___find_assets",
        correlation_id,
        kind="content",
        status="READY",
        ladder_version=channel["ladder_version"],
        limit=LAB_PICKS,
    )["assets"]
    if not assets:
        raise LabError(f"{lab}: no READY content of ladder {channel['ladder_version']} to smoke-test with")
    start = (datetime.now(UTC) + LAB_LEAD).isoformat(timespec="seconds").replace("+00:00", "Z")
    plan = tool(
        "schedule___plan",
        correlation_id,
        channel=lab,
        picks=[a["asset_id"] for a in assets],
        start=start,
        break_policy=LAB_BREAKS,
    )
    tool("schedule___publish", correlation_id, plan_id=plan["plan_id"])
    tool("channels___start_channel", correlation_id, channel=lab)
    logger.info("lab channel started", extra={"channel": lab, "plan_id": plan["plan_id"], "start": start})
    return {"lab": lab, "plan_id": plan["plan_id"], "start": start}


def lab_stop(lab: str, correlation_id: str) -> dict[str, Any]:
    """Stop the lab channel (its programs are removed on the next start, as missed)."""
    tool("channels___stop_channel", correlation_id, channel=lab)
    logger.info("lab channel stopped", extra={"channel": lab})
    return {"lab": lab, "stopped": True}


def open_session(sibling: str, output: str = "hls") -> dict[str, Any]:
    """An implicit session on ``sibling``: its first variant's media playlist and the session id."""
    prefix = aws.client("mediatailor").get_playback_configuration(Name=sibling)["HlsConfiguration"][
        "ManifestEndpointPrefix"
    ]
    master_url = urljoin(prefix, f"{output}.m3u8")
    with httpx.Client(timeout=10, follow_redirects=True) as client:
        master = client.get(master_url).raise_for_status()
    variants = parse_multivariant(master.text)
    if not variants:
        raise ValueError(f"{sibling}: the multivariant playlist has no variant")
    playlist = urljoin(str(master.url), variants[0].uri)
    match = _SESSION.search(urlsplit(playlist).path)
    if match is None:
        raise ValueError(f"{sibling}: no session id in the media playlist URL")
    with httpx.Client(timeout=10, follow_redirects=True) as client:
        joined = markers(client.get(playlist).raise_for_status().text)
    logger.info("probe session opened", extra={"sibling": sibling, "session": match["session"], "joined": joined})
    return {
        "sibling": sibling,
        "playlist": playlist,
        "session": match["session"],
        "markers": [],
        "joined": joined,
        "started": time.time(),
    }


def markers(text: str) -> list[str]:
    """The ids of the SCTE-35 out markers (``EXT-X-DATERANGE``) in a media playlist."""
    return [
        found["ID"]
        for line in text.splitlines()
        if line.startswith("#EXT-X-DATERANGE:") and "SCTE35-OUT" in (found := attributes(line)) and "ID" in found
    ]


def watch(session: dict[str, Any], breaks: int, seconds: float = WATCH_SECONDS) -> dict[str, Any]:
    """Poll the session's media playlist until it has seen ``breaks`` new markers or ``seconds`` pass."""
    seen = list(session["markers"])
    joined = set(session.get("joined", ()))
    deadline = time.monotonic() + seconds
    with httpx.Client(timeout=10, follow_redirects=True) as client:
        while len(seen) < breaks and time.monotonic() < deadline:
            response = client.get(session["playlist"])
            response.raise_for_status()
            seen += [m for m in markers(response.text) if m not in seen and m not in joined]
            time.sleep(POLL_SECONDS)
    return session | {"markers": seen, "done": len(seen) >= breaks}


def insights(group: str, query: str, start: float, end: float) -> list[dict[str, str]]:
    """The rows of a Logs Insights ``query`` over ``group`` between ``start`` and ``end`` (epoch seconds)."""
    client = aws.client("logs")
    started = client.start_query(
        logGroupNames=[group],
        startTime=max(0, int(start) - 60),
        endTime=int(end) + 300,
        queryString=query,
    )["queryId"]
    deadline = time.monotonic() + QUERY_SECONDS
    while time.monotonic() < deadline:
        result = client.get_query_results(queryId=started)
        if result["status"] == "Complete":
            return [{cell["field"]: cell["value"] for cell in row} for row in result.get("results", [])]
        if result["status"] in ("Failed", "Cancelled", "Timeout", "Unknown"):
            raise RuntimeError(f"Logs Insights query {started} ended {result['status']}")
        time.sleep(2)
    raise TimeoutError(f"Logs Insights query {started} did not complete in {QUERY_SECONDS} s")


def _quoted(session: str) -> str:
    return session.replace("'", "")


def events(session: str, start: float, end: float) -> list[dict[str, str]]:
    """The session's MediaTailor log events in the module's log group."""
    config = settings()
    config.require("prefix")
    return insights(names.log_group(config.prefix), QUERY.format(session=_quoted(session)), start, end)


def decisions(session: str, start: float, end: float) -> set[str]:
    """The breaks (``evid``) the Pod Decision Service recorded a ``/policy`` decision for in the session (``sid``),
    from its function's log group."""
    config = settings()
    config.require("pds_policy_log_group")
    rows = insights(config.pds_policy_log_group, DECISIONS.format(session=_quoted(session)), start, end)
    return {row["evid"] for row in rows if row.get("evid")}


def judge(
    session: dict[str, Any],
    breaks: int,
    *,
    probe: bool,
    bundle_key: str | None = None,
    release: int | None = None,
) -> dict[str, Any]:
    """The verdict on the watched session; with the release, also its PRE_ADS requests' latency against their
    timeouts on the sibling (:func:`~fast_channels.functions.release.promote.timed_out_steps`)."""
    now = time.time()
    found = events(session["session"], session["started"], now)
    joined = set(session.get("joined", ()))
    result = verdict(
        sibling=session["sibling"],
        session=session["session"],
        events=[e for e in found if e.get("marker") not in joined],
        decisions=decisions(session["session"], session["started"], now),
        breaks=breaks,
        seen=[m for m in session.get("markers", ()) if m not in joined],
        decisions_required=not probe,
    )
    if bundle_key and release:
        start, end = datetime.fromtimestamp(session["started"], UTC), datetime.fromtimestamp(now, UTC)
        late = promote.timed_out_steps(bundle_key, release, session["sibling"], start, end)
        failures = tuple(
            f"PRE_ADS request {f} reached its {t['timeout_ms']} ms timeout in {t['minutes']} minutes"
            for f, t in late.items()
        )
        result = result.model_copy(update={"failures": (*result.failures, *failures)})
    logger.info("smoke verdict", extra=result.model_dump())
    return result.model_dump() | {"passed": result.passed}


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """One smoke step (``event["step"]``): ``open`` a session, ``watch`` it, ``judge`` it."""
    match event["step"]:
        case "open":
            return open_session(event["sibling"], event.get("output", "hls"))
        case "watch":
            return watch(event["session"], event["breaks"])
        case "judge":
            return judge(
                event["session"],
                event["breaks"],
                probe=event.get("probe", False),
                bundle_key=event.get("bundle_key"),
                release=event.get("release"),
            )
        case "lab-start":
            return lab_start(event["lab"], event["change_id"])
        case "lab-stop":
            return lab_stop(event["lab"], event["change_id"])
    raise ValueError(f"unknown step {event['step']!r}")
