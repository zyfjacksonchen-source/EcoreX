from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import sqlite3

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
import pytest

from ecorex.control_plane.device_identity import (
    DeviceAccountIdentity,
    DeviceIdentitySecrets,
    DeviceIdentityUnauthorized,
    ManagedDeviceIdentityBroker,
)
from ecorex.control_plane.device_identity_router import create_device_identity_router
from ecorex.release.signing import Ed25519MemorySigner
from ecorex.security import Ed25519AccessTokenVerifier
from ecorex.session import Ed25519SessionLeaseVerifier, HTTPSDeviceAuthorizationBroker


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class Directory:
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
        )


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


def test_https_runtime_client_matches_exact_authorize_and_token_contract(
    identity,
) -> None:
    asyncio.run(_exercise_https_runtime_client(identity))


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
