from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import runpy
import stat

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pytest

from ecorex.deployment.cloud_artifact import (
    CloudArtifactBuildError,
    build_signed_cloud_artifact,
    canonical_cloud_manifest,
    cloud_manifest_file_bytes,
    cloud_manifest_signing_payload,
    unsigned_cloud_manifest,
)
from ecorex.deployment.cloud_artifact_builder import (
    CLOUD_PIP_INDEX_URL_ENV,
    DOMESTIC_PYPI_SIMPLE_INDEX_URL,
    DESCRIPTOR_NAME,
    MANIFEST_NAME,
    PAYLOAD_NAME,
    RECEIPT_NAME,
    CloudArtifactPipelineError,
    attach_detached_cloud_signature,
    create_detached_signature_response,
    _cloud_pip_index_url,
)
from ecorex.release import Ed25519MemorySigner


@pytest.mark.skipif(os.name == "nt", reason="POSIX cloud mode transport contract")
def test_cloud_transport_archive_preserves_only_approved_modes(tmp_path: Path) -> None:
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "scripts/build-v1-linux-cloud-artifact.py"
        )
    )
    source = tmp_path / "source"
    (source / "venv/bin").mkdir(parents=True)
    executable = source / "venv/bin/ecorex-runtime"
    regular = source / "manifest.json"
    executable.write_bytes(b"runtime")
    regular.write_bytes(b"manifest")
    executable.chmod(0o755)
    regular.chmod(0o644)
    archive = tmp_path / "cloud.tar"

    script["_pack"](source, archive)
    output = tmp_path / "output"
    script["_unpack"](archive, output)

    assert stat.S_IMODE(output.stat().st_mode) == 0o555
    assert (output / "venv/bin/ecorex-runtime").read_bytes() == b"runtime"
    assert stat.S_IMODE((output / "venv/bin/ecorex-runtime").stat().st_mode) == 0o755
    assert stat.S_IMODE((output / "manifest.json").stat().st_mode) == 0o644
    output.chmod(0o700)


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


def test_cloud_dependency_index_is_explicitly_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CLOUD_PIP_INDEX_URL_ENV, raising=False)
    assert _cloud_pip_index_url() == "https://pypi.org/simple"

    monkeypatch.setenv(CLOUD_PIP_INDEX_URL_ENV, DOMESTIC_PYPI_SIMPLE_INDEX_URL)
    assert _cloud_pip_index_url() == DOMESTIC_PYPI_SIMPLE_INDEX_URL

    monkeypatch.setenv(CLOUD_PIP_INDEX_URL_ENV, "https://untrusted.example/simple")
    with pytest.raises(CloudArtifactPipelineError, match="cloud_dependency_index_unapproved"):
        _cloud_pip_index_url()


def _tree(root: Path) -> Path:
    for relative in REQUIRED:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"payload:{relative}\n", encoding="utf-8")
        if relative.startswith("venv/bin/"):
            path.chmod(0o755)
    return root


