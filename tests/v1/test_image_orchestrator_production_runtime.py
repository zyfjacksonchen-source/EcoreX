from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
import pytest
from fastapi.testclient import TestClient

import ecorex.image_orchestrator.production as image_production

from ecorex.image_orchestrator.managed_provider import (
    ManagedHTTPSImageProvider,
    ManagedImageProviderConfigurationError,
)
from ecorex.image_orchestrator.models import (
    ImageOperation,
    ImageSubmitRequest,
)
from ecorex.image_orchestrator.production import (
    EnvironmentImageSecretProvider,
    ImageProductionConfig,
    ImageProductionConfigurationError,
    ImageProductionLifecycle,
    PostgresS3ManagedImageProvider,
    _S3Dependency,
    create_image_production_app,
    main as image_main,
)
from ecorex.image_orchestrator.production_auth import (
    Ed25519ImageJWTAuthenticator,
)
from ecorex.image_orchestrator.provider import (
    ProviderRejected,
    ProviderState,
    ProviderUncertain,
    ProviderUnavailable,
)
from ecorex.image_orchestrator.s3_cas import BotoS3ObjectTransport
from ecorex.image_orchestrator.cas import ImageContentStore
from ecorex.image_orchestrator.service import ImageOrchestrationService
from ecorex.image_orchestrator.sqlite_schema import SQLiteImageSchemaManager
from ecorex.image_orchestrator.sqlite_store import SQLiteImageJobStore
from ecorex.image_orchestrator.worker import (
    ImageWorkerOutcome,
    ImageWorkerResult,
    ImageWorkerSupervisor,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"managed-provider-result"


def test_managed_image_response_rejects_excessive_json_depth() -> None:
    payload = b'{"data":' + b"[" * 64 + b"0" + b"]" * 64 + b"}"
    with pytest.raises(ProviderRejected, match="response"):
        ManagedHTTPSImageProvider._decode_object(payload)


def _keyring() -> str:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return json.dumps({"image-key-1": base64.b64encode(public).decode("ascii")})


def _access_token(
    private_key: Ed25519PrivateKey,
    *,
    allowed_model_ids: list[str],
    include_quota: bool = True,
) -> str:
    def encode(value: Mapping[str, Any]) -> str:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")

    now = 1_788_825_600
    header = encode({"alg": "EdDSA", "kid": "image-key-1", "typ": "JWT"})
    claims: dict[str, Any] = {
        "iss": "https://identity.ecorex.invalid",
        "aud": "ecorex-image",
        "token_use": "access",
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "sub": "user-001",
        "client_id": "ecorex-web",
        "account_id": "account-001",
        "roles": [],
        "allowed_model_ids": allowed_model_ids,
    }
    if include_quota:
        claims.update(
            quota_period="2026-09",
            request_limit=100,
            concurrent_request_limit=4,
        )
    encoded_claims = encode(claims)
    signed = f"{header}.{encoded_claims}".encode("ascii")
    signature = base64.urlsafe_b64encode(private_key.sign(signed)).rstrip(b"=")
    return f"{signed.decode('ascii')}.{signature.decode('ascii')}"


def _environment() -> dict[str, str]:
    return {
        "ECOREX_IMAGE_STORAGE_BACKEND": "postgresql",
        "ECOREX_IMAGE_POSTGRES_DSN": "postgresql://operator:secret@db.invalid/ecorex",
        "ECOREX_IMAGE_INSTANCE_ID": "image-worker-001",
        "ECOREX_IMAGE_S3_BUCKET": "ecorex-private-images",
        "ECOREX_IMAGE_S3_PREFIX": "ecorex/images/v1",
        "ECOREX_IMAGE_S3_REGION": "cn-north-1",
        "ECOREX_IMAGE_S3_ENCRYPTION": "AES256",
        "ECOREX_IMAGE_AUTH_ISSUER": "https://identity.ecorex.invalid",
        "ECOREX_IMAGE_AUTH_AUDIENCE": "ecorex-image",
        "ECOREX_IMAGE_AUTH_PUBLIC_KEYS_JSON": _keyring(),
        "ECOREX_IMAGE_MODEL_ALLOWLIST_JSON": '["image-2"]',
        "ECOREX_IMAGE_PROVIDER_ID": "ecorex-managed-image",
        "ECOREX_IMAGE_PROVIDER_ORIGIN": "https://image.ecorex.invalid",
        "ECOREX_IMAGE_PROVIDER_ALLOWED_ORIGINS_JSON": '["https://image.ecorex.invalid"]',
        "ECOREX_IMAGE_PROVIDER_BEARER_TOKEN": "provider-workload-token-00000001",
    }


def _local_environment(tmp_path: Path) -> dict[str, str]:
    values = _environment()
    for name in tuple(values):
        if name.startswith("ECOREX_IMAGE_S3_"):
            del values[name]
    values.update(
        ECOREX_IMAGE_CONTENT_STORAGE_MODE="attested-encrypted-local-cas",
        ECOREX_IMAGE_LOCAL_CAS_ROOT=str((tmp_path / "volume" / "cas").resolve()),
        ECOREX_IMAGE_LOCAL_CAS_ATTESTATION_PATH=str(
            (tmp_path / "attestation.json").resolve()
        ),
        ECOREX_IMAGE_LOCAL_CAS_ATTESTATION_SHA256="a" * 64,
        ECOREX_IMAGE_LOCAL_CAS_VOLUME_ID="ecorex-volume-production",
        ECOREX_IMAGE_LOCAL_CAS_MACHINE_ID_SHA256="b" * 64,
        ECOREX_IMAGE_LOCAL_CAS_REPLICA_COUNT="1",
        ECOREX_IMAGE_LOCAL_CAS_QUOTA_BYTES=str(256 * 1024**3),
        ECOREX_IMAGE_LOCAL_CAS_MINIMUM_FREE_BYTES=str(10 * 1024**3),
        ECOREX_IMAGE_LOCAL_CAS_OWNER_GID="1001",
    )
    return values


def _job(tmp_path: Path):
    database = tmp_path / "image-provider.db"
    SQLiteImageSchemaManager(database).migrate()
    store = SQLiteImageJobStore(database)
    job, _created = store.submit(
        "account-001",
        ImageSubmitRequest(
            operation=ImageOperation.GENERATE,
            model_id="image-2",
            client_request_id="managed-provider-request-0001",
            prompt="draw a stable production diagram",
        ),
    )
    return job


def _response(job, *, state: str, request_id: str | None = None) -> dict[str, Any]:
    completed = state == "completed"
    return {
        "schema_version": 1,
        "state": state,
        "account_id": job.account_id,
        "job_id": job.job_id,
        "provider_request_id": request_id,
        "result": (
            {
                "sha256": hashlib.sha256(PNG).hexdigest(),
                "size_bytes": len(PNG),
                "mime_type": "image/png",
            }
            if completed
            else None
        ),
        "usage": (
            {
                "provider": "ecorex-managed-image",
                "model_id": "image-2",
                "input_units": 0,
                "output_units": 1,
                "billed_units": 7,
            }
            if completed
            else None
        ),
        "error_code": None,
    }


def test_production_config_is_postgres_only_and_bounds_process_memory() -> None:
    values = _environment()
    config = ImageProductionConfig.from_environment(values)
    assert config.storage_backend == "postgresql"
    assert config.model_allowlist == frozenset({"image-2"})
    assert config.provider_timeout_seconds == 120
    assert config.provider_generation_timeout_seconds == 300
    assert "secret" not in repr(config)

    with pytest.raises(ImageProductionConfigurationError, match="out of range"):
        ImageProductionConfig.from_environment(
            dict(values, ECOREX_IMAGE_PROVIDER_GENERATION_TIMEOUT_SECONDS="119")
        )

    sqlite = dict(values, ECOREX_IMAGE_STORAGE_BACKEND="sqlite-wal")
    with pytest.raises(ImageProductionConfigurationError, match="PostgreSQL"):
        ImageProductionConfig.from_environment(sqlite)

    undersized = dict(
        values,
        ECOREX_IMAGE_MAX_BYTES=str(64 * 1024 * 1024),
        ECOREX_IMAGE_WORKER_CONCURRENCY="16",
        ECOREX_IMAGE_WORKER_MEMORY_ENVELOPE_BYTES=str(512 * 1024 * 1024),
    )
    with pytest.raises(ImageProductionConfigurationError, match="configuration"):
        ImageProductionConfig.from_environment(undersized)

    admin_undersized = dict(
        values,
        ECOREX_IMAGE_ADMIN_MANAGEMENT_ENABLED="true",
        ECOREX_IMAGE_ADMIN_MANAGEMENT_DATABASE_PATH=str(
            (Path.cwd() / "admin-management.db").resolve()
        ),
        ECOREX_IMAGE_MODEL_PROVIDER_ORIGINS_JSON=(
            '{"ecorex_image":"https://image.ecorex.invalid"}'
        ),
        ECOREX_IMAGE_WORKER_MEMORY_ENVELOPE_BYTES=str(2 * 1024**3),
    )
    with pytest.raises(ImageProductionConfigurationError, match="configuration"):
        ImageProductionConfig.from_environment(admin_undersized)
    admin_undersized["ECOREX_IMAGE_WORKER_MEMORY_ENVELOPE_BYTES"] = str(4 * 1024**3)
    assert ImageProductionConfig.from_environment(admin_undersized).admin_management_enabled


def test_attested_local_cas_config_is_explicit_single_host_and_not_mixed(
    tmp_path: Path,
) -> None:
    values = _local_environment(tmp_path)
    config = ImageProductionConfig.from_environment(values)

    assert config.content_storage_mode == "attested-encrypted-local-cas"
    assert config.local_cas_replica_count == 1
    assert config.local_cas_root == (tmp_path / "volume" / "cas").resolve()
    assert config.s3_bucket == ""
    assert "a" * 64 not in repr(config)

    mixed = dict(values, ECOREX_IMAGE_S3_BUCKET="must-not-be-accepted")
    with pytest.raises(ImageProductionConfigurationError, match="ambiguous"):
        ImageProductionConfig.from_environment(mixed)

    wrong_replica = dict(values, ECOREX_IMAGE_LOCAL_CAS_REPLICA_COUNT="2")
    with pytest.raises(ImageProductionConfigurationError):
        ImageProductionConfig.from_environment(wrong_replica)


def test_production_config_rejects_ssrf_and_missing_auth_or_provider_secret() -> None:
    values = _environment()
    unsafe = dict(values, ECOREX_IMAGE_PROVIDER_ORIGIN="http://169.254.169.254")
    with pytest.raises(ManagedImageProviderConfigurationError):
        ImageProductionConfig.from_environment(unsafe)

    missing_auth = dict(values)
    del missing_auth["ECOREX_IMAGE_AUTH_PUBLIC_KEYS_JSON"]
    with pytest.raises(ImageProductionConfigurationError):
        ImageProductionConfig.from_environment(missing_auth)

    secrets = EnvironmentImageSecretProvider({})
    with pytest.raises(ImageProductionConfigurationError, match="unavailable"):
        secrets.read("managed-provider-bearer")


def test_managed_provider_submit_download_recover_cancel_and_health(tmp_path: Path) -> None:
    async def scenario() -> None:
        job = _job(tmp_path)
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.url.host == "image.ecorex.invalid"
            assert request.headers["authorization"] == "Bearer provider-token-000000000001"
            if request.url.path == "/v1/image/jobs":
                assert request.headers["idempotency-key"] == job.provider_idempotency_key
                submitted = json.loads(request.content)
                assert submitted["account_id"] == job.account_id
                assert submitted["job_id"] == job.job_id
                return httpx.Response(
                    200,
                    json=_response(
                        job,
                        state="completed",
                        request_id="provider-request-0001",
                    ),
                )
            if request.url.path == "/v1/image/results/provider-request-0001":
                return httpx.Response(
                    200,
                    content=PNG,
                    headers={
                        "Content-Type": "image/png",
                        "Content-Length": str(len(PNG)),
                    },
                )
            if request.url.path == "/v1/image/jobs/recover":
                return httpx.Response(200, json=_response(job, state="not_found"))
            if request.url.path == "/v1/image/jobs/cancel":
                return httpx.Response(200, json={"schema_version": 1, "cancelled": True})
            if request.url.path == "/v1/image/health":
                return httpx.Response(200, json={"schema_version": 1, "status": "ready"})
            raise AssertionError(request.url)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        )
        provider = ManagedHTTPSImageProvider(
            provider_id="ecorex-managed-image",
            origin="https://image.ecorex.invalid",
            allowed_origins=frozenset({"https://image.ecorex.invalid"}),
            allowed_models=frozenset({"image-2"}),
            bearer_token=lambda: "provider-token-000000000001",
            max_image_bytes=1024,
            client=client,
        )
        completed = await provider.submit(
            job, idempotency_key=job.provider_idempotency_key
        )
        assert completed.state is ProviderState.COMPLETED
        assert completed.payload == PNG
        assert completed.usage is not None and completed.usage.billed_units == 7
        recovered = await provider.recover(
            job,
            idempotency_key=job.provider_idempotency_key,
            provider_request_id=None,
        )
        assert recovered.state is ProviderState.NOT_FOUND
        await provider.cancel(
            job,
            idempotency_key=job.provider_idempotency_key,
            provider_request_id="provider-request-0001",
        )
        await provider.health()
        assert all(request.url.scheme == "https" for request in requests)
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [500, 503, 504])
def test_submit_5xx_is_uncertain_while_recover_is_unavailable(
    tmp_path: Path, status: int
) -> None:
    async def scenario() -> None:
        job = _job(tmp_path)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(status)),
            follow_redirects=False,
            trust_env=False,
        )
        provider = ManagedHTTPSImageProvider(
            provider_id="ecorex-managed-image",
            origin="https://image.ecorex.invalid",
            allowed_origins=frozenset({"https://image.ecorex.invalid"}),
            allowed_models=frozenset({"image-2"}),
            bearer_token=lambda: "provider-token-000000000001",
            max_image_bytes=1024,
            client=client,
        )
        with pytest.raises(ProviderUncertain):
            await provider.submit(job, idempotency_key=job.provider_idempotency_key)
        with pytest.raises(ProviderUnavailable):
            await provider.recover(
                job,
                idempotency_key=job.provider_idempotency_key,
                provider_request_id=None,
            )
        await client.aclose()

    asyncio.run(scenario())


