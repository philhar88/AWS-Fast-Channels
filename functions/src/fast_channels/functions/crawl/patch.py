"""The crawl module's ``/policy`` patch contributor, which the PDS sidecar's ``/policy`` loads when ``modules.crawl`` is
on (docs/content-enrichment.md, DESIGN §9.6 and §9.8).

For each break it:

1. assigns the arm: ``bucket("crawl-v1", "<sid>.<evid>") < holdout_percent`` is the holdout (``enr`` 0), any other
   break the enriched arm (``enr`` 1); ``/select`` computes the same arm for our impression tracker (:func:`arm`);
2. finds the title's metadata: the content id comes from the UPID, then the PDS's copy of the catalog context objects
   (:class:`CatalogContexts`), else the ``x-fast-ctx`` header when it names the same id;
3. patches ``app.content`` (``core.crawl.ortb_content``) on the enriched arm only, and records the arm and where the
   metadata was found (``contributed.enr``, ``contributed.ctx``) in both.

The copy is read before the sidecar listens and refreshed every 30 seconds in the background, so no request waits on
S3: a list of ``catalog/ctx/v1/`` and a GET of each object whose ETag changed. It holds catalog metadata only.
"""

import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger
from pydantic import ValidationError

from fast_channels.core.crawl.catalog_context import CONTEXT_PREFIX, CatalogContext, context_key
from fast_channels.core.crawl.names import CONTRIBUTOR, RECORD_ARM, RECORD_SOURCE, Source
from fast_channels.core.crawl.ortb_content import (
    Metadata,
    app_content,
    channel_of,
    content_id,
    from_context,
    from_header,
)
from fast_channels.core.crawl.taxonomy import load_taxonomy
from fast_channels.core.pds.buckets import crawl_holdout
from fast_channels.core.pds.contributors import Contribution
from fast_channels.functions.pds.store import CLIENT_CONFIG, Refreshed, Spawn, spawn_thread
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from fast_channels.core.pds.models import Mode, PolicyRequest

REFRESH_SECONDS: Final = 30
"""How often the copy of the catalog is compared with the bucket."""
READERS: Final = 8
"""Concurrent GETs of changed objects (a cold start reads every title)."""

logger = Logger(child=True)

type Contexts = Mapping[str, CatalogContext]
"""Catalog context objects by content id."""


def arm(sid: str, evid: str, holdout_percent: int | None) -> int:
    """``enr`` of a session-break: 1 on the enriched arm, 0 in the ``crawl-v1`` holdout or with the module off
    (``holdout_percent`` None)."""
    if holdout_percent is None:
        return 0
    return 0 if crawl_holdout(sid, evid, holdout_percent) else 1


class CatalogContexts(Refreshed[Contexts]):
    """The PDS's copy of the catalog context objects of one bucket (module docstring).

    Args:
        bucket: the catalog-context bucket.
        client: an S3 client (default: one with the PDS's refresh timeouts).
        clock: monotonic seconds (tests pass their own).
        spawn: runs a refresh off the request path (default: a daemon thread; tests run it inline).
    """

    name = "catalog contexts"

    def __init__(
        self,
        bucket: str,
        *,
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
        spawn: Spawn = spawn_thread,
    ) -> None:
        super().__init__({}, every=REFRESH_SECONDS, clock=clock, spawn=spawn)
        self._bucket = bucket
        self._client = client
        self._etags: dict[str, str] = {}

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("s3", config=CLIENT_CONFIG)
        return self._client

    def _fetch(self) -> Contexts | None:
        listed: dict[str, str] = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self._bucket, Prefix=CONTEXT_PREFIX):
            for entry in page.get("Contents", []):
                key = entry["Key"]
                if key.endswith(".json"):
                    listed[key.removeprefix(CONTEXT_PREFIX).removesuffix(".json")] = entry["ETag"]
        if listed == self._etags:
            return None
        kept = {id_: ctx for id_, ctx in self._value.items() if listed.get(id_) == self._etags.get(id_)}
        changed = [id_ for id_ in listed if id_ not in kept]
        with ThreadPoolExecutor(max_workers=READERS) as pool:
            bodies = list(pool.map(self._read, changed))
        invalid: list[str] = []
        for id_, body in zip(changed, bodies, strict=True):
            try:
                context = CatalogContext.model_validate_json(body)
            except ValidationError:
                invalid.append(id_)
                continue
            if context.id == id_:
                kept[id_] = context
            else:
                invalid.append(id_)
        if invalid:
            logger.warning("catalog contexts skipped", extra={"invalid": invalid[:20], "count": len(invalid)})
        logger.info("catalog contexts loaded", extra={"titles": len(kept), "read": len(changed)})
        self._etags = listed
        return kept

    def _read(self, content: str) -> bytes:
        body: bytes = self.client.get_object(Bucket=self._bucket, Key=context_key(content))["Body"].read()
        return body

    def _describe(self, value: Contexts) -> dict[str, Any]:
        return {"titles": len(value)}


@dataclass(frozen=True, slots=True)
class CrawlContributor:
    """The crawl module's patch contributor (module docstring).

    Args:
        contexts: the current copy of the catalog context objects.
        holdout_percent: the ``crawl-v1`` holdout's share of session-breaks.
        publisher_domain: ``content.channel.domain``, when configured.
        genre_names: Content Taxonomy 3.1 names by id, for the header's ``genre``.
    """

    contexts: Callable[[], Contexts]
    holdout_percent: int
    publisher_domain: str | None = None
    genre_names: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return CONTRIBUTOR

    def contribute(self, request: PolicyRequest, mode: Mode) -> Contribution:
        enr = arm(request.sid, request.evid, self.holdout_percent)
        metadata, source = self.metadata(request)
        record = {RECORD_ARM: enr, RECORD_SOURCE: source.value}
        if not enr or metadata is None:
            return Contribution(record=record)
        channel = channel_of(request.pc, domain=self.publisher_domain)
        return Contribution(app_content(metadata, mode=mode, channel=channel), record)

    def metadata(self, request: PolicyRequest) -> tuple[Metadata | None, Source]:
        """The break's metadata and where it was found: the copy first, then a header naming the same title."""
        content = content_id(request.upid) if request.upid else None
        if content is None:
            return None, Source.NONE
        if (context := self.contexts().get(content)) is not None:
            return from_context(context), Source.SNAPSHOT
        header = from_header(request.ctx, genre_names=self.genre_names) if request.ctx else None
        if header is not None and header.id == content:
            return header, Source.HEADER
        return None, Source.NONE


@cache
def catalog_contexts() -> CatalogContexts:
    """The function's copy of the catalog (one per execution environment), on ``CATALOG_CONTEXT_BUCKET``."""
    settings().require("catalog_context_bucket")
    return CatalogContexts(settings().catalog_context_bucket)


@cache
def contributor() -> CrawlContributor | None:
    """The PDS's crawl contributor, or None with the module off (no ``CRAWL_HOLDOUT_PERCENT``).

    Raises:
        RuntimeError: the module is on without ``CATALOG_CONTEXT_BUCKET``.
    """
    configured = settings()
    if configured.crawl_holdout_percent is None:
        return None
    configured.require("catalog_context_bucket")
    return CrawlContributor(
        contexts=catalog_contexts().get,
        holdout_percent=configured.crawl_holdout_percent,
        publisher_domain=configured.publisher_domain or None,
        genre_names=load_taxonomy(),
    )
