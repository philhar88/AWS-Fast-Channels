"""FAST Channels probe: the ops module's CloudWatch Synthetics canary (Python runtime; stdlib and boto3 only).

Every five minutes while the census counts a running channel (``CENSUS_PARAMETER``), it plays each running channel
of ``CHANNELS`` (``{"<channel>": "<SSAI multivariant playlist URL>"}``) as a viewer would:

- the multivariant playlist resolves and lists a variant;
- the variant's media sequence advances between two polls a target duration and a half apart (the channel is on
  air: two failed runs in a row start runbook R12);
- the newest segment resolves (its first kilobyte);
- every ``EXT-X-DATERANGE`` has an ``ID`` and a ``START-DATE`` that parses.

With no running channel the run passes and logs ``skipped``; the census also stops the canary itself while no
channel runs (``Census Changed``). Until playback tickets exist (M4) its sessions are ordinary viewer sessions of the
IaC playback configuration; a ``-p-z`` probe sibling replaces it once the release module has one.
"""

import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime
from typing import Any, Final
from urllib.parse import urljoin

import boto3

TIMEOUT_S: Final = 10
MAX_WAIT_S: Final = 15.0
USER_AGENT: Final = "fast-channels-probe/1"

logger = logging.getLogger("fast_probe")
logger.setLevel(logging.INFO)


class ProbeError(RuntimeError):
    """A channel failed the probe."""


def fetch(url: str, *, first_bytes: int | None = None) -> tuple[str, bytes]:
    """The final URL (after redirects) and body of ``url``, or its first ``first_bytes`` bytes."""
    headers = {"User-Agent": USER_AGENT, **({"Range": f"bytes=0-{first_bytes - 1}"} if first_bytes else {})}
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - https URLs from the stack
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
        return response.geturl(), response.read()


def variants(base: str, playlist: str) -> list[str]:
    """Variant URLs of a multivariant playlist, absolute."""
    lines = [line.strip() for line in playlist.splitlines()]
    return [
        urljoin(base, lines[i + 1])
        for i, line in enumerate(lines[:-1])
        if line.startswith("#EXT-X-STREAM-INF") and lines[i + 1] and not lines[i + 1].startswith("#")
    ]


def media_sequence(playlist: str) -> int:
    match = re.search(r"^#EXT-X-MEDIA-SEQUENCE:(\d+)", playlist, re.MULTILINE)
    if match is None:
        raise ProbeError("no EXT-X-MEDIA-SEQUENCE in the media playlist")
    return int(match.group(1))


def target_duration(playlist: str) -> int:
    match = re.search(r"^#EXT-X-TARGETDURATION:(\d+)", playlist, re.MULTILINE)
    return int(match.group(1)) if match else 6


def segments(base: str, playlist: str) -> list[str]:
    """Segment URLs of a media playlist, absolute."""
    return [urljoin(base, line.strip()) for line in playlist.splitlines() if line.strip() and not line.startswith("#")]


def dateranges(playlist: str) -> list[dict[str, str]]:
    """The attributes of every ``EXT-X-DATERANGE`` tag."""
    found = []
    for line in playlist.splitlines():
        if line.startswith("#EXT-X-DATERANGE:"):
            pairs = re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.removeprefix("#EXT-X-DATERANGE:"))
            found.append({key: value.strip('"') for key, value in pairs})
    return found


def probe(channel: str, url: str) -> dict[str, Any]:
    """Play ``channel`` from its multivariant playlist ``url``; raises :class:`ProbeError` when it fails a check."""
    base, body = fetch(url)
    listed = variants(base, body.decode())
    if not listed:
        raise ProbeError(f"{channel}: the multivariant playlist lists no variant")
    variant_url, first = fetch(listed[0])
    before = media_sequence(first.decode())
    wait = min(target_duration(first.decode()) * 1.5, MAX_WAIT_S)
    time.sleep(wait)
    variant_url, second = fetch(listed[0])
    playlist = second.decode()
    after = media_sequence(playlist)
    if after <= before:
        raise ProbeError(f"{channel}: the media sequence stayed at {before} for {wait:.0f} s (off air?)")
    newest = segments(variant_url, playlist)
    if not newest:
        raise ProbeError(f"{channel}: the media playlist lists no segment")
    _, head = fetch(newest[-1], first_bytes=1024)
    if not head:
        raise ProbeError(f"{channel}: the newest segment is empty")
    ranges = dateranges(playlist)
    for attributes in ranges:
        if not attributes.get("ID"):
            raise ProbeError(f"{channel}: an EXT-X-DATERANGE has no ID")
        try:
            datetime.fromisoformat(attributes.get("START-DATE", "").replace("Z", "+00:00"))
        except ValueError:
            raise ProbeError(f"{channel}: DATERANGE {attributes['ID']} has no valid START-DATE") from None
    return {"channel": channel, "media_sequence": [before, after], "dateranges": len(ranges)}


def handler(event: Any, context: Any) -> dict[str, Any]:
    """Probe every running channel; pass with ``skipped`` while none runs."""
    running = int(boto3.client("ssm").get_parameter(Name=os.environ["CENSUS_PARAMETER"])["Parameter"]["Value"])
    channels: dict[str, str] = json.loads(os.environ["CHANNELS"])
    if running == 0:
        logger.info("skipped: the census counts no running channel")
        return {"skipped": True}
    mediatailor = boto3.client("mediatailor")
    on_air = [name for name in channels if mediatailor.describe_channel(ChannelName=name)["ChannelState"] == "RUNNING"]
    if not on_air:
        logger.info("skipped: no channel of this deployment runs")
        return {"skipped": True}
    results, failures = [], []
    for name in on_air:
        try:
            results.append(probe(name, channels[name]))
        except (ProbeError, OSError) as error:
            failures.append(f"{name}: {error}")
    logger.info("probed %s", json.dumps(results))
    if failures:
        raise ProbeError("; ".join(failures))
    return {"skipped": False, "probed": results}
