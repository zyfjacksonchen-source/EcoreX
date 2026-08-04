from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import sqlite3
from types import SimpleNamespace

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
import pytest

from ecorex.control_plane.device_identity import (
    DeviceAccountIdentity,
    DeviceIdentitySecrets,
    DeviceIdentityUnauthorized,
    DeviceRefreshRequired,
    ManagedDeviceIdentityBroker,
)
from ecorex.control_plane.device_identity_router import create_device_identity_router
from ecorex.control_plane.device_identity_schema import (
    DEVICE_IDENTITY_MIGRATION_NAME,
    DEVICE_IDENTITY_SCHEMA_SQL,
    DeviceIdentitySchemaManager,
)
from ecorex.control_plane.management import (
    AdminManagementRepository,
    AdminPasswordAuthenticationError,
    AdminPasswordLocked,
)
from ecorex.control_plane.management_models import CreateAdminUserRequest
from ecorex.control_plane.management_schema import AdminManagementSchemaManager
from ecorex.control_plane.models import ControlPrincipal
from ecorex.release.signing import Ed25519MemorySigner
from ecorex.security import Ed25519AccessTokenVerifier
from ecorex.session import Ed25519SessionLeaseVerifier, HTTPSDeviceAuthorizationBroker


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class Directory:
    def __init__(self) -> None:
        self.auth_epoch = 0

    def resolve(self, account_id: str) -> DeviceAccountIdentity:
        if account_id != "acct-1":
            raise RuntimeError("unknown account")
        return DeviceAccountIdentity(
            account_id=account_id,
            organization_id="org-1",
            display_name="EcoreX User",
            roles=("user",),
            model_allowlist=("gpt-5.6-sol", "gpt-image-2"),
            quota={"managed_requests": 100, "concurrent_requests": 4},
            auth_epoch=self.auth_epoch,
        )


def test_device_identity_v1_schema_migrates_to_revocation_authority(
    tmp_path,
) -> None:
    path = tmp_path / "device-v1.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(DEVICE_IDENTITY_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO device_identity_schema_migrations("
            "version,migration_name,migration_checksum,installed_at) "
            "VALUES(1,?,?,?)",
            (
                DEVICE_IDENTITY_MIGRATION_NAME,
                hashlib.sha256(DEVICE_IDENTITY_SCHEMA_SQL.encode()).hexdigest(),
                NOW.isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    receipt = DeviceIdentitySchemaManager(path).migrate()
    assert receipt.migration_version == 2
    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
    finally:
        connection.close()
    assert {
        "device_identity_grant_authority",
        "device_identity_revocations",
    } <= tables


def public(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


@pytest.fixture
def identity(tmp_path):
    access_private = Ed25519PrivateKey.generate()
    lease_private = Ed25519PrivateKey.generate()
    broker = ManagedDeviceIdentityBroker(
        tmp_path / "control.db",
        account_directory=Directory(),
        access_signer=Ed25519MemorySigner("access-key", access_private),
        lease_signer=Ed25519MemorySigner("session-key", lease_private),
        secrets=DeviceIdentitySecrets(b"a" * 32, b"b" * 32),
        issuer="https://identity.ecorex.test",
        audience="ecorex-managed-runtime",
        verification_url="https://identity.ecorex.test/device",
        allowed_client_ids=frozenset({"ecorex-webui"}),
        clock=lambda: NOW,
    )
    return broker, access_private, lease_private, tmp_path / "control.db"


def test_broker_is_idempotent_signed_durable_and_secret_free(identity) -> None:
    broker, access_private, lease_private, database = identity
    first = broker.begin(
        client_id="ecorex-webui", idempotency_key="device-begin-request-0001"
    )
    replay = broker.begin(
        client_id="ecorex-webui", idempotency_key="device-begin-request-0001"
    )
    assert replay == first
    pending = broker.poll(
        client_id="ecorex-webui",
        provider_flow_id=first.provider_flow_id,
        device_code=first.device_code,
        idempotency_key="device-poll-request-0001",
    )
    assert pending.status == "pending"

    lease = broker.approve(user_code=first.user_code, account_id="acct-1")
    same = broker.approve(user_code=first.user_code, account_id="acct-1")
    assert same.digest == lease.digest
    granted = broker.poll(
        client_id="ecorex-webui",
        provider_flow_id=first.provider_flow_id,
        device_code=first.device_code,
        idempotency_key="device-poll-request-0002",
    )
    assert granted.status == "authorized"
    assert granted.lease is not None
    assert (
        granted.lease.claims.expires_at - granted.lease.claims.issued_at
        == timedelta(hours=72)
    )
    access_verifier = Ed25519AccessTokenVerifier(
        {"access-key": public(access_private)},
        issuer="https://identity.ecorex.test",
        audience="ecorex-managed-runtime",
        clock=lambda: NOW,
    )
    claims = access_verifier.verify(str(granted.access_token))
    assert claims.account_id == "acct-1"
    lease_verifier = Ed25519SessionLeaseVerifier({"session-key": public(lease_private)})
    assert (
        lease_verifier.verify(
            granted.lease,
            now=NOW,
            access_token=str(granted.access_token),
            refresh_token=str(granted.refresh_token),
        )
        is True
    )
    refreshed = broker.refresh(
        client_id="ecorex-webui",
        lease_id=granted.lease.claims.lease_id,
        refresh_token=str(granted.refresh_token),
        idempotency_key=f"session-refresh:{granted.lease.digest}",
    )
    assert refreshed.lease is not None
    assert refreshed.lease.claims.revision == granted.lease.claims.revision + 1
    assert refreshed.lease.claims.expires_at == granted.lease.claims.expires_at
    assert refreshed.access_token != granted.access_token
    assert refreshed.refresh_token != granted.refresh_token
    replayed_refresh = broker.refresh(
        client_id="ecorex-webui",
        lease_id=granted.lease.claims.lease_id,
        refresh_token=str(granted.refresh_token),
        idempotency_key=f"session-refresh:{granted.lease.digest}",
    )
    assert replayed_refresh.access_token == refreshed.access_token

    raw = database.read_bytes()
    for secret in (
        first.device_code,
        str(granted.access_token),
        str(granted.refresh_token),
    ):
        assert secret.encode() not in raw
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM device_identity_grants"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM device_identity_audit").fetchone()[
                0
            ]
            >= 2
        )


