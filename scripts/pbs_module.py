#!/usr/bin/env -S uv run python
"""Build Prebid Server Java with the fast-ctv module exactly as the guidance's image does, and run the module's tests.

    uv run python scripts/pbs_module.py build      # build/pbs/<version>/prebid-server.jar (+ -stock.jar)
    uv run python scripts/pbs_module.py test       # the module's JUnit tests against that build
    uv run python scripts/pbs_module.py build --pbs-version 4.4.0   # another release (PIN.toml [pbs_java.builds])

``build`` applies the guidance overlays (``prebid_guidance.apply``), checks Prebid Server Java out at the tag the
guidance's ``docker-build-config.json`` names, refusing it unless the tag resolves to the commit
``vendor/prebid-guidance/PIN.toml`` pins, and repeats the Dockerfile's build stage: the guidance's analytics reporter,
its ``amt`` adapter (whose copy script, overlay O4, also copies the module), then ``mvn clean package`` with the
guidance's Maven options. ``prebid-server-stock.jar`` is the same jar without the module's classes, for the no-hook
contract's "module absent" case. Needs Java 21 and Maven 3.9.11 (downloaded and checksum-verified into
``build/tools`` when ``mvn`` is not that version).

``--pbs-version`` builds a release the guidance does not, with the JDK and Maven options ``PIN.toml`` gives it
(``[pbs_java.builds]``); on a 4.x tree overlay O4 also ports the ``amt`` adapter's configuration. When the ``java``
on PATH is another major version, the pinned Corretto (``[jdks]``) is downloaded, checksum-verified, into
``build/tools``. ``build/pbs/<version>/JAVA_HOME`` records the JDK a build used, for the tests and ``tests/pbs``.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import prebid_guidance as guidance

from fast_channels.core.pbs_config import PBS_VERSION

REPO: Final = Path(__file__).resolve().parents[1]
BUILD: Final = REPO / "build" / "pbs"
TOOLS: Final = REPO / "build" / "tools"
MODULE: Final = REPO / "pbs" / "fast-ctv"
FIXTURES: Final = REPO / "contracts" / "fixtures" / "pds"
MODULE_PACKAGE: Final = "org.prebid.server.hooks.modules.fast.ctv"
MODULE_CLASSES: Final = "BOOT-INF/classes/org/prebid/server/hooks/modules/fast/"
MAVEN_VERSION: Final = "3.9.11"
MAVEN_URL: Final = (
    f"https://archive.apache.org/dist/maven/maven-3/{MAVEN_VERSION}/binaries/apache-maven-{MAVEN_VERSION}-bin.tar.gz"
)
MAVEN_SHA512: Final = (
    "bcfe4fe305c962ace56ac7b5fc7a08b87d5abd8b7e89027ab251069faebee516"
    "b0ded8961445d6d91ec1985dfe30f8153268843c89aa392733d1a3ec956c9978"
)
_JUNIT_PLATFORM: Final = re.compile(r"junit-platform-engine-(\d+\.\d+\.\d+)\.jar")


class BuildError(RuntimeError):
    """The toolchain, the pin or the build failed."""


def run(argv: Sequence[str | Path], cwd: Path, *, quiet: bool = False, env: dict[str, str] | None = None) -> str:
    """Run a build tool; raise BuildError with its output's tail on failure."""
    result = subprocess.run(  # noqa: S603 - fixed argv built here, no shell
        [str(arg) for arg in argv], cwd=cwd, capture_output=True, text=True, check=False, env=env
    )
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-40:])
        raise BuildError(f"{' '.join(map(str, argv[:3]))} … failed ({result.returncode}):\n{tail}")
    if not quiet and result.stdout.strip():
        print(result.stdout.strip()[-2000:])
    return result.stdout


def maven() -> Path:
    """``mvn`` at version 3.9.11: the one on PATH, else one downloaded (checksum-verified) into build/tools."""
    if (found := shutil.which("mvn")) and f"Apache Maven {MAVEN_VERSION}" in run([found, "-v"], REPO, quiet=True):
        return Path(found)
    home = TOOLS / f"apache-maven-{MAVEN_VERSION}"
    if not (home / "bin" / "mvn").is_file():
        TOOLS.mkdir(parents=True, exist_ok=True)
        archive = TOOLS / f"apache-maven-{MAVEN_VERSION}-bin.tar.gz"
        with urllib.request.urlopen(MAVEN_URL, timeout=120) as response:
            archive.write_bytes(response.read())
        if (digest := hashlib.sha512(archive.read_bytes()).hexdigest()) != MAVEN_SHA512:
            archive.unlink()
            raise BuildError(f"{MAVEN_URL}: SHA-512 {digest} is not the pinned {MAVEN_SHA512}")
        with tarfile.open(archive) as tar:
            tar.extractall(TOOLS, filter="data")
    return home / "bin" / "mvn"


