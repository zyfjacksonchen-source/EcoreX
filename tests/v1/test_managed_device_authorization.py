from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.connectors import InMemoryCredentialVault
from ecorex.runtime.invariant_guard import RuntimeExecutionGate
from ecorex.runtime.invariants import RuntimeInvariantAuditor
from ecorex.session import (
    BrokerDeviceChallenge,
    BrokerDeviceGrant,
    BrokerPollResult,
    BrokerPollStatus,
    DeviceAuthorizationSupervisor,
    DeviceAuthorizationUnavailable,
    DeviceFlowStatus,
    Ed25519SessionLeaseVerifier,
    ManagedDeviceAuthorizationService,
    ManagedSessionLeaseClaims,
    ManagedSessionService,
    SessionLeaseSignature,
    SignedManagedSessionLease,
    token_digest,
    HTTPSDeviceAuthorizationBroker,
    create_device_authorization_router,
)


ACCESS = "device-access-token-secret"
REFRESH = "device-refresh-token-secret"
DEVICE_CODE = "device-code-secret-never-for-webui"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class Broker:
    def __init__(self, clock: Clock, grant: BrokerDeviceGrant) -> None:
        self.clock = clock
        self.grant = grant
        self.begin_keys: list[str] = []
        self.flow_ids: dict[str, str] = {}
        self.poll_keys: list[str] = []
        self.results: list[BrokerPollResult | Exception] = []
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def begin(self, *, idempotency_key: str) -> BrokerDeviceChallenge:
        self.begin_keys.append(idempotency_key)
        await asyncio.sleep(0)
        provider_flow_id = self.flow_ids.setdefault(
            idempotency_key,
            f"provider-flow-{len(self.flow_ids) + 1}",
        )
        return BrokerDeviceChallenge(
            provider_flow_id=provider_flow_id,
            device_code=DEVICE_CODE,
            user_code="ECOR-X123",
            verification_url="https://account.ecorex.test/device",
            expires_at=self.clock() + timedelta(minutes=10),
            poll_interval_seconds=5,
        )

    async def poll(self, **values) -> BrokerPollResult:
        assert values["device_code"] == DEVICE_CODE
        assert values["provider_flow_id"] in self.flow_ids.values()
        self.poll_keys.append(values["idempotency_key"])
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        result = self.results.pop(0) if self.results else BrokerPollResult(BrokerPollStatus.PENDING)
        if isinstance(result, Exception):
            raise result
        return result


def _lease(private: Ed25519PrivateKey, now: datetime) -> SignedManagedSessionLease:
    claims = ManagedSessionLeaseClaims(
        lease_id="lease-device-1",
        account_id="account-device",
        organization_id="organization-device",
        display_name="Device User",
        roles=("member",),
        model_allowlist=("ecorex-chat", "gpt-image-2"),
        quota={"managed_requests": 1000},
        admin_denies=(),
        issued_at=now,
        expires_at=now + timedelta(hours=24),
        revision=1,
        access_token_sha256=token_digest(ACCESS),
        refresh_token_sha256=token_digest(REFRESH),
    )
    return SignedManagedSessionLease(
        claims=claims,
        signature=SessionLeaseSignature(
            algorithm="ed25519",
            key_id="session-device-key",
            value=base64.b64encode(private.sign(claims.canonical_payload())).decode(),
        ),
    )


def _services(tmp_path):
    clock = Clock()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    vault = InMemoryCredentialVault()
    session = ManagedSessionService(
        tmp_path / "runtime.db",
        vault=vault,
        verifier=Ed25519SessionLeaseVerifier({"session-device-key": public}),
        clock=clock,
    )
    grant = BrokerDeviceGrant(_lease(private, clock()), ACCESS, REFRESH)
    broker = Broker(clock, grant)
    device = ManagedDeviceAuthorizationService(
        tmp_path / "runtime.db",
        session=session,
        vault=vault,
        broker=broker,
        clock=clock,
        poll_lease_seconds=30,
    )
    return clock, vault, session, broker, device