def test_managed_provider_rejects_redirects_oversized_json_and_model_drift(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        job = _job(tmp_path)
        responses = iter(
            (
                httpx.Response(302, headers={"Location": "https://evil.invalid/result"}),
                httpx.Response(200, content=b"x" * (128 * 1024 + 1)),
            )
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: next(responses)),
            follow_redirects=False,
            trust_env=False,
        )
        provider = ManagedHTTPSImageProvider(
            provider_id="ecorex-managed-image",
            origin="https://image.ecorex.invalid",
            allowed_origins=frozenset({"https://image.ecorex.invalid"}),
            allowed_models=frozenset({"image-2"}),
            bearer_token=lambda: "provider-token-000000000001",
            max_image_bytes=1024,
            client=client,
        )
        with pytest.raises(ProviderRejected):
            await provider.submit(job, idempotency_key=job.provider_idempotency_key)
        with pytest.raises(ProviderRejected):
            await provider.submit(job, idempotency_key=job.provider_idempotency_key)
        await client.aclose()

        with pytest.raises(ManagedImageProviderConfigurationError, match="allowlisted"):
            ManagedHTTPSImageProvider(
                provider_id="ecorex-managed-image",
                origin="https://image.ecorex.invalid",
                allowed_origins=frozenset({"https://different.invalid"}),
                allowed_models=frozenset({"image-2"}),
                bearer_token=lambda: "provider-token-000000000001",
            )

    asyncio.run(scenario())


