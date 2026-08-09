from __future__ import annotations

import base64
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
import pytest

from ecorex import __version__
from ecorex.control_plane.production import (
    ControlPlaneProductionConfig,
    EnvironmentSecretProvider,
    ProductionConfigurationError,
    SingleNodeSQLiteS3Provider,
    main,
)
from ecorex.control_plane.app import UpdateSignalHub, _ClientConnection
from ecorex.control_plane.models import ControlPrincipal
from ecorex.control_plane.production_auth import Ed25519JWTAuthenticator
from ecorex.control_plane.production_storage import (
    ControlPlaneInstanceLock,
    ProductionStorageError,
)
from ecorex.control_plane.share_s3_objects import S3ShareObjectStore
from ecorex.update import ReleaseChannel


class SecretMap:
    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = dict(values)

    def read(self, logical_name: str) -> str:
        return self.values[logical_name]


class FakeS3:
    def __init__(self, *, encrypted: bool = True, private: bool = True) -> None:
        self.encrypted = encrypted
        self.private = private
        self.closed = False
        self.objects: dict[str, dict[str, Any]] = {}

    def head_bucket(self, **kwargs: Any) -> Mapping[str, Any]:
        assert kwargs["Bucket"] == "private-ecorex-test"
        return {}

    def get_bucket_encryption(self, **kwargs: Any) -> Mapping[str, Any]:
        del kwargs
        rules = (
            [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            if self.encrypted
            else []
        )
        return {"ServerSideEncryptionConfiguration": {"Rules": rules}}

    def get_public_access_block(self, **kwargs: Any) -> Mapping[str, Any]:
        del kwargs
        return {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": self.private,
                "IgnorePublicAcls": self.private,
                "BlockPublicPolicy": self.private,
                "RestrictPublicBuckets": self.private,
            }
        }

    def put_object(self, **kwargs: Any) -> Mapping[str, Any]:
        key = kwargs["Key"]
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise RuntimeError("precondition")
        self.objects[key] = {
            "content": body,
            "metadata": kwargs.get("Metadata", {}),
            "content_type": kwargs.get("ContentType", "application/octet-stream"),
        }
        return {"ETag": '"fake"'}

    def head_object(self, **kwargs: Any) -> Mapping[str, Any]:
        item = self.objects[kwargs["Key"]]
        return {
            "ContentLength": len(item["content"]),
            "ContentType": item["content_type"],
            "Metadata": item["metadata"],
            "ETag": '"fake"',
        }

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]:
        raise AssertionError(f"unexpected get_object: {kwargs!r}")

    def delete_object(self, **kwargs: Any) -> Mapping[str, Any]:
        self.objects.pop(kwargs["Key"], None)
        return {}

    def close(self) -> None:
        self.closed = True


class FakeS3Factory:
    def __init__(self, *, encrypted: bool = True, private: bool = True) -> None:
        self.encrypted = encrypted
        self.private = private
        self.clients: list[FakeS3] = []

    def create(self, _config: ControlPlaneProductionConfig) -> FakeS3:
        client = FakeS3(encrypted=self.encrypted, private=self.private)
        self.clients.append(client)
        return client


def _key() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(public).decode("ascii")


def _material(tmp_path: Path):
    auth_private, auth_public = _key()
    _release_private, release_public = _key()
    _publication_private, publication_public = _key()
    _rollback_private, rollback_public = _key()
    database_root = tmp_path / "database"
    backups = tmp_path / "backups"
    spool = tmp_path / "spool"
    database_root.mkdir()
    backups.mkdir()
    spool.mkdir()
    config = ControlPlaneProductionConfig(
        storage_backend="sqlite-wal",
        replica_count=1,
        database_path=(database_root / "control-plane.sqlite3").absolute(),
        backup_directory=backups.absolute(),
        storage_volume_id="production-volume-a",
        storage_encryption_at_rest=True,
        minimum_free_bytes=64 * 1024 * 1024,
        backup_interval_seconds=900,
        maximum_backup_age_seconds=3600,
        backup_retain_count=4,
        public_share_base_url="https://share.ecorex.test/s",
        share_spool_directory=spool.absolute(),
        share_storage_mode="s3",
        s3_bucket="private-ecorex-test",
        s3_prefix="ecorex/share/v1",
        s3_region="cn-test-1",
        s3_endpoint_url="https://s3.ecorex.test",
        s3_addressing_style="path",
        s3_max_connections=8,
        local_cas_root=None,
        local_cas_attestation_path=None,
        local_cas_attestation_sha256=None,
        local_cas_volume_id=None,
        local_cas_machine_id_sha256=None,
        local_cas_replica_count=1,
        local_cas_quota_bytes=0,
        local_cas_minimum_free_bytes=0,
        local_cas_owner_gid=None,
        local_cas_max_object_bytes=0,
        local_cas_max_open_streams=0,
        auth_issuer="https://identity.ecorex.test",
        auth_audience="ecorex-control-plane",
        auth_public_keys_json=json.dumps({"auth-v1": auth_public}),
        release_public_keys_json=json.dumps({"release-v1": release_public}),
        publication_public_keys_json=json.dumps({"publication-v1": publication_public}),
        rollback_signer_public_keys_json=json.dumps({"rollback-v1": rollback_public}),
        public_bootstrap_index_path=(
            tmp_path / "public" / "public-bootstrap-index.json"
        ).absolute(),
        public_bootstrap_index_url=(
            "https://download.ecorex.test/stable/public-bootstrap-index.json"
        ),
        public_bootstrap_readback_hosts=("download.ecorex.test",),
        instance_id="cp-test-1",
        bind_host="127.0.0.1",
        bind_port=8443,
        dependency_timeout_seconds=5,
        readiness_cache_seconds=1,
        bootstrap_freshness_automation_enabled=False,
    )
    share_key = base64.b64encode(b"s" * 32).decode("ascii")
    secrets = SecretMap(
        {
            "share-keyring": json.dumps(
                {
                    "active_key_id": "share-v1",
                    "keys": {"share-v1": share_key},
                    "legacy_key_id": None,
                }
            ),
            "audit-encryption-key": base64.b64encode(b"e" * 32).decode("ascii"),
            "audit-integrity-key": base64.b64encode(b"i" * 32).decode("ascii"),
        }
    )
    return config, secrets, auth_private


