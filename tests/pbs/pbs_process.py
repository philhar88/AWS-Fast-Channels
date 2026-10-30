"""A local Prebid Server for the contract tests: the jar from ``scripts/pbs_module.py build``, run with the stage's
rendered configuration (``fast_channels.core.pbs_config``) pointed at :mod:`local_mocks`.

``FAST_PBS_VERSION`` picks the release (default ``core.pbs_config.PBS_VERSION``, the only supported one):
``build/pbs/<version>/``, run with the JDK its build recorded (``JAVA_HOME`` there)."""

import copy
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx

from fast_channels.core.pbs_config import PBS_VERSION as SUPPORTED_PBS_VERSION
from fast_channels.core.pbs_config import PbsSettings, account, house_ads, prebid_config, render, to_yaml
from fast_channels.core.pds.book import Creative

if TYPE_CHECKING:
    from local_mocks import Mocks

REPO: Final = Path(__file__).resolve().parents[2]
BUILD: Final = REPO / "build" / "pbs"
CHECKOUT: Final = REPO / "build" / "prebid-guidance" / "checkout"
PREFIX: Final = "fastpbs"
PBS_VERSION: Final = os.environ.get("FAST_PBS_VERSION", SUPPORTED_PBS_VERSION)
SETTINGS: Final = PbsSettings(
    prefix=PREFIX,
    simulator=True,
    server_instances=2,
    tracker_base_url="https://cdn.example",
    publisher_domain="cdn.example",
    house_ads=house_ads(  # the kit's, as deploy-demand renders them
        Creative(
            crid=f"kit{n}",
            creative_id=f"kit{n}",
            hls_uri=f"https://cdn.example/ads/kit{n}/main.m3u8",
            duration_ms=30_030,
        )
        for n in (1, 2)
    ),
)
ACCOUNTS: Final = {
    "shipped": PREFIX,
    "module-off": f"{PREFIX}-off",
    "absent-hooks": f"{PREFIX}-absent",
    "group-cut": f"{PREFIX}-groupcut",
}
"""The stage's account as rendered, and test-only variants of it (never shipped)."""


WARM_UP_AUCTIONS: Final = 20


class MissingPrerequisiteError(RuntimeError):
    """What the contract tests need is not there (the jar, Java, the applied guidance checkout)."""


def find_jar(name: str) -> Path:
    jar = BUILD / PBS_VERSION / name
    if not jar.is_file():
        raise MissingPrerequisiteError(
            f"no {name}: run `uv run python scripts/pbs_module.py build --pbs-version {PBS_VERSION}`"
        )
    if java() == "java" and shutil.which("java") is None:
        raise MissingPrerequisiteError(f"no java on PATH for Prebid Server Java {PBS_VERSION}")
    return jar


def java() -> str:
    """The ``java`` of the JDK the build used (``build/pbs/<version>/JAVA_HOME``), else the one on PATH."""
    recorded = BUILD / PBS_VERSION / "JAVA_HOME"
    home = recorded.read_text().strip() if recorded.is_file() else ""
    return str(Path(home) / "bin" / "java") if home else "java"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def contract_accounts() -> list[dict[str, Any]]:
    """The shipped account and its test-only variants: module execution off, absent hook ids, a policy group
    timeout (5 ms) far below the module's own 50 ms HTTP timeout."""
    shipped = account(SETTINGS)
    off = copy.deepcopy(shipped) | {"id": ACCOUNTS["module-off"]}
    off["hooks"]["admin"] = {"module-execution": {"fast-ctv": False}}
    renamed = json.dumps(shipped).replace('"fast-ctv-policy"', '"fast-ctv-policy-absent"')
    absent = json.loads(renamed.replace('"fast-ctv-vast"', '"fast-ctv-vast-absent"')) | {"id": ACCOUNTS["absent-hooks"]}
    cut = copy.deepcopy(shipped) | {"id": ACCOUNTS["group-cut"]}
    stages = cut["hooks"]["execution-plan"]["endpoints"]["/openrtb2/auction"]["stages"]
    stages["processed-auction-request"]["groups"][0]["timeout"] = 5
    return [shipped, off, absent, cut]


