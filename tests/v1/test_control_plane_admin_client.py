from __future__ import annotations

import json

import httpx
import pytest

from ecorex.control_plane import (
    AdminControlPlaneClient,
    ControlPlaneAuthenticationError,
    ControlPlaneClientError,
    ControlPlaneRequestError,
)


TOKEN = "admin-token-" + "x" * 32


class Credential:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token

    def bearer_token(self) -> str:
        return self.token


def candidate() -> dict:
    return {
        "release_id": "release-1.0.0",
        "version": "1.0.0",
        "build_digest": "a" * 64,
        "channel": "stable",
        "status": "candidate",
        "gates": {},
        "missing_gates": ["lint"],
    }


def client(handler, *, credential: Credential | None = None):
    transport = httpx.MockTransport(handler)
    injected = httpx.Client(transport=transport, follow_redirects=True)
    return AdminControlPlaneClient(
        "https://control.ecorex.test",
        credentials=credential or Credential(),
        allowed_hosts=frozenset({"control.ecorex.test"}),
        client=injected,
    )


def test_admin_client_uses_bearer_strict_contract_and_refuses_redirect() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["accept-encoding"] == "identity"
        assert request.url.path == "/api/v1/admin/releases"
        payload = json.loads(request.content)
        assert payload["client_request_id"] == "candidate-one"
        return httpx.Response(201, json=candidate())

    control = client(handler)
    result = control.create_candidate(
        {"release_id": "release-1.0.0"},
        client_request_id="candidate-one",
    )
    assert result.release_id == "release-1.0.0"
    assert len(requests) == 1

    redirect = client(
        lambda _request: httpx.Response(
            307,
            headers={"Location": "https://evil.test/steal"},
            json={"detail": "redirect"},
        )
    )
    with pytest.raises(ControlPlaneClientError, match="redirect"):
        redirect.distribution()


def test_admin_client_fails_closed_on_auth_error_and_invalid_response() -> None:
    invalid = client(lambda _request: httpx.Response(200, text="not json"))
    with pytest.raises(ControlPlaneClientError, match="response type"):
        invalid.distribution()

    rejected = client(
        lambda _request: httpx.Response(
            409,
            json={"detail": {"code": "release_conflict", "message": "conflict"}},
        )
    )
    with pytest.raises(ControlPlaneRequestError) as captured:
        rejected.publish("release-1.0.0", client_request_id="publish-one")
    assert captured.value.status_code == 409
    assert captured.value.code == "release_conflict"

    weak = client(
        lambda _request: httpx.Response(200, json={}), credential=Credential("short")
    )
    with pytest.raises(ControlPlaneAuthenticationError, match="invalid"):
        weak.distribution()


def test_admin_client_rejects_unallowlisted_or_credentialed_endpoint() -> None:
    with pytest.raises(ValueError, match="allowlisted"):
        AdminControlPlaneClient(
            "https://control.ecorex.test",
            credentials=Credential(),
            allowed_hosts=frozenset({"other.test"}),
        )
    with pytest.raises(ValueError, match="credential-free"):
        AdminControlPlaneClient(
            "https://user:secret@control.ecorex.test",
            credentials=Credential(),
            allowed_hosts=frozenset({"control.ecorex.test"}),
        )


def test_admin_client_creates_and_activates_signed_rollback_workflow() -> None:
    observed: list[tuple[str, str, dict]] = []

    def projection(status: str) -> dict:
        return {
            "rollback_id": "rollback_123",
            "source_release_id": "release-1.0.2-stable",
            "target_release_id": "release-1.0.1-stable",
            "channel": "stable",
            "status": status,
            "percentage": 10,
            "target_organization_ids": ["org-1"],
            "target_account_ids": [],
            "authorization_ttl_seconds": 300,
            "created_at": "2026-07-12T08:00:00+00:00",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        observed.append((request.method, request.url.path, body))
        return httpx.Response(
            201 if request.url.path.endswith("/rollbacks") else 200,
            json=projection(
                "draft" if request.url.path.endswith("/rollbacks") else "active"
            ),
        )

    control = client(handler)
    created = control.create_rollback(
        "release-1.0.2-stable",
        "release-1.0.1-stable",
        percentage=10,
        organizations=["org-1"],
        accounts=[],
        authorization_ttl_seconds=300,
        client_request_id="rollback-create",
    )
    active = control.rollback_action(
        created.rollback_id,
        "activate",
        client_request_id="rollback-activate",
    )

    assert active.status == "active"
    assert observed[0][1] == "/api/v1/admin/rollbacks"
    assert observed[0][2]["authorization_ttl_seconds"] == 300
    assert observed[1][1] == "/api/v1/admin/rollbacks/rollback_123/activate"


def test_admin_client_reads_and_triggers_bootstrap_freshness() -> None:
    observed: list[str] = []

    def payload(*, run_state: str | None = None) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": 1,
            "status": "healthy",
            "active_expires_at": "2026-07-13T00:00:00Z",
            "active_authority_sha256": "f" * 64,
            "remaining_seconds": 36000,
            "last_checked_at": "2026-07-12T14:00:00+00:00",
            "next_check_at": "2026-07-12T15:00:00+00:00",
            "last_attempt_record_id": "brefresh_" + "a" * 32,
            "last_success_at": "2026-07-12T14:00:00+00:00",
            "last_failure_at": None,
            "last_error_code": None,
            "lease_owner_id": None,
            "lease_expires_at": None,
            "updated_at": "2026-07-12T14:00:00+00:00",
            "automation_enabled": True,
            "signer_configured": True,
            "lead_seconds": 28800,
            "check_interval_seconds": 3600,
            "lease_seconds": 600,
            "scheduler_running": True,
            "scheduler_ready": True,
            "scheduler_last_heartbeat_at": "2026-07-12T14:00:00+00:00",
            "scheduler_last_error_code": None,
            "scheduler_heartbeat_max_age_seconds": 4320,
        }
        if run_state is not None:
            value["run_state"] = run_state
        return value

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            assert json.loads(request.content)["client_request_id"] == "refresh-one"
            return httpx.Response(200, json=payload(run_state="succeeded"))
        return httpx.Response(200, json=payload())

    control = client(handler)
    assert control.bootstrap_freshness_status().status == "healthy"
    assert (
        control.refresh_bootstrap_freshness(client_request_id="refresh-one").run_state
        == "succeeded"
    )
    assert observed == [
        "GET /api/v1/admin/bootstrap-index/freshness",
        "POST /api/v1/admin/bootstrap-index/freshness/refresh",
    ]