def test_cloud_artifact_manifest_binds_every_file_and_signature(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    signer = Ed25519MemorySigner("release-cloud-test", private)
    root = _tree(tmp_path / "cloud")
    result = build_signed_cloud_artifact(
        root,
        release_id="ecorex-cloud-v1.0.0-test",
        signer=signer,
        source_commit="a" * 40,
        dependency_lock_manifest_sha256="b" * 64,
    )
    manifest_bytes = (root / "cloud-release-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    signature = json.loads((root / "cloud-release-manifest.sig.json").read_bytes())
    assert result["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert {item["path"] for item in manifest["files"]} == set(REQUIRED)
    assert {item["posix_mode"] for item in manifest["files"]} <= {"0644", "0755"}
    Ed25519PublicKey.from_public_bytes(signer.public_key_bytes).verify(
        base64.b64decode(signature["signature_b64"], validate=True),
        cloud_manifest_signing_payload(manifest),
    )


def test_cloud_artifact_manifest_binds_empty_runtime_marker(tmp_path: Path) -> None:
    signer = Ed25519MemorySigner("release-cloud-test", Ed25519PrivateKey.generate())
    root = _tree(tmp_path / "cloud")
    marker = root / "venv/lib/python3.11/site-packages/runtime-marker.pyi"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"")

    build_signed_cloud_artifact(
        root,
        release_id="ecorex-cloud-v1.0.0-test",
        signer=signer,
        source_commit="a" * 40,
        dependency_lock_manifest_sha256="b" * 64,
    )

    manifest = json.loads((root / "cloud-release-manifest.json").read_text())
    row = next(item for item in manifest["files"] if item["path"] == marker.relative_to(root).as_posix())
    assert row["size_bytes"] == 0


def test_cloud_artifact_refuses_links_and_existing_authority(tmp_path: Path) -> None:
    signer = Ed25519MemorySigner("release-cloud-test", Ed25519PrivateKey.generate())
    root = _tree(tmp_path / "cloud")
    (root / "cloud-release-manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CloudArtifactBuildError, match="manifest_exists"):
        build_signed_cloud_artifact(
            root,
            release_id="ecorex-cloud-v1.0.0-test",
            signer=signer,
            source_commit="a" * 40,
            dependency_lock_manifest_sha256="b" * 64,
        )


def _handoff(root: Path, handoff: Path) -> tuple[dict, bytes]:
    handoff.mkdir()
    manifest = unsigned_cloud_manifest(
        root,
        release_id="ecorex-cloud-v1.0.0-detached-test",
        source_commit="a" * 40,
        dependency_lock_manifest_sha256="b" * 64,
    )
    payload = cloud_manifest_signing_payload(manifest)
    manifest_bytes = cloud_manifest_file_bytes(manifest)
    receipt = {
        "schema_version": 1,
        "contract": "ecorex.linux-aarch64-cloud-build.v1",
        "release_id": manifest["release_id"],
        "version": "1.0.0",
        "source_commit": "a" * 40,
        "source_tree_clean": True,
        "source_date_epoch": 1,
        "platform": "linux",
        "architecture": "aarch64",
        "python_version": "3.11.9",
        "dependency_lock_manifest_sha256": "b" * 64,
        "dependency_transport": {
            "index_url": "https://pypi.org/simple",
            "verification": "pip-require-hashes",
        },
        "dependency_locks": {},
        "application_wheel": {},
        "artifact_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "canonical_manifest_sha256": hashlib.sha256(
            canonical_cloud_manifest(manifest)
        ).hexdigest(),
        "signing_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "file_count": len(manifest["files"]),
        "total_bytes": sum(item["size_bytes"] for item in manifest["files"]),
        "posix_mode_contract": {
            "allowed_file_modes": ["0644", "0755"],
            "required_executable_paths": [],
        },
        "verification": {},
    }
    receipt_bytes = (
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    descriptor = {
        "schema_version": 1,
        "contract": "ecorex.detached-cloud-manifest-signing.v1",
        "algorithm": "ed25519",
        "release_id": manifest["release_id"],
        "version": "1.0.0",
        "source_commit": "a" * 40,
        "manifest_file": MANIFEST_NAME,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "payload_file": PAYLOAD_NAME,
        "payload_format": "ecorex-domain-prefix-nul-plus-canonical-json",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_size_bytes": len(payload),
        "build_receipt_file": RECEIPT_NAME,
        "build_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
    }
    (handoff / MANIFEST_NAME).write_bytes(manifest_bytes)
    (handoff / PAYLOAD_NAME).write_bytes(payload)
    (handoff / RECEIPT_NAME).write_bytes(receipt_bytes)
    (handoff / DESCRIPTOR_NAME).write_text(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return descriptor, payload


def test_detached_windows_signature_is_reverified_and_attached_on_linux(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path / "cloud")
    handoff = tmp_path / "handoff"
    _, payload = _handoff(root, handoff)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    response = create_detached_signature_response(
        handoff / DESCRIPTOR_NAME,
        handoff / PAYLOAD_NAME,
        key_id="release-detached-test",
        signature=private.sign(payload),
    )
    response_path = tmp_path / "signature.json"
    response_path.write_text(json.dumps(response), encoding="utf-8")
    keyring = tmp_path / "keys.json"
    keyring.write_text(
        json.dumps(
            {"release-detached-test": base64.b64encode(public).decode("ascii")}
        ),
        encoding="utf-8",
    )

    result = attach_detached_cloud_signature(root, handoff, response_path, keyring)

    assert result["key_id"] == "release-detached-test"
    assert (root / "cloud-release-manifest.json").is_file()
    assert set(response) == {
        "schema_version",
        "contract",
        "algorithm",
        "key_id",
        "manifest_sha256",
        "payload_sha256",
        "signature_b64",
    }


def test_detached_signature_refuses_tree_change_after_linux_receipt(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path / "cloud")
    handoff = tmp_path / "handoff"
    _, payload = _handoff(root, handoff)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    response = create_detached_signature_response(
        handoff / DESCRIPTOR_NAME,
        handoff / PAYLOAD_NAME,
        key_id="release-detached-test",
        signature=private.sign(payload),
    )
    response_path = tmp_path / "signature.json"
    response_path.write_text(json.dumps(response), encoding="utf-8")
    keyring = tmp_path / "keys.json"
    keyring.write_text(
        json.dumps(
            {"release-detached-test": base64.b64encode(public).decode("ascii")}
        ),
        encoding="utf-8",
    )
    (root / REQUIRED[-1]).write_text("changed\n", encoding="utf-8")

    with pytest.raises(CloudArtifactPipelineError, match="manifest_changed"):
        attach_detached_cloud_signature(root, handoff, response_path, keyring)


def test_windows_bridge_refuses_raw_canonical_json_without_cloud_domain(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path / "cloud")
    handoff = tmp_path / "handoff"
    descriptor, _ = _handoff(root, handoff)
    manifest = json.loads((handoff / MANIFEST_NAME).read_text(encoding="utf-8"))
    raw_canonical = canonical_cloud_manifest(manifest)
    (handoff / PAYLOAD_NAME).write_bytes(raw_canonical)
    descriptor["payload_sha256"] = hashlib.sha256(raw_canonical).hexdigest()
    descriptor["payload_size_bytes"] = len(raw_canonical)
    (handoff / DESCRIPTOR_NAME).write_text(json.dumps(descriptor), encoding="utf-8")

    with pytest.raises(CloudArtifactPipelineError, match="unsigned_descriptor_invalid"):
        create_detached_signature_response(
            handoff / DESCRIPTOR_NAME,
            handoff / PAYLOAD_NAME,
            key_id="release-detached-test",
            signature=b"0" * 64,
        )


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX modes")
def test_cloud_artifact_refuses_non_executable_console_entrypoint(tmp_path: Path) -> None:
    root = _tree(tmp_path / "cloud")
    (root / "venv/bin/ecorex-image").chmod(0o644)
    signer = Ed25519MemorySigner("release-cloud-test", Ed25519PrivateKey.generate())

    with pytest.raises(CloudArtifactBuildError, match="entrypoint_not_executable"):
        build_signed_cloud_artifact(
            root,
            release_id="ecorex-cloud-v1.0.0-test",
            signer=signer,
            source_commit="a" * 40,
            dependency_lock_manifest_sha256="b" * 64,
        )
