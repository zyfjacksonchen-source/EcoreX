from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import json
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
import httpx
import pytest

from ecorex.artifacts import ArtifactScope
from ecorex.gateway import GatewayEvent, ManagedModelGatewayClient
from ecorex.runtime import RuntimeSettings, create_app
from ecorex.server import ProductServerSettings, ServerConfigurationError
from ecorex.session import (
    Ed25519SessionLeaseVerifier,
    ManagedSessionLeaseClaims,
    ManagedSessionService,
    SessionLeaseSignature,
    SignedManagedSessionLease,
    token_digest,
)


RUNTIME_TOKEN = "r" * 43
CSRF_TOKEN = "c" * 43
ORIGIN = "http://testserver"
ACCESS_1 = "managed-access-token-revision-one"
REFRESH_1 = "managed-refresh-token-revision-one"
ACCESS_2 = "managed-access-token-revision-two"
REFRESH_2 = "managed-refresh-token-revision-two"


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class Vault:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, str]] = {}

    def put(self, reference: str, material) -> None:
        self.values[reference] = dict(material)

    def get(self, reference: str):
        if reference not in self.values:
            raise KeyError(reference)
        return dict(self.values[reference])

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)


class CompletingGateway:
    def __init__(self) -> None:
        self.requests = []
        self.closed = False
        self.close_count = 0

    async def stream(self, request):
        self.requests.append(request)
        yield GatewayEvent.model_validate(
            {
                "seq": 1,
                "event_type": "response.completed",
                "response_id": f"response_{len(self.requests)}",
            }
        )

    async def aclose(self) -> None:
        self.close_count += 1
        self.closed = True


class LocalArtifactLauncher:
    def __init__(self) -> None:
        self.calls = []

    def validate(self, action, target) -> None:
        assert action.value in {"open", "reveal"}
        assert target.kind in {"file", "uri"}

    def launch(self, action, target) -> None:
        self.calls.append((action.value, target.kind, target.value))


def test_unauthenticated_managed_runtime_closes_gateway_without_worker(
    tmp_path,
) -> None:
    _private, public = _keys()
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    service = _service(tmp_path / "runtime.db", public, MutableClock(now), Vault())
    gateway = CompletingGateway()
    app = create_app(
        settings=_settings(tmp_path / "runtime.db", service, gateway)
    )

    assert app.state.model_worker_supervisor is None
    assert app.state.model_gateway_lifecycle is not None
    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/bootstrap", headers=_headers())
        assert bootstrap.status_code == 200
        assert bootstrap.json()["login"]["authenticated"] is False
        assert gateway.close_count == 0
    assert gateway.close_count == 1
    assert app.state.model_gateway_lifecycle.closed is True


