"""Building blocks of the ad-server leg (``test_15_ad_server.py``, M8-09): viewers on an ad-server sibling, and the join
of what MediaTailor, the fast-ctv module and the mock ad server logged for each break.

The readers are pure functions over log events, unit-tested in ``test_harness_ad_server.py``; :class:`Viewer` and
:class:`Logs` do the I/O.
"""

import json
import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from urllib.parse import parse_qs, urljoin, urlsplit
from xml.etree import ElementTree

import httpx
from harness import Player, TransientResponseError
from playback import media_segments

from fast_channels.core.manifests import parse_multivariant

POLL_SECONDS: Final = 3.5
SMART_TV_USER_AGENT: Final = "Mozilla/5.0 (SMART-TV; Linux) fast-channels-e2e/4.0"
UUID4: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SESSION_ID: Final = re.compile(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")
_MODULE_MESSAGE: Final = re.compile(r'"message":\s*"(\{.*\})"')


# --- log lines -----------------------------------------------------------------------------------------------------


def json_line(message: str) -> dict[str, Any] | None:
    """The JSON object one log event carries: the event itself (vended logs, Powertools), the ``message`` of a
    Lambda JSON record, or Prebid Server's message, which its JSON layout writes unescaped."""
    for text in (message, message[message.find("{") :] if "{" in message else ""):
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            inner = value.get("message")
            if isinstance(inner, str) and inner.startswith("{"):
                nested = json_line(inner)
                return nested if nested is not None else value
            return value
    if found := _MODULE_MESSAGE.search(message):
        return json_line(found.group(1))
    return None


@dataclass(frozen=True, slots=True)
class Auction:
    """One ``fast-ctv-pod`` line of the ad-server mode: a break's auction and whether its pod was cached."""

    sid: str
    evid: str
    cached: bool
    hb_pb: str
    uuid8: str
    winners: tuple[str, ...]


def auctions(messages: Iterable[str]) -> list[Auction]:
    """The ad-server mode's auctions in Prebid Server's log (``fast-ctv-pod`` lines that have ``cached``)."""
    found: list[Auction] = []
    for message in messages:
        line = json_line(message)
        if line is None or line.get("evt") != "fast-ctv-pod" or "cached" not in line:
            continue
        found.append(
            Auction(
                sid=str(line.get("sid", "")),
                evid=str(line.get("evid", "")),
                cached=bool(line["cached"]),
                hb_pb=str(line.get("hb_pb", "")),
                uuid8=str(line.get("uuid8", "")),
                winners=tuple(line.get("winners") or ()),
            )
        )
    return found


def per_break(found: Iterable[Auction]) -> Counter[tuple[str, str]]:
    """How many auctions each (session, break) had."""
    return Counter((a.sid, a.evid) for a in found)


@dataclass(frozen=True, slots=True)
class Decision:
    """One decision of the mock ad server."""

    sid: str
    evid: str
    line: str
    uuid8: str


def decisions(messages: Iterable[str]) -> list[Decision]:
    """The mock ad server's ``ad decision`` records."""
    found: list[Decision] = []
    for message in messages:
        line = json_line(message)
        if line is None or line.get("message") != "ad decision":
            continue
        found.append(
            Decision(
                sid=str(line.get("sid", "")),
                evid=str(line.get("evid", "")),
                line=str(line.get("line", "")),
                uuid8=str(line.get("uuid8", "")),
            )
        )
    return found


@dataclass
class Break:
    """What MediaTailor logged for one session's ad request (by ``adBreakCorrelationId``)."""

    sid: str
    evid: str = ""
    redirect: str = ""
    filled_seconds: float = 0.0
    creatives: tuple[str, ...] = ()
    """The media file URL of every stitched ad."""
    skipped: tuple[str, ...] = ()
    events: set[str] = field(default_factory=set)

    @property
    def uuid(self) -> str:
        """The ``uuid`` of the Wrapper's target, when it is Prebid Cache."""
        values = parse_qs(urlsplit(self.redirect).query).get("uuid", [""])
        return values[0] if UUID4.fullmatch(values[0]) else ""


def breaks(events: Iterable[Mapping[str, Any]]) -> dict[str, Break]:
    """MediaTailor's ADS log as breaks: the event id of the request, the Wrapper it followed and what it filled."""
    found: dict[str, Break] = {}
    for event in events:
        correlation = str(event.get("adBreakCorrelationId", ""))
        if not correlation:
            continue
        entry = found.setdefault(correlation, Break(sid=str(event.get("sessionId", ""))))
        kind = str(event.get("eventType", ""))
        entry.events.add(kind)
        match kind:
            case "MAKING_ADS_REQUEST":
                query = parse_qs(urlsplit(str(event.get("adsRequestUrl", ""))).query)
                entry.evid = query.get("evid", [""])[0]
            case "VAST_REDIRECT":
                entry.redirect = str((event.get("vastAd") or {}).get("vastAdTagUri", ""))
            case "FILLED_AVAIL":
                avail = event.get("avail") or {}
                entry.filled_seconds = float(avail.get("filledDuration") or 0)
                entry.creatives = tuple(str(a.get("uri", "")) for a in avail.get("creativeAds") or [])
                entry.skipped = tuple(str(s.get("skippedReason", "")) for s in avail.get("skippedAds") or [])
    return found


def pod_media(vast: str) -> list[str]:
    """The ``MediaFile`` URLs of a cached pod, in document order: what MediaTailor's ``FILLED_AVAIL`` names each
    stitched ad by (``creativeAds[].uri``)."""
    root = ElementTree.fromstring(vast)
    return [(m.text or "").strip() for m in root.iter() if m.tag.rsplit("}", 1)[-1] == "MediaFile"]


# --- I/O ------------------------------------------------------------------------------------------------------------


def session_id(url: str) -> str | None:
    """MediaTailor's session id in a session's playlist URL."""
    found = _SESSION_ID.search(urlsplit(url).path)
    return found.group(1) if found else None


@dataclass
class Viewer:
    """One SSAI session on a playback URL, played like a player: its lowest variant read every few seconds, and each
    stitched ad segment requested once (MediaTailor fires the server-side beacons when its segments are fetched)."""

    player: Player
    variant: str
    sid: str
    requested: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def join(cls, player: Player, url: str) -> Viewer:
        response = player.get(url)
        variants = parse_multivariant(response.text)
        if not variants:
            raise AssertionError(f"{url} lists no variants: {response.text[:300]}")
        variant = urljoin(str(response.url), min(variants, key=lambda v: v.bandwidth).uri)
        if (sid := session_id(variant)) is None:
            raise AssertionError(f"no MediaTailor session id in {variant}")
        return cls(player, variant, sid)

    def poll(self) -> None:
        try:
            text = self.player.get(self.variant).text
        except (AssertionError, httpx.HTTPError, TransientResponseError) as error:
            self.errors.append(f"{self.sid}: {error}")
            return
        for segment in media_segments(text, self.variant):
            if segment.is_ad and segment.uri not in self.requested:
                try:
                    self.player.client.get(segment.uri, follow_redirects=False)
                    self.requested.add(segment.uri)
                except httpx.HTTPError as error:
                    self.errors.append(f"{self.sid}: GET {segment.uri}: {error}")


def watch(viewers: Sequence[Viewer], *, ends: datetime) -> None:
    """Poll every viewer until ``ends``."""
    while datetime.now(ends.tzinfo) < ends:
        for viewer in viewers:
            viewer.poll()
        time.sleep(POLL_SECONDS)


PREBID_CONTAINER: Final = "Prebid-Container"
"""The guidance's Prebid Server container (``scripts/_demand.py``)."""


def running_tasks(ecs: Any, *, cluster: str, service: str) -> list[str]:
    """The running Prebid Server tasks of the guidance's service (task ARNs)."""
    arns = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING")["taskArns"]
    tasks = ecs.describe_tasks(cluster=cluster, tasks=arns)["tasks"] if arns else []
    return [str(task["taskArn"]) for task in tasks if task.get("lastStatus") == "RUNNING"]


def warmups(codebuild: Any, project: str, tasks: Sequence[str]) -> dict[str, str]:
    """The newest warm-up build's status of each of ``tasks`` that has one (task ARN -> ``buildStatus``)."""
    ids = codebuild.list_builds_for_project(projectName=project, sortOrder="DESCENDING")["ids"][:100]
    status: dict[str, str] = {}
    for build in codebuild.batch_get_builds(ids=ids)["builds"] if ids else []:
        variables = {v["name"]: v["value"] for v in build.get("environment", {}).get("environmentVariables", [])}
        task = variables.get("TASK_ARN")
        if task is not None and task in tasks and task not in status:
            status[str(task)] = str(build["buildStatus"])
    return status


def provisioned(lambda_: Any, functions: Iterable[str], alias: str) -> dict[str, str]:
    """The provisioned-concurrency status of each function's ``alias`` (``NONE`` while it has none)."""
    status: dict[str, str] = {}
    for function in functions:
        try:
            config = lambda_.get_provisioned_concurrency_config(FunctionName=function, Qualifier=alias)
        except lambda_.exceptions.ProvisionedConcurrencyConfigNotFoundException:
            status[function] = "NONE"
        else:
            status[function] = str(config["Status"])
    return status


@dataclass
class Logs:
    """CloudWatch Logs reads of one run's window."""

    client: Any
    start_ms: int

    def messages(self, group: str, pattern: str = "") -> list[str]:
        try:
            pages = self.client.get_paginator("filter_log_events").paginate(
                logGroupName=group, startTime=self.start_ms, **({"filterPattern": pattern} if pattern else {})
            )
            return [event["message"] for page in pages for event in page["events"]]
        except self.client.exceptions.ResourceNotFoundException:
            return []

    def records(self, group: str, pattern: str = "") -> list[dict[str, Any]]:
        return [line for message in self.messages(group, pattern) if (line := json_line(message)) is not None]
