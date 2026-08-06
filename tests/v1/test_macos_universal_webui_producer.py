from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
from types import SimpleNamespace
import zipfile

import pytest

from ecorex.release.legacy_webui_manifest import (
    build_legacy_webui_manifest,
    RECEIPT_SCHEMA,
)
from ecorex.release.windows_webui import WINDOWS_RECEIPT_SCHEMA


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-v030-macos-universal-webui.py"
SMOKE_SCRIPT = ROOT / "scripts" / "smoke-v030-macos-terminal-package.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "emate-v030-macos-universal.yml"


def _module():
    spec = importlib.util.spec_from_file_location("macos_webui_producer", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_non_macos_fixture_fails_closed_before_creating_release_bytes(
    tmp_path, monkeypatch
):
    module = _module()
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    args = argparse.Namespace(
        candidate_root=tmp_path / "candidate",
        web_dist=tmp_path / "dist",
        windows_package=tmp_path / "windows.zip",
        windows_receipt=tmp_path / "windows-receipt.json",
        output=tmp_path / "output",
        version="0.3.1",
        commit_sha="a" * 40,
        identity="",
        notary_profile="",
        notary_keychain="",
    )

    with pytest.raises(module.MacWebUIBuildError, match="macos_host_required"):
        module.build(args)
    assert not args.output.exists()


def test_missing_distribution_authority_never_falls_back_to_ad_hoc_signing(
    tmp_path, monkeypatch
):
    module = _module()
    monkeypatch.setattr(module, "_require_tools", lambda **_: None)
    args = argparse.Namespace(
        candidate_root=tmp_path / "candidate",
        web_dist=tmp_path / "dist",
        windows_package=tmp_path / "windows.zip",
        windows_receipt=tmp_path / "windows-receipt.json",
        output=tmp_path / "output",
        version="0.3.1",
        commit_sha="a" * 40,
        identity="",
        notary_profile="",
        notary_keychain="",
    )

    with pytest.raises(
        module.MacWebUIBuildError, match="macos_distribution_authority_missing"
    ):
        module.build(args)
    assert not args.output.exists()


def test_verified_receipt_binds_exact_windows_and_notarized_macos_bytes(tmp_path):
    module = _module()
    windows = tmp_path / "EcoreX_0.3.1-webui-windows-x64.zip"
    macos = tmp_path / "EcoreX_0.3.1-webui-macos-universal.zip"
    windows.write_bytes(b"verified-windows")
    macos.write_bytes(b"accepted-notarized-macos-webui-zip")

    receipt = module._write_legacy_receipt(
        tmp_path,
        version="0.3.1",
        windows=windows,
        macos=macos,
        generated_at="2026-08-04T12:00:00Z",
    )
    value = json.loads(receipt.read_text(encoding="utf-8"))
    assert value["schema"] == RECEIPT_SCHEMA
    assert value["status"] == "verified"
    assert [item["id"] for item in value["artifacts"]] == [
        "webui-windows-x64",
        "webui-macos-universal",
    ]
    assert (
        value["artifacts"][1]["sha256"]
        == hashlib.sha256(macos.read_bytes()).hexdigest()
    )
    assert build_legacy_webui_manifest(receipt)["version"] == "0.3.1"


def test_distribution_receipt_rejects_missing_accepted_notarization(tmp_path):
    module = _module()
    valid = {
        "schema": "emate.macos-distribution-receipt.v1",
        "status": "verified",
        "notarization": {"status": "Accepted", "submission_id": "123"},
        "stapling": {"applicable": False, "reason": "zip-ticket-cannot-be-stapled"},
    }
    path = tmp_path / "macos-distribution-receipt.json"
    module._write_distribution_receipt(path, valid)
    assert (
        json.loads(path.read_text(encoding="utf-8"))["notarization"]["status"]
        == "Accepted"
    )

    tampered = {**valid, "notarization": {"status": "Invalid", "submission_id": "123"}}
    with pytest.raises(
        module.MacWebUIBuildError, match="macos_distribution_receipt_invalid"
    ):
        module._write_distribution_receipt(tmp_path / "tampered.json", tampered)
    assert not (tmp_path / "tampered.json").exists()

    terminal = {
        "schema": "emate.macos-distribution-receipt.v1",
        "status": "verified",
        "distribution_mode": "terminal-command",
        "notarization": {
            "status": "not-applicable",
            "reason": "terminal-command-distribution",
        },
        "stapling": {"applicable": False, "reason": "no-app-bundle"},
    }
    terminal_path = tmp_path / "terminal.json"
    module._write_distribution_receipt(terminal_path, terminal)
    assert json.loads(terminal_path.read_text(encoding="utf-8"))[
        "distribution_mode"
    ] == "terminal-command"


def test_portable_terminal_zip_keeps_one_root_and_executable_installer(tmp_path):
    module = _module()
    root = tmp_path / "e-Mate WebUI"
    root.mkdir()
    installer = root / "Install e-Mate WebUI.command"
    installer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    payload = root / "signed" / "release.json"
    payload.parent.mkdir()
    payload.write_text("{}\n", encoding="utf-8")

    package = tmp_path / "package.zip"
    module._write_portable_zip(root, package)

    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {
            "e-Mate WebUI/",
            "e-Mate WebUI/Install e-Mate WebUI.command",
            "e-Mate WebUI/signed/",
            "e-Mate WebUI/signed/release.json",
        }
        mode = archive.getinfo(
            "e-Mate WebUI/Install e-Mate WebUI.command"
        ).external_attr >> 16
        assert stat.S_ISREG(mode) and mode & 0o111 == 0o111


def test_windows_partial_receipt_must_bind_exact_package_and_candidate(tmp_path):
    module = _module()
    package = tmp_path / "EcoreX_0.3.1-webui-windows-x64.zip"
    package.write_bytes(b"windows")
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_bytes(b"manifest")
    candidate_receipt = tmp_path / "candidate-build-receipt.json"
    candidate_receipt.write_bytes(b"candidate")
    receipt = tmp_path / "windows-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": WINDOWS_RECEIPT_SCHEMA,
                "status": "partial",
                "production_eligible": True,
                "artifacts": [
                    {
                        "id": "webui-windows-x64",
                        "file_name": package.name,
                        "size_bytes": package.stat().st_size,
                        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
                        "provenance": {
                            "release_id": "release-1",
                            "build_digest": "a" * 64,
                            "manifest_sha256": hashlib.sha256(
                                manifest_path.read_bytes()
                            ).hexdigest(),
                            "candidate_receipt_sha256": hashlib.sha256(
                                candidate_receipt.read_bytes()
                            ).hexdigest(),
                            "signing_key_id": "release-key",
                            "core_artifact_id": "core-windows-x64",
                            "core_sha256": "b" * 64,
                            "web_manifest_sha256": "c" * 64,
                            "included_artifact_ids": [
                                "core-windows-x64",
                                "web-manifest",
                            ],
                            "mode": "production",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    artifacts = {
        "core-windows-x64": SimpleNamespace(sha256="b" * 64),
        "web-manifest": SimpleNamespace(sha256="c" * 64),
    }
    manifest = SimpleNamespace(
        release_id="release-1",
        build_digest="a" * 64,
        signature=SimpleNamespace(key_id="release-key"),
        artifact=lambda artifact_id: artifacts[artifact_id],
    )
    module._verify_windows_partial_receipt(
        receipt, package, manifest, manifest_path, candidate_receipt
    )
    package.write_bytes(b"tampered")
    with pytest.raises(
        module.MacWebUIBuildError, match="windows_webui_receipt_invalid"
    ):
        module._verify_windows_partial_receipt(
            receipt, package, manifest, manifest_path, candidate_receipt
        )


def test_workflow_builds_and_runs_both_terminal_macos_architectures():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    producer = SCRIPT.read_text(encoding="utf-8")
    smoke = SMOKE_SCRIPT.read_text(encoding="utf-8")

    assert "github.sha == vars.ECOREX_V031_RELEASE_COMMIT_SHA" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.repository == 'zyfjacksonchen-source/EcoreX'" in workflow
    assert "github.ref_protected" not in workflow
    assert "EcoreX_0.3.1-direct-candidate.zip" in workflow
    assert "candidate_bundle_sha256" in workflow
    assert "candidate_commit_sha" in workflow
    assert "APPLE_NOTARY_KEY_BASE64" not in workflow
    assert "--terminal-distribution" in workflow
    assert "runs-on: macos-15" in workflow
    assert "runs-on: macos-15-intel" in workflow
    assert "runs-on: windows-2022" not in workflow
    assert "build-v030-windows-webui.py" not in workflow
    assert workflow.count(
        "python scripts/install-v1-python-profile.py --profile runtime"
    ) == 2
    assert "Download authenticated manual Candidate and Windows handoffs" in workflow
    assert workflow.count("smoke-v030-macos-terminal-package.sh") == 2
    assert workflow.index("build-v030-macos-universal-webui.py") < workflow.index(
        "Upload package handoff and arm64 user evidence"
    )
    assert (
        workflow.index("build-v030-macos-universal-webui.py")
        < workflow.index("ecorex.release.legacy_webui_manifest")
        < workflow.index("Upload package handoff and arm64 user evidence")
    )
    assert workflow.count("include-hidden-files: true") == 2
    assert "emate-v031-verified-webui-packages" not in workflow
    assert "emate-v031-arm64-qualified-webui-packages" in workflow
    assert "--web-dist .producer/source/desktop/dist" in workflow
    assert "--candidate-root .producer/candidate" in workflow
    assert "--windows-receipt" in workflow
    assert "--windows-package .producer/downloads/EcoreX_0.3.1-webui-windows-x64.zip" in workflow
    assert "--windows-receipt .producer/downloads/emate-webui-build-receipt.json" in workflow
    assert "macos-distribution-receipt.json" in workflow
    assert "x86_64) PACKAGE_ARCH=x64" in smoke
    assert 'ecorex-core-macos-$PACKAGE_ARCH-*.zip' in smoke
    assert 'tail -n 20 "$INSTALL_ROOT/install-journal.ndjson"' in smoke
    assert "_verify_candidate_receipt(" in producer
    assert producer.index("verify_windows_webui_package(") < producer.index(
        "_verify_windows_partial_receipt(\n        windows_receipt"
    )
    assert 'package_root / "evidence"' in producer
    assert 'expected_platform="macos"' in producer
    assert 'for architecture in ("arm64", "x64")' in producer
    assert 'shutil.copytree(web, package_root / "web"' not in producer
    assert "_verify_terminal_slice(" in producer
    assert '"Signature=adhoc"' in producer
    assert 'distribution_mode = "terminal-command"' in producer
    assert '"--keychain"' in producer and "args.notary_keychain" in producer
    assert '"notarytool"' in producer and '"submit"' in producer
    assert '"status") != "Accepted"' in producer
    assert "stapler" not in producer
    assert "WebUI.app" not in producer
    assert '"-create"' not in producer
    assert 'case "$(uname -m)"' in producer
    assert '--local-release "$BASE_DIR/signed"' in producer
    assert "zip-ticket-cannot-be-stapled" in producer
    assert (
        '"--assess"' in producer and '"--type"' in producer and '"execute"' in producer
    )
    assert '"--verify"' in producer and '"--strict"' in producer