def test_device_begin_is_idempotent_and_never_persists_or_projects_device_secret(
    tmp_path,
) -> None:
    _clock, vault, _session, broker, device = _services(tmp_path)

    async def begin_many():
        return await asyncio.gather(
            *(device.begin(client_request_id="stable-device-login") for _ in range(8))
        )

    results = asyncio.run(begin_many())
    assert {result.flow_id for result in results} == {results[0].flow_id}
    assert all(result.status is DeviceFlowStatus.PENDING for result in results)
    assert all(result.user_code == "ECOR-X123" for result in results)
    assert DEVICE_CODE not in repr(results)
    assert len(set(broker.begin_keys)) == 1
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM managed_device_flows").fetchone()[0] == 1
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert DEVICE_CODE.encode() not in (tmp_path / "runtime.db").read_bytes()
    assert len(vault._values) == 1  # test-only vault inspection


def test_device_poll_installs_signed_session_and_recovers_terminal_state(tmp_path) -> None:
    clock, vault, session, broker, device = _services(tmp_path)
    flow = asyncio.run(device.begin(client_request_id="authorize-device-login"))
    broker.results = [
        BrokerPollResult(BrokerPollStatus.PENDING),
        BrokerPollResult(BrokerPollStatus.AUTHORIZED, grant=broker.grant),
    ]

    pending = asyncio.run(device.poll_once(flow.flow_id))
    assert pending.status is DeviceFlowStatus.PENDING
    clock.value += timedelta(seconds=pending.poll_interval_seconds)
    authorized = asyncio.run(device.poll_once(flow.flow_id))

    assert authorized.status is DeviceFlowStatus.AUTHORIZED
    assert authorized.restart_required is True
    assert authorized.session_generation == session.snapshot().generation
    assert session.bearer_token() == ACCESS
    assert not any("device_code" in material for material in vault._values.values())
    wire = (tmp_path / "runtime.db").read_bytes()
    assert ACCESS.encode() not in wire
    assert REFRESH.encode() not in wire
    restarted = ManagedDeviceAuthorizationService(
        tmp_path / "runtime.db",
        session=session,
        vault=vault,
        broker=broker,
        clock=clock,
    )
    assert restarted.get(flow.flow_id) == authorized
    assert len(broker.poll_keys) == 2


def test_poll_lease_fences_concurrent_provider_calls_and_transient_retry(tmp_path) -> None:
    clock, _vault, _session, broker, device = _services(tmp_path)
    flow = asyncio.run(device.begin(client_request_id="concurrent-device-login"))
    broker.entered = asyncio.Event()
    broker.release = asyncio.Event()
    broker.results = [RuntimeError("provider secret detail"), BrokerPollResult(BrokerPollStatus.PENDING)]

    async def race():
        first = asyncio.create_task(device.poll_once(flow.flow_id))
        await broker.entered.wait()
        second = await device.poll_once(flow.flow_id)
        broker.release.set()
        return await first, second

    retried, contender = asyncio.run(race())
    assert contender.status is DeviceFlowStatus.PENDING
    assert len(broker.poll_keys) == 1
    assert retried.status is DeviceFlowStatus.PENDING
    assert retried.error_code == "runtimeerror"
    assert "provider secret" not in repr(retried)
    clock.value = retried.next_poll_at
    after = asyncio.run(device.poll_once(flow.flow_id))
    assert after.status is DeviceFlowStatus.PENDING
    assert len(broker.poll_keys) == 2


def test_device_sqlite_work_never_stalls_event_loop(tmp_path, monkeypatch) -> None:
    _clock, _vault, _session, _broker, device = _services(tmp_path)
    original = device._by_request_hash

    def slow_lookup(request_hash: str):
        time.sleep(0.15)
        return original(request_hash)

    monkeypatch.setattr(device, "_by_request_hash", slow_lookup)

    async def scenario():
        started = asyncio.get_running_loop().time()
        pending = asyncio.create_task(
            device.begin(client_request_id="responsive-device-login")
        )
        await asyncio.sleep(0.02)
        loop_delay = asyncio.get_running_loop().time() - started
        result = await pending
        return loop_delay, result

    loop_delay, result = asyncio.run(scenario())
    assert loop_delay < 0.1
    assert result.status is DeviceFlowStatus.PENDING


