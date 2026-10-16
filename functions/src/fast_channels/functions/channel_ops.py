"""Channel Assembly operations shared by the channel and schedule tools, the channel custom resources and the reaper.

MediaTailor shapes everything here:

* CreateProgram and DeleteProgram are limited to 3 TPS per account (channel-assembly.md §5.6): calls are paced by a
  :class:`Pacer`;
* ConflictException ("The channel schedule has changed since your last request") is not retried by botocore, so
  program writes retry it with exponential backoff;
* MediaTailor's NotFoundException is not modelled in boto3 (e2e.md §8 item 4): errors are matched by code;
* Channel Assembly deletes aired programs about 15 minutes after they end, and a stopped channel lists its
  RELATIVE programs at 1970 (channel-assembly.md §5.2, §5.6). The media library's ``PROGRAM`` items are therefore
  the schedule of record, and :func:`drain_channel` combines both sources.
"""

import math
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, Final

from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from fast_channels.functions import aws
from fast_channels.functions.media_library import CHANNEL_PREFIX, MAX_EPOCH_MS, ProgramStatus, program_sk

SCHEDULE_MINUTES: Final = 14 * 24 * 60
"""GetChannelSchedule look-ahead: the API maximum ("The duration-minutes can be up to 20160 minutes (14 days)",
observed live)."""
PROGRAM_TPS_INTERVAL_S: Final = 0.34
"""CreateProgram and DeleteProgram: 3 TPS per account (channel-assembly.md §5.6)."""
CONFLICT_ATTEMPTS: Final = 5
"""Attempts of a program write that keeps hitting ConflictException (backoff 1, 2, 4 and 8 s in between)."""
STATE_POLL_S: Final = 2.0
STATE_TIMEOUT_S: Final = 120.0
RECENT_PROGRAMS_MS: Final = 86_400_000
"""Library programs that started within this window may still be held by Channel Assembly. Older ones have aired
(and been deleted) or are RELATIVE programs that a stopped channel's schedule lists anyway; the rare older ABSOLUTE
program it might not list would only delay deleting its VOD source by 6-8 minutes, which the reaper retries."""
AIRED_RETENTION_MS: Final = 3_600_000
"""An aired program is gone from Channel Assembly this long after its end (about 15 minutes, with a margin)."""
MISSING: Final = frozenset({"NotFoundException", "ResourceNotFoundException"})

logger = Logger(child=True)

type OutOfTime = Callable[[], bool]


def _never() -> bool:
    return False


def error_code(error: ClientError) -> str:
    """The AWS error code of ``error``."""
    return str(error.response.get("Error", {}).get("Code", ""))


def is_missing(error: ClientError) -> bool:
    """Whether ``error`` says the resource does not exist."""
    return error_code(error) in MISSING


class ChannelStateTimeoutError(TimeoutError):
    """A started or stopped channel did not reach its state in time; the transition is still under way."""


# --- channel state -------------------------------------------------------------------------------------------------


def channel_state(channel: str) -> str | None:
    """``RUNNING`` or ``STOPPED``; ``None`` when the channel does not exist."""
    try:
        return str(aws.client("mediatailor").describe_channel(ChannelName=channel)["ChannelState"])
    except ClientError as error:
        if is_missing(error):
            return None
        raise


def wait_for_state(channel: str, state: str) -> None:
    """Poll DescribeChannel until ``channel`` reaches ``state``; raises :class:`ChannelStateTimeoutError`."""
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while (current := channel_state(channel)) != state:
        if time.monotonic() > deadline:
            raise ChannelStateTimeoutError(f"channel {channel} is still {current} after {STATE_TIMEOUT_S:.0f} s")
        time.sleep(STATE_POLL_S)


def stop_channel(channel: str) -> bool:
    """Stop ``channel`` (idempotent) and wait until it is stopped; ``False`` when it does not exist."""
    try:
        aws.client("mediatailor").stop_channel(ChannelName=channel)
    except ClientError as error:
        if is_missing(error):
            return False
        raise
    wait_for_state(channel, "STOPPED")
    return True


