from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from fastapi.testclient import TestClient
from starlette.requests import Request

from ecorex.gateway import (
    GatewayEvent,
    GatewayEventType,
    GatewayPrincipal,
    GatewayQuotaExceeded,
    GatewayRequestActive,
    GatewayRequestConflict,
    GatewaySchemaError,
    GatewaySchemaManager,
    GatewayStoreError,
    ModelGatewayRequest,
    SQLiteGatewayStore as _SQLiteGatewayStore,
    create_managed_gateway_app,
)
from ecorex.gateway import server as gateway_server


TOKEN = "managed-session-" + "x" * 32


def SQLiteGatewayStore(path):
    """Test deployment boundary: migrate explicitly before process startup."""

    GatewaySchemaManager(path).migrate()
    return _SQLiteGatewayStore(path)


class Authenticator:
    def __init__(self, *, limit: int = 10) -> None:
        self.limit = limit

    def authenticate(self, bearer_token: str) -> GatewayPrincipal:
        if bearer_token != TOKEN:
            raise PermissionError("bad secret " + bearer_token)
        return GatewayPrincipal(
            subject="user-1",
            account_id="account-1",
            allowed_model_ids=frozenset({"ecorex-chat"}),
            quota_period="2026-07",
            request_limit=self.limit,
        )


class Provider:
    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[ModelGatewayRequest] = []
        self.secret = "PROVIDER-SECRET-MUST-STAY-CLOUD"
        self.fail = False
        self.gap = False
        self.report_failure = False

    async def stream(self, request, principal):
        self.calls += 1
        self.requests.append(request)
        assert principal.account_id == "account-1"
        assert "api_key" not in request.model_dump(mode="json")
        if self.fail:
            raise RuntimeError(self.secret)
        if self.report_failure:
            yield GatewayEvent(
                seq=1,
                event_type=GatewayEventType.RESPONSE_FAILED,
                response_id="response-failed",
                error_code="provider_secret_" + self.secret,
                error_message="upstream rejected key " + self.secret,
                retryable=False,
            )
            return
        first_seq = 2 if self.gap else 1
        yield GatewayEvent(
            seq=first_seq,
            event_type=GatewayEventType.OUTPUT_TEXT_DELTA,
            response_id="response-1",
            delta="你好",
        )
        yield GatewayEvent(
            seq=first_seq + 1,
            event_type=GatewayEventType.RESPONSE_COMPLETED,
            response_id="response-1",
            usage={"input_tokens": 2, "output_tokens": 1},
        )


def request(request_id: str = "request-1", text: str = "hello") -> dict:
    return ModelGatewayRequest(
        request_id=request_id,
        thread_id="thread-1",
        turn_id="turn-1",
        trace_id="trace-1",
        model_id="ecorex-chat",
        input=text,
        config_snapshot_id="config-1",
        capability_snapshot_id="capability-1",
        permission_snapshot_id="permission-1",
    ).model_dump(mode="json")


def headers(token: str = TOKEN) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-EcoreX-Protocol": "1",
    }


def events(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line]


