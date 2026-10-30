"""The sidecar's ASGI application: ``/policy``, ``/select`` and ``/ownbook/openrtb`` for Prebid Server on
``127.0.0.1:8090`` in the same task, and ``GET /health`` for the container's health check.

- **Startup** (the ASGI lifespan, before uvicorn listens): :func:`state.load` reads the snapshot, the lines' budgets
  and, with the crawl module on, the catalog contexts, and opens the reservations' connection. A read that fails is
  logged and leaves the empty value; the refreshes keep trying. So ``/health`` answers 200 as soon as the server
  listens, with or without a snapshot, and Prebid Server (which waits for that) starts in any case: without a
  snapshot ``/policy`` and ``/select`` answer 503 at once and the module falls back (``http.NO_SNAPSHOT``).
- **Requests**: the event loop reads the body and hands the endpoint to a bounded pool of :data:`state.WORKERS`
  threads. The endpoints are the synchronous code the Lambda functions ran (pydantic, the pod rules, boto3 on the
  locked SDK), so the loop only parses HTTP and never waits on a reservation or a pod rule.
- **Telemetry**: each minute the loop emits the counts as metrics and logs one ``pds stats`` line: per-endpoint
  latency, the pool's queue wait, the event loop's lag (sampled every :data:`LAG_SAMPLE_S`), the process's CPU share
  and peak memory.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final

from aws_lambda_powertools import Logger
from aws_lambda_powertools.metrics import MetricUnit

from fast_channels.core.pds.names import Endpoint
from fast_channels.pds import ownbook, policy, select, state
from fast_channels.pds.http import Answer
from fast_channels.pds.telemetry import REPORT_SECONDS, counters, window

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type Endpoints = Mapping[str, Callable[[bytes, float], Answer]]

HEALTH: Final = "/health"
MAX_BODY: Final = 1 << 20
"""The largest request body read (a ``/select`` request with every bid's VAST is tens of kilobytes)."""
LAG_SAMPLE_S: Final = 0.1
"""How often the event loop's lag is sampled: the delay of a sleep of this length beyond its length."""
ENDPOINTS: Final[Endpoints] = {
    Endpoint.POLICY.value: policy.answer,
    Endpoint.SELECT.value: select.answer,
    Endpoint.OWN_BOOK.value: ownbook.answer,
}
_JSON: Final = [(b"content-type", b"application/json")]

logger = Logger()


class Sidecar:
    """The ASGI application (module docstring).

    Args:
        endpoints: path -> the endpoint's answer for a body and the request's arrival (``time.perf_counter``).
        load: what startup runs before the server listens.
        workers: the pool's threads.
        report_seconds: how often telemetry is emitted.
    """

    def __init__(
        self,
        endpoints: Endpoints = ENDPOINTS,
        *,
        load: Callable[[], Mapping[str, Any]] = state.load,
        workers: int = state.WORKERS,
        report_seconds: float = REPORT_SECONDS,
    ) -> None:
        self._endpoints = endpoints
        self._load = load
        self._workers = workers
        self._report_seconds = report_seconds
        self._pool: ThreadPoolExecutor | None = None
        self._telemetry: asyncio.Task[None] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        match scope["type"]:
            case "lifespan":
                await self._lifespan(receive, send)
            case "http":
                await self._http(scope, receive, send)

    # ----- lifespan --------------------------------------------------------------------------------------------

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            match message["type"]:
                case "lifespan.startup":
                    await self.start()
                    await send({"type": "lifespan.startup.complete"})
                case "lifespan.shutdown":
                    await self.stop()
                    await send({"type": "lifespan.shutdown.complete"})
                    return

    async def start(self) -> None:
        """Create the pool, load what the endpoints serve from, and start the telemetry."""
        self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="pds")
        started = time.perf_counter()
        try:
            loaded = await asyncio.get_running_loop().run_in_executor(self._pool, self._load)
        except Exception:  # the endpoints serve the empty values; the refreshes keep trying
            logger.exception("startup load failed")
            loaded = {}
        logger.info(
            "pds started",
            extra={**loaded, "workers": self._workers, "startup_ms": round((time.perf_counter() - started) * 1000, 1)},
        )
        self._telemetry = asyncio.create_task(self._report())

    async def stop(self) -> None:
        """Stop the telemetry (emitting what it holds) and let the pool finish its requests."""
        if self._telemetry is not None:
            self._telemetry.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._telemetry
        self._emit()
        if self._pool is not None:
            self._pool.shutdown(wait=True)

    # ----- requests --------------------------------------------------------------------------------------------

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = time.perf_counter()
        path, method = scope["path"], scope["method"]
        if path == HEALTH:
            await _respond(send, Answer(200, '{"status":"ok"}') if method in {"GET", "HEAD"} else _NOT_ALLOWED)
            return
        if (endpoint := self._endpoints.get(path)) is None:
            await _respond(send, _NOT_FOUND)
            return
        if method != "POST":
            await _respond(send, _NOT_ALLOWED)
            return
        body = await _body(receive)
        if body is None:
            await _respond(send, _TOO_LARGE)
            return
        assert self._pool is not None, "requests arrive after startup"

        def run() -> Answer:
            window.add("pool_wait_ms", (time.perf_counter() - started) * 1000)
            return endpoint(body, started)

        answer = await asyncio.get_running_loop().run_in_executor(self._pool, run)
        await _respond(send, answer)
        window.add(f"{path.strip('/').replace('/', '_')}_ms", (time.perf_counter() - started) * 1000)

    # ----- telemetry -------------------------------------------------------------------------------------------

    async def _report(self) -> None:
        loop = asyncio.get_running_loop()
        due = loop.time() + self._report_seconds
        while True:
            before = loop.time()
            await asyncio.sleep(LAG_SAMPLE_S)
            now = loop.time()
            window.add("loop_lag_ms", max(0.0, (now - before - LAG_SAMPLE_S) * 1000))
            if now >= due:
                due = now + self._report_seconds
                self._emit()

    def _emit(self) -> None:
        stats = window.take()
        lag = stats.get("loop_lag_ms", {})
        if lag:
            counters.add("LoopLagMaxMs", float(lag["max"]), unit=MetricUnit.Milliseconds)
        logger.info("pds stats", extra={"stats": stats})
        counters.flush()


async def _body(receive: Receive) -> bytes | None:
    """The request's body, or None past :data:`MAX_BODY`."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


_NOT_FOUND: Final = Answer(404, '{"error":"not found"}')
_NOT_ALLOWED: Final = Answer(405, '{"error":"method not allowed"}')
_TOO_LARGE: Final = Answer(413, '{"error":"too large"}')


async def _respond(send: Send, answer: Answer) -> None:
    body = answer.body.encode()
    headers = [*(_JSON if body else []), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": answer.status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


app = Sidecar()
"""The sidecar (``python -m fast_channels.pds`` serves it)."""
