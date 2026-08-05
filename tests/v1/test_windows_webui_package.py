from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from ecorex import __version__
from ecorex.product_version import stable_release_sequence
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuildSpec,
    ReleaseBuilder,
    WebBundleBuildInput,
    candidate_receipt_signing_payload,
)
from ecorex.release.windows_webui import (
    WINDOWS_FILE_NAME,
    WINDOWS_RECEIPT_SCHEMA,
    WindowsWebUIBuildError,
    build_windows_webui_package,
    verify_windows_webui_package,
)
from ecorex.update import ReleaseChannel, ReleaseSource, SourceKind


def _json(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sources() -> tuple[ReleaseSource, ...]:
    return (
        ReleaseSource("mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://mirror.example/releases"),
        ReleaseSource("github", SourceKind.GITHUB_RELEASE, 1, "https://github.example/releases"),
        ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.example/releases"),
    )


def _runtime_config(public_key: bytes, session_key: bytes) -> bytes:
    encoded = base64.b64encode(public_key).decode()
    session_encoded = base64.b64encode(session_key).decode()
    return json.dumps(
        {
            "schema_version": 1,
            "identity": {"version": __version__, "platform": "windows", "architecture": "x64"},
            "paths": {"database": "state/runtime.sqlite3", "web_root": "web", "web_manifest": "web-manifest.json", "workspace_roots": ["workspace"]},
            "release_public_keys": {"test-release": encoded},
            "rollback_public_keys": {"rollback-key": session_encoded},
            "session_public_keys": {"session-key": session_encoded},
            "gateway": {"endpoint": "https://gateway.example/v1/responses", "allowed_hosts": ["gateway.example"]},
            "device_authorization": {"base_url": "https://identity.example", "allowed_hosts": ["identity.example"], "client_id": "ecorex-product", "timeout_seconds": 20, "supervisor_poll_seconds": 1},
            "update": {"release_feed_endpoint": "https://control.example/api/v1/releases/latest", "signal_endpoint": "wss://control.example/api/v1/client/updates/ws", "control_plane_hosts": ["control.example"], "artifact_hosts": ["cdn.example", "github.example", "mirror.example"], "channel": "stable", "poll_interval_seconds": 300},
            "share": None,
            "image_orchestration": None,
            "audit": None,
            "tracing": None,
            "connectors": None,
            "capability_packs": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _candidate(tmp_path: Path, bootstrap_payload: bytes = b"test-bootstrap"):
    private = Ed25519PrivateKey.generate()
    signer = Ed25519MemorySigner("test-release", private)
    public = signer.public_key_bytes
    session_public = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    core = tmp_path / "core"
    (core / "bin").mkdir(parents=True)
    (core / "bin/ecorex.exe").write_bytes(b"test-runtime")
    (core / "runtime-config.json").write_bytes(_runtime_config(public, session_public))
    bootstrap = tmp_path / "bootstrap"
    (bootstrap / "bin").mkdir(parents=True)
    (bootstrap / "bin/ecorex-bootstrap.exe").write_bytes(bootstrap_payload)
    helper = b"test-sandbox-helper"
    (bootstrap / "bin/ecorex-sandbox-host.exe").write_bytes(helper)
    (bootstrap / "EcoreX Installer.cmd").write_bytes(
        b'@echo off\r\n"%~dp0bin\\ecorex-bootstrap.exe"\r\n'
    )
    sequence = stable_release_sequence(__version__)
    minimum_payload = (
        b"ecorex.bootstrap-minimum-stable.v1\0"
        + str(sequence).encode()
        + b"\0"
        + __version__.encode()
    )
    (bootstrap / "bootstrap-config.json").write_bytes(
        _json(
            {
                "schema_version": 1,
                "public_index_url": "https://dl.ecoremedia.net/ecorex-agent/public-bootstrap-index.json",
                "release_public_keys": {"test-release": base64.b64encode(public).decode()},
                "publication_public_keys": {"publication-test": base64.b64encode(public).decode()},
                "sandbox_helper_sha256": hashlib.sha256(helper).hexdigest(),
                "minimum_stable": {
                    "sequence": sequence,
                    "version": __version__,
                    "signature": {"algorithm": "ed25519", "key_id": "test-release", "value": base64.b64encode(private.sign(minimum_payload)).decode()},
                },
            }
        )
    )
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    javascript = b"document.body.dataset.emate='ready';\n"
    digest = hashlib.sha256(javascript).hexdigest()
    asset = f"assets/app.{digest[:16]}.js"
    (dist / asset).write_bytes(javascript)
    (dist / "index.html").write_text(
        "<!doctype html><html><head><!--__ECOREX_RUNTIME_CONFIG__-->"
        f'<script type="module" src="/{asset}"></script>'
        "</head><body></body></html>",
        encoding="utf-8",
    )
    built = ReleaseBuilder(signer).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.STABLE,
            created_at="2026-08-04T00:00:00+00:00",
            sources=_sources(),
            artifacts=(
                ArtifactBuildInput(core, ArtifactKind.CORE, "windows", "x64", executable_paths=("bin/ecorex.exe",), product_runtime=True),
                ArtifactBuildInput(bootstrap, ArtifactKind.BOOTSTRAP, "windows", "x64", executable_paths=("bin/ecorex-bootstrap.exe", "bin/ecorex-sandbox-host.exe")),
            ),
            web_bundle=WebBundleBuildInput(dist),
            dependency_lock_sha256=hashlib.sha256(b"non-production-test-lock").hexdigest(),
        ),
        tmp_path / "candidate-release",
    )
    manifest_payload = built.manifest_path.read_bytes()
    receipt = {
        "schema_version": 2,
        "receipt_type": "ecorex-candidate-build",
        "status": "passed",
        "code": None,
        "commit_sha": "a" * 40,
        "staging_provenance": {"workflow_path": ".github/workflows/ecorex-v1-platform-stage.yml", "workflow_run_id": 17, "run_attempt": 1, "receipt_sha256": "b" * 64},
        "release_id": built.manifest.release_id,
        "version": built.manifest.version,
        "channel": built.manifest.channel.value,
        "build_digest": built.manifest.build_digest,
        "python_dependency_lock_sha256": "c" * 64,
        "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "web_tree_sha256": "d" * 64,
        "stage_receipts": {},
        "artifacts": {item.artifact_id: {"file_name": item.file_name, "size_bytes": item.size_bytes, "sha256": item.sha256} for item in built.manifest.artifacts},
        "signing": {"algorithm": "ed25519", "key_id": signer.key_id, "operation_count": 1, "executable_sha256": "e" * 64, "adapter_sha256": "f" * 64},
    }
    receipt["signature"] = {"algorithm": "ed25519", "key_id": signer.key_id, "value": base64.b64encode(signer.sign(candidate_receipt_signing_payload(receipt))).decode()}
    receipt_path = tmp_path / "candidate-build-receipt.json"
    receipt_path.write_bytes(_json(receipt))
    return built, receipt_path, public


def test_non_production_windows_webui_package_is_deterministic_and_offline_bound(tmp_path: Path) -> None:
    built, candidate_receipt, public = _candidate(tmp_path)
    outputs = []
    for name in ("first", "second"):
        package, receipt = build_windows_webui_package(
            release_dir=built.output_dir,
            candidate_receipt_path=candidate_receipt,
            output_dir=tmp_path / name,
            trusted_public_keys={"test-release": public},
            generated_at="2026-08-04T12:00:00+08:00",
            production=False,
        )
        outputs.append(package.read_bytes())
        value = json.loads(receipt.read_text())
        assert value["schema"] == WINDOWS_RECEIPT_SCHEMA
        assert value["status"] == "non-production"
        assert value["production_eligible"] is False
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            assert package.name == WINDOWS_FILE_NAME
            assert any(name.startswith("signed/release/") and "core-windows-x64" in name for name in names)
            assert "signed/release/web-manifest.json" in names
            assert not any(name.startswith("runtime/") for name in names)
            assert b"--local-release" in archive.read("Install EcoreX WebUI.cmd")
            release = json.loads(archive.read("release.json"))
            assert release["ui_kind"] == "react-webui"
            assert release["desktop_shell"] == "browser"
            assert release["native_desktop_ui"] is False
            assert release["browser_launch_url"] == "http://127.0.0.1:8765/"
    assert outputs[0] == outputs[1]


def test_production_requires_explicit_signing_key_admission(tmp_path: Path) -> None:
    built, candidate_receipt, public = _candidate(tmp_path)
    with pytest.raises(WindowsWebUIBuildError, match="not admitted"):
        build_windows_webui_package(
            release_dir=built.output_dir,
            candidate_receipt_path=candidate_receipt,
            output_dir=tmp_path / "output",
            trusted_public_keys={"test-release": public},
            generated_at="2026-08-04T12:00:00+08:00",
            production=True,
        )


def test_compressible_bootstrap_can_be_larger_than_its_signed_zip(tmp_path: Path) -> None:
    built, candidate_receipt, public = _candidate(
        tmp_path, bootstrap_payload=b"e-Mate" * 1024 * 1024
    )

    package, _ = build_windows_webui_package(
        release_dir=built.output_dir,
        candidate_receipt_path=candidate_receipt,
        output_dir=tmp_path / "output",
        trusted_public_keys={"test-release": public},
        generated_at="2026-08-04T12:00:00+08:00",
        production=False,
    )

    assert package.is_file()


def test_final_windows_package_is_reopened_and_rejects_layout_or_semantic_tampering(
    tmp_path: Path,
) -> None:
    built, candidate_receipt, public = _candidate(tmp_path)
    package, _ = build_windows_webui_package(
        release_dir=built.output_dir,
        candidate_receipt_path=candidate_receipt,
        output_dir=tmp_path / "output",
        trusted_public_keys={"test-release": public},
        generated_at="2026-08-04T12:00:00+08:00",
        production=False,
    )
    verified = verify_windows_webui_package(
        package,
        trusted_public_keys={"test-release": public},
        production=False,
    )
    assert verified["release_id"] == built.manifest.release_id

    original = package.read_bytes()
    for index, name in enumerate(("unexpected.txt", "../escape", "release.json")):
        target = tmp_path / f"tampered-{index}" / package.name
        target.parent.mkdir()
        target.write_bytes(original)
        with zipfile.ZipFile(target, "a") as archive:
            archive.writestr(name, b"tampered")
        with pytest.raises(WindowsWebUIBuildError, match="layout|inventory"):
            verify_windows_webui_package(
                target,
                trusted_public_keys={"test-release": public},
                production=False,
            )

    target = tmp_path / "semantic" / package.name
    target.parent.mkdir()
    with zipfile.ZipFile(package) as source, zipfile.ZipFile(target, "w") as output:
        for member in source.infolist():
            payload = source.read(member)
            if member.filename == "release.json":
                value = json.loads(payload)
                value["native_desktop_ui"] = True
                payload = _json(value)
            output.writestr(member, payload)
    with pytest.raises(WindowsWebUIBuildError, match="release contract"):
        verify_windows_webui_package(
            target,
            trusted_public_keys={"test-release": public},
            production=False,
        )


def test_go_bootstrap_local_release_path_is_fail_closed_and_network_independent() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "platform-staging/bootstrap/main.go"
    ).read_text(encoding="utf-8")
    local_branch = source.index('if *localRelease != "" {')
    network_branch = source.index("if err := run(*indexURL, *installRootFlag)")
    loader_start = source.index("func loadLocalRelease(")
    loader_end = source.index("func localManifestDiscovery(", loader_start)
    loader = source[loader_start:loader_end]

    assert local_branch < network_branch
    assert "runLocalRelease(*localRelease" in source[local_branch:network_branch]
    local_installer = source[source.index("func runLocalRelease("):loader_start]
    assert "newHTTPClient" not in local_installer
    assert "acquireLocalInstallLock(" in local_installer
    assert "openRunningRuntime(root)" not in local_installer
    assert "return waitForRuntimeAndOpen(root" not in local_installer
    assert "filepath.IsAbs(localRelease)" in loader
    assert "filepath.EvalSymlinks(releaseDir)" in loader
    assert "validateManifest(&release" in loader
    assert "validateMinimumStable(floor" in loader
    assert "requiredArtifacts(&release" in loader
    assert '"bootstrap-" + platform + "-" + architecture' in loader
    assert '"web-manifest"' in loader
    assert "fileMatches(path, item.SizeBytes, item.SHA256)" in loader
    assert "verifyArtifactSignature(&release, item, keys)" in loader
    assert "byFileName" in loader and "os.ReadDir(releaseDir)" in loader
    assert "local release directory inventory is invalid" in loader
    assert (
        "major*100_000_000 + minor*10_000 + patch + 1" in source
    )
    assert "Bootstrap accepts stable v1 releases only" not in source


def test_windows_webui_workflow_is_pinned_and_emits_only_verified_handoff() -> None:
    workflow = (
        Path(__file__).resolve().parents[2]
        / ".github"
        / "workflows"
        / "emate-v030-macos-universal.yml"
    ).read_text(encoding="utf-8")

    assert "github.sha == vars.ECOREX_V030_RELEASE_COMMIT_SHA" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.repository == 'zyfjacksonchen-source/EcoreX'" in workflow
    assert "github.ref_protected" not in workflow
    assert "candidate_commit_mismatch" in workflow
    assert "EcoreX_0.3.0-direct-candidate.zip" in workflow
    assert "build-v030-windows-webui.py" in workflow
    assert "smoke-v030-windows-terminal-package.ps1" in workflow
    assert "runs-on: windows-2022" in workflow
    assert "include-hidden-files: true" in workflow
    assert workflow.index("build-v030-windows-webui.py") < workflow.index(
        "actions/upload-artifact"
    )
    assert "emate-v030-windows-webui" in workflow
