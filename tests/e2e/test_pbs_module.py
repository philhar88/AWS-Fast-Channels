"""Offline tests of scripts/pbs_module.py (not marked e2e): the stock jar, the pin check and the toolchain lookup.
Building Prebid Server Java and running the module's JUnit tests is the CI job ``pbs-module``."""

import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(f"fast_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pbs_module = _load("pbs_module")


def test_the_stock_jar_is_the_jar_without_the_module(tmp_path: Path) -> None:
    jar, stock = tmp_path / "prebid-server.jar", tmp_path / "prebid-server-stock.jar"
    with zipfile.ZipFile(jar, "w") as out:
        out.writestr("BOOT-INF/lib/netty.jar", b"nested", compress_type=zipfile.ZIP_STORED)
        out.writestr("BOOT-INF/classes/org/prebid/server/Application.class", b"app", compress_type=zipfile.ZIP_DEFLATED)
        out.writestr(f"{pbs_module.MODULE_CLASSES}ctv/v1/FastCtvModule.class", b"ours")
        out.writestr(f"{pbs_module.MODULE_CLASSES}ctv/core/PodRenderer.class", b"ours")

    pbs_module.strip_module(jar, stock)

    with zipfile.ZipFile(stock) as result:
        assert result.namelist() == ["BOOT-INF/lib/netty.jar", "BOOT-INF/classes/org/prebid/server/Application.class"]
        assert result.getinfo("BOOT-INF/lib/netty.jar").compress_type == zipfile.ZIP_STORED  # Spring Boot needs it
        assert result.read("BOOT-INF/lib/netty.jar") == b"nested"


def test_a_prebid_server_release_without_a_pinned_commit_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "deployment/ecr/prebid-server/docker-build-config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"GIT_TAG_VERSION": "9.9.9", "GIT_REPO_URL": "https://example.invalid", "MVN_CLI_OPTIONS": ""})
    )

    with pytest.raises(pbs_module.BuildError, match=r"no commit for Prebid Server Java 9\.9\.9"):
        pbs_module.prepare(pbs_module.guidance.Pin.load(), tmp_path)


def test_the_pinned_guidance_release_has_a_pinned_prebid_server_commit() -> None:
    pin = pbs_module.guidance.Pin.load()

    assert pin.pbs_java_commits == {"3.43.0": "ba7f97e33fe1dcc5e64f926bc8bea5d4d4252fc1"}
    assert pin.pbs_java_repository == "https://github.com/prebid/prebid-server-java"


def test_javac_falls_back_to_the_compiler_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pbs_module.shutil, "which", lambda name: None)
    assert pbs_module.javac() == ["java", "-m", "jdk.compiler/com.sun.tools.javac.Main"]
    monkeypatch.setattr(pbs_module.shutil, "which", lambda name: f"/opt/jdk/bin/{name}")
    assert pbs_module.javac() == ["/opt/jdk/bin/javac"]


def test_main_reports_build_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fail(repository: Path | None) -> Path:
        raise pbs_module.BuildError("no toolchain")

    monkeypatch.setattr(pbs_module, "build", fail)

    assert pbs_module.main(["build"]) == 1
    assert "error: no toolchain" in capsys.readouterr().err
