from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from ecorex.connectors import (
    AuthChallenge,
    AuthGrant,
    ConnectorAuthKind,
    ConnectorHealth,
    ConnectorHealthResult,
    InMemoryCredentialVault,
)
from ecorex.runtime import RuntimeSettings, create_app


TOKEN = "r" * 43
CSRF = "c" * 43
ORIGIN = "http://testserver"
CALLBACK = "http://127.0.0.1:8765/api/v1/connectors/oauth/callback"


class FeishuAdapter:
    async def begin_auth(
        self,
        *,
        flow_id: str,
        auth_kind: ConnectorAuthKind,
        return_uri: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> AuthChallenge:
        assert return_uri == CALLBACK
        return AuthChallenge(
            flow_id=flow_id,
            connector_id="feishu",
            auth_kind=auth_kind,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            authorization_url=(
                "https://auth.example/authorize"
                f"?state={state}&code_challenge={code_challenge}"
                f"&code_challenge_method={code_challenge_method}"
            ),
        )

    async def complete_auth(self, *, flow_id, response, private_state) -> AuthGrant:
        del flow_id
        assert response["state"] == private_state["state"]
        return AuthGrant(
            account_subject="provider-account",
            account_display_name="办公团队",
            granted_scopes=frozenset(
                {"docx:document:readonly", "docx:document", "drive:drive:readonly", "im:message"}
            ),
            credential_material={"access_token": "SECRET-IN-OS-VAULT"},
        )

    async def check_health(self, credentials) -> ConnectorHealthResult:
        assert credentials["access_token"] == "SECRET-IN-OS-VAULT"
        return ConnectorHealthResult(ConnectorHealth.CONNECTED)

    async def invoke(
        self,
        *,
        action_id: str,
        inputs: Mapping[str, Any],
        credentials: Mapping[str, str],
        idempotency_key: str | None,
    ) -> Any:
        del idempotency_key
        assert credentials["access_token"] == "SECRET-IN-OS-VAULT"
        return {
            "ok": True,
            "action_id": action_id,
            "document_id": str(inputs["document_id"]),
            "revision_id": None,
            "title": "产品方案",
            "content": "办公文档正文",
            "url": "https://docs.example/document/public-id",
            "updated_at": "2026-07-10T15:34:00+08:00",
            "document": None,
        }

    async def revoke(self, *, credentials, idempotency_key) -> bool:
        del credentials, idempotency_key
        return True


class OrderedCloseFeishuAdapter(FeishuAdapter):
    def __init__(self, lifecycle: list[str], gate) -> None:
        self.lifecycle = lifecycle
        self.gate = gate

    async def aclose(self) -> None:
        assert not self.gate.snapshot().healthy
        self.lifecycle.append("adapter_closed")


def _headers(mutation: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if mutation:
        headers.update({"Origin": ORIGIN, "X-EcoreX-CSRF": CSRF})
    return headers


def test_runtime_mounts_dynamic_connectors_security_lifecycle_and_event_bridge(tmp_path) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
            connector_adapters={"feishu": FeishuAdapter()},
            connector_vault=InMemoryCredentialVault(),
            connector_oauth_return_uri=CALLBACK,
            connector_maintenance_seconds=0.01,
        )
    )

    with TestClient(app) as client:
        initial = client.get("/api/v1/bootstrap", headers=_headers()).json()
        feishu = next(item for item in initial["connectors"] if item["connector_id"] == "feishu")
        assert feishu["adapter_available"] is True
        assert feishu["health"] == "disconnected"
        assert client.get("/api/v1/connectors").status_code == 401
        assert client.post(
            "/api/v1/connectors/feishu/auth/begin",
            json={"auth_kind": "oauth2"},
            headers=_headers(),
        ).status_code == 403
        begun = client.post(
            "/api/v1/connectors/feishu/auth/begin",
            json={"auth_kind": "oauth2"},
            headers=_headers(True),
        )
        state = parse_qs(
            urlsplit(begun.json()["authorization_url"]).query
        )["state"][0]
        assert client.get(
            "/api/v1/connectors/oauth/callback/extra",
            params={"state": state, "code": "provider-code"},
        ).status_code == 401
        completed = client.get(
            "/api/v1/connectors/oauth/callback",
            params={"state": state, "code": "provider-code"},
        )
        assert completed.status_code == 200
        instance_id = completed.json()["instance_id"]

        connected = client.get("/api/v1/bootstrap", headers=_headers()).json()
        feishu = next(item for item in connected["connectors"] if item["connector_id"] == "feishu")
        assert feishu["health"] == "connected"
        invoked = client.post(
            f"/api/v1/connectors/instances/{instance_id}/actions/documents.read",
            json={"inputs": {"document_id": "public-doc-id"}},
            headers=_headers(True),
        )
        assert invoked.status_code == 200
        assert invoked.json()["content"] == "办公文档正文"

    with app.state.runtime.database.reader() as connection:
        audit_thread = connection.execute(
            "SELECT thread_id FROM threads WHERE client_request_id='system:connector-audit'"
        ).fetchone()
        assert audit_thread is not None
        event_types = {
            row["event_type"]
            for row in connection.execute(
                "SELECT event_type FROM events WHERE thread_id=?",
                (audit_thread["thread_id"],),
            )
        }
    assert "connector.instance.connected" in event_types
    assert "connector.invocation.completed" in event_types
    assert b"SECRET-IN-OS-VAULT" not in (tmp_path / "runtime.db").read_bytes()