def test_device_supervisor_timeout_leaves_recoverable_lease(tmp_path) -> None:
    clock, _vault, _session, broker, device = _services(tmp_path)
    flow = asyncio.run(device.begin(client_request_id="restart-device-login"))

    async def time_out_then_recover():
        broker.entered = asyncio.Event()
        broker.release = asyncio.Event()
        first = DeviceAuthorizationSupervisor(
            device,
            poll_seconds=0.05,
            poll_timeout_seconds=0.05,
        )
        await first.start()
        first.notify()
        await broker.entered.wait()
        await asyncio.sleep(0.08)
        await first.stop()
        assert len(broker.poll_keys) == 1

        # The cancelled call cannot be retried until its durable lease expires.
        broker.release.set()
        clock.value += timedelta(seconds=device.poll_lease_seconds + 1)
        second = DeviceAuthorizationSupervisor(
            device,
            poll_seconds=0.05,
            poll_timeout_seconds=1,
        )
        await second.start()
        second.notify()
        deadline = asyncio.get_running_loop().time() + 1
        while len(broker.poll_keys) < 2:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("restarted device supervisor did not reclaim poll")
            await asyncio.sleep(0.01)
        await second.stop()

    asyncio.run(time_out_then_recover())
    assert device.get(flow.flow_id).status is DeviceFlowStatus.PENDING
    assert len(broker.poll_keys) == 2


def test_expiry_and_supervisor_cleanup_are_durable(tmp_path) -> None:
    clock, vault, _session, broker, device = _services(tmp_path)
    flow = asyncio.run(device.begin(client_request_id="expiring-device-login"))
    clock.value = flow.expires_at
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        before_row = connection.execute(
            "SELECT status,updated_at FROM managed_device_flows WHERE flow_id=?",
            (flow.flow_id,),
        ).fetchone()
        before_audit = connection.execute(
            "SELECT COUNT(*) FROM managed_device_audit"
        ).fetchone()[0]
    assert device.get(flow.flow_id).status is DeviceFlowStatus.PENDING
    assert vault._values
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        assert connection.execute(
            "SELECT status,updated_at FROM managed_device_flows WHERE flow_id=?",
            (flow.flow_id,),
        ).fetchone() == before_row
        assert connection.execute(
            "SELECT COUNT(*) FROM managed_device_audit"
        ).fetchone()[0] == before_audit

    maintenance = {"allowed": False}

    async def converge_expiry() -> None:
        supervisor = DeviceAuthorizationSupervisor(
            device,
            poll_seconds=0.05,
            maintenance_allowed=lambda: maintenance["allowed"],
        )
        await supervisor.start()
        supervisor.notify()
        await asyncio.sleep(0.08)
        assert device.get(flow.flow_id).status is DeviceFlowStatus.PENDING
        assert vault._values
        maintenance["allowed"] = True
        supervisor.notify()
        deadline = asyncio.get_running_loop().time() + 1
        while device.get(flow.flow_id).status is DeviceFlowStatus.PENDING:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("device expiry maintenance did not converge")
            await asyncio.sleep(0.01)
        await supervisor.stop()

    asyncio.run(converge_expiry())
    assert device.get(flow.flow_id).status is DeviceFlowStatus.EXPIRED
    assert not vault._values
    assert broker.poll_keys == []

    # A fresh flow is polled by the supervisor without a WebUI polling loop.
    broker.results = [BrokerPollResult(BrokerPollStatus.AUTHORIZED, grant=broker.grant)]
    clock.value -= timedelta(minutes=5)
    second = asyncio.run(device.begin(client_request_id="supervised-device-login"))

    async def supervise():
        supervisor = DeviceAuthorizationSupervisor(device, poll_seconds=0.05)
        await supervisor.start()
        supervisor.notify()
        deadline = asyncio.get_running_loop().time() + 2
        while device.get(second.flow_id).status is DeviceFlowStatus.PENDING:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("device supervisor did not authorize")
            await asyncio.sleep(0.02)
        await supervisor.stop()
        return device.get(second.flow_id), supervisor.running

    result, running = asyncio.run(supervise())
    assert result.status is DeviceFlowStatus.AUTHORIZED
    assert running is False


