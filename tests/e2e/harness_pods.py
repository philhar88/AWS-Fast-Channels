"""Building blocks of the MediaTailor-driven demand smoke (``test_14_demand_pods.py``, BUILD-PLAN M8-08): viewers on a
monetized channel's stable sibling, and the join of what MediaTailor, Prebid Server's fast-ctv module, the Pod Decision
Service and the bidder simulator logged for each of their breaks.

The readers and checks are pure functions over log events, unit-tested in ``test_harness_pods.py``. :class:`Viewer`,
:class:`Logs` and the readiness probes do the I/O.
"""

import json
import re
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Any, Final
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from _demand import PREBID_CONTAINER
from harness import Player, TransientResponseError
from playback import Segment, Splice, media_segments, splices

from fast_channels.core.demo_demand import DemoIndex
from fast_channels.core.manifests import parse_multivariant
from fast_channels.core.pds.book import Creative, house_creatives
from fast_channels.core.pds.records import POLICY_RECORD, SELECT_RECORD

type IpAddress = IPv4Address | IPv6Address

SMART_TV_USER_AGENT: Final = "Mozilla/5.0 (SMART-TV; Linux) fast-channels-e2e/3.0"
"""A Connected TV to the PDS's device-type rule (``core.pds.devicetype``)."""
EGRESS_URL: Final = "https://checkip.amazonaws.com"
"""Answers the caller's public IPv4 address as text."""
MASK_BITS: Final = {4: 24, 6: 56}
"""What Prebid Server keeps of the viewer's address for a bidder denied ``transmitPreciseGeo`` (every bidder but the
own book): an IPv4 address's /24 (``IpAddressHelper.maskIpv4``, ``x.y.z.0``), an IPv6 address's first 56 bits
(``ipv6.anon-left-mask-bits``, 56 by default in 3.43.0 and 4.4.0; the stage's configuration leaves it alone)."""
POLL_SECONDS: Final = 3.5
_SESSION_ID: Final = re.compile(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")
_MODULE_MESSAGE: Final = re.compile(r'"message":\s*"(\{.*\})"')
ADS_EVENTS: Final = frozenset({"MAKING_ADS_REQUEST", "VAST_RESPONSE", "FILLED_AVAIL"})
"""The ADS log events of a break answered and stitched."""
FIRST_SIGHT: Final = frozenset({"NEW_CREATIVE", "IMPORT_IN_PROGRESS"})
"""MediaTailor's skip reasons for a creative it has not imported yet: ``NEW_CREATIVE`` the first time it sees it (it
starts the import), ``IMPORT_IN_PROGRESS`` until the import is done."""
LOGGED: Final = "@logged_ms"
"""The key under which the readers keep a record's or a module line's CloudWatch event time (ms since the epoch): it
places the record in the auction of one of MediaTailor's requests (:meth:`Break.auctions`)."""
CLOCK_SLACK: Final = timedelta(seconds=1)
"""How much earlier than MediaTailor's ``MAKING_ADS_REQUEST`` an auction's records may be logged."""


# --- the viewer's address ------------------------------------------------------------------------------------------


def masked(address: IpAddress) -> IpAddress:
    """``address`` as Prebid Server sends it to a bidder denied precise geolocation (:data:`MASK_BITS`)."""
    return ip_network(f"{address}/{MASK_BITS[address.version]}", strict=False).network_address


def ipv4_client(**options: Any) -> httpx.Client:
    """An HTTP client whose connections leave from IPv4, so the viewer's address is the one :data:`EGRESS_URL`
    reports."""
    return httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0"), **options)


def egress_address(client: httpx.Client) -> IpAddress:
    """The public address ``client``'s requests come from."""
    response = client.get(EGRESS_URL)
    response.raise_for_status()
    return ip_address(response.text.strip())


# --- viewers -------------------------------------------------------------------------------------------------------


def session_id(url: str) -> str | None:
    """MediaTailor's session id in a session's playlist URL (``…/<configuration>/<session id>/0.m3u8``)."""
    found = _SESSION_ID.search(urlsplit(url).path)
    return found.group(1) if found else None


@dataclass
class Viewer:
    """One SSAI session, played as a player plays it: its lowest-bandwidth variant read every few seconds, and every
    stitched ad segment requested once without following the redirect. MediaTailor fires an ad's server-side beacons
    (our impression tracker among them) when its segments are requested."""

    player: Player
    variant: str
    sid: str
    segments: dict[str, Segment] = field(default_factory=dict)
    splices: dict[str, Splice] = field(default_factory=dict)
    requested: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def join(cls, player: Player, url: str) -> Viewer:
        """A new session on the playback URL ``url``."""
        response = player.get(url)
        variants = parse_multivariant(response.text)
        if not variants:
            raise AssertionError(f"{url} lists no variants: {response.text[:300]}")
        variant = urljoin(str(response.url), min(variants, key=lambda v: v.bandwidth).uri)
        if (sid := session_id(variant)) is None:
            raise AssertionError(f"no MediaTailor session id in {variant}")
        return cls(player, variant, sid)

    def poll(self) -> None:
        """Read the variant once; request the ad segments it shows for the first time."""
        try:
            text = self.player.get(self.variant).text
        except (AssertionError, httpx.HTTPError, TransientResponseError) as error:
            self.errors.append(f"{self.sid}: {error}")
            return
        for segment in media_segments(text, self.variant):
            self.segments.setdefault(segment.uri, segment)
            if segment.is_ad and segment.uri not in self.requested:
                self._request(segment.uri)
        for splice in splices(text, self.variant):
            if splice.id not in self.splices or not self.splices[splice.id].complete:
                self.splices[splice.id] = splice

    def _request(self, uri: str) -> None:
        try:
            response = self.player.client.get(uri, follow_redirects=False)
        except httpx.HTTPError as error:
            self.errors.append(f"{self.sid}: GET {uri}: {error}")
            return  # asked again on the next read
        if response.status_code >= 400:
            self.errors.append(f"{self.sid}: GET {uri} -> {response.status_code}")
        if response.status_code < 500:
            self.requested.add(uri)

    def break_seconds(self, starts: datetime, seconds: float) -> tuple[float, float]:
        """Seconds of ad segments and of other segments (the break slate) that start in the ``seconds`` from
        ``starts``."""
        ends = starts + timedelta(seconds=seconds)
        inside = [s for s in self.segments.values() if s.starts is not None and starts <= s.starts < ends]
        return sum(s.duration for s in inside if s.is_ad), sum(s.duration for s in inside if not s.is_ad)


def watch(viewers: Sequence[Viewer], *, ends: datetime) -> None:
    """Poll every viewer until ``ends``."""
    while datetime.now(UTC) < ends:
        started = time.monotonic()
        for viewer in viewers:
            viewer.poll()
        time.sleep(max(POLL_SECONDS - (time.monotonic() - started), 0.0))


# --- logs ----------------------------------------------------------------------------------------------------------


@dataclass
class Logs:
    """CloudWatch Logs reads of one run's window."""

    client: Any
    start_ms: int
    end_ms: int | None = None

    def events(self, group: str, pattern: str) -> list[dict[str, Any]]:
        """Every event of ``group`` matching ``pattern`` in the window, oldest first ([] when the group does not
        exist)."""
        window: dict[str, Any] = {"startTime": self.start_ms} | ({"endTime": self.end_ms} if self.end_ms else {})
        try:
            pages = self.client.get_paginator("filter_log_events").paginate(
                logGroupName=group, filterPattern=pattern, **window
            )
            found = [event for page in pages for event in page["events"]]
        except self.client.exceptions.ResourceNotFoundException:
            return []
        return sorted(found, key=lambda event: (event["timestamp"], event.get("eventId", "")))


def any_of(field_: str, values: Iterable[str]) -> str:
    """A JSON filter-pattern clause: ``field_`` is one of ``values``."""
    return "(" + " || ".join(f'$.{field_} = "{value}"' for value in sorted(values)) + ")"


def record_pattern(record: str, sids: Iterable[str]) -> str:
    """The filter pattern of the PDS's ``record`` lines of sessions ``sids``."""
    return f'{{ ($.record = "{record}") && {any_of("sid", sids)} }}'


def json_object(message: str) -> dict[str, Any] | None:
    """The JSON object a log line carries: the whole line (vended logs, Powertools), or what follows the prefix of a
    Lambda text line (``[INFO]\\t<time>\\t<request id>\\t{...}``)."""
    candidates = [message] + ([message[message.index("{") :]] if "{" in message else [])
    for text in candidates:
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def event_line(message: str) -> dict[str, Any] | None:
    """The JSON line with an ``evt`` that one log event carries: the event itself (a Lambda text line's object
    included), the ``message`` of a JSON-format record (the bidder simulator logs in Lambda's JSON format), or Prebid
    Server's message.

    Prebid Server's JSON layout writes the message unescaped (``"message":"{"evt":"fast-ctv-pod",…}"``), which makes
    the event itself invalid JSON.
    """
    outer = json_object(message)
    if outer is not None and isinstance(outer.get("message"), str):
        inner = json_object(outer["message"])
    elif outer is None and (found := _MODULE_MESSAGE.search(message)):
        inner = json_object(found.group(1))
    else:
        inner = outer
    return inner if inner is not None and isinstance(inner.get("evt"), str) else None


def evid(url: str | None) -> str | None:
    """The ``evid`` of an ad request URL."""
    values = parse_qs(urlsplit(url or "").query).get("evid")
    return values[0] if values else None


def timestamp(event: Mapping[str, Any]) -> datetime:
    """An ADS log event's ``eventTimestamp``."""
    return datetime.fromisoformat(str(event["eventTimestamp"]))


def logged(record: Mapping[str, Any]) -> datetime | None:
    """When a record or module line was logged (:data:`LOGGED`), None if the reader did not say."""
    value = record.get(LOGGED)
    return datetime.fromtimestamp(value / 1000, UTC) if isinstance(value, int) else None


# --- creatives -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Creatives:
    """The stage's creatives by MediaTailor's names for them: ``creativeUniqueId`` (our immutable creative id) and
    ``adMezzanineUrl`` (the preconditioned ad's playlist)."""

    by_id: Mapping[str, str]
    by_uri: Mapping[str, str]

    @classmethod
    def of(cls, index: DemoIndex, house: Iterable[Creative]) -> Creatives:
        """The demo creatives (``ads/<ladder>/demo/creatives.json``) and the kit's house ads (``house1``, …)."""
        known = [(c.crid, c.creative_id, c.hls_uri) for c in index.creatives]
        known += [(c.crid, c.creative_id, c.hls_uri) for c in house]
        return cls({cid: crid for crid, cid, _ in known}, {uri: crid for crid, _, uri in known})

    @classmethod
    def read(cls, s3: Any, bucket: str, *, index_key: str, vast_key: str) -> Creatives:
        def text(key: str) -> str:
            return str(s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode())

        return cls.of(DemoIndex.model_validate_json(text(index_key)), house_creatives(text(vast_key)))

    def crid(self, ad: Mapping[str, Any]) -> str | None:
        """The crid of an ad of a ``FILLED_AVAIL`` (``creativeAds``, ``skippedAds``)."""
        return self.by_id.get(str(ad.get("creativeUniqueId"))) or self.by_uri.get(str(ad.get("adMezzanineUrl")))


# --- one break of one session --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ad:
    """An ad of a ``FILLED_AVAIL``: stitched (no reason) or skipped."""

    crid: str | None
    sequence: int | None
    reason: str | None = None


def avail_ads(avail: Mapping[str, Any], creatives: Creatives) -> tuple[list[Ad], list[Ad]]:
    """The stitched ads of an avail in order, and its skipped ads."""
    stitched = [Ad(creatives.crid(ad), ad.get("sequence")) for ad in avail.get("creativeAds") or []]
    skipped = [
        Ad(creatives.crid(ad), ad.get("sequence"), str(ad.get("skippedReason"))) for ad in avail.get("skippedAds") or []
    ]
    return stitched, skipped


@dataclass(frozen=True, slots=True)
class Fill:
    """How MediaTailor filled an avail with the module's pod."""

    replaced: tuple[tuple[str, str | None, str], ...] = ()
    """(winner, its skip reason, standalone): a standalone stitched in the position of a winner MediaTailor
    skipped."""
    unfilled: tuple[tuple[str, str | None], ...] = ()
    """(winner, its skip reason): skipped with no standalone in its position."""
    problems: tuple[str, ...] = ()


def reconcile(pod: Sequence[str], standalones: Collection[str], stitched: Sequence[Ad], skipped: Sequence[Ad]) -> Fill:
    """Whether MediaTailor stitched the pod in ``sequence`` order: each winner in its position, or, where it skipped
    the winner, one of the pod's standalones (an ad buffet replaces a failed sequenced ad in its position) or
    nothing; and nothing else."""
    reasons = {ad.crid: ad.reason for ad in skipped if ad.crid is not None}
    replaced: list[tuple[str, str | None, str]] = []
    unfilled: list[tuple[str, str | None]] = []
    problems: list[str] = []
    position = 0
    for sequence, winner in enumerate(pod, start=1):
        ad = stitched[position] if position < len(stitched) else None
        if ad is not None and ad.crid == winner:
            if ad.sequence != sequence:
                problems.append(f"{winner} stitched with sequence {ad.sequence}, not {sequence}")
            position += 1
        elif winner not in reasons:
            problems.append(f"winner {winner} (sequence {sequence}) is neither stitched in its position nor skipped")
        elif (
            ad is not None
            and (standalone := ad.crid) is not None
            and standalone in standalones
            and ad.sequence in {None, sequence}
            and standalone not in {used for *_, used in replaced}
        ):
            replaced.append((winner, reasons[winner], standalone))
            position += 1
        else:
            unfilled.append((winner, reasons[winner]))
    if rest := stitched[position:]:
        problems.append(f"stitched out of the pod's order or beyond it: {[(a.crid, a.sequence) for a in rest]}")
    return Fill(tuple(replaced), tuple(unfilled), tuple(problems))


@dataclass(frozen=True, slots=True)
class Request:
    """One of MediaTailor's ad server requests for a break: a prefetch retrieval ahead of it (its schedule's name), or
    a real-time request at the break (None)."""

    request_id: str
    at: datetime
    prefetch: str | None
    dur_ms: int | None = None
    """The avail's duration the request asked for (its URL's ``dur``): a mid-break joiner's is what was left."""


def dur_ms(url: str | None) -> int | None:
    """The ``dur`` of an ad request URL, in milliseconds."""
    values = parse_qs(urlsplit(url or "").query).get("dur")
    return int(values[0]) if values and values[0].isdigit() else None


@dataclass(frozen=True, slots=True)
class Auction:
    """What one request got: the PDS's records and the module's lines logged from the request until the break's next
    one."""

    request: Request
    policy: list[dict[str, Any]]
    select: list[dict[str, Any]]
    lines: list[dict[str, Any]]


def pod_of(select: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """The crids of a ``/select`` record's pod in sequence order, and of its standalones."""
    crid = {bid["ref"]: str(bid["crid"]) for bid in select["bids"]}
    return [crid[ref] for ref in select["pod"]], [crid[ref] for ref in select["standalone"]]


@dataclass
class Break:
    """One break of one session: MediaTailor's ADS events, and the decision records and module lines of its auctions.

    With prefetch a break can have two ad server requests: the retrieval ahead of the break and, when MediaTailor does
    not use what it retrieved, a real-time request at the break. Each is an auction of its own."""

    sid: str
    evid: str
    events: list[dict[str, Any]] = field(default_factory=list)
    policy: list[dict[str, Any]] = field(default_factory=list)
    select: list[dict[str, Any]] = field(default_factory=list)
    pod_lines: list[dict[str, Any]] = field(default_factory=list)

    @property
    def event_types(self) -> list[str]:
        return [str(event.get("eventType")) for event in self.events]

    @property
    def requests(self) -> list[Request]:
        """MediaTailor's ad server requests for the break, oldest first."""
        return [
            Request(
                str(e.get("requestId")),
                timestamp(e),
                str(e["prefetchScheduleName"]) if e.get("prefetchScheduleName") else None,
                dur_ms(e.get("adsRequestUrl")),
            )
            for e in sorted(self.events, key=timestamp)
            if e.get("eventType") == "MAKING_ADS_REQUEST"
        ]

    @property
    def requested_at(self) -> datetime | None:
        """When MediaTailor first asked for the break's ads (at retrieval, when it prefetched them)."""
        requests = self.requests
        return requests[0].at if requests else None

    def auctions(self) -> list[Auction]:
        """One per request: the records and lines logged from :data:`CLOCK_SLACK` before it until as long before the
        next."""
        requests = self.requests
        found = []
        for n, request in enumerate(requests):
            starts = request.at - CLOCK_SLACK
            ends = requests[n + 1].at - CLOCK_SLACK if n + 1 < len(requests) else None

            def within(record: Mapping[str, Any], starts: datetime = starts, ends: datetime | None = ends) -> bool:
                at = logged(record)
                return at is not None and starts <= at and (ends is None or at < ends)

            found.append(
                Auction(
                    request,
                    [r for r in self.policy if within(r)],
                    [r for r in self.select if within(r)],
                    [line for line in self.pod_lines if within(line)],
                )
            )
        return found

    @property
    def filled(self) -> Mapping[str, Any] | None:
        """The break's ``FILLED_AVAIL`` event."""
        filled = [e for e in self.events if e.get("eventType") == "FILLED_AVAIL"]
        return max(filled, key=timestamp) if filled else None

    @property
    def avail(self) -> Mapping[str, Any] | None:
        """The ``avail`` of the break's ``FILLED_AVAIL``."""
        return (self.filled or {}).get("avail")

    @property
    def filled_from(self) -> str | None:
        """The prefetch schedule whose retrieval filled the avail (``FILLED_AVAIL.prefetchScheduleName``), None for a
        real-time fill."""
        return str(name) if (name := (self.filled or {}).get("prefetchScheduleName")) else None

    @property
    def aired(self) -> Auction | None:
        """The auction whose answer MediaTailor stitched: the retrieval it filled the avail from, else the last
        real-time request's (the last request's when the ``FILLED_AVAIL`` names none)."""
        auctions = self.auctions()
        if (schedule := self.filled_from) is not None:
            return next((a for a in auctions if a.request.prefetch == schedule), None)
        real_time = [a for a in auctions if a.request.prefetch is None] or auctions
        return real_time[-1] if real_time else None

    def placed(self) -> tuple[list[str], list[str]]:
        """The crids of the aired auction's ``/select`` pod in sequence order, and of its standalones."""
        aired = self.aired
        return pod_of(aired.select[-1]) if aired is not None and aired.select else ([], [])

    def rendered(self) -> tuple[list[str], list[str]]:
        """The crids of the pod the module rendered for the aired auction (its line's ``seat:crid`` winners, in sequence
        order) and of its standalones: the ``/select`` pod, or the house ads of a fallback."""
        aired = self.aired
        if aired is None or not aired.lines:
            return [], []
        line = aired.lines[-1]
        return (
            [str(label).split(":", 1)[-1] for label in line.get("winners") or []],
            [str(label).split(":", 1)[-1] for label in line.get("standalones") or []],
        )

    def fill(self, creatives: Creatives) -> Fill:
        """:func:`reconcile` of the break's ``FILLED_AVAIL`` with the pod the module rendered."""
        pod, standalones = self.rendered()
        stitched, skipped = avail_ads(self.avail or {}, creatives)
        return reconcile(pod, standalones, stitched, skipped)


def breaks(
    ads_events: Iterable[Mapping[str, Any]],
    records: Iterable[Mapping[str, Any]],
    pod_lines: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], Break]:
    """Every break by (session id, ``evid``): its ADS events (those that carry the ad request URL, and the others of
    the same ``requestId``: ``VAST_RESPONSE`` carries none), the PDS's policy and select records, and the module's
    ``fast-ctv-pod`` lines."""
    found: dict[tuple[str, str], Break] = {}

    def at(sid: str, event_id: str) -> Break:
        return found.setdefault((sid, event_id), Break(sid, event_id))

    events = [event for event in ads_events if event.get("eventType") != "BEACON_FIRED"]
    requests: dict[str, tuple[str, str]] = {}
    for event in events:
        if (sid := event.get("sessionId")) and (event_id := evid(event.get("adsRequestUrl"))) is not None:
            requests.setdefault(str(event.get("requestId") or id(event)), (str(sid), event_id))
    for event in events:
        if (key := requests.get(str(event.get("requestId") or id(event)))) is not None:
            at(*key).events.append(dict(event))
    for record in records:
        if record.get("record") == POLICY_RECORD:
            at(str(record["sid"]), str(record["evid"])).policy.append(dict(record))
        elif record.get("record") == SELECT_RECORD:
            at(str(record["sid"]), str(record["evid"])).select.append(dict(record))
    for line in pod_lines:
        if line.get("evt") == "fast-ctv-pod" and line.get("sid") and line.get("evid") is not None:
            at(str(line["sid"]), str(line["evid"])).pod_lines.append(dict(line))
    for found_break in found.values():
        found_break.events.sort(key=timestamp)
    return found


# --- readiness -----------------------------------------------------------------------------------------------------


def prebid_tasks(ecs: Any, *, cluster: str, service: str) -> dict[str, str]:
    """The running Prebid Server tasks of the guidance's service: task ARN -> the Prebid container's image."""
    arns = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING")["taskArns"]
    tasks = ecs.describe_tasks(cluster=cluster, tasks=arns)["tasks"] if arns else []
    return {
        task["taskArn"]: next((c["image"] for c in task["containers"] if c["name"] == PREBID_CONTAINER), "")
        for task in tasks
        if task.get("lastStatus") == "RUNNING"
    }


def warmups(codebuild: Any, project: str, tasks: Collection[str]) -> dict[str, str]:
    """The newest warm-up build's status of each of ``tasks`` (task ARN -> ``buildStatus``) that has one."""
    ids = codebuild.list_builds_for_project(projectName=project, sortOrder="DESCENDING")["ids"][:100]
    status: dict[str, str] = {}
    for build in codebuild.batch_get_builds(ids=ids)["builds"] if ids else []:
        variables = {v["name"]: v["value"] for v in build.get("environment", {}).get("environmentVariables", [])}
        task = variables.get("TASK_ARN")
        if task is not None and task in tasks and task not in status:
            status[str(task)] = str(build["buildStatus"])
    return status