def _complete_usage_request(
    store,
    *,
    request_id: str,
    account_id: str,
    completed_at: datetime,
    input_tokens: int,
    output_tokens: int,
) -> None:
    principal = GatewayPrincipal(
        subject=f"subject-{account_id}",
        account_id=account_id,
        allowed_model_ids=frozenset({"ecorex-chat"}),
        quota_period="2026-07",
        request_limit=100,
    )
    body = ModelGatewayRequest.model_validate(request(request_id))
    reservation = store.reserve(
        body,
        principal,
        lease_seconds=180,
        now=completed_at,
    )
    assert reservation.mode == "execute"
    assert reservation.lease_token
    original = gateway_server._utcnow
    gateway_server._utcnow = lambda: completed_at
    try:
        store.append_terminal(
            request_id,
            reservation.lease_token,
            GatewayEvent(
                seq=1,
                event_type=GatewayEventType.RESPONSE_COMPLETED,
                response_id=f"response-{request_id}",
                usage={
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            ),
        )
    finally:
        gateway_server._utcnow = original


def test_usage_settlement_outbox_is_atomic_and_replay_safe(tmp_path) -> None:
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    completed_at = datetime.now(timezone.utc)
    _complete_usage_request(
        store,
        request_id="usage-outbox-1",
        account_id="account-1",
        completed_at=completed_at,
        input_tokens=13,
        output_tokens=21,
    )

    assert store.usage_settlement_counts() == {
        "pending": 1,
        "settled": 0,
        "usage_missing": 0,
    }
    facts = store.pending_usage_facts(
        account_id="account-1",
        maximum=1,
        now=completed_at + timedelta(seconds=1),
    )
    assert len(facts) == 1
    assert facts[0].total_tokens == 34
    assert store.has_unsettled_usage("account-1") is True

    store.mark_usage_settled(
        facts[0],
        now=completed_at + timedelta(seconds=2),
    )
    store.mark_usage_settled(
        facts[0],
        now=completed_at + timedelta(seconds=3),
    )

    assert store.usage_settlement_counts() == {
        "pending": 0,
        "settled": 1,
        "usage_missing": 0,
    }
    assert store.has_unsettled_usage("account-1") is False
    assert store.pending_usage_facts(
        account_id="account-1",
        now=completed_at + timedelta(days=1),
    ) == ()


def test_usage_settlement_failure_backoff_and_missing_usage_are_durable(
    tmp_path,
) -> None:
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    completed_at = datetime.now(timezone.utc)
    _complete_usage_request(
        store,
        request_id="usage-retry-1",
        account_id="account-1",
        completed_at=completed_at,
        input_tokens=5,
        output_tokens=8,
    )
    fact = store.pending_usage_facts(
        request_id="usage-retry-1",
        now=completed_at + timedelta(seconds=1),
    )[0]
    store.defer_usage_settlement(
        fact.request_id,
        now=completed_at + timedelta(seconds=1),
    )
    assert store.pending_usage_facts(
        request_id=fact.request_id,
        now=completed_at + timedelta(seconds=2),
    ) == ()
    assert store.pending_usage_facts(
        request_id=fact.request_id,
        now=completed_at + timedelta(seconds=4),
    ) == (fact,)

    principal = GatewayPrincipal(
        subject="subject-account-1",
        account_id="account-1",
        allowed_model_ids=frozenset({"ecorex-chat"}),
        quota_period="2026-07",
        request_limit=100,
    )
    body = ModelGatewayRequest.model_validate(request("usage-missing-1"))
    reservation = store.reserve(
        body,
        principal,
        lease_seconds=180,
        now=completed_at,
    )
    original = gateway_server._utcnow
    gateway_server._utcnow = lambda: completed_at
    try:
        store.append_terminal(
            body.request_id,
            reservation.lease_token,
            GatewayEvent(
                seq=1,
                event_type=GatewayEventType.RESPONSE_COMPLETED,
                response_id="response-usage-missing",
            ),
        )
    finally:
        gateway_server._utcnow = original

    assert store.pending_usage_facts(
        request_id=body.request_id,
        now=completed_at + timedelta(seconds=1),
    ) == ()
    assert store.usage_settlement_counts() == {
        "pending": 1,
        "settled": 0,
        "usage_missing": 1,
    }
    assert store.has_unsettled_usage("account-1") is True


def test_usage_endpoint_fails_closed_for_missing_provider_usage(tmp_path) -> None:
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    completed_at = datetime.now(timezone.utc)
    principal = GatewayPrincipal(
        subject="subject-account-1",
        account_id="account-1",
        allowed_model_ids=frozenset({"ecorex-chat"}),
        quota_period="2026-07",
        request_limit=100,
    )
    body = ModelGatewayRequest.model_validate(request("usage-missing-endpoint"))
    reservation = store.reserve(
        body,
        principal,
        lease_seconds=180,
        now=completed_at,
    )
    original = gateway_server._utcnow
    gateway_server._utcnow = lambda: completed_at
    try:
        store.append_terminal(
            body.request_id,
            reservation.lease_token,
            GatewayEvent(
                seq=1,
                event_type=GatewayEventType.RESPONSE_COMPLETED,
                response_id="response-usage-missing-endpoint",
            ),
        )
    finally:
        gateway_server._utcnow = original

    class Accountant:
        def settle(self, _fact) -> None:
            raise AssertionError("missing usage cannot be settled")

        def reconcile(self, _facts) -> None:
            raise AssertionError("full-ledger reconciliation must not run")

        def tokens_available(self, _account_id: str) -> bool:
            return True

        def project(self, _account_id: str, *, timezone_name: str):
            del timezone_name
            raise AssertionError("incomplete usage must not be projected")

    app = create_managed_gateway_app(
        store,
        authenticator=Authenticator(),
        provider=Provider(),
        allowed_model_ids=frozenset({"ecorex-chat"}),
        usage_accountant=Accountant(),
    )
    response = TestClient(app).get("/api/v1/usage", headers=headers())

    assert response.status_code == 503
    assert response.json()["detail"] == "managed account usage is unavailable"


def test_unsettled_account_usage_blocks_new_provider_admission(tmp_path) -> None:
    provider = Provider()
    store = SQLiteGatewayStore(tmp_path / "gateway.db")

    class FailingAccountant:
        def settle(self, _fact) -> None:
            raise RuntimeError("control plane unavailable")

        def reconcile(self, _facts) -> None:
            raise AssertionError("full-ledger reconciliation must not run")

        def tokens_available(self, _account_id: str) -> bool:
            return True

        def project(self, account_id: str, *, timezone_name: str):
            return store.account_usage(account_id, timezone_name=timezone_name)

    app = create_managed_gateway_app(
        store,
        authenticator=Authenticator(),
        provider=provider,
        allowed_model_ids=frozenset({"ecorex-chat"}),
        usage_accountant=FailingAccountant(),
    )
    client = TestClient(app)

    first = client.post(
        "/api/v1/model/stream",
        headers=headers(),
        json=request("usage-fail-1"),
    )
    blocked = client.post(
        "/api/v1/model/stream",
        headers=headers(),
        json=request("usage-fail-2"),
    )

    assert first.status_code == 200
    assert blocked.status_code == 503
    assert blocked.json()["detail"] == "managed usage settlement is pending"
    assert provider.calls == 1


def test_cloud_gateway_auth_allowlist_persists_before_stream_and_replays(tmp_path) -> None:
    provider = Provider()
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    app = create_managed_gateway_app(
        store,
        authenticator=Authenticator(),
        provider=provider,
        allowed_model_ids=frozenset({"ecorex-chat", "ecorex-image"}),
    )
    client = TestClient(app)

    assert client.get("/api/v1/models").status_code == 401
    assert client.get("/api/v1/models", headers=headers()).json() == {
        "schema_version": 1,
        "models": ["ecorex-chat"],
    }
    first = client.post("/api/v1/model/stream", headers=headers(), json=request())
    replay = client.post("/api/v1/model/stream", headers=headers(), json=request())
    assert first.status_code == replay.status_code == 200
    assert events(first) == events(replay)
    assert replay.headers["x-ecorex-replay"] == "true"
    assert provider.calls == 1
    assert [item.seq for item in store.events("request-1")] == [1, 2]

    with sqlite3.connect(tmp_path / "gateway.db") as connection:
        assert connection.execute(
            "SELECT status FROM gateway_requests WHERE request_id='request-1'"
        ).fetchone()[0] == "completed"


def test_account_usage_is_cross_request_account_scoped_and_replay_safe(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteGatewayStore(tmp_path / "gateway-usage.db")
    monday = datetime(2026, 7, 13, 16, 30, tzinfo=timezone.utc)
    today = datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc)
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    _complete_usage_request(
        store,
        request_id="device-a-request",
        account_id="account-1",
        completed_at=monday,
        input_tokens=10,
        output_tokens=2,
    )
    _complete_usage_request(
        store,
        request_id="device-b-request",
        account_id="account-1",
        completed_at=today,
        input_tokens=20,
        output_tokens=3,
    )
    _complete_usage_request(
        store,
        request_id="other-account-request",
        account_id="account-2",
        completed_at=today,
        input_tokens=900,
        output_tokens=100,
    )

    projection = store.account_usage(
        "account-1",
        timezone_name="Asia/Shanghai",
        now=now,
    )
    assert projection.today.model_dump() == {
        "input_tokens": 20,
        "output_tokens": 3,
        "total_tokens": 23,
    }
    assert projection.week.model_dump() == {
        "input_tokens": 30,
        "output_tokens": 5,
        "total_tokens": 35,
    }

    replay = store.reserve(
        ModelGatewayRequest.model_validate(request("device-b-request")),
        GatewayPrincipal(
            subject="subject-account-1",
            account_id="account-1",
            allowed_model_ids=frozenset({"ecorex-chat"}),
            quota_period="2026-07",
            request_limit=100,
        ),
        lease_seconds=180,
        now=now,
    )
    assert replay.mode == "replay"
    assert store.account_usage(
        "account-1",
        timezone_name="Asia/Shanghai",
        now=now,
    ) == projection

    monkeypatch.setattr(gateway_server, "_utcnow", lambda: now)
    app = create_managed_gateway_app(
        store,
        authenticator=Authenticator(),
        provider=Provider(),
        allowed_model_ids=frozenset({"ecorex-chat"}),
    )
    client = TestClient(app)
    assert client.get("/api/v1/usage").status_code == 401
    response = client.get(
        "/api/v1/usage",
        params={"timezone": "Asia/Shanghai"},
        headers=headers(),
    )
    assert response.status_code == 200
    assert response.json()["scope"] == "account"
    assert response.json()["week"]["total_tokens"] == 35


def test_gateway_request_identity_quota_and_model_policy_fail_closed(tmp_path) -> None:
    provider = Provider()
    app = create_managed_gateway_app(
        SQLiteGatewayStore(tmp_path / "gateway.db"),
        authenticator=Authenticator(limit=1),
        provider=provider,
        allowed_model_ids=frozenset({"ecorex-chat", "ecorex-image"}),
    )
    client = TestClient(app)
    assert client.post(
        "/api/v1/model/stream", headers=headers(), json=request()
    ).status_code == 200
    conflict = client.post(
        "/api/v1/model/stream", headers=headers(), json=request(text="different")
    )
    assert conflict.status_code == 409
    exhausted = client.post(
        "/api/v1/model/stream", headers=headers(), json=request("request-2")
    )
    assert exhausted.status_code == 429

    blocked = request("request-3")
    blocked["model_id"] = "ecorex-image"
    assert client.post(
        "/api/v1/model/stream", headers=headers(), json=blocked
    ).status_code == 403
    assert provider.calls == 1


def test_gateway_request_digest_covers_typed_input_items(tmp_path) -> None:
    provider = Provider()
    app = create_managed_gateway_app(
        SQLiteGatewayStore(tmp_path / "gateway.db"),
        authenticator=Authenticator(),
        provider=provider,
        allowed_model_ids=frozenset({"ecorex-chat"}),
    )
    client = TestClient(app)
    first = request("typed-input")
    first.pop("input")
    first["input_items"] = [
        {
            "type": "user_message",
            "message_id": "message-1",
            "content": "第一版指令",
        }
    ]
    changed = json.loads(json.dumps(first))
    changed["input_items"][0]["content"] = "被篡改的指令"

    accepted = client.post(
        "/api/v1/model/stream", headers=headers(), json=first
    )
    conflict = client.post(
        "/api/v1/model/stream", headers=headers(), json=changed
    )

    assert accepted.status_code == 200
    assert conflict.status_code == 409
    assert provider.calls == 1
    assert provider.requests[0].input_items is not None
    assert provider.requests[0].input_items[0].message_id == "message-1"


def test_provider_failure_and_protocol_gap_are_redacted_terminal_facts(tmp_path) -> None:
    provider = Provider()
    provider.fail = True
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    app = create_managed_gateway_app(
        store,
        authenticator=Authenticator(),
        provider=provider,
        allowed_model_ids=frozenset({"ecorex-chat"}),
    )
    client = TestClient(app)

    failed = client.post(
        "/api/v1/model/stream", headers=headers(), json=request("failed")
    )
    assert failed.status_code == 200
    payload = events(failed)
    assert payload[-1]["event_type"] == "response.failed"
    assert payload[-1]["retryable"] is True
    assert provider.secret not in failed.text

    provider.fail = False
    provider.report_failure = True
    reported = client.post(
        "/api/v1/model/stream", headers=headers(), json=request("reported-failure")
    )
    assert reported.status_code == 200
    reported_events = events(reported)
    assert reported_events == [
        {
            "schema_version": 1,
            "seq": 1,
            "event_type": "response.failed",
            "response_id": "response-failed",
            "delta": None,
            "reasoning_id": None,
            "tool_call_id": None,
            "tool_name": None,
            "arguments": None,
            "idempotency_key": None,
            "error_code": "provider_response_failed",
            "error_message": "The managed model provider rejected the request.",
            "retryable": False,
            "usage": None,
        }
    ]
    assert provider.secret not in reported.text

    provider.report_failure = False
    provider.gap = True
    gap = client.post(
        "/api/v1/model/stream", headers=headers(), json=request("gap")
    )
    assert gap.status_code == 200
    gap_events = events(gap)
    assert [item["seq"] for item in gap_events] == [1]
    assert gap_events[0]["event_type"] == "response.failed"
    assert provider.secret not in gap.text


def test_gateway_rejects_oversized_declared_body_without_auth_leak(tmp_path) -> None:
    app = create_managed_gateway_app(
        SQLiteGatewayStore(tmp_path / "gateway.db"),
        authenticator=Authenticator(),
        provider=Provider(),
        allowed_model_ids=frozenset({"ecorex-chat"}),
    )
    response = TestClient(app).post(
        "/api/v1/model/stream",
        headers={**headers(), "Content-Length": str(5 * 1024 * 1024)},
        content=b"{}",
    )
    assert response.status_code == 413
    assert TOKEN not in response.text


def test_gateway_rejects_chunked_oversized_body_and_requires_protocol(tmp_path) -> None:
    provider = Provider()
    app = create_managed_gateway_app(
        SQLiteGatewayStore(tmp_path / "gateway.db"),
        authenticator=Authenticator(),
        provider=provider,
        allowed_model_ids=frozenset({"ecorex-chat"}),
    )
    client = TestClient(app)

    def chunks():
        block = b"x" * (1024 * 1024)
        for _ in range(5):
            yield block

    oversized = client.post(
        "/api/v1/model/stream",
        headers={**headers(), "Content-Type": "application/json"},
        content=chunks(),
    )
    assert oversized.status_code == 413
    assert provider.calls == 0

    missing_protocol = client.post(
        "/api/v1/model/stream",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json=request("missing-protocol"),
    )
    assert missing_protocol.status_code == 400
    wrong_type = client.post(
        "/api/v1/model/stream",
        headers={**headers(), "Content-Type": "text/plain"},
        content=json.dumps(request("wrong-type")),
    )
    assert wrong_type.status_code == 415
    assert provider.calls == 0


def test_gateway_rejects_deep_request_resources_before_provider(tmp_path) -> None:
    provider = Provider()
    app = create_managed_gateway_app(
        SQLiteGatewayStore(tmp_path / "gateway.db"),
        authenticator=Authenticator(),
        provider=provider,
        allowed_model_ids=frozenset({"ecorex-chat"}),
    )
    payload = request("deep-request")
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(24):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    payload["direct_tools"] = [nested]
    response = TestClient(app).post(
        "/api/v1/model/stream", headers=headers(), json=payload
    )
    assert response.status_code == 422

    invalid_unicode = request("invalid-unicode")
    invalid_unicode["input"] = "\ud800"
    response = TestClient(app).post(
        "/api/v1/model/stream",
        headers={**headers(), "Content-Type": "application/json"},
        content=json.dumps(invalid_unicode),
    )
    assert response.status_code == 422

    duplicate_outputs = request("duplicate-outputs")
    duplicate_outputs["previous_response_id"] = "response-before-tools"
    duplicate_outputs["tool_outputs"] = [
        {"tool_call_id": "call-1", "output": {"ok": True}},
        {"tool_call_id": "call-1", "output": {"ok": True}},
    ]
    response = TestClient(app).post(
        "/api/v1/model/stream", headers=headers(), json=duplicate_outputs
    )
    assert response.status_code == 422
    assert provider.calls == 0


def test_authenticator_failures_are_redacted(tmp_path) -> None:
    secret = "AUTH-BACKEND-SECRET"

    class BrokenAuthenticator:
        def authenticate(self, bearer_token):
            del bearer_token
            raise RuntimeError(secret)

    app = create_managed_gateway_app(
        SQLiteGatewayStore(tmp_path / "gateway.db"),
        authenticator=BrokenAuthenticator(),
        provider=Provider(),
        allowed_model_ids=frozenset({"ecorex-chat"}),
    )
    response = TestClient(app).get("/api/v1/models", headers=headers())
    assert response.status_code == 503
    assert secret not in response.text


def test_quota_admission_is_atomic_under_concurrency(tmp_path) -> None:
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    principal = Authenticator(limit=1).authenticate(TOKEN)
    barrier = threading.Barrier(2)

    def reserve(request_id: str) -> str:
        body = ModelGatewayRequest.model_validate(request(request_id))
        barrier.wait(timeout=5)
        try:
            return store.reserve(
                body, principal, lease_seconds=30
            ).mode
        except GatewayQuotaExceeded:
            return "quota"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, ["concurrent-1", "concurrent-2"]))
    assert sorted(outcomes) == ["execute", "quota"]


