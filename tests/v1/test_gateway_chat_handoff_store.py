from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import sqlite3

import pytest

from ecorex.gateway.handoff import ChatModelRevision
from ecorex.gateway.models import (
    GatewayEvent,
    GatewayEventType,
    ModelGatewayRequest,
    ecorex_chat_gateway_policy,
)
from ecorex.gateway.schema import GatewaySchemaManager
from ecorex.gateway.server import (
    GatewayPrincipal,
    GatewayRequestConflict,
    GatewayStoreError,
    SQLiteGatewayStore,
)


MODEL_ID = "ecorex-deepseek-v4-pro"


def revision(number: int = 7) -> ChatModelRevision:
    return ChatModelRevision(
        config_id="model-deepseek",
        revision=number,
        local_model_id=MODEL_ID,
        upstream_model_id="deepseek-v4-flash",
        provider_protocol="openai_compatible_chat",
        provider_origin_preset="deepseek_chat",
    )


def principal() -> GatewayPrincipal:
    return GatewayPrincipal(
        subject="member-1",
        account_id="account-1",
        allowed_model_ids=frozenset({MODEL_ID}),
        quota_period="2026-07",
        request_limit=20,
    )


def request(
    request_id: str,
    *,
    previous_response_id: str | None = None,
    tool_call_id: str | None = None,
) -> ModelGatewayRequest:
    values = dict(
        request_id=request_id,
        thread_id="thread-1",
        turn_id="turn-1",
        trace_id="trace-1",
        model_id=MODEL_ID,
        model_policy=ecorex_chat_gateway_policy(MODEL_ID),
        config_snapshot_id="config-1",
        capability_snapshot_id="capability-1",
        permission_snapshot_id="permission-1",
    )
    if tool_call_id is None:
        return ModelGatewayRequest(input="读取季度报告", **values)
    return ModelGatewayRequest(
        previous_response_id=previous_response_id,
        input_items=[
            {
                "type": "function_call_output",
                "tool_call_id": tool_call_id,
                "output": {"status": "ok"},
            }
        ],
        **values,
    )


def available_handoff(store: SQLiteGatewayStore) -> tuple[ModelGatewayRequest, GatewayEvent]:
    source = request("source-request")
    reservation = store.reserve(source, principal(), lease_seconds=60)
    assert reservation.lease_token
    store.bind_chat_model_attempt(source, revision(), ttl_seconds=300)
    event = GatewayEvent(
        seq=1,
        event_type=GatewayEventType.TOOL_CALL_REQUESTED,
        response_id="chatcmpl_source",
        tool_call_id="call_source",
        tool_name="read.document",
        arguments={"path": "report.docx"},
        idempotency_key="tool-source",
    )
    store.stage_chat_handoff(
        source,
        revision(),
        event,
        provider_tool_name="read_document",
        arguments_json='{"path":"report.docx"}',
    )
    store.append_terminal(source.request_id, reservation.lease_token, event)
    return source, event


def test_chat_handoff_survives_restart_and_is_consumed_once(tmp_path) -> None:
    database = tmp_path / "gateway.sqlite3"
    GatewaySchemaManager(database).migrate()
    first = SQLiteGatewayStore(database)
    _source, event = available_handoff(first)

    restarted = SQLiteGatewayStore(database)
    target = request(
        "target-request",
        previous_response_id=event.response_id,
        tool_call_id=event.tool_call_id,
    )
    restarted.reserve(target, principal(), lease_seconds=60)
    preview = restarted.consume_chat_handoff(target, revision(), consume=False)
    handoff = restarted.consume_chat_handoff(target, revision())
    assert handoff is not None
    assert preview == handoff
    assert handoff.assistant_message()["tool_calls"][0]["function"] == {
        "name": "read_document",
        "arguments": '{"path":"report.docx"}',
    }
    assert restarted.consume_chat_handoff(target, revision()) == handoff

    second = request(
        "second-target",
        previous_response_id=event.response_id,
        tool_call_id=event.tool_call_id,
    )
    restarted.reserve(second, principal(), lease_seconds=60)
    with pytest.raises(GatewayRequestConflict, match="already consumed"):
        restarted.consume_chat_handoff(second, revision())


