from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pytest

from ecorex.deployment.cloud_artifact import (
    CloudArtifactBuildError,
    build_signed_cloud_artifact,
    canonical_cloud_manifest,
)
from ecorex.release import Ed25519MemorySigner


REQUIRED = (
    "venv/bin/python3.11",
    "venv/bin/ecorex-control-plane",
    "venv/bin/ecorex-gateway",
    "venv/bin/ecorex-image",
    "deployment/systemd/ecorex-control-plane@.service",
    "deployment/systemd/ecorex-gateway@.service",
    "deployment/systemd/ecorex-image-api@.service",
    "deployment/systemd/ecorex-image-worker@.service",
    "deployment/nginx/control-plane-blue.conf",
    "deployment/nginx/control-plane-green.conf",
    "deployment/nginx/control-plane-disabled.conf",
    "deployment/nginx/admin-route-control-plane.conf",
    "deployment/nginx/ecorex-cloud.routes.conf",
)


def _tree(root: Path) -> Path:
    for relative in REQUIRED:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"payload:{relative}\n", encoding="utf-8")
    return root


def test_cloud_artifact_manifest_binds_every_file_and_signature(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    signer = Ed25519MemorySigner("release-cloud-test", private)
    root = _tree(tmp_path / "cloud")
    result = build_signed_cloud_artifact(
        root, release_id="ecorex-cloud-v1.0.0-test", signer=signer
    )
    manifest_bytes = (root / "cloud-release-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    signature = json.loads((root / "cloud-release-manifest.sig.json").read_bytes())
    assert result["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert {item["path"] for item in manifest["files"]} == set(REQUIRED)
    Ed25519PublicKey.from_public_bytes(signer.public_key_bytes).verify(
        base64.b64decode(signature["signature_b64"], validate=True),
        canonical_cloud_manifest(manifest),
    )


def test_cloud_artifact_refuses_links_and_existing_authority(tmp_path: Path) -> None:
    signer = Ed25519MemorySigner("release-cloud-test", Ed25519PrivateKey.generate())
    root = _tree(tmp_path / "cloud")
    (root / "cloud-release-manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CloudArtifactBuildError, match="manifest_exists"):
        build_signed_cloud_artifact(
            root, release_id="ecorex-cloud-v1.0.0-test", signer=signer
        )