def test_unauthenticated_local_artifact_actions_use_an_exact_security_exception(
    tmp_path,
) -> None:
    _private, public = _keys()
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    service = _service(tmp_path / "runtime.db", public, MutableClock(now), Vault())
    launcher = LocalArtifactLauncher()
    app = create_app(
        settings=_settings(
            tmp_path / "runtime.db",
            service,
            CompletingGateway(),
            artifact_action_launcher=launcher,
        )
    )
    visible = app.state.artifact_service.create_artifact(
        b"local office result",
        requested_name="本地报告.pdf",
        mime_type="application/pdf",
    )
    wrong_account = app.state.artifact_service.create_artifact(
        b"another account",
        requested_name="其他账号.pdf",
        mime_type="application/pdf",
        scope=ArtifactScope(account_id="another-account"),
    )
    internal = app.state.artifact_service.create_artifact(
        b"print('internal')",
        requested_name="worker.py",
        mime_type="text/x-python",
    )
    base = f"/api/v1/artifacts/{visible.artifact_id}/actions"

    with TestClient(app) as client:
        forged = client.post(
            f"{base}/open",
            json={"client_request_id": "local-forged", "path": r"C:\Windows\win.ini"},
            headers=_headers(mutation=True),
        )
        assert forged.status_code == 422
        assert launcher.calls == []

        opened = client.post(
            f"{base}/open",
            json={"client_request_id": "local-open"},
            headers=_headers(mutation=True),
        )
        assert opened.status_code == 200
        assert opened.json()["status"] == "completed"
        assert "path" not in json.dumps(opened.json())
        assert len(launcher.calls) == 1

        assert client.post(
            f"/api/v1/artifacts/{wrong_account.artifact_id}/actions/open",
            json={"client_request_id": "wrong-account"},
            headers=_headers(mutation=True),
        ).status_code == 404
        assert client.post(
            f"/api/v1/artifacts/{internal.artifact_id}/actions/open",
            json={"client_request_id": "internal"},
            headers=_headers(mutation=True),
        ).status_code == 404
        assert len(launcher.calls) == 1

        # The exception is not a prefix wildcard and never weakens the local
        # bearer/origin/CSRF boundary.
        assert client.post(
            f"{base}/download",
            json={"client_request_id": "wrong-action"},
            headers=_headers(mutation=True),
        ).status_code == 401
        assert client.post(
            f"{base}/open/extra",
            json={"client_request_id": "wrong-path"},
            headers=_headers(mutation=True),
        ).status_code == 401
        assert client.post(
            "/api/v1/artifacts/not-a-safe-id/actions/open",
            json={"client_request_id": "unsafe-id"},
            headers=_headers(mutation=True),
        ).status_code == 401
        assert client.post(
            f"/api/v1/artifacts/art_{'0' * 32}/actions/open",
            json={"client_request_id": "retired-uuid-shape"},
            headers=_headers(mutation=True),
        ).status_code == 401
        assert client.post(
            f"/api/v1/artifacts/art_{'0' * 26}/actions/open",
            json={"client_request_id": "unknown-canonical-id"},
            headers=_headers(mutation=True),
        ).status_code == 404
        assert client.post(
            f"{base}/open",
            json={"client_request_id": "no-bearer"},
            headers={"Origin": ORIGIN, "X-EcoreX-CSRF": CSRF_TOKEN},
        ).status_code == 401
        assert client.post(
            f"{base}/open",
            json={"client_request_id": "wrong-origin"},
            headers={
                "Authorization": f"Bearer {RUNTIME_TOKEN}",
                "Origin": "http://attacker.invalid",
                "X-EcoreX-CSRF": CSRF_TOKEN,
            },
        ).status_code == 403
        assert client.post(
            f"{base}/open",
            json={"client_request_id": "wrong-csrf"},
            headers={
                "Authorization": f"Bearer {RUNTIME_TOKEN}",
                "Origin": ORIGIN,
                "X-EcoreX-CSRF": "invalid",
            },
        ).status_code == 403
        assert client.post(
            f"/api/v1/artifacts/{visible.artifact_id.lower()}/actions/open",
            json={"client_request_id": "non-canonical-id"},
            headers=_headers(mutation=True),
        ).status_code == 401
        assert len(launcher.calls) == 1


class NeverRetouchAdapter:
    def __init__(self) -> None:
        self.close_count = 0

    async def edit(self, _request):  # pragma: no cover - assertion path
        raise AssertionError("unsigned image model must never execute retouch")

    async def recover(self, _idempotency_key):  # pragma: no cover - assertion path
        raise AssertionError("unsigned image model must never recover retouch")

    async def aclose(self) -> None:
        self.close_count += 1


def test_unauthenticated_managed_runtime_closes_unstarted_retouch_adapter(
    tmp_path,
) -> None:
    _private, public = _keys()
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    service = _service(tmp_path / "runtime.db", public, MutableClock(now), Vault())
    adapter = NeverRetouchAdapter()
    app = create_app(
        settings=_settings(
            tmp_path / "runtime.db",
            service,
            CompletingGateway(),
            installed_capability_packs=frozenset({"image"}),
            retouch_adapter=adapter,
        )
    )

    assert app.state.retouch_worker_supervisor is not None
    with TestClient(app) as client:
        assert client.get("/api/v1/bootstrap", headers=_headers()).status_code == 200
        assert app.state.retouch_worker_supervisor.running is False
        assert adapter.close_count == 0
    assert adapter.close_count == 1


