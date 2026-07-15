from __future__ import annotations

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]


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
    forbidden = (
        "import cli",
        "from cli",
        "import channel",
        "from channel",
        "import agent",
        "from agent",
    )
    violations: list[str] = []
    for path in sorted((ROOT / "ecorex").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if any(marker in source for marker in forbidden):
            violations.append(path.relative_to(ROOT).as_posix())
    assert violations == []