def start_channel(channel: str) -> None:
    """Start ``channel`` unless it runs already, and wait until it runs."""
    if channel_state(channel) == "RUNNING":
        return
    aws.client("mediatailor").start_channel(ChannelName=channel)
    wait_for_state(channel, "RUNNING")


def channel_arn(channel: str, known: str | None = None) -> str:
    """``known`` (the ARN the media library recorded), else the channel's ARN from DescribeChannel."""
    return known or str(aws.client("mediatailor").describe_channel(ChannelName=channel)["Arn"])


# --- schedule ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiveProgram:
    """A program in Channel Assembly's schedule, in list (= air) order."""

    name: str
    start_ms: int
    """The planned start from the media library when known, else Channel Assembly's approximate start."""


def _schedule_entries(channel: str) -> Iterator[Mapping[str, Any]]:
    pages = aws.client("mediatailor").get_paginator("get_channel_schedule")
    for page in pages.paginate(ChannelName=channel, DurationMinutes=str(SCHEDULE_MINUTES)):
        yield from page.get("Items", [])


def live_programs(channel: str, *, known_starts: Mapping[str, int] | None = None) -> list[LiveProgram]:
    """Programs of ``channel`` in schedule order (filler entries skipped).

    Channel Assembly only approximates starts (a RELATIVE program created on a stopped channel reports 1970, and
    sources it cannot read report zero durations; verified live), so the media library's planned starts win.
    """
    known = known_starts or {}
    return [
        LiveProgram(entry["ProgramName"], known.get(entry["ProgramName"], _epoch_ms(entry["ApproximateStartTime"])))
        for entry in _schedule_entries(channel)
        if entry.get("ScheduleEntryType") == "PROGRAM"
    ]


def scheduled_programs(channel: str) -> set[str]:
    """Names of the programs in the channel schedule (a stopped channel lists every program it holds within the
    look-ahead, its RELATIVE ones at 1970)."""
    return {str(entry["ProgramName"]) for entry in _schedule_entries(channel) if entry.get("ProgramName")}


def _epoch_ms(moment: datetime) -> int:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return round(aware.timestamp() * 1000)


# --- program writes ------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Pacer:
    """Spaces calls at least ``interval_s`` apart (MediaTailor per-API TPS quotas)."""

    interval_s: float = PROGRAM_TPS_INTERVAL_S
    _last: float = field(default=-math.inf)

    def wait(self) -> None:
        if (delay := self._last + self.interval_s - time.monotonic()) > 0:
            time.sleep(delay)
        self._last = time.monotonic()


def _with_conflict_retries[T](call: Callable[[], T], pacer: Pacer) -> T:
    """``call()`` paced, retrying ConflictException with exponential backoff."""
    for attempt in range(CONFLICT_ATTEMPTS - 1):
        pacer.wait()
        try:
            return call()
        except ClientError as error:
            if error_code(error) != "ConflictException":
                raise
        time.sleep(2**attempt)
    pacer.wait()
    return call()


def _already_exists(error: ClientError) -> bool:
    # A duplicate ProgramName is BadRequestException "This program already exists." (verified live).
    message = str(error.response.get("Error", {}).get("Message", ""))
    return error_code(error) in {"BadRequestException", "ConflictException"} and "already exists" in message


def create_programs(requests: Sequence[Mapping[str, Any]], pacer: Pacer | None = None) -> Iterator[tuple[str, bool]]:
    """CreateProgram for each request in order, yielding ``(program name, created)`` as soon as the program exists.

    ``created`` is ``False`` for a program that already existed (a retried publish). Lazy: a caller that records
    each yielded program keeps its own records in step with Channel Assembly even when a later call fails.
    """
    mediatailor, pacer = aws.client("mediatailor"), pacer or Pacer()
    for request in requests:
        try:
            _with_conflict_retries(partial(mediatailor.create_program, **request), pacer)
        except ClientError as error:
            if not _already_exists(error):
                raise
            yield request["ProgramName"], False
        else:
            yield request["ProgramName"], True