def test_runtime_shutdown_flushes_connector_events_before_gate_and_adapter_close(
    tmp_path,
    monkeypatch,
) -> None:
    lifecycle: list[str] = []
    adapter = OrderedCloseFeishuAdapter(lifecycle, None)
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
            connector_adapters={"feishu": adapter},
            connector_vault=InMemoryCredentialVault(),
            connector_oauth_return_uri=CALLBACK,
            connector_maintenance_seconds=3600,
            lifecycle_shutdown_seconds=2,
        )
    )
    gate = app.state.runtime_execution_gate
    adapter.gate = gate
    service = app.state.connector_composition.service
    original_flush = service.flush_pending_outbox
    original_invariant_stop = app.state.invariant_supervisor.stop

    def recorded_flush(*, timeout_seconds: float, limit: int = 100) -> int:
        published = original_flush(timeout_seconds=timeout_seconds, limit=limit)
        assert service.repository.pending_outbox_count() == 0
        lifecycle.append("flush_done")
        return published

    async def recorded_invariant_stop() -> None:
        assert "flush_done" in lifecycle
        await original_invariant_stop()
        assert not gate.snapshot().healthy
        lifecycle.append("gate_closed")

    monkeypatch.setattr(service, "flush_pending_outbox", recorded_flush)
    monkeypatch.setattr(
        app.state.invariant_supervisor,
        "stop",
        recorded_invariant_stop,
    )

    with TestClient(app) as client:
        begun = client.post(
            "/api/v1/connectors/feishu/auth/begin",
            json={"auth_kind": "oauth2"},
            headers=_headers(True),
        )
        state = parse_qs(
            urlsplit(begun.json()["authorization_url"]).query
        )["state"][0]
        completed = client.get(
            "/api/v1/connectors/oauth/callback",
            params={"state": state, "code": "provider-code"},
        )
        instance_id = completed.json()["instance_id"]

        async def retain_durable_intent(*, wait_seconds: float = 2.0) -> None:
            del wait_seconds

        # The long-interval supervisor has completed its startup pass. Disable
        # only the request nudge so this action is provably left for shutdown.
        monkeypatch.setattr(
            service,
            "publish_pending_best_effort",
            retain_durable_intent,
        )
        invoked = client.post(
            f"/api/v1/connectors/instances/{instance_id}/actions/documents.read",
            json={"inputs": {"document_id": "shutdown-flush-doc"}},
            headers=_headers(True),
        )
        assert invoked.status_code == 200
        assert service.repository.pending_outbox_count() >= 1
        assert service.outbox_delivery_health().status == "degraded"

    assert lifecycle.index("flush_done") < lifecycle.index("gate_closed")
    assert lifecycle.index("gate_closed") < lifecycle.index("adapter_closed")
    assert service.repository.pending_outbox_count() == 0
    with app.state.runtime.database.reader() as connection:
        completion_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type='connector.invocation.completed'"
            ).fetchone()[0]
        )
    assert completion_count == 1