def _keys():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, public


def _lease(
    private: Ed25519PrivateKey,
    *,
    now: datetime,
    revision: int = 1,
    access_token: str = ACCESS_1,
    refresh_token: str = REFRESH_1,
    models: tuple[str, ...] = ("ecorex-chat",),
    account_id: str = "account-runtime-1",
    organization_id: str = "organization-runtime-1",
    display_name: str = "张三",
    admin_denies: tuple[str, ...] = ("shell",),
) -> SignedManagedSessionLease:
    claims = ManagedSessionLeaseClaims(
        lease_id=f"managed-lease-{revision}",
        account_id=account_id,
        organization_id=organization_id,
        display_name=display_name,
        roles=("member", "workspace_admin"),
        model_allowlist=models,
        quota={"managed_requests": 99, "image_jobs": 12},
        admin_denies=admin_denies,
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=2),
        revision=revision,
        access_token_sha256=token_digest(access_token),
        refresh_token_sha256=token_digest(refresh_token),
    )
    signature = private.sign(claims.canonical_payload())
    return SignedManagedSessionLease(
        claims=claims,
        signature=SessionLeaseSignature(
            algorithm="ed25519",
            key_id="managed-session-key",
            value=base64.b64encode(signature).decode("ascii"),
        ),
    )


def _service(database, public: bytes, clock: MutableClock, vault: Vault):
    return ManagedSessionService(
        database,
        vault=vault,
        verifier=Ed25519SessionLeaseVerifier(
            {"managed-session-key": public}
        ),
        clock=clock,
    )


def _settings(database, service, gateway, **updates) -> RuntimeSettings:
    values = {
        "database_path": database,
        "runtime_bearer_token": RUNTIME_TOKEN,
        "csrf_token": CSRF_TOKEN,
        "webui_origins": (ORIGIN,),
        "managed_session_service": service,
        "require_managed_session": True,
        "model_gateway": gateway,
        "model_worker_concurrency": 1,
        "model_worker_poll_seconds": 0.01,
        "model_worker_shutdown_seconds": 1,
    }
    values.update(updates)
    return RuntimeSettings(**values)


