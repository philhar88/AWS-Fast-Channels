"""Offline tests of scripts/prebid_guidance.py (not marked e2e): the pin, checkout, verify and apply against a local
stand-in for the guidance repository, and render-config.

The tests that need the real, patched guidance (the overlays' own behaviour) run when ``prebid_guidance.py apply`` has
filled ``build/prebid-guidance/checkout``, as the CI job ``guidance-overlays`` does; otherwise they are skipped.
"""

import ast
import importlib.util
import json
import logging
import shutil
import subprocess
import sys
import warnings
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
UPSTREAM_FILE = "source/infrastructure/app.py"
RELIED_ON = "deployment/ecr/prebid-server/Dockerfile"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"fast_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guidance = _load("prebid_guidance")
CHECKOUT = guidance.DEFAULT_CHECKOUT
applied = pytest.mark.skipif(
    not (CHECKOUT / guidance.APPLIED_MARKER).is_file(),
    reason="needs `uv run python scripts/prebid_guidance.py apply` (network) to fill build/prebid-guidance/checkout",
)


def run(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> Callable[..., Any]:
    """A local repository standing in for the guidance: ``upstream(text)`` commits ``UPSTREAM_FILE`` with ``text``,
    tags the commit ``v9.9.9`` (moving the tag) and returns a pin of it whose one overlay appends a line."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    run("init", "--quiet", "--initial-branch", "main", cwd=repo)
    for key, value in (("user.email", "test@example.com"), ("user.name", "test"), ("commit.gpgsign", "false")):
        run("config", key, value, cwd=repo)
    vendor = tmp_path / "vendor"
    (vendor / "patches").mkdir(parents=True)
    (vendor / "extra.sh").write_text("echo extra\n")

    def make(text: str = "one\ntwo\n") -> Any:
        (repo / UPSTREAM_FILE).parent.mkdir(parents=True, exist_ok=True)
        (repo / UPSTREAM_FILE).write_text(text)
        (repo / RELIED_ON).parent.mkdir(parents=True, exist_ok=True)
        (repo / RELIED_ON).write_text("FROM scratch\n")
        run("add", ".", cwd=repo)
        run("commit", "--quiet", "--allow-empty", "-m", "release", cwd=repo)
        run("tag", "--force", "v9.9.9", cwd=repo)
        patch = vendor / "patches" / "O1-test.patch"
        patch.write_text(
            "O1: a test overlay\n\n"
            f"diff --git a/{UPSTREAM_FILE} b/{UPSTREAM_FILE}\n--- a/{UPSTREAM_FILE}\n+++ b/{UPSTREAM_FILE}\n"
            "@@ -1,2 +1,3 @@\n one\n two\n+three\n"
        )
        return guidance.Pin(
            repository=str(repo),
            tag="v9.9.9",
            commit=run("rev-parse", "HEAD", cwd=repo),
            license="MIT-0",
            overlays=(guidance.Overlay("O1", "test", patch, {"extra.sh": "source/extra.sh"}),),
            patched={UPSTREAM_FILE: guidance.sha256(repo / UPSTREAM_FILE)},
            relied_on={RELIED_ON: guidance.sha256(repo / RELIED_ON)},
            vendor=vendor,
        )

    return make


def test_the_shipped_pin_covers_every_file_its_overlays_patch() -> None:
    pin = guidance.Pin.load()

    assert (pin.tag, pin.commit) == ("v1.4.0", "0c7e7e714de37d1f56b29219d7ad9e6d17fac6b2")
    assert [overlay.id for overlay in pin.overlays] == ["O1", "O2", "O4", "O5", "O6", "O7"]
    assert pin.unpinned_patch_targets() == set()
    assert set(pin.patched) == {path for overlay in pin.overlays for path in overlay.patched_paths()}
    for overlay in pin.overlays:
        assert overlay.patch.is_file()
        assert all((pin.vendor / source).is_file() for source in overlay.files)
    assert all(len(digest) == 64 for digest in pin.hashes.values())


def test_the_pin_refuses_an_abbreviated_commit(tmp_path: Path) -> None:
    pin = tmp_path / "PIN.toml"
    pin.write_text(guidance.PIN_FILE.read_text().replace("0c7e7e714de37d1f56b29219d7ad9e6d17fac6b2", "0c7e7e7"))
    with pytest.raises(guidance.GuidanceError, match="40-character"):
        guidance.Pin.load(pin)


def test_changed_files_lists_missing_and_modified_pinned_files(tmp_path: Path, upstream: Callable[..., Any]) -> None:
    pin = upstream()
    (tmp_path / UPSTREAM_FILE).parent.mkdir(parents=True)
    (tmp_path / UPSTREAM_FILE).write_text("one\ntwo\n")

    assert guidance.changed_files(pin, tmp_path) == [RELIED_ON]
    (tmp_path / UPSTREAM_FILE).write_text("one\nTWO\n")
    assert guidance.changed_files(pin, tmp_path) == [RELIED_ON, UPSTREAM_FILE]


def test_apply_checks_out_verifies_and_applies(tmp_path: Path, upstream: Callable[..., Any]) -> None:
    pin = upstream()
    module = tmp_path / "module" / "java"
    module.mkdir(parents=True)
    (module / "Module.java").write_text("class Module {}\n")
    work = tmp_path / "checkout"

    assert guidance.apply(pin, work, module=module.parent) == ["O1"]
    assert (work / UPSTREAM_FILE).read_text() == "one\ntwo\nthree\n"
    assert (work / "source/extra.sh").read_text() == "echo extra\n"
    assert (work / guidance.MODULE_DESTINATION / "java/Module.java").is_file()
    assert json.loads((work / guidance.APPLIED_MARKER).read_text()) == {
        "tag": "v9.9.9",
        "commit": pin.commit,
        "overlays": ["O1"],
        "module": True,
    }
    # A second apply starts again from the pinned commit: the overlay is not applied twice.
    guidance.apply(pin, work, module=None)
    assert (work / UPSTREAM_FILE).read_text() == "one\ntwo\nthree\n"
    assert not (work / guidance.MODULE_DESTINATION).exists()


def test_verify_fails_when_a_pinned_upstream_file_changed(tmp_path: Path, upstream: Callable[..., Any]) -> None:
    pin = upstream()
    moved = upstream("one\ntwo\n# upstream change\n")  # a new release under the same tag: the commit moved too
    stale = replace(moved, patched={UPSTREAM_FILE: pin.patched[UPSTREAM_FILE]})
    work = tmp_path / "checkout"

    guidance.checkout(stale, work)
    with pytest.raises(guidance.GuidanceError, match=f"upstream file changed or missing: {UPSTREAM_FILE}"):
        guidance.verify(stale, work)


def test_checkout_fails_when_the_tag_moved(tmp_path: Path, upstream: Callable[..., Any]) -> None:
    pin = upstream()
    upstream("one\ntwo\nmoved\n")

    with pytest.raises(
        guidance.GuidanceError, match=f"tag v9.9.9 of .* is [0-9a-f]{{40}}, not the pinned {pin.commit}"
    ):
        guidance.checkout(pin, tmp_path / "checkout")


def test_verify_fails_when_a_patch_no_longer_applies(tmp_path: Path, upstream: Callable[..., Any]) -> None:
    pin = upstream()
    pin.overlays[0].patch.write_text(pin.overlays[0].patch.read_text().replace(" two\n", " TWO\n"))
    work = tmp_path / "checkout"

    guidance.checkout(pin, work)
    with pytest.raises(guidance.GuidanceError, match="O1 does not apply"):
        guidance.verify(pin, work)


def test_verify_fails_when_the_pin_misses_a_patched_file(tmp_path: Path, upstream: Callable[..., Any]) -> None:
    pin = replace(upstream(), patched={})
    work = tmp_path / "checkout"

    guidance.checkout(pin, work)
    with pytest.raises(guidance.GuidanceError, match=f"PIN.toml \\[patched\\] lacks {UPSTREAM_FILE}"):
        guidance.verify(pin, work)


def test_render_config_writes_the_stage_configuration(tmp_path: Path) -> None:
    out = tmp_path / "config"
    status = guidance.main(
        [
            "render-config",
            "--stage",
            "fastm",
            "--content-base-url",
            "https://d111111abcdef8.cloudfront.net/",
            "--out",
            str(out),
        ]
    )

    assert status == 0
    written = sorted(str(path.relative_to(out)) for path in out.rglob("*") if path.is_file())
    assert written == sorted(
        [
            "current-config/prebid-config.yaml",
            "stored_accounts/fastm.json",
            *(
                f"stored_requests/fast-pod-{kids}{seconds}s.json"
                for kids in ("", "kids-")
                for seconds in (30, 60, 90, 120)
            ),
        ]
    )
    config = (out / "current-config/prebid-config.yaml").read_text()
    assert '"http://127.0.0.1:8090/policy"' in config  # the PDS sidecar (ADR-0036)
    assert '"https://d111111abcdef8.cloudfront.net/ads/b.gif?d={sid}' in config


def test_pbs_settings_follow_the_stage(tmp_path: Path) -> None:
    config = guidance.stage_config("fastm", "fastx", "us-east-1")
    settings = guidance.pbs_settings(config, "https://media.example.com")

    assert (settings.prefix, settings.simulator, settings.server_instances) == ("fastx", True, 2)
    assert (settings.tracker_base_url, settings.publisher_domain) == ("https://media.example.com", "media.example.com")
    assert (settings.policy_timeout_ms, settings.policy_group_timeout_ms) == (50, 75)
    assert (settings.select_timeout_ms, settings.select_group_timeout_ms) == (200, 250)


def test_render_config_refuses_a_stage_without_demand(capsys: pytest.CaptureFixture[str]) -> None:
    assert guidance.main(["render-config", "--prefix", "fastx", "--content-base-url", "https://d1.cloudfront.net"]) == 1
    assert "modules.demand is off" in capsys.readouterr().err


@applied
def test_the_applied_checkout_is_the_pinned_guidance_with_every_overlay() -> None:
    marker = json.loads((CHECKOUT / guidance.APPLIED_MARKER).read_text())
    pin = guidance.Pin.load()

    assert (marker["commit"], marker["overlays"]) == (pin.commit, [overlay.id for overlay in pin.overlays])
    patched = [CHECKOUT / rel for rel in pin.patched if rel.endswith(".py")]
    subprocess.run([sys.executable, "-m", "py_compile", *map(str, patched)], check=True)
    assert set(guidance.changed_files(pin, CHECKOUT)) == set(pin.patched)  # every patched file changed, nothing else


@applied
def test_o6_the_jdk_is_a_build_argument_defaulting_to_the_guidances() -> None:
    dockerfile = (CHECKOUT / "deployment/ecr/prebid-server/Dockerfile").read_text()

    assert "ARG JDK=21" in dockerfile
    assert "amazon-corretto-${JDK} \\" in dockerfile
    assert "ENV JAVA_HOME=/usr/lib/jvm/java-${JDK}-amazon-corretto" in dockerfile
    assert "amazon-corretto-21" not in dockerfile


def test_o4_copies_the_module_and_the_ported_amt_configuration(tmp_path: Path) -> None:
    tree = tmp_path / "prebid-server-java"
    amt = tree / "amt-bidder"
    shutil.copytree(guidance.VENDOR / "amt-bidder", amt)
    module = amt / "fast-ctv/src/main/java/org/prebid/server/hooks/modules/fast/ctv/v1"
    module.mkdir(parents=True)
    (module / "FastCtvModule.java").write_text("final class FastCtvModule {}\n")
    configuration = tree / "src/main/java/org/prebid/server/spring/config/bidder/AmtConfiguration.java"
    configuration.parent.mkdir(parents=True)
    configuration.write_text("// the guidance's\n")

    subprocess.run(["bash", str(amt / "copy-fast-ctv.sh")], cwd=tree, check=True, capture_output=True)

    assert (tree / "src/main/java/org/prebid/server/hooks/modules/fast/ctv/v1/FastCtvModule.java").is_file()
    port = (guidance.VENDOR / "amt-bidder/pbs-4/AmtConfiguration.java").read_text()
    assert configuration.read_text() == port


def test_o4_copy_script_has_no_pipe_a_reader_can_close() -> None:
    """Under pipefail, ``producer | head`` fails when head exits first and the producer gets SIGPIPE (exit 141): an
    earlier 4.4.0 image build failed so on CodeBuild."""
    script = (guidance.VENDOR / "amt-bidder/copy-fast-ctv.sh").read_text()

    assert "set -euo pipefail" in script
    assert "| head" not in script.replace("`| head`", "")


@applied
def test_o1_prefixes_the_glue_database_the_only_name_unique_per_account() -> None:
    """Without the prefix, two stages' guidance stacks in one account and Region collide on the database name."""
    source = (CHECKOUT / "source/infrastructure/prebid_server/prebid_glue_constructs.py").read_text()

    assert 'stack_prefix = self.node.try_get_context("stackPrefix")' in source
    assert 'self.GLUE_DATABASE_NAME = f"{stack_prefix.lower()}-{self.GLUE_DATABASE_NAME}"' in source


def _declared_parameters(checkout: Path) -> set[str]:
    """Every CfnParameter id in the guidance's sources (both stacks)."""
    found: set[str] = set()
    for path in (checkout / "source").rglob("*.py"):
        if "cdk.out" in path.parts or "tests" in path.parts or path.name.startswith("test_"):
            continue
        with warnings.catch_warnings():  # upstream sources may hold invalid escapes (a SyntaxWarning)
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            name = getattr(getattr(node, "func", None), "id", getattr(getattr(node, "func", None), "attr", ""))
            if isinstance(node, ast.Call) and name == "CfnParameter":
                keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                ident = node.args[1] if len(node.args) > 1 else keywords["id"]
                assert isinstance(ident, ast.Constant), f"{path}: a computed parameter id"
                found.add(str(ident.value))
    return found


@applied
def test_every_guidance_parameter_is_passed_on_each_deploy_or_left_at_its_default() -> None:
    """Guidance deploys do not reuse previous parameter values, so a parameter the deploy does not pass is reset to
    its default: a guidance bump that adds one must classify it here."""
    demand = _load("_demand")

    assert _declared_parameters(CHECKOUT) == demand.GUIDANCE_PARAMETERS | set(demand.GUIDANCE_DEFAULTS)
    assert not demand.GUIDANCE_PARAMETERS & set(demand.GUIDANCE_DEFAULTS)


@applied
def test_o5_simulator_bids_with_the_configured_creatives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    handler_dir = CHECKOUT / "source/loadtest/bidder_simulator/lambdas/loadtest_bidder"
    for name in ("handler.py", "bid_response.json"):
        (tmp_path / name).write_bytes((handler_dir / name).read_bytes())
    creatives = [
        {
            "crid": f"sim{n}",
            "adomain": [f"brand-{n}.example"],
            "adm": f"<VAST version='4.2'><Ad id='sim{n}'><InLine/></Ad></VAST>",
            "price": 10.0 + n,
            "dur": 30,
            "w": 1920,
            "h": 1080,
            "burl": f"https://cdn.example/ads/b.gif?seat=amt&cr=sim{n}&p=${{AUCTION_PRICE}}",
        }
        for n in (1, 2)
    ]
    (tmp_path / "video_creatives.json").write_text(json.dumps(creatives))
    for name in (
        "BID_RESPONSES_DELAY_PERCENTAGE",
        "BID_RESPONSES_TIMEOUT_PERCENTAGE",
        "A_BID_RESPONSE_DELAY_PROBABILITY",
        "A_BID_RESPONSE_TIMEOUT_PROBABILITY",
    ):
        monkeypatch.setenv(name, "0")
    spec = importlib.util.spec_from_file_location("o5_handler", tmp_path / "handler.py")
    assert spec and spec.loader
    handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler)
    request = {"id": "r", "imp": [{"id": f"s{n}", "video": {"rqddurs": [30]}} for n in (1, 2)]}

    with caplog.at_level(logging.INFO):
        response = handler.lambda_handler({"body": json.dumps(request), "headers": {"X-Prebid": "pbs"}}, None)
    bids = json.loads(response["body"])["seatbid"][0]["bid"]

    assert [(bid["crid"], bid["adomain"], bid["dur"], bid["w"]) for bid in bids] == [
        ("sim1", ["brand-1.example"], 30, 1920),
        ("sim2", ["brand-2.example"], 30, 1920),
    ]
    assert bids[0]["burl"].endswith("cr=sim1&p=${AUCTION_PRICE}")
    assert "nurl" not in bids[0]
    logged = [json.loads(r.getMessage()) for r in caplog.records if "sim-bid-request" in r.getMessage()]
    assert logged == [{"evt": "sim-bid-request", "imps": 2, "header_names": ["x-prebid"]}]
