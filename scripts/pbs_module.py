#!/usr/bin/env -S uv run python
"""Build Prebid Server Java with the fast-ctv module exactly as the guidance's image does, and run the module's tests.

    uv run python scripts/pbs_module.py build      # build/pbs/<version>/prebid-server.jar (+ -stock.jar)
    uv run python scripts/pbs_module.py test       # the module's JUnit tests against that build

``build`` applies the guidance overlays (``prebid_guidance.apply``), checks Prebid Server Java out at the tag the
guidance's ``docker-build-config.json`` names, refusing it unless the tag resolves to the commit
``vendor/prebid-guidance/PIN.toml`` pins, and repeats the Dockerfile's build stage: the guidance's analytics reporter,
its ``amt`` adapter (whose copy script, overlay O4, also copies the module), then ``mvn clean package`` with the
guidance's Maven options. ``prebid-server-stock.jar`` is the same jar without the module's classes, for the no-hook
contract's "module absent" case. Needs Java 21 and Maven 3.9.11 (downloaded and checksum-verified into
``build/tools`` when ``mvn`` is not that version).
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


def run(argv: Sequence[str | Path], cwd: Path, *, quiet: bool = False) -> str:
    """Run a build tool; raise BuildError with its output's tail on failure."""
    result = subprocess.run(  # noqa: S603 - fixed argv built here, no shell
        [str(arg) for arg in argv], cwd=cwd, capture_output=True, text=True, check=False
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


def build_config(checkout: Path) -> dict[str, str]:
    """The guidance's ``docker-build-config.json``: the Prebid Server Java tag and the Maven options."""
    config: dict[str, str] = json.loads(
        (checkout / "deployment/ecr/prebid-server/docker-build-config.json").read_text()
    )
    return config


def prepare(pin: guidance.Pin, checkout: Path) -> tuple[Path, str]:
    """The Prebid Server Java tree with the guidance's extra modules and ours, as the Dockerfile's build stage
    prepares it; returns the tree and the Maven options."""
    config = build_config(checkout)
    version = config["GIT_TAG_VERSION"]
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
    return tree, config["MVN_CLI_OPTIONS"]


def strip_module(jar: Path, stock: Path) -> None:
    """``jar`` without the module's classes: Prebid Server as the guidance builds it without the module."""
    with zipfile.ZipFile(jar) as source, zipfile.ZipFile(stock, "w") as target:
        for item in source.infolist():
            if not item.filename.startswith(MODULE_CLASSES):
                target.writestr(item, source.read(item), compress_type=item.compress_type)


def build(repository: Path | None) -> Path:
    pin = guidance.Pin.load()
    checkout = guidance.DEFAULT_CHECKOUT
    guidance.apply(pin, checkout)
    tree, options = prepare(pin, checkout)
    mvn = maven()
    local = [f"-Dmaven.repo.local={repository}"] if repository else []
    run([mvn, "-B", "-q", *local, "clean", "package", *options.split()], tree)
    out = tree.parent
    jar = out / "prebid-server.jar"
    shutil.copy2(tree / "target" / "prebid-server.jar", jar)
    strip_module(jar, out / "prebid-server-stock.jar")
    print(f"{jar} and {out / 'prebid-server-stock.jar'}")
    return jar


def javac() -> list[str]:
    """``javac``, or the JDK's compiler module where only ``java`` is on PATH."""
    if found := shutil.which("javac"):
        return [found]
    return ["java", "-m", "jdk.compiler/com.sun.tools.javac.Main"]


def test(version: str, repository: Path | None) -> int:
    tree = BUILD / version / "prebid-server-java"
    if not (tree / "target" / "classes").is_dir():
        raise BuildError(f"no build of Prebid Server Java {version}: run `pbs_module.py build` first")
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
    )
    classes = tree.parent / "test-classes"
    shutil.rmtree(classes, ignore_errors=True)
    sources = sorted(str(path) for path in (MODULE / "src" / "test" / "java").rglob("*.java"))
    full = f"{tree / 'target' / 'classes'}{os.pathsep}{classpath}"
    run([*javac(), "-proc:none", "-nowarn", "-d", classes, "-cp", full, *sources], tree)
    resources = MODULE / "src" / "test" / "resources"
    if resources.is_dir():
        shutil.copytree(resources, classes, dirs_exist_ok=True)
    reports = tree.parent / "test-reports"
    result = subprocess.run(  # noqa: S603 - fixed argv built here, no shell
        [  # noqa: S607 - the JDK on PATH, as Maven uses it
            "java",
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
    )
    return result.returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("build", "test"))
    parser.add_argument("--maven-repository", type=Path, help="Maven local repository (default ~/.m2/repository)")
    args = parser.parse_args(argv)
    try:
        match args.command:
            case "build":
                build(args.maven_repository)
            case "test":
                version = build_config(guidance.DEFAULT_CHECKOUT)["GIT_TAG_VERSION"]
                return test(version, args.maven_repository)
    except (BuildError, guidance.GuidanceError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