def _jwt(private: Ed25519PrivateKey, *, expired: bool = False) -> str:
    now = int(datetime.now(UTC).timestamp())
    header = {"alg": "EdDSA", "kid": "auth-v1", "typ": "JWT"}
    claims = {
        "iss": "https://identity.ecorex.test",
        "aud": "ecorex-control-plane",
        "iat": now - (1000 if expired else 1),
        "nbf": now - (1000 if expired else 1),
        "exp": now - 1 if expired else now + 300,
        "token_use": "access",
        "sub": "subject-1",
        "client_id": "client-1",
        "account_id": "account-1",
        "organization_id": "organization-1",
        "roles": ["release_admin", "audit_admin"],
    }

    def segment(value: Any) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(encoded).decode().rstrip("=")

    signing = f"{segment(header)}.{segment(claims)}"
    signature = (
        base64.urlsafe_b64encode(private.sign(signing.encode())).decode().rstrip("=")
    )
    return f"{signing}.{signature}"


def test_production_provider_migrates_checks_serves_and_drains(tmp_path: Path) -> None:
    config, secrets, auth_private = _material(tmp_path)
    factory = FakeS3Factory()
    provider = SingleNodeSQLiteS3Provider(factory)

    migrated = provider.migrate(config, secrets)
    assert migrated.storage_backend == "sqlite-wal"
    assert migrated.control_schema_version == 1
    assert migrated.audit_schema_version == 1
    assert migrated.share_schema_version == 1
    assert migrated.backup.backup_id.startswith("cpb_")
    checked = provider.check(config, secrets)
    assert checked.backup.database_sha256 == migrated.backup.database_sha256
    assert factory.clients[-1].closed is True

    bundle = provider.compose(config, secrets)
    assert isinstance(bundle.share_repository.object_store, S3ShareObjectStore)
    seed = bundle.skill_hub_registry.get("official-writing")
    assert seed.version == "1.0.2"
    assert bundle.skill_hub_bundle_store.verify(seed.package_sha256).metadata.name == (
        "official-writing"
    )
    app = bundle.create_app()
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "live"}
        assert client.get("/health/ready").json() == {"status": "ready"}
        bundle.lifecycle._run_maintenance()
        assert bundle.audit_repository.integrity_entries()[-1].action == (
            "audit.retention.enforced"
        )
        response = client.get(
            "/api/v1/releases/latest",
            params={
                "channel": "stable",
                "platform": "windows",
                "architecture": "x64",
                "current_version": "1.0.0",
            },
            headers={"Authorization": f"Bearer {_jwt(auth_private)}"},
        )
        assert response.status_code == 204
        skill_hub = client.get(
            "/ecorex-agent/client/skill-hub/v1/skills",
            headers={"Authorization": f"Bearer {_jwt(auth_private)}"},
        )
        assert skill_hub.status_code == 200
        assert skill_hub.json()["items"][0]["slug"] == "official-writing"
        bundle.lifecycle.begin_drain()
        assert client.get("/api/v1/admin/distribution").status_code == 503
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
    assert factory.clients[-1].closed is True

    # Normal shutdown releases the single-node lock.
    second = provider.compose(config, secrets)
    second.lifecycle.force_close()


