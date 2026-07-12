from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Mapping

import pytest

from ecorex.connectors import ConnectorOutboxEvent
from ecorex.integration.connector_events import (
    ConnectorEventScopeError,
    RuntimeConnectorEventSink,
)
from ecorex.protocol import (
    CreateThreadRequest,
    CreateTurnRequest,
    ItemKind,
    ItemStatus,
    SteerTurnRequest,
)
from ecorex.replay import ReplayIntegrityError
from ecorex.runtime import RuntimeSettings, create_app
from ecorex.runtime.public_tools import PublicToolActivityProjector


TOKEN = "r" * 43
CSRF = "c" * 43
ORIGIN = "http://testserver"
INSTANCE_ID = "conninst_observability"
CONNECTOR_ID = "feishu"
ACTION_ID = "documents.read"
CONTRACT_SHA256 = "a" * 64
INPUT_SHA256 = "b" * 64
IDEMPOTENCY_SHA256 = "c" * 64
POLICY_SHA256 = "d" * 64
RESULT_SHA256 = "e" * 64
DISCOVERY_ID = (
    f"connector:{INSTANCE_ID}@{CONNECTOR_ID}/{ACTION_ID}@{CONTRACT_SHA256}"
)


def _settings(tmp_path) -> RuntimeSettings:
    return RuntimeSettings(
        database_path=tmp_path / "runtime.db",
        runtime_bearer_token=TOKEN,
        csrf_token=CSRF,
        webui_origins=(ORIGIN,),
    )


def _event(
    *,
    event_id: str,
    event_type: str,
    aggregate_id: str,
    aggregate_seq: int,
    payload: Mapping[str, Any],
) -> ConnectorOutboxEvent:
    return ConnectorOutboxEvent(
        event_id=event_id,
        event_type=event_type,
        aggregate_id=aggregate_id,
        aggregate_seq=aggregate_seq,
        payload=dict(payload),
        created_at=datetime.now(UTC),
        lease_token=f"lease-{event_id}",
        attempts=0,
    )


def _insert_outbox(app, event: ConnectorOutboxEvent) -> None:
    encoded = json.dumps(
        dict(event.payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            "INSERT INTO connector_outbox("
            "event_id, event_type, aggregate_id, payload_json, payload_sha256, "
            "aggregate_seq, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.event_type,
                event.aggregate_id,
                encoded,
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                event.aggregate_seq,
                event.created_at.isoformat(),
            ),
        )


def _insert_invocation(
    app,
    invocation_id: str,
    *,
    instance_id: str = INSTANCE_ID,
    connector_id: str = CONNECTOR_ID,
    action_id: str = ACTION_ID,
) -> None:
    now = datetime.now(UTC).isoformat()
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            "INSERT INTO connector_invocations("
            "invocation_id, operation_id, instance_id, connector_id, action_id, "
            "input_sha256, idempotency_key_sha256, admission_policy_sha256, "
            "status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)",
            (
                invocation_id,
                f"operation-{invocation_id}",
                instance_id,
                connector_id,
                action_id,
                INPUT_SHA256,
                IDEMPOTENCY_SHA256,
                POLICY_SHA256,
                now,
                now,
            ),
        )


