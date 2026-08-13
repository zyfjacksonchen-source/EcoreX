from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.control_plane.management import AdminManagementRepository
from ecorex.control_plane.management_schema import AdminManagementSchemaManager
from ecorex.control_plane.production_storage import (
    PersistentVolumeGuard,
    ProductionStorageError,
    SQLiteBackupManager,
)
from ecorex.control_plane.schema import (
    ControlPlaneSchemaError,
    ControlPlaneSchemaManager,
)
from ecorex.deployment import cloud_sidecar as deployment


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _spec(tmp_path: Path, artifact: Path | None = None) -> deployment.CloudDeploymentSpec:
    keyring = tmp_path / "release-public-keys.json"
    keyring.write_text("{}", encoding="utf-8")
    attestation = tmp_path / "encrypted-volume-attestation.json"
    attestation.write_text("{}", encoding="utf-8")
    return deployment.CloudDeploymentSpec(
        release_id="ecorex-cloud-v1.0.0-test",
        source_commit="a" * 40,
        dependency_lock_manifest_sha256="b" * 64,
        artifact_root=artifact or tmp_path / "artifact",
        artifact_manifest_sha256="0" * 64,
        release_keyring_path=keyring,
        release_keyring_sha256=_sha(keyring),
        target_machine_id_sha256="1" * 64,
        encryption_attestation_path=attestation,
        encryption_attestation_sha256=_sha(attestation),
    )


def _signed_artifact(
    tmp_path: Path,
    *,
    version: str = deployment.PRODUCT_VERSION,
    dependency_lock_manifest_sha256: str = "b" * 64,
) -> tuple[deployment.CloudDeploymentSpec, Path]:
    root = tmp_path / "artifact"
    required = (
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
    files = []
    for relative in required:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"fixture:{relative}\n".encode())
        posix_mode = "0755" if relative.startswith("venv/bin/") else "0644"
        # Model the real cloud builder contract on POSIX.  write_bytes() is
        # umask-dependent and otherwise leaves executable fixture files at
        # 0644 on Linux while the signed manifest correctly declares 0755.
        target.chmod(int(posix_mode, 8))
        files.append(
            {
                "path": relative,
                "sha256": _sha(target),
                "size_bytes": target.stat().st_size,
                "posix_mode": posix_mode,
            }
        )
    manifest = {
        "schema_version": 1,
        "release_id": "ecorex-cloud-v1.0.0-test",
        "version": version,
        "platform": "linux",
        "architecture": "aarch64",
        "python_version": "3.11.9",
        "build_contract": deployment.BUILD_CONTRACT,
        "source_commit": "a" * 40,
        "dependency_lock_manifest_sha256": dependency_lock_manifest_sha256,
        "files": files,
    }
    manifest_path = root / "cloud-release-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    signature = private.sign(
        deployment.CLOUD_MANIFEST_SIGNING_DOMAIN
        + deployment._canonical_json(manifest)
    )
    signature_path = root / "cloud-release-manifest.sig.json"
    signature_path.write_text(
        json.dumps(
            {
                "key_id": "release-test",
                "manifest_sha256": _sha(manifest_path),
                "signature_b64": base64.b64encode(signature).decode("ascii"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    keyring = tmp_path / "release-public-keys.json"
    keyring.write_text(
        json.dumps({"release-test": base64.b64encode(public).decode("ascii")}),
        encoding="utf-8",
    )
    attestation = tmp_path / "encrypted-volume-attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "alibaba-cloud-kms",
                "volume_id": "d-ecorex-production",
                "mount_root": "/var/lib/ecorex",
                "encrypted": True,
                "evidence_reference": "acs:disk/evidence/immutable",
                "evidence_sha256": "a" * 64,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    spec = deployment.CloudDeploymentSpec(
        release_id="ecorex-cloud-v1.0.0-test",
        source_commit="a" * 40,
        dependency_lock_manifest_sha256=dependency_lock_manifest_sha256,
        artifact_root=root,
        artifact_manifest_sha256=_sha(manifest_path),
        release_keyring_path=keyring,
        release_keyring_sha256=_sha(keyring),
        target_machine_id_sha256="1" * 64,
        encryption_attestation_path=attestation,
        encryption_attestation_sha256=_sha(attestation),
    )
    return spec, root


def test_signed_aarch64_python_3119_artifact_and_encryption_attestation_pass(
    tmp_path: Path,
) -> None:
    spec, _ = _signed_artifact(tmp_path)

    manifest = deployment._validate_artifact(spec)
    deployment._validate_attestation(spec)

    assert manifest["architecture"] == "aarch64"
    assert manifest["python_version"] == "3.11.9"


def test_artifact_byte_tamper_is_rejected_before_staging(tmp_path: Path) -> None:
    spec, root = _signed_artifact(tmp_path)
    (root / "venv/bin/ecorex-image").write_text("tampered", encoding="utf-8")

    with pytest.raises(
        deployment.CloudDeployError, match="artifact_file_digest_mismatch"
    ):
        deployment._validate_artifact(spec)


@pytest.mark.skipif(
    os.name == "nt" or deployment._effective_user_id() != 0,
    reason="requires root-owned POSIX release directories",
)
@pytest.mark.parametrize("source_mode", (0o700, 0o755))
def test_install_release_seals_new_and_existing_legacy_private_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_mode: int
) -> None:
    spec, source = _signed_artifact(tmp_path)
    source.chmod(source_mode)
    release_root = tmp_path / "installed-releases"
    monkeypatch.setattr(deployment, "RELEASE_ROOT", release_root)
    manifest = deployment._validate_artifact(spec)

    destination = deployment._install_release(spec, manifest)

    assert stat.S_IMODE(destination.lstat().st_mode) == 0o555
    destination.chmod(0o700)

    recovered = deployment._install_release(spec, manifest)

    assert recovered == destination
    assert stat.S_IMODE(destination.lstat().st_mode) == 0o555


@pytest.mark.skipif(
    os.name == "nt" or deployment._effective_user_id() != 0,
    reason="requires root-owned POSIX release directories",
)
@pytest.mark.parametrize("malicious_state", ("symlink", "foreign", "unsafe-mode"))
def test_install_release_rejects_malicious_existing_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malicious_state: str,
) -> None:
    spec, source = _signed_artifact(tmp_path)
    source.chmod(0o700)
    release_root = tmp_path / "installed-releases"
    monkeypatch.setattr(deployment, "RELEASE_ROOT", release_root)
    manifest = deployment._validate_artifact(spec)
    release_root.mkdir()
    destination = release_root / spec.release_id
    if malicious_state == "symlink":
        destination.symlink_to(source, target_is_directory=True)
        expected = "release_directory_identity_invalid"
    else:
        shutil.copytree(source, destination)
        if malicious_state == "foreign":
            os.chown(destination, 65534, 65534)
            expected = "release_directory_identity_invalid"
        else:
            destination.chmod(0o775)
            expected = "release_directory_mode_invalid"

    with pytest.raises(deployment.CloudDeployError, match=expected):
        deployment._install_release(spec, manifest)


@pytest.mark.skipif(
    os.name == "nt" or deployment._effective_user_id() != 0,
    reason="requires root-owned POSIX release directories",
)
def test_existing_legacy_private_release_is_verified_before_mode_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, source = _signed_artifact(tmp_path)
    source.chmod(0o700)
    release_root = tmp_path / "installed-releases"
    monkeypatch.setattr(deployment, "RELEASE_ROOT", release_root)
    manifest = deployment._validate_artifact(spec)
    destination = deployment._install_release(spec, manifest)
    destination.chmod(0o700)
    (destination / "venv/bin/ecorex-image").write_bytes(b"tampered")

    with pytest.raises(
        deployment.CloudDeployError, match="artifact_file_digest_mismatch"
    ):
        deployment._install_release(spec, manifest)

    assert stat.S_IMODE(destination.lstat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX modes")
def test_artifact_executable_mode_tamper_is_rejected_before_staging(
    tmp_path: Path,
) -> None:
    spec, root = _signed_artifact(tmp_path)
    target = root / "venv/bin/ecorex-image"
    target.chmod(0o644)

    with pytest.raises(deployment.CloudDeployError, match="artifact_file_mode_mismatch"):
        deployment._validate_artifact(spec)


def test_artifact_manifest_is_bound_to_expected_source_and_lock(tmp_path: Path) -> None:
    spec, _ = _signed_artifact(tmp_path)

    with pytest.raises(deployment.CloudDeployError, match="artifact_target_mismatch"):
        deployment._validate_artifact(
            deployment.dataclasses.replace(spec, source_commit="c" * 40)
        )
    with pytest.raises(deployment.CloudDeployError, match="artifact_target_mismatch"):
        deployment._validate_artifact(
            deployment.dataclasses.replace(
                spec, dependency_lock_manifest_sha256="d" * 64
            )
        )


def test_v03x_accepts_only_the_retired_v10_internal_release_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deployment, "PRODUCT_VERSION", "0.3.0")
    spec, _ = _signed_artifact(tmp_path, version="1.0.17")

    deployment._validate_artifact(spec, historical_release=True)
    assert deployment._historical_product_version_is_compatible("1.0.0")
    assert not deployment._historical_product_version_is_compatible("1.0.18")
    monkeypatch.setattr(deployment, "PRODUCT_VERSION", "0.3.2")
    assert deployment._historical_product_version_is_compatible("1.0.0")


def test_transition_release_uses_its_signed_source_commit_not_target_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, source = _signed_artifact(tmp_path)
    release_root = tmp_path / "releases"
    release = release_root / spec.release_id
    release_root.mkdir()
    shutil.copytree(source, release)
    monkeypatch.setattr(deployment, "RELEASE_ROOT", release_root)
    monkeypatch.setattr(
        deployment, "_release_directory_identity", lambda _release: (1, 1)
    )
    monkeypatch.setattr(
        deployment, "_seal_release_directory", lambda _release, _identity: None
    )
    monkeypatch.setattr(deployment, "_verify_staged_runtime", lambda _release: None)
    target_spec = deployment.dataclasses.replace(spec, source_commit="c" * 40)

    verified = deployment._verify_transition_release(
        target_spec,
        {
            "active_release_id": spec.release_id,
            "artifact_manifest_sha256": spec.artifact_manifest_sha256,
        },
    )

    assert verified == release


def test_v031_transition_recovery_accepts_signed_v030_release_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    historical_lock = "c" * 64
    spec, source = _signed_artifact(
        tmp_path,
        version="0.3.0",
        dependency_lock_manifest_sha256=historical_lock,
    )
    release_root = tmp_path / "releases"
    release = release_root / spec.release_id
    release_root.mkdir()
    shutil.copytree(source, release)
    monkeypatch.setattr(deployment, "RELEASE_ROOT", release_root)
    monkeypatch.setattr(
        deployment, "_release_directory_identity", lambda _release: (1, 1)
    )
    monkeypatch.setattr(
        deployment, "_seal_release_directory", lambda _release, _identity: None
    )
    monkeypatch.setattr(deployment, "_verify_staged_runtime", lambda _release: None)
    v102_target_spec = deployment.dataclasses.replace(
        spec,
        source_commit="d" * 40,
        dependency_lock_manifest_sha256="e" * 64,
    )

    verified = deployment._verify_transition_release(
        v102_target_spec,
        {
            "active_release_id": spec.release_id,
            "artifact_manifest_sha256": spec.artifact_manifest_sha256,
        },
    )

    assert verified == release


@pytest.mark.skipif(
    os.name == "nt" or deployment._effective_user_id() != 0,
    reason="requires root-owned POSIX release directories",
)
def test_transition_recovery_repairs_verified_legacy_private_release_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, source = _signed_artifact(tmp_path)
    release_root = tmp_path / "releases"
    release = release_root / spec.release_id
    release_root.mkdir()
    shutil.copytree(source, release)
    release.chmod(0o700)
    monkeypatch.setattr(deployment, "RELEASE_ROOT", release_root)
    monkeypatch.setattr(deployment, "_verify_staged_runtime", lambda _release: None)

    deployment._verify_transition_release(
        deployment.dataclasses.replace(spec, source_commit="c" * 40),
        {
            "active_release_id": spec.release_id,
            "artifact_manifest_sha256": spec.artifact_manifest_sha256,
        },
    )

    assert stat.S_IMODE(release.lstat().st_mode) == 0o555


@pytest.mark.parametrize("source_commit", (None, "", "A" * 40, "a" * 39, "../bad"))
def test_transition_release_rejects_missing_or_malformed_manifest_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_commit: str | None,
) -> None:
    spec, source = _signed_artifact(tmp_path)
    manifest_path = source / "cloud-release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if source_commit is None:
        del manifest["source_commit"]
    else:
        manifest["source_commit"] = source_commit
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    release_root = tmp_path / "releases"
    release = release_root / spec.release_id
    release_root.mkdir()
    shutil.copytree(source, release)
    monkeypatch.setattr(deployment, "RELEASE_ROOT", release_root)
    monkeypatch.setattr(
        deployment, "_release_directory_identity", lambda _release: (1, 1)
    )

    with pytest.raises(
        deployment.CloudDeployError, match="artifact_manifest_invalid"
    ):
        deployment._verify_transition_release(
            deployment.dataclasses.replace(spec, source_commit="c" * 40),
            {
                "active_release_id": spec.release_id,
                "artifact_manifest_sha256": _sha(manifest_path),
            },
        )


def test_raw_canonical_signature_cannot_cross_cloud_signing_domain(
    tmp_path: Path,
) -> None:
    spec, root = _signed_artifact(tmp_path)
    manifest = json.loads((root / "cloud-release-manifest.json").read_text())
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    signature_path = root / "cloud-release-manifest.sig.json"
    signature_path.write_text(
        json.dumps(
            {
                "key_id": "wrong-domain",
                "manifest_sha256": spec.artifact_manifest_sha256,
                "signature_b64": base64.b64encode(
                    private.sign(deployment._canonical_json(manifest))
                ).decode("ascii"),
            }
        ),
        encoding="utf-8",
    )
    spec.release_keyring_path.write_text(
        json.dumps({"wrong-domain": base64.b64encode(public).decode("ascii")}),
        encoding="utf-8",
    )
    spec = deployment.dataclasses.replace(
        spec, release_keyring_sha256=_sha(spec.release_keyring_path)
    )

    with pytest.raises(deployment.CloudDeployError, match="artifact_signature_invalid"):
        deployment._validate_artifact(spec)


def _operator_waived_artifact(
    tmp_path: Path,
) -> tuple[deployment.CloudDeploymentSpec, Path, Path]:
    spec, root = _signed_artifact(tmp_path)
    (root / "cloud-release-manifest.sig.json").unlink()
    waiver = {
        "schema_version": 1,
        "document_type": "ecorex.cloud-unsigned-release-waiver",
        "status": "operator-waived-unsigned",
        "represented_as_signed": False,
        "scope": "single-release",
        "operator_instruction_sha256": "8" * 64,
        "release_id": spec.release_id,
        "version": deployment.PRODUCT_VERSION,
        "source_commit": spec.source_commit,
        "dependency_lock_manifest_sha256": spec.dependency_lock_manifest_sha256,
        "manifest_sha256": spec.artifact_manifest_sha256,
    }
    path = root / deployment.UNSIGNED_WAIVER_NAME
    path.write_text(json.dumps(waiver, sort_keys=True), encoding="utf-8")
    return spec, root, path


def test_unsigned_artifact_requires_exact_release_scoped_operator_waiver(
    tmp_path: Path,
) -> None:
    spec, root, waiver = _operator_waived_artifact(tmp_path)
    with pytest.raises(deployment.CloudDeployError, match="unsigned_waiver_required"):
        deployment._validate_artifact(spec)

    accepted = deployment.dataclasses.replace(
        spec, unsigned_release_waivers={spec.release_id: _sha(waiver)}
    )
    assert deployment._validate_artifact(accepted)["source_commit"] == "a" * 40
    (root / "venv/bin/ecorex-image").write_text("tampered", encoding="utf-8")
    with pytest.raises(deployment.CloudDeployError, match="file_digest_mismatch"):
        deployment._validate_artifact(accepted)


def test_unsigned_waiver_never_coexists_with_signature(tmp_path: Path) -> None:
    spec, root, waiver = _operator_waived_artifact(tmp_path)
    (root / "cloud-release-manifest.sig.json").write_text("{}", encoding="utf-8")
    spec = deployment.dataclasses.replace(
        spec, unsigned_release_waivers={spec.release_id: _sha(waiver)}
    )
    with pytest.raises(deployment.CloudDeployError, match="authentication_ambiguous"):
        deployment._validate_artifact(spec)

    signed, _ = _signed_artifact(tmp_path / "signed")
    signed = deployment.dataclasses.replace(
        signed, unsigned_release_waivers={signed.release_id: "4" * 64}
    )
    assert deployment._artifact_authentication(signed)["mode"] == "signed"


def test_unlisted_artifact_file_is_rejected(tmp_path: Path) -> None:
    spec, root = _signed_artifact(tmp_path)
    (root / "venv/lib/python3.11/site-packages").mkdir(parents=True)
    (root / "venv/lib/python3.11/site-packages/unsigned.py").write_text(
        "raise SystemExit('unsigned')", encoding="utf-8"
    )

    with pytest.raises(deployment.CloudDeployError, match="artifact_unlisted_file"):
        deployment._validate_artifact(spec)


def test_encryption_flag_is_not_accepted_as_proof(tmp_path: Path) -> None:
    spec, _ = _signed_artifact(tmp_path)
    value = json.loads(spec.encryption_attestation_path.read_text(encoding="utf-8"))
    value["encrypted"] = False
    spec.encryption_attestation_path.write_text(json.dumps(value), encoding="utf-8")
    spec = deployment.dataclasses.replace(
        spec,
        encryption_attestation_sha256=_sha(spec.encryption_attestation_path),
    )

    with pytest.raises(
        deployment.CloudDeployError, match="encryption_attestation_invalid"
    ):
        deployment._validate_attestation(spec)


def test_spec_fences_release_keys_attestation_and_binaries() -> None:
    spec = deployment.CloudDeploymentSpec(
        release_id="ecorex-cloud-v1.0.0-test",
        source_commit="a" * 40,
        dependency_lock_manifest_sha256="b" * 64,
        artifact_root=Path("/tmp/untrusted"),
        artifact_manifest_sha256="0" * 64,
        release_keyring_path=Path("/etc/ecorex/cloud/release-public-keys.json"),
        release_keyring_sha256="1" * 64,
        target_machine_id_sha256="2" * 64,
        encryption_attestation_path=Path(
            "/etc/ecorex/cloud/encrypted-volume-attestation.json"
        ),
        encryption_attestation_sha256="3" * 64,
    )

    with pytest.raises(deployment.CloudDeployError, match="artifact_root_outside_fence"):
        spec.validate()

    fenced = deployment.dataclasses.replace(
        spec,
        artifact_root=Path("/srv/ecorex-upload/ecorex-cloud-v1.0.0-test"),
        nginx_server_config=Path("/etc/nginx/conf.d/another-server.conf"),
    )
    with pytest.raises(
        deployment.CloudDeployError, match="nginx_server_config_outside_fence"
    ):
        fenced.validate()


def test_default_plan_is_read_only_and_uses_storage_contract_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _ = _signed_artifact(tmp_path)
    monkeypatch.setattr(deployment, "_state", lambda: None)
    monkeypatch.setattr(
        deployment, "validate_provider_bridge_materials", lambda: object()
    )
    monkeypatch.setattr(deployment, "_target_preflight", lambda *_args: None)

    plan = deployment.build_plan(spec)

    assert plan.dry_run is True
    assert plan.target_slot == "blue"
    assert plan.blockers == ()
    assert "run_exact_control_plane_and_image_storage_contract_checks" in plan.actions
    assert "validate_and_install_loopback_provider_tls_bridge" in plan.actions
    assert all("minio" not in action for action in plan.actions)


def test_read_only_plan_surfaces_pending_activation_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    monkeypatch.setattr(deployment, "_state", lambda: None)
    monkeypatch.setattr(
        deployment,
        "_transition_journal",
        lambda: {"phase": "state_written"},
    )
    monkeypatch.setattr(deployment, "_target_preflight", lambda *_args: None)

    plan = deployment.build_plan(spec, inspect_files=False)

    assert "activation_recovery_required" in plan.blockers
    assert "recover_incomplete_activation_before_new_mutation" in plan.actions


def test_read_only_plan_executes_real_target_preflight_and_surfaces_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    calls: list[tuple[deployment.CloudDeploymentSpec, str]] = []
    monkeypatch.setattr(deployment, "_state", lambda: None)
    monkeypatch.setattr(deployment, "_transition_journal", lambda: None)

    def preflight(value, confirmation):
        calls.append((value, confirmation))
        raise deployment.CloudDeployError("postgres_service_unavailable")

    monkeypatch.setattr(deployment, "_target_preflight", preflight)

    plan = deployment.build_plan(spec, inspect_files=False)

    assert calls == [(spec, spec.target_machine_id_sha256)]
    assert "postgres_service_unavailable" in plan.blockers


def test_schema_migration_and_contract_gates_are_separated_for_legacy_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release"
    calls: list[tuple[tuple[str, ...], str]] = []
    monkeypatch.setattr(
        deployment, "_service_environment", lambda service, slot: {"SAFE": "value"}
    )

    def run(command, *, code, environment=None, timeout=180.0):
        calls.append((tuple(command), code))
        if command[-2:] == ["schema", "check"] and any(
            "image_orchestrator" in part for part in command
        ):
            raise deployment.CloudDeployError(code)

    monkeypatch.setattr(deployment, "_run", run)

    deployment._schema_gate(release, "blue")

    with pytest.raises(
        deployment.CloudDeployError, match="image_production_contract_failed"
    ):
        deployment._production_contract_gate(release, "blue")

    assert [command[-2:] for command, _ in calls] == [
        ("schema", "migrate"),
        ("schema", "migrate"),
        ("schema", "migrate"),
        ("schema", "check"),
        ("schema", "check"),
        ("schema", "check"),
    ]


def test_recovery_schema_check_covers_all_four_service_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        deployment, "_service_environment", lambda service, _slot: {"SVC": service}
    )
    monkeypatch.setattr(
        deployment,
        "_run_service_command",
        lambda command, **kwargs: calls.append((kwargs["code"], tuple(command))),
    )

    deployment._recovery_schema_check(tmp_path / "release", "blue", source=True)

    assert [code for code, _command in calls] == [
        "control_plane_recovery_schema_incompatible",
        "gateway_recovery_schema_incompatible",
        "image_api_recovery_schema_incompatible",
        "image_worker_recovery_schema_incompatible",
    ]
    assert all(command[-2:] == ("schema", "check") for _code, command in calls)


