from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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
        artifact_root=artifact or tmp_path / "artifact",
        artifact_manifest_sha256="0" * 64,
        release_keyring_path=keyring,
        release_keyring_sha256=_sha(keyring),
        target_machine_id_sha256="1" * 64,
        encryption_attestation_path=attestation,
        encryption_attestation_sha256=_sha(attestation),
    )


def _signed_artifact(tmp_path: Path) -> tuple[deployment.CloudDeploymentSpec, Path]:
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
        "deployment/nginx/ecorex-cloud.routes.conf",
    )
    files = []
    for relative in required:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"fixture:{relative}\n".encode())
        files.append(
            {
                "path": relative,
                "sha256": _sha(target),
                "size_bytes": target.stat().st_size,
            }
        )
    manifest = {
        "schema_version": 1,
        "release_id": "ecorex-cloud-v1.0.0-test",
        "version": "1.0.0",
        "platform": "linux",
        "architecture": "aarch64",
        "python_version": "3.11.9",
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
    signature = private.sign(deployment._canonical_json(manifest))
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


def test_default_plan_is_read_only_and_uses_storage_contract_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _ = _signed_artifact(tmp_path)
    monkeypatch.setattr(deployment, "_state", lambda: None)

    plan = deployment.build_plan(spec)

    assert plan.dry_run is True
    assert plan.target_slot == "blue"
    assert plan.blockers == ()
    assert "run_exact_control_plane_and_image_storage_contract_checks" in plan.actions
    assert all("minio" not in action for action in plan.actions)


def test_schema_gate_runs_real_checks_and_blocks_incompatible_minio(
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

    with pytest.raises(
        deployment.CloudDeployError, match="image_production_contract_failed"
    ):
        deployment._schema_gate(release, "blue")

    assert [command[-2:] for command, _ in calls] == [
        ("schema", "migrate"),
        ("schema", "check"),
        ("schema", "migrate"),
        ("schema", "check"),
        ("schema", "migrate"),
        ("schema", "check"),
    ]


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
    minio = (root / "minio.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=/var/lib/ecorex/secrets/minio.secret.env" in minio
    assert "/etc/ecorex/cloud/secrets/" not in minio