def test_cloud_revoke_is_idempotent_and_blocks_refresh(identity) -> None:
    broker, _access, _lease, _database = identity
    granted = broker.grant_account(
        client_id="ecorex-webui",
        account_id="acct-1",
        idempotency_key="password-grant-for-revoke-0001",
    )
    assert granted.lease is not None
    first = broker.revoke(
        client_id="ecorex-webui",
        lease_id=granted.lease.claims.lease_id,
        account_id="acct-1",
        refresh_token=str(granted.refresh_token),
        idempotency_key="session-revoke-request-0001",
    )
    replay = broker.revoke(
        client_id="ecorex-webui",
        lease_id=granted.lease.claims.lease_id,
        account_id="acct-1",
        refresh_token=str(granted.refresh_token),
        idempotency_key="session-revoke-request-0001",
    )
    assert first.already_revoked is False
    assert replay.already_revoked is True
    with pytest.raises(DeviceRefreshRequired):
        broker.refresh(
            client_id="ecorex-webui",
            lease_id=granted.lease.claims.lease_id,
            refresh_token=str(granted.refresh_token),
            idempotency_key="refresh-after-revoke-0001",
        )


def test_password_epoch_invalidates_access_and_revokes_every_lease(identity) -> None:
    broker, access_private, _lease, _database = identity
    first = broker.grant_account(
        client_id="ecorex-webui",
        account_id="acct-1",
        idempotency_key="password-grant-all-revoke-0001",
    )
    second = broker.grant_account(
        client_id="ecorex-webui",
        account_id="acct-1",
        idempotency_key="password-grant-all-revoke-0002",
    )
    verifier = Ed25519AccessTokenVerifier(
        {"access-key": public(access_private)},
        issuer="https://identity.ecorex.test",
        audience="ecorex-managed-runtime",
        clock=lambda: NOW,
    )
    first_claims = verifier.verify(str(first.access_token))
    assert first_claims.token_id is not None
    assert broker.access_token_is_current(
        account_id="acct-1", token_id=first_claims.token_id
    ) is True
    broker.account_directory.auth_epoch = 1
    assert broker.access_token_is_current(
        account_id="acct-1", token_id=first_claims.token_id
    ) is False
    assert broker.revoke_account_sessions(
        account_id="acct-1",
        idempotency_key="password-change-revoke-all-0001",
    ) == 2
    assert broker.revoke_account_sessions(
        account_id="acct-1",
        idempotency_key="password-change-revoke-all-0001",
    ) == 0
    for grant, suffix in ((first, "first"), (second, "second")):
        with pytest.raises(DeviceRefreshRequired):
            broker.refresh(
                client_id="ecorex-webui",
                lease_id=grant.lease.claims.lease_id,
                refresh_token=str(grant.refresh_token),
                idempotency_key=f"refresh-after-password-{suffix}",
            )