def test_concurrent_request_limit_releases_after_terminal_or_lease_expiry(tmp_path) -> None:
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    principal = GatewayPrincipal(
        subject="user-1",
        account_id="account-1",
        allowed_model_ids=frozenset({"ecorex-chat"}),
        quota_period="2026-07",
        request_limit=10,
        concurrent_request_limit=1,
    )
    first = ModelGatewayRequest.model_validate(request("active-1"))
    second = ModelGatewayRequest.model_validate(request("active-2"))
    reservation = store.reserve(first, principal, lease_seconds=30)
    assert reservation.lease_token
    with pytest.raises(GatewayQuotaExceeded, match="concurrent"):
        store.reserve(second, principal, lease_seconds=30)
    store.append_terminal(
        first.request_id,
        reservation.lease_token,
        GatewayEvent(
            seq=1,
            event_type=GatewayEventType.RESPONSE_COMPLETED,
            response_id="response-active-1",
        ),
    )
    assert store.reserve(second, principal, lease_seconds=30).mode == "execute"

    expired_store = SQLiteGatewayStore(tmp_path / "expired-gateway.db")
    now = datetime.now(timezone.utc)
    expired_store.reserve(
        ModelGatewayRequest.model_validate(request("expired-slot")),
        principal,
        lease_seconds=30,
        now=now - timedelta(seconds=31),
    )
    assert expired_store.reserve(
        ModelGatewayRequest.model_validate(request("after-expired-slot")),
        principal,
        lease_seconds=30,
        now=now,
    ).mode == "execute"


