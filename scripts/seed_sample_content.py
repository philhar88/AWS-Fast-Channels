#!/usr/bin/env -S uv run python
"""Load the open-licensed sample titles into the reference catalog and encode them with the default ladder.

    uv run python scripts/seed_sample_content.py                     # every title; asks before encoding
    uv run python scripts/seed_sample_content.py --titles sintel-trailer elephants-dream --yes --wait
    uv run python scripts/seed_sample_content.py --dry-run           # what would happen, and what it costs

For each title of assets/sample-catalog.json the script writes the ReferenceCatalog item that the catalog tool
serves, then streams the mezzanine from its publisher into the ingest bucket (``samples/<content id>.<ext>``) after
its JSON sidecar, which is exactly how a CMS hands content over: the upload triggers the ingest pipeline, which
encodes it, registers the VOD source and marks the asset READY in the media library. (MediaConvert can read HTTPS
inputs directly, but the Blender Foundation's servers refuse its fetcher, so the catalog item's ``mezzanine_uri`` is
the staged ``s3://`` copy and the publisher's URL is kept as ``source_url``.) Titles that are already READY with the
ladder, or being ingested, are not encoded again, so the script can be re-run. MediaConvert bills per output
minute: the estimate is printed first and has to be confirmed unless ``--yes`` is given. Each request's
correlation id is printed; ``scripts/trace.py <id>`` follows it.
"""

import argparse
import io
import json
import sys
import time
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from _stack import REPO, Plane, Stage, add_prefix_argument, run
from boto3.s3.transfer import TransferConfig

