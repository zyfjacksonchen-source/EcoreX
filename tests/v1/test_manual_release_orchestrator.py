from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ecorex.release.manual import (
    ALL_STEPS,
    BUILTIN_TOOL_IDS,
    COW_HARD_TOOL_IDS,
    COW_OFFICE_TOOL_IDS,
    PREPARE_STEPS,
    ManualReleaseError,
    ReleaseRunStore,
    ReleaseSpec,
    browser_request,
    confirmation_phrase,
    validate_codex_browser_receipt,
)


ROOT = Path(__file__).resolve().parents[2]
SPEC = ReleaseSpec(version="1.0.0", commit="a" * 40, from_version="0.3.2")


def _passed(name: str, **extra: object) -> dict[str, object]:
    return {"schema_version": 1, "status": "passed", "name": name, **extra}


def _ready_for_browser(tmp_path: Path) -> ReleaseRunStore:
    store = ReleaseRunStore(tmp_path, SPEC)
    store.create()
    for step in ALL_STEPS[:-1]:
        receipt = (
            _passed(step, browser_nonce="b" * 64)
            if step == "update-notification"
            else _passed(step)
        )
        store.complete(step, receipt)
    return store


def _observations() -> dict[str, object]:
    return {
        "versions": {
            "installed_runtime": "1.0.0",
            "visible_ui": "1.0.0",
            "public_manifest": "1.0.0",
            "admin_service": "1.0.0",
        },
        "model_stream": {
            "prompt_nonce": "b" * 64,
            "request_id": "req-production-1",
            "model_id": "ecorex-chat",
            "reasoning_effort": "max",
            "incremental_frame_count": 3,
            "terminal_frame_count": 1,
            "answer_contains_nonce": True,
        },
        "tool_call": {
            "tool_call_id": "tool-production-1",
            "status_sequence": ["pending", "running", "completed"],
            "terminal_result_present": True,
            "read_only": True,
        },
        "builtin_capabilities": {
            tool_id: {"status": "completed", "terminal_visible": True}
            for tool_id in BUILTIN_TOOL_IDS
        },
        "image_concurrency": {
            "model_id": "gpt-image-2",
            "job_ids": ["image-1", "image-2"],
            "completed_count": 2,
            "overlap_observed": True,
            "chat_responsive_during_jobs": True,
        },
        "long_session": {
            "turn_count": 120,
            "mounted_turn_count": 18,
            "history_restored": True,
            "jump_to_latest": True,
            "follow_pause_resume": True,
        },
        "runtime_projection": {
            "skill_count": 2,
            "mcp_count": 0,
            "mcp_status": "unconfigured",
            "matches_runtime_api": True,
        },
        "release_change": {
            "assertion": "terminal dynamic component rendered once",
            "matched": True,
        },
    }


def test_state_machine_is_monotonic_resumable_and_identity_bound(tmp_path):
    store = ReleaseRunStore(tmp_path, SPEC)
    store.create()
    receipt = _passed("preflight")
    store.complete("preflight", receipt)
    assert store.complete("preflight", receipt)["status"] == "preparing"
    with pytest.raises(ManualReleaseError, match="release_step_receipt_conflict"):
        store.complete("preflight", _passed("different"))
    with pytest.raises(ManualReleaseError, match="release_step_order_invalid"):
        store.complete("candidate-build", _passed("candidate-build"))
    assert ReleaseRunStore.open(tmp_path, SPEC.run_id).spec == SPEC


def test_release_store_uses_windows_native_acl_without_fchmod(tmp_path, monkeypatch):
    monkeypatch.delattr("ecorex.release.manual.os.fchmod", raising=False)
    store = ReleaseRunStore(tmp_path, SPEC)
    assert store.create()["status"] == "created"


def test_prepare_boundary_requires_explicit_exact_confirmation(tmp_path):
    store = ReleaseRunStore(tmp_path, SPEC)
    store.create()
    for step in PREPARE_STEPS:
        store.complete(step, _passed(step))
    assert store.read()["status"] == "awaiting-user-confirmation"
    assert confirmation_phrase(SPEC) == "PUBLISH v1.0.0@aaaaaaaa AND NOTIFY USERS"


