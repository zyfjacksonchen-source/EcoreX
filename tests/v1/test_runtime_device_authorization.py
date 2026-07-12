from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from ecorex.connectors import InMemoryCredentialVault
from ecorex.protocol import CreateThreadRequest
from ecorex.server import ProductServerSettings, create_product_app
from ecorex.session import (
    BrokerDeviceChallenge,
    BrokerDeviceGrant,
    BrokerPollResult,
    BrokerPollStatus,
    Ed25519SessionLeaseVerifier,
    ManagedDeviceAuthorizationService,
    ManagedSessionLeaseClaims,
    ManagedSessionService,
    SessionLeaseSignature,
    SignedManagedSessionLease,
    token_digest,
)


ORIGIN = "http://127.0.0.1:8765"
RUNTIME_BEARER = "runtime-device-bearer-" + "r" * 32
CSRF_TOKEN = "runtime-device-csrf-" + "c" * 32
DEVICE_CODE = "device-code-must-never-reach-webui-or-sqlite"
ACCESS_TOKEN = "managed-access-token-must-stay-in-vault"
REFRESH_TOKEN = "managed-refresh-token-must-stay-in-vault"


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class AuthorizingBroker:
    def __init__(self, clock: FixedClock, grant: BrokerDeviceGrant) -> None:
        self.clock = clock
        self.grant = grant
        self.begin_calls = 0
        self.poll_calls = 0

    async def begin(self, *, idempotency_key: str) -> BrokerDeviceChallenge:
        assert idempotency_key.startswith("device-begin:")
        self.begin_calls += 1
        return BrokerDeviceChallenge(
            provider_flow_id="product-provider-flow-1",
            device_code=DEVICE_CODE,
            user_code="ECOR-1000",
            verification_url="https://account.ecorex.test/device",
            expires_at=self.clock() + timedelta(minutes=10),
            poll_interval_seconds=5,
        )

    async def poll(
        self,
        *,
        provider_flow_id: str,
        device_code: str,
        idempotency_key: str,
    ) -> BrokerPollResult:
        assert provider_flow_id == "product-provider-flow-1"
        assert device_code == DEVICE_CODE
        assert idempotency_key.startswith("device-poll:devflow_")
        self.poll_calls += 1
        return BrokerPollResult(BrokerPollStatus.AUTHORIZED, grant=self.grant)


def _signed_lease(
    private_key: Ed25519PrivateKey,
    now: datetime,
) -> SignedManagedSessionLease:
    claims = ManagedSessionLeaseClaims(
        lease_id="product-device-lease-1",
        account_id="product-device-account",
        organization_id="product-device-organization",
        display_name="设备授权用户",
        roles=("member",),
        model_allowlist=("ecorex-chat", "gpt-image-2"),
        quota={"managed_requests": 80},
        admin_denies=(),
        issued_at=now,
        expires_at=now + timedelta(hours=24),
        revision=1,
        access_token_sha256=token_digest(ACCESS_TOKEN),
        refresh_token_sha256=token_digest(REFRESH_TOKEN),
    )
    return SignedManagedSessionLease(
        claims=claims,
        signature=SessionLeaseSignature(
            algorithm="ed25519",
            key_id="product-device-session-key",
            value=base64.b64encode(
                private_key.sign(claims.canonical_payload())
            ).decode("ascii"),
        ),
    )


def _authorization_services(tmp_path):
    clock = FixedClock()
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    vault = InMemoryCredentialVault()
    database = tmp_path / "runtime.db"
    session = ManagedSessionService(
        database,
        vault=vault,
        verifier=Ed25519SessionLeaseVerifier(
            {"product-device-session-key": public_key}
        ),
        clock=clock,
    )
    grant = BrokerDeviceGrant(
        _signed_lease(private_key, clock()),
        ACCESS_TOKEN,
        REFRESH_TOKEN,
    )
    broker = AuthorizingBroker(clock, grant)
    device = ManagedDeviceAuthorizationService(
        database,
        session=session,
        vault=vault,
        broker=broker,
        clock=clock,
    )
    return database, vault, session, broker, device