class _FakeS3:
    def __init__(self, *, public: bool = False, encrypted: bool = True) -> None:
        self.public = public
        self.encrypted = encrypted
        self.objects: dict[str, bytes] = {}
        self.put_arguments: list[dict[str, Any]] = []
        self.closed = False

    def head_bucket(self, **_kwargs):
        return {}

    def get_bucket_encryption(self, **_kwargs):
        return {
            "ServerSideEncryptionConfiguration": {
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "AES256" if self.encrypted else "none"
                        }
                    }
                ]
            }
        }

    def get_public_access_block(self, **_kwargs):
        return {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            }
        }

    def get_bucket_policy_status(self, **_kwargs):
        return {"PolicyStatus": {"IsPublic": self.public}}

    def put_object(self, **kwargs):
        self.put_arguments.append(dict(kwargs))
        payload = bytes(kwargs["Body"])
        self.objects[kwargs["Key"]] = payload
        return {"ETag": '"0123456789abcdef"'}

    def head_object(self, **kwargs):
        payload = self.objects[kwargs["Key"]]
        return {
            "ContentLength": len(payload),
            "ContentType": "application/octet-stream",
            "Metadata": {},
            "ETag": '"0123456789abcdef"',
            "ServerSideEncryption": "AES256",
        }

    def get_object(self, **kwargs):
        return {"Body": self.objects[kwargs["Key"]]}

    def delete_object(self, **kwargs):
        self.objects.pop(kwargs["Key"], None)
        return {}

    def close(self):
        self.closed = True


