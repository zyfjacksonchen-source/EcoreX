from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-v1-reproducibility.py"
SPEC = importlib.util.spec_from_file_location("ecorex_v1_reproducibility_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)


def test_checked_in_byte_contract_is_canonical_and_platform_neutral() -> None:
    contract = GATE.build_contract(ROOT)
    encoded = GATE.canonical_json_bytes(contract)

    assert encoded.endswith(b"\n")
    assert b"\r" not in encoded
    assert json.loads(encoded) == contract
    assert contract["document_type"] == GATE.CONTRACT_TYPE
    assert any(value["kind"] == "canonical-json" for value in contract["files"])
    assert any(value["kind"] == "shell-source" for value in contract["files"])
    assert any(value["kind"] in {"public-entry", "web-entry"} for value in contract["files"])
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.html text eol=lf" in attributes.splitlines()
    assert "*.ts text eol=lf" in attributes.splitlines()
    assert "*.tsx text eol=lf" in attributes.splitlines()


def test_byte_contract_contains_only_current_v1_shell_identity_inputs() -> None:
    assert GATE.V1_SHELL_FILES == ("run.sh", "scripts/start.sh")

    contract = GATE.build_contract(ROOT)
    shell_paths = {
        value["path"]
        for value in contract["files"]
        if value["kind"] == "shell-source"
    }
    assert shell_paths == set(GATE.V1_SHELL_FILES)


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises(GATE.ReproducibilityError, match="canonical JSON"):
        GATE.canonical_json_bytes({"invalid": float("nan")})


def test_digest_gate_rejects_name_content_mismatch(tmp_path: Path) -> None:
    asset = tmp_path / "app.000000000000.js"
    asset.write_text("export {};\n", encoding="utf-8", newline="\n")

    with pytest.raises(GATE.ReproducibilityError, match="digest/name mismatch"):
        GATE._validate_hashed_assets(
            tmp_path,
            tmp_path,
            digest_length=12,
            kind="test-asset",
        )


def test_lf_gate_rejects_windows_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "install.sh"
    payload = b"#!/bin/sh\r\nexit 0\r\n"
    path.write_bytes(payload)

    with pytest.raises(GATE.ReproducibilityError, match="CR/CRLF"):
        GATE._require_lf_text(path, payload)


def test_contract_comparison_requires_identical_canonical_bytes(tmp_path: Path) -> None:
    first = tmp_path / "windows-x64" / "byte-contract.json"
    second = tmp_path / "macos-arm64" / "byte-contract.json"
    first.parent.mkdir()
    second.parent.mkdir()
    contract = {"document_type": GATE.CONTRACT_TYPE, "files": [], "schema_version": 1}
    first.write_bytes(GATE.canonical_json_bytes(contract))
    second.write_bytes(GATE.canonical_json_bytes(contract))

    assert len(GATE.compare_contracts(tmp_path, 2)) == 2
    second.write_bytes(
        GATE.canonical_json_bytes(
            {
                "document_type": GATE.CONTRACT_TYPE,
                "files": [{"path": "changed"}],
                "schema_version": 1,
            }
        )
    )
    with pytest.raises(GATE.ReproducibilityError, match="differ"):
        GATE.compare_contracts(tmp_path, 2)


def test_v1_ci_matrix_is_read_only_and_covers_supported_architectures() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ecorex-v1-ci.yml").read_text(
        encoding="utf-8"
    )

    assert "permissions:\n  contents: read" in workflow
    assert "windows-2022" in workflow
    assert "windows-latest" not in workflow
    assert "windows-2025" not in workflow
    assert "macos-15\n" in workflow
    assert "macos-15-intel" in workflow
    assert "node-version: \"22.23.1\"" in workflow
    assert "python-version: \"3.11.9\"" in workflow
    assert "--expected-count 4" in workflow
    assert "python scripts/run-v1-lint.py --compile --output-format=github" in workflow
    assert "ECOREX_GITHUB_HOSTED_WINDOWS_NATIVE_COMPATIBILITY" in workflow
    assert "npm audit --audit-level=high" in workflow
    assert "secrets." not in workflow
    assert "continue-on-error" not in workflow
    assert "contents: write" not in workflow
    assert "packages: write" not in workflow
    release_stage = (
        ROOT / ".github" / "workflows" / "ecorex-v1-platform-stage.yml"
    ).read_text(encoding="utf-8")
    assert "ECOREX_GITHUB_HOSTED_WINDOWS_NATIVE_COMPATIBILITY" not in release_stage
    assert (
        '"ecorex-platform-stage-venv-${{ github.run_id }}-'
        '${{ github.run_attempt }}-${{ matrix.id }}"' in release_stage
    )
    assert 'if (Test-Path -LiteralPath $venvRoot)' in release_stage
    assert 'throw "python_venv_root_not_clean:$venvName"' in release_stage
    assert "& $python -m venv $venvRoot" in release_stage
    assert '(Join-Path $installRoot "Scripts") | Out-File' in release_stage
    assert "--break-system-packages" not in release_stage


def test_dev_toolchain_is_pinned_and_lint_has_a_cross_platform_entrypoint() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["optional-dependencies"]["dev"] == [
        "jsonschema==4.26.0",
        "Pillow==12.3.0",
        "pytest==9.1.1",
        "ruff==0.15.21",
    ]
    assert "python-multipart==0.0.26" in project["project"]["dependencies"]
    assert project["tool"]["ruff"]["target-version"] == "py311"
    assert project["tool"]["ruff"]["lint"]["select"] == ["E4", "E7", "E9", "F"]
    assert (ROOT / "scripts" / "run-v1-lint.py").is_file()
