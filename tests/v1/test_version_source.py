from __future__ import annotations

import tomllib
import json
from pathlib import Path

import ecorex
from ecorex.runtime import RuntimeSettings


ROOT = Path(__file__).resolve().parents[2]


def test_product_version_has_one_python_source() -> None:
    assert ecorex.__version__ == "0.3.0"

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    assert "version" not in project
    assert project["dynamic"] == ["version"]
    assert project["requires-python"] == ">=3.11"
    assert config["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "ecorex.__version__"
    }
    assert "ecorex*" in config["tool"]["setuptools"]["packages"]["find"]["include"]

    web_package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    web_lock = json.loads((ROOT / "desktop" / "package-lock.json").read_text(encoding="utf-8"))
    assert web_package["version"] == ecorex.__version__
    assert web_lock["version"] == ecorex.__version__
    assert web_lock["packages"][""]["version"] == ecorex.__version__
    assert (ROOT / "cli" / "VERSION").read_text(encoding="utf-8").strip() == ecorex.__version__


def test_runtime_default_uses_the_product_version_source(tmp_path: Path) -> None:
    settings = RuntimeSettings(database_path=tmp_path / "runtime.db")
    assert settings.product_version == ecorex.__version__
