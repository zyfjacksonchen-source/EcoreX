from __future__ import annotations

import base64
import hashlib
import json
import os
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
        "deployment/nginx/admin-route-control-plane.conf",
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

    location ^~ /ecorex-agent/ {
        alias /srv/ecorex-agent-download/current/;
    }
}
"""


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

    deployment._install_legacy_admin_route_wiring(spec)

    migrated = server.read_text(encoding="utf-8")
    legacy = (nginx_root / "admin-route-legacy.conf").read_text(encoding="utf-8")
    assert migrated.count(deployment.NGINX_ROUTE_INCLUDE) == 1
    assert "location ^~ /ecorex-agent/ {" in migrated
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

    deployment._install_legacy_admin_route_wiring(spec)
    assert len(calls) == 2

    candidate.write_text("tampered", encoding="utf-8")
    with pytest.raises(
        deployment.CloudDeployError, match="nginx_admin_route_wiring_invalid"
    ):
        deployment._install_legacy_admin_route_wiring(spec)
    candidate.write_text(candidate_source, encoding="utf-8")
    external = tmp_path / "external-admin.conf"
    external.write_text(candidate_source, encoding="utf-8")
    (nginx_root / "active-admin-route.conf").unlink()
    (nginx_root / "active-admin-route.conf").symlink_to(external)
    with pytest.raises(
        deployment.CloudDeployError, match="nginx_admin_route_wiring_invalid"
    ):
        deployment._install_legacy_admin_route_wiring(spec)


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
    monkeypatch.setattr(deployment, "_state", lambda: None)
    monkeypatch.setattr(deployment, "_install_release", lambda *_args: release)
    monkeypatch.setattr(deployment, "_install_deployment_templates", lambda *_args: None)
    monkeypatch.setattr(deployment, "_write_slot_environment", lambda *_args: None)
    monkeypatch.setattr(deployment, "_verify_staged_runtime", lambda *_args: None)
    monkeypatch.setattr(deployment, "_verify_nginx_wiring", lambda *_args: None)
    monkeypatch.setattr(deployment, "_schema_gate", lambda *_args: None)
    monkeypatch.setattr(deployment, "_systemctl", lambda *_args: None)
    monkeypatch.setattr(
        deployment, "_wait_health", lambda *_args: events.append("candidate_healthy")
    )

    def switch(*_args) -> None:
        assert events == ["candidate_healthy"]
        events.append("routes_switched")

    monkeypatch.setattr(deployment, "_switch_nginx", switch)
    monkeypatch.setattr(deployment, "_atomic_write", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment, "_atomic_symlink", lambda *_args: None)

    deployment.deploy(spec, confirmation="1" * 64)

    assert events == ["candidate_healthy", "routes_switched"]


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
