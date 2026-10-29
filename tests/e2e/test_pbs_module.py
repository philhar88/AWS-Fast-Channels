"""Offline tests of scripts/pbs_module.py (not marked e2e): the stock jar, the pin check, the releases and the
toolchain lookup. Building Prebid Server Java and running the module's JUnit tests is the CI job ``pbs-module``, once
per release."""

import dataclasses
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

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
        pbs_module.prepare(pbs_module.guidance.Pin.load(), tmp_path, "9.9.9")


def test_every_selectable_release_has_a_pinned_commit_and_a_build() -> None:
    pin = pbs_module.guidance.Pin.load()

    assert pin.pbs_java_commits == {
        "3.43.0": "ba7f97e33fe1dcc5e64f926bc8bea5d4d4252fc1",
        "4.4.0": "de50af1076d9cf8375c4af4fa1adcfff96db2c7c",
    }
    assert pin.pbs_java_repository == "https://github.com/prebid/prebid-server-java"
    assert pin.pbs_java_builds == {
        "4.4.0": pbs_module.guidance.PbsBuild(jdk="25", mvn_options="-Dmaven.test.skip -Dcheckstyle.skip")
    }
    assert set(pin.jdks["25"]) == {"linux-x86_64", "linux-aarch64"}
    assert all(archive.url.startswith("https://corretto.aws/") for archive in pin.jdks["25"].values())


def _guidance_build(checkout: Path) -> None:
    config = checkout / "deployment/ecr/prebid-server/docker-build-config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"GIT_TAG_VERSION": "3.43.0", "GIT_REPO_URL": "https://example.invalid", "MVN_CLI_OPTIONS": "-q"})
    )


def test_a_release_is_the_guidances_build_or_its_pinned_build(tmp_path: Path) -> None:
    _guidance_build(tmp_path)
    pin = pbs_module.guidance.Pin.load()

    assert pbs_module.release(pin, tmp_path, None) == ("3.43.0", "-q", "21")
    assert pbs_module.release(pin, tmp_path, "3.43.0") == ("3.43.0", "-q", "21")
    assert pbs_module.release(pin, tmp_path, "4.4.0") == ("4.4.0", "-Dmaven.test.skip -Dcheckstyle.skip", "25")
    with pytest.raises(pbs_module.BuildError, match=r"no build of Prebid Server Java 9\.9\.9"):
        pbs_module.release(pin, tmp_path, "9.9.9")


def test_the_java_on_path_is_used_when_it_is_the_major_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pbs_module, "java_major", lambda java="java": "25")

    assert pbs_module.jdk(pbs_module.guidance.Pin.load(), "25") is None
    assert pbs_module.java_environment(None)["PATH"] == pbs_module.os.environ["PATH"]


def _corretto(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, sha256: str | None = None) -> Any:
    """A pin whose JDK 25 is a small archive served by a stubbed urlopen, with build/tools under tmp_path."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        java = tarfile.TarInfo("amazon-corretto-25.0.5.10.1-linux-x64/bin/java")
        java.size, java.mode = 2, 0o755
        tar.addfile(java, io.BytesIO(b"#!"))
    archive = buffer.getvalue()
    machine = {"aarch64": "aarch64", "arm64": "aarch64"}.get(pbs_module.os.uname().machine, "x86_64")
    url = "https://corretto.aws/downloads/resources/25.0.5.10.1/test.tar.gz"
    pin = pbs_module.guidance.Pin.load()
    jdks = {"25": {f"linux-{machine}": pbs_module.guidance.Jdk(url, sha256 or hashlib.sha256(archive).hexdigest())}}
    monkeypatch.setattr(pbs_module, "java_major", lambda java="java": "21")
    monkeypatch.setattr(pbs_module, "TOOLS", tmp_path / "tools")
    monkeypatch.setattr(pbs_module.urllib.request, "urlopen", lambda requested, timeout: io.BytesIO(archive))
    return dataclasses.replace(pin, jdks=jdks)


def test_another_major_is_the_pinned_corretto_checksum_verified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pin = _corretto(monkeypatch, tmp_path)

    home = pbs_module.jdk(pin, "25")

    assert home == tmp_path / "tools" / "jdk-25"
    assert (home / "bin" / "java").read_bytes() == b"#!"
    assert not list((tmp_path / "tools").glob("*.tar.gz")), "the archive is removed once extracted"
    environment = pbs_module.java_environment(home)
    assert environment["JAVA_HOME"] == str(home)
    assert environment["PATH"].startswith(f"{home / 'bin'}{pbs_module.os.pathsep}")
    assert pbs_module.javac(home) == [str(home / "bin" / "javac")]


def test_a_corretto_archive_that_does_not_match_its_checksum_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pin = _corretto(monkeypatch, tmp_path, sha256="0" * 64)

    with pytest.raises(pbs_module.BuildError, match="is not the pinned"):
        pbs_module.jdk(pin, "25")
    assert not (tmp_path / "tools" / "jdk-25").exists()
    assert not list((tmp_path / "tools").glob("*.tar.gz"))


def test_a_build_records_its_jdk_for_the_tests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pbs_module, "BUILD", tmp_path)
    (tmp_path / "4.4.0").mkdir()
    assert pbs_module.build_java_home("4.4.0") is None
    (tmp_path / "4.4.0" / "JAVA_HOME").write_text("/opt/jdk-25\n")
    assert pbs_module.build_java_home("4.4.0") == Path("/opt/jdk-25")
    (tmp_path / "4.4.0" / "JAVA_HOME").write_text("\n")  # the java on PATH
    assert pbs_module.build_java_home("4.4.0") is None


def test_javac_falls_back_to_the_compiler_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pbs_module.shutil, "which", lambda name: None)
    assert pbs_module.javac() == ["java", "-m", "jdk.compiler/com.sun.tools.javac.Main"]
    monkeypatch.setattr(pbs_module.shutil, "which", lambda name: f"/opt/jdk/bin/{name}")
    assert pbs_module.javac() == ["/opt/jdk/bin/javac"]


def test_main_reports_build_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fail(repository: Path | None, pbs_version: str | None = None) -> Path:
        raise pbs_module.BuildError("no toolchain")

    monkeypatch.setattr(pbs_module, "build", fail)

    assert pbs_module.main(["build"]) == 1
    assert "error: no toolchain" in capsys.readouterr().err
