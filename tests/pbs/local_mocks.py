"""Local stand-ins for everything Prebid Server calls in the contract tests, with an exact call ledger.

- ``/policy`` (on its own port, so a test can make it unreachable): the Pod Decision Service's policy, steered by the
  ticket (``X-Fast-T``) the module forwards; see :data:`POLICY_TICKETS`.
- ``/select``: the Pod Decision Service's pod rules (``fast_channels.core.pds.pod``), with its trackers, or HTTP 503
  while ``select_fails`` is set.

Both service calls are validated against the contract's models (``fast_channels.core.pds.models``, strict, version
1), as the service validates them: a request that does not match is answered 422 and recorded in
``Mocks.contract_errors``.
- ``/ownbook/openrtb``: the own book, one house ad per slot (``ext.fast.house``).
- ``/amt``: the guidance's bidder simulator, ``handler.py`` from the applied checkout with overlay O5 and
  :data:`SIMULATOR_CREATIVES`.
- ``/echo26``: an OpenRTB 2.6 bidder that records what it receives and never bids.

Every call is recorded (:class:`Call`): path, header names and the parsed body.
"""

import importlib.util
import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from pydantic import JsonValue, ValidationError

from fast_channels.core.pds.models import (
    OWN_BOOK,
    Device,
    DeviceType,
    Fpd,
    Mode,
    PolicyRequest,
    PolicyResponse,
    Reason,
    Regs,
    SelectRequest,
)
from fast_channels.core.pds.pod import select_pod, select_response

TRACKER_BASE: Final = "https://cdn.example/ads/b.gif"
TRACKER_ENR: Final = 1
"""The service's trackers carry ``enr=1`` here and the module's own ``enr=0``, so a test can tell them apart."""
THIRD_PARTIES: Final = ("/amt", "/echo26")
"""Bidders that are not the own book: the no-hook contract counts calls to these."""
CONTENT: Final = {
    "id": "tears-of-steel",
    "title": "Tears of Steel",
    "genre": "Science Fiction",
    "cat": ["652", "325"],
    "cattax": 9,
    "contentrating": "TV-14",
    "len": 734,
    "language": "en",
    "ext": {"genres": ["652", "325"], "gtax": 9, "realtime": 0, "firstbroadcast": 0},
}
"""The ``app_content`` a positive policy answer patches in: OpenRTB 2.5 fields natively, the 2.6 ones in ``ext``."""
POSITIVE: Final = {"FULL1": "FULL", "PSEUDO1": "PSEUDONYMOUS", "CTX1": "CONTEXTUAL", "": "CONTEXTUAL", "late1": "FULL"}
"""Tickets the policy answers positively, with third-party bidders (an empty ticket: the Phase-1 contextual case)."""
POLICY_TICKETS: Final = {
    **{ticket: f"positive {mode}" for ticket, mode in POSITIVE.items()},
    "late1": "positive FULL after 25 ms: in time for the module, past a 5 ms group timeout",
    "SAFE1": "SAFE, no bidders",
    "SAFEbad1": "SAFE that wrongly names bidders (the module must add none)",
    "KIDS1": "KIDS, no bidders",
    "KIDSbad1": "KIDS that wrongly names bidders",
    "slow1": "FULL with bidders after 200 ms (past the module's 50 ms timeout)",
    "err1": "HTTP 500",
    "junk1": "200 with a body that is not JSON",
    "v2x1": "an unknown schema version",
}
SIMULATOR_CREATIVES: Final[list[dict[str, Any]]] = [
    {"crid": "sim1", "adomain": ["brand-a.example"], "price": 12.5, "burl": None, "impression": False},
    {"crid": "sim2", "adomain": ["brand-b.example"], "price": 22.5, "burl": True, "impression": True},
    {"crid": "sim3", "adomain": ["brand-c.example"], "price": 35.0, "burl": True, "impression": False},
    {"crid": "sim4", "adomain": ["brand-c.example"], "price": 30.0, "burl": True, "impression": False},
]
"""sim1 has neither an impression nor a billing notice (our tracker is then its only impression); sim3 and sim4
share an advertiser domain."""


def vast(crid: str, *, impression: str | None = None) -> str:
    """A 30.03 s inline VAST ad on the ladder (as the demo creatives are)."""
    tracker = f"<Impression><![CDATA[{impression}]]></Impression>" if impression else ""
    return (
        f'<VAST version="4.2"><Ad id="{crid}"><InLine><AdSystem>test</AdSystem><AdTitle>{crid}</AdTitle>{tracker}'
        "<Creatives><Creative><Linear><Duration>00:00:30.030</Duration><MediaFiles>"
        '<MediaFile delivery="streaming" type="application/x-mpegURL" width="1920" height="1080">'
        f"<![CDATA[https://cdn.example/ads/standard/demo/{crid}/main.m3u8]]></MediaFile></MediaFiles></Linear>"
        "</Creative></Creatives></InLine></Ad></VAST>"
    )


