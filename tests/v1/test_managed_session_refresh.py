from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from ecorex.control_plane.device_identity import (
    DeviceAccountIdentity,
    DeviceIdentitySecrets,
    ManagedDeviceIdentityBroker,
)
from ecorex.release.signing import Ed25519MemorySigner
from ecorex.session import (
    DeviceRefreshInvalidGrant,
    Ed25519SessionLeaseVerifier,
    ManagedSessionRefreshService,
    ManagedSessionRefreshSupervisor,
    ManagedSessionService,
    SessionReauthorizationRequired,
)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Vault:
    def __init__(self) -> None:
        self.values = {}

    def put(self, key, value) -> None:
        self.values[key] = dict(value)

    def get(self, key):
        if key not in self.values:
            raise KeyError(key)
        return dict(self.values[key])

    def delete(self, key) -> None:
        self.values.pop(key, None)


class Directory:
    def resolve(self, account_id: str) -> DeviceAccountIdentity:
        return DeviceAccountIdentity(
            account_id=account_id,
            organization_id="org-refresh",
            display_name="Refresh User",
            roles=("user",),
            model_allowlist=("gpt-5.6-sol",),
            quota={"managed_requests": 100, "concurrent_requests": 4},
        )


class AsyncBroker:
    def __init__(self, broker: ManagedDeviceIdentityBroker) -> None:
        self.broker = broker
        self.calls = 0

    async def refresh(self, **kwargs):
        self.calls += 1
        return self.broker.refresh(client_id="ecorex-webui", **kwargs)


class InvalidBroker:
    async def refresh(self, **_kwargs):
        raise DeviceRefreshInvalidGrant("invalid")


def _public(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _fixture(tmp_path, *, phase_hook=None):
    clock = Clock(datetime(2026, 7, 16, 12, 0, tzinfo=UTC))
    access = Ed25519PrivateKey.generate()
    lease_key = Ed25519PrivateKey.generate()
    cloud = ManagedDeviceIdentityBroker(
        tmp_path / "cloud.db",
        account_directory=Directory(),
        access_signer=Ed25519MemorySigner("access-key", access),
        lease_signer=Ed25519MemorySigner("lease-key", lease_key),
        secrets=DeviceIdentitySecrets(b"a" * 32, b"b" * 32),
        issuer="https://identity.ecorex.test",
        audience="ecorex-runtime",
        verification_url="https://identity.ecorex.test/device",
        allowed_client_ids=frozenset({"ecorex-webui"}),
        clock=clock,
    )
    challenge = cloud.begin(
        client_id="ecorex-webui", idempotency_key="refresh-test-begin"
    )
    cloud.approve(user_code=challenge.user_code, account_id="account-refresh")
    grant = cloud.poll(
        client_id="ecorex-webui",
        provider_flow_id=challenge.provider_flow_id,
        device_code=challenge.device_code,
        idempotency_key="refresh-test-poll",
    ).to_dict()
    vault = Vault()
    session = ManagedSessionService(
        tmp_path / "runtime.db",
        vault=vault,
        verifier=Ed25519SessionLeaseVerifier({"lease-key": _public(lease_key)}),
        clock=clock,
        phase_hook=phase_hook,
    )
    from ecorex.session import SignedManagedSessionLease

    initial = SignedManagedSessionLease.from_dict(grant["lease"])
    session.install(
        initial,
        access_token=grant["access_token"],
        refresh_token=grant["refresh_token"],
        client_request_id="refresh-test-login",
    )
    return clock, cloud, session, vault


def test_refresh_is_single_flight_atomic_and_preserves_policy_expiry(tmp_path) -> None:
    clock, cloud, session, _vault = _fixture(tmp_path)
    original = session.snapshot()
    clock.now += timedelta(minutes=13)
    transport = AsyncBroker(cloud)
    refresh = ManagedSessionRefreshService(
        tmp_path / "runtime.db", session=session, broker=transport, clock=clock
    )

    async def exercise():
        return await asyncio.gather(refresh.refresh_if_due(), refresh.refresh_if_due())

    asyncio.run(exercise())
    active = session.snapshot()
    assert active.revision == original.revision + 1
    assert active.expires_at == original.expires_at
    assert transport.calls == 1
    assert refresh.repository.projection().status == "idle"


def test_refresh_crash_after_session_commit_recovers_without_duplicate(
    tmp_path,
) -> None:
    crashed = {"armed": False, "value": False}

    def hook(phase, _identity):
        if phase == "committed" and crashed["armed"] and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated crash")

    clock, cloud, session, _vault = _fixture(tmp_path, phase_hook=hook)
    crashed["armed"] = True
    clock.now += timedelta(minutes=13)
    refresh = ManagedSessionRefreshService(
        tmp_path / "runtime.db",
        session=session,
        broker=AsyncBroker(cloud),
        clock=clock,
    )
    with pytest.raises(Exception, match="failed safely"):
        asyncio.run(refresh.refresh_if_due())
    assert session.snapshot().revision == 2
    recovered = ManagedSessionRefreshService(
        tmp_path / "runtime.db",
        session=session,
        broker=AsyncBroker(cloud),
        clock=clock,
        initialize=False,
    )
    assert recovered.recover().status == "idle"
    assert session.snapshot().revision == 2


def test_invalid_grant_requires_reauthorization_without_deleting_lease_history(
    tmp_path,
) -> None:
    clock, _cloud, session, _vault = _fixture(tmp_path)
    original = session.read_data_scope_snapshot()
    clock.now += timedelta(minutes=13)
    refresh = ManagedSessionRefreshService(
        tmp_path / "runtime.db", session=session, broker=InvalidBroker(), clock=clock
    )
    with pytest.raises(SessionReauthorizationRequired):
        asyncio.run(refresh.refresh_if_due())
    assert refresh.repository.projection().status == "reauthorization_required"
    assert session.read_data_scope_snapshot().lease_digest == original.lease_digest
    assert any(
        record.event_type == "session.refresh.reauthorization_required"
        for record in session.repository.audit_records()
    )


def test_invalid_local_access_token_persists_reauthorization_and_supervisor_lives(
    tmp_path,
) -> None:
    clock, cloud, session, vault = _fixture(tmp_path)
    credential_ref = next(iter(vault.values))
    vault.values[credential_ref]["access_token"] = "invalid-access-token"
    refresh = ManagedSessionRefreshService(
        tmp_path / "runtime.db",
        session=session,
        broker=AsyncBroker(cloud),
        clock=clock,
    )
    supervisor = ManagedSessionRefreshSupervisor(refresh, poll_seconds=1)

    async def exercise() -> None:
        await supervisor.start()
        await asyncio.sleep(0.05)
        assert supervisor.running
        assert refresh.repository.projection().status == "reauthorization_required"
        assert refresh.repository.projection().error_code == "lease_validation_failed"
        supervisor.notify()
        await asyncio.sleep(0.05)
        assert supervisor.running
        await supervisor.close()
        assert not supervisor.running

    asyncio.run(exercise())
    matching = [
        record
        for record in session.repository.audit_records()
        if record.event_type == "session.refresh.reauthorization_required"
        and record.reason_code == "lease_validation_failed"
    ]
    assert matching