def test_s3_controls_require_private_encrypted_bucket_and_explicit_write_sse() -> None:
    config = ImageProductionConfig.from_environment(_environment())
    client = _FakeS3()
    dependency = _S3Dependency(client, config)
    dependency.validate_controls(write_probe=True)
    assert not client.objects
    assert client.put_arguments[-1]["ServerSideEncryption"] == "AES256"

    with pytest.raises(ImageProductionConfigurationError, match="public"):
        _S3Dependency(_FakeS3(public=True), config).validate_controls(write_probe=False)
    with pytest.raises(ImageProductionConfigurationError, match="encryption"):
        _S3Dependency(_FakeS3(encrypted=False), config).validate_controls(write_probe=False)


def test_boto_transport_applies_encryption_to_every_cas_write() -> None:
    client = _FakeS3()
    transport = BotoS3ObjectTransport(
        client,
        server_side_encryption="AES256",
    )
    transport.put_object(
        bucket="ecorex-private-images",
        key="ecorex/images/v1/blob",
        payload=PNG,
        content_type="image/png",
        metadata={"ecorex-kind": "blob"},
        checksum_sha256=hashlib.sha256(PNG).hexdigest(),
    )
    assert client.put_arguments[-1]["ServerSideEncryption"] == "AES256"
    assert client.put_arguments[-1]["IfNoneMatch"] if "IfNoneMatch" in client.put_arguments[-1] else True


