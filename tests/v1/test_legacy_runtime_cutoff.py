from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def _cutoff_module():
    spec = importlib.util.spec_from_file_location(
        "ecorex_v1_legacy_cutoff_gate",
        ROOT / "scripts" / "check-v1-legacy-cutoff.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_static_legacy_cutoff_gate_passes() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check-v1-legacy-cutoff.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "EcoreX v1 legacy cutoff gate passed"
    assert result.stderr == ""


def test_strict_release_cutoff_passes_without_a_legacy_packager() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/check-v1-legacy-cutoff.py",
            "--strict-production",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "EcoreX v1 legacy cutoff gate passed"
    assert result.stderr == ""


def test_retired_tree_gate_rejects_pyc_and_only_exempts_dedicated_history_docs(
    tmp_path: Path,
) -> None:
    gate = _cutoff_module()
    retired = tmp_path / "channel" / "web"
    cache = retired / "__pycache__" / "web_channel.cpython-311.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"cached executable")
    history = retired / "docs" / "v0.3-history.md"
    history.parent.mkdir(parents=True)
    history.write_text("historical note", encoding="utf-8")

    assert gate._source_files(retired) == (cache,)


def test_retired_webchannel_tree_has_no_file_residue() -> None:
    gate = _cutoff_module()
    assert gate._source_files(ROOT / "channel" / "web") == ()


def test_inherited_cowagent_publish_workflows_are_permanently_retired() -> None:
    gate = _cutoff_module()
    retired = {
        ".github/workflows/deploy-image.yml",
        ".github/workflows/deploy-image-arm.yml",
    }

    assert retired.issubset(set(gate.RETIRED_FILES))
    assert all(not (ROOT / relative).exists() for relative in retired)


def test_webchannel_module_is_no_longer_importable() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util; "
                "\ntry:\n"
                " spec = importlib.util.find_spec('channel.web.web_channel')\n"
                "except ModuleNotFoundError:\n"
                " spec = None\n"
                "raise SystemExit(0 if spec is None else 1)"
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_python_entrypoint_is_a_non_starting_tombstone() -> None:
    result = subprocess.run(
        [sys.executable, "app.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 78
    assert result.stdout == ""
    assert "v0.3 app.py entrypoint is retired" in result.stderr


def test_migration_boundary_does_not_load_legacy_runtime_modules() -> None:
    forbidden = {
        "agent",
        "bridge",
        "channel",
        "cli",
        "common",
        "models",
        "plugins",
        "tools",
        "translate",
        "voice",
    }
    before = set(sys.modules)
    spec = importlib.util.find_spec("ecorex.migration")
    assert spec is not None
    __import__("ecorex.migration")
    loaded = {
        name.split(".", 1)[0]
        for name in set(sys.modules) - before
        if name.split(".", 1)[0] in forbidden
    }
    assert loaded == set()