def test_request_id_cannot_replay_events_across_accounts(tmp_path) -> None:
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    first = Authenticator().authenticate(TOKEN)
    second = GatewayPrincipal(
        subject="user-2",
        account_id="account-2",
        allowed_model_ids=frozenset({"ecorex-chat"}),
        quota_period="2026-07",
        request_limit=10,
    )
    body = ModelGatewayRequest.model_validate(request("shared-id"))
    reservation = store.reserve(body, first, lease_seconds=30)
    assert reservation.lease_token
    store.append_terminal(
        body.request_id,
        reservation.lease_token,
        GatewayEvent(
            seq=1,
            event_type=GatewayEventType.RESPONSE_COMPLETED,
            response_id="response-shared",
        ),
    )
    with pytest.raises(GatewayRequestConflict):
        store.reserve(body, second, lease_seconds=30)


def test_terminal_append_rolls_back_as_one_transaction(tmp_path, monkeypatch) -> None:
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    principal = Authenticator().authenticate(TOKEN)
    body = ModelGatewayRequest.model_validate(request("terminal-atomic"))
    reservation = store.reserve(body, principal, lease_seconds=30)
    assert reservation.lease_token
    terminal = GatewayEvent(
        seq=1,
        event_type=GatewayEventType.RESPONSE_COMPLETED,
        response_id="response-atomic",
    )
    original = store._complete_in_transaction

    with pytest.raises(GatewayStoreError, match="append_terminal"):
        store.append(body.request_id, reservation.lease_token, terminal)
    assert store.events(body.request_id) == ()

    def fail_completion(*args, **kwargs):
        del args, kwargs
        raise GatewayStoreError("injected completion failure")

    monkeypatch.setattr(store, "_complete_in_transaction", fail_completion)
    with pytest.raises(GatewayStoreError, match="injected"):
        store.append_terminal(body.request_id, reservation.lease_token, terminal)
    assert store.events(body.request_id) == ()

    monkeypatch.setattr(store, "_complete_in_transaction", original)
    store.append_terminal(body.request_id, reservation.lease_token, terminal)
    assert store.events(body.request_id) == (terminal,)


