"""Every bundled workspace package carries the workspace packages it imports (the agent Runtime once shipped without
``fast_channels.core``): its locked closure, installed alone, imports every one of its modules.

The functions' code asset, the PDS sidecar's image and the agent's code zip each install one package's locked
closure (``fast_channels.infra.bundling.install_package``), not the workspace. A module that imports a workspace
package its ``pyproject.toml`` does not depend on passes every unit test, where the workspace is installed, and
fails only where it runs.
"""

import ast
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final

import pytest

from fast_channels.infra.bundling import REPO_ROOT

BUNDLED: Final = {"fast-channels-agent": "agent", "fast-channels-functions": "functions", "fast-channels-pds": "pds"}
"""The packages that are bundled on their own, and their workspace directory."""
WORKSPACE: Final = {"core": "core", "functions": "functions", "pds": "pds", "agent": "agent", "infra": "infra"}
"""``fast_channels.<name>`` by workspace directory."""
EXPORT: Final = ("export", "--frozen", "--no-dev", "--no-editable", "--no-hashes", "--quiet")


def uv() -> str:
    found = shutil.which("uv")
    if found is None:
        pytest.skip("needs uv")
    return found


def closure(package: str) -> set[str]:
    """The workspace directories in ``package``'s locked closure (the ``./<dir>`` lines of its export)."""
    exported = subprocess.run(
        [uv(), *EXPORT, "--package", package], check=True, capture_output=True, text=True, cwd=REPO_ROOT
    ).stdout
    return {line.strip().removeprefix("./") for line in exported.splitlines() if line.startswith("./")}


def workspace_packages(requirements: Path) -> list[str]:
    """The workspace packages (``./<dir>`` lines) of an export, by project name: uv reuses its cached build of a local
    package until its ``pyproject.toml`` changes, so each is rebuilt from its sources (as ``bundling`` does)."""
    directories = [line.strip() for line in requirements.read_text().splitlines() if line.startswith("./")]
    return [tomllib.loads((REPO_ROOT / d / "pyproject.toml").read_text())["project"]["name"] for d in directories]


def modules(directory: str) -> list[str]:
    """Every module of the workspace package in ``directory`` (``fast_channels.<name>...``)."""
    root = REPO_ROOT / directory / "src"
    return sorted(
        ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(".__init__")
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def imported_workspace(directory: str) -> set[str]:
    """The workspace directories whose ``fast_channels.<name>`` the package in ``directory`` imports."""
    found: set[str] = set()
    for path in (REPO_ROOT / directory / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            for name in names:
                parts = name.split(".")
                if len(parts) >= 2 and parts[0] == "fast_channels" and parts[1] in WORKSPACE:
                    found.add(WORKSPACE[parts[1]])
    return found


@pytest.mark.parametrize("package", sorted(BUNDLED))
def test_a_bundled_packages_closure_holds_every_workspace_package_its_code_imports(package: str) -> None:
    packages = closure(package)

    assert BUNDLED[package] in packages
    for directory in packages:
        missing = imported_workspace(directory) - packages
        assert not missing, (
            f"{directory} imports fast_channels.{', '.join(sorted(missing))}, which {package}'s closure lacks: add the "
            f"dependency (and its workspace source) to {directory}/pyproject.toml"
        )


def test_the_workspace_map_names_every_member() -> None:
    members = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"]["uv"]["workspace"]["members"]

    assert set(members) == set(WORKSPACE.values())


@pytest.mark.parametrize("package", sorted(BUNDLED))
def test_a_bundled_packages_closure_installed_alone_imports_every_module(package: str, tmp_path: Path) -> None:
    """The closure installed for this host into a directory of its own, then every module of the package imported
    by a Python that sees only that directory and the standard library (``-I -S``)."""
    requirements = tmp_path / "requirements.txt"
    subprocess.run([uv(), *EXPORT, "--package", package, "--output-file", str(requirements)], check=True, cwd=REPO_ROOT)
    target = tmp_path / "bundle"
    install = [uv(), "pip", "install", "--no-sources", "--quiet", "--python", sys.executable, "--target", str(target)]
    refresh = [arg for name in workspace_packages(requirements) for arg in ("--refresh-package", name)]
    subprocess.run([*install, *refresh, "--requirement", str(requirements)], check=True, cwd=REPO_ROOT)
    names = modules(BUNDLED[package])
    program = (
        f"import importlib, sys\nsys.path[:0] = [{str(target)!r}]\n"
        f"for name in {names!r}:\n    importlib.import_module(name)\n"
    )
    env = {"PATH": os.environ.get("PATH", ""), "AWS_DEFAULT_REGION": "us-east-1", "HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program], capture_output=True, text=True, env=env, cwd=tmp_path, check=False
    )

    assert result.returncode == 0, result.stderr[-2000:]