def java_major(java: str = "java") -> str | None:
    """The major version of ``java``, or None when there is none."""
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, check=False)  # noqa: S603
    except FileNotFoundError:
        return None
    match = re.search(r'version "(\d+)', out.stderr + out.stdout)
    return match.group(1) if match else None


def jdk(pin: guidance.Pin, major: str) -> Path | None:
    """JAVA_HOME of a JDK ``major``: None for the ``java`` on PATH when it is that version, else the pinned Corretto
    (``PIN.toml [jdks]``), downloaded and checksum-verified into build/tools."""
    if java_major() == major:
        return None
    machine = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}[os.uname().machine]
    archive = pin.jdks.get(major, {}).get(f"linux-{machine}")
    if archive is None:
        raise BuildError(f"PIN.toml [jdks] has no JDK {major} for linux-{machine}, and java on PATH is not {major}")
    home = TOOLS / f"jdk-{major}"
    if not (home / "bin" / "java").is_file():
        TOOLS.mkdir(parents=True, exist_ok=True)
        target = TOOLS / f"jdk-{major}.tar.gz"
        if not archive.url.startswith("https://"):
            raise BuildError(f"PIN.toml [jdks]: {archive.url} is not an https URL")
        with urllib.request.urlopen(archive.url, timeout=300) as response:  # noqa: S310 - https only, checked above
            target.write_bytes(response.read())
        if (digest := hashlib.sha256(target.read_bytes()).hexdigest()) != archive.sha256:
            target.unlink()
            raise BuildError(f"{archive.url}: SHA-256 {digest} is not the pinned {archive.sha256}")
        with tarfile.open(target) as tar:
            top = tar.getnames()[0].split("/", 1)[0]
            tar.extractall(TOOLS, filter="data")
        target.unlink()
        (TOOLS / top).rename(home)
    return home


def java_environment(home: Path | None) -> dict[str, str]:
    """The environment that puts ``home``'s ``java`` first (the current one when ``home`` is None)."""
    if home is None:
        return dict(os.environ)
    return {**os.environ, "JAVA_HOME": str(home), "PATH": f"{home / 'bin'}{os.pathsep}{os.environ['PATH']}"}


def release(pin: guidance.Pin, version: str | None) -> tuple[str, str, str]:
    """(version, Maven options, JDK major) of ``version`` (default :data:`PBS_VERSION`, the only supported release)
    from ``[pbs_java.builds]``. The guidance's own docker-build-config.json names 3.43.0, which is not built."""
    version = version or PBS_VERSION
    if (spec := pin.pbs_java_builds.get(version)) is None:
        raise BuildError(f"PIN.toml [pbs_java.builds] has no build of Prebid Server Java {version}")
    return version, spec.mvn_options, spec.jdk


def build_config(checkout: Path) -> dict[str, str]:
    """The guidance's ``docker-build-config.json``: the Prebid Server Java tag and the Maven options."""
    config: dict[str, str] = json.loads(
        (checkout / "deployment/ecr/prebid-server/docker-build-config.json").read_text()
    )
    return config


def prepare(pin: guidance.Pin, checkout: Path, version: str) -> Path:
    """The Prebid Server Java ``version`` tree with the guidance's extra modules and ours, as the Dockerfile's build
    stage prepares it."""
    config = build_config(checkout)
    if (commit := pin.pbs_java_commits.get(version)) is None:
        raise BuildError(f"PIN.toml [pbs_java.commits] has no commit for Prebid Server Java {version}")
    tree = BUILD / version / "prebid-server-java"
    source = guidance.Pin(
        repository=config["GIT_REPO_URL"],
        tag=version,
        commit=commit,
        license="Apache-2.0",
        overlays=(),
        patched={},
        relied_on={},
    )
    guidance.checkout(source, tree)
    context = checkout / "deployment/ecr/prebid-server"
    reporter = context / "extra-modules/log-module-reporter"
    shutil.copytree(reporter / "psdoa", tree / "src/main/java/org/prebid/server/analytics/reporter/psdoa")
    for item in (reporter / "config").iterdir():
        target = tree / "src/main/java/org/prebid/server/spring/config" / item.name
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, target)
    shutil.copytree(checkout / "source/loadtest/amt-bidder", tree / "amt-bidder")
    run(["bash", "./amt-bidder/copy-bidder-files.sh"], tree, quiet=True)
    if not (tree / "src/main/java/org/prebid/server/hooks/modules/fast/ctv").is_dir():
        raise BuildError("overlay O4 did not copy the fast-ctv module into the Prebid Server tree")
    return tree


def strip_module(jar: Path, stock: Path) -> None:
    """``jar`` without the module's classes: Prebid Server as the guidance builds it without the module."""
    with zipfile.ZipFile(jar) as source, zipfile.ZipFile(stock, "w") as target:
        for item in source.infolist():
            if not item.filename.startswith(MODULE_CLASSES):
                target.writestr(item, source.read(item), compress_type=item.compress_type)