def test_device_epoch_close_rejects_late_grant_and_supervisor_new_work(
    tmp_path,
) -> None:
    clock, _vault, _session, broker, device = _services(tmp_path)
    flow = asyncio.run(device.begin(client_request_id="epoch-device-login"))
    gate = RuntimeExecutionGate()
    device.bind_execution_gate(gate)
    gate.record_report(RuntimeInvariantAuditor(device.database).audit())
    assert gate.snapshot().healthy
    broker.results = [
        BrokerPollResult(BrokerPollStatus.AUTHORIZED, grant=broker.grant)
    ]
    broker.entered = asyncio.Event()
    broker.release = asyncio.Event()

    async def close_during_poll():
        polling = asyncio.create_task(device.poll_once(flow.flow_id))
        await broker.entered.wait()
        loop_progressed = asyncio.Event()
        await asyncio.sleep(0)
        loop_progressed.set()
        assert loop_progressed.is_set()
        gate.mark_critical(error_code="device_provider_epoch_closed")
        broker.release.set()
        with pytest.raises(DeviceAuthorizationUnavailable):
            await polling

        before_calls = len(broker.poll_keys)
        supervisor = DeviceAuthorizationSupervisor(
            device,
            poll_seconds=0.05,
            execution_gate=gate,
        )
        await supervisor.start()
        supervisor.notify()
        await asyncio.sleep(0.05)
        await supervisor.stop()
        assert len(broker.poll_keys) == before_calls

    asyncio.run(close_during_poll())
    assert device.get(flow.flow_id).status is DeviceFlowStatus.PENDING
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        state = connection.execute(
            "SELECT generation,active_intent_id FROM managed_session_state "
            "WHERE singleton=1"
        ).fetchone()
    assert state == (0, None)


def test_device_projection_only_reads_without_writes_then_converges(
    tmp_path,
) -> None:
    clock, vault, session, broker, device = _services(tmp_path)
    flow = asyncio.run(device.begin(client_request_id="projection-device-login"))

    def snapshot_rows():
        with sqlite3.connect(tmp_path / "runtime.db") as connection:
            return (
                connection.execute(
                    "SELECT * FROM managed_device_flows ORDER BY flow_id"
                ).fetchall(),
                connection.execute(
                    "SELECT * FROM managed_device_audit ORDER BY sequence"
                ).fetchall(),
            )

    before = snapshot_rows()
    reader = ManagedDeviceAuthorizationService(
        tmp_path / "runtime.db",
        session=session,
        vault=vault,
        broker=broker,
        clock=clock,
        initialize=False,
    )
    assert reader.startup_converged is False
    assert reader.get(flow.flow_id) == flow
    assert reader.due_flow_ids() == (flow.flow_id,)
    with pytest.raises(DeviceAuthorizationUnavailable, match="has not converged"):
        asyncio.run(reader.begin(client_request_id="projection-blocked-write"))
    assert snapshot_rows() == before

    reader.converge_startup()
    assert reader.startup_converged is True
    assert snapshot_rows() == before


def test_device_maintenance_precommit_epoch_close_rolls_back(
    tmp_path, monkeypatch
) -> None:
    clock, vault, _session, _broker, device = _services(tmp_path)
    flow = asyncio.run(device.begin(client_request_id="precommit-device-login"))
    clock.value = flow.expires_at
    gate = RuntimeExecutionGate()
    device.bind_execution_gate(gate)
    gate.record_report(RuntimeInvariantAuditor(device.database).audit())
    original_assert = gate.assert_permit
    closed = False

    def close_before_commit(permit) -> None:
        nonlocal closed
        closed = True
        gate.request_critical(error_code="device_maintenance_precommit")
        original_assert(permit)

    monkeypatch.setattr(gate, "assert_permit", close_before_commit)
    with pytest.raises(DeviceAuthorizationUnavailable):
        device.expire_due()
    assert closed
    assert device.get(flow.flow_id).status is DeviceFlowStatus.PENDING
    assert vault._values