@pytest.mark.parametrize(
    "source,expected",
    (
        (True, "recovery_source_schema_incompatible"),
        (False, "recovery_target_schema_incompatible"),
    ),
)
def test_recovery_schema_incompatibility_is_directional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: bool,
    expected: str,
) -> None:
    monkeypatch.setattr(
        deployment, "_service_environment", lambda *_args: {"SAFE": "value"}
    )
    monkeypatch.setattr(
        deployment,
        "_run_service_command",
        lambda *_args, **kwargs: (_ for _ in ()).throw(
            deployment.CloudDeployError(kwargs["code"])
        ),
    )
    with pytest.raises(deployment.CloudDeployError, match=expected):
        deployment._recovery_schema_check(
            tmp_path / "release", "blue", source=source
        )


def test_target_apply_requires_exact_machine_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    monkeypatch.setattr(deployment, "_os_release", lambda: {"ID": "alinux", "VERSION_ID": "4"})
    monkeypatch.setattr(deployment.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(deployment, "_machine_id_sha256", lambda: "1" * 64)
    monkeypatch.setattr(deployment, "_effective_user_id", lambda: 0)

    with pytest.raises(
        deployment.CloudDeployError, match="target_machine_fence_mismatch"
    ):
        deployment._target_preflight(spec, "2" * 64)


def test_secret_environment_mode_is_fail_closed(tmp_path: Path) -> None:
    secret = tmp_path / "secret.env"
    secret.write_text("TOKEN=do-not-log\n", encoding="utf-8")
    secret.chmod(0o644)

    with pytest.raises(
        deployment.CloudDeployError, match="secret_environment_permissions_invalid"
    ):
        deployment._parse_env(secret, secret=True)


def test_service_environment_does_not_inherit_operator_shell_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deployment, "CONFIG_ROOT", tmp_path)
    monkeypatch.setattr(deployment, "SECRET_ROOT", tmp_path / "secrets")
    monkeypatch.setattr(
        deployment, "_validate_secret_environment_file", lambda _path: None
    )
    def parse(path: Path, *, secret: bool):
        if secret:
            return {"ECOREX_GATEWAY_PROVIDER_BEARER_TOKEN": "fixed"}
        if "slots" in path.parts:
            return {"ECOREX_GATEWAY_BIND_PORT": "18772"}
        return {"ECOREX_GATEWAY_STORAGE_BACKEND": "sqlite-wal"}

    monkeypatch.setattr(deployment, "_parse_env", parse)
    monkeypatch.setenv("UNRELATED_ROOT_TOKEN", "must-not-propagate")

    environment = deployment._service_environment("gateway", "blue")

    assert "UNRELATED_ROOT_TOKEN" not in environment
    assert environment["ECOREX_GATEWAY_PROVIDER_BEARER_TOKEN"] == "fixed"


