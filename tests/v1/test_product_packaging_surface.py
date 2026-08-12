from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import tomllib

from ecorex.pack_catalog import COW_RUNTIME_SOURCE_ROOTS


ROOT = Path(__file__).resolve().parents[2]
_COW_PACKAGE_ROOTS = frozenset(COW_RUNTIME_SOURCE_ROOTS)


def _cow_imports(source: str) -> tuple[str, ...]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            candidates = (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            candidates = (node.module,)
        else:
            continue
        for module in candidates:
            root = module.partition(".")[0]
            if root in _COW_PACKAGE_ROOTS:
                imported.add(module)
    return tuple(sorted(imported))


def test_python_product_distribution_contains_the_cow_runtime() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    discovery = project["tool"]["setuptools"]["packages"]["find"]
    assert {f"{root}*" for root in set(COW_RUNTIME_SOURCE_ROOTS) - {"skills"}} <= set(
        discovery["include"]
    )
    assert "ecorex*" in discovery["include"]
    assert set(discovery["exclude"]) == {"desktop*", "tests*"}
    assert project["tool"]["setuptools"]["py-modules"] == ["config"]
    assert project["tool"]["setuptools"]["package-data"] == {
        "ecorex.control_plane.admin_web": ["static/*"],
        "ecorex.control_plane": ["seed_skills/official-writing/*"],
    }

    dependencies = {
        dependency.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].casefold()
        for dependency in project["project"]["dependencies"]
    }
    assert "click" not in dependencies
    assert "requests" in dependencies

    scripts = project["project"]["scripts"]
    assert {
        "ecorex": "ecorex.server.cli:main",
        "ecorex-product": "ecorex.server.cli:main",
        "ecorex-bootstrap": "ecorex.bootstrap.cli:main",
        "ecorex-release": "ecorex.control_plane.cli:main",
        "ecorex-control-plane": "ecorex.control_plane.production:main",
        "ecorex-gateway": "ecorex.gateway.production:main",
        "ecorex-gateway-schema": "ecorex.gateway.schema:main",
        "ecorex-image": "ecorex.image_orchestrator.production:main",
        "ecorex-share-schema": "ecorex.control_plane.share_schema:main",
    }.items() <= scripts.items()
    assert scripts["emate"] == scripts["ecorex"]
    assert scripts["emate-backend"] == scripts["ecorex-product"]


def test_v1_runtime_imports_the_packaged_cow_data_plane() -> None:
    imports: set[str] = set()
    for path in sorted((ROOT / "ecorex").rglob("*.py")):
        imports.update(_cow_imports(path.read_text(encoding="utf-8")))

    assert {module.partition(".")[0] for module in imports} == {
        "agent",
        "bridge",
        "channel",
        "common",
    }
    assert {
        "agent.tools",
        "bridge.agent_initializer",
        "channel.channel_manager",
        "common.ecorex_tool_permissions",
    } <= imports


def test_cow_import_inventory_uses_syntax_not_identifier_substrings() -> None:
    assert _cow_imports("from .repository import client_request_hash\n") == ()
    assert _cow_imports("value = 'from agent is documentation'\n") == ()
    assert _cow_imports("from agent.tools import ToolManager\n") == (
        "agent.tools",
    )


def test_cow_import_does_not_write_without_a_desktop_data_root(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment.pop("EMATE_DATA_DIR", None)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); import config",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert list(tmp_path.iterdir()) == []
