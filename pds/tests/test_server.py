"""The sidecar's ASGI application: routing, the worker pool, startup and telemetry; and the process as ECS runs it."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from fast_channels.core.pds.snapshot import EMPTY, SnapshotBase, build, demo_demand
from fast_channels.pds import policy, select, server
from fast_channels.pds.http import Answer
from fast_channels.pds.server import ENDPOINTS, MAX_BODY, Sidecar
from fast_channels.pds.telemetry import counters, window

SELECT_FIXTURES = Path(__file__).parents[2] / "contracts" / "fixtures" / "pds" / "select"


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


async def serve[T](app: Sidecar, use: Callable[[httpx.AsyncClient], Coroutine[Any, Any, T]]) -> T:
    """``use`` a client of ``app`` between its startup and its shutdown."""
    await app.start()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8090") as client:
            return await use(client)
    finally:
        await app.stop()


def echo(threads: list[str]) -> Callable[[bytes, float], Answer]:
    def endpoint(body: bytes, started: float) -> Answer:
        threads.append(threading.current_thread().name)
        return Answer(200, json.dumps({"got": body.decode()}))

    return endpoint


def sidecar(endpoints: Mapping[str, Callable[[bytes, float], Answer]], **kwargs: Any) -> Sidecar:
    return Sidecar(endpoints, load=kwargs.pop("load", dict), **kwargs)


def test_each_path_is_its_endpoint_on_a_worker_thread() -> None:
    threads: list[str] = []
    app = sidecar({"/policy": echo(threads)})

    async def use(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/policy", content=b'{"v":1}')

    response = run(serve(app, use))

    assert (response.status_code, response.json()) == (200, {"got": '{"v":1}'})
    assert response.headers["content-type"] == "application/json"
    assert threads[0].startswith("pds"), "not the event loop's thread"


def test_health_unknown_paths_methods_and_large_bodies() -> None:
    app = sidecar({"/policy": echo([])})

    async def use(client: httpx.AsyncClient) -> list[int]:
        return [
            (await client.get("/health")).status_code,
            (await client.post("/health")).status_code,
            (await client.post("/other", content=b"{}")).status_code,
            (await client.get("/policy")).status_code,
            (await client.post("/policy", content=b"x" * (MAX_BODY + 1))).status_code,
        ]

    assert run(serve(app, use)) == [200, 405, 404, 405, 413]


def test_a_204_has_no_body_and_no_content_type() -> None:
    app = sidecar({"/ownbook/openrtb": lambda body, started: Answer(204)})

    async def use(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/ownbook/openrtb", content=b"{}")

    response = run(serve(app, use))

    assert (response.status_code, response.content, "content-type" in response.headers) == (204, b"", False)


def test_a_slow_endpoint_never_holds_the_event_loop() -> None:
    """A reservation's round trip (here 300 ms) runs on a worker: the loop answers meanwhile."""
    release = threading.Event()

    def slow(body: bytes, started: float) -> Answer:
        release.wait(5)
        return Answer(200, "{}")

    app = sidecar({"/select": slow})

    async def use(client: httpx.AsyncClient) -> float:
        pending = asyncio.create_task(client.post("/select", content=b"{}"))
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        assert (await client.get("/health")).status_code == 200
        elapsed = time.perf_counter() - started
        release.set()
        assert (await pending).status_code == 200
        return elapsed

    assert run(serve(app, use)) < 0.1