def _headers(*, mutation: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {RUNTIME_TOKEN}"}
    if mutation:
        headers.update({"Origin": ORIGIN, "X-EcoreX-CSRF": CSRF_TOKEN})
    return headers


def _install(service, lease, access=ACCESS_1, refresh=REFRESH_1, request="login-001"):
    return service.install(
        lease,
        access_token=access,
        refresh_token=refresh,
        client_request_id=request,
    )


def test_bootstrap_uses_signed_identity_policy_quota_and_model_allowlist(tmp_path) -> None:
    private, public = _keys()
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = Vault()
    database = tmp_path / "runtime.db"
    service = _service(database, public, clock, vault)
    lease = _lease(private, now=now)
    _install(service, lease)
    gateway = CompletingGateway()
    app = create_app(
        settings=_settings(
            database,
            service,
            gateway,
            retouch_adapter=NeverRetouchAdapter(),
            installed_capability_packs=frozenset({"image"}),
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/bootstrap", headers=_headers())
        assert response.status_code == 200
        body = response.json()
        assert body["login"] == {
            "authenticated": True,
            "account_id": "account-runtime-1",
            "display_name": "张三",
            "organization_id": "organization-runtime-1",
            "roles": ["member", "workspace_admin"],
            "session_revision": 1,
            "session_lease_digest": service.snapshot().lease_digest,
        }
        assert body["policy_lease"]["lease_id"] == lease.claims.lease_id
        assert body["policy_lease"]["issued_at"] == lease.claims.issued_at.isoformat().replace(
            "+00:00", "Z"
        )
        assert body["policy_lease"]["expires_at"] == lease.claims.expires_at.isoformat().replace(
            "+00:00", "Z"
        )
        assert body["quota"]["limits"] == {
            "image_jobs": 12,
            "managed_requests": 99,
        }
        assert body["quota"]["remaining"] == 99
        assert [item["model_id"] for item in body["models"]["chat"]] == [
            "ecorex-chat"
        ]
        assert body["models"]["image"] == []
        assert body["model_service"] == {"state": "ready", "reason": None}
        assert body["retouch_service"] == {
            "state": "unavailable",
            "reason": "signed_image_model_not_allowed",
        }
        assert body["permissions"]["admin_hard_denies"] == ["shell"]
        created = client.post(
            "/api/v1/threads",
            json={"client_request_id": "managed-thread-1"},
            headers=_headers(mutation=True),
        )
        assert created.status_code == 201


def test_expiry_and_token_tamper_block_mutations_but_keep_history_and_artifacts(
    tmp_path,
) -> None:
    private, public = _keys()
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = Vault()
    database = tmp_path / "runtime.db"
    service = _service(database, public, clock, vault)
    lease = _lease(private, now=now)
    _install(service, lease)
    launcher = LocalArtifactLauncher()
    app = create_app(
        settings=_settings(
            database,
            service,
            CompletingGateway(),
            artifact_action_launcher=launcher,
        )
    )

    with TestClient(app) as client:
        thread = client.post(
            "/api/v1/threads",
            json={"client_request_id": "history-before-expiry"},
            headers=_headers(mutation=True),
        ).json()
        artifact = app.state.artifact_service.create_artifact(
            b"offline history remains readable",
            requested_name="history.txt",
            mime_type="text/plain",
            scope=ArtifactScope(
                account_id="account-runtime-1",
                thread_id=thread["thread_id"],
            ),
        )

        clock.value = lease.claims.expires_at
        bootstrap = client.get("/api/v1/bootstrap", headers=_headers())
        assert bootstrap.status_code == 200
        assert bootstrap.json()["login"]["authenticated"] is False
        assert bootstrap.json()["model_service"] == {
            "state": "unavailable",
            "reason": "managed_session_unavailable",
        }
        assert not any(
            bootstrap.json()["models"][modality]
            for modality in ("chat", "image", "vision", "audio", "embedding")
        )
        assert client.get("/api/v1/threads", headers=_headers()).status_code == 200
        assert client.get(
            f"/api/v1/threads/{thread['thread_id']}/projection",
            headers=_headers(),
        ).status_code == 200
        assert client.get(
            f"/api/v1/artifacts/{artifact.artifact_id}", headers=_headers()
        ).status_code == 200
        local_action = client.post(
            f"/api/v1/artifacts/{artifact.artifact_id}/actions/open",
            json={"client_request_id": "expired-local-open"},
            headers=_headers(mutation=True),
        )
        assert local_action.status_code == 200
        assert len(launcher.calls) == 1
        assert client.post(
            "/api/v1/threads",
            json={"client_request_id": "expired-write"},
            headers=_headers(mutation=True),
        ).status_code == 401

        # A token commitment failure has the same fail-closed product shape.
        clock.value = now
        active_ref = next(iter(vault.values))
        vault.values[active_ref]["access_token"] = "tampered-token-material"
        assert client.get("/api/v1/threads", headers=_headers()).status_code == 200
        assert client.post(
            "/api/v1/threads",
            json={"client_request_id": "tampered-token-write"},
            headers=_headers(mutation=True),
        ).status_code == 401

    # Restarting while the lease is already expired still derives the signed
    # read-only account partition, never a synthetic local-user identity.
    vault.values[active_ref]["access_token"] = ACCESS_1
    clock.value = lease.claims.expires_at
    restarted_service = _service(database, public, clock, vault)
    restarted = create_app(
        settings=_settings(database, restarted_service, CompletingGateway())
    )
    restarted_client = TestClient(restarted)
    assert restarted_client.get(
        f"/api/v1/artifacts/{artifact.artifact_id}", headers=_headers()
    ).status_code == 200
    assert restarted_client.get(
        "/api/v1/bootstrap", headers=_headers()
    ).json()["login"]["authenticated"] is False
    assert restarted_client.post(
        "/api/v1/threads",
        json={"client_request_id": "expired-after-restart"},
        headers=_headers(mutation=True),
    ).status_code == 401


def test_unknown_signed_model_allowlist_never_pretends_service_is_ready(tmp_path) -> None:
    private, public = _keys()
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = Vault()
    database = tmp_path / "runtime.db"
    service = _service(database, public, clock, vault)
    _install(service, _lease(private, now=now, models=("unknown-cloud-model",)))
    app = create_app(settings=_settings(database, service, CompletingGateway()))
    client = TestClient(app)

    bootstrap = client.get("/api/v1/bootstrap", headers=_headers()).json()
    assert bootstrap["model_service"] == {
        "state": "unavailable",
        "reason": "signed_model_allowlist_empty",
    }
    assert bootstrap["models"]["chat"] == []
    assert bootstrap["models"]["image"] == []
    assert app.state.model_worker_supervisor is None
    thread = client.post(
        "/api/v1/threads",
        json={"client_request_id": "catalog-empty-thread"},
        headers=_headers(mutation=True),
    ).json()
    turn = client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        json={"input": "should not queue", "client_message_id": "empty-model-1"},
        headers=_headers(mutation=True),
    )
    assert turn.status_code == 503


def test_account_switch_is_fenced_until_controlled_restart(tmp_path) -> None:
    private, public = _keys()
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = Vault()
    database = tmp_path / "runtime.db"
    bound_service = _service(database, public, clock, vault)
    _install(bound_service, _lease(private, now=now))
    app = create_app(
        settings=_settings(database, bound_service, CompletingGateway())
    )
    client = TestClient(app)

    assert client.post(
        "/api/v1/threads",
        json={"client_request_id": "before-account-switch"},
        headers=_headers(mutation=True),
    ).status_code == 201

    other_process = _service(database, public, clock, vault)
    switched = _lease(
        private,
        now=now,
        revision=2,
        access_token=ACCESS_2,
        refresh_token=REFRESH_2,
        account_id="account-runtime-2",
        organization_id="organization-runtime-2",
        display_name="李四",
    )
    _install(
        other_process,
        switched,
        access=ACCESS_2,
        refresh=REFRESH_2,
        request="other-process-account-switch",
    )

    mutation = client.post(
        "/api/v1/threads",
        json={"client_request_id": "after-account-switch"},
        headers=_headers(mutation=True),
    )
    assert mutation.status_code == 409
    assert mutation.json()["code"] == "managed_session_restart_required"
    bootstrap = client.get("/api/v1/bootstrap", headers=_headers())
    assert bootstrap.status_code == 409
    assert bootstrap.json()["detail"]["code"] == "managed_session_restart_required"
    assert client.get("/api/v1/threads", headers=_headers()).status_code == 200


def test_managed_gateway_reads_bearer_directly_from_session_vault(tmp_path) -> None:
    private, public = _keys()
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = Vault()
    database = tmp_path / "runtime.db"
    service = _service(database, public, clock, vault)
    _install(service, _lease(private, now=now))
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/x-ndjson"},
            content=(
                json.dumps(
                    {
                        "seq": 1,
                        "event_type": "response.completed",
                        "response_id": "managed-response-1",
                    }
                ).encode()
                + b"\n"
            ),
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = ManagedModelGatewayClient(
        "https://models.ecorex.test/v1/responses",
        credentials=service,
        allowed_hosts=frozenset({"models.ecorex.test"}),
        client=http,
    )
    app = create_app(
        settings=_settings(
            database,
            service,
            gateway,
            close_model_gateway_on_shutdown=False,
        )
    )

    with TestClient(app) as client:
        thread = client.post(
            "/api/v1/threads",
            json={"client_request_id": "gateway-auth-thread"},
            headers=_headers(mutation=True),
        ).json()
        turn = client.post(
            f"/api/v1/threads/{thread['thread_id']}/turns",
            json={"input": "gateway auth", "client_message_id": "gateway-auth-1"},
            headers=_headers(mutation=True),
        )
        assert turn.status_code == 202
        deadline = time.monotonic() + 3
        while not captured and time.monotonic() < deadline:
            time.sleep(0.01)
        assert captured
        assert captured[0].headers["authorization"] == f"Bearer {ACCESS_1}"
    asyncio.run(http.aclose())


def test_logout_is_csrf_bound_cleans_vault_stops_tasks_and_schedules_reload(tmp_path) -> None:
    private, public = _keys()
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = Vault()
    database = tmp_path / "runtime.db"
    service = _service(database, public, clock, vault)
    snapshot = _install(service, _lease(private, now=now))
    gateway = CompletingGateway()
    settings = _settings(database, service, gateway)
    reloads: list[str] = []
    settings.session_reload_requester = lambda identity: reloads.append(identity) or True
    app = create_app(settings=settings)

    with TestClient(app) as client:
        thread = client.post(
            "/api/v1/threads",
            json={"client_request_id": "logout-history-thread"},
            headers=_headers(mutation=True),
        ).json()
        without_csrf = client.post(
            "/api/v1/session/logout",
            json={
                "lease_digest": snapshot.lease_digest,
                "client_request_id": "stable-logout-request",
                "confirmed": True,
            },
            headers=_headers(),
        )
        assert without_csrf.status_code == 403
        response = client.post(
            "/api/v1/session/logout",
            json={
                "lease_digest": snapshot.lease_digest,
                "client_request_id": "stable-logout-request",
                "confirmed": True,
            },
            headers=_headers(mutation=True),
        )
        assert response.status_code == 200
        assert response.json()["restart_required"] is True
        assert response.json()["restart_scheduled"] is True
        assert reloads == [f"session-logout:{response.json()['generation']}"]
        assert not vault.values
        assert app.state.model_worker_supervisor.running is False
        assert gateway.closed is True
        assert app.state.connector_composition.maintenance._task is None
        assert app.state.system_observability_supervisor._task is None
        assert app.state.logout_shutdown_failures == ()
        assert client.get(
            f"/api/v1/threads/{thread['thread_id']}/projection",
            headers=_headers(),
        ).status_code == 200
        assert client.post(
            "/api/v1/threads",
            json={"client_request_id": "write-after-logout"},
            headers=_headers(mutation=True),
        ).status_code == 401
        assert client.get(
            "/api/v1/bootstrap", headers=_headers()
        ).json()["login"]["authenticated"] is False


def test_production_gateway_requires_session_and_explicit_test_override(tmp_path) -> None:
    gateway = CompletingGateway()
    with pytest.raises(ValueError, match="requires a managed signed session"):
        create_app(
            settings=RuntimeSettings(
                database_path=tmp_path / "runtime.db",
                model_gateway=gateway,
            )
        )

    _private, public = _keys()
    with pytest.raises(ServerConfigurationError, match="managed signed session"):
        ProductServerSettings(
            database_path=tmp_path / "product.db",
            web_root=tmp_path,
            release_manifest_path=tmp_path / "release.json",
            web_manifest_path=tmp_path / "web.json",
            trusted_public_keys={"release-key": public},
            model_gateway=gateway,
        )

    explicit = RuntimeSettings(
        database_path=tmp_path / "dev.db",
        model_gateway=gateway,
        allow_unmanaged_model_gateway_for_testing=True,
    )
    assert create_app(settings=explicit).state.model_worker_supervisor is not None