def test_https_device_broker_is_fixed_origin_bounded_and_returns_signed_grant(
    tmp_path,
) -> None:
    clock, _vault, _session, broker_fixture, _device = _services(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "identity.ecorex.test"
        assert request.headers["idempotency-key"].startswith("test-")
        body = json.loads(request.content)
        assert body["client_id"] == "ecorex-desktop"
        if request.url.path.endswith("/authorize"):
            return httpx.Response(
                201,
                json={
                    "schema_version": 1,
                    "provider_flow_id": "provider-flow-https",
                    "device_code": DEVICE_CODE,
                    "user_code": "HTTPS-123",
                    "verification_url": "https://account.ecorex.test/device",
                    "expires_at": (clock() + timedelta(minutes=10)).isoformat(),
                    "poll_interval_seconds": 5,
                },
            )
        assert body["device_code"] == DEVICE_CODE
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "status": "authorized",
                "lease": broker_fixture.grant.lease.to_dict(),
                "access_token": ACCESS,
                "refresh_token": REFRESH,
            },
        )

    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        broker = HTTPSDeviceAuthorizationBroker(
            "https://identity.ecorex.test",
            client_id="ecorex-desktop",
            allowed_hosts=frozenset({"identity.ecorex.test"}),
            client=client,
        )
        challenge = await broker.begin(idempotency_key="test-device-begin")
        result = await broker.poll(
            provider_flow_id=challenge.provider_flow_id,
            device_code=challenge.device_code,
            idempotency_key="test-device-poll",
        )
        await client.aclose()
        return challenge, result

    challenge, result = asyncio.run(exercise())
    assert challenge.user_code == "HTTPS-123"
    assert result.status is BrokerPollStatus.AUTHORIZED
    assert result.grant and result.grant.access_token == ACCESS
    assert len(requests) == 2

    with pytest.raises(ValueError, match="allowlisted HTTPS"):
        HTTPSDeviceAuthorizationBroker(
            "http://identity.ecorex.test",
            client_id="ecorex-desktop",
            allowed_hosts=frozenset({"identity.ecorex.test"}),
        )


def test_device_login_router_never_returns_tokens_and_schedules_same_slot_reload(
    tmp_path,
) -> None:
    _clock, _vault, _session, broker, device = _services(tmp_path)
    broker.results = [BrokerPollResult(BrokerPollStatus.AUTHORIZED, grant=broker.grant)]
    supervisor = DeviceAuthorizationSupervisor(device)
    reloads: list[str] = []
    app = FastAPI()
    app.include_router(
        create_device_authorization_router(
            device,
            supervisor=supervisor,
            authenticated=lambda: False,
            reload_requester=lambda identity: reloads.append(identity) or True,
        ),
        prefix="/api/v1",
    )
    client = TestClient(app)
    started = client.post(
        "/api/v1/session/device",
        json={"client_request_id": "router-device-login"},
    )
    assert started.status_code == 202
    flow_id = started.json()["flow_id"]
    assert ACCESS not in started.text and REFRESH not in started.text and DEVICE_CODE not in started.text
    polled = client.post(
        f"/api/v1/session/device/{flow_id}/poll",
        json={"client_request_id": "router-device-poll"},
    )
    assert polled.status_code == 200
    assert polled.json()["status"] == "authorized"
    assert polled.json()["restart_scheduled"] is True
    assert reloads == [f"session-login:{polled.json()['session_generation']}"]
    assert client.get(f"/api/v1/session/device/{flow_id}").json()["restart_required"] is True

    authenticated_app = FastAPI()
    authenticated_app.include_router(
        create_device_authorization_router(
            device,
            supervisor=supervisor,
            authenticated=lambda: True,
        ),
        prefix="/api/v1",
    )
    assert TestClient(authenticated_app).post(
        "/api/v1/session/device",
        json={"client_request_id": "account-switch-denied"},
    ).status_code == 409