def test_comprehensive_codex_browser_receipt_is_digest_bound(tmp_path):
    store = _ready_for_browser(tmp_path)
    request = browser_request(store)
    evidence_root = tmp_path / "browser-evidence"
    evidence_root.mkdir()
    evidence = []
    for index in range(10):
        path = evidence_root / f"evidence-{index}.json"
        path.write_text(json.dumps({"index": index}), encoding="utf-8")
        evidence.append(
            {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    receipt = {
        "schema_version": 1,
        "executor": "codex-browser-automation",
        "status": "passed",
        "run_id": SPEC.run_id,
        "nonce": request["nonce"],
        "version": SPEC.version,
        "installed_url": request["installed_url"],
        "public_url": request["public_url"],
        "checks": {name: True for name in request["required_checks"]},
        "observations": _observations(),
        "evidence": evidence,
        "completed_at": "2026-08-07T12:00:00Z",
    }

    assert validate_codex_browser_receipt(
        receipt,
        spec=SPEC,
        run_id=SPEC.run_id,
        nonce=str(request["nonce"]),
        evidence_root=evidence_root,
    )["status"] == "passed"
    shell = receipt["observations"]["builtin_capabilities"].pop("bash")
    with pytest.raises(ManualReleaseError, match="codex_browser_observations_invalid"):
        validate_codex_browser_receipt(
            receipt,
            spec=SPEC,
            run_id=SPEC.run_id,
            nonce=str(request["nonce"]),
            evidence_root=evidence_root,
        )
    receipt["observations"]["builtin_capabilities"]["bash"] = shell
    receipt["observations"]["image_concurrency"]["overlap_observed"] = False
    with pytest.raises(ManualReleaseError, match="codex_browser_observations_invalid"):
        validate_codex_browser_receipt(
            receipt,
            spec=SPEC,
            run_id=SPEC.run_id,
            nonce=str(request["nonce"]),
            evidence_root=evidence_root,
        )


def test_browser_request_requires_real_production_model_images_and_long_session(tmp_path):
    request = browser_request(_ready_for_browser(tmp_path))
    scenarios = {item["id"] for item in request["production_scenarios"]}
    assert scenarios == {
        "model-stream",
        "tool-lifecycle",
        "builtin-matrix",
        "image-concurrency",
        "long-session",
        "runtime-management",
    }
    assert "token consumption is authorized" in request["token_policy"]
    assert len(COW_HARD_TOOL_IDS) == 19
    assert set(COW_OFFICE_TOOL_IDS) == {
        "office_documents",
        "office_pdf",
        "office_presentations",
        "office_spreadsheets",
    }
    assert request["required_builtin_tool_ids"] == list(BUILTIN_TOOL_IDS)
    assert set(request["receipt_observations"]) == set(_observations())


def test_manual_workflows_have_no_automatic_trigger_or_publication_permission():
    script = (ROOT / "scripts/release-v1.py").read_text(encoding="utf-8")
    assert 'add_argument("--yes"' not in script
    assert "ECOREX_GITHUB_TOKEN" not in script
    assert "_READ_ONLY_BUILD_WORKFLOWS" in script
    assert "ecorex-v1-promote-candidate.yml" not in script
    assert not (ROOT / ".github/workflows/ecorex-v1-promote-candidate.yml").exists()
    for name in (
        "ecorex-v1-ci.yml",
        "ecorex-v1-platform-stage.yml",
        "ecorex-v1-candidate.yml",
        "emate-v030-macos-universal.yml",
        "ecorex-v1-online-update.yml",
    ):
        workflow = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        assert "  push:" not in workflow
        assert "  pull_request:" not in workflow
        assert "contents: write" not in workflow
        assert "releases: write" not in workflow
        assert "packages: write" not in workflow
        assert "deployments: write" not in workflow
    online = (ROOT / ".github/workflows/ecorex-v1-online-update.yml").read_text(
        encoding="utf-8"
    )
    assert "runs-on: windows-2022" in online
    assert "runs-on: macos-15-intel" not in online  # matrix value, not ambient runner
    assert "os: macos-15-intel" in online

    assert not (ROOT / ".github/workflows/ecorex-v1-pr.yml").exists()
    trusted = (ROOT / ".github/workflows/ecorex-v1-pr-trusted.yml").read_text(
        encoding="utf-8"
    )
    assert "  pull_request_target:" in trusted
    assert "  pull_request:" not in trusted
    assert "  workflow_dispatch:" not in trusted
    assert "  push:" not in trusted
    assert "name: v1 PR trusted development gate" in trusted
    assert "contents: read" in trusted
    assert "fetch-depth: 0" in trusted
    assert "persist-credentials: false" in trusted
    assert trusted.index("run: npm run build:web") < trusted.index("run: npm run test:v1")
    assert "git diff --name-only -z --diff-filter=ACMRT" in trusted
    assert 'if ((${#guarded[@]} && ! ${#tests[@]})); then' in trusted
    assert "python_product_change_requires_changed_regression" in trusted
    assert "python -m pytest --collect-only -q" in trusted
    for smoke in (
        "test_version_source.py",
        "test_bootstrap_supervisor.py",
        "test_connector_vault.py",
        "test_output_service.py",
        "test_runtime_state_machine_invariants.py",
        "test_macos_codesign_contract.py",
        "test_update_manifest.py",
        "test_update_coordinator.py",
    ):
        assert f"tests/v1/{smoke}" in trusted
    assert "ref: ${{ github.event.pull_request.head.sha }}" in trusted
    assert "cache:" not in trusted
    for forbidden in (
        "environment:",
        "secrets.",
        "codesign ",
        "signtool",
        "ecorex_release_signer",
        "electron-builder",
        "deploy",
        "release-v1.py",
    ):
        assert forbidden not in trusted.lower()


def test_online_smokes_use_the_installed_old_bootstrap_not_the_new_installer():
    for name in (
        "smoke-v1-online-update-macos.sh",
        "smoke-v1-online-update-windows.ps1",
    ):
        script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "/api/update-check" in script
        assert "desktop-entry.json" in script
        assert "--install-root" in script
        assert "online_bootstrap_executed" in script
        assert "automatic_browser_open" in script