class _LifecycleStore:
    def __init__(self) -> None:
        self.pings = 0
        self.closed = False

    def ping(self) -> None:
        self.pings += 1

    def close(self) -> None:
        self.closed = True


class _LifecycleS3:
    def __init__(self) -> None:
        self.probes: list[bool] = []
        self.closed = False

    def validate_controls(self, *, write_probe: bool) -> None:
        self.probes.append(write_probe)

    def close(self) -> None:
        self.closed = True


class _LifecycleProvider:
    def __init__(self) -> None:
        self.health_calls = 0
        self.closed = False

    async def health(self) -> None:
        self.health_calls += 1

    async def aclose(self) -> None:
        self.closed = True


class _LifecycleSupervisor:
    def __init__(self) -> None:
        self.healthy = False
        self.draining = False
        self.stopped = False

    async def start(self) -> None:
        self.healthy = True

    def begin_drain(self) -> None:
        self.draining = True
        self.healthy = False

    async def stop(self) -> None:
        self.stopped = True


def test_worker_lifecycle_probes_dependencies_drains_and_closes() -> None:
    async def scenario() -> None:
        config = ImageProductionConfig.from_environment(_environment())
        store = _LifecycleStore()
        s3 = _LifecycleS3()
        provider = _LifecycleProvider()
        supervisor = _LifecycleSupervisor()
        lifecycle = ImageProductionLifecycle(
            config=config,
            store=store,  # type: ignore[arg-type]
            storage=s3,  # type: ignore[arg-type]
            provider=provider,  # type: ignore[arg-type]
            supervisor=supervisor,  # type: ignore[arg-type]
        )
        await lifecycle.startup()
        assert lifecycle.live and lifecycle.accepting and supervisor.healthy
        assert await lifecycle.readiness()
        lifecycle.begin_drain()
        assert not lifecycle.accepting and supervisor.draining
        await lifecycle.shutdown()
        assert not lifecycle.live
        assert supervisor.stopped and store.closed and s3.closed and provider.closed
        assert s3.probes[0] is True

    asyncio.run(scenario())