def test_local_cas_units_do_not_depend_on_minio_and_secrets_require_mount() -> None:
    root = Path("deploy/ecorex-cloud-sidecar/systemd")
    for name in (
        "ecorex-control-plane@.service",
        "ecorex-gateway@.service",
        "ecorex-image-api@.service",
        "ecorex-image-worker@.service",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "RequiresMountsFor=/var/lib/ecorex" in source
        assert "EnvironmentFile=/var/lib/ecorex/secrets/" in source
        assert "/etc/ecorex/cloud/secrets/" not in source
        assert "minio.service" not in source
    for name in (
        "ecorex-control-plane@.service",
        "ecorex-image-api@.service",
        "ecorex-image-worker@.service",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "/var/lib/ecorex/cas" in source
        assert "SupplementaryGroups=ecorex-storage" in source
        assert "RestrictSUIDSGID=true" not in source
        assert "setgid ecorex-storage directories" in source
    gateway = (root / "ecorex-gateway@.service").read_text(encoding="utf-8")
    assert "RestrictSUIDSGID=true" in gateway
    minio = (root / "minio.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=/var/lib/ecorex/secrets/minio.secret.env" in minio
    assert "/etc/ecorex/cloud/secrets/" not in minio


def test_slot_ports_are_unique_and_image_processes_never_share_a_listener() -> None:
    allocated = [
        port
        for slot_ports in deployment.PORTS.values()
        for port in slot_ports.values()
    ]

    assert len(allocated) == len(set(allocated))
    for slot in deployment.SLOTS:
        ports = deployment.PORTS[slot]
        assert set(ports) == {
            "control_plane",
            "gateway",
            "image",
            "image_worker",
        }
        assert ports["image"] != ports["image_worker"]


def test_image_units_use_separate_slot_environment_files() -> None:
    root = Path("deploy/ecorex-cloud-sidecar/systemd")
    api = (root / "ecorex-image-api@.service").read_text(encoding="utf-8")
    worker = (root / "ecorex-image-worker@.service").read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/ecorex/cloud/slots/%i/image.env" in api
    assert "EnvironmentFile=/etc/ecorex/cloud/slots/%i/image-worker.env" not in api
    assert "EnvironmentFile=/etc/ecorex/cloud/slots/%i/image-worker.env" in worker
    assert "EnvironmentFile=/etc/ecorex/cloud/slots/%i/image.env" not in worker


def test_slot_environment_assigns_distinct_image_api_and_worker_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = tmp_path / "config-root"
    release = tmp_path / "release"
    symlinks: list[tuple[Path, Path]] = []
    monkeypatch.setattr(deployment, "CONFIG_ROOT", config_root)
    monkeypatch.setattr(deployment, "SLOT_ROOT", tmp_path / "slots")
    monkeypatch.setattr(
        deployment, "_prepare_slot_runtime_directory", lambda _slot: None
    )

    def write(path: Path, payload: bytes, mode: int = 0o640) -> None:
        del mode
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    monkeypatch.setattr(deployment, "_atomic_write", write)
    monkeypatch.setattr(
        deployment,
        "_atomic_symlink",
        lambda target, link: symlinks.append((Path(target), Path(link))),
    )

    deployment._write_slot_environment("blue", release)

    control_plane = deployment._parse_env(
        config_root / "slots" / "blue" / "control-plane.env", secret=False
    )
    api = deployment._parse_env(
        config_root / "slots" / "blue" / "image.env", secret=False
    )
    worker = deployment._parse_env(
        config_root / "slots" / "blue" / "image-worker.env", secret=False
    )
    assert api == {
        "ECOREX_IMAGE_BIND_HOST": "127.0.0.1",
        "ECOREX_IMAGE_BIND_PORT": str(deployment.PORTS["blue"]["image"]),
        "ECOREX_IMAGE_INSTANCE_ID": "ecorex-image-blue",
    }
    assert worker == {
        "ECOREX_IMAGE_BIND_HOST": "127.0.0.1",
        "ECOREX_IMAGE_BIND_PORT": str(
            deployment.PORTS["blue"]["image_worker"]
        ),
        "ECOREX_IMAGE_INSTANCE_ID": "ecorex-image-worker-blue",
    }
    assert api["ECOREX_IMAGE_BIND_PORT"] != worker["ECOREX_IMAGE_BIND_PORT"]
    assert control_plane["ECOREX_CP_RELEASE_REPLICA_NAMESPACE"] == (
        f"v{deployment.PRODUCT_VERSION}"
    )
    assert control_plane["ECOREX_CP_RELEASE_REPLICA_PRODUCT_VERSION"] == (
        deployment.PRODUCT_VERSION
    )
    assert symlinks == [(release, tmp_path / "slots" / "blue" / "current")]


def test_v031_release_replica_storage_accepts_legacy_base_version_and_stages_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = tmp_path / "config-root"
    replica_root = tmp_path / "replica"
    replica_root.mkdir()
    public_root = "https://downloads.example.invalid/releases"
    public_config = config_root / "config" / "control-plane.env"
    public_config.parent.mkdir(parents=True)
    public_config.write_text(
        "\n".join(
            (
                "ECOREX_CP_RELEASE_REPLICA_ENABLED=true",
                f"ECOREX_CP_RELEASE_REPLICA_STORAGE_ROOT={replica_root}",
                f"ECOREX_CP_RELEASE_REPLICA_PUBLIC_ROOT={public_root}",
                "ECOREX_CP_RELEASE_REPLICA_NAMESPACE=v0.3.0",
                "ECOREX_CP_RELEASE_REPLICA_PRODUCT_VERSION=0.3.0",
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(deployment, "CONFIG_ROOT", config_root)
    monkeypatch.setattr(deployment, "RELEASE_REPLICA_ROOT", replica_root)
    monkeypatch.setattr(deployment, "RELEASE_REPLICA_PUBLIC_ROOT", public_root)
    monkeypatch.setattr(deployment.shutil, "chown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment.os, "chmod", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment, "_fsync_directory", lambda _path: None)

    deployment._prepare_release_replica_storage()

    namespace = replica_root / f"v{deployment.PRODUCT_VERSION}"
    assert namespace.is_dir()
    assert (namespace / "stable").is_dir()
    assert (namespace / "canary").is_dir()


def test_release_replica_storage_rejects_future_base_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = tmp_path / "config-root"
    replica_root = tmp_path / "replica"
    replica_root.mkdir()
    public_root = "https://downloads.example.invalid/releases"
    public_config = config_root / "config" / "control-plane.env"
    public_config.parent.mkdir(parents=True)
    public_config.write_text(
        "\n".join(
            (
                "ECOREX_CP_RELEASE_REPLICA_ENABLED=true",
                f"ECOREX_CP_RELEASE_REPLICA_STORAGE_ROOT={replica_root}",
                f"ECOREX_CP_RELEASE_REPLICA_PUBLIC_ROOT={public_root}",
                "ECOREX_CP_RELEASE_REPLICA_NAMESPACE=v9.9.9",
                "ECOREX_CP_RELEASE_REPLICA_PRODUCT_VERSION=9.9.9",
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(deployment, "CONFIG_ROOT", config_root)
    monkeypatch.setattr(deployment, "RELEASE_REPLICA_ROOT", replica_root)
    monkeypatch.setattr(deployment, "RELEASE_REPLICA_PUBLIC_ROOT", public_root)

    with pytest.raises(
        deployment.CloudDeployError,
        match="release_replica_configuration_invalid",
    ):
        deployment._prepare_release_replica_storage()


def test_final_health_rechecks_all_endpoints_and_requires_active_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[str] = []
    commands: list[tuple[str, ...]] = []

    class ReadyResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"status":"ready"}'

    def open_ready(request, *, timeout: float):
        assert timeout == 2.0
        requested.append(request.full_url)
        return ReadyResponse()

    def run(command, **_kwargs):
        commands.append(tuple(command))
        return SimpleNamespace(stdout=b"active\n", stderr=b"", returncode=0)

    monkeypatch.setattr(deployment.urllib.request, "urlopen", open_ready)
    monkeypatch.setattr(deployment, "_run", run)
    spec = _spec(tmp_path)

    deployment._wait_health(spec, "blue", timeout_seconds=0.1)

    ports = deployment.PORTS["blue"]
    assert requested == [
        f"http://127.0.0.1:{ports['control_plane']}/health/ready",
        f"http://127.0.0.1:{ports['gateway']}/health/ready",
        f"http://127.0.0.1:{ports['image']}/health/ready",
        f"http://127.0.0.1:{ports['image_worker']}/health/ready",
    ]
    assert commands == [
        (
            str(spec.systemctl_binary),
            "is-active",
            "ecorex-image-worker@blue.service",
        )
    ]


def test_worker_health_polls_only_the_dedicated_worker_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[str] = []

    class ReadyResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"status":"ready"}'

    def open_ready(request, *, timeout: float):
        assert timeout == 2.0
        requested.append(request.full_url)
        return ReadyResponse()

    monkeypatch.setattr(deployment.urllib.request, "urlopen", open_ready)

    deployment._wait_worker_health(
        _spec(tmp_path), "green", timeout_seconds=0.1
    )

    assert requested == [
        "http://127.0.0.1:"
        f"{deployment.PORTS['green']['image_worker']}/health/ready"
    ]


def test_recovery_checks_worker_with_its_dedicated_slot_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environments: list[tuple[str, str]] = []
    checks: list[tuple[tuple[str, ...], str]] = []

    def environment(service: str, slot: str) -> dict[str, str]:
        environments.append((service, slot))
        return {"ECOREX_TEST_ENVIRONMENT": service}

    def run(command, *, environment, **_kwargs):
        checks.append(
            (
                tuple(command),
                environment["ECOREX_TEST_ENVIRONMENT"],
            )
        )
        return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

    monkeypatch.setattr(deployment, "_service_environment", environment)
    monkeypatch.setattr(deployment, "_run_service_command", run)

    deployment._recovery_schema_check(tmp_path / "release", "green", source=False)

    assert environments == [
        ("control-plane", "green"),
        ("gateway", "green"),
        ("image", "green"),
        ("image-worker", "green"),
    ]
    worker_command, worker_environment = checks[-1]
    assert worker_command[-3:] == (
        "ecorex.image_orchestrator.production",
        "schema",
        "check",
    )
    assert worker_environment == "image-worker"


def _legacy_nginx_server() -> str:
    return """server {
    listen 443 ssl;

    location = /ecorex-agent/admin {
        return 301 /ecorex-agent/admin/;
    }

    location ^~ /ecorex-agent/admin/api/ {
        proxy_pass http://127.0.0.1:18084/admin/api/;
    }

    location ^~ /ecorex-agent/api/admin/ {
        proxy_pass http://127.0.0.1:18084/api/admin/;
    }

    location ^~ /ecorex-agent/admin/ {
        auth_basic "EcoreX Admin";
        alias /srv/ecorex-agent-download/current/admin/;
    }

    location = /ecorex-agent/public-bootstrap-index.json {
        alias /srv/ecorex-agent-download/current/public-bootstrap-index.json;
    }

    location ^~ /ecorex-agent/ {
        alias /srv/ecorex-agent-download/current/;
    }
}
"""


def test_session_login_nginx_limits_have_correct_scope_and_route_contract() -> None:
    source = _legacy_nginx_server().encode("utf-8")
    guarded = deployment._with_login_http_limits(source)
    text = guarded.decode("utf-8")
    assert text.startswith(deployment.NGINX_LOGIN_HTTP_LIMITS)
    assert text.index("limit_req_zone ") < text.index("server {")
    assert text.index("limit_conn_zone ") < text.index("server {")
    assert deployment._with_login_http_limits(guarded) == guarded

    routes = Path(
        "deploy/ecorex-cloud-sidecar/nginx/ecorex-cloud.routes.conf"
    ).read_text(encoding="utf-8")
    assert routes.count("location = /v1/session/login {") == 1
    login = routes.split("location = /v1/session/login {", 1)[1].split(
        "\n}", 1
    )[0]
    for directive in (
        "client_max_body_size 64k;",
        "limit_except POST { deny all; }",
        "limit_req zone=ecorex_session_login_per_ip burst=5 nodelay;",
        "limit_req_status 429;",
        "limit_conn ecorex_session_login_conn_per_ip 2;",
        "limit_conn_status 429;",
        "access_log off;",
        "proxy_set_header X-Real-IP $remote_addr;",
        "proxy_set_header X-Forwarded-For $remote_addr;",
        "proxy_request_buffering off;",
        "proxy_buffering off;",
    ):
        assert directive in login
    assert "$proxy_add_x_forwarded_for" not in login
    assert "limit_req_zone " not in routes
    assert "limit_conn_zone " not in routes

    assert routes.count("location = /v1/account/password {") == 1
    password = routes.split("location = /v1/account/password {", 1)[1].split(
        "\n}", 1
    )[0]
    for directive in (
        "client_max_body_size 64k;",
        "limit_except POST { deny all; }",
        "access_log off;",
        "proxy_set_header Authorization $http_authorization;",
        "proxy_request_buffering off;",
        "proxy_buffering off;",
    ):
        assert directive in password

    assert routes.count(
        "location ^~ /ecorex-agent/client/skill-hub/v1/ {"
    ) == 1
    skill_hub = routes.split(
        "location ^~ /ecorex-agent/client/skill-hub/v1/ {", 1
    )[1].split("\n}", 1)[0]
    for directive in (
        "client_max_body_size 100m;",
        "limit_except GET POST { deny all; }",
        "proxy_set_header Authorization $http_authorization;",
        "proxy_request_buffering off;",
        "proxy_buffering off;",
    ):
        assert directive in skill_hub


def test_session_login_nginx_limit_zone_conflicts_fail_closed() -> None:
    conflicting = (
        "limit_req_zone $binary_remote_addr "
        "zone=ecorex_session_login_per_ip:10m rate=999r/s;\nserver {}\n"
    ).encode("utf-8")
    with pytest.raises(
        deployment.CloudDeployError,
        match="nginx_login_limit_wiring_invalid",
    ):
        deployment._with_login_http_limits(conflicting)


@pytest.mark.skipif(os.name == "nt", reason="requires production-style symlinks")
def test_legacy_admin_routes_move_behind_reversible_include(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nginx_root = tmp_path / "ecorex-cloud"
    nginx_root.mkdir()
    server = tmp_path / "ecorex.conf"
    server.write_text(_legacy_nginx_server(), encoding="utf-8")
    server.chmod(0o644)
    state = tmp_path / "state"
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(deployment, "NGINX_ROOT", nginx_root)
    monkeypatch.setattr(deployment, "STATE_ROOT", state)
    download = tmp_path / "download"
    legacy_pointer = download / "current" / "public-bootstrap-index.json"
    target_pointer = download / "public-pointer" / "public-bootstrap-index.json"
    legacy_pointer.parent.mkdir(parents=True)
    target_pointer.parent.mkdir()
    pointer_payload = _unpublished_pointer_bytes()
    legacy_pointer.write_bytes(pointer_payload)
    target_pointer.write_bytes(pointer_payload)
    pointer_digest = hashlib.sha256(pointer_payload).hexdigest()
    legacy_seed = deployment._PublicBootstrapSeedIdentity(
        payload=pointer_payload,
        sha256=pointer_digest,
        size_bytes=len(pointer_payload),
        legacy_exact_route=True,
    )
    target_seed = deployment.dataclasses.replace(
        legacy_seed, legacy_exact_route=False
    )
    monkeypatch.setattr(deployment, "PUBLIC_BOOTSTRAP_ROOT", target_pointer.parent)
    monkeypatch.setattr(deployment, "PUBLIC_BOOTSTRAP_INDEX_PATH", target_pointer)
    monkeypatch.setattr(
        deployment, "LEGACY_PUBLIC_BOOTSTRAP_INDEX_PATH", legacy_pointer
    )
    monkeypatch.setattr(
        deployment,
        "_run",
        lambda command, **_kwargs: calls.append(tuple(command)),
    )

    candidate_source = Path(
        "deploy/ecorex-cloud-sidecar/nginx/admin-route-control-plane.conf"
    ).read_text(encoding="utf-8")
    candidate = nginx_root / "admin-route-control-plane.conf"
    candidate.write_text(candidate_source, encoding="utf-8")
    spec = deployment.dataclasses.replace(
        _spec(tmp_path), nginx_server_config=server
    )

    deployment._install_legacy_admin_route_wiring(
        spec, public_bootstrap_seed=legacy_seed
    )

    migrated = server.read_text(encoding="utf-8")
    legacy = (nginx_root / "admin-route-legacy.conf").read_text(encoding="utf-8")
    assert migrated.count(deployment.NGINX_ROUTE_INCLUDE) == 1
    assert "location ^~ /ecorex-agent/ {" in migrated
    assert deployment.LEGACY_POINTER_LOCATION_HEADER not in migrated
    assert deployment.LEGACY_POINTER_LOCATION_HEADER not in legacy
    for header in deployment.LEGACY_ADMIN_LOCATION_HEADERS:
        assert f"{header} {{" not in migrated
        assert f"{header} {{" in legacy
    assert (nginx_root / "active-admin-route.conf").resolve() == (
        nginx_root / "admin-route-legacy.conf"
    ).resolve()
    assert (state / "nginx-pre-v1.conf").read_text(encoding="utf-8") == _legacy_nginx_server()
    assert calls == [
        ("/usr/sbin/nginx", "-t"),
        ("/usr/bin/systemctl", "reload", "nginx.service"),
    ]

    # An older v1 deployment could already have the include while retaining
    # the old exact location.  Upgrading must retire it before the new cloud
    # fragment owns the only exact location.
    old_pointer = """
    location = /ecorex-agent/public-bootstrap-index.json {
        alias /srv/ecorex-agent-download/current/public-bootstrap-index.json;
    }
"""
    server.write_text(
        migrated.replace("location ^~ /ecorex-agent/ {", old_pointer + "\n    location ^~ /ecorex-agent/ {"),
        encoding="utf-8",
    )
    deployment._install_legacy_admin_route_wiring(
        spec, public_bootstrap_seed=legacy_seed
    )
    assert deployment.LEGACY_POINTER_LOCATION_HEADER not in server.read_text(
        encoding="utf-8"
    )
    assert len(calls) == 4

    deployment._install_legacy_admin_route_wiring(
        spec, public_bootstrap_seed=target_seed
    )
    assert len(calls) == 4

    candidate.write_text("tampered", encoding="utf-8")
    with pytest.raises(
        deployment.CloudDeployError, match="nginx_admin_route_wiring_invalid"
    ):
        deployment._install_legacy_admin_route_wiring(
            spec, public_bootstrap_seed=target_seed
        )
    candidate.write_text(candidate_source, encoding="utf-8")
    external = tmp_path / "external-admin.conf"
    external.write_text(candidate_source, encoding="utf-8")
    (nginx_root / "active-admin-route.conf").unlink()
    (nginx_root / "active-admin-route.conf").symlink_to(external)
    with pytest.raises(
        deployment.CloudDeployError, match="nginx_admin_route_wiring_invalid"
    ):
        deployment._install_legacy_admin_route_wiring(
            spec, public_bootstrap_seed=target_seed
        )


def test_public_bootstrap_storage_is_cp_owned_and_publicly_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    download_root = tmp_path / "download"
    download_root.mkdir()
    pointer_root = download_root / "public-pointer"
    pointer_path = pointer_root / "public-bootstrap-index.json"
    config_root = tmp_path / "config-root"
    config_path = config_root / "config" / "control-plane.env"
    config_path.parent.mkdir(parents=True)
    public_url = "https://dl.ecoremedia.net/ecorex-agent/public-bootstrap-index.json"
    config_path.write_text(
        "\n".join(
            (
                f"ECOREX_CP_PUBLIC_BOOTSTRAP_INDEX_PATH={pointer_path}",
                f"ECOREX_CP_PUBLIC_BOOTSTRAP_INDEX_URL={public_url}",
            )
        ),
        encoding="utf-8",
    )
    ownership: list[tuple[Path, str, str]] = []
    monkeypatch.setattr(deployment, "CONFIG_ROOT", config_root)
    monkeypatch.setattr(deployment, "PUBLIC_BOOTSTRAP_ROOT", pointer_root)
    monkeypatch.setattr(deployment, "PUBLIC_BOOTSTRAP_INDEX_PATH", pointer_path)
    monkeypatch.setattr(deployment, "PUBLIC_BOOTSTRAP_INDEX_URL", public_url)
    monkeypatch.setattr(deployment, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(
        deployment.shutil,
        "chown",
        lambda path, *, user, group: ownership.append((Path(path), user, group)),
    )

    deployment._prepare_public_bootstrap_storage()
    assert pointer_root.is_dir()
    if os.name != "nt":
        assert stat.S_IMODE(pointer_root.stat().st_mode) == 0o755
    assert ownership == [(pointer_root, "ecorex-cloud", "ecorex-storage")]

    pointer_path.write_bytes(b"signed pointer")
    pointer_path.chmod(0o600)
    deployment._prepare_public_bootstrap_storage()
    if os.name != "nt":
        assert stat.S_IMODE(pointer_path.stat().st_mode) == 0o644
    assert ownership[-2:] == [
        (pointer_root, "ecorex-cloud", "ecorex-storage"),
        (pointer_path, "ecorex-cloud", "ecorex-storage"),
    ]


def _unpublished_pointer_bytes() -> bytes:
    return deployment._canonical_json(
        {
            "schema_version": 1,
            "document_type": "ecorex.public-bootstrap-discovery",
            "trust": "untrusted-discovery-hint",
            "status": "unpublished",
            "authority": None,
            "freshness": None,
            "release": None,
        }
    )


def _public_pointer_seed_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[deployment.CloudDeploymentSpec, Path, Path, Path]:
    download = tmp_path / "download"
    legacy_root = download / "current"
    target_root = download / "public-pointer"
    legacy_root.mkdir(parents=True)
    target_root.mkdir()
    legacy = legacy_root / "public-bootstrap-index.json"
    target = target_root / "public-bootstrap-index.json"
    server = tmp_path / "ecorex.conf"
    server.write_text(_legacy_nginx_server(), encoding="utf-8")
    monkeypatch.setattr(deployment, "PUBLIC_BOOTSTRAP_ROOT", target_root)
    monkeypatch.setattr(deployment, "PUBLIC_BOOTSTRAP_INDEX_PATH", target)
    monkeypatch.setattr(deployment, "LEGACY_PUBLIC_BOOTSTRAP_INDEX_PATH", legacy)
    monkeypatch.setattr(deployment, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(deployment.shutil, "chown", lambda *_args, **_kwargs: None)

    def atomic_write(path: Path, payload: bytes, mode: int = 0o640) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.test")
        temporary.write_bytes(payload)
        temporary.chmod(mode)
        os.replace(temporary, path)

    monkeypatch.setattr(deployment, "_atomic_write", atomic_write)
    return (
        deployment.dataclasses.replace(_spec(tmp_path), nginx_server_config=server),
        server,
        legacy,
        target,
    )


def test_first_legacy_exact_route_is_seeded_before_it_can_be_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, server, legacy, target = _public_pointer_seed_fixture(
        tmp_path, monkeypatch
    )
    payload = _unpublished_pointer_bytes()
    legacy.write_bytes(payload)

    seed = deployment._seed_legacy_public_bootstrap_pointer(spec)

    assert seed.sha256 == hashlib.sha256(payload).hexdigest()
    assert seed.payload == payload
    assert seed.legacy_exact_route is True
    assert target.read_bytes() == payload
    assert deployment.LEGACY_POINTER_LOCATION_HEADER in server.read_text(
        encoding="utf-8"
    )


def test_absent_404_route_seeds_canonical_unpublished_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, server, _legacy, target = _public_pointer_seed_fixture(
        tmp_path, monkeypatch
    )
    server.write_bytes(
        deployment._without_legacy_managed_locations(server.read_bytes())
    )
    expected = _unpublished_pointer_bytes() + b"\n"

    seed = deployment._seed_legacy_public_bootstrap_pointer(spec)

    assert seed.payload == expected
    assert seed.sha256 == hashlib.sha256(expected).hexdigest()
    assert seed.size_bytes == len(expected)
    assert seed.legacy_exact_route is False
    assert target.read_bytes() == expected
    deployment._verify_public_bootstrap_seed_before_route_retire(spec, seed)
    target.write_bytes(expected + b" ")
    with pytest.raises(
        deployment.CloudDeployError, match="public_bootstrap_seed_identity_changed"
    ):
        deployment._verify_public_bootstrap_seed_before_route_retire(spec, seed)


def test_managed_nginx_migration_removes_the_nested_legacy_download_location() -> None:
    source = b"""server {
    location ^~ /ecorex-agent/downloads/ {
        types {
            application/zip zip;
        }
    }
}
"""

    migrated = deployment._without_legacy_managed_locations(source)

    assert deployment.LEGACY_DOWNLOAD_LOCATION_HEADER.encode() not in migrated
    assert migrated == b"server {\n}\n"


def test_legacy_source_mutation_between_seed_and_retire_keeps_old_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, server, legacy, target = _public_pointer_seed_fixture(
        tmp_path, monkeypatch
    )
    payload = _unpublished_pointer_bytes()
    legacy.write_bytes(payload)
    seed = deployment._seed_legacy_public_bootstrap_pointer(spec)
    before = server.read_bytes()
    # Whitespace preserves a valid public-index document while changing its
    # exact publication identity after the first stable read.
    legacy.write_bytes(payload + b"\n")
    nginx_root = tmp_path / "nginx"
    nginx_root.mkdir()
    monkeypatch.setattr(deployment, "NGINX_ROOT", nginx_root)
    monkeypatch.setattr(deployment, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(deployment, "_atomic_symlink", lambda *_args: None)
    monkeypatch.setattr(deployment, "_validate_admin_route_resources", lambda: None)

    with pytest.raises(
        deployment.CloudDeployError, match="legacy_public_bootstrap_seed_changed"
    ):
        deployment._install_legacy_admin_route_wiring(
            spec, public_bootstrap_seed=seed
        )

    assert server.read_bytes() == before
    assert target.read_bytes() == payload


@pytest.mark.parametrize(
    "payload,expected",
    (
        (None, "legacy_public_bootstrap_seed_unavailable"),
        (b"not-json", "legacy_public_bootstrap_seed_invalid"),
    ),
)
def test_missing_or_invalid_legacy_seed_fails_before_exact_route_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes | None,
    expected: str,
) -> None:
    spec, server, legacy, target = _public_pointer_seed_fixture(
        tmp_path, monkeypatch
    )
    before = server.read_bytes()
    if payload is not None:
        legacy.write_bytes(payload)

    with pytest.raises(deployment.CloudDeployError, match=expected):
        deployment._seed_legacy_public_bootstrap_pointer(spec)

    assert server.read_bytes() == before
    assert not target.exists()


def test_existing_different_public_pointer_fails_closed_without_two_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, server, legacy, target = _public_pointer_seed_fixture(
        tmp_path, monkeypatch
    )
    source = _unpublished_pointer_bytes()
    competing = source + b"\n"
    legacy.write_bytes(source)
    target.write_bytes(competing)
    before = server.read_bytes()

    with pytest.raises(deployment.CloudDeployError, match="public_bootstrap_seed_conflict"):
        deployment._seed_legacy_public_bootstrap_pointer(spec)

    assert server.read_bytes() == before
    assert legacy.read_bytes() == source
    assert target.read_bytes() == competing


def test_template_failure_after_seed_keeps_legacy_public_pointer_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, server, legacy, target = _public_pointer_seed_fixture(
        tmp_path, monkeypatch
    )
    payload = _unpublished_pointer_bytes()
    legacy.write_bytes(payload)
    release = tmp_path / "release"
    for directory, names in (
        (
            release / "deployment" / "systemd",
            (
                "ecorex-control-plane@.service",
                "ecorex-gateway@.service",
                "ecorex-image-api@.service",
                "ecorex-image-worker@.service",
            ),
        ),
        (
            release / "deployment" / "nginx",
            (
                "control-plane-blue.conf",
                "control-plane-green.conf",
                "control-plane-disabled.conf",
                "admin-route-control-plane.conf",
                "ecorex-cloud.routes.conf",
            ),
        ),
    ):
        directory.mkdir(parents=True)
        for name in names:
            (directory / name).write_text(name, encoding="utf-8")
    monkeypatch.setattr(deployment, "SYSTEMD_ROOT", tmp_path / "systemd")
    monkeypatch.setattr(deployment, "NGINX_ROOT", tmp_path / "nginx")
    monkeypatch.setattr(deployment, "_prepare_release_replica_storage", lambda: None)
    monkeypatch.setattr(deployment, "_prepare_public_bootstrap_storage", lambda: None)
    monkeypatch.setattr(deployment, "_install_publication_keyring", lambda _spec: None)
    monkeypatch.setattr(
        deployment, "validate_provider_bridge_materials", lambda: object()
    )
    monkeypatch.setattr(deployment, "install_provider_bridge", lambda _value: None)
    monkeypatch.setattr(deployment, "_atomic_symlink", lambda *_args: None)
    monkeypatch.setattr(
        deployment,
        "_install_legacy_admin_route_wiring",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            deployment.CloudDeployError("nginx_configuration_invalid")
        ),
    )

    with pytest.raises(deployment.CloudDeployError, match="nginx_configuration_invalid"):
        deployment._install_deployment_templates(spec, release)

    assert target.read_bytes() == payload
    assert legacy.read_bytes() == payload
    assert deployment.LEGACY_POINTER_LOCATION_HEADER in server.read_text(
        encoding="utf-8"
    )


def test_candidate_failure_compensation_preserves_seeded_pointer_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _server, legacy, target = _public_pointer_seed_fixture(
        tmp_path, monkeypatch
    )
    payload = _unpublished_pointer_bytes()
    legacy.write_bytes(payload)
    deployment._seed_legacy_public_bootstrap_pointer(spec)
    journal = {
        "operation": "activate",
        "phase": "migrating",
        "source_state": None,
        "target_state": _slot_state("ecorex-cloud-v1.0.0-target", "blue"),
    }
    monkeypatch.setattr(
        deployment, "_classify_transition_routes", lambda _journal: "source"
    )
    monkeypatch.setattr(deployment, "_stop_transition_writers", lambda *_args: None)
    monkeypatch.setattr(deployment, "_start_target_services", lambda *_args: None)
    monkeypatch.setattr(deployment, "_switch_nginx_legacy", lambda *_args: None)
    monkeypatch.setattr(deployment, "_remove_slot_projection", lambda: None)
    monkeypatch.setattr(deployment, "_clear_transition_journal", lambda: None)

    assert deployment._compensate_transition(spec, journal) == "source"
    assert target.read_bytes() == payload


def test_publication_keyring_is_materialized_with_separate_trust_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path)
    release_public = base64.b64encode(b"r" * 32).decode("ascii")
    publication_public = base64.b64encode(b"p" * 32).decode("ascii")
    release_ring = {"release-key": release_public}
    publication_ring = {"publication-key": publication_public}
    spec.release_keyring_path.write_text(json.dumps(release_ring), encoding="utf-8")
    target = tmp_path / "publication-public-keys.json"
    monkeypatch.setattr(deployment, "PUBLICATION_KEYRING_PATH", target)
    monkeypatch.setattr(
        deployment,
        "_atomic_write",
        lambda path, payload, mode=0o640: Path(path).write_bytes(payload),
    )
    monkeypatch.setattr(
        deployment,
        "_parse_env",
        lambda *_args, **_kwargs: {
            "ECOREX_CP_RELEASE_PUBLIC_KEYS_JSON": json.dumps(release_ring),
            "ECOREX_CP_PUBLICATION_PUBLIC_KEYS_JSON": json.dumps(publication_ring),
        },
    )

    deployment._install_publication_keyring(spec)
    assert json.loads(target.read_text(encoding="utf-8")) == publication_ring

    monkeypatch.setattr(
        deployment,
        "_parse_env",
        lambda *_args, **_kwargs: {
            "ECOREX_CP_RELEASE_PUBLIC_KEYS_JSON": json.dumps(release_ring),
            "ECOREX_CP_PUBLICATION_PUBLIC_KEYS_JSON": json.dumps(
                {"publication-key": release_public}
            ),
        },
    )
    with pytest.raises(
        deployment.CloudDeployError,
        match="public_pointer_trust_roles_overlap",
    ):
        deployment._install_publication_keyring(spec)


def test_nginx_wiring_requires_one_dynamic_pointer_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = b"""
include /etc/nginx/ecorex-cloud/active-control-plane.conf;
include /etc/nginx/ecorex-cloud/active-admin-route.conf;
location ^~ /ecorex-agent/admin/ {}
location = /ecorex-agent/public-bootstrap-index.json {
    alias /srv/ecorex-agent-download/public-pointer/public-bootstrap-index.json;
}
"""
    monkeypatch.setattr(
        deployment,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=required),
    )
    deployment._verify_nginx_wiring(_spec(tmp_path))

    for invalid in (
        required
        + b"\nlocation = /ecorex-agent/public-bootstrap-index.json {}\n",
        required.replace(b"public-pointer", b"current"),
    ):
        monkeypatch.setattr(
            deployment,
            "_run",
            lambda *_args, payload=invalid, **_kwargs: SimpleNamespace(
                stdout=payload
            ),
        )
        with pytest.raises(deployment.CloudDeployError, match="nginx_route_not_wired"):
            deployment._verify_nginx_wiring(_spec(tmp_path))


@pytest.mark.skipif(os.name == "nt", reason="requires production-style symlinks")
@pytest.mark.parametrize(
    "needle,replacement",
    (
        (
            "location = /ecorex-agent/admin/health/ready {",
            "location = /ecorex-agent/admin/health/live {",
        ),
        (
            "rewrite ^/ecorex-agent/admin/(.*)$ /admin/$1 break;",
            "rewrite ^/ecorex-agent/admin/(.*)$ /admin/$1 last;",
        ),
        (
            "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "proxy_set_header X-Forwarded-For $http_x_forged_for;",
        ),
    ),
)
def test_admin_route_contract_rejects_location_directive_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    needle: str,
    replacement: str,
) -> None:
    nginx_root = tmp_path / "ecorex-cloud"
    nginx_root.mkdir()
    _, legacy = deployment._legacy_admin_route_payload(
        _legacy_nginx_server().encode("utf-8")
    )
    legacy_path = nginx_root / "admin-route-legacy.conf"
    legacy_path.write_bytes(legacy)
    candidate_path = nginx_root / "admin-route-control-plane.conf"
    candidate = Path(
        "deploy/ecorex-cloud-sidecar/nginx/admin-route-control-plane.conf"
    ).read_text(encoding="utf-8")
    assert needle in candidate
    candidate_path.write_text(
        candidate.replace(needle, replacement, 1), encoding="utf-8"
    )
    (nginx_root / "active-admin-route.conf").symlink_to(candidate_path)
    monkeypatch.setattr(deployment, "NGINX_ROOT", nginx_root)

    with pytest.raises(
        deployment.CloudDeployError, match="nginx_admin_route_wiring_invalid"
    ):
        deployment._validate_admin_route_resources()


@pytest.mark.skipif(os.name == "nt", reason="requires production-style symlinks")
def test_nginx_candidate_switch_moves_both_routes_after_health_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nginx_root = tmp_path / "ecorex-cloud"
    nginx_root.mkdir()
    for name in (
        "control-plane-disabled.conf",
        "control-plane-blue.conf",
    ):
        (nginx_root / name).write_text(name, encoding="utf-8")
    _, legacy = deployment._legacy_admin_route_payload(
        _legacy_nginx_server().encode("utf-8")
    )
    (nginx_root / "admin-route-legacy.conf").write_bytes(legacy)
    (nginx_root / "admin-route-control-plane.conf").write_text(
        Path(
            "deploy/ecorex-cloud-sidecar/nginx/admin-route-control-plane.conf"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (nginx_root / "active-control-plane.conf").symlink_to(
        nginx_root / "control-plane-disabled.conf"
    )
    (nginx_root / "active-admin-route.conf").symlink_to(
        nginx_root / "admin-route-legacy.conf"
    )
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(deployment, "NGINX_ROOT", nginx_root)
    monkeypatch.setattr(
        deployment,
        "_run",
        lambda command, **_kwargs: calls.append(tuple(command)),
    )

    deployment._switch_nginx(_spec(tmp_path), "blue")

    assert (nginx_root / "active-control-plane.conf").resolve() == (
        nginx_root / "control-plane-blue.conf"
    ).resolve()
    assert (nginx_root / "active-admin-route.conf").resolve() == (
        nginx_root / "admin-route-control-plane.conf"
    ).resolve()
    assert calls == [
        ("/usr/sbin/nginx", "-t"),
        ("/usr/bin/systemctl", "reload", "nginx.service"),
    ]


def test_deploy_does_not_switch_admin_route_before_candidate_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    release = tmp_path / "release"
    events: list[str] = []
    monkeypatch.setattr(deployment.CloudDeploymentSpec, "validate", lambda _self: None)
    monkeypatch.setattr(deployment, "_target_preflight", lambda *_args: None)
    monkeypatch.setattr(deployment, "_validate_artifact", lambda _spec: {})
    monkeypatch.setattr(deployment, "_validate_attestation", lambda _spec: None)
    monkeypatch.setattr(
        deployment,
        "_deployment_lock",
        lambda: deployment.contextlib.nullcontext(),
    )
    monkeypatch.setattr(deployment, "_recover_pending_transition", lambda *_args: None)
    monkeypatch.setattr(deployment, "_state", lambda: None)
    monkeypatch.setattr(deployment, "_install_release", lambda *_args: release)
    monkeypatch.setattr(deployment, "_install_deployment_templates", lambda *_args: None)
    monkeypatch.setattr(deployment, "_write_slot_environment", lambda *_args: None)
    monkeypatch.setattr(deployment, "_verify_staged_runtime", lambda *_args: None)
    monkeypatch.setattr(deployment, "_verify_nginx_wiring", lambda *_args: None)
    monkeypatch.setattr(deployment, "_schema_gate", lambda *_args: None)
    monkeypatch.setattr(deployment, "_systemctl", lambda *_args: None)
    monkeypatch.setattr(
        deployment,
        "_start_target_services",
        lambda *_args: events.append("candidate_healthy"),
    )

    def switch(*_args) -> None:
        assert events == ["candidate_healthy"]
        events.append("routes_switched")

    monkeypatch.setattr(deployment, "_switch_nginx", switch)
    monkeypatch.setattr(
        deployment,
        "_new_transition_journal",
        lambda **_kwargs: {"phase": "prepared"},
    )
    monkeypatch.setattr(
        deployment,
        "_advance_transition_journal",
        lambda journal, phase: {**journal, "phase": phase},
    )
    monkeypatch.setattr(deployment, "_clear_transition_journal", lambda: None)
    monkeypatch.setattr(deployment, "_fsync_directory", lambda *_args: None)
    monkeypatch.setattr(deployment, "_atomic_write", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment, "_atomic_symlink", lambda *_args: None)

    deployment.deploy(spec, confirmation="1" * 64)

    assert events == ["candidate_healthy", "routes_switched"]


def test_stage_health_checks_inactive_slot_without_switching_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    release = tmp_path / "release"
    prior = _slot_state("ecorex-cloud-v1.0.0-source", "blue")
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(deployment.CloudDeploymentSpec, "validate", lambda _self: None)
    monkeypatch.setattr(deployment, "_target_preflight", lambda *_args: None)
    monkeypatch.setattr(deployment, "_validate_attestation", lambda _spec: None)
    monkeypatch.setattr(
        deployment,
        "_deployment_lock",
        lambda: deployment.contextlib.nullcontext(),
    )
    monkeypatch.setattr(deployment, "_transition_journal", lambda: None)
    monkeypatch.setattr(deployment, "_validate_artifact", lambda _spec: {})
    monkeypatch.setattr(deployment, "_state", lambda: prior)
    monkeypatch.setattr(deployment, "_validate_legacy_migration_plan", lambda *_args: None)
    monkeypatch.setattr(deployment, "_install_release", lambda *_args: release)
    monkeypatch.setattr(deployment, "_install_deployment_templates", lambda *_args: None)
    monkeypatch.setattr(
        deployment, "_write_slot_environment", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(deployment, "_verify_staged_runtime", lambda *_args: None)
    monkeypatch.setattr(deployment, "_verify_nginx_wiring", lambda *_args: None)
    monkeypatch.setattr(
        deployment,
        "_isolated_stage_environment",
        lambda _slot: deployment.contextlib.nullcontext(
            {"control-plane": {"ECOREX_CP_DATABASE_PATH": "/stage/control.sqlite3"}}
        ),
    )
    monkeypatch.setattr(
        deployment,
        "_schema_gate",
        lambda _release, _slot, *, services: events.append(
            ("migrate", tuple(services))
        ),
    )
    monkeypatch.setattr(deployment, "_recovery_schema_check", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment, "_slot_api_units", lambda _slot: ("api-a", "api-b"))
    monkeypatch.setattr(
        deployment, "_slot_units", lambda _slot: ("api-a", "api-b", "worker")
    )
    monkeypatch.setattr(deployment, "_prepare_slot_runtime_directory", lambda *_args: None)
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, action, units: events.append((action, tuple(units))),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_api_health",
        lambda _spec, slot: events.append(("health", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_switch_nginx",
        lambda *_args: (_ for _ in ()).throw(AssertionError("route switch forbidden")),
    )

    receipt = deployment.stage(spec, confirmation="1" * 64)

    assert receipt["status"] == "staged"
    assert receipt["target_slot"] == "green"
    assert receipt["active_release_id"] == prior["active_release_id"]
    assert receipt["live_routes_changed"] is False
    assert events == [
        ("stop", ("worker", "api-b", "api-a")),
        ("migrate", ("control-plane", "gateway")),
        ("start", ("api-a", "api-b")),
        ("health", "green"),
        ("is-active", ("api-a", "api-b")),
        ("stop", ("worker", "api-b", "api-a")),
    ]


def test_rollback_target_stage_persists_exact_four_role_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _ = _signed_artifact(tmp_path, version="2.0.4")
    state_root = tmp_path / "state"
    release = tmp_path / "installed" / spec.release_id
    release.mkdir(parents=True)
    current = _slot_state(
        "ecorex-cloud-v2.0.5-active",
        "green",
        previous_target_type="slot",
        previous_release_id="ecorex-cloud-v2.0.4-original",
        previous_slot="blue",
    )
    checked: list[tuple[Path, str]] = []
    monkeypatch.setattr(deployment, "STATE_ROOT", state_root)
    monkeypatch.setattr(deployment.CloudDeploymentSpec, "validate", lambda _self: None)
    monkeypatch.setattr(deployment, "_target_preflight", lambda *_args: None)
    monkeypatch.setattr(deployment, "_validate_attestation", lambda *_args: None)
    monkeypatch.setattr(
        deployment, "_deployment_lock", lambda: deployment.contextlib.nullcontext()
    )
    monkeypatch.setattr(deployment, "_transition_journal", lambda: None)
    monkeypatch.setattr(deployment, "_state", lambda: current)
    monkeypatch.setattr(
        deployment,
        "_validate_artifact",
        lambda _spec, *, historical_release=False: {
            "version": "2.0.4",
            "source_commit": spec.source_commit,
            "dependency_lock_manifest_sha256": spec.dependency_lock_manifest_sha256,
        }
        if historical_release
        else pytest.fail("ordinary admission used for rollback artifact"),
    )
    monkeypatch.setattr(
        deployment,
        "_install_release",
        lambda _spec, _manifest, *, historical_release=False: release
        if historical_release
        else pytest.fail("rollback artifact installed as ordinary candidate"),
    )
    monkeypatch.setattr(
        deployment, "_write_slot_environment", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(deployment, "_verify_staged_runtime", lambda *_args: None)
    monkeypatch.setattr(deployment, "_verify_nginx_wiring", lambda *_args: None)
    monkeypatch.setattr(deployment, "_verify_staged_slot_release", lambda *_args: None)
    monkeypatch.setattr(
        deployment,
        "_check_rollback_target_schema",
        lambda root, slot, **_kwargs: checked.append((root, slot)),
    )

    receipt = deployment.stage_rollback_target(
        spec, confirmation=spec.target_machine_id_sha256
    )

    assert checked == [(release, "blue")]
    assert receipt["schema_roles"] == {
        "control-plane": "passed",
        "gateway": "passed",
        "image-api": "passed",
        "image-worker": "passed",
    }
    durable = deployment._rollback_stage_receipt_path(spec.release_id)
    assert durable.read_bytes() == deployment._canonical_json(receipt) + b"\n"


def test_replace_previous_target_changes_only_rollback_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _ = _signed_artifact(tmp_path, version="2.0.4")
    state_root = tmp_path / "state"
    state_root.mkdir()
    release = tmp_path / "installed" / spec.release_id
    release.mkdir(parents=True)
    current = _slot_state(
        "ecorex-cloud-v2.0.5-active",
        "green",
        previous_target_type="slot",
        previous_release_id="ecorex-cloud-v2.0.4-original",
        previous_slot="blue",
    )
    (state_root / "active.json").write_bytes(deployment._canonical_json(current) + b"\n")
    forbidden: list[str] = []
    monkeypatch.setattr(deployment, "STATE_ROOT", state_root)
    monkeypatch.setattr(deployment.CloudDeploymentSpec, "validate", lambda _self: None)
    monkeypatch.setattr(deployment, "_target_preflight", lambda *_args: None)
    monkeypatch.setattr(deployment, "_validate_attestation", lambda *_args: None)
    monkeypatch.setattr(
        deployment, "_deployment_lock", lambda: deployment.contextlib.nullcontext()
    )
    monkeypatch.setattr(deployment, "_transition_journal", lambda: None)
    monkeypatch.setattr(
        deployment,
        "_validate_artifact",
        lambda _spec, *, historical_release=False: {
            "version": "2.0.4",
            "source_commit": spec.source_commit,
            "dependency_lock_manifest_sha256": spec.dependency_lock_manifest_sha256,
        }
        if historical_release
        else pytest.fail("ordinary candidate admission used"),
    )
    monkeypatch.setattr(deployment, "_verify_transition_release", lambda *_args: release)
    monkeypatch.setattr(deployment, "_verify_staged_slot_release", lambda *_args: None)
    monkeypatch.setattr(
        deployment, "_check_rollback_target_schema", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        deployment, "_systemctl", lambda *_args: forbidden.append("systemctl")
    )
    monkeypatch.setattr(
        deployment, "_switch_nginx", lambda *_args: forbidden.append("nginx")
    )
    deployment._write_rollback_stage_receipt(
        deployment._rollback_stage_receipt(
            spec=spec,
            manifest={
                "version": "2.0.4",
                "source_commit": spec.source_commit,
                "dependency_lock_manifest_sha256": spec.dependency_lock_manifest_sha256,
            },
            current=current,
            target_slot="blue",
            release=release,
        )
    )

    receipt = deployment.replace_previous_target(
        spec, confirmation=spec.target_machine_id_sha256
    )
    updated = json.loads((state_root / "active.json").read_bytes())

    assert forbidden == []
    immutable = set(current) - {
        "previous_target_type",
        "previous_release_id",
        "previous_slot",
    }
    assert {key: updated[key] for key in immutable} == {
        key: current[key] for key in immutable
    }
    assert updated["previous_release_id"] == spec.release_id
    assert receipt["status"] == "committed"
    assert (state_root / "rollback-target-receipts" / receipt["receipt_file"]).is_file()


def test_replace_previous_target_rejects_unstaged_original_204(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _ = _signed_artifact(tmp_path, version="2.0.4")
    state_root = tmp_path / "state"
    state_root.mkdir()
    current = _slot_state(
        "ecorex-cloud-v2.0.5-active",
        "green",
        previous_target_type="slot",
        previous_release_id="ecorex-cloud-v2.0.4-original",
        previous_slot="blue",
    )
    original = deployment._canonical_json(current) + b"\n"
    (state_root / "active.json").write_bytes(original)
    monkeypatch.setattr(deployment, "STATE_ROOT", state_root)
    monkeypatch.setattr(deployment.CloudDeploymentSpec, "validate", lambda _self: None)
    monkeypatch.setattr(deployment, "_target_preflight", lambda *_args: None)
    monkeypatch.setattr(deployment, "_validate_attestation", lambda *_args: None)
    monkeypatch.setattr(
        deployment, "_deployment_lock", lambda: deployment.contextlib.nullcontext()
    )
    monkeypatch.setattr(deployment, "_transition_journal", lambda: None)

    with pytest.raises(deployment.CloudDeployError, match="rollback_target_not_staged"):
        deployment.replace_previous_target(
            spec, confirmation=spec.target_machine_id_sha256
        )

    assert (state_root / "active.json").read_bytes() == original


def test_replace_previous_target_restores_state_when_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _ = _signed_artifact(tmp_path, version="2.0.4")
    state_root = tmp_path / "state"
    state_root.mkdir()
    release = tmp_path / "installed" / spec.release_id
    release.mkdir(parents=True)
    current = _slot_state(
        "ecorex-cloud-v2.0.5-active",
        "green",
        previous_target_type="slot",
        previous_release_id="ecorex-cloud-v2.0.4-original",
        previous_slot="blue",
    )
    original = deployment._canonical_json(current) + b"\n"
    (state_root / "active.json").write_bytes(original)
    monkeypatch.setattr(deployment, "STATE_ROOT", state_root)
    monkeypatch.setattr(deployment.CloudDeploymentSpec, "validate", lambda _self: None)
    monkeypatch.setattr(deployment, "_target_preflight", lambda *_args: None)
    monkeypatch.setattr(deployment, "_validate_attestation", lambda *_args: None)
    monkeypatch.setattr(
        deployment, "_deployment_lock", lambda: deployment.contextlib.nullcontext()
    )
    monkeypatch.setattr(deployment, "_transition_journal", lambda: None)
    monkeypatch.setattr(
        deployment,
        "_validate_artifact",
        lambda _spec, *, historical_release=False: {
            "version": "2.0.4",
            "source_commit": spec.source_commit,
            "dependency_lock_manifest_sha256": spec.dependency_lock_manifest_sha256,
        },
    )
    monkeypatch.setattr(deployment, "_verify_transition_release", lambda *_args: release)
    monkeypatch.setattr(deployment, "_verify_staged_slot_release", lambda *_args: None)
    monkeypatch.setattr(
        deployment, "_check_rollback_target_schema", lambda *_args, **_kwargs: None
    )
    deployment._write_rollback_stage_receipt(
        deployment._rollback_stage_receipt(
            spec=spec,
            manifest={
                "version": "2.0.4",
                "source_commit": spec.source_commit,
                "dependency_lock_manifest_sha256": spec.dependency_lock_manifest_sha256,
            },
            current=current,
            target_slot="blue",
            release=release,
        )
    )
    writes = 0
    original_atomic_write = deployment._atomic_write

    def fail_after_state_replace(path: Path, payload: bytes, mode: int = 0o640) -> None:
        nonlocal writes
        original_atomic_write(path, payload, mode)
        if path == state_root / "active.json":
            writes += 1
            if writes == 1:
                raise OSError("simulated post-replace failure")

    monkeypatch.setattr(deployment, "_atomic_write", fail_after_state_replace)

    with pytest.raises(deployment.CloudDeployError, match="replacement_commit_failed"):
        deployment.replace_previous_target(
            spec, confirmation=spec.target_machine_id_sha256
        )

    assert (state_root / "active.json").read_bytes() == original
    assert not list((state_root / "rollback-target-receipts").glob("*.json"))


def test_stage_environment_clones_sqlite_and_removes_disposable_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = tmp_path / "control-plane" / "live.sqlite3"
    gateway = tmp_path / "gateway" / "live.sqlite3"
    production_backups = tmp_path / "control-plane-backups"
    production_backups.mkdir()
    production_backup = production_backups / "production-only"
    production_backup.write_bytes(b"do-not-touch")
    for database, value in ((control, "control"), (gateway, "gateway")):
        database.parent.mkdir()
        with deployment.sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE records(value TEXT NOT NULL)")
            connection.execute("INSERT INTO records VALUES(?)", (value,))
    source_identity = (control.stat().st_ino, _sha(control))
    production_backup_identity = (
        production_backup.stat().st_ino,
        _sha(production_backup),
    )

    monkeypatch.setattr(deployment, "ENCRYPTED_VOLUME_ROOT", tmp_path)
    monkeypatch.setattr(deployment.shutil, "chown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(
        deployment,
        "_service_environment",
        lambda service, _slot: {
            "ECOREX_CP_DATABASE_PATH": str(control),
            "ECOREX_CP_BACKUP_DIRECTORY": str(production_backups),
            "ECOREX_CP_STORAGE_VOLUME_ID": "stage-volume",
        }
        if service == "control-plane"
        else {"ECOREX_GATEWAY_DATABASE_PATH": str(gateway)},
    )

    staged_directories: tuple[Path, Path]
    with pytest.raises(RuntimeError, match="simulated stage failure"):
        with deployment._isolated_stage_environment("blue") as overrides:
            staged_control = Path(
                overrides["control-plane"]["ECOREX_CP_DATABASE_PATH"]
            )
            staged_gateway = Path(
                overrides["gateway"]["ECOREX_GATEWAY_DATABASE_PATH"]
            )
            staged_directories = staged_control.parent, staged_gateway.parent
            assert overrides["gateway"][
                "ECOREX_GATEWAY_ADMIN_MANAGEMENT_DATABASE_PATH"
            ] == str(staged_control)
            assert overrides["image"][
                "ECOREX_IMAGE_ADMIN_MANAGEMENT_DATABASE_PATH"
            ] == str(staged_control)
            staged_backups = Path(
                overrides["control-plane"]["ECOREX_CP_BACKUP_DIRECTORY"]
            )
            empty_backups = staged_control.parent / "empty-backups"
            empty_backups.mkdir()
            with pytest.raises(ProductionStorageError, match="backup is missing"):
                SQLiteBackupManager(
                    staged_control,
                    empty_backups,
                    volume_id="stage-volume",
                ).latest(full_digest=True)
            staged_receipt = SQLiteBackupManager(
                staged_control,
                staged_backups,
                volume_id="stage-volume",
            ).latest(full_digest=True)
            assert staged_receipt.database_sha256 == _sha(
                staged_backups / f"{staged_receipt.backup_id}.sqlite3"
            )
            PersistentVolumeGuard(
                staged_control, volume_id="stage-volume"
            ).validate_wal()
            with deployment.sqlite3.connect(staged_control) as connection:
                assert connection.execute("SELECT value FROM records").fetchone() == (
                    "control",
                )
                connection.execute("INSERT INTO records VALUES('stage-only')")
            with deployment.sqlite3.connect(staged_gateway) as connection:
                assert connection.execute("SELECT value FROM records").fetchone() == (
                    "gateway",
                )
            with deployment.sqlite3.connect(control) as connection:
                assert connection.execute("SELECT COUNT(*) FROM records").fetchone() == (
                    1,
                )
            raise RuntimeError("simulated stage failure")

    assert all(not directory.exists() for directory in staged_directories)
    assert (control.stat().st_ino, _sha(control)) == source_identity
    assert (
        production_backup.stat().st_ino,
        _sha(production_backup),
    ) == production_backup_identity


def test_stage_backup_and_clone_tampering_still_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = tmp_path / "control-plane" / "live.sqlite3"
    gateway = tmp_path / "gateway" / "live.sqlite3"
    control.parent.mkdir()
    gateway.parent.mkdir()
    ControlPlaneSchemaManager(control).migrate()
    with deployment.sqlite3.connect(gateway) as connection:
        connection.execute("CREATE TABLE records(value TEXT NOT NULL)")

    monkeypatch.setattr(deployment, "ENCRYPTED_VOLUME_ROOT", tmp_path)
    monkeypatch.setattr(deployment.shutil, "chown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(
        deployment,
        "_service_environment",
        lambda service, _slot: {
            "ECOREX_CP_DATABASE_PATH": str(control),
            "ECOREX_CP_STORAGE_VOLUME_ID": "stage-volume",
        }
        if service == "control-plane"
        else {"ECOREX_GATEWAY_DATABASE_PATH": str(gateway)},
    )

    with deployment._isolated_stage_environment("blue") as overrides:
        staged_control = Path(overrides["control-plane"]["ECOREX_CP_DATABASE_PATH"])
        staged_backups = Path(
            overrides["control-plane"]["ECOREX_CP_BACKUP_DIRECTORY"]
        )
        manager = SQLiteBackupManager(
            staged_control,
            staged_backups,
            volume_id="stage-volume",
        )
        receipt = manager.latest(full_digest=True)
        manifest = staged_backups / f"{receipt.backup_id}.json"
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        with pytest.raises(ProductionStorageError, match="receipt is invalid"):
            manager.latest(full_digest=True)

        with deployment.sqlite3.connect(staged_control) as connection:
            connection.execute("CREATE TABLE control_unsafe_state(value TEXT)")
        with pytest.raises(ControlPlaneSchemaError, match="fingerprint"):
            ControlPlaneSchemaManager(staged_control).validate()


def test_rollback_schema_gate_runs_all_roles_with_verified_clone_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = tmp_path / "control-plane" / "live.sqlite3"
    gateway = tmp_path / "gateway" / "live.sqlite3"
    for database in (control, gateway):
        database.parent.mkdir()
        with deployment.sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE records(value TEXT NOT NULL)")
    staged: dict[str, dict[str, str]] = {}
    calls: list[str] = []

    def service_environment(service: str, _slot: str) -> dict[str, str]:
        base = (
            {
                "ECOREX_CP_DATABASE_PATH": str(control),
                "ECOREX_CP_STORAGE_VOLUME_ID": "stage-volume",
            }
            if service == "control-plane"
            else {"ECOREX_GATEWAY_DATABASE_PATH": str(gateway)}
            if service == "gateway"
            else {}
        )
        return {**base, **staged.get(service, {})}

    def write_environment(
        _slot: str,
        _release: Path,
        *,
        overrides: dict[str, dict[str, str]] | None = None,
        **_kwargs,
    ) -> None:
        staged.clear()
        if overrides is not None:
            staged.update(overrides)

    def run_service(_command, *, code, environment, **_kwargs) -> None:
        if code == "control_plane_recovery_schema_incompatible":
            backup = SQLiteBackupManager(
                Path(environment["ECOREX_CP_DATABASE_PATH"]),
                Path(environment["ECOREX_CP_BACKUP_DIRECTORY"]),
                volume_id=environment["ECOREX_CP_STORAGE_VOLUME_ID"],
            )
            backup.latest(full_digest=True)
        calls.append(code)

    monkeypatch.setattr(deployment, "ENCRYPTED_VOLUME_ROOT", tmp_path)
    monkeypatch.setattr(deployment.shutil, "chown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(deployment, "_service_environment", service_environment)
    monkeypatch.setattr(deployment, "_write_slot_environment", write_environment)
    monkeypatch.setattr(deployment, "_run_service_command", run_service)

    deployment._check_rollback_target_schema(tmp_path / "release", "blue")

    assert calls == [
        "control_plane_recovery_schema_incompatible",
        "gateway_recovery_schema_incompatible",
        "image_api_recovery_schema_incompatible",
        "image_worker_recovery_schema_incompatible",
    ]


@pytest.mark.skipif(os.name == "nt", reason="requires production-style symlinks")
def test_nginx_candidate_switch_restores_legacy_admin_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nginx_root = tmp_path / "ecorex-cloud"
    nginx_root.mkdir()
    for name in (
        "control-plane-disabled.conf",
        "control-plane-blue.conf",
    ):
        (nginx_root / name).write_text(name, encoding="utf-8")
    _, legacy = deployment._legacy_admin_route_payload(
        _legacy_nginx_server().encode("utf-8")
    )
    (nginx_root / "admin-route-legacy.conf").write_bytes(legacy)
    (nginx_root / "admin-route-control-plane.conf").write_text(
        Path(
            "deploy/ecorex-cloud-sidecar/nginx/admin-route-control-plane.conf"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    control = nginx_root / "active-control-plane.conf"
    admin = nginx_root / "active-admin-route.conf"
    control.symlink_to(nginx_root / "control-plane-disabled.conf")
    admin.symlink_to(nginx_root / "admin-route-legacy.conf")
    calls = 0

    def fail_candidate_once(_command, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise deployment.CloudDeployError("nginx_configuration_invalid")

    monkeypatch.setattr(deployment, "NGINX_ROOT", nginx_root)
    monkeypatch.setattr(deployment, "_run", fail_candidate_once)

    with pytest.raises(
        deployment.CloudDeployError, match="nginx_configuration_invalid"
    ):
        deployment._switch_nginx(_spec(tmp_path), "blue")

    assert control.resolve() == (nginx_root / "control-plane-disabled.conf").resolve()
    assert admin.resolve() == (nginx_root / "admin-route-legacy.conf").resolve()
    assert calls == 3


def _slot_state(
    release_id: str,
    slot: str,
    *,
    previous_target_type: str = "legacy",
    previous_release_id: str | None = None,
    previous_slot: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "active_target_type": "slot",
        "active_release_id": release_id,
        "active_slot": slot,
        "previous_target_type": previous_target_type,
        "previous_release_id": previous_release_id,
        "previous_slot": previous_slot,
        "artifact_manifest_sha256": "a" * 64,
        "activated_at_unix": 1_700_000_000,
    }


def _legacy_migration_contract() -> dict[str, object]:
    return {
        "source_version": "0.2.9.2",
        "as_of": "2026-07-17T00:00:00Z",
        "source_database_sha256": "b" * 64,
        "source_snapshot_sha256": "c" * 64,
        "import_receipt_sha256": "d" * 64,
        "identity_records_sha256": "e" * 64,
    }


def test_first_activation_receipt_retains_typed_legacy_rollback_target() -> None:
    first = deployment._activation_state(
        release_id="ecorex-cloud-v1.0.0-first",
        slot="blue",
        prior=None,
        artifact_manifest_sha256="a" * 64,
    )
    second = deployment._activation_state(
        release_id="ecorex-cloud-v1.0.0-second",
        slot="green",
        prior=first,
        artifact_manifest_sha256="b" * 64,
    )

    assert first["active_target_type"] == "slot"
    assert first["previous_target_type"] == "legacy"
    assert first["previous_release_id"] is None
    assert first["previous_slot"] is None
    assert second["previous_target_type"] == "slot"
    assert second["previous_release_id"] == first["active_release_id"]
    assert second["previous_slot"] == "blue"


def test_transition_journal_is_canonical_durable_and_tamper_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    journal_path = state_root / "activation-pending.json"
    syncs: list[Path] = []
    monkeypatch.setattr(deployment, "STATE_ROOT", state_root)
    monkeypatch.setattr(deployment, "ACTIVATION_JOURNAL_PATH", journal_path)

    def atomic_write(path: Path, payload: bytes, mode: int = 0o640) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(mode)

    monkeypatch.setattr(deployment, "_atomic_write", atomic_write)
    monkeypatch.setattr(
        deployment, "_fsync_directory", lambda path: syncs.append(path)
    )
    target = _slot_state("ecorex-cloud-v1.0.0-target", "blue")

    journal = deployment._new_transition_journal(
        operation="activate", source_state=None, target_state=target
    )
    journal = deployment._advance_transition_journal(journal, "routes_switched")

    assert journal["source_target_type"] == "legacy"
    assert journal["target_target_type"] == "slot"
    assert journal["phase"] == "routes_switched"
    assert syncs == [state_root, state_root]
    assert journal_path.read_bytes() == deployment._canonical_json(journal) + b"\n"

    value = json.loads(journal_path.read_text(encoding="utf-8"))
    value["unexpected"] = True
    journal_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(deployment.CloudDeployError, match="activation_journal_invalid"):
        deployment._transition_journal()

    value.pop("unexpected")
    value["phase"] = "committed"
    journal_path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(deployment.CloudDeployError, match="activation_journal_invalid"):
        deployment._transition_journal()


@pytest.mark.parametrize(
    "phase,route_state,expected",
    (
        ("prepared", "source", "source"),
        ("target_ready", "source", "source"),
        ("prepared", "target", "target"),
        ("prepared", "partial", "target"),
        ("prepared", "unknown", "target"),
        ("target_ready", "target", "target"),
        ("target_ready", "partial", "target"),
        ("routes_switched", "source", "target"),
        ("state_written", "source", "target"),
    ),
)
def test_phase_and_double_route_identity_choose_safe_recovery_direction(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    route_state: str,
    expected: str,
) -> None:
    journal = {"phase": phase, "source_state": None, "target_state": {}}
    monkeypatch.setattr(
        deployment, "_classify_transition_routes", lambda _journal: route_state
    )

    assert deployment._transition_resolution(journal) == expected


def test_activation_schema_boundary_is_durable_before_first_legacy_writer_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []
    target = _slot_state("ecorex-cloud-v1.0.0-target", "blue")
    journal = {
        "operation": "activate",
        "phase": "prepared",
        "source_state": None,
        "target_state": target,
    }

    def advance(value, phase):
        events.append(("journal", phase))
        return {**value, "phase": phase}

    monkeypatch.setattr(deployment, "_advance_transition_journal", advance)
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, verb, units: events.append((verb, tuple(units))),
    )
    monkeypatch.setattr(
        deployment,
        "_schema_gate",
        lambda _release, slot: events.append(("schema", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_production_contract_gate",
        lambda _release, slot: events.append(("contracts", slot)),
    )

    result = deployment._ensure_activation_schema_ready(
        _spec(tmp_path), journal, target_release=tmp_path / "release"
    )

    assert result["phase"] == "schema_ready"
    assert events == [
        ("journal", "migrating"),
        ("stop", tuple(reversed(deployment._slot_units("blue")))),
        ("stop", tuple(reversed(deployment.LEGACY_SERVICE_NAMES))),
        ("schema", "blue"),
        ("contracts", "blue"),
        ("journal", "schema_ready"),
    ]


def test_v1_to_v1_transition_keeps_v0292_compatibility_service_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal v1 upgrade never re-fences the restored v0.2.9.2 Web service."""

    events: list[object] = []
    source = _slot_state("ecorex-cloud-v1.0.6-source", "blue")
    target = _slot_state("ecorex-cloud-v1.0.7-target", "green")
    journal = {
        "operation": "activate",
        "phase": "prepared",
        "source_state": source,
        "target_state": target,
    }

    def advance(value, phase):
        events.append(("journal", phase))
        return {**value, "phase": phase}

    monkeypatch.setattr(deployment, "_advance_transition_journal", advance)
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, verb, units: events.append((verb, tuple(units))),
    )
    monkeypatch.setattr(
        deployment,
        "_schema_gate",
        lambda _release, slot: events.append(("schema", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_production_contract_gate",
        lambda _release, slot: events.append(("contracts", slot)),
    )
    monkeypatch.setattr(deployment, "_commit_legacy_password_credentials", lambda _slot: None)

    result = deployment._ensure_activation_schema_ready(
        _spec(tmp_path), journal, target_release=tmp_path / "release"
    )

    assert result["phase"] == "schema_ready"
    assert events == [
        ("journal", "migrating"),
        ("stop", tuple(reversed(deployment._slot_units("green")))),
        ("stop", tuple(reversed(deployment._slot_units("blue")))),
        ("schema", "green"),
        ("contracts", "green"),
        ("journal", "schema_ready"),
    ]
    assert all(
        "ecorex-web.service" not in units
        for verb, units in events
        if verb == "stop"
    )


def test_legacy_commit_runs_only_after_both_writer_sets_are_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []
    target = _slot_state("ecorex-cloud-v1.0.0-target", "blue")
    journal = {
        "operation": "activate",
        "phase": "prepared",
        "source_state": None,
        "target_state": target,
        "legacy_admin_migration": deployment._legacy_migration_seed(
            deployment.dataclasses.replace(
                _spec(tmp_path),
                legacy_admin_migration=deployment.LegacyAdminMigrationSpec("0.2.9.2"),
            ),
            as_of=deployment.datetime(2026, 7, 17, tzinfo=deployment.UTC),
        ),
    }

    def advance(value, phase):
        events.append(("journal", phase))
        return {**value, "phase": phase}

    monkeypatch.setattr(deployment, "_advance_transition_journal", advance)
    monkeypatch.setattr(
        deployment,
        "_stop_transition_writers",
        lambda *_args: events.append("both_writers_stopped"),
    )
    monkeypatch.setattr(
        deployment, "_schema_gate", lambda *_args: events.append("schema_ready")
    )
    monkeypatch.setattr(
        deployment,
        "_production_contract_gate",
        lambda *_args: events.append("contracts_checked"),
    )
    monkeypatch.setattr(
        deployment,
        "_prepare_legacy_import_contract",
        lambda value: (
            {**value, "legacy_admin_migration": _legacy_migration_contract()},
            (),
        ),
    )
    monkeypatch.setattr(
        deployment,
        "_commit_legacy_admin_and_identity",
        lambda *_args: events.append("legacy_business_write"),
    )

    result = deployment._ensure_activation_schema_ready(
        _spec(tmp_path), journal, target_release=tmp_path / "release"
    )

    assert result["phase"] == "schema_ready"
    assert events == [
        ("journal", "migrating"),
        "both_writers_stopped",
        "schema_ready",
        "legacy_business_write",
        ("journal", "legacy_imported"),
        "contracts_checked",
        ("journal", "schema_ready"),
    ]


def test_configured_deployment_platform_admin_is_created_after_management_import(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control-plane.sqlite3"
    AdminManagementSchemaManager(database).migrate()
    key = b"a" * 32
    environment = {
        "ECOREX_CP_DEVICE_PLATFORM_ADMIN_ACCOUNT_IDS": "ecorex-platform-admin"
    }

    deployment._ensure_configured_deployment_platform_admin(
        database,
        encryption_key=key,
        environment=environment,
    )
    deployment._ensure_configured_deployment_platform_admin(
        database,
        encryption_key=key,
        environment=environment,
    )

    user = AdminManagementRepository(database, encryption_key=key).get_user(
        "ecorex-platform-admin"
    )
    assert user.status == "active"
    assert user.display_name == "e-Mate 管理员"
    assert user.organization_id == "ecorex-production"


def test_legacy_identity_inventory_skips_deleted_suspended_and_unusable_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = tuple({"account_id": account_id} for account_id in (
        "active-user",
        "suspended-user",
        "deleted-user",
    ))

    class Repository:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def active_public_catalog(self):
            return [{"local_model_id": "gpt-5.6-sol"}]

        def get_user(self, account_id: str):
            if account_id == "deleted-user":
                raise deployment.AdminManagementNotFound("missing")
            return SimpleNamespace(
                account_id=account_id,
                status="active" if account_id == "active-user" else "suspended",
            )

    monkeypatch.setattr(deployment, "AdminManagementRepository", Repository)

    assert deployment._eligible_legacy_identity_records(
        records,
        target=tmp_path / "control-plane.sqlite3",
        encryption_key=b"a" * 32,
    ) == (records[0],)

    class EmptyCatalogRepository(Repository):
        def active_public_catalog(self):
            return []

    monkeypatch.setattr(
        deployment, "AdminManagementRepository", EmptyCatalogRepository
    )
    assert deployment._eligible_legacy_identity_records(
        records,
        target=tmp_path / "control-plane.sqlite3",
        encryption_key=b"a" * 32,
    ) == ()


def test_crash_after_admin_commit_forces_idempotent_rollforward_without_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_database = tmp_path / "control-plane.sqlite3"
    connection = deployment.sqlite3.connect(target_database)
    connection.execute(
        "CREATE TABLE admin_ops_idempotency("
        "actor_subject TEXT,client_request_id TEXT,operation TEXT,response_json TEXT)"
    )
    contract = _legacy_migration_contract()
    connection.execute(
        "INSERT INTO admin_ops_idempotency VALUES(?,?,?,?)",
        (
            "migration:v0.2.9.2",
            "legacy-admin-" + str(contract["import_receipt_sha256"])[:32],
            "legacy.v0292.admin-management.import",
            json.dumps(
                {"import_receipt_sha256": contract["import_receipt_sha256"]}
            ),
        ),
    )
    connection.commit()
    connection.close()
    journal = {
        "operation": "activate",
        "phase": "migrating",
        "source_state": None,
        "target_state": _slot_state("ecorex-cloud-v1.0.0-target", "blue"),
        "legacy_admin_migration": contract,
    }
    events: list[str] = []
    monkeypatch.setattr(
        deployment, "CONTROL_PLANE_DATABASE_PATH", target_database
    )
    monkeypatch.setattr(
        deployment, "_classify_transition_routes", lambda _journal: "source"
    )
    monkeypatch.setattr(
        deployment,
        "_ensure_activation_schema_ready",
        lambda _spec, value: events.append("resume_import")
        or {**value, "phase": "schema_ready"},
    )
    monkeypatch.setattr(
        deployment,
        "_complete_transition_target",
        lambda *_args: events.append("target_started"),
    )
    monkeypatch.setattr(
        deployment,
        "_restore_transition_source",
        lambda *_args: pytest.fail("legacy writer restarted after target commit"),
    )

    assert deployment._resolve_pending_transition(_spec(tmp_path), journal) == "target"
    assert events == ["resume_import", "target_started"]


def test_failed_migration_leaves_migrating_journal_and_writers_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    phases: list[str] = []
    stops: list[tuple[str, ...]] = []
    target = _slot_state("ecorex-cloud-v1.0.0-target", "blue")
    journal = {
        "operation": "activate",
        "phase": "prepared",
        "source_state": None,
        "target_state": target,
    }

    def advance(value, phase):
        phases.append(phase)
        return {**value, "phase": phase}

    monkeypatch.setattr(deployment, "_advance_transition_journal", advance)
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, verb, units: stops.append(tuple(units)),
    )
    monkeypatch.setattr(
        deployment,
        "_schema_gate",
        lambda *_args: (_ for _ in ()).throw(
            deployment.CloudDeployError("control_plane_schema_migration_failed")
        ),
    )

    with pytest.raises(
        deployment.CloudDeployError, match="control_plane_schema_migration_failed"
    ):
        deployment._ensure_activation_schema_ready(
            _spec(tmp_path), journal, target_release=tmp_path / "release"
        )

    assert phases == ["migrating"]
    assert stops == [
        tuple(reversed(deployment._slot_units("blue"))),
        tuple(reversed(deployment.LEGACY_SERVICE_NAMES)),
    ]


def test_schema_ready_recovery_does_not_repeat_completed_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = {
        "operation": "activate",
        "phase": "schema_ready",
        "source_state": None,
        "target_state": _slot_state("ecorex-cloud-v1.0.0-target", "blue"),
    }
    monkeypatch.setattr(
        deployment,
        "_schema_gate",
        lambda *_args: pytest.fail("durably completed migration repeated"),
    )
    monkeypatch.setattr(
        deployment,
        "_stop_transition_writers",
        lambda *_args: pytest.fail("completed boundary refrozen by schema helper"),
    )

    assert deployment._ensure_activation_schema_ready(_spec(tmp_path), journal) == journal


def test_migrating_recovery_reruns_schema_before_target_can_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []
    source = _slot_state("ecorex-cloud-v1.0.0-source", "blue")
    target = _slot_state("ecorex-cloud-v1.0.0-target", "green")
    journal = {
        "operation": "activate",
        "phase": "migrating",
        "source_state": source,
        "target_state": target,
    }
    monkeypatch.setattr(
        deployment, "_transition_resolution", lambda _journal: "target"
    )
    monkeypatch.setattr(
        deployment,
        "_verify_transition_release",
        lambda _spec, state: events.append(("verify", state["active_slot"]))
        or tmp_path / "release",
    )
    monkeypatch.setattr(
        deployment,
        "_schema_gate",
        lambda _release, slot: events.append(("migrate", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_production_contract_gate",
        lambda _release, slot: events.append(("contracts", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_advance_transition_journal",
        lambda value, phase: events.append(("journal", phase))
        or {**value, "phase": phase},
    )
    monkeypatch.setattr(
        deployment,
        "_recovery_schema_check",
        lambda _release, slot, **_kwargs: events.append(("check", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, verb, units: events.append((verb, tuple(units))),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_api_health",
        lambda _spec, slot: events.append(("api-health", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_worker_health",
        lambda _spec, slot: events.append(("worker-health", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_health",
        lambda _spec, slot: events.append(("health", slot)),
    )
    monkeypatch.setattr(
        deployment, "_prepare_slot_runtime_directory", lambda _slot: None
    )
    monkeypatch.setattr(deployment, "_switch_nginx", lambda *_args: None)
    monkeypatch.setattr(deployment, "_write_slot_projection", lambda *_args: None)
    monkeypatch.setattr(deployment, "_clear_transition_journal", lambda: None)

    assert deployment._resolve_pending_transition(_spec(tmp_path), journal) == "target"

    migration_index = events.index(("migrate", "green"))
    contract_index = events.index(("contracts", "green"))
    schema_ready_index = events.index(("journal", "schema_ready"))
    start_index = next(i for i, event in enumerate(events) if event[0] == "start")
    assert migration_index < contract_index < schema_ready_index < start_index


def test_migrating_first_release_restores_immutable_legacy_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = {
        "operation": "activate",
        "phase": "migrating",
        "source_state": None,
        "target_state": _slot_state("ecorex-cloud-v1.0.0-target", "blue"),
        "legacy_admin_migration": _legacy_migration_contract(),
    }
    events: list[object] = []
    monkeypatch.setattr(
        deployment, "_classify_transition_routes", lambda _journal: "source"
    )
    monkeypatch.setattr(
        deployment,
        "_schema_gate",
        lambda *_args: pytest.fail("legacy fallback retried target migration"),
    )
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, verb, units: events.append((verb, tuple(units))),
    )
    monkeypatch.setattr(
        deployment,
        "_switch_nginx_legacy",
        lambda _spec: events.append("legacy_routes"),
    )
    monkeypatch.setattr(
        deployment, "_remove_slot_projection", lambda: events.append("legacy_state")
    )
    monkeypatch.setattr(
        deployment, "_clear_transition_journal", lambda: events.append("journal_clear")
    )

    assert deployment._resolve_pending_transition(_spec(tmp_path), journal) == "source"

    assert events == [
        ("stop", tuple(reversed(deployment._slot_units("blue")))),
        ("stop", tuple(reversed(deployment.LEGACY_SERVICE_NAMES))),
        ("start", tuple(deployment.LEGACY_SERVICE_NAMES)),
        ("is-active", tuple(deployment.LEGACY_SERVICE_NAMES)),
        "legacy_routes",
        "legacy_state",
        "journal_clear",
    ]


def test_double_nginx_link_classifier_detects_source_target_and_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nginx = tmp_path / "nginx"
    monkeypatch.setattr(deployment, "NGINX_ROOT", nginx)
    control_link = nginx / "active-control-plane.conf"
    admin_link = nginx / "active-admin-route.conf"
    resolutions = {
        control_link: nginx / "control-plane-disabled.conf",
        admin_link: nginx / "admin-route-legacy.conf",
    }
    original_is_symlink = Path.is_symlink
    original_resolve = Path.resolve

    def is_symlink(path: Path) -> bool:
        if path in {control_link, admin_link}:
            return True
        return original_is_symlink(path)

    def resolve(path: Path, strict: bool = False) -> Path:
        if path in resolutions:
            return resolutions[path]
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    monkeypatch.setattr(Path, "resolve", resolve)
    journal = {
        "source_state": None,
        "target_state": _slot_state("ecorex-cloud-v1.0.0-target", "blue"),
    }

    assert deployment._classify_transition_routes(journal) == "source"
    resolutions[control_link] = nginx / "control-plane-blue.conf"
    resolutions[admin_link] = nginx / "admin-route-control-plane.conf"
    assert deployment._classify_transition_routes(journal) == "target"
    resolutions[admin_link] = nginx / "admin-route-legacy.conf"
    assert deployment._classify_transition_routes(journal) == "partial"


@pytest.mark.parametrize(
    "phase,route_state,expected",
    (
        ("prepared", "source", "source"),
        ("target_ready", "source", "source"),
        ("target_ready", "partial", "target"),
        ("routes_switched", "source", "target"),
        ("state_written", "unknown", "target"),
    ),
)
def test_kill_window_recovery_executes_selected_direction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    route_state: str,
    expected: str,
) -> None:
    journal = {
        "operation": "activate",
        "phase": phase,
        "source_state": None,
        "target_state": _slot_state("ecorex-cloud-v1.0.0-target", "blue"),
    }
    calls: list[str] = []
    monkeypatch.setattr(deployment, "_transition_journal", lambda: journal)
    monkeypatch.setattr(
        deployment, "_classify_transition_routes", lambda _journal: route_state
    )
    monkeypatch.setattr(
        deployment,
        "_restore_transition_source",
        lambda *_args: calls.append("source"),
    )
    monkeypatch.setattr(
        deployment,
        "_complete_transition_target",
        lambda *_args: calls.append("target"),
    )

    result = deployment._recover_pending_transition(_spec(tmp_path))

    assert result is not None
    assert result["resolution"] == expected
    assert calls == [expected]


def test_schema_incompatible_source_rolls_forward_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = {
        "phase": "prepared",
        "source_state": _slot_state("ecorex-cloud-v1.0.0-source", "blue"),
        "target_state": _slot_state("ecorex-cloud-v1.0.0-target", "green"),
    }
    calls: list[str] = []
    monkeypatch.setattr(deployment, "_transition_resolution", lambda _journal: "source")
    monkeypatch.setattr(
        deployment,
        "_restore_transition_source",
        lambda *_args: (_ for _ in ()).throw(
            deployment._RecoverySourceSchemaIncompatible(
                "recovery_source_schema_incompatible"
            )
        ),
    )
    monkeypatch.setattr(
        deployment,
        "_complete_transition_target",
        lambda *_args: calls.append("target"),
    )

    assert deployment._resolve_pending_transition(_spec(tmp_path), journal) == "target"
    assert calls == ["target"]


def test_source_recovery_stops_target_writer_before_starting_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _slot_state("ecorex-cloud-v1.0.0-source", "blue")
    target = _slot_state(
        "ecorex-cloud-v1.0.0-target",
        "green",
        previous_target_type="slot",
        previous_release_id=str(source["active_release_id"]),
        previous_slot="blue",
    )
    events: list[object] = []
    monkeypatch.setattr(
        deployment,
        "_verify_transition_release",
        lambda _spec, state: events.append(("verify", state["active_slot"]))
        or tmp_path,
    )
    monkeypatch.setattr(
        deployment,
        "_recovery_schema_check",
        lambda _release, slot, **_kwargs: events.append(("schema", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, verb, units: events.append((verb, tuple(units))),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_api_health",
        lambda _spec, slot: events.append(("api-health", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_worker_health",
        lambda _spec, slot: events.append(("worker-health", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_health",
        lambda _spec, slot: events.append(("health", slot)),
    )
    monkeypatch.setattr(
        deployment, "_prepare_slot_runtime_directory", lambda _slot: None
    )
    monkeypatch.setattr(
        deployment,
        "_switch_nginx",
        lambda _spec, slot: events.append(("routes", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_write_slot_projection",
        lambda state: events.append(("state", state["active_slot"])),
    )
    monkeypatch.setattr(
        deployment, "_clear_transition_journal", lambda: events.append("journal_clear")
    )
    deployment._restore_transition_source(
        _spec(tmp_path), {"source_state": source, "target_state": target}
    )

    assert events == [
        ("stop", tuple(reversed(deployment._slot_units("green")))),
        ("stop", tuple(reversed(deployment._slot_units("blue")))),
        ("verify", "blue"),
        ("schema", "blue"),
        ("start", tuple(deployment._slot_api_units("blue"))),
        ("api-health", "blue"),
        (
            "start",
            (deployment._unit(deployment.IMAGE_WORKER_SERVICE_NAME, "blue"),),
        ),
        ("worker-health", "blue"),
        ("health", "blue"),
        ("routes", "blue"),
        ("state", "blue"),
        "journal_clear",
    ]


def test_target_roll_forward_stops_source_writer_then_reverifies_and_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _slot_state("ecorex-cloud-v1.0.0-source", "blue")
    target = _slot_state(
        "ecorex-cloud-v1.0.0-target",
        "green",
        previous_target_type="slot",
        previous_release_id=str(source["active_release_id"]),
        previous_slot="blue",
    )
    events: list[object] = []
    monkeypatch.setattr(
        deployment,
        "_verify_transition_release",
        lambda _spec, state: events.append(("verify", state["active_slot"]))
        or tmp_path,
    )
    monkeypatch.setattr(
        deployment,
        "_recovery_schema_check",
        lambda _release, slot, **_kwargs: events.append(("schema", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, verb, units: events.append((verb, tuple(units))),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_api_health",
        lambda _spec, slot: events.append(("api-health", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_worker_health",
        lambda _spec, slot: events.append(("worker-health", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_health",
        lambda _spec, slot: events.append(("health", slot)),
    )
    monkeypatch.setattr(
        deployment, "_prepare_slot_runtime_directory", lambda _slot: None
    )
    monkeypatch.setattr(
        deployment,
        "_switch_nginx",
        lambda _spec, slot: events.append(("routes", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_write_slot_projection",
        lambda state: events.append(("state", state["active_slot"])),
    )
    monkeypatch.setattr(
        deployment, "_clear_transition_journal", lambda: events.append("journal_clear")
    )

    deployment._complete_transition_target(
        _spec(tmp_path), {"source_state": source, "target_state": target}
    )

    assert events == [
        ("stop", tuple(reversed(deployment._slot_units("green")))),
        ("stop", tuple(reversed(deployment._slot_units("blue")))),
        ("verify", "green"),
        ("schema", "green"),
        ("start", tuple(deployment._slot_api_units("green"))),
        ("api-health", "green"),
        (
            "start",
            (deployment._unit(deployment.IMAGE_WORKER_SERVICE_NAME, "green"),),
        ),
        ("worker-health", "green"),
        ("health", "green"),
        ("routes", "green"),
        ("state", "green"),
        "journal_clear",
    ]


def test_slot_runtime_directory_is_service_group_traversable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot_root = tmp_path / "cloud" / "slots"
    ownership: list[tuple[Path, str, str]] = []
    monkeypatch.setattr(deployment, "SLOT_ROOT", slot_root)
    monkeypatch.setattr(deployment, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(
        deployment.shutil,
        "chown",
        lambda path, *, user, group: ownership.append((Path(path), user, group)),
    )

    deployment._prepare_slot_runtime_directory("blue")

    directory = slot_root / "blue"
    assert directory.is_dir()
    if os.name != "nt":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o750
    assert ownership == [(directory, "root", "ecorex-cloud")]


def test_slot_start_is_staged_before_worker_and_rechecks_all_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        deployment,
        "_prepare_slot_runtime_directory",
        lambda slot: events.append(("prepare", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, verb, units: events.append((verb, tuple(units))),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_api_health",
        lambda _spec, slot: events.append(("api-health", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_worker_health",
        lambda _spec, slot: events.append(("worker-health", slot)),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_health",
        lambda _spec, slot: events.append(("final-health", slot)),
    )

    deployment._start_target_services(
        _spec(tmp_path),
        _slot_state("ecorex-cloud-v1.0.0-target", "blue"),
    )

    assert events == [
        ("prepare", "blue"),
        ("start", tuple(deployment._slot_api_units("blue"))),
        ("api-health", "blue"),
        (
            "start",
            (deployment._unit(deployment.IMAGE_WORKER_SERVICE_NAME, "blue"),),
        ),
        ("worker-health", "blue"),
        ("final-health", "blue"),
    ]


def test_api_health_failure_stops_all_slot_units_without_starting_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        deployment, "_prepare_slot_runtime_directory", lambda _slot: None
    )
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, verb, units: events.append((verb, tuple(units))),
    )

    def unhealthy(_spec, slot):
        events.append(("api-health", slot))
        raise deployment.CloudDeployError("service_health_failed")

    monkeypatch.setattr(deployment, "_wait_api_health", unhealthy)
    monkeypatch.setattr(
        deployment,
        "_wait_worker_health",
        lambda *_args: pytest.fail("worker phase started after API health failure"),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_health",
        lambda *_args: pytest.fail("final phase started after API health failure"),
    )
    with pytest.raises(deployment.CloudDeployError, match="service_health_failed"):
        deployment._start_target_services(
            _spec(tmp_path),
            _slot_state("ecorex-cloud-v1.0.0-target", "blue"),
        )

    units = deployment._slot_units("blue")
    assert events == [
        ("start", tuple(deployment._slot_api_units("blue"))),
        ("api-health", "blue"),
        ("stop", tuple(reversed(units))),
    ]


@pytest.mark.parametrize("failure_stage", ("worker", "final"))
def test_worker_or_final_health_failure_stops_all_slot_units_in_reverse_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        deployment, "_prepare_slot_runtime_directory", lambda _slot: None
    )
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, verb, units: events.append((verb, tuple(units))),
    )
    monkeypatch.setattr(
        deployment,
        "_wait_api_health",
        lambda _spec, slot: events.append(("api-health", slot)),
    )

    def worker_health(_spec, slot):
        events.append(("worker-health", slot))
        if failure_stage == "worker":
            raise deployment.CloudDeployError("service_health_failed")

    def final_health(_spec, slot):
        events.append(("final-health", slot))
        if failure_stage == "final":
            raise deployment.CloudDeployError("service_health_failed")

    monkeypatch.setattr(deployment, "_wait_worker_health", worker_health)
    monkeypatch.setattr(deployment, "_wait_health", final_health)
    with pytest.raises(deployment.CloudDeployError, match="service_health_failed"):
        deployment._start_target_services(
            _spec(tmp_path),
            _slot_state("ecorex-cloud-v1.0.0-target", "green"),
        )

    units = deployment._slot_units("green")
    assert events == [
        ("start", tuple(deployment._slot_api_units("green"))),
        ("api-health", "green"),
        (
            "start",
            (deployment._unit(deployment.IMAGE_WORKER_SERVICE_NAME, "green"),),
        ),
        ("worker-health", "green"),
        *((("final-health", "green"),) if failure_stage == "final" else ()),
        ("stop", tuple(reversed(units))),
    ]


def test_legacy_source_and_target_paths_stop_slot_before_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = _slot_state("ecorex-cloud-v1.0.0-slot", "blue")
    events: list[object] = []
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, verb, units: events.append((verb, tuple(units))),
    )
    monkeypatch.setattr(
        deployment,
        "_switch_nginx_legacy",
        lambda _spec: events.append("legacy_routes"),
    )
    monkeypatch.setattr(
        deployment, "_remove_slot_projection", lambda: events.append("legacy_state")
    )
    monkeypatch.setattr(
        deployment, "_clear_transition_journal", lambda: events.append("journal_clear")
    )

    deployment._restore_transition_source(
        _spec(tmp_path),
        {
            "operation": "activate",
            "phase": "migrating",
            "source_state": None,
            "target_state": slot,
        },
    )
    assert events == [
        ("stop", tuple(reversed(deployment._slot_units("blue")))),
        ("stop", tuple(reversed(deployment.LEGACY_SERVICE_NAMES))),
        ("start", tuple(deployment.LEGACY_SERVICE_NAMES)),
        ("is-active", tuple(deployment.LEGACY_SERVICE_NAMES)),
        "legacy_routes",
        "legacy_state",
        "journal_clear",
    ]
    events.clear()
    deployment._complete_transition_target(
        _spec(tmp_path),
        {
            "operation": "rollback",
            "phase": "prepared",
            "source_state": slot,
            "target_state": None,
        },
    )

    assert events == [
        ("stop", tuple(reversed(deployment.LEGACY_SERVICE_NAMES))),
        ("stop", tuple(reversed(deployment._slot_units("blue")))),
        ("start", tuple(deployment.LEGACY_SERVICE_NAMES)),
        ("is-active", tuple(deployment.LEGACY_SERVICE_NAMES)),
        "legacy_routes",
        "legacy_state",
        "journal_clear",
    ]


def test_journal_removal_is_the_only_commit_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restores = 0

    def restore(*_args) -> None:
        nonlocal restores
        restores += 1

    monkeypatch.setattr(deployment, "_transition_journal", lambda: None)
    monkeypatch.setattr(deployment, "_restore_transition_source", restore)

    deployment._recover_pending_transition(_spec(tmp_path))

    assert restores == 0


def test_failed_recovery_keeps_journal_for_next_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = {
        "phase": "state_written",
        "source_state": None,
        "target_state": _slot_state("ecorex-cloud-v1.0.0-target", "blue"),
    }
    clears = 0
    monkeypatch.setattr(deployment, "_transition_journal", lambda: journal)
    monkeypatch.setattr(
        deployment,
        "_complete_transition_target",
        lambda *_args: (_ for _ in ()).throw(
            deployment.CloudDeployError("nginx_restore_failed")
        ),
    )

    def clear() -> None:
        nonlocal clears
        clears += 1

    monkeypatch.setattr(deployment, "_clear_transition_journal", clear)

    with pytest.raises(deployment.CloudDeployError, match="activation_recovery_failed"):
        deployment._recover_pending_transition(_spec(tmp_path))

    assert clears == 0
    assert deployment._transition_journal() is journal


def test_failed_immediate_compensation_keeps_journal_for_startup_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = {
        "phase": "routes_switched",
        "source_state": None,
        "target_state": _slot_state("ecorex-cloud-v1.0.0-target", "blue"),
    }
    clears = 0
    monkeypatch.setattr(
        deployment,
        "_complete_transition_target",
        lambda *_args: (_ for _ in ()).throw(
            deployment.CloudDeployError("nginx_restore_failed")
        ),
    )
    monkeypatch.setattr(deployment, "_transition_journal", lambda: journal)

    def clear() -> None:
        nonlocal clears
        clears += 1

    monkeypatch.setattr(deployment, "_clear_transition_journal", clear)

    with pytest.raises(
        deployment.CloudDeployError, match="activation_compensation_failed"
    ):
        deployment._compensate_transition(_spec(tmp_path), journal)

    assert clears == 0
    assert deployment._transition_journal() is journal


@pytest.mark.parametrize(
    "failure_stage,expected_phase",
    (
        ("state", "routes_switched"),
        ("current", "state_written"),
        ("clear", "state_written"),
    ),
)
def test_post_route_commit_failures_roll_forward_and_return_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_phase: str,
) -> None:
    spec = _spec(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    prior = _slot_state("ecorex-cloud-v1.0.0-source", "blue")
    completed: list[dict[str, object]] = []
    monkeypatch.setattr(deployment.CloudDeploymentSpec, "validate", lambda _self: None)
    monkeypatch.setattr(deployment, "_target_preflight", lambda *_args: None)
    monkeypatch.setattr(deployment, "_validate_artifact", lambda _spec: {})
    monkeypatch.setattr(deployment, "_validate_attestation", lambda _spec: None)
    monkeypatch.setattr(
        deployment,
        "_deployment_lock",
        lambda: deployment.contextlib.nullcontext(),
    )
    monkeypatch.setattr(deployment, "_recover_pending_transition", lambda *_args: None)
    monkeypatch.setattr(deployment, "_state", lambda: prior)
    monkeypatch.setattr(deployment, "_install_release", lambda *_args: release)
    monkeypatch.setattr(deployment, "_install_deployment_templates", lambda *_args: None)
    monkeypatch.setattr(deployment, "_write_slot_environment", lambda *_args: None)
    monkeypatch.setattr(deployment, "_verify_staged_runtime", lambda *_args: None)
    monkeypatch.setattr(deployment, "_verify_nginx_wiring", lambda *_args: None)
    monkeypatch.setattr(deployment, "_schema_gate", lambda *_args: None)
    monkeypatch.setattr(deployment, "_systemctl", lambda *_args: None)
    monkeypatch.setattr(deployment, "_start_target_services", lambda *_args: None)
    monkeypatch.setattr(deployment, "_switch_nginx", lambda *_args: None)
    monkeypatch.setattr(
        deployment,
        "_new_transition_journal",
        lambda **kwargs: {
            "phase": "prepared",
            "source_state": kwargs["source_state"],
            "target_state": kwargs["target_state"],
        },
    )
    monkeypatch.setattr(
        deployment,
        "_advance_transition_journal",
        lambda journal, phase: {**journal, "phase": phase},
    )

    def write_state(path: Path, *_args, **_kwargs) -> None:
        if failure_stage == "state" and path.name == "active.json":
            raise OSError("simulated durable state failure")

    def write_current(*_args, **_kwargs) -> None:
        if failure_stage == "current":
            raise OSError("simulated current link failure")

    def clear_journal() -> None:
        if failure_stage == "clear":
            raise deployment.CloudDeployError("activation_journal_clear_failed")

    monkeypatch.setattr(deployment, "_atomic_write", write_state)
    monkeypatch.setattr(deployment, "_atomic_symlink", write_current)
    monkeypatch.setattr(deployment, "_fsync_directory", lambda *_args: None)
    monkeypatch.setattr(deployment, "_clear_transition_journal", clear_journal)
    monkeypatch.setattr(
        deployment,
        "_classify_transition_routes",
        lambda _journal: "target",
    )
    monkeypatch.setattr(
        deployment,
        "_complete_transition_target",
        lambda _spec, journal: completed.append(dict(journal)),
    )

    receipt = deployment.deploy(spec, confirmation="1" * 64)

    assert receipt["active_release_id"] == spec.release_id
    assert len(completed) == 1
    assert completed[0]["phase"] == expected_phase
    assert completed[0]["source_state"] == prior


def test_first_activation_can_rollback_to_typed_legacy_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    current = _slot_state("ecorex-cloud-v1.0.0-current", "blue")
    active: dict[str, object] | None = current
    events: list[object] = []
    monkeypatch.setattr(deployment.CloudDeploymentSpec, "validate", lambda _self: None)
    monkeypatch.setattr(deployment, "_target_preflight", lambda *_args: None)
    monkeypatch.setattr(deployment, "_validate_attestation", lambda _spec: None)
    monkeypatch.setattr(
        deployment,
        "_deployment_lock",
        lambda: deployment.contextlib.nullcontext(),
    )
    monkeypatch.setattr(deployment, "_recover_pending_transition", lambda *_args: None)
    monkeypatch.setattr(deployment, "_state", lambda: active)
    monkeypatch.setattr(
        deployment,
        "_new_transition_journal",
        lambda **kwargs: {
            "phase": "prepared",
            "source_state": kwargs["source_state"],
            "target_state": kwargs["target_state"],
        },
    )
    monkeypatch.setattr(
        deployment,
        "_advance_transition_journal",
        lambda journal, phase: {**journal, "phase": phase},
    )
    monkeypatch.setattr(
        deployment,
        "_systemctl",
        lambda _spec, verb, units: events.append((verb, tuple(units))),
    )
    monkeypatch.setattr(
        deployment,
        "_switch_nginx_legacy",
        lambda _spec: events.append("legacy_routes"),
    )
    def remove_slot_projection() -> None:
        nonlocal active
        active = None
        events.append("legacy_state")

    monkeypatch.setattr(deployment, "_remove_slot_projection", remove_slot_projection)
    monkeypatch.setattr(
        deployment, "_clear_transition_journal", lambda: events.append("journal_clear")
    )

    receipt = deployment.rollback(spec, confirmation="1" * 64)

    assert receipt["active_target_type"] == "legacy"
    assert receipt["previous_target_type"] == "slot"
    assert receipt["previous_release_id"] == current["active_release_id"]
    assert active is None
    assert deployment.build_plan(spec, inspect_files=False).target_slot == "blue"
    assert events == [
        ("stop", tuple(reversed(deployment._slot_units("blue")))),
        ("start", tuple(deployment.LEGACY_SERVICE_NAMES)),
        ("is-active", tuple(deployment.LEGACY_SERVICE_NAMES)),
        "legacy_routes",
        "legacy_state",
        "journal_clear",
    ]
