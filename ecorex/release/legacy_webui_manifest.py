"""Build the v0.2.9.2 WebUI update bridge from verified package bytes."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from ecorex import __version__


RECEIPT_SCHEMA = "emate.webui-build-receipt.v1"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_ARTIFACTS = {
    "webui-windows-x64": ("Windows", "WebUI one-click local package"),
    "webui-macos-universal": ("macOS", "WebUI one-click local package"),
}


class LegacyManifestError(ValueError):
    """The verified package receipt cannot be exposed to legacy clients."""


def build_legacy_webui_manifest(receipt_path: Path) -> dict[str, object]:
    """Verify both packages and project the exact legacy manifest schema."""

    receipt_path = receipt_path.resolve(strict=True)
    receipt = _load_object(receipt_path)
    if (
        set(receipt) != {"schema", "version", "status", "generated_at", "artifacts"}
        or receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("version") != __version__
        or receipt.get("status") != "verified"
        or not _timestamp(receipt.get("generated_at"))
    ):
        raise LegacyManifestError("build receipt identity is invalid")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(_ARTIFACTS):
        raise LegacyManifestError("build receipt must contain both WebUI packages")

    projected: dict[str, dict[str, object]] = {}
    for item in artifacts:
        if not isinstance(item, Mapping) or set(item) != {
            "id", "file_name", "size_bytes", "sha256"
        }:
            raise LegacyManifestError("build receipt artifact is invalid")
        artifact_id = item.get("id")
        if artifact_id not in _ARTIFACTS or artifact_id in projected:
            raise LegacyManifestError("build receipt artifact set is invalid")
        expected_name = f"EcoreX_{__version__}-{artifact_id}.zip"
        if item.get("file_name") != expected_name:
            raise LegacyManifestError("build receipt artifact filename is invalid")
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise LegacyManifestError("build receipt artifact integrity is invalid")
        package = receipt_path.parent / expected_name
        if not package.is_file() or package.is_symlink():
            raise LegacyManifestError(f"verified package is missing: {expected_name}")
        actual_size, actual_digest = _file_identity(package)
        if actual_size != size or actual_digest != digest.casefold():
            raise LegacyManifestError(f"verified package changed: {expected_name}")
        platform, variant = _ARTIFACTS[artifact_id]
        projected[artifact_id] = {
            "id": artifact_id,
            "version": __version__,
            "platform": platform,
            "variant": variant,
            "fileName": expected_name,
            "href": f"downloads/{expected_name}",
            "size": actual_size,
            "sha256": actual_digest.upper(),
            "status": "ready",
            "source": f"Verified e-Mate v{__version__} WebUI release package.",
            "updatedAt": receipt["generated_at"],
        }

    version = __version__
    return {
        "product": "e-Mate",
        "version": version,
        "updatedAt": receipt["generated_at"],
        "notes": f"e-Mate v{version} WebUI update from the v0.2.9.2 baseline.",
        "download": {
            "mode": "github-cn-primary",
            "mirrors": [
                {
                    "id": f"ecorex-github-cn-mirror-v{version}",
                    "kind": "github-release-cn-mirror",
                    "baseUrl": "https://gh-proxy.com/https://github.com/zyfjacksonchen-source/EcoreX-installers/releases/download/"
                    f"v{version}",
                    "pathMode": "fileName",
                },
                {
                    "id": f"ecorex-download-origin-v{version}",
                    "kind": "asset-cache",
                    "baseUrl": "https://mvdcm.ecoremedia.net/ecorex-agent/downloads",
                    "pathMode": "fileName",
                },
                {
                    "id": f"ecorex-download-cdn-v{version}",
                    "kind": "asset-cdn",
                    "baseUrl": "https://dl.ecoremedia.net/ecorex-agent/downloads",
                    "pathMode": "fileName",
                },
            ],
            "integrity": "sha256",
        },
        "update": {
            "webui": {
                "mode": "manifest-download",
                "channel": "stable",
                "promotion": "admin-gated",
                "backgroundUpdate": {
                    "mode": "staged-download-idle-install",
                    "idleGate": "/api/active-requests",
                    "installEnv": "ECOREX_UPDATE_MODE=background",
                    "browserAfterInstall": "health-gated-replace-existing-tab",
                    "manualBrowserOpen": "first-install-or-user-request",
                    "activationPolicy": "prompt-health-gated-replace-existing-tab",
                    "healthCheck": "/api/version",
                    "stateFile": "update-state.json",
                    "autoLaunchBrowser": "never-in-background",
                    "rollback": "keep-current-runtime-until-new-runtime-health-check-passes",
                },
                "artifactIds": list(_ARTIFACTS),
            }
        },
        "recommendedDownloads": {
            "win32": {"primary": "webui-windows-x64", "webui": "webui-windows-x64"},
            "darwin": {"primary": "webui-macos-universal", "webui": "webui-macos-universal"},
            "web": {
                "primary": "webui-windows-x64",
                "windows": "webui-windows-x64",
                "macos": "webui-macos-universal",
            },
        },
        "artifacts": [projected[artifact_id] for artifact_id in _ARTIFACTS],
        "admin": {"href": "admin/", "auth": "basic"},
        "downloadsExternalized": True,
    }


def write_legacy_webui_manifest(receipt_path: Path, output_path: Path) -> None:
    """Verify every artifact before atomically replacing the mutable pointer."""

    payload = (
        json.dumps(
            build_legacy_webui_manifest(receipt_path),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _load_object(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise LegacyManifestError("build receipt is invalid") from None
    if not isinstance(value, Mapping):
        raise LegacyManifestError("build receipt must be an object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise LegacyManifestError("build receipt contains a duplicate key")
        value[key] = item
    return value


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Atomically generate the v0.2.9.2-compatible e-Mate update manifest."
    )
    parser.add_argument("receipt", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    write_legacy_webui_manifest(args.receipt, args.output)


if __name__ == "__main__":
    main()