def delete_program(channel: str, program: str, pacer: Pacer | None = None) -> None:
    """Delete one program (a missing program is a success), retrying schedule conflicts."""
    delete = partial(aws.client("mediatailor").delete_program, ChannelName=channel, ProgramName=program)
    _with_conflict_retries(delete, pacer or Pacer())


def delete_programs(channel: str, names: Iterable[str], pacer: Pacer | None = None) -> list[str]:
    """Delete each program in order; returns the names deleted."""
    pacer = pacer or Pacer()
    deleted = []
    for name in names:
        delete_program(channel, name, pacer)
        deleted.append(name)
    return deleted


# --- drain ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Drained:
    """What :func:`drain_channel` did: programs deleted, and those it had no time left for."""

    deleted: int
    remaining: int

    @property
    def complete(self) -> bool:
        return self.remaining == 0


def drain_channel(channel: str, *, table_name: str | None = None, out_of_time: OutOfTime = _never) -> Drained:
    """Stop ``channel`` and delete every program Channel Assembly may still hold (nothing when it does not exist).

    The candidates are the channel's schedule plus, with ``table_name``, the library programs it may not list
    (:func:`recent_library_programs`); never the whole EPG history, which grows by a hundred or more programs a
    day while Channel Assembly forgets them as they air. Deletion stops early once ``out_of_time()`` (checked
    before each program). Deleted programs leave the schedule, so draining again resumes where a pass stopped
    (repeating only the few library-only candidates, whose deletion is then a no-op).
    """
    if not stop_channel(channel):
        return Drained(deleted=0, remaining=0)
    names = scheduled_programs(channel) | (recent_library_programs(table_name, channel) if table_name else set())
    pending = sorted(names)  # program names start with their UTC start time
    pacer, deleted = Pacer(), 0
    for name in pending:
        if out_of_time():
            break
        delete_program(channel, name, pacer)
        deleted += 1
    drained = Drained(deleted=deleted, remaining=len(pending) - deleted)
    logger.info(
        "channel drained", extra={"channel": channel, "deleted": drained.deleted, "remaining": drained.remaining}
    )
    return drained


def recent_library_programs(table_name: str, channel: str, *, now_ms: int | None = None) -> set[str]:
    """Names of the channel's library programs that Channel Assembly may still hold: those starting from a day ago
    on (the schedule omits programs beyond its 14-day look-ahead), except aired ones that ended more than
    :data:`AIRED_RETENTION_MS` ago, which Channel Assembly has deleted. A key-range query: older history is never
    read."""
    now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    request: dict[str, Any] = {
        "KeyConditionExpression": Key("pk").eq(f"{CHANNEL_PREFIX}{channel}")
        & Key("sk").between(program_sk(max(0, now - RECENT_PROGRAMS_MS)), program_sk(MAX_EPOCH_MS)),
        "ProjectionExpression": "#n, #s, start_ms, duration_ms",
        "ExpressionAttributeNames": {"#n": "name", "#s": "status"},
    }
    names: set[str] = set()
    while True:
        page = aws.table(table_name).query(**request)
        names |= {str(item["name"]) for item in page.get("Items", []) if item.get("name") and _held(item, now)}
        if not (start := page.get("LastEvaluatedKey")):
            return names
        request["ExclusiveStartKey"] = start


def _held(item: Mapping[str, Any], now_ms: int) -> bool:
    if item.get("status") != ProgramStatus.AIRED:
        return True
    end_ms = int(item.get("start_ms", 0)) + int(item.get("duration_ms", 0))
    return end_ms > now_ms - AIRED_RETENTION_MS