def _create_product_runtime(
    tmp_path,
    *,
    session: ManagedSessionService,
    device: ManagedDeviceAuthorizationService,
    reloads: list[str],
    registration_recorder=None,
    runtime_ready_recorder=None,
):
    generated_secrets = iter((RUNTIME_BEARER, CSRF_TOKEN))
    settings = ProductServerSettings(
        database_path=tmp_path / "runtime.db",
        web_root=tmp_path,
        release_manifest_path=tmp_path / "release-manifest.json",
        web_manifest_path=tmp_path / "web-manifest.json",
        trusted_public_keys={"release-test-key": b"k" * 32},
        managed_session_service=session,
        device_authorization_service=device,
        session_reload_requester=lambda identity: reloads.append(identity) or True,
        first_install_registration_recorder=registration_recorder,
        first_install_runtime_ready_recorder=runtime_ready_recorder,
        secret_factory=lambda _size: next(generated_secrets),
        workspace_roots=(tmp_path,),
    )
    verified_bundle = SimpleNamespace(
        release_manifest=SimpleNamespace(
            version="1.0.0",
            build_digest="d" * 64,
        ),
    )
    # Cryptographic bundle verification has its own product tests.  Patching
    # only that loader keeps this test focused on Product -> Runtime wiring.
    with patch(
        "ecorex.server.app.load_verified_web_bundle",
        return_value=verified_bundle,
    ):
        return create_product_app(settings)


def _headers(
    *,
    bearer: str = RUNTIME_BEARER,
    origin: str = ORIGIN,
    csrf: str = CSRF_TOKEN,
    mutation: bool = False,
) -> dict[str, str]:
    result = {"Authorization": f"Bearer {bearer}"}
    if mutation:
        result.update({"Origin": origin, "X-EcoreX-CSRF": csrf})
    return result


def _assert_no_credentials(value: str) -> None:
    assert DEVICE_CODE not in value
    assert ACCESS_TOKEN not in value
    assert REFRESH_TOKEN not in value
    assert "device_code" not in value.casefold()
    assert "access_token" not in value.casefold()
    assert "refresh_token" not in value.casefold()