def test_local_cas_mode_never_constructs_s3(tmp_path: Path) -> None:
    config = ImageProductionConfig.from_environment(_local_environment(tmp_path))
    dependency = _LifecycleS3()
    content = ImageContentStore(tmp_path / "fake-local-cas")

    class LocalFactory:
        def create(self, received):
            assert received is config
            return dependency, content

    class RejectS3Factory:
        def create(self, _config):
            raise AssertionError("S3 must not be constructed in local CAS mode")

    provider = PostgresS3ManagedImageProvider(
        s3_factory=RejectS3Factory(),  # type: ignore[arg-type]
        local_cas_factory=LocalFactory(),  # type: ignore[arg-type]
    )
    selected_dependency, selected_content = provider._content_storage(config)

    assert selected_dependency is dependency
    assert selected_content is content


def test_image_schema_migration_does_not_require_dynamic_admin_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Storage:
        def __init__(self) -> None:
            self.checked = False
            self.closed = False

        def validate_controls(self, *, write_probe: bool) -> None:
            assert write_probe is True
            self.checked = True

        def close(self) -> None:
            self.closed = True

    class SchemaManager:
        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://migration.invalid/ecorex"

        def migrate(self) -> SimpleNamespace:
            return SimpleNamespace(schema_version=1)

    storage = Storage()
    provider = PostgresS3ManagedImageProvider()
    config = SimpleNamespace(
        postgres_dsn="postgresql://migration.invalid/ecorex",
        storage_backend="postgresql",
        content_storage_mode="attested-encrypted-local-cas",
    )
    monkeypatch.setattr(provider, "_static_dependencies", lambda *_args: object())
    monkeypatch.setattr(
        provider, "_content_storage", lambda _config: (storage, object())
    )
    monkeypatch.setattr(
        provider,
        "_external_dependencies",
        lambda *_args: pytest.fail("dynamic models must not resolve during migration"),
    )
    monkeypatch.setattr(image_production, "PostgresImageSchemaManager", SchemaManager)

    report = provider.migrate(config, object())

    assert storage.checked and storage.closed
    assert report.provider_checked is False


class _SupervisorStore:
    def __init__(self) -> None:
        self.reclaims = 0

    def reclaim_expired(self) -> int:
        self.reclaims += 1
        return 0


def test_supervisor_enforces_local_concurrency_and_drain_boundary() -> None:
    async def scenario() -> None:
        class BlockingWorker:
            def __init__(self) -> None:
                self.store = _SupervisorStore()
                self.active = 0
                self.maximum = 0
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def run_once(self, worker_id: str) -> ImageWorkerResult:
                assert worker_id.startswith("img-replica-a-")
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                if self.active == 4:
                    self.entered.set()
                try:
                    await self.release.wait()
                    return ImageWorkerResult(
                        ImageWorkerOutcome.COMPLETED,
                        "imgjob_" + "0" * 32,
                    )
                finally:
                    self.active -= 1

        worker = BlockingWorker()
        supervisor = ImageWorkerSupervisor(
            worker,  # type: ignore[arg-type]
            concurrency=4,
            shutdown_seconds=1,
            worker_id_prefix="img-replica-a",
        )
        await supervisor.start()
        await asyncio.wait_for(worker.entered.wait(), timeout=1)
        snapshot = supervisor.snapshot()
        assert snapshot.in_flight == 4
        assert snapshot.healthy
        worker.release.set()
        await supervisor.stop()
        assert worker.maximum == 4
        assert worker.store.reclaims == 1
        assert supervisor.snapshot().in_flight == 0
        assert not supervisor.snapshot().accepting

    asyncio.run(scenario())