def simulator_creatives() -> list[dict[str, Any]]:
    """``video_creatives.json`` for overlay O5."""
    return [
        {
            "crid": c["crid"],
            "adomain": c["adomain"],
            "price": c["price"],
            "dur": 30,
            "w": 1920,
            "h": 1080,
            "adm": vast(c["crid"], impression="https://brand.example/i" if c["impression"] else None),
            **(
                {
                    "burl": f"https://cdn.example/ads/b.gif?seat=amt&cr={c['crid']}&p=${{AUCTION_PRICE}}&e=burl",
                    "nurl": f"https://cdn.example/ads/b.gif?seat=amt&cr={c['crid']}&p=${{AUCTION_PRICE}}&e=nurl",
                }
                if c["burl"]
                else {}
            ),
        }
        for c in SIMULATOR_CREATIVES
    ]


def policy_answer(request: PolicyRequest) -> tuple[int, str, float]:
    """``/policy``'s status, body and delay for a request, steered by its ticket (:data:`POLICY_TICKETS`); every
    answer is built from the contract's models, so it is one the Pod Decision Service could send."""
    ticket = request.t
    bidders: dict[str, dict[str, JsonValue]] = {"amt": {}, "echo26": {}}

    def answer(mode: Mode, reason: Reason, named: dict[str, dict[str, JsonValue]], *, version: int = 1) -> str:
        response = PolicyResponse(
            decision_id=request.decision_id,
            mode=mode,
            reason=reason,
            bidders=named,
            regs=Regs(coppa=1 if mode is Mode.KIDS else 0),
            device=Device(ip=IPv4Address("198.51.100.7"), devicetype=DeviceType.CONNECTED_TV),
            app_content=CONTENT,
            fpd=Fpd(bidders=[OWN_BOOK]),
        )
        return json.dumps(response.model_dump(mode="json") | {"v": version})

    match ticket:
        case _ if ticket in POSITIVE:
            mode = Mode(POSITIVE[ticket])
            reason = Reason.MISSING if mode is Mode.CONTEXTUAL else Reason.BOUND
            return 200, answer(mode, reason, bidders), 0.025 if ticket == "late1" else 0
        case "SAFE1":
            return 200, answer(Mode.SAFE, Reason.CONSENT_UNAVAILABLE, {}), 0
        case "KIDS1":
            return 200, answer(Mode.KIDS, Reason.KIDS, {}), 0
        case "SAFEbad1":
            return 200, answer(Mode.SAFE, Reason.CONSENT_UNAVAILABLE, bidders), 0
        case "KIDSbad1":
            return 200, answer(Mode.KIDS, Reason.KIDS, bidders), 0
        case "slow1":
            return 200, answer(Mode.FULL, Reason.BOUND, bidders), 0.2
        case "err1":
            return 500, "{}", 0
        case "junk1":
            return 200, "not json", 0
        case "v2x1":
            return 200, answer(Mode.FULL, Reason.BOUND, bidders, version=2), 0
    return 200, answer(Mode.CONTEXTUAL, Reason.MISSING, bidders), 0


def select(request: SelectRequest) -> str:
    """The Pod Decision Service's own pod rules and trackers (:mod:`fast_channels.core.pds.pod`)."""
    return select_response(request, select_pod(request), tracker_base=TRACKER_BASE, enr=TRACKER_ENR).model_dump_json()


def own_book(request: dict[str, Any]) -> dict[str, Any]:
    """One house ad per slot, alternating two creatives."""
    bids = [
        {
            "id": f"house-{imp['id']}",
            "impid": imp["id"],
            "price": 0.01,
            "crid": f"house{n % 2 + 1}",
            "adomain": [f"house{n % 2 + 1}.fast.example"],
            "dur": 30,
            "adm": vast(f"house{n % 2 + 1}"),
            "ext": {"fast": {"house": True}},
        }
        for n, imp in enumerate(request.get("imp", []))
    ]
    return {"id": request.get("id", ""), "cur": "USD", "seatbid": [{"seat": "ownbook", "bid": bids}]}


@dataclass(frozen=True)
class Call:
    """One request a mock received."""

    at: float
    path: str
    header_names: tuple[str, ...]
    body: Any