def test_migration_initializes_missing_control_plane_work_directories(
    tmp_path: Path,
) -> None:
    config, secrets, _auth_private = _material(tmp_path)
    backup = tmp_path / "database" / "new-backups"
    spool = tmp_path / "database" / "new-share-spool"
    assert not backup.exists() and not spool.exists()
    configured = replace(
        config,
        backup_directory=backup.absolute(),
        share_spool_directory=spool.absolute(),
    )

    SingleNodeSQLiteS3Provider(FakeS3Factory()).migrate(configured, secrets)

    assert backup.is_dir() and spool.is_dir()


def test_serve_never_creates_or_migrates_missing_storage(tmp_path: Path) -> None:
    config, secrets, _private = _material(tmp_path)
    provider = SingleNodeSQLiteS3Provider(FakeS3Factory())
    assert not config.database_path.exists()
    with pytest.raises(ProductionStorageError):
        provider.compose(config, secrets)
    assert not config.database_path.exists()


def test_single_node_lock_and_pg_multi_replica_fail_closed(tmp_path: Path) -> None:
    config, _secrets, _private = _material(tmp_path)
    first = ControlPlaneInstanceLock(config.database_path)
    second = ControlPlaneInstanceLock(config.database_path)
    first.acquire()
    try:
        with pytest.raises(ProductionStorageError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()

    environment = _environment(config)
    environment["ECOREX_CP_STORAGE_BACKEND"] = "postgresql"
    with pytest.raises(ProductionConfigurationError, match="single-node SQLite WAL"):
        ControlPlaneProductionConfig.from_environment(environment)


def test_direct_release_admission_requires_explicit_single_release_scope(
    tmp_path: Path,
) -> None:
    config, _secrets, _private = _material(tmp_path)
    environment = _environment(config)
    parsed = ControlPlaneProductionConfig.from_environment(environment)
    assert parsed.direct_release_admission_enabled is False
    assert parsed.direct_release_id is None
    assert parsed.direct_release_instruction_sha256 is None

    instruction = hashlib.sha256(b"operator direct release instruction").hexdigest()
    environment.update(
        {
            "ECOREX_CP_DIRECT_RELEASE_ADMISSION_ENABLED": "true",
            "ECOREX_CP_DIRECT_RELEASE_ID": "release-stable-" + "a" * 24,
            "ECOREX_CP_DIRECT_RELEASE_INSTRUCTION_SHA256": instruction,
        }
    )
    direct = ControlPlaneProductionConfig.from_environment(environment)
    assert direct.direct_release_admission_enabled is True
    assert direct.direct_release_id == "release-stable-" + "a" * 24
    assert direct.direct_release_instruction_sha256 == instruction

    environment["ECOREX_CP_DIRECT_RELEASE_ADMISSION_ENABLED"] = "false"
    with pytest.raises(ProductionConfigurationError, match="must not retain"):
        ControlPlaneProductionConfig.from_environment(environment)
    environment["ECOREX_CP_STORAGE_BACKEND"] = "sqlite-wal"
    environment["ECOREX_CP_REPLICA_COUNT"] = "2"
    with pytest.raises(ProductionConfigurationError, match="single-node SQLite WAL"):
        ControlPlaneProductionConfig.from_environment(environment)


def test_attested_share_cas_mode_is_explicit_single_host_and_never_opens_s3(
    tmp_path: Path,
) -> None:
    base, _secrets, _private = _material(tmp_path)
    local = replace(
        base,
        share_storage_mode="attested-encrypted-local-cas",
        s3_bucket="",
        s3_prefix="",
        s3_region="",
        s3_endpoint_url=None,
        s3_max_connections=0,
        local_cas_root=(tmp_path / "encrypted" / "cas").resolve(),
        local_cas_attestation_path=(tmp_path / "attestation.json").resolve(),
        local_cas_attestation_sha256="a" * 64,
        local_cas_volume_id=base.storage_volume_id,
        local_cas_machine_id_sha256="b" * 64,
        local_cas_replica_count=1,
        local_cas_quota_bytes=256 * 1024**3,
        local_cas_minimum_free_bytes=10 * 1024**3,
        local_cas_owner_gid=1001,
        local_cas_max_object_bytes=64 * 1024 * 1024,
        local_cas_max_open_streams=32,
    )
    parsed = ControlPlaneProductionConfig.from_environment(_environment(local))
    assert parsed.share_storage_mode == "attested-encrypted-local-cas"
    assert parsed.local_cas_replica_count == 1
    assert parsed.s3_bucket == ""
    assert "a" * 64 not in repr(parsed)

    mixed = _environment(local)
    mixed["ECOREX_CP_S3_BUCKET"] = "must-not-be-accepted"
    with pytest.raises(ProductionConfigurationError, match="ambiguous"):
        ControlPlaneProductionConfig.from_environment(mixed)

    class Dependency:
        def validate_controls(self, *, write_probe: bool) -> None:
            del write_probe

        def ping(self) -> None:
            return None

        def close(self) -> None:
            return None

    dependency = Dependency()
    object_store = object()

    class LocalFactory:
        def create(self, received):
            assert received is parsed
            return dependency, object_store

    class RejectS3Factory:
        def create(self, _config):
            raise AssertionError("S3 must not open in attested local CAS mode")

    provider = SingleNodeSQLiteS3Provider(
        RejectS3Factory(),  # type: ignore[arg-type]
        local_cas_factory=LocalFactory(),  # type: ignore[arg-type]
    )
    selected_dependency, selected_store = provider._share_storage(parsed)
    assert selected_dependency is dependency
    assert selected_store is object_store


def test_publication_signer_configuration_is_complete_and_digest_pinned(
    tmp_path: Path,
) -> None:
    config, secrets, _private = _material(tmp_path)
    with pytest.raises(
        ProductionConfigurationError, match="signer configuration is incomplete"
    ):
        replace(config, publication_signer_key_id="publication-v1")
    with pytest.raises(
        ProductionConfigurationError, match="rollback signer configuration is incomplete"
    ):
        replace(config, rollback_signer_key_id="rollback-v1")
    with pytest.raises(ProductionConfigurationError, match="configuration is invalid"):
        replace(config, bootstrap_freshness_lease_seconds=299)
    with pytest.raises(ProductionConfigurationError, match="configuration is invalid"):
        replace(config, model_activation_timeout_seconds=29)
    with pytest.raises(ProductionConfigurationError, match="configuration is invalid"):
        replace(
            config,
            bootstrap_freshness_automation_enabled=True,
            bootstrap_freshness_lead_seconds=60 * 60,
            bootstrap_freshness_check_interval_seconds=60 * 60,
        )
    release_material = next(iter(json.loads(config.release_public_keys_json).values()))
    with pytest.raises(ProductionConfigurationError, match="distinct keys"):
        replace(
            config,
            publication_public_keys_json=json.dumps(
                {"publication-alias": release_material}
            ),
        )
    publication_material = next(
        iter(json.loads(config.publication_public_keys_json).values())
    )
    with pytest.raises(ProductionConfigurationError, match="distinct keys"):
        replace(
            config,
            rollback_signer_public_keys_json=json.dumps(
                {"rollback-alias": publication_material}
            ),
        )

    executable = Path(sys.executable).resolve(strict=True)
    adapter = (tmp_path / "kms-publication-adapter.py").absolute()
    adapter.write_text(
        "raise SystemExit('not invoked by composition test')\n", encoding="utf-8"
    )
    rollback_adapter = (tmp_path / "kms-rollback-adapter.py").absolute()
    rollback_adapter.write_text(
        "raise SystemExit('not invoked by composition test')\n", encoding="utf-8"
    )

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    environment = _environment(config)
    environment.update(
        {
            "ECOREX_CP_PUBLICATION_SIGNER_EXECUTABLE": str(executable),
            "ECOREX_CP_PUBLICATION_SIGNER_EXECUTABLE_SHA256": digest(executable),
            "ECOREX_CP_PUBLICATION_SIGNER_ADAPTER": str(adapter),
            "ECOREX_CP_PUBLICATION_SIGNER_ADAPTER_SHA256": digest(adapter),
            "ECOREX_CP_PUBLICATION_SIGNER_KEY_ID": "publication-v1",
            "ECOREX_CP_ROLLBACK_SIGNER_EXECUTABLE": str(executable),
            "ECOREX_CP_ROLLBACK_SIGNER_EXECUTABLE_SHA256": digest(executable),
            "ECOREX_CP_ROLLBACK_SIGNER_ADAPTER": str(rollback_adapter),
            "ECOREX_CP_ROLLBACK_SIGNER_ADAPTER_SHA256": digest(rollback_adapter),
            "ECOREX_CP_ROLLBACK_SIGNER_KEY_ID": "rollback-v1",
            "ECOREX_CP_BOOTSTRAP_FRESHNESS_LEAD_SECONDS": str(8 * 60 * 60),
            "ECOREX_CP_BOOTSTRAP_FRESHNESS_CHECK_INTERVAL_SECONDS": str(60 * 60),
            "ECOREX_CP_BOOTSTRAP_FRESHNESS_LEASE_SECONDS": str(10 * 60),
        }
    )
    configured = ControlPlaneProductionConfig.from_environment(environment)
    assert configured.publication_signer_adapter == adapter
    provider = SingleNodeSQLiteS3Provider(FakeS3Factory())
    provider.migrate(configured, secrets)
    bundle = provider.compose(configured, secrets)
    assert bundle.bootstrap_freshness_refresher.signer is not None
    assert bundle.bootstrap_freshness_refresher.signer.key_id == "publication-v1"
    assert bundle.rollback_signer is not None
    assert bundle.rollback_signer.key_id == "rollback-v1"
    assert bundle.rollback_signer is not bundle.bootstrap_freshness_refresher.signer
    bundle.lifecycle.force_close()


def test_enabled_freshness_without_signer_never_reports_ready_without_active(
    tmp_path: Path,
) -> None:
    config, secrets, _private = _material(tmp_path)
    enabled = replace(config, bootstrap_freshness_automation_enabled=True)
    provider = SingleNodeSQLiteS3Provider(FakeS3Factory())
    provider.migrate(enabled, secrets)
    bundle = provider.compose(enabled, secrets)
    with TestClient(bundle.create_app()) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        status = bundle.bootstrap_freshness_refresher.status()
        assert status["automation_enabled"] is True
        assert status["signer_configured"] is False
        assert status["scheduler_running"] is True
        assert status["scheduler_ready"] is True
        assert bundle.bootstrap_freshness_refresher.ready is False


def test_single_node_lock_excludes_a_second_os_process(tmp_path: Path) -> None:
    config, _secrets, _private = _material(tmp_path)
    program = (
        "from pathlib import Path\n"
        "from ecorex.control_plane.production_storage import ControlPlaneInstanceLock\n"
        f"lock=ControlPlaneInstanceLock(Path({str(config.database_path)!r}))\n"
        "lock.acquire()\n"
        "print('ready', flush=True)\n"
        "input()\n"
        "lock.release()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", program],
        cwd=Path(__file__).parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(ProductionStorageError):
            ControlPlaneInstanceLock(config.database_path).acquire()
        assert child.stdin is not None
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.wait(timeout=10) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    lock = ControlPlaneInstanceLock(config.database_path)
    lock.acquire()
    lock.release()


def test_update_hub_drain_wakes_active_sockets_and_rejects_new_ones() -> None:
    async def exercise() -> None:
        hub = UpdateSignalHub()
        first = _ClientConnection(
            principal=ControlPrincipal(
                subject="subject-1", client_id="client-1", account_id="account-1"
            ),
            channel=ReleaseChannel.STABLE,
            platform="windows",
            architecture="x64",
            current_version="1.0.0",
            queue=asyncio.Queue(maxsize=1),
        )
        assert await hub.add(first) is True
        await hub.begin_drain()
        assert await first.queue.get() is None
        second = _ClientConnection(
            principal=ControlPrincipal(
                subject="subject-2", client_id="client-2", account_id="account-2"
            ),
            channel=ReleaseChannel.STABLE,
            platform="windows",
            architecture="x64",
            current_version="1.0.0",
            queue=asyncio.Queue(maxsize=1),
        )
        assert await hub.add(second) is False

    asyncio.run(exercise())


def test_migration_failure_removes_partial_new_database_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, secrets, _private = _material(tmp_path)
    provider = SingleNodeSQLiteS3Provider(FakeS3Factory())

    def fail(_self):
        raise RuntimeError("injected audit migration failure")

    monkeypatch.setattr(
        "ecorex.control_plane.production.CloudAuditSchemaManager.migrate", fail
    )
    with pytest.raises(RuntimeError, match="injected audit"):
        provider.migrate(config, secrets)
    assert not config.database_path.exists()
    assert not (
        config.database_path.parent / ".ecorex-control-plane-volume.json"
    ).exists()


def test_failed_existing_migration_restores_verified_preupgrade_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, secrets, _private = _material(tmp_path)
    provider = SingleNodeSQLiteS3Provider(FakeS3Factory())
    provider.migrate(config, secrets)
    connection = sqlite3.connect(config.database_path)
    try:
        connection.execute(
            "INSERT INTO control_clients VALUES (?,?,?,?,?,?,?,?)",
            (
                "preserved-client",
                "account-1",
                None,
                "windows",
                "x64",
                "1.0.0",
                "idle",
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    def fail(_self):
        raise RuntimeError("injected Share migration failure")

    monkeypatch.setattr(
        "ecorex.control_plane.production.CloudShareSchemaManager.migrate", fail
    )
    with pytest.raises(RuntimeError, match="injected Share"):
        provider.migrate(config, secrets)
    connection = sqlite3.connect(f"{config.database_path.as_uri()}?mode=ro", uri=True)
    try:
        assert connection.execute(
            "SELECT client_id FROM control_clients WHERE client_id='preserved-client'"
        ).fetchone() == ("preserved-client",)
    finally:
        connection.close()
    # Restored storage remains a valid serve target and the lock was released.
    bundle = provider.compose(config, secrets)
    bundle.lifecycle.force_close()


def test_corrupt_backup_blocks_serve_without_opening_s3(tmp_path: Path) -> None:
    config, secrets, _private = _material(tmp_path)
    factory = FakeS3Factory()
    provider = SingleNodeSQLiteS3Provider(factory)
    report = provider.migrate(config, secrets)
    copy = config.backup_directory / f"{report.backup.backup_id}.sqlite3"
    with copy.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ProductionStorageError):
        provider.compose(config, secrets)
    assert factory.clients == []


def test_failed_asgi_startup_closes_s3_and_releases_instance_lock(
    tmp_path: Path,
) -> None:
    config, secrets, _private = _material(tmp_path)
    bootstrap = SingleNodeSQLiteS3Provider(FakeS3Factory())
    bootstrap.migrate(config, secrets)
    bad_factory = FakeS3Factory(encrypted=False)
    bad_provider = SingleNodeSQLiteS3Provider(bad_factory)
    bundle = bad_provider.compose(config, secrets)
    with pytest.raises(ProductionConfigurationError, match="encryption"):
        with TestClient(bundle.create_app()):
            pass
    assert bad_factory.clients[-1].closed is True
    replacement = bootstrap.compose(config, secrets)
    replacement.lifecycle.force_close()


def test_feishu_gateway_production_composition_reuses_control_plane_auth(
    tmp_path: Path,
) -> None:
    config, secrets, auth_private = _material(tmp_path)
    config = replace(config, feishu_connector_enabled=True)
    secrets.values.update(
        {
            "feishu-app-id": "cli_test",
            "feishu-app-secret": "server-only-secret",
            "feishu-token-encryption-key": base64.b64encode(b"f" * 32).decode(),
        }
    )
    provider = SingleNodeSQLiteS3Provider(FakeS3Factory())
    provider.migrate(config, secrets)
    provider.check(config, secrets)
    bundle = provider.compose(config, secrets)
    assert bundle.feishu_connector_gateway is not None
    assert ControlPlaneProductionConfig.from_environment(
        _environment(config)
    ).feishu_connector_enabled is True

    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    with TestClient(bundle.create_app()) as client:
        response = client.post(
            "/api/v1/connectors/feishu/auth/begin",
            headers={
                "Authorization": "Bearer " + _jwt(auth_private),
                "Idempotency-Key": "connflow_production",
            },
            json={
                "flow_id": "connflow_production",
                "auth_kind": "oauth2",
                "return_uri": (
                    "http://127.0.0.1:8765/api/v1/connectors/oauth/callback"
                ),
                "state": "state_0123456789abcdef",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert response.status_code == 200
        assert response.json()["connector_id"] == "feishu"
    bundle.lifecycle.force_close()


def test_schema_check_rejects_unencrypted_or_public_s3(tmp_path: Path) -> None:
    config, secrets, _private = _material(tmp_path)
    bootstrap = SingleNodeSQLiteS3Provider(FakeS3Factory())
    bootstrap.migrate(config, secrets)
    with pytest.raises(ProductionConfigurationError, match="encryption"):
        SingleNodeSQLiteS3Provider(FakeS3Factory(encrypted=False)).check(
            config, secrets
        )
    with pytest.raises(ProductionConfigurationError, match="public access"):
        SingleNodeSQLiteS3Provider(FakeS3Factory(private=False)).check(config, secrets)


def test_ed25519_jwt_authenticator_is_short_lived_and_redacts_token(
    tmp_path: Path,
) -> None:
    config, _secrets, private = _material(tmp_path)
    public = json.loads(config.auth_public_keys_json)["auth-v1"]
    authenticator = Ed25519JWTAuthenticator(
        {"auth-v1": base64.b64decode(public)},
        issuer=config.auth_issuer,
        audience=config.auth_audience,
    )
    principal = authenticator.authenticate(_jwt(private))
    assert principal.account_id == "account-1"
    expired = _jwt(private, expired=True)
    with pytest.raises(PermissionError) as captured:
        authenticator.authenticate(expired)
    assert expired not in str(captured.value)
    tampered = expired[:-1] + ("A" if expired[-1] != "A" else "B")
    with pytest.raises(PermissionError):
        authenticator.authenticate(tampered)


def test_cli_has_no_secret_or_path_arguments_and_redacts_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, secrets, _private = _material(tmp_path)
    environment = _environment(config)
    environment.update(
        {
            "ECOREX_CP_SHARE_KEYRING_JSON": secrets.read("share-keyring"),
            "ECOREX_CP_AUDIT_ENCRYPTION_KEY_B64": secrets.read("audit-encryption-key"),
            "ECOREX_CP_AUDIT_INTEGRITY_KEY_B64": secrets.read("audit-integrity-key"),
        }
    )
    provider = SingleNodeSQLiteS3Provider(FakeS3Factory())
    assert (
        main(
            ["schema", "migrate"],
            environment=environment,
            secret_provider=EnvironmentSecretProvider(environment),
            provider=provider,
        )
        == 0
    )
    output = capsys.readouterr()
    assert str(config.database_path) not in output.out
    assert secrets.read("audit-encryption-key") not in output.out

    bad = dict(environment)
    bad["ECOREX_CP_STORAGE_BACKEND"] = "postgresql"
    assert main(["serve"], environment=bad, provider=provider) == 2
    failure = capsys.readouterr()
    assert str(config.database_path) not in failure.err
    assert "postgresql" not in failure.err

    with pytest.raises(SystemExit):
        main(["--help"], environment=environment, provider=provider)
    help_text = capsys.readouterr().out
    assert "--database" not in help_text
    assert "--secret" not in help_text
    assert "--token" not in help_text


def test_cli_serve_runner_cannot_leak_the_process_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, secrets, _private = _material(tmp_path)
    environment = _environment(config)
    environment.update(
        {
            "ECOREX_CP_SHARE_KEYRING_JSON": secrets.read("share-keyring"),
            "ECOREX_CP_AUDIT_ENCRYPTION_KEY_B64": secrets.read("audit-encryption-key"),
            "ECOREX_CP_AUDIT_INTEGRITY_KEY_B64": secrets.read("audit-integrity-key"),
        }
    )
    factory = FakeS3Factory()
    provider = SingleNodeSQLiteS3Provider(factory)
    provider.migrate(config, secrets)
    seen: list[str] = []

    def runner(bundle) -> None:
        seen.append(bundle.config.instance_id)

    assert (
        main(
            ["serve"],
            environment=environment,
            secret_provider=EnvironmentSecretProvider(environment),
            provider=provider,
            server_runner=runner,
        )
        == 0
    )
    assert seen == [config.instance_id]
    assert factory.clients[-1].closed is True
    assert capsys.readouterr().out == ""
    replacement = provider.compose(config, secrets)
    replacement.lifecycle.force_close()


def _environment(config: ControlPlaneProductionConfig) -> dict[str, str]:
    values = {
        "ECOREX_CP_STORAGE_BACKEND": config.storage_backend,
        "ECOREX_CP_REPLICA_COUNT": str(config.replica_count),
        "ECOREX_CP_DATABASE_PATH": str(config.database_path),
        "ECOREX_CP_BACKUP_DIRECTORY": str(config.backup_directory),
        "ECOREX_CP_SHARE_SPOOL_DIRECTORY": str(config.share_spool_directory),
        "ECOREX_CP_STORAGE_VOLUME_ID": config.storage_volume_id,
        "ECOREX_CP_STORAGE_ENCRYPTION_AT_REST": "true",
        "ECOREX_CP_PUBLIC_SHARE_BASE_URL": config.public_share_base_url,
        "ECOREX_CP_SHARE_STORAGE_MODE": config.share_storage_mode,
        "ECOREX_CP_S3_ENDPOINT_URL": config.s3_endpoint_url or "",
        "ECOREX_CP_S3_BUCKET": config.s3_bucket,
        "ECOREX_CP_S3_PREFIX": config.s3_prefix,
        "ECOREX_CP_S3_REGION": config.s3_region,
        "ECOREX_CP_S3_ADDRESSING_STYLE": config.s3_addressing_style,
        "ECOREX_CP_AUTH_ISSUER": config.auth_issuer,
        "ECOREX_CP_AUTH_AUDIENCE": config.auth_audience,
        "ECOREX_CP_AUTH_PUBLIC_KEYS_JSON": config.auth_public_keys_json,
        "ECOREX_CP_RELEASE_PUBLIC_KEYS_JSON": config.release_public_keys_json,
        "ECOREX_CP_PUBLICATION_PUBLIC_KEYS_JSON": (config.publication_public_keys_json),
        "ECOREX_CP_ROLLBACK_SIGNER_PUBLIC_KEYS_JSON": (
            config.rollback_signer_public_keys_json
        ),
        "ECOREX_CP_PUBLIC_BOOTSTRAP_INDEX_PATH": str(
            config.public_bootstrap_index_path
        ),
        "ECOREX_CP_PUBLIC_BOOTSTRAP_INDEX_URL": (config.public_bootstrap_index_url),
        "ECOREX_CP_PUBLIC_BOOTSTRAP_READBACK_HOSTS": ",".join(
            config.public_bootstrap_readback_hosts
        ),
        "ECOREX_CP_INSTANCE_ID": config.instance_id,
        "ECOREX_CP_BOOTSTRAP_FRESHNESS_AUTOMATION_ENABLED": (
            "true" if config.bootstrap_freshness_automation_enabled else "false"
        ),
        "ECOREX_CP_BIND_HOST": config.bind_host,
        "ECOREX_CP_BIND_PORT": str(config.bind_port),
        "ECOREX_CP_MINIMUM_FREE_BYTES": str(config.minimum_free_bytes),
        "ECOREX_CP_BACKUP_INTERVAL_SECONDS": str(config.backup_interval_seconds),
        "ECOREX_CP_MAXIMUM_BACKUP_AGE_SECONDS": str(config.maximum_backup_age_seconds),
        "ECOREX_CP_BACKUP_RETAIN_COUNT": str(config.backup_retain_count),
        "ECOREX_CP_MODEL_ACTIVATION_TIMEOUT_SECONDS": str(
            config.model_activation_timeout_seconds
        ),
        "ECOREX_CP_FEISHU_CONNECTOR_ENABLED": (
            "true" if config.feishu_connector_enabled else "false"
        ),
    }
    if config.share_storage_mode == "attested-encrypted-local-cas":
        for name in tuple(values):
            if name.startswith("ECOREX_CP_S3_"):
                del values[name]
        assert config.local_cas_root is not None
        assert config.local_cas_attestation_path is not None
        assert config.local_cas_attestation_sha256 is not None
        assert config.local_cas_volume_id is not None
        assert config.local_cas_machine_id_sha256 is not None
        assert config.local_cas_owner_gid is not None
        values.update(
            ECOREX_CP_LOCAL_CAS_ROOT=str(config.local_cas_root),
            ECOREX_CP_LOCAL_CAS_ATTESTATION_PATH=str(
                config.local_cas_attestation_path
            ),
            ECOREX_CP_LOCAL_CAS_ATTESTATION_SHA256=(
                config.local_cas_attestation_sha256
            ),
            ECOREX_CP_LOCAL_CAS_VOLUME_ID=config.local_cas_volume_id,
            ECOREX_CP_LOCAL_CAS_MACHINE_ID_SHA256=(
                config.local_cas_machine_id_sha256
            ),
            ECOREX_CP_LOCAL_CAS_REPLICA_COUNT=str(config.local_cas_replica_count),
            ECOREX_CP_LOCAL_CAS_QUOTA_BYTES=str(config.local_cas_quota_bytes),
            ECOREX_CP_LOCAL_CAS_MINIMUM_FREE_BYTES=str(
                config.local_cas_minimum_free_bytes
            ),
            ECOREX_CP_LOCAL_CAS_OWNER_GID=str(config.local_cas_owner_gid),
            ECOREX_CP_LOCAL_CAS_MAX_OBJECT_BYTES=str(
                config.local_cas_max_object_bytes
            ),
            ECOREX_CP_LOCAL_CAS_MAX_OPEN_STREAMS=str(
                config.local_cas_max_open_streams
            ),
        )
    return values


def test_release_replica_environment_is_version_namespace_fenced(
    tmp_path: Path,
) -> None:
    config, _secrets, _private = _material(tmp_path)
    environment = _environment(config)
    environment.update(
        ECOREX_CP_RELEASE_REPLICA_ENABLED="true",
        ECOREX_CP_RELEASE_REPLICA_STORAGE_ROOT=(
            "/srv/ecorex-agent-download/v1-artifacts"
        ),
        ECOREX_CP_RELEASE_REPLICA_PUBLIC_ROOT=(
            "https://dl.ecoremedia.net/ecorex-agent/releases"
        ),
        ECOREX_CP_RELEASE_REPLICA_NAMESPACE=f"v{__version__}",
        ECOREX_CP_RELEASE_REPLICA_PRODUCT_VERSION=__version__,
        ECOREX_CP_RELEASE_REPLICA_MAX_ASSET_BYTES=str(500 * 1024 * 1024),
    )
    parsed = ControlPlaneProductionConfig.from_environment(environment)
    assert parsed.release_replica_enabled is True
    assert parsed.release_replica_namespace == f"v{__version__}"
    assert parsed.release_replica_product_version == __version__

    for name, invalid in (
        ("ECOREX_CP_RELEASE_REPLICA_NAMESPACE", "v1.0.1"),
        ("ECOREX_CP_RELEASE_REPLICA_PRODUCT_VERSION", "1.0.1"),
        (
            "ECOREX_CP_RELEASE_REPLICA_STORAGE_ROOT",
            "/srv/ecorex-agent-download/v1-artifacts/v1.0.0",
        ),
        (
            "ECOREX_CP_RELEASE_REPLICA_PUBLIC_ROOT",
            "https://dl.ecoremedia.net/ecorex-agent/releases/v1.0.0",
        ),
    ):
        drifted = dict(environment)
        drifted[name] = invalid
        with pytest.raises(ProductionConfigurationError, match="replica fence"):
            ControlPlaneProductionConfig.from_environment(drifted)