from fast_channels.core.catalog import ContentItem
from fast_channels.core.cost import encode_cost
from fast_channels.core.ingest import IngestRequest
from fast_channels.core.mediaconvert import needs_esam
from fast_channels.core.naming import slug
from fast_channels.functions.aws import execution_arn
from fast_channels.functions.tools.catalog import catalog_item
from fast_channels.functions.tools.library_store import (
    Asset,
    AssetStatus,
    LadderRecord,
    LadderStatus,
    asset_key,
    ladder_key,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_stepfunctions import SFNClient

    from fast_channels.core.ladder import Ladder

CATALOG: Final = REPO / "assets" / "sample-catalog.json"
NOT_AGAIN: Final = frozenset({AssetStatus.READY, AssetStatus.INGESTING, AssetStatus.ENCODED})
"""Library statuses of an encode that is done or under way."""
SAMPLES_PREFIX: Final = "samples/"
UPLOAD: Final = TransferConfig(multipart_chunksize=64 * 1024 * 1024, max_concurrency=8)
POLL_SECONDS: Final = 15
START_GRACE_SECONDS: Final = 180
"""How long an upload may go without an execution before it counts as rejected (the trigger then publishes an
``Asset Failed`` event with error InvalidIngestRequest instead of starting one)."""


@dataclass(frozen=True, slots=True)
class Title:
    """A catalog title, its ingest request and the library status of that encode (None: never ingested)."""

    content: ContentItem
    request: IngestRequest
    status: AssetStatus | None

    @property
    def asset_id(self) -> str:
        return self.request.resolved_asset_id

    @property
    def needs_ingest(self) -> bool:
        return self.status not in NOT_AGAIN


@dataclass(frozen=True, slots=True)
class Progress:
    """Where one requested ingest stands."""

    state: str
    done: bool = False
    ready: bool = False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--titles", nargs="+", metavar="CONTENT_ID", help="only these titles (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and its cost; change nothing")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation before encoding")
    parser.add_argument("--wait", action="store_true", help="poll the media library until every encode is done")
    add_prefix_argument(parser)
    args = parser.parse_args()

    stage = Stage.of(args.prefix)
    dynamodb = stage.session.resource("dynamodb", region_name=stage.region)
    library = dynamodb.Table(stage.output(Plane.DATA, "LibraryTableName"))
    catalog = dynamodb.Table(stage.output(Plane.DATA, "CatalogTableName"))
    ladder_version = stage.output(Plane.MEDIA, "DefaultLadderVersion")
    ladder = provisioned_ladder(library, ladder_version)
    titles = [title_of(library, content, ladder_version) for content in sample_titles(CATALOG, args.titles or ())]

    print(f"Deployment {stage.prefix} in {stage.region}, ladder {ladder_version} (MediaConvert Basic tier)\n")
    estimate = print_plan(titles, ladder)
    pending = [title for title in titles if title.needs_ingest]
    if args.dry_run:
        print(f"\nDry run: would write {len(titles)} catalog items and request {len(pending)} ingests.")
        return 0
    if pending and not args.yes and not confirm(f"\nEncode {len(pending)} titles for about ${estimate:.2f}? [y/N] "):
        print("Nothing changed.")
        return 1

    bucket = stage.output(Plane.DATA, "IngestBucketName")
    write_catalog(catalog, titles, ingest_bucket=bucket)
    print(f"\nWrote {len(titles)} items to {catalog.name}.")
    if not pending:
        return 0
    s3 = stage.session.client("s3", region_name=stage.region)
    print("Uploading to the ingest bucket (trace one with: uv run python scripts/trace.py <correlation id>):")
    with httpx.Client(follow_redirects=True, timeout=60) as http:
        for title in pending:
            key = upload(s3, http, bucket, title)
            print(f"  {title.content.content_id:<26} {title.request.correlation_id}  s3://{bucket}/{key}")
    if not args.wait:
        return 0
    sfn = stage.session.client("stepfunctions", region_name=stage.region)
    return 0 if wait(library, sfn, stage.output(Plane.MEDIA, "IngestStateMachineArn"), pending) else 1


def sample_titles(path: Path, wanted: Collection[str]) -> list[ContentItem]:
    """The catalog's titles, or the ``wanted`` ones (in catalog order); exits on an unknown content id."""
    titles = [ContentItem.model_validate(title) for title in json.loads(path.read_text())["titles"]]
    if unknown := sorted(set(wanted) - {t.content_id for t in titles}):
        sys.exit(f"unknown titles {', '.join(unknown)}; {path.name} has {', '.join(t.content_id for t in titles)}")
    return [title for title in titles if not wanted or title.content_id in wanted]


def provisioned_ladder(library: Table, ladder_version: str) -> Ladder:
    """The definition of ``ladder_version``; exits unless the pipeline can encode with it (PROVISIONING or READY)."""
    item = library.get_item(Key=ladder_key(ladder_version)).get("Item")
    if item is None:
        sys.exit(f"ladder {ladder_version} is not in {library.name}: has the Media stack finished deploying?")
    record = LadderRecord.from_item(item)
    if record.status not in {LadderStatus.PROVISIONING, LadderStatus.READY}:
        sys.exit(f"ladder {ladder_version} is {record.status}: the ingest pipeline refuses requests for it")
    return record.definition()


def title_of(library: Table, content: ContentItem, ladder_version: str) -> Title:
    """``content`` with a fresh ingest request and the library status of its encode with ``ladder_version``."""
    if content.mezzanine_uri is None:
        sys.exit(f"title {content.content_id} has no mezzanine_uri")
    request = IngestRequest(
        source_uri=content.mezzanine_uri,
        asset_id=slug(content.content_id),
        content_id=content.content_id,
        cue_points_ms=content.cue_points_ms,
        ladder_version=ladder_version,
        correlation_id=f"seed-{slug(content.content_id)}-{uuid4().hex[:8]}",
        metadata={"title": content.title},
    )
    item = library.get_item(Key=asset_key(request.resolved_asset_id, ladder_version)).get("Item")
    return Title(content, request, Asset.from_item(item).status if item else None)


def print_plan(titles: list[Title], ladder: Ladder) -> float:
    """Print each title's status and encode cost; returns the total for the titles that need an ingest."""
    print(f"{'title':<26} {'duration':>9}  {'library':<10} {'encode cost':>11}")
    total = 0.0
    for title in titles:
        duration_ms = title.content.duration_ms
        professional = needs_esam(ladder, title.request.cue_points_ms)  # off-grid cue points add ESAM
        cost = encode_cost(ladder, duration_ms, professional=professional) if duration_ms else None
        if title.needs_ingest and cost is not None:
            total += cost
        status = title.status or "-"
        price = "skipped" if not title.needs_ingest else f"${cost:.2f}" if cost is not None else "unknown"
        print(f"{title.content.content_id:<26} {minutes(duration_ms):>9}  {status:<10} {price:>11}")
    print(f"{'total':<26} {'':>9}  {'':<10} {f'${total:.2f}':>11}")
    return total


def minutes(duration_ms: int | None) -> str:
    if duration_ms is None:
        return "?"
    seconds = round(duration_ms / 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


def confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def staged_key(content: ContentItem) -> str:
    """Ingest-bucket key of a title's staged mezzanine: ``samples/<content id><extension of the publisher's file>``."""
    return f"{SAMPLES_PREFIX}{content.content_id}{Path(urlparse(str(content.mezzanine_uri)).path).suffix.lower()}"


def write_catalog(catalog: Table, titles: list[Title], *, ingest_bucket: str) -> None:
    """Put each title's ReferenceCatalog item (exactly what the catalog tool reads), linked to its asset.

    The item's ``mezzanine_uri`` is the staged copy in ``ingest_bucket``, which MediaConvert can read, and the
    publisher's URL stays on the item as ``source_url`` (metadata that ``ContentItem`` and the tools ignore).
    """
    with catalog.batch_writer() as batch:
        for title in titles:
            staged = f"s3://{ingest_bucket}/{staged_key(title.content)}"
            linked = title.content.model_copy(update={"asset_ids": [title.asset_id], "mezzanine_uri": staged})
            batch.put_item(Item={**catalog_item(linked), "source_url": str(title.content.mezzanine_uri)})


def upload(s3: S3Client, http: httpx.Client, bucket: str, title: Title) -> str:
    """Stream the title's mezzanine into the ingest bucket after its sidecar; returns the object key.

    The sidecar carries the request (asset and content ids, cue points, ladder, correlation id), so the upload's
    ``Object Created`` event becomes exactly this ingest request.
    """
    uri, key = str(title.request.source_uri), staged_key(title.content)
    sidecar = title.request.model_dump(mode="json", exclude={"source_uri", "generator"}, exclude_none=True)
    s3.put_object(Bucket=bucket, Key=f"{key}.json", Body=json.dumps(sidecar).encode(), ContentType="application/json")
    with http.stream("GET", uri) as response:
        response.raise_for_status()
        body = io.BufferedReader(_Chunks(response.iter_bytes(8 * 1024 * 1024)), buffer_size=8 * 1024 * 1024)
        s3.upload_fileobj(
            body,
            bucket,
            key,
            Config=UPLOAD,
            ExtraArgs={"ContentType": response.headers.get("content-type", "application/octet-stream")},
        )
    return key


class _Chunks(io.RawIOBase):
    """A readable stream over an iterator of byte chunks (an HTTP body), for boto3's multipart upload."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._pending = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        while not self._pending:
            if (chunk := next(self._chunks, None)) is None:
                return 0
            self._pending = chunk
        size = min(len(buffer), len(self._pending))
        buffer[:size] = self._pending[:size]
        self._pending = self._pending[size:]
        return size


def wait(library: Table, sfn: SFNClient, state_machine_arn: str, titles: list[Title]) -> bool:
    """Print every status change until each ingest is done; returns whether all of them are READY."""
    started = time.monotonic()
    last: dict[str, Progress] = {}
    while True:
        late = time.monotonic() - started > START_GRACE_SECONDS
        now = {t.content.content_id: progress(library, sfn, state_machine_arn, t, late=late) for t in titles}
        for content_id, state in now.items():
            if last.get(content_id) != state:
                print(f"{datetime.now(UTC):%H:%M:%S}  {content_id:<26} {state.state}")
        last = now
        if all(state.done for state in now.values()):
            ready = sum(state.ready for state in now.values())
            print(f"\n{ready} of {len(titles)} titles READY.")
            return ready == len(titles)
        time.sleep(POLL_SECONDS)


def progress(library: Table, sfn: SFNClient, state_machine_arn: str, title: Title, *, late: bool) -> Progress:
    """The library status of this request's encode, or the state of its execution before Prepare wrote it."""
    item = library.get_item(Key=asset_key(title.asset_id, title.request.ladder_version)).get("Item")
    if item is not None and item.get("correlation_id") == title.request.correlation_id:
        asset = Asset.from_item(item)
        if asset.status is AssetStatus.FAILED:
            return Progress(f"FAILED: {asset.error}", done=True)
        return Progress(asset.status, done=asset.status is AssetStatus.READY, ready=asset.status is AssetStatus.READY)
    name = str(title.request.correlation_id)
    try:
        execution = sfn.describe_execution(executionArn=execution_arn(state_machine_arn, name))
    except sfn.exceptions.ExecutionDoesNotExist:
        if late:
            return Progress(f"REJECTED: no execution started (scripts/trace.py {name})", done=True)
        return Progress("requested")
    status = execution["status"]
    return Progress(f"execution {status}", done=status not in {"RUNNING", "PENDING_REDRIVE"})


if __name__ == "__main__":
    run(main)