def test_expired_active_request_converges_without_reinvoking_provider(tmp_path) -> None:
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    principal = Authenticator().authenticate(TOKEN)
    body = ModelGatewayRequest.model_validate(request("uncertain"))
    start = datetime.now(timezone.utc)
    reservation = store.reserve(body, principal, lease_seconds=30, now=start)
    assert reservation.lease_token
    store.append(
        body.request_id,
        reservation.lease_token,
        GatewayEvent(
            seq=1,
            event_type=GatewayEventType.OUTPUT_TEXT_DELTA,
            response_id="response-uncertain",
            delta="partial",
        ),
    )
    with pytest.raises(GatewayRequestActive):
        store.reserve(
            body,
            principal,
            lease_seconds=30,
            now=start + timedelta(seconds=1),
        )
    recovered = store.reserve(
        body,
        principal,
        lease_seconds=30,
        now=start + timedelta(seconds=31),
    )
    assert recovered.mode == "replay"
    assert [event.event_type for event in recovered.events] == [
        GatewayEventType.OUTPUT_TEXT_DELTA,
        GatewayEventType.RESPONSE_FAILED,
    ]
    assert recovered.events[-1].error_code == "gateway_execution_uncertain"
    with pytest.raises(GatewayRequestConflict):
        store.append(
            body.request_id,
            reservation.lease_token,
            GatewayEvent(
                seq=3,
                event_type=GatewayEventType.OUTPUT_TEXT_DELTA,
                response_id="response-uncertain",
                delta="stale owner",
            ),
        )

    longest_id = "r" * 256
    long_body = ModelGatewayRequest.model_validate(request(longest_id))
    store.reserve(long_body, principal, lease_seconds=30, now=start)
    long_recovery = store.reserve(
        long_body,
        principal,
        lease_seconds=30,
        now=start + timedelta(seconds=31),
    )
    assert long_recovery.events[-1].event_type is GatewayEventType.RESPONSE_FAILED
    assert len(long_recovery.events[-1].response_id) <= 256