def _model_scope(app, *, replanned: bool = False):
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="Connector trace"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="使用飞书读取产品文档",
            client_message_id="connector-observability-message",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    batch = kernel.turn_execution_batches.create(
        turn_id=created.turn.turn_id,
        first_revision_ordinal=0,
        last_revision_ordinal=0,
        snapshot_context=prepared.snapshot_context,
    )
    if replanned:
        kernel.steer_turn(
            created.turn.turn_id,
            SteerTurnRequest(
                input="继续使用飞书读取补充文档",
                client_message_id="connector-observability-steer",
            ),
        )
        prepared = composition.prepare_turn(
            CreateTurnRequest(
                input="使用飞书读取产品文档，并继续读取补充文档",
                agent_model_id=created.turn.agent_model_id,
                image_model_id=created.turn.image_model_id,
                client_message_id="connector-observability-replanned",
            )
        )
        batch = kernel.turn_execution_batches.create(
            turn_id=created.turn.turn_id,
            first_revision_ordinal=1,
            last_revision_ordinal=1,
            snapshot_context=prepared.snapshot_context,
        )
    tool_call_id = "connector-observability-call"
    arguments = {
        "discovery_id": DISCOVERY_ID,
        "input": {"document_id": "public-document-id"},
    }
    public_activity = PublicToolActivityProjector().requested(
        composition.capability_service.registry.get("connector_read"),
        tool_call_id=tool_call_id,
        arguments=arguments,
    )
    item = kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.TOOL_CALL,
        status=ItemStatus.IN_PROGRESS,
        content=public_activity.model_dump(mode="json"),
    )
    call_event = kernel.events.append(
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        item_id=item.item_id,
        tool_call_id=tool_call_id,
        event_type="tool.call_requested",
        payload={"activity": public_activity.model_dump(mode="json")},
        idempotency_key="connector-observability-tool-call",
    )
    composition.tool_execution_repository.begin(
        tool_call_id=tool_call_id,
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
        capability_snapshot_id=prepared.snapshot_context.capability_snapshot_id,
        policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
        tool_id="connector_read",
        arguments=arguments,
        idempotency_key="connector-observability-idempotency",
    )
    context = {
        "job_id": created.job.job_id,
        "thread_id": thread.thread_id,
        "turn_id": created.turn.turn_id,
        "execution_batch_id": batch.batch_id,
        "tool_call_id": tool_call_id,
        "capability_snapshot_id": (
            prepared.snapshot_context.capability_snapshot_id
        ),
        "permission_snapshot_id": (
            prepared.snapshot_context.permission_snapshot_id
        ),
        "connector_catalog_snapshot_id": (
            composition.connector_catalog_snapshot.snapshot_id
        ),
        "discovery_id": DISCOVERY_ID,
    }
    _insert_invocation(app, "conninvoke_observability")
    accepted = next(
        event
        for event in kernel.events.page(thread.thread_id, limit=1000).events
        if event.event_type == "turn.accepted"
    )
    return {
        "thread_id": thread.thread_id,
        "turn_id": created.turn.turn_id,
        "job_id": created.job.job_id,
        "tool_call_id": tool_call_id,
        "item_id": item.item_id,
        "call_event_id": call_event.event_id,
        "accepted_trace_id": accepted.trace_id,
        "runtime": context,
    }


def _invocation_payload(*, runtime=None, status: str = "running") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "invocation_id": "conninvoke_observability",
        "instance_id": INSTANCE_ID,
        "connector_id": CONNECTOR_ID,
        "action_id": ACTION_ID,
        "input_sha256": INPUT_SHA256,
        "idempotency_key_sha256": IDEMPOTENCY_SHA256,
        "admission_policy_sha256": POLICY_SHA256,
        "status": status,
    }
    if runtime is not None:
        payload["runtime"] = runtime
    return payload