def test_supervisor_timeout_cancels_inflight_without_leasing_more_work() -> None:
    async def scenario() -> None:
        class StuckWorker:
            def __init__(self) -> None:
                self.store = _SupervisorStore()
                self.started = asyncio.Event()
                self.cancelled = 0
                self.calls = 0

            async def run_once(self, _worker_id: str) -> ImageWorkerResult:
                self.calls += 1
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled += 1
                    raise

        worker = StuckWorker()
        supervisor = ImageWorkerSupervisor(
            worker,  # type: ignore[arg-type]
            concurrency=1,
            shutdown_seconds=0.1,
            worker_id_prefix="img-replica-stuck",
        )
        await supervisor.start()
        await asyncio.wait_for(worker.started.wait(), timeout=1)
        await supervisor.stop()
        assert worker.calls == 1
        assert worker.cancelled == 1
        assert supervisor.snapshot().in_flight == 0
        assert not supervisor.snapshot().running

    asyncio.run(scenario())


class _AppLifecycle:
    def __init__(self) -> None:
        self.accepting = False
        self.live = False

    async def startup(self) -> None:
        self.live = True
        self.accepting = True

    async def readiness(self) -> bool:
        return self.accepting

    def begin_drain(self) -> None:
        self.accepting = False

    async def shutdown(self) -> None:
        self.accepting = False
        self.live = False

    async def force_close(self) -> None:
        await self.shutdown()


def test_production_health_and_drain_are_real_asgi_state(tmp_path: Path) -> None:
    database = tmp_path / "health.db"
    SQLiteImageSchemaManager(database).migrate()
    store = SQLiteImageJobStore(database)
    service = ImageOrchestrationService(
        store,
        allowed_models=frozenset({"image-2"}),
    )
    lifecycle = _AppLifecycle()

    class Authenticator:
        def authenticate(self, _token: str):
            raise PermissionError

    bundle = SimpleNamespace(
        lifecycle=lifecycle,
        service=service,
        content_store=ImageContentStore(tmp_path / "health-cas"),
        config=SimpleNamespace(
            api_blob_memory_envelope_bytes=512 * 1024 * 1024
        ),
        authenticator=Authenticator(),
    )
    app = create_image_production_app(bundle, include_api=True)  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/v1/images/metrics").status_code == 401
        lifecycle.begin_drain()
        draining = client.get("/api/v1/images/metrics")
        assert draining.status_code == 503
        assert draining.json() == {"status": "draining"}
        # Health remains reachable while orchestration admission is fenced.
        assert client.get("/health/ready").status_code == 503


def test_image_access_token_projects_only_signed_account_entitlements() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    authenticator = Ed25519ImageJWTAuthenticator(
        public_keys={"image-key-1": public},
        issuer="https://identity.ecorex.invalid",
        audience="ecorex-image",
        service_model_ids=frozenset({"image-2", "image-3"}),
        clock=lambda: datetime.fromtimestamp(1_788_825_600, UTC),
    )

    principal = authenticator.authenticate(
        _access_token(private, allowed_model_ids=["image-2", "not-deployed"])
    )
    assert principal.account_id == "account-001"
    assert principal.allowed_model_ids == frozenset({"image-2"})
    assert principal.quota_period == "2026-09"
    assert principal.request_limit == 100
    assert principal.concurrent_request_limit == 4

    with pytest.raises(PermissionError, match="incomplete"):
        authenticator.authenticate(
            _access_token(
                private,
                allowed_model_ids=["image-2"],
                include_quota=False,
            )
        )
    with pytest.raises(PermissionError, match="empty"):
        authenticator.authenticate(
            _access_token(private, allowed_model_ids=["not-deployed"])
        )


