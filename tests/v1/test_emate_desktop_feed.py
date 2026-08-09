from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex import __version__
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuilder,
    ReleaseBuildSpec,
)
from ecorex.update import ReleaseChannel, ReleaseSource, SourceKind


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "prepare-emate-desktop-feed.py"
NGINX = ROOT / "deploy" / "e-mate" / "nginx" / "update-feed.conf"
COMMIT = "a" * 40
VERSION = __version__


def _sha512(path: Path) -> str:
    return base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode("ascii")


def _metadata(path: Path, names: tuple[str, ...]) -> None:
    entries = []
    for name in names:
        artifact = path.parent / name
        entries.extend(
            (
                f"  - url: {name}",
                f"    sha512: {_sha512(artifact)}",
                f"    size: {artifact.stat().st_size}",
            )
        )
    primary = path.parent / names[0]
    path.write_text(
        "\n".join(
            (
                f"version: {VERSION}",
                "files:",
                *entries,
                f"path: {names[0]}",
                f"sha512: {_sha512(primary)}",
                "releaseDate: '2026-08-09T00:00:00.000Z'",
                "",
            )
        ),
        encoding="utf-8",
    )


def _desktop(root: Path, target: str, names: tuple[str, ...]) -> None:
    root.mkdir()
    for name in names:
        (root / name).write_bytes(f"{target}:{name}".encode())
        (root / f"{name}.blockmap").write_bytes(f"blockmap:{name}".encode())
    metadata = (
        "latest.yml" if target == "windows-x64" else f"latest-mac-{target[6:]}.yml"
    )
    _metadata(root / metadata, names)
    handoff = (*names, *(f"{name}.blockmap" for name in names), metadata)
    (root / f"{target}.sha256").write_text(
        "".join(
            f"{hashlib.sha256((root / name).read_bytes()).hexdigest()}  {name}\n"
            for name in sorted(handoff)
        ),
        encoding="utf-8",
    )


def _runtime(root: Path) -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    source = root.parent / "runtime-source"
    source.mkdir()
    (source / "emate-bootstrap.exe").write_bytes(b"bootstrap")
    built = ReleaseBuilder(Ed25519MemorySigner("release-test", private)).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.STABLE,
            created_at="2026-08-09T00:00:00Z",
            sources=(
                ReleaseSource(
                    "mirror",
                    SourceKind.GITHUB_CN_MIRROR,
                    0,
                    "https://mirror.example/e-mate",
                ),
                ReleaseSource(
                    "github",
                    SourceKind.GITHUB_RELEASE,
                    1,
                    "https://github.example/e-mate",
                ),
                ReleaseSource(
                    "cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.example/e-mate"
                ),
            ),
            artifacts=(
                ArtifactBuildInput(
                    source,
                    ArtifactKind.BOOTSTRAP,
                    "windows",
                    "x64",
                    executable_paths=("emate-bootstrap.exe",),
                ),
            ),
        ),
        root / "release",
    )
    (root / "manual-webui-build-receipt.json").write_text(
        json.dumps(
            {
                "schema": "emate.desktop-runtime-build-receipt.v2",
                "status": "verified",
                "version": VERSION,
                "source_commit": COMMIT,
                "release_id": built.manifest.release_id,
                "build_digest": built.manifest.build_digest,
                "manifest_sha256": hashlib.sha256(
                    built.manifest_path.read_bytes()
                ).hexdigest(),
                "signing": {
                    "key_id": "release-test",
                    "public_key_sha256": hashlib.sha256(public).hexdigest(),
                    "inner_integrity": "ed25519",
                },
            }
        ),
        encoding="utf-8",
    )
    encoded = base64.b64encode(public).decode("ascii")
    for target in ("windows-x64", "macos-arm64", "macos-x64"):
        destination = root / "bootstraps" / target
        destination.mkdir(parents=True)
        (destination / "bootstrap-config.json").write_text(
            json.dumps({"release_public_keys": {"release-test": encoded}}),
            encoding="utf-8",
        )


