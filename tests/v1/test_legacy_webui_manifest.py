from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ecorex.release.legacy_webui_manifest import (
    build_legacy_webui_manifest,
    LegacyManifestError,
    RECEIPT_SCHEMA,
    write_legacy_webui_manifest,
)
from ecorex.release.legacy_webui_publication import (
    LegacyPublicationError,
    PACKAGE_ORIGINS,
    PublicationPaths,
    publish_legacy_webui,
)


ROOT = Path(__file__).resolve().parents[2]


def test_legacy_manifest_is_last_atomic_step_and_uses_verified_bytes(tmp_path):
    artifacts = []
    for artifact_id, payload in (
        ("webui-windows-x64", b"windows-package"),
        ("webui-macos-universal", b"macos-package"),
    ):
        name = f"EcoreX_0.3.1-{artifact_id}.zip"
        (tmp_path / name).write_bytes(payload)
        artifacts.append(
            {
                "id": artifact_id,
                "file_name": name,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    receipt = tmp_path / "webui-build-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": RECEIPT_SCHEMA,
                "version": "0.3.1",
                "status": "verified",
                "generated_at": "2026-08-04T12:00:00Z",
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "legacy-pointer" / "manifest.json"
    write_legacy_webui_manifest(receipt, output)

    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["product"] == "e-Mate"
    assert manifest["version"] == "0.3.1"
    assert manifest["download"]["mirrors"][0]["baseUrl"] == (
        "https://gh-proxy.com/https://github.com/zyfjacksonchen-source/"
        "EcoreX-installers/releases/download/v0.3.1"
    )
    assert manifest["update"]["webui"]["artifactIds"] == [
        "webui-windows-x64",
        "webui-macos-universal",
    ]
    background = manifest["update"]["webui"]["backgroundUpdate"]
    assert background["browserAfterInstall"] == "health-gated-replace-existing-tab"
    assert background["activationPolicy"] == "prompt-health-gated-replace-existing-tab"
    assert background["rollback"] == (
        "keep-current-runtime-until-new-runtime-health-check-passes"
    )
    assert [item["size"] for item in manifest["artifacts"]] == [
        len(b"windows-package"),
        len(b"macos-package"),
    ]

    original = output.read_bytes()
    (tmp_path / "EcoreX_0.3.1-webui-macos-universal.zip").write_bytes(b"changed")
    with pytest.raises(LegacyManifestError, match="changed"):
        write_legacy_webui_manifest(receipt, output)
    assert output.read_bytes() == original


def test_public_servers_expose_only_the_atomic_legacy_pointer():
    nginx = (ROOT / "deploy/ecorex-site/nginx/ecorex-agent.conf.example").read_text(
        encoding="utf-8"
    )
    cloud = (
        ROOT / "deploy/ecorex-cloud-sidecar/nginx/ecorex-cloud.routes.conf"
    ).read_text(encoding="utf-8")
    caddy = (ROOT / "deploy/ecorex-site/caddy/ecorex-agent.routes.caddy").read_text(
        encoding="utf-8"
    )
    assert nginx.count("location = /ecorex-agent/manifest.json") == 1
    assert cloud.count("location = /ecorex-agent/manifest.json") == 1
    assert "/srv/ecorex-agent-download/legacy-pointer/manifest.json" in nginx
    assert "/srv/ecorex-agent-download/legacy-pointer/manifest.json" in cloud
    assert "location ^~ /ecorex-agent/downloads/" in cloud
    assert "alias /srv/ecorex-agent-download/current/downloads/;" in cloud
    assert "handle /ecorex-agent/manifest.json" in caddy
    assert "root * /srv/ecorex-agent-download/legacy-pointer" in caddy


class _Readback:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def identity(self, url: str, *, maximum_bytes: int) -> tuple[int, str]:
        self.calls.append(url)
        payload = self.payloads[url]
        assert len(payload) <= maximum_bytes
        return len(payload), hashlib.sha256(payload).hexdigest()


def _publication_fixture(
    tmp_path: Path,
) -> tuple[Path, PublicationPaths, dict[str, bytes]]:
    artifacts = []
    packages: dict[str, bytes] = {}
    for artifact_id, payload in (
        ("webui-windows-x64", b"windows-package"),
        ("webui-macos-universal", b"macos-package"),
    ):
        name = f"EcoreX_0.3.1-{artifact_id}.zip"
        (tmp_path / name).write_bytes(payload)
        packages[name] = payload
        artifacts.append(
            {
                "id": artifact_id,
                "file_name": name,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    receipt = tmp_path / "webui-build-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": RECEIPT_SCHEMA,
                "version": "0.3.1",
                "status": "verified",
                "generated_at": "2026-08-04T12:00:00Z",
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    pointer = tmp_path / "site" / "legacy-pointer" / "manifest.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"version": "0.2.9.2"}), encoding="utf-8")
    paths = PublicationPaths(
        downloads=tmp_path / "site" / "current" / "downloads",
        pointer=pointer,
        lock=tmp_path / "deploy.lock",
    )
    return receipt, paths, packages


def test_publication_reads_back_two_origins_before_switching_manifest(tmp_path):
    receipt, paths, packages = _publication_fixture(tmp_path)
    origins = (
        "https://mvdcm.ecoremedia.net/ecorex-agent",
        "https://dl.ecoremedia.net/ecorex-agent",
    )
    manifest = build_legacy_webui_manifest(receipt)
    manifest_bytes = (
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    payloads = {
        **{
            f"{origin}/downloads/{name}": payload
            for origin in origins
            for name, payload in packages.items()
        },
        **{f"{origin}/manifest.json": manifest_bytes for origin in origins},
    }
    readback = _Readback(payloads)

    result = publish_legacy_webui(
        receipt,
        paths=paths,
        readback=readback,
        package_origins=tuple(f"{origin}/downloads" for origin in origins),
        manifest_origins=origins,
        enforce_server_fence=False,
    )

    assert result["status"] == "published"
    assert json.loads(paths.pointer.read_text(encoding="utf-8"))["version"] == "0.3.1"
    assert readback.calls[-2:] == [f"{origin}/manifest.json" for origin in origins]


def test_manifest_readback_failure_restores_v0292_pointer(tmp_path):
    receipt, paths, packages = _publication_fixture(tmp_path)
    original = paths.pointer.read_bytes()
    origins = (
        "https://mvdcm.ecoremedia.net/ecorex-agent",
        "https://dl.ecoremedia.net/ecorex-agent",
    )
    payloads = {
        f"{origin}/downloads/{name}": payload
        for origin in origins
        for name, payload in packages.items()
    }
    payloads.update({f"{origin}/manifest.json": b"stale" for origin in origins})

    with pytest.raises(LegacyPublicationError, match="manifest_readback_mismatch"):
        publish_legacy_webui(
            receipt,
            paths=paths,
            readback=_Readback(payloads),
            package_origins=tuple(f"{origin}/downloads" for origin in origins),
            manifest_origins=origins,
            enforce_server_fence=False,
        )

    assert paths.pointer.read_bytes() == original


def test_user_package_workflow_hands_verified_bytes_to_direct_publisher():
    workflow = (ROOT / ".github/workflows/emate-v030-macos-universal.yml").read_text(
        encoding="utf-8"
    )

    admission = (
        "github.repository == 'zyfjacksonchen-source/EcoreX'"
        " && github.ref == 'refs/heads/main'"
        " && github.sha == vars.ECOREX_V031_RELEASE_COMMIT_SHA"
    )
    assert workflow.count(admission) == 1
    assert "EcoreX_0.3.1-webui-windows-x64.zip" in workflow
    assert "EcoreX_0.3.1-webui-macos-universal.zip" in workflow
    assert "webui-build-receipt.json" in workflow
    assert "macos-arm64-user-smoke.json" in workflow
    assert "macos-x64-user-smoke.json" in workflow
    assert "secrets.ECOREX_GITHUB_RELEASE_TOKEN" not in workflow
    assert "gh release upload" not in workflow
    assert "build-v030-windows-webui.py" not in workflow
    assert "ecorex.release.legacy_webui_publication" not in workflow
    assert workflow.index("ecorex.release.legacy_webui_manifest") < workflow.index(
        "Upload package handoff and arm64 user evidence"
    )
    assert PACKAGE_ORIGINS[0] == (
        "https://gh-proxy.com/https://github.com/zyfjacksonchen-source/"
        "EcoreX-installers/releases/download/v0.3.1"
    )


def test_macos_user_smoke_uses_and_restores_a_real_temporary_keychain():
    smoke = (ROOT / "scripts/smoke-v030-macos-terminal-package.sh").read_text(
        encoding="utf-8"
    )

    assert "/usr/bin/security create-keychain" in smoke
    assert "/usr/bin/security unlock-keychain" in smoke
    assert '/usr/bin/security default-keychain -d user -s "$KEYCHAIN_PATH"' in smoke
    assert '/usr/bin/security list-keychains -d user -s "$KEYCHAIN_PATH"' in smoke
    assert "ORIGINAL_DEFAULT_KEYCHAIN" in smoke
    assert "ORIGINAL_KEYCHAIN_LIST" in smoke
    assert '/usr/bin/security delete-keychain "$KEYCHAIN_PATH"' in smoke
    assert "_MacOSKeychainBackend" in smoke
    assert "macos_keychain_backend=passed" in smoke
    assert "macos_keychain_backend_osstatus=" in smoke