def test_production_api_rejects_a_service_model_missing_from_account_token(
    tmp_path: Path,
) -> None:
    database = tmp_path / "account-model-entitlement.db"
    SQLiteImageSchemaManager(database).migrate()
    store = SQLiteImageJobStore(database)
    service = ImageOrchestrationService(
        store,
        allowed_models=frozenset({"image-2", "image-3"}),
        max_output_count=1,
    )
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    authenticator = Ed25519ImageJWTAuthenticator(
        public_keys={"image-key-1": public},
        issuer="https://identity.ecorex.invalid",
        audience="ecorex-image",
        service_model_ids=frozenset({"image-2", "image-3"}),
        clock=lambda: datetime.fromtimestamp(1_788_825_600, UTC),
    )
    lifecycle = _AppLifecycle()
    bundle = SimpleNamespace(
        lifecycle=lifecycle,
        service=service,
        content_store=ImageContentStore(tmp_path / "account-model-cas"),
        config=SimpleNamespace(
            api_blob_memory_envelope_bytes=512 * 1024 * 1024
        ),
        authenticator=authenticator,
    )
    token = _access_token(private, allowed_model_ids=["image-2"])
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "operation": "generate",
        "model_id": "image-3",
        "client_request_id": "account-model-request-0001",
        "prompt": "draw the office workflow",
    }

    app = create_image_production_app(bundle, include_api=True)  # type: ignore[arg-type]
    with TestClient(app) as client:
        forbidden = client.post("/api/v1/images/jobs", headers=headers, json=body)
        assert forbidden.status_code == 403
        assert forbidden.json() == {"detail": "image model is not authorized"}
        assert service.metrics("account-001").queued == 0

        body["model_id"] = "image-2"
        body["client_request_id"] = "account-model-request-0002"
        accepted = client.post("/api/v1/images/jobs", headers=headers, json=body)
        assert accepted.status_code == 202
        assert accepted.json()["job"]["model_id"] == "image-2"
        assert service.metrics("account-001").queued == 1


def test_service_model_allowlist_is_authoritative(tmp_path: Path) -> None:
    database = tmp_path / "allowlist.db"
    SQLiteImageSchemaManager(database).migrate()
    service = ImageOrchestrationService(
        SQLiteImageJobStore(database),
        allowed_models=frozenset({"image-2"}),
        max_output_count=1,
    )
    with pytest.raises(ValueError, match="not available"):
        service.submit(
            "account-001",
            ImageSubmitRequest(
                operation=ImageOperation.GENERATE,
                model_id="unmanaged-image",
                client_request_id="disallowed-model-request-0001",
                prompt="this must never enter the durable queue",
            ),
        )
    with pytest.raises(ValueError, match="output count"):
        service.submit(
            "account-001",
            ImageSubmitRequest(
                operation=ImageOperation.GENERATE,
                model_id="image-2",
                client_request_id="multi-output-request-0001",
                prompt="each output needs an independent durable job",
                count=2,
            ),
        )


@dataclass
class _FakeReport:
    def to_dict(self) -> Mapping[str, Any]:
        return {"schema_version": 1, "status": "checked"}


class _FakeLifecycle:
    async def force_close(self) -> None:
        return None


@dataclass
class _FakeBundle:
    lifecycle: _FakeLifecycle


class _FakeProductionProvider:
    def __init__(self) -> None:
        self.modes: list[str] = []

    def migrate(self, _config, _secrets):
        return _FakeReport()

    def check(self, _config, _secrets):
        return _FakeReport()

    def compose(self, _config, _secrets, *, mode: str):
        self.modes.append(mode)
        return _FakeBundle(_FakeLifecycle())


@pytest.mark.parametrize("mode", ["serve", "worker", "all"])
def test_production_cli_has_independently_scalable_modes(mode: str) -> None:
    selected = _FakeProductionProvider()
    seen: list[object] = []
    assert image_main(
        [mode],
        environment=_environment(),
        provider=selected,  # type: ignore[arg-type]
        server_runner=seen.append,  # type: ignore[arg-type]
    ) == 0
    assert selected.modes == [mode]
    assert len(seen) == 1


def test_cli_failure_output_never_contains_dsn_or_provider_token(capsys) -> None:
    values = _environment()
    secret_dsn = values["ECOREX_IMAGE_POSTGRES_DSN"]
    secret_token = values["ECOREX_IMAGE_PROVIDER_BEARER_TOKEN"]

    class FailingProvider(_FakeProductionProvider):
        def check(self, _config, _secrets):
            raise RuntimeError(secret_dsn + secret_token)

    assert image_main(
        ["schema", "check"],
        environment=values,
        provider=FailingProvider(),  # type: ignore[arg-type]
    ) == 2
    output = capsys.readouterr().err
    assert secret_dsn not in output
    assert secret_token not in output
    assert json.loads(output)["status"] == "failed"