def build(repository: Path | None, pbs_version: str | None = None) -> Path:
    pin = guidance.Pin.load()
    checkout = guidance.DEFAULT_CHECKOUT
    guidance.apply(pin, checkout)
    version, options, major = release(pin, pbs_version)
    tree = prepare(pin, checkout, version)
    home = jdk(pin, major)
    mvn = maven()
    local = [f"-Dmaven.repo.local={repository}"] if repository else []
    run([mvn, "-B", "-q", *local, "clean", "package", *options.split()], tree, env=java_environment(home))
    out = tree.parent
    (out / "JAVA_HOME").write_text(f"{home or ''}\n")
    jar = out / "prebid-server.jar"
    shutil.copy2(tree / "target" / "prebid-server.jar", jar)
    strip_module(jar, out / "prebid-server-stock.jar")
    print(f"{jar} and {out / 'prebid-server-stock.jar'}")
    return jar


def javac(home: Path | None = None) -> list[str]:
    """``javac`` (of ``home`` when given), or the JDK's compiler module where only ``java`` is on PATH."""
    if home is not None:
        return [str(home / "bin" / "javac")]
    if found := shutil.which("javac"):
        return [found]
    return ["java", "-m", "jdk.compiler/com.sun.tools.javac.Main"]


def build_java_home(version: str) -> Path | None:
    """The JDK a build of ``version`` used (``build/pbs/<version>/JAVA_HOME``; None: the ``java`` on PATH)."""
    recorded = BUILD / version / "JAVA_HOME"
    value = recorded.read_text().strip() if recorded.is_file() else ""
    return Path(value) if value else None


def test(version: str, repository: Path | None) -> int:
    tree = BUILD / version / "prebid-server-java"
    if not (tree / "target" / "classes").is_dir():
        raise BuildError(f"no build of Prebid Server Java {version}: run `pbs_module.py build` first")
    home = build_java_home(version)
    env = java_environment(home)
    mvn = maven()
    local = [f"-Dmaven.repo.local={repository}"] if repository else []
    classpath_file = tree.parent / "test-classpath.txt"
    run(
        [
            mvn,
            "-B",
            "-q",
            *local,
            "dependency:build-classpath",
            "-Dmdep.includeScope=test",
            f"-Dmdep.outputFile={classpath_file}",
        ],
        tree,
        env=env,
    )
    classpath = classpath_file.read_text().strip()
    if not (match := _JUNIT_PLATFORM.search(classpath)):
        raise BuildError("the test classpath has no JUnit Platform")
    console = f"org.junit.platform:junit-platform-console-standalone:{match.group(1)}"
    run(
        [
            mvn,
            "-B",
            "-q",
            *local,
            "dependency:copy",
            f"-Dartifact={console}",
            f"-DoutputDirectory={tree.parent}",
            "-Dmdep.stripVersion=true",
        ],
        tree,
        env=env,
    )
    classes = tree.parent / "test-classes"
    shutil.rmtree(classes, ignore_errors=True)
    sources = sorted(str(path) for path in (MODULE / "src" / "test" / "java").rglob("*.java"))
    full = f"{tree / 'target' / 'classes'}{os.pathsep}{classpath}"
    run([*javac(home), "-proc:none", "-nowarn", "-d", classes, "-cp", full, *sources], tree, env=env)
    resources = MODULE / "src" / "test" / "resources"
    if resources.is_dir():
        shutil.copytree(resources, classes, dirs_exist_ok=True)
    reports = tree.parent / "test-reports"
    result = subprocess.run(  # noqa: S603 - fixed argv built here, no shell
        [
            str(home / "bin" / "java") if home else "java",
            f"-Dfast.ctv.fixtures={FIXTURES}",
            "-jar",
            str(tree.parent / "junit-platform-console-standalone.jar"),
            "execute",
            "--class-path",
            f"{classes}{os.pathsep}{full}",
            "--select-package",
            MODULE_PACKAGE,
            "--reports-dir",
            str(reports),
            "--details=tree",
            "--disable-banner",
            "--fail-if-no-tests",
        ],
        cwd=tree,
        check=False,
        env=env,
    )
    return result.returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("build", "test"))
    parser.add_argument("--maven-repository", type=Path, help="Maven local repository (default ~/.m2/repository)")
    parser.add_argument("--pbs-version", help=f"Prebid Server Java release (default {PBS_VERSION})")
    args = parser.parse_args(argv)
    try:
        match args.command:
            case "build":
                build(args.maven_repository, args.pbs_version)
            case "test":
                version = args.pbs_version or PBS_VERSION
                return test(version, args.maven_repository)
    except (BuildError, guidance.GuidanceError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