@dataclass
class Mocks:
    """The mock servers; :meth:`start` binds them to free local ports."""

    checkout: Path
    """The applied guidance checkout (for the simulator's ``handler.py``)."""
    work: Path
    calls: list[Call] = field(default_factory=list)
    select_fails: bool = False
    contract_errors: list[str] = field(default_factory=list)
    """Service calls the contract refuses (path and the validation error)."""
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _servers: dict[str, ThreadingHTTPServer] = field(default_factory=dict)
    _simulator: ModuleType | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._servers['mocks'].server_address[1]}"

    @property
    def policy_url(self) -> str:
        return f"http://127.0.0.1:{self._policy_port}/policy"

    def start(self) -> None:
        self._simulator = self._load_simulator()
        self._servers["mocks"] = self._serve(0)
        self._policy_port = self._serve_policy(0)

    def stop(self) -> None:
        for server in self._servers.values():
            server.shutdown()
            server.server_close()

    @contextmanager
    def policy_unreachable(self) -> Iterator[None]:
        """Close the policy port while the block runs (connections are refused), then serve it again."""
        server = self._servers.pop("policy")
        server.shutdown()
        server.server_close()
        try:
            yield
        finally:
            self._serve_policy(self._policy_port)

    def third_party_calls(self, since: float) -> list[Call]:
        with self._lock:
            return [call for call in self.calls if call.at >= since and call.path in THIRD_PARTIES]

    def calls_to(self, path: str, since: float = 0) -> list[Call]:
        with self._lock:
            return [call for call in self.calls if call.at >= since and call.path == path]

    def _load_simulator(self) -> ModuleType:
        handler_dir = self.work / "simulator"
        handler_dir.mkdir(parents=True, exist_ok=True)
        source = self.checkout / "source/loadtest/bidder_simulator/lambdas/loadtest_bidder"
        for name in ("handler.py", "bid_response.json"):
            (handler_dir / name).write_bytes((source / name).read_bytes())
        (handler_dir / "video_creatives.json").write_text(json.dumps(simulator_creatives()))
        for name in (
            "BID_RESPONSES_DELAY_PERCENTAGE",
            "BID_RESPONSES_TIMEOUT_PERCENTAGE",
            "A_BID_RESPONSE_DELAY_PROBABILITY",
            "A_BID_RESPONSE_TIMEOUT_PROBABILITY",
        ):
            os.environ.setdefault(name, "0")
        spec = importlib.util.spec_from_file_location("fast_pbs_simulator", handler_dir / "handler.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _record(self, path: str, headers: dict[str, str], body: Any) -> None:
        with self._lock:
            self.calls.append(Call(time.time(), path, tuple(sorted(headers)), body))

    def _serve_policy(self, port: int) -> int:
        # One connection per call: once the port is closed, nothing keeps a pooled connection to it open.
        self._servers["policy"] = self._serve(port, keep_alive=False)
        return self._servers["policy"].server_address[1]

    def _serve(self, port: int, *, keep_alive: bool = True) -> ThreadingHTTPServer:
        mocks = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if keep_alive else "HTTP/1.0"

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0)).decode()
                headers = {name.lower(): value for name, value in self.headers.items()}
                path = self.path.split("?")[0]
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                mocks._record(path, headers, body)
                status, payload, delay = mocks._answer(path, body, raw, headers)
                time.sleep(delay)
                data = payload.encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                with suppress(BrokenPipeError):  # Prebid Server gave up on the call (its timeout), as some tests intend
                    self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _answer(self, path: str, body: Any, raw: str, headers: dict[str, str]) -> tuple[int, str, float]:
        match path:
            case "/policy":
                try:
                    policy = PolicyRequest.model_validate_json(raw)
                except ValidationError as error:
                    return self._refused(path, error)
                return policy_answer(policy)
            case "/select":
                try:
                    request = SelectRequest.model_validate_json(raw)
                except ValidationError as error:
                    return self._refused(path, error)
                return (503, "{}", 0) if self.select_fails else (200, select(request), 0)
            case "/ownbook/openrtb":
                return 200, json.dumps(own_book(body)), 0
            case "/amt":
                assert self._simulator is not None
                out = self._simulator.lambda_handler({"body": json.dumps(body), "headers": headers}, None)
                return int(out["statusCode"]), str(out["body"]), 0
            case "/echo26":
                return 200, json.dumps({"id": (body or {}).get("id", ""), "seatbid": []}), 0
        return 404, "{}", 0

    def _refused(self, path: str, error: ValidationError) -> tuple[int, str, float]:
        with self._lock:
            self.contract_errors.append(f"{path}: {error}")
        return 422, "{}", 0