def test_https_runtime_client_matches_exact_authorize_and_token_contract(
    identity,
) -> None:
    asyncio.run(_exercise_https_runtime_client(identity))


def test_password_login_uses_transport_source_and_maps_lockout(identity, monkeypatch) -> None:
    asyncio.run(_exercise_password_login_boundary(identity, monkeypatch))


def test_self_service_password_route_changes_login_and_revokes_access(identity) -> None:
    asyncio.run(_exercise_self_service_password_route(identity))


async def _exercise_self_service_password_route(identity) -> None:
    broker, access_private, _lease, database = identity
    AdminManagementSchemaManager(database).migrate()
    repository = AdminManagementRepository(database, encryption_key=b"p" * 32)
    admin = ControlPrincipal(
        subject="admin", client_id="tests", account_id="admin",
        roles=frozenset({"platform_admin"}),
    )
    repository.create_user(
        CreateAdminUserRequest(
            account_id="acct-1",
            display_name="e-Mate User",
            email="user@example.com",
            organization_id="org-1",
            token_limit=100,
            image_limit=10,
            password="original-password",
            client_request_id="create-password-route-user",
        ),
        actor=admin,
    )
    broker.account_directory.auth_epoch = 1
    verifier = Ed25519AccessTokenVerifier(
        {"access-key": public(access_private)},
        issuer="https://identity.ecorex.test",
        audience="ecorex-managed-runtime",
        clock=lambda: NOW,
    )

    def account(request: Request) -> ControlPrincipal:
        token = request.headers["authorization"].removeprefix("Bearer ")
        claims = verifier.verify(token)
        return ControlPrincipal(
            subject=claims.subject,
            client_id=claims.client_id,
            account_id=claims.account_id,
            organization_id=claims.organization_id,
            roles=claims.roles,
            token_id=claims.token_id,
        )

    app = FastAPI()
    app.include_router(
        create_device_identity_router(
            broker,
            account_dependency=account,
            password_repository=repository,
        )
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://identity.ecorex.test",
    )
    runtime = HTTPSDeviceAuthorizationBroker(
        "https://identity.ecorex.test",
        client_id="ecorex-webui",
        allowed_hosts=frozenset({"identity.ecorex.test"}),
        client=client,
    )
    grant = await runtime.login(
        identifier="user@example.com",
        password="original-password",
        idempotency_key="password-route-login-0001",
    )
    claims = verifier.verify(grant.access_token)
    changed = await runtime.change_password(
        current_password="original-password",
        new_password="replacement-password",
        access_token=grant.access_token,
        client_request_id="password-route-change-0001",
        idempotency_key="password-route-change-0001",
    )
    assert changed.reauthentication_required is True
    assert claims.token_id is not None
    assert broker.access_token_is_current(
        account_id="acct-1", token_id=claims.token_id
    ) is False
    with pytest.raises(AdminPasswordAuthenticationError):
        repository.authenticate_password("acct-1", "original-password")
    assert repository.authenticate_password(
        "USER@EXAMPLE.COM", "replacement-password"
    ).account_id == "acct-1"
    await client.aclose()