def test_chat_handoff_expiry_and_revision_drift_fail_closed(tmp_path) -> None:
    database = tmp_path / "gateway.sqlite3"
    GatewaySchemaManager(database).migrate()
    store = SQLiteGatewayStore(database)
    _source, event = available_handoff(store)

    drift = request(
        "drift-target",
        previous_response_id=event.response_id,
        tool_call_id=event.tool_call_id,
    )
    store.reserve(drift, principal(), lease_seconds=60)
    with pytest.raises(GatewayRequestConflict, match="configuration changed"):
        store.consume_chat_handoff(drift, revision(8))

    expired = request(
        "expired-target",
        previous_response_id=event.response_id,
        tool_call_id=event.tool_call_id,
    )
    store.reserve(expired, principal(), lease_seconds=60)
    with sqlite3.connect(database) as connection:
        expiry = datetime.fromisoformat(
            connection.execute(
                "SELECT expires_at FROM gateway_chat_handoffs"
            ).fetchone()[0]
        )
    with pytest.raises(GatewayRequestConflict, match="expired"):
        store.consume_chat_handoff(
            expired,
            revision(),
            now=expiry + timedelta(seconds=1),
        )


def test_corrupt_handoff_is_quarantined_without_breaking_other_requests(tmp_path) -> None:
    database = tmp_path / "gateway.sqlite3"
    GatewaySchemaManager(database).migrate()
    store = SQLiteGatewayStore(database)
    _source, event = available_handoff(store)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER gateway_chat_handoffs_identity_immutable")
        connection.execute(
            "UPDATE gateway_chat_handoffs SET arguments_sha256=?",
            (hashlib.sha256(b"tampered").hexdigest(),),
        )

    target = request(
        "corrupt-target",
        previous_response_id=event.response_id,
        tool_call_id=event.tool_call_id,
    )
    store.reserve(target, principal(), lease_seconds=60)
    with pytest.raises(GatewayStoreError, match="corrupt"):
        store.consume_chat_handoff(target, revision())
    with sqlite3.connect(database) as connection:
        state = connection.execute(
            "SELECT state FROM gateway_chat_handoffs"
        ).fetchone()[0]
    assert state == "corrupt"

    unrelated = request("unrelated-request")
    assert store.reserve(unrelated, principal(), lease_seconds=60).mode == "execute"


def test_tenant_model_attempt_and_terminal_are_immutable_audit_facts(tmp_path) -> None:
    database = tmp_path / "gateway.sqlite3"
    GatewaySchemaManager(database).migrate()
    store = SQLiteGatewayStore(database)
    tenant = GatewayPrincipal(
        subject="member-1",
        account_id="account-1",
        organization_id="organization-1",
        allowed_model_ids=frozenset({MODEL_ID}),
        quota_period="2026-07",
        request_limit=20,
    )
    body = request("audited-request").model_copy(
        update={
            "model_policy": ecorex_chat_gateway_policy(MODEL_ID).model_copy(
                update={"reasoning_effort": "max"}
            )
        }
    )
    reservation = store.reserve(body, tenant, lease_seconds=60)
    assert reservation.lease_token
    store.bind_model_attempt(
        body,
        config_id="model-luna",
        config_revision=9,
        upstream_model_id="gpt-5.6-luna",
        provider_protocol="responses",
        provider_origin_preset="ecorex_chat",
        ttl_seconds=300,
    )
    terminal = GatewayEvent(
        seq=1,
        event_type=GatewayEventType.RESPONSE_FAILED,
        response_id="response-audited",
        error_code="provider_rejected",
        error_message="The managed model provider rejected the request.",
    )
    store.append_terminal(body.request_id, reservation.lease_token, terminal)

    with sqlite3.connect(database) as connection:
        request_fact = connection.execute(
            "SELECT organization_id,terminal_event_type FROM gateway_requests "
            "WHERE request_id=?",
            (body.request_id,),
        ).fetchone()
        attempt_fact = connection.execute(
            "SELECT organization_id,thread_id,turn_id,upstream_model_id,"
            "reasoning_effort FROM gateway_model_attempts WHERE request_id=?",
            (body.request_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE gateway_model_attempts SET reasoning_effort='medium' "
                "WHERE request_id=?",
                (body.request_id,),
            )

    assert request_fact == ("organization-1", GatewayEventType.RESPONSE_FAILED.value)
    assert attempt_fact == (
        "organization-1",
        "thread-1",
        "turn-1",
        "gpt-5.6-luna",
        "max",
    )
    changed_tenant = GatewayPrincipal(
        subject=tenant.subject,
        account_id=tenant.account_id,
        organization_id="organization-2",
        allowed_model_ids=tenant.allowed_model_ids,
        quota_period=tenant.quota_period,
        request_limit=tenant.request_limit,
    )
    with pytest.raises(GatewayRequestConflict, match="different input"):
        store.reserve(body, changed_tenant, lease_seconds=60)
