from __future__ import annotations

import ast
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]
_LEGACY_PACKAGE_ROOTS = frozenset({"agent", "channel", "cli"})


def _legacy_imports(source: str) -> tuple[str, ...]:
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
            if root in _LEGACY_PACKAGE_ROOTS:
                imported.add(module)
    return tuple(sorted(imported))


def test_python_product_distribution_excludes_legacy_runtime_packages() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    discovery = project["tool"]["setuptools"]["packages"]["find"]
    assert discovery["include"] == ["ecorex*"]
    assert set(discovery["exclude"]) >= {
        "cli*",
        "agent*",
        "channel*",
        "desktop*",
        "tests*",
    }
    assert project["tool"]["setuptools"]["package-data"] == {
        "ecorex.control_plane.admin_web": ["static/*"]
    }

    dependencies = {
        dependency.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].casefold()
        for dependency in project["project"]["dependencies"]
    }
    assert "click" not in dependencies
    assert "requests" not in dependencies

    scripts = project["project"]["scripts"]
    assert scripts == {
        "ecorex": "ecorex.server.cli:main",
        "ecorex-product": "ecorex.server.cli:main",
        "ecorex-bootstrap": "ecorex.bootstrap.cli:main",
        "ecorex-release": "ecorex.control_plane.cli:main",
        "ecorex-control-plane": "ecorex.control_plane.production:main",
        "ecorex-gateway": "ecorex.gateway.production:main",
        "ecorex-gateway-schema": "ecorex.gateway.schema:main",
        "ecorex-image": "ecorex.image_orchestrator.production:main",
        "ecorex-share-schema": "ecorex.control_plane.share_schema:main",
    }


def test_v1_runtime_has_no_import_dependency_on_legacy_packages() -> None:
    violations: list[str] = []
    for path in sorted((ROOT / "ecorex").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        imports = _legacy_imports(source)
        if imports:
            violations.append(
                f"{path.relative_to(ROOT).as_posix()}: {', '.join(imports)}"
            )
    assert violations == []


def test_legacy_import_gate_uses_syntax_not_identifier_substrings() -> None:
    assert _legacy_imports("from .repository import client_request_hash\n") == ()
    assert _legacy_imports("value = 'from agent is documentation'\n") == ()
    assert _legacy_imports("from agent.tools import ToolManager\n") == (
        "agent.tools",
    )
