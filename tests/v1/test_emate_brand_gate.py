from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check-emate-brand.py"


def _gate_module():
    spec = importlib.util.spec_from_file_location("emate_brand_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_gate_rejects_source_path_binary_and_utf16_brand_leaks(tmp_path: Path) -> None:
    gate = _gate_module()
    (tmp_path / "safe.py").write_text("PRODUCT = 'e-Mate'\n", encoding="utf-8")
    (tmp_path / "old-CowAgent-logo.svg").write_text("<svg/>\n", encoding="utf-8")
    (tmp_path / "bundle.js").write_text("const old = 'Cow Agent';\n", encoding="utf-8")
    (tmp_path / "helper.bin").write_bytes(b"prefix\0" + "CowAgent".encode("utf-16le"))

    violations = gate.check((tmp_path,))

    assert {(item.path, item.location) for item in violations} == {
        ("old-CowAgent-logo.svg", "path"),
        ("bundle.js", "line:1"),
        ("helper.bin", "line:1"),
    }


def test_gate_exempts_only_migration_notice_tests_and_ignores_non_product_trees(
    tmp_path: Path,
) -> None:
    gate = _gate_module()
    for relative in (
        "migration/compatibility.py",
        "NOTICE.txt",
        "tests/fixtures/legacy.txt",
        "docs/history.md",
        "node_modules/package/index.js",
        ".git/logs/HEAD",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("CowAgent https://skills.cowagent.ai Cow\n", encoding="utf-8")
    (tmp_path / "product/main.py").parent.mkdir()
    (tmp_path / "product/main.py").write_text("name = 'e-Mate'\n", encoding="utf-8")

    assert gate.check((tmp_path,)) == []


def test_gate_fails_closed_for_a_symlinked_product_entry(tmp_path: Path) -> None:
    gate = _gate_module()
    outside = tmp_path / "outside"
    outside.write_text("e-Mate", encoding="utf-8")
    link = tmp_path / "product-link"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("this account cannot create symbolic links")

    violations = gate.check((tmp_path,))
    assert [(item.path, item.rule) for item in violations] == [
        ("product-link", "unsafe-entry")
    ]


def test_artifact_mode_allows_only_contained_links_and_scans_zip_members(
    tmp_path: Path,
) -> None:
    gate = _gate_module()
    target = tmp_path / "Versions/A/runtime"
    target.parent.mkdir(parents=True)
    target.write_text("e-Mate", encoding="utf-8")
    link = tmp_path / "runtime"
    try:
        os.symlink(target.relative_to(tmp_path), link)
    except OSError:
        pytest.skip("this account cannot create symbolic links")
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("binary.bin", b"\0Cow\0")
        output.writestr("assets/leak.txt", "CowAgent")
        output.writestr("ecorex/migration/compat.py", "CowAgent")

    violations = gate.check((tmp_path,), allow_contained_symlinks=True)

    assert [(item.path, item.rule) for item in violations] == [
        ("release.zip!/assets/leak.txt", "predecessor-product")
    ]


def test_command_outputs_machine_readable_release_evidence(tmp_path: Path) -> None:
    (tmp_path / "app.txt").write_text("CowAgent", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, SCRIPT, tmp_path],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stdout == ""
    result = json.loads(completed.stderr)
    assert result["ok"] is False
    assert result["scanned_files"] == 1
    assert result["violations"][0]["path"] == "app.txt"