async def _exercise_password_login_boundary(identity, monkeypatch) -> None:
    broker, _access, _lease, database = identity
    AdminManagementSchemaManager(database).migrate()
    repository = AdminManagementRepository(database, encryption_key=b"p" * 32)
    observed: list[tuple[str, str | None]] = []

    def authenticate(identifier: str, password: str, *, source_ip: str | None = None):
        observed.append((identifier, source_ip))
        assert password == "abcd1234"
        return SimpleNamespace(account_id="acct-1")

    monkeypatch.setattr(repository, "authenticate_password", authenticate)
    app = FastAPI()
    app.include_router(
        create_device_identity_router(
            broker,
            password_repository=repository,
        )
    )
    transport = httpx.ASGITransport(
        app=app,
        client=("203.0.113.19", 43100),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://identity.ecorex.test",
    ) as client:
        response = await client.post(
            "/v1/session/login",
            headers={
                "Idempotency-Key": "password-login-route-0001",
                "X-Forwarded-For": "198.51.100.200",
            },
            json={
                "schema_version": 1,
                "client_id": "ecorex-webui",
                "identifier": "user@example.com",
                "password": "abcd1234",
            },
        )
        assert response.status_code == 200
        assert observed == [("user@example.com", "203.0.113.19")]

        spoofed = await client.post(
            "/v1/session/login",
            headers={
                "Idempotency-Key": "password-login-route-0001-spoof",
                "X-Real-IP": "198.51.100.201",
            },
            json={
                "schema_version": 1,
                "client_id": "ecorex-webui",
                "identifier": "user@example.com",
                "password": "abcd1234",
            },
        )
        assert spoofed.status_code == 200
        assert observed[-1] == ("user@example.com", "203.0.113.19")

        loopback_transport = httpx.ASGITransport(
            app=app,
            client=("127.0.0.1", 43101),
        )
        async with httpx.AsyncClient(
            transport=loopback_transport,
            base_url="https://identity.ecorex.test",
        ) as proxy_client:
            for index, source in enumerate(("198.51.100.21", "198.51.100.22")):
                proxied = await proxy_client.post(
                    "/v1/session/login",
                    headers={
                        "Idempotency-Key": (
                            f"password-login-route-proxy-{index:04d}"
                        ),
                        "X-Real-IP": source,
                        "X-Forwarded-For": "203.0.113.250, 203.0.113.251",
                    },
                    json={
                        "schema_version": 1,
                        "client_id": "ecorex-webui",
                        "identifier": "user@example.com",
                        "password": "abcd1234",
                    },
                )
                assert proxied.status_code == 200
            assert observed[-2:] == [
                ("user@example.com", "198.51.100.21"),
                ("user@example.com", "198.51.100.22"),
            ]

            malformed = await proxy_client.post(
                "/v1/session/login",
                headers={
                    "Idempotency-Key": "password-login-route-proxy-invalid",
                    "X-Real-IP": "198.51.100.21, 198.51.100.22",
                },
                json={
                    "schema_version": 1,
                    "client_id": "ecorex-webui",
                    "identifier": "user@example.com",
                    "password": "abcd1234",
                },
            )
            assert malformed.status_code == 400
            assert malformed.json()["detail"]["code"] == "invalid_proxy_client_ip"

        def locked(*_args, **_kwargs):
            raise AdminPasswordLocked(17)

        monkeypatch.setattr(repository, "authenticate_password", locked)
        limited = await client.post(
            "/v1/session/login",
            headers={"Idempotency-Key": "password-login-route-0002"},
            json={
                "schema_version": 1,
                "client_id": "ecorex-webui",
                "identifier": "user@example.com",
                "password": "abcd1234",
            },
        )
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "17"
        assert limited.json()["detail"]["code"] == "password_login_rate_limited"

        def invalid(*_args, **_kwargs):
            raise AdminPasswordAuthenticationError("private detail")

        monkeypatch.setattr(repository, "authenticate_password", invalid)
        denied = await client.post(
            "/v1/session/login",
            headers={"Idempotency-Key": "password-login-route-0003"},
            json={
                "schema_version": 1,
                "client_id": "ecorex-webui",
                "identifier": "user@example.com",
                "password": "abcd1234",
            },
        )
        assert denied.status_code == 401
        assert denied.json()["detail"] == {
            "code": "invalid_credentials",
            "message": "account login failed",
        }