def local_config(mocks: Mocks, out: Path, *, port: int, module: bool, analytics: bool = False) -> Path:
    """The stage's rendered configuration with what a local run needs instead of the guidance's environment: plain
    HTTP, file-system settings, the mocks' endpoints and a test-only OpenRTB 2.6 bidder (``echo26``). ``analytics``
    turns the guidance's log analytics reporter on (stages deploy it off)."""
    for rel, content in render(SETTINGS).items():
        if rel.startswith("stored_requests/"):
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_text(content)
    for name in ("stored_imps", "stored_responses", "categories"):
        (out / name).mkdir(exist_ok=True)
    (out / "settings.yaml").write_text(to_yaml({"accounts": contract_accounts()}))
    config = prebid_config(SETTINGS)
    config["server"] = {"http": {"enabled": True, "port": port, "server-instances": 2}, "ssl": False}
    config["admin"] = {"port": free_port()}
    adapters = config["adapters"]
    adapters["amt"]["endpoint"] = f"{mocks.base_url}/amt"
    aliases = adapters["generic"]["aliases"]
    aliases["ownbook"]["endpoint"] = f"{mocks.base_url}/ownbook/openrtb"
    aliases["echo26"] = {"enabled": True, "endpoint": f"{mocks.base_url}/echo26", "ortb-version": "ORTB_2_6"}
    aliases["echo26"]["meta-info"] = {"vendor-id": 0}  # every configured generic alias needs a vendor id
    config["hooks"]["fast-ctv"] |= {
        "enabled": module,
        "policy-url": mocks.policy_url,
        "select-url": f"{mocks.base_url}/select",
    }
    config["cache"] = {"scheme": "http", "host": "127.0.0.1:1", "path": "/cache", "query": "uuid="}
    config["settings"] = {
        "enforce-valid-account": False,
        "generate-storedrequest-bidrequest-id": True,
        "filesystem": {
            "settings-filename": str(out / "settings.yaml"),
            "stored-requests-dir": str(out / "stored_requests"),
            "stored-imps-dir": str(out / "stored_imps"),
            "stored-responses-dir": str(out / "stored_responses"),
            "categories-dir": str(out / "categories"),
        },
    }
    config["gdpr"]["vendorlist"] = {
        "v2": {"cache-dir": str(out / "vendor2")},
        "v3": {"cache-dir": str(out / "vendor3")},
        "live": {"startup-cache-dir": str(out / "vendor-live")},
    }
    config["analytics"]["psdoa"] = {"enabled": analytics}
    path = out / "local-config.yaml"
    path.write_text(to_yaml(config))
    return path


@dataclass
class Pbs:
    """A running Prebid Server."""

    base_url: str
    log: Path

    def auction(
        self,
        ticket: str = "",
        *,
        seconds: int = 120,
        account: str | None = None,
        headers: dict[str, str] | None = None,
        query: str | None = None,
    ) -> httpx.Response:
        """MediaTailor's POST: the body names only the stored request; the session, the ticket and the playback
        configuration are headers, the break is the query (``query`` replaces it)."""
        body: dict[str, Any] = {"ext": {"prebid": {"storedrequest": {"id": f"fast-pod-{seconds}s"}}}}
        if account:
            body["app"] = {"publisher": {"id": account}}
        sent = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (SMART-TV; Linux; Tizen 7.0) contract-test",
            "X-Forwarded-For": "198.51.100.7, 203.0.113.20",
            "X-Fast-Origin": "contract-origin-secret",
            "X-Fast-T": ticket,
            "X-Fast-Sid": "sid-contract",
            "X-Fast-Pc": f"{PREFIX}-demo-p-a",
            "X-Fast-Fv": "r0001",
            **(headers or {}),
        }
        if query is None:
            query = f"evid=820461826&upid=tears-of-steel&dur={seconds * 1001}"
        return httpx.post(f"{self.base_url}/openrtb2/auction?{query}", json=body, headers=sent, timeout=10)


def start(jar: Path, config: Path, port: int, mocks: Mocks) -> Iterator[Pbs]:
    """Run ``jar`` with ``config`` until the generator is closed, once ``/status`` answers."""
    log = config.parent / "pbs.log"
    environment = {**os.environ, "AMT_BIDDING_SERVER_SIMULATOR_ENDPOINT": f"{mocks.base_url}/amt"}
    with log.open("w") as out:
        process = subprocess.Popen(
            [java(), "-jar", str(jar), f"--spring.config.additional-location={config}"],
            stdout=out,
            stderr=subprocess.STDOUT,
            env=environment,
        )
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 180
        try:
            while not _ready(base):
                if process.poll() is not None:
                    raise RuntimeError(f"Prebid Server exited ({process.returncode}); see {log}")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Prebid Server did not start in 180 s; see {log}")
                time.sleep(1)
            server = Pbs(base, log)
            for _ in range(WARM_UP_AUCTIONS):  # the first auctions of a fresh JVM miss the 50 ms policy timeout
                server.auction("CTX1")
            yield server
        finally:
            process.terminate()
            process.wait(timeout=30)


def _ready(base: str) -> bool:
    try:
        return httpx.get(f"{base}/status", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False