def test_startup_loads_before_serving_and_survives_a_failed_load(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> Mapping[str, Any]:
        raise RuntimeError("S3 unreachable")

    app = sidecar({"/policy": echo([])}, load=broken)

    async def use(client: httpx.AsyncClient) -> int:
        return (await client.get("/health")).status_code

    assert run(serve(app, use)) == 200
    started = [vars(r) for r in caplog.records if r.getMessage() == "pds started"]
    assert started and started[0]["workers"] == 16
    assert any(r.getMessage() == "startup load failed" for r in caplog.records)


def test_without_a_snapshot_policy_and_select_answer_503_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real endpoints, before any snapshot: the module serves the break SAFE and, with the own book silent,
    renders the kit's house ads from its configuration (HOUSE_CONFIG: tests/pbs)."""

    class Empty:
        def get(self) -> Any:
            return EMPTY

    monkeypatch.setenv("CONTENT_BASE_URL", "https://d111111abcdef8.cloudfront.net")
    from fast_channels.functions.settings import settings  # noqa: PLC0415

    settings.cache_clear()
    select.tracker_base.cache_clear()
    monkeypatch.setattr(policy, "store", Empty)
    monkeypatch.setattr(select, "store", Empty)
    app = sidecar(ENDPOINTS)
    body = json.dumps(json.loads((SELECT_FIXTURES / "demo-full-120s.json").read_text())["request"])
    question = {"v": 1, "sid": "6f1c2a9e-4b7d-4e2a-9c1e-2d3f4a5b6c7d", "pc": "fastm-demo-p-a", "evid": "1"}
    question |= {"upid": "u", "dur_ms": 60060}

    async def use(client: httpx.AsyncClient) -> list[tuple[int, float]]:
        timed = []
        for path, content in (("/policy", json.dumps(question)), ("/select", body)):
            started = time.perf_counter()
            response = await client.post(path, content=content)
            timed.append((response.status_code, (time.perf_counter() - started) * 1000))
        return timed

    try:
        answers = run(serve(app, use))
    finally:
        settings.cache_clear()
        select.tracker_base.cache_clear()

    assert [status for status, _ in answers] == [503, 503], answers
    assert max(ms for _, ms in answers) < 50, answers


def test_the_stats_line_and_the_metrics(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> None:
    counters.take()
    window.take()
    app = sidecar({"/policy": echo([])}, report_seconds=0.3)

    async def use(client: httpx.AsyncClient) -> None:
        for _ in range(3):
            await client.post("/policy", content=b"{}")
        counters.add("PolicyDecisions", dimension=("mode", "CONTEXTUAL"))
        counters.add("PolicyNoViewerIp")
        await asyncio.sleep(0.5)

    run(serve(app, use))

    [first, *_] = [vars(r)["stats"] for r in caplog.records if r.getMessage() == "pds stats"]
    assert first["policy_ms"]["n"] == 3
    assert first["pool_wait_ms"]["n"] == 3
    assert first["loop_lag_ms"]["n"] >= 2
    assert {"cpu_pct", "max_rss_mb"} <= set(first)
    emf = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"_aws"' in line]
    names = {m["Name"] for blob in emf for d in blob["_aws"]["CloudWatchMetrics"] for m in d["Metrics"]}
    assert {"PolicyDecisions", "PolicyNoViewerIp", "LoopLagMaxMs"} <= names
    [by_mode] = [blob for blob in emf if "PolicyDecisions" in blob]
    assert (by_mode["mode"], by_mode["PolicyDecisions"]) == ("CONTEXTUAL", [1.0])


def test_counters_from_many_threads_add_up() -> None:
    counters.take()

    def add() -> None:
        for _ in range(1000):
            counters.add("OwnBookBids", 2)

    workers = [threading.Thread(target=add) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sum(counters.take().values()) == 16000


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_the_process_listens_on_the_loopback_with_a_long_keep_alive(tmp_path: Any) -> None:
    """``python -m fast_channels.pds`` as the container runs it, without AWS: the loads fail fast, ``/health`` is 200
    and ``/policy`` answers 503 until a snapshot exists; an idle connection outlives uvicorn's 5 s default."""
    port = _free_port()
    environment = {
        **os.environ,
        "PDS_CONFIG_BUCKET": "fastm-pds-config",
        "PDS_TABLE": "fastm-pds",
        "LEDGER_TABLE": "fastm-ledger",
        "CONTENT_BASE_URL": "https://d111111abcdef8.cloudfront.net",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "none",
        "AWS_SECRET_ACCESS_KEY": "none",
        "AWS_ENDPOINT_URL": "http://127.0.0.1:9",  # nothing listens: every AWS call fails at once
        "POWERTOOLS_SERVICE_NAME": "pds",
        "PYTHONUNBUFFERED": "1",  # as the image sets it: each line reaches the log driver at once
    }
    process = subprocess.Popen(
        [sys.executable, "-c", f"from fast_channels.pds.__main__ import main; main(port={port})"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        started = time.monotonic()
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2) as client:
            while True:
                try:
                    health = client.get("/health")
                    break
                except httpx.TransportError:
                    assert time.monotonic() - started < 30, "the sidecar did not listen"
                    time.sleep(0.1)
            assert health.status_code == 200
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            assert _post(connection, "/policy").startswith(b"HTTP/1.1 503")
            time.sleep(6)
            assert _post(connection, "/policy").startswith(b"HTTP/1.1 503"), "the same connection, kept alive"
        with pytest.raises(httpx.TransportError):
            httpx.get(f"http://{_outside_address()}:{port}/health", timeout=1)
    finally:
        process.terminate()
        output, _ = process.communicate(timeout=30)
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    [up] = [line for line in lines if line.get("message") == "pds started"]
    assert "snapshot" not in up, "none loaded (Powertools drops null fields)"
    assert up["load_ms"] < 5000
    assert process.returncode in {0, -15}, output[-2000:]  # uvicorn re-raises the SIGTERM it handled


def _post(connection: socket.socket, path: str) -> bytes:
    """One keep-alive request on ``connection`` and the start of its response."""
    body = b'{"v":1,"sid":"s1","pc":"fastm-demo-p-a","evid":"1","upid":"u","dur_ms":60060}'
    head = f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
    connection.sendall(f"{head}Content-Length: {len(body)}\r\n\r\n".encode() + body)
    received = b""
    while b"\r\n\r\n" not in received:
        received += connection.recv(4096)
    headers, _, rest = received.partition(b"\r\n\r\n")
    length = int(next(line for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:"))[15:])
    while len(rest) < length:
        rest += connection.recv(4096)
    return headers


def _outside_address() -> str:
    """An address of this host other than the loopback (the sidecar must not answer on it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: no packet is sent
        return str(probe.getsockname()[0])


def test_the_built_snapshot_is_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a snapshot, the same request is a decision (the endpoint tests cover the answers)."""
    snapshot = build(SnapshotBase(demand=demo_demand("fastm")), now=datetime(2026, 10, 29, tzinfo=UTC))

    class Loaded:
        def get(self) -> Any:
            return snapshot

    monkeypatch.setattr(policy, "store", Loaded)
    app = sidecar({"/policy": server.ENDPOINTS["/policy"]})
    question = {"v": 1, "sid": "s1", "pc": "fastm-demo-p-a", "evid": "1", "upid": "u", "dur_ms": 60060}

    async def use(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/policy", json=question)

    response = run(serve(app, use))

    assert (response.status_code, response.json()["mode"]) == (200, "CONTEXTUAL")