def test_gateway_event_ledger_is_append_only_and_detects_forced_tamper(tmp_path) -> None:
    provider = Provider()
    database = tmp_path / "gateway.db"
    store = SQLiteGatewayStore(database)
    app = create_managed_gateway_app(
        store,
        authenticator=Authenticator(),
        provider=provider,
        allowed_model_ids=frozenset({"ecorex-chat"}),
    )
    assert TestClient(app).post(
        "/api/v1/model/stream", headers=headers(), json=request("tamper")
    ).status_code == 200
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE gateway_events SET payload_json='{}' "
                "WHERE request_id='tamper' AND seq=1"
            )
        connection.execute("DROP TRIGGER gateway_events_no_update")
        connection.execute(
            "UPDATE gateway_events SET payload_json='{}' "
            "WHERE request_id='tamper' AND seq=1"
        )
    with pytest.raises(GatewayStoreError, match="integrity"):
        store.events("tamper")


def test_event_chain_detects_payload_hash_rewrite(tmp_path) -> None:
    database = tmp_path / "gateway.db"
    store = SQLiteGatewayStore(database)
    app = create_managed_gateway_app(
        store,
        authenticator=Authenticator(),
        provider=Provider(),
        allowed_model_ids=frozenset({"ecorex-chat"}),
    )
    assert TestClient(app).post(
        "/api/v1/model/stream", headers=headers(), json=request("chain-tamper")
    ).status_code == 200
    replacement = json.dumps(
        {
            "schema_version": 1,
            "seq": 1,
            "event_type": "output_text.delta",
            "response_id": "response-1",
            "delta": "篡改",
            "tool_call_id": None,
            "tool_name": None,
            "arguments": None,
            "idempotency_key": None,
            "error_code": None,
            "error_message": None,
            "retryable": False,
            "usage": None,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    import hashlib

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER gateway_events_no_update")
        connection.execute(
            "UPDATE gateway_events SET payload_json=?,payload_sha256=? "
            "WHERE request_id='chain-tamper' AND seq=1",
            (replacement, hashlib.sha256(replacement.encode("utf-8")).hexdigest()),
        )
    with pytest.raises(GatewayStoreError, match="integrity"):
        store.events("chain-tamper")


def test_stream_disconnect_closes_provider_and_persists_cancelled_terminal(tmp_path) -> None:
    secret = "PROVIDER-FINALIZER-SECRET"

    class CancellableProvider:
        def __init__(self) -> None:
            self.closed = False

        async def stream(self, body, principal):
            del body, principal
            try:
                yield GatewayEvent(
                    seq=1,
                    event_type=GatewayEventType.OUTPUT_TEXT_DELTA,
                    response_id="response-cancel",
                    delta="partial",
                )
                await asyncio.Event().wait()
            finally:
                self.closed = True
                raise RuntimeError(secret)

    provider = CancellableProvider()
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    authenticator = Authenticator()
    principal = authenticator.authenticate(TOKEN)
    app = create_managed_gateway_app(
        store,
        authenticator=authenticator,
        provider=provider,
        allowed_model_ids=frozenset({"ecorex-chat"}),
    )
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/v1/model/stream"
    )
    encoded = json.dumps(request("disconnect")).encode("utf-8")

    async def scenario():
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}

        incoming = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/model/stream",
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"x-ecorex-protocol", b"1"),
                ],
            },
            receive,
        )
        response = await endpoint(incoming, principal)
        first = await anext(response.body_iterator)
        assert b"partial" in first
        await response.body_iterator.aclose()

    asyncio.run(scenario())
    assert provider.closed is True
    persisted = store.events("disconnect")
    assert [event.event_type for event in persisted] == [
        GatewayEventType.OUTPUT_TEXT_DELTA,
        GatewayEventType.RESPONSE_FAILED,
    ]
    assert persisted[-1].error_code == "gateway_cancelled"
    assert secret not in json.dumps(
        [event.model_dump(mode="json") for event in persisted]
    )