def test_product_runtime_device_authorization_is_secure_and_requires_reload(
    tmp_path,
) -> None:
    database, vault, session, broker, device = _authorization_services(tmp_path)
    reloads: list[str] = []
    app = _create_product_runtime(
        tmp_path,
        session=session,
        device=device,
        reloads=reloads,
    )
    client = TestClient(app, base_url=ORIGIN)

    bootstrap_response = client.get("/api/v1/bootstrap", headers=_headers())
    assert bootstrap_response.status_code == 200
    bootstrap = bootstrap_response.json()
    assert bootstrap["login"] == {
        "authenticated": False,
        "account_id": None,
        "display_name": None,
        "organization_id": None,
        "roles": [],
        "session_revision": None,
    }
    assert bootstrap["policy_lease"] is None
    assert bootstrap["login_service"] == {"state": "ready", "reason": None}
    assert bootstrap["model_service"] == {
        "state": "unavailable",
        "reason": "managed_session_unavailable",
    }
    assert not any(
        bootstrap["models"][modality]
        for modality in ("chat", "image", "vision", "audio", "embedding")
    )

    # Seed only a local history container so the denied request is specifically
    # a model-producing mutation rather than thread creation.
    thread = app.state.runtime.create_thread(
        CreateThreadRequest(client_request_id="device-auth-seeded-thread")
    )
    denied_turn = client.post(
        f"/api/v1/threads/{thread.thread_id}/turns",
        json={
            "input": "must not reach the model before managed login",
            "client_message_id": "device-auth-model-message",
        },
        headers=_headers(mutation=True),
    )
    assert denied_turn.status_code == 401
    assert denied_turn.json()["detail"] == "managed account authentication is required"

    start_payload = {"client_request_id": "product-device-login-0001"}
    wrong_bearer = client.post(
        "/api/v1/session/device",
        json=start_payload,
        headers=_headers(bearer="wrong-" + "x" * 40, mutation=True),
    )
    wrong_origin = client.post(
        "/api/v1/session/device",
        json=start_payload,
        headers=_headers(origin="http://localhost:8765", mutation=True),
    )
    wrong_csrf = client.post(
        "/api/v1/session/device",
        json=start_payload,
        headers=_headers(csrf="wrong-" + "x" * 40, mutation=True),
    )
    assert [wrong_bearer.status_code, wrong_origin.status_code, wrong_csrf.status_code] == [
        401,
        403,
        403,
    ]
    assert broker.begin_calls == 0

    started = client.post(
        "/api/v1/session/device",
        json=start_payload,
        headers=_headers(mutation=True),
    )
    assert started.status_code == 202
    assert started.json()["status"] == "pending"
    assert started.json()["user_code"] == "ECOR-1000"
    flow_id = started.json()["flow_id"]
    assert broker.begin_calls == 1
    _assert_no_credentials(started.text)

    projected = client.get(
        f"/api/v1/session/device/{flow_id}",
        headers=_headers(),
    )
    assert projected.status_code == 200
    assert projected.json()["status"] == "pending"
    _assert_no_credentials(projected.text)

    poll_payload = {"client_request_id": "product-device-poll-0001"}
    wrong_poll_csrf = client.post(
        f"/api/v1/session/device/{flow_id}/poll",
        json=poll_payload,
        headers=_headers(csrf="wrong-" + "p" * 40, mutation=True),
    )
    assert wrong_poll_csrf.status_code == 403
    assert broker.poll_calls == 0

    authorized = client.post(
        f"/api/v1/session/device/{flow_id}/poll",
        json=poll_payload,
        headers=_headers(mutation=True),
    )
    assert authorized.status_code == 200
    authorized_body = authorized.json()
    assert authorized_body["status"] == "authorized"
    assert authorized_body["restart_required"] is True
    assert authorized_body["restart_scheduled"] is True
    assert authorized_body["session_generation"] == session.snapshot().generation
    assert reloads == [
        f"session-login:{authorized_body['session_generation']}"
    ]
    assert broker.poll_calls == 1
    _assert_no_credentials(authorized.text)

    # The running process remains fenced to its startup identity until the
    # supervisor performs the requested same-slot reload.
    pre_reload = client.get("/api/v1/bootstrap", headers=_headers())
    assert pre_reload.status_code == 409
    assert pre_reload.json()["detail"]["code"] == "managed_session_restart_required"

    # Credential material lives only in the vault.  Neither the durable device
    # flow nor the signed session projection stores plaintext provider secrets.
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for persisted in (database, database.with_name(database.name + "-wal")):
        if persisted.exists():
            payload = persisted.read_bytes()
            assert DEVICE_CODE.encode() not in payload
            assert ACCESS_TOKEN.encode() not in payload
            assert REFRESH_TOKEN.encode() not in payload

    runtime_ready: list[dict[str, object]] = []
    # A controlled reload creates a fresh process-local execution authority.
    # Reconstruct the Device service instead of carrying the old gate into the
    # new Runtime process while reusing only its durable flow/session state.
    restarted_device = ManagedDeviceAuthorizationService(
        database,
        session=session,
        vault=vault,
        broker=broker,
        clock=device.clock,
        initialize=False,
    )
    restarted_app = _create_product_runtime(
        tmp_path,
        session=session,
        device=restarted_device,
        reloads=reloads,
        runtime_ready_recorder=lambda value: runtime_ready.append(dict(value)) or True,
    )
    with TestClient(restarted_app, base_url=ORIGIN) as restarted:
        restarted_bootstrap = restarted.get(
            "/api/v1/bootstrap",
            headers=_headers(),
        )
        assert restarted_bootstrap.status_code == 200
        assert restarted_bootstrap.json()["login"] == {
            "authenticated": True,
            "account_id": "product-device-account",
            "display_name": "设备授权用户",
            "organization_id": "product-device-organization",
            "roles": ["member"],
            "session_revision": 1,
        }
        assert restarted_bootstrap.json()["policy_lease"]["lease_id"] == (
            "product-device-lease-1"
        )

        already_authenticated = restarted.post(
            "/api/v1/session/device",
            json={"client_request_id": "product-device-switch-denied"},
            headers=_headers(mutation=True),
        )
        assert already_authenticated.status_code == 409
        assert already_authenticated.json()["detail"]["code"] == (
            "session_already_authenticated"
        )
    assert runtime_ready == [
        {
            "account_id": "product-device-account",
            "organization_id": "product-device-organization",
            "lease_id": "product-device-lease-1",
            "lease_digest": session.snapshot().lease_digest,
            "session_generation": session.snapshot().generation,
            "lease_revision": 1,
        }
    ]
    assert broker.begin_calls == 1