def _command(tmp_path: Path, output: str, nginx: Path = NGINX) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--runtime-root",
        str(tmp_path / "runtime"),
        "--windows-root",
        str(tmp_path / "windows-x64"),
        "--macos-arm64-root",
        str(tmp_path / "macos-arm64"),
        "--macos-x64-root",
        str(tmp_path / "macos-x64"),
        "--nginx-config",
        str(nginx),
        "--output",
        str(tmp_path / output),
        "--expected-version",
        VERSION,
        "--expected-source-sha",
        COMMIT,
    ]


def test_feed_gate_merges_mac_metadata_and_rejects_tampering(tmp_path: Path) -> None:
    _runtime(tmp_path / "runtime")
    _desktop(
        tmp_path / "windows-x64",
        "windows-x64",
        (f"e-Mate-Setup-{VERSION}-x64.exe",),
    )
    _desktop(
        tmp_path / "macos-arm64",
        "macos-arm64",
        (f"e-Mate-{VERSION}-arm64.dmg", f"e-Mate-{VERSION}-arm64.zip"),
    )
    _desktop(
        tmp_path / "macos-x64",
        "macos-x64",
        (f"e-Mate-{VERSION}-x64.dmg", f"e-Mate-{VERSION}-x64.zip"),
    )

    completed = subprocess.run(
        _command(tmp_path, "feed"), capture_output=True, text=True
    )

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads((tmp_path / "feed" / "feed-stage-receipt.json").read_text())
    assert receipt["status"] == "awaiting-public-bootstrap-index"
    assert receipt["activation"]["allowed_operations"] == ["activate", "rollback"]
    assert receipt["activation"]["missing_files_must_return"] == 404
    merged = (tmp_path / "feed" / "latest-mac.yml").read_text()
    assert merged.count("  - url: e-Mate-") == 4
    assert "arm64.zip" in merged and "x64.zip" in merged
    download_index = json.loads(
        (tmp_path / "feed" / "download-index.json").read_text()
    )
    assert download_index["version"] == VERSION
    assert [item["target"] for item in download_index["downloads"]] == [
        "windows-x64",
        "macos-arm64",
        "macos-x64",
    ]
    assert all(
        item["url"].startswith("https://mvdcm.ecoremedia.net/e-mate/update/")
        for item in download_index["downloads"]
    )
    assert "download-index.json" in receipt["activation"]["pointer_files"]

    unsafe_nginx = tmp_path / "unsafe-nginx.conf"
    unsafe_nginx.write_text(NGINX.read_text() + "\ntry_files $uri /index.html;\n")
    rejected = subprocess.run(
        _command(tmp_path, "unsafe-feed", unsafe_nginx), capture_output=True, text=True
    )
    assert rejected.returncode == 1
    assert "SPA" in rejected.stderr

    (tmp_path / "macos-x64" / f"e-Mate-{VERSION}-x64.dmg").write_bytes(b"tampered")
    rejected = subprocess.run(
        _command(tmp_path, "tampered-feed"), capture_output=True, text=True
    )
    assert rejected.returncode == 1
    assert "checksum receipt digest mismatch" in rejected.stderr


def test_workflow_builds_the_branch_and_defers_mac_merge() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "emate-2.0-desktop-release.yml"
    ).read_text()
    assert "- codex/e-mate-*" in workflow
    assert "needs.runtime.outputs.version" in workflow
    assert 'package["version"] == branch_version == __version__' in workflow
    assert '--expected-version "${{ needs.runtime.outputs.version }}"' in workflow
    assert "e-mate-2.0.0-runtime-seed" not in workflow
    assert "workflow_dispatch:" in workflow
    assert '"desktop/release/latest-mac-$arch.yml"' in workflow
    assert "name: verify and merge desktop feed" in workflow
    assert "scripts/prepare-emate-desktop-feed.py" in workflow
    for required_gate in (
        "tests/v1/test_public_download_site.py",
        "tests/v1/test_emate_feed_deploy.py",
        "test_product_composes_message_channels_with_the_agent_runtime",
        "npm run typecheck",
        "tools/ga-mock-server.test.mjs",
        "npx playwright install chromium",
        "scheduled tasks and external connections",
    ):
        assert required_gate in workflow