async def _exercise_https_runtime_client(identity) -> None:
    broker, _access, _lease, _database = identity
    app = FastAPI()
    app.include_router(create_device_identity_router(broker))
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(
        transport=transport, base_url="https://identity.ecorex.test"
    )
    runtime = HTTPSDeviceAuthorizationBroker(
        "https://identity.ecorex.test",
        client_id="ecorex-webui",
        allowed_hosts=frozenset({"identity.ecorex.test"}),
        client=client,
    )
    challenge = await runtime.begin(idempotency_key="runtime-device-begin-0001")
    before = await runtime.poll(
        provider_flow_id=challenge.provider_flow_id,
        device_code=challenge.device_code,
        idempotency_key="runtime-device-poll-0001",
    )
    assert before.status.value == "pending"
    broker.approve(user_code=challenge.user_code, account_id="acct-1")
    after = await runtime.poll(
        provider_flow_id=challenge.provider_flow_id,
        device_code=challenge.device_code,
        idempotency_key="runtime-device-poll-0002",
    )
    assert after.status.value == "authorized"
    assert after.grant is not None
    refreshed = await runtime.refresh(
        lease_id=after.grant.lease.claims.lease_id,
        refresh_token=after.grant.refresh_token,
        idempotency_key=f"session-refresh:{after.grant.lease.digest}",
    )
    assert refreshed.lease.claims.revision == after.grant.lease.claims.revision + 1
    revoked = await runtime.revoke(
        lease_id=refreshed.lease.claims.lease_id,
        account_id=refreshed.lease.claims.account_id,
        refresh_token=refreshed.refresh_token,
        idempotency_key="runtime-session-revoke-0001",
    )
    assert revoked.already_revoked is False
    replayed = await runtime.revoke(
        lease_id=refreshed.lease.claims.lease_id,
        account_id=refreshed.lease.claims.account_id,
        refresh_token=refreshed.refresh_token,
        idempotency_key="runtime-session-revoke-0001",
    )
    assert replayed.already_revoked is True
    await client.aclose()


def test_legacy_credential_migration_is_idempotent_and_never_restores_sessions(
    identity,
) -> None:
    broker, _access, _lease, database = identity
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE legacy_deleted_sessions(session_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO legacy_deleted_sessions(session_id) VALUES('deleted-session')"
        )
        connection.commit()
    credential = "legacy-user-token-very-secret"
    source_hash = hashlib.sha256(b"legacy-record-1").hexdigest()
    record = {
        "account_id": "acct-1",
        "credential_sha256": hashlib.sha256(credential.encode()).hexdigest(),
        "display_name": "EcoreX User",
        "email": "user@example.com",
        "role": "member",
        "daily_token_limit": 0,
        "weekly_token_limit": 0,
        "session_expires_at": "2026-07-17T12:00:00Z",
        "source_record_sha256": source_hash,
    }
    assert broker.import_legacy_credentials([record]) == {"imported": 1, "replayed": 0}
    assert broker.import_legacy_credentials([record]) == {"imported": 0, "replayed": 1}
    challenge = broker.begin(
        client_id="ecorex-webui", idempotency_key="legacy-device-begin-0001"
    )
    assert (
        broker.verify_legacy_credential(
            user_code=challenge.user_code,
            credential=credential,
        )
        == "acct-1"
    )
    granted = broker.poll(
        client_id="ecorex-webui",
        provider_flow_id=challenge.provider_flow_id,
        device_code=challenge.device_code,
        idempotency_key="legacy-device-poll-0001",
    )
    assert granted.status == "authorized"
    raw = database.read_bytes()
    assert credential.encode() not in raw
    with sqlite3.connect(database) as connection:
        # Identity migration has no write path into conversation/session state.
        assert connection.execute(
            "SELECT session_id FROM legacy_deleted_sessions"
        ).fetchall() == [("deleted-session",)]


def test_legacy_verification_locks_flow_after_bounded_failures(identity) -> None:
    broker, *_ = identity
    challenge = broker.begin(
        client_id="ecorex-webui", idempotency_key="failed-device-begin-0001"
    )
    for _ in range(5):
        with pytest.raises(DeviceIdentityUnauthorized):
            broker.verify_legacy_credential(
                user_code=challenge.user_code,
                credential="wrong-legacy-credential",
            )
    result = broker.poll(
        client_id="ecorex-webui",
        provider_flow_id=challenge.provider_flow_id,
        device_code=challenge.device_code,
        idempotency_key="failed-device-poll-0001",
    )
    assert result.status == "denied"