def test_model_connector_events_use_authoritative_thread_audit_and_tool_trace(
    tmp_path,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    scope = _model_scope(app)
    sink = RuntimeConnectorEventSink(app.state.runtime, account_id="local-user")
    started_payload = {
        **_invocation_payload(runtime=scope["runtime"]),
        "access_token": "must-not-reach-runtime-event",
        "request_body": {"password": "must-not-reach-audit"},
        "local_path": "C:\\Users\\alice\\private.txt",
    }
    started = _event(
        event_id="connevent_observability_started",
        event_type="connector.invocation.started",
        aggregate_id="conninvoke_observability",
        aggregate_seq=1,
        payload=started_payload,
    )
    _insert_outbox(app, started)
    sink.publish(started)

    # Completion deliberately omits Runtime correlation.  Recovery must use
    # the immutable started fact from the same Connector aggregate.
    completed = _event(
        event_id="connevent_observability_completed",
        event_type="connector.invocation.completed",
        aggregate_id="conninvoke_observability",
        aggregate_seq=2,
        payload={
            **_invocation_payload(status="completed"),
            "delivery": "inline",
            "result_envelope_sha256": RESULT_SHA256,
            "response_body": "must-not-reach-event",
            "credential_path": "C:\\Users\\alice\\credential.json",
        },
    )
    _insert_outbox(app, completed)
    sink.publish(completed)

    events = app.state.runtime.events.page(
        scope["thread_id"], limit=1000
    ).events
    connector_events = [
        event for event in events if event.event_type.startswith("connector.")
    ]
    assert [event.event_type for event in connector_events] == [
        "connector.invocation.started",
        "connector.invocation.completed",
    ]
    assert all(event.turn_id == scope["turn_id"] for event in connector_events)
    assert all(event.job_id == scope["job_id"] for event in connector_events)
    assert all(
        event.tool_call_id == scope["tool_call_id"] for event in connector_events
    )
    assert all(event.item_id == scope["item_id"] for event in connector_events)
    assert all(
        event.causation_id == scope["call_event_id"] for event in connector_events
    )
    assert all(
        event.trace_id == scope["accepted_trace_id"] for event in connector_events
    )
    event_wire = json.dumps(
        [event.payload for event in connector_events], ensure_ascii=False
    )
    assert "must-not-reach" not in event_wire
    assert "private.txt" not in event_wire
    assert "credential.json" not in event_wire
    assert "runtime" not in event_wire

    audit_records = [
        record
        for record in app.state.audit_outbox.list(
            thread_id=scope["thread_id"], limit=1000
        )
        if record.event_type.startswith("connector.")
    ]
    assert len(audit_records) == 2
    assert all(record.category == "connector" for record in audit_records)
    assert all(record.trace_id == scope["accepted_trace_id"] for record in audit_records)
    assert audit_records[-1].payload["connector_id"] == CONNECTOR_ID
    assert audit_records[-1].payload["action_id"] == ACTION_ID
    assert audit_records[-1].payload["delivery"] == "inline"
    assert audit_records[-1].payload["outcome"] == "completed"
    audit_wire = json.dumps(
        [record.payload for record in audit_records], ensure_ascii=False
    )
    assert "must-not-reach" not in audit_wire
    assert "private.txt" not in audit_wire
    assert "credential.json" not in audit_wire

    trace = app.state.trace_projector.project(scope["thread_id"])
    tool_span = next(
        span
        for span in trace.spans
        if span.attributes.get("ecorex.tool.call_id") == scope["tool_call_id"]
    )
    assert tool_span.attributes["ecorex.connector.id"] == CONNECTOR_ID
    assert tool_span.attributes["ecorex.connector.action_id"] == ACTION_ID
    assert tool_span.attributes["ecorex.connector.instance_id"] == INSTANCE_ID
    assert (
        tool_span.attributes["ecorex.connector.invocation_id"]
        == "conninvoke_observability"
    )
    assert tool_span.attributes["ecorex.connector.discovery_id"] == DISCOVERY_ID
    assert tool_span.attributes["ecorex.connector.delivery"] == "inline"
    assert tool_span.attributes["ecorex.connector.outcome"] == "completed"
    trace_wire = json.dumps(trace.model_dump(mode="json"), ensure_ascii=False)
    assert "must-not-reach" not in trace_wire
    assert "private.txt" not in trace_wire
    assert "credential.json" not in trace_wire


def test_malformed_or_drifted_runtime_scope_fails_closed(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    scope = _model_scope(app)
    sink = RuntimeConnectorEventSink(app.state.runtime, account_id="local-user")
    malformed_runtime = {**scope["runtime"], "access_token": "not-a-scope-field"}
    malformed = _event(
        event_id="connevent_malformed_scope",
        event_type="connector.invocation.started",
        aggregate_id="conninvoke_observability",
        aggregate_seq=1,
        payload=_invocation_payload(runtime=malformed_runtime),
    )
    with pytest.raises(ConnectorEventScopeError, match="shape"):
        sink.publish(malformed)

    drifted_runtime = {**scope["runtime"], "job_id": "job_forged"}
    drifted = _event(
        event_id="connevent_drifted_scope",
        event_type="connector.invocation.started",
        aggregate_id="conninvoke_observability",
        aggregate_seq=2,
        payload=_invocation_payload(runtime=drifted_runtime),
    )
    with pytest.raises(ConnectorEventScopeError, match="Job scope"):
        sink.publish(drifted)

    with app.state.runtime.database.reader() as connection:
        leaked = connection.execute(
            "SELECT 1 FROM events WHERE idempotency_key IN (?, ?)",
            (
                "connector:connevent_malformed_scope",
                "connector:connevent_drifted_scope",
            ),
        ).fetchone()
        audit = connection.execute(
            "SELECT 1 FROM observability_audit_outbox WHERE source_event_id IN ("
            "SELECT event_id FROM events WHERE idempotency_key IN (?, ?))",
            (
                "connector:connevent_malformed_scope",
                "connector:connevent_drifted_scope",
            ),
        ).fetchone()
    assert leaked is None
    assert audit is None


def test_reconciled_invocation_recovers_scope_from_started_aggregate(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    scope = _model_scope(app)
    sink = RuntimeConnectorEventSink(app.state.runtime, account_id="local-user")
    started = _event(
        event_id="connevent_reconcile_started",
        event_type="connector.invocation.started",
        aggregate_id="conninvoke_observability",
        aggregate_seq=1,
        payload=_invocation_payload(runtime=scope["runtime"]),
    )
    _insert_outbox(app, started)
    reconciled = _event(
        event_id="connevent_reconciled_without_runtime",
        event_type="connector.invocation.reconciled",
        aggregate_id="conninvoke_observability",
        aggregate_seq=2,
        payload={
            **_invocation_payload(status="completed"),
            "resolution": "manually_reconciled",
        },
    )
    _insert_outbox(app, reconciled)
    sink.publish(reconciled)

    event = next(
        event
        for event in app.state.runtime.events.page(
            scope["thread_id"], limit=1000
        ).events
        if event.event_type == "connector.invocation.reconciled"
    )
    assert event.turn_id == scope["turn_id"]
    assert event.job_id == scope["job_id"]
    assert event.tool_call_id == scope["tool_call_id"]
    assert event.trace_id == scope["accepted_trace_id"]
    assert event.payload["resolution"] == "manually_reconciled"
    assert event.payload["outcome"] == "reconciled"
    assert "runtime" not in event.payload


def test_replanned_batch_keeps_accepted_trace_but_uses_current_authority(
    tmp_path,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    scope = _model_scope(app, replanned=True)
    accepted = next(
        event
        for event in app.state.runtime.events.page(
            scope["thread_id"], limit=1000
        ).events
        if event.event_type == "turn.accepted"
    )
    assert accepted.capability_snapshot_id != scope["runtime"][
        "capability_snapshot_id"
    ]
    sink = RuntimeConnectorEventSink(app.state.runtime, account_id="local-user")
    started = _event(
        event_id="connevent_replanned_started",
        event_type="connector.invocation.started",
        aggregate_id="conninvoke_observability",
        aggregate_seq=1,
        payload=_invocation_payload(runtime=scope["runtime"]),
    )
    sink.publish(started)
    routed = next(
        event
        for event in app.state.runtime.events.page(
            scope["thread_id"], limit=1000
        ).events
        if event.event_type == "connector.invocation.started"
    )
    assert routed.trace_id == scope["accepted_trace_id"]
    assert routed.capability_snapshot_id == accepted.capability_snapshot_id
    assert routed.tool_call_id == scope["tool_call_id"]


def test_direct_connector_and_lifecycle_events_remain_internal_and_sanitized(
    tmp_path,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    scope = _model_scope(app)
    sink = RuntimeConnectorEventSink(app.state.runtime, account_id="local-user")
    lifecycle = _event(
        event_id="connevent_direct_lifecycle",
        event_type="connector.instance.connected",
        aggregate_id="conninst_direct",
        aggregate_seq=1,
        payload={
            "instance_id": "conninst_direct",
            "connector_id": "feishu",
            "health": "connected",
            "access_token": "must-not-reach-internal-event",
            "path": "C:\\Users\\alice\\connector.json",
        },
    )
    sink.publish(lifecycle)

    direct_invocation_id = "conninvoke_direct"
    _insert_invocation(
        app,
        direct_invocation_id,
        instance_id="conninst_direct",
    )
    direct = _event(
        event_id="connevent_direct_invocation",
        event_type="connector.invocation.completed",
        aggregate_id=direct_invocation_id,
        aggregate_seq=1,
        payload={
            "invocation_id": direct_invocation_id,
            "instance_id": "conninst_direct",
            "connector_id": CONNECTOR_ID,
            "action_id": ACTION_ID,
            "status": "completed",
            "request_body": {"secret": "must-not-reach-audit"},
        },
    )
    _insert_outbox(app, direct)
    sink.publish(direct)

    user_event_types = {
        event.event_type
        for event in app.state.runtime.events.page(
            scope["thread_id"], limit=1000
        ).events
    }
    assert "connector.instance.connected" not in user_event_types
    assert "connector.invocation.completed" not in user_event_types
    with app.state.runtime.database.reader() as connection:
        internal = connection.execute(
            "SELECT thread_id FROM threads "
            "WHERE client_request_id='system:connector-audit'"
        ).fetchone()
    assert internal is not None
    internal_events = [
        event
        for event in app.state.runtime.events.page(
            str(internal["thread_id"]), limit=1000
        ).events
        if event.event_type.startswith("connector.")
    ]
    assert {event.event_type for event in internal_events} == {
        "connector.instance.connected",
        "connector.invocation.completed",
    }
    internal_wire = json.dumps(
        [event.payload for event in internal_events], ensure_ascii=False
    )
    assert "must-not-reach" not in internal_wire
    assert "connector.json" not in internal_wire
    audit = app.state.audit_outbox.list(
        thread_id=str(internal["thread_id"]), limit=1000
    )
    connector_audit = [record for record in audit if record.category == "connector"]
    assert len(connector_audit) == 2
    assert "must-not-reach" not in json.dumps(
        [record.payload for record in connector_audit], ensure_ascii=False
    )


def test_connector_trace_never_creates_a_span_and_identity_drift_fails_closed(
    tmp_path,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    scope = _model_scope(app)
    sink = RuntimeConnectorEventSink(app.state.runtime, account_id="local-user")
    started = _event(
        event_id="connevent_trace_identity",
        event_type="connector.invocation.started",
        aggregate_id="conninvoke_observability",
        aggregate_seq=1,
        payload=_invocation_payload(runtime=scope["runtime"]),
    )
    sink.publish(started)
    app.state.runtime.events.append(
        thread_id=scope["thread_id"],
        turn_id=scope["turn_id"],
        tool_call_id="connector-call-without-tool-span",
        event_type="connector.invocation.completed",
        payload={
            "invocation_id": "conninvoke_fake",
            "connector_id": "fake",
            "secret": "must-not-reach-trace",
        },
        idempotency_key="connector-trace-no-fake-span",
    )
    trace = app.state.trace_projector.project(scope["thread_id"])
    tool_spans = [span for span in trace.spans if span.name == "ecorex.tool"]
    assert len(tool_spans) == 1
    assert "must-not-reach-trace" not in json.dumps(
        trace.model_dump(mode="json"), ensure_ascii=False
    )

    app.state.runtime.events.append(
        thread_id=scope["thread_id"],
        turn_id=scope["turn_id"],
        tool_call_id=scope["tool_call_id"],
        event_type="connector.invocation.completed",
        payload={
            "invocation_id": "conninvoke_different",
            "connector_id": CONNECTOR_ID,
        },
        idempotency_key="connector-trace-identity-drift",
    )
    with pytest.raises(ReplayIntegrityError, match="identity changed"):
        app.state.trace_projector.project(scope["thread_id"])
