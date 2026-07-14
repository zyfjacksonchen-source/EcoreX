from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ecorex.protocol import (
    CreateThreadRequest,
    CreateTurnRequest,
    ForkThreadRequest,
    InteractionKind,
    ItemKind,
    ItemStatus,
    PublicToolActivity,
    ReplaceTurnRequest,
    SteerTurnRequest,
    TurnStatus,
)
from ecorex.runtime import (
    DurableJobStore,
    EventStore,
    RuntimeKernel,
    RuntimeSettings,
    SQLiteDatabase,
    create_app,
)
from ecorex.runtime.api import _stream_events
from ecorex.runtime.errors import (
    ConflictError,
    IdempotencyConflictError,
    LeaseError,
    SchemaVersionError,
)


BASE = datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc)
RUNTIME_TOKEN = "r" * 32
CSRF_TOKEN = "c" * 32


def _public_tool_content(
    status: ItemStatus,
    *,
    tool_call_id: str = "test-tool-call",
    tool_id: str = "read",
) -> dict:
    phase = {
        ItemStatus.CREATED: "requested",
        ItemStatus.IN_PROGRESS: "running",
        ItemStatus.WAITING_HUMAN: "waiting_human",
        ItemStatus.COMPLETED: "completed",
        ItemStatus.FAILED: "failed",
        ItemStatus.CANCELLED: "cancelled",
    }[status]
    return PublicToolActivity(
        tool_call_id=tool_call_id,
        tool_id=tool_id,
        tool_name=tool_id,
        display_label="读取工作资料",
        phase=phase,
        status=status.value,
        risk="low",
        argument_summary="正在读取工作资料",
        result_summary=("此步骤已完成" if status is ItemStatus.COMPLETED else None),
        argument_sha256="0" * 64,
    ).model_dump(mode="json")


def test_projection_and_event_page_watermarks_share_one_snapshot(tmp_path, monkeypatch):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    writer = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    original = kernel.events.watermark
    fired = False

    def interleaved(thread_id, connection=None):
        nonlocal fired
        if not fired:
            fired = True
            writer.create_turn(
                thread_id,
                CreateTurnRequest(input="late", client_message_id="late-message"),
            )
        return original(thread_id, connection)

    monkeypatch.setattr(kernel.events, "watermark", interleaved)
    projection = kernel.projection(thread.thread_id)
    assert len(projection.turns) == 0
    # The concurrent turn must remain after the snapshot cursor.
    assert kernel.events.page(thread.thread_id, after_seq=projection.watermark).events

    store = EventStore(tmp_path / "events.db")
    concurrent = EventStore(tmp_path / "events.db")
    store.append(thread_id="t", event_type="test.one")
    page_watermark = store.watermark
    fired = False

    def append_before_watermark(thread_id, connection=None):
        nonlocal fired
        if not fired:
            fired = True
            concurrent.append(thread_id="t", event_type="test.two")
        return page_watermark(thread_id, connection)

    monkeypatch.setattr(store, "watermark", append_before_watermark)
    page = store.page("t", limit=10)
    assert [event.seq for event in page.events] == [1]
    assert page.watermark == 1


def test_event_storage_rejects_replace_autocommit_and_wrong_schema(tmp_path):
    store = EventStore(tmp_path / "events.db")
    event = store.append(thread_id="t", event_type="test.original")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store.database.transaction() as connection:
            connection.execute(
                """REPLACE INTO events
                SELECT event_id, schema_version, thread_id, seq, turn_id, item_id,
                       job_id, tool_call_id, client_message_id, causation_id,
                       correlation_id, trace_id, config_snapshot_id,
                       capability_snapshot_id, permission_snapshot_id,
                       extension_snapshot_id,
                       'test.tampered', created_at, payload_json, idempotency_key
                  FROM events WHERE event_id = ?""",
                (event.event_id,),
            )

    connection = store.database.connect()
    try:
        with pytest.raises(RuntimeError, match="active transaction"):
            store.append_in_transaction(
                connection, thread_id="gap", event_type="test.invalid"
            )
    finally:
        connection.close()
    assert store.watermark("gap") == 0

    wrong = tmp_path / "wrong.db"
    connection = sqlite3.connect(wrong)
    connection.execute("CREATE TABLE runtime_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "INSERT INTO runtime_meta(key, value) VALUES ('storage_schema_version', '999')"
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="schema version"):
        SQLiteDatabase(wrong)


@pytest.mark.parametrize("missing_column", ["event", "job_context"])
def test_runtime_rejects_incomplete_declared_schema_without_mutating_it(
    tmp_path, missing_column
):
    path = tmp_path / f"incomplete-{missing_column}.db"
    connection = sqlite3.connect(path)
    event_extension = (
        "" if missing_column == "event" else ", extension_snapshot_id TEXT"
    )
    job_extension = (
        "" if missing_column == "job_context" else ", extension_snapshot_id TEXT"
    )
    connection.executescript(
        f"""
        CREATE TABLE runtime_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO runtime_meta(key, value)
        VALUES ('storage_schema_version', '1');
        CREATE TABLE events(
            event_id TEXT,
            thread_id TEXT,
            seq INTEGER,
            idempotency_key TEXT
            {event_extension}
        );
        CREATE TABLE job_runtime_contexts(
            job_id TEXT,
            config_snapshot_id TEXT,
            capability_snapshot_id TEXT,
            permission_snapshot_id TEXT,
            model_catalog_snapshot_id TEXT
            {job_extension}
        );
        """
    )
    connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    expected = "events columns" if missing_column == "event" else "job_runtime_contexts columns"
    with pytest.raises(SchemaVersionError, match=expected):
        SQLiteDatabase(path)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    connection = sqlite3.connect(path)
    try:
        event_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(events)")
        }
        job_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(job_runtime_contexts)")
        }
    finally:
        connection.close()
    assert ("extension_snapshot_id" in event_columns) is (missing_column != "event")
    assert ("extension_snapshot_id" in job_columns) is (
        missing_column != "job_context"
    )


def test_runtime_rejects_nonempty_unversioned_database_without_repair(tmp_path):
    path = tmp_path / "unversioned.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE orphaned_legacy_state(value TEXT)")
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with pytest.raises(SchemaVersionError, match="version table is missing"):
        SQLiteDatabase(path)

    assert path.read_bytes() == before
    connection = sqlite3.connect(path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
    assert tables == {"orphaned_legacy_state"}


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("DROP INDEX idx_events_turn", "core objects are missing"),
        (
            "DROP TRIGGER events_are_append_only_delete; "
            "CREATE TRIGGER events_are_append_only_delete "
            "BEFORE DELETE ON events BEGIN SELECT 1; END",
            "definition is incompatible",
        ),
    ],
)
def test_runtime_rejects_core_schema_drift_without_recreating_it(
    tmp_path, corruption, message
):
    path = tmp_path / ("core-drift-" + hashlib.sha256(corruption.encode()).hexdigest()[:8] + ".db")
    SQLiteDatabase(path)
    connection = sqlite3.connect(path)
    connection.executescript(corruption)
    connection.commit()
    before = tuple(
        connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name IN ('idx_events_turn', 'events_are_append_only_delete') "
            "ORDER BY type, name"
        ).fetchall()
    )
    connection.close()

    with pytest.raises(SchemaVersionError, match=message):
        SQLiteDatabase(path)

    connection = sqlite3.connect(path)
    try:
        after = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                "WHERE name IN ('idx_events_turn', 'events_are_append_only_delete') "
                "ORDER BY type, name"
            ).fetchall()
        )
    finally:
        connection.close()
    assert after == before


def test_runtime_rejects_product_catalog_identity_drift_without_repair(tmp_path):
    path = tmp_path / "product-catalog-drift.db"
    SQLiteDatabase(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE runtime_meta SET value = ? WHERE key = 'product_schema_sha256'",
        ("0" * 64,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SchemaVersionError, match="fingerprint metadata"):
        SQLiteDatabase(path)

    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM runtime_meta WHERE key = 'product_schema_sha256'"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("0" * 64,)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("tool_call_id", "tool-2"),
        ("client_message_id", "message-2"),
        ("causation_id", "cause-2"),
        ("correlation_id", "correlation-2"),
        ("trace_id", "trace-2"),
        ("config_snapshot_id", "config-2"),
        ("capability_snapshot_id", "capability-2"),
        ("permission_snapshot_id", "permission-2"),
        ("created_at", BASE + timedelta(seconds=1)),
    ],
)
def test_event_idempotency_covers_the_full_requested_envelope(
    tmp_path, field, changed
):
    store = EventStore(tmp_path / f"{field}.db")
    request = {
        "thread_id": "thread",
        "turn_id": "turn",
        "item_id": "item",
        "job_id": "job",
        "tool_call_id": "tool-1",
        "client_message_id": "message-1",
        "causation_id": "cause-1",
        "correlation_id": "correlation-1",
        "trace_id": "trace-1",
        "config_snapshot_id": "config-1",
        "capability_snapshot_id": "capability-1",
        "permission_snapshot_id": "permission-1",
        "event_type": "test.envelope",
        "payload": {"value": 1},
        "idempotency_key": "same-key",
        "created_at": BASE,
    }
    store.append(**request)
    with pytest.raises(IdempotencyConflictError):
        store.append(**{**request, field: changed})


def test_lease_fencing_deadline_convergence_and_heartbeat_audit(tmp_path):
    jobs = DurableJobStore(tmp_path / "runtime.db")
    first = jobs.enqueue(
        kind="turn",
        payload={},
        idempotency_key="first",
        thread_id="t",
        available_at=BASE,
        max_attempts=3,
    )
    attempt_one = jobs.lease_next("same-worker", now=BASE, lease_seconds=10)
    assert attempt_one is not None and attempt_one.lease_token
    jobs.start(
        first.job_id,
        "same-worker",
        attempt_one.lease_token,
        now=BASE + timedelta(seconds=1),
    )
    jobs.heartbeat(
        first.job_id,
        "same-worker",
        attempt_one.lease_token,
        checkpoint={"offset": 1},
        lease_seconds=5,
        now=BASE + timedelta(seconds=2),
    )
    jobs.heartbeat(
        first.job_id,
        "same-worker",
        attempt_one.lease_token,
        checkpoint={"offset": 2},
        lease_seconds=5,
        now=BASE + timedelta(seconds=3),
    )
    assert sum(
        event.event_type == "job.heartbeat"
        for event in jobs.events.page("t").events
    ) == 2

    attempt_two = jobs.lease_next(
        "same-worker", now=BASE + timedelta(seconds=9), lease_seconds=10
    )
    assert attempt_two is not None and attempt_two.attempt == 2
    jobs.start(
        first.job_id,
        "same-worker",
        attempt_two.lease_token,
        now=BASE + timedelta(seconds=10),
    )
    with pytest.raises(LeaseError):
        jobs.complete(
            first.job_id,
            "same-worker",
            attempt_one.lease_token,
            now=BASE + timedelta(seconds=11),
        )

    expired = jobs.enqueue(
        kind="turn",
        payload={},
        idempotency_key="expired",
        thread_id="deadline-thread",
        available_at=BASE,
        deadline=BASE - timedelta(seconds=1),
    )
    next_job = jobs.enqueue(
        kind="turn",
        payload={},
        idempotency_key="after-expired",
        thread_id="deadline-thread",
        available_at=BASE,
    )
    leased = jobs.lease_next("other-worker", now=BASE)
    assert jobs.get(expired.job_id).status.value == "failed"
    assert leased is not None and leased.job_id == next_job.job_id


def test_active_deadline_failure_commits_before_owner_is_rejected(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    created = kernel.create_turn(thread.thread_id, CreateTurnRequest(input="deadline"))
    now = datetime.now(timezone.utc)
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET available_at = ?, deadline = ? WHERE job_id = ?",
            (
                now.isoformat(timespec="microseconds"),
                (now + timedelta(seconds=1)).isoformat(timespec="microseconds"),
                created.job.job_id,
            ),
        )
    leased = kernel.jobs.lease_next("worker", now=now, lease_seconds=10)
    assert leased is not None
    kernel.jobs.start(
        leased.job_id,
        "worker",
        leased.lease_token,
        now=now + timedelta(milliseconds=500),
    )
    with pytest.raises(LeaseError):
        kernel.jobs.heartbeat(
            leased.job_id,
            "worker",
            leased.lease_token,
            now=now + timedelta(seconds=2),
        )
    assert kernel.jobs.get(leased.job_id).status.value == "failed"
    assert kernel.get_turn(created.turn.turn_id).status == TurnStatus.FAILED


def test_scheduler_rotates_equal_priority_threads(tmp_path):
    jobs = DurableJobStore(tmp_path / "runtime.db")
    first_a = jobs.enqueue(
        kind="turn", payload={}, idempotency_key="a1", thread_id="a"
    )
    second_a = jobs.enqueue(
        kind="turn", payload={}, idempotency_key="a2", thread_id="a"
    )
    first_b = jobs.enqueue(
        kind="turn", payload={}, idempotency_key="b1", thread_id="b"
    )
    leased_a = jobs.lease_next("worker")
    assert leased_a is not None and leased_a.job_id == first_a.job_id
    jobs.start(leased_a.job_id, "worker", leased_a.lease_token)
    jobs.complete(leased_a.job_id, "worker", leased_a.lease_token)
    leased_b = jobs.lease_next("worker")
    assert leased_b is not None and leased_b.job_id == first_b.job_id
    assert leased_b.job_id != second_a.job_id


def test_thread_message_id_and_terminal_dependents_are_invariant(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    turn = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="same", client_message_id="message-id"),
    )
    repeated = kernel.steer_turn(
        turn.turn.turn_id,
        SteerTurnRequest(input="same", client_message_id="message-id"),
    )
    assert repeated.turn.turn_id == turn.turn.turn_id
    assert len(kernel.projection(thread.thread_id).items) == 1

    item = kernel.create_item(
        turn_id=turn.turn.turn_id,
        kind=ItemKind.TOOL_CALL,
        content=_public_tool_content(ItemStatus.CREATED),
    )
    interaction = kernel.interactions.create(
        kind=InteractionKind.INFORMATION,
        prompt="more?",
        thread_id=thread.thread_id,
        turn_id=turn.turn.turn_id,
        job_id=turn.job.job_id,
        idempotency_key="interaction",
    )
    replacement = kernel.replace_turn(
        turn.turn.turn_id,
        ReplaceTurnRequest(input="replacement", client_message_id="replacement-message"),
    )
    assert kernel.interactions.get(interaction.interaction_id).status.value == "cancelled"
    with pytest.raises(ConflictError):
        kernel.transition_item(item.item_id, ItemStatus.IN_PROGRESS)
    retry = kernel.replace_turn(
        turn.turn.turn_id,
        ReplaceTurnRequest(input="replacement", client_message_id="replacement-message"),
    )
    assert retry.replacement_turn.turn_id == replacement.replacement_turn.turn_id


def test_hitl_request_response_is_atomic_restart_safe_and_public(tmp_path):
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    thread = kernel.create_thread()
    created = kernel.create_turn(thread.thread_id, CreateTurnRequest(input="write"))
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    leased = kernel.jobs.lease_next("worker")
    assert leased is not None
    kernel.jobs.start(leased.job_id, "worker", leased.lease_token)
    interaction = kernel.request_interaction(
        job_id=leased.job_id,
        worker_id="worker",
        lease_token=leased.lease_token,
        kind=InteractionKind.PERMISSION_APPROVAL,
        prompt="allow?",
        options=[{"id": "allow", "label": "允许"}, {"id": "deny", "label": "拒绝"}],
        idempotency_key="approval",
    )
    assert kernel.jobs.get(leased.job_id).status.value == "waiting_human"
    assert kernel.get_turn(created.turn.turn_id).status == TurnStatus.WAITING_HUMAN
    interaction_item = next(
        item
        for item in kernel.projection(thread.thread_id).items
        if item.item_id == interaction.interaction_id
    )
    assert interaction_item.status == ItemStatus.WAITING_HUMAN

    restarted = RuntimeKernel(path)
    assert restarted.list_interactions(thread.thread_id).interactions == [interaction]
    settings = RuntimeSettings(
        database_path=path,
        runtime_bearer_token=RUNTIME_TOKEN,
        csrf_token=CSRF_TOKEN,
        webui_origins=("http://testserver",),
    )
    client = TestClient(create_app(settings=settings))
    auth = {"Authorization": f"Bearer {RUNTIME_TOKEN}"}
    listed = client.get(
        f"/api/v1/threads/{thread.thread_id}/interactions", headers=auth
    )
    assert listed.status_code == 200 and len(listed.json()["interactions"]) == 1
    resolved = client.post(
        f"/api/v1/interactions/{interaction.interaction_id}/respond",
        json={
            "response": {"action_id": "allow", "values": {}},
            "client_request_id": "approve-runtime-interaction",
        },
        headers={
            **auth,
            "Origin": "http://testserver",
            "X-EcoreX-CSRF": CSRF_TOKEN,
        },
    )
    assert resolved.status_code == 200
    result = resolved.json()
    assert result["interaction"]["status"] == "resolved"
    assert result["job"]["status"] == "queued"
    assert result["turn"]["status"] == "preparing"
    item = next(
        item
        for item in restarted.projection(thread.thread_id).items
        if item.item_id == interaction.interaction_id
    )
    assert item.status == ItemStatus.COMPLETED


def test_finish_turn_job_commits_turn_items_and_job_in_one_terminal_transition(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    created = kernel.create_turn(thread.thread_id, CreateTurnRequest(input="finish"))
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    leased = kernel.jobs.lease_next("worker")
    assert leased is not None
    kernel.jobs.start(leased.job_id, "worker", leased.lease_token)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.MODEL_REQUESTED)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.STREAMING)
    item = kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.TOOL_CALL,
        content=_public_tool_content(ItemStatus.IN_PROGRESS),
        status=ItemStatus.IN_PROGRESS,
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.FINALIZING)
    result = kernel.finish_turn_job(
        job_id=leased.job_id,
        worker_id="worker",
        lease_token=leased.lease_token,
        target=TurnStatus.COMPLETED,
    )
    assert result.turn.status == TurnStatus.COMPLETED
    assert result.job is not None and result.job.status.value == "completed"
    assert next(
        value
        for value in kernel.projection(thread.thread_id).items
        if value.item_id == item.item_id
    ).status == ItemStatus.COMPLETED


def test_expired_hitl_fails_all_dependents_before_terminal_turn_event(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    created = kernel.create_turn(thread.thread_id, CreateTurnRequest(input="expire"))
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    open_item = kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.TOOL_CALL,
        content=_public_tool_content(ItemStatus.IN_PROGRESS),
        status=ItemStatus.IN_PROGRESS,
    )
    leased = kernel.jobs.lease_next("worker")
    assert leased is not None
    kernel.jobs.start(leased.job_id, "worker", leased.lease_token)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    interaction = kernel.request_interaction(
        job_id=leased.job_id,
        worker_id="worker",
        lease_token=leased.lease_token,
        kind=InteractionKind.INFORMATION,
        prompt="answer",
        idempotency_key="expires",
        expires_at=expires_at,
    )
    assert kernel.interactions.expire_due(
        now=expires_at + timedelta(seconds=1)
    ) == [interaction.interaction_id]
    assert kernel.get_turn(created.turn.turn_id).status == TurnStatus.FAILED
    assert kernel.jobs.get(leased.job_id).status.value == "failed"
    item = next(
        value
        for value in kernel.projection(thread.thread_id).items
        if value.item_id == open_item.item_id
    )
    assert item.status == ItemStatus.FAILED
    assert kernel.events.page(thread.thread_id).events[-1].event_type == "turn.status_changed"


def test_fail_turn_job_atomically_enters_retry_wait(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    created = kernel.create_turn(thread.thread_id, CreateTurnRequest(input="retry"))
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.MODEL_REQUESTED)
    leased = kernel.jobs.lease_next("worker")
    assert leased is not None
    kernel.jobs.start(leased.job_id, "worker", leased.lease_token)
    result = kernel.fail_turn_job(
        job_id=leased.job_id,
        worker_id="worker",
        lease_token=leased.lease_token,
        error="gateway unavailable",
        retryable=True,
        retry_delay_seconds=5,
    )
    assert result.turn.status == TurnStatus.RETRY_WAIT
    assert result.job is not None and result.job.status.value == "retry_scheduled"
    assert result.job.lease_token is None


def test_create_thread_client_request_is_idempotent(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    request = CreateThreadRequest(
        title="same", metadata={"a": 1}, client_request_id="request-id"
    )
    first = kernel.create_thread(request)
    assert kernel.create_thread(request).thread_id == first.thread_id
    with pytest.raises(ConflictError):
        kernel.create_thread(
            CreateThreadRequest(
                title="different", client_request_id="request-id"
            )
        )


def test_api_requires_bearer_exact_origin_csrf_and_resyncs_ahead_cursor(tmp_path):
    settings = RuntimeSettings(
        database_path=tmp_path / "runtime.db",
        runtime_bearer_token=RUNTIME_TOKEN,
        csrf_token=CSRF_TOKEN,
        webui_origins=("http://127.0.0.1:8765",),
    )
    client = TestClient(create_app(settings=settings))
    auth = {"Authorization": f"Bearer {RUNTIME_TOKEN}"}
    mutation = {
        **auth,
        "Origin": "http://127.0.0.1:8765",
        "X-EcoreX-CSRF": CSRF_TOKEN,
    }
    assert client.get("/api/v1/bootstrap").status_code == 401
    assert client.post("/api/v1/threads", json={}, headers=auth).status_code == 403
    assert (
        client.post(
            "/api/v1/threads",
            json={},
            headers={**mutation, "Origin": "http://localhost:8765"},
        ).status_code
        == 403
    )
    created = client.post("/api/v1/threads", json={}, headers=mutation)
    assert created.status_code == 201
    thread_id = created.json()["thread_id"]
    ahead = client.get(
        f"/api/v1/threads/{thread_id}/events",
        params={"after_seq": 999},
        headers=auth,
    )
    assert ahead.status_code == 409
    assert ahead.json()["watermark"] < 999
    malformed = client.get(
        f"/api/v1/threads/{thread_id}/events",
        headers={**auth, "Last-Event-ID": "not-a-number"},
    )
    assert malformed.status_code == 400
    openapi = client.get("/api/v1/openapi.json", headers=auth).json()
    assert openapi["security"] == [{"RuntimeBearer": []}]
    assert "RuntimeBearer" in openapi["components"]["securitySchemes"]
    response_contracts = {
        ("/api/v1/threads", "post", "201"): "ThreadProjection",
        ("/api/v1/threads/{thread_id}", "put", "200"): "ThreadProjection",
        ("/api/v1/threads/{thread_id}/archive", "post", "200"): "ThreadProjection",
        ("/api/v1/threads/{thread_id}/restore", "post", "200"): "ThreadProjection",
        ("/api/v1/threads/{thread_id}/turns", "post", "202"): "TurnMutationResponse",
        ("/api/v1/turns/{turn_id}/steer", "post", "202"): "TurnMutationResponse",
        ("/api/v1/threads/{thread_id}/queue", "post", "202"): "TurnMutationResponse",
        ("/api/v1/turns/{turn_id}/replace", "post", "202"): "ReplaceTurnResponse",
        ("/api/v1/threads/{thread_id}/fork", "post", "201"): "ThreadProjection",
        ("/api/v1/turns/{turn_id}/interrupt", "post", "200"): "TurnMutationResponse",
        ("/api/v1/threads/{thread_id}/projection", "get", "200"): "ThreadProjectionResponse",
        (
            "/api/v1/interactions/{interaction_id}/connector-login/begin",
            "post",
            "200",
        ): "ConnectorLoginBeginResponse",
        (
            "/api/v1/interactions/{interaction_id}/connector-login/check",
            "post",
            "200",
        ): "ConnectorLoginCheckResponse",
        (
            "/api/v1/interactions/{interaction_id}/connector-login/check",
            "post",
            "202",
        ): "ConnectorLoginCheckResponse",
        (
            "/api/v1/interactions/{interaction_id}/connector-login/cancel",
            "post",
            "200",
        ): "ConnectorLoginCancelResponse",
    }
    for (path, method, status), schema_name in response_contracts.items():
        schema = openapi["paths"][path][method]["responses"][status]["content"][
            "application/json"
        ]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{schema_name}"}
    create_thread_parameters = openapi["paths"]["/api/v1/threads"]["post"][
        "parameters"
    ]
    assert any(
        parameter["name"] == "X-EcoreX-CSRF"
        for parameter in create_thread_parameters
    )
    stream_parameters = openapi["paths"][
        "/api/v1/threads/{thread_id}/events/stream"
    ]["get"]["parameters"]
    follow = next(parameter for parameter in stream_parameters if parameter["name"] == "follow")
    assert follow["schema"]["default"] is True

    unauthenticated = TestClient(
        create_app(
            settings=RuntimeSettings(
                database_path=tmp_path / "logged-out.db",
                authenticated=False,
                runtime_bearer_token=RUNTIME_TOKEN,
                csrf_token=CSRF_TOKEN,
                webui_origins=("http://127.0.0.1:8765",),
            )
        )
    )
    assert (
        unauthenticated.post("/api/v1/threads", json={}, headers=mutation).status_code
        == 401
    )


def test_follow_stream_uses_one_watermark_keepalive_and_stops_on_disconnect(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    settings = RuntimeSettings(
        database_path=tmp_path / "runtime.db",
        event_poll_interval_seconds=0,
        sse_keepalive_seconds=0,
    )

    class DisconnectingRequest:
        calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls >= 2

    async def collect():
        return [
            chunk
            async for chunk in _stream_events(
                DisconnectingRequest(),
                kernel,
                settings,
                thread.thread_id,
                0,
                True,
            )
        ]

    chunks = asyncio.run(collect())
    assert sum("event: watermark" in chunk for chunk in chunks) == 1
    assert sum(chunk == ": keepalive\n\n" for chunk in chunks) == 1


def test_event_stream_uses_commit_notifications_and_low_frequency_fallback(tmp_path):
    settings = RuntimeSettings(database_path=tmp_path / "runtime.db")
    assert settings.event_poll_interval_seconds <= 0.05
    assert settings.event_idle_poll_interval_seconds <= 0.25
    assert (
        settings.event_idle_poll_interval_seconds
        >= settings.event_poll_interval_seconds
    )
    assert (
        settings.event_notification_fallback_seconds
        >= settings.event_idle_poll_interval_seconds * 4
    )


def test_fork_projection_contains_history_through_boundary(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    source = kernel.create_thread()
    original = kernel.create_turn(
        source.thread_id,
        CreateTurnRequest(input="inherited", client_message_id="source-message"),
    )
    fork_request = ForkThreadRequest(
        from_turn_id=original.turn.turn_id,
        client_request_id="fork-request",
    )
    fork = kernel.fork_thread(source.thread_id, request=fork_request)
    assert kernel.fork_thread(source.thread_id, request=fork_request).thread_id == fork.thread_id
    projection = kernel.projection(fork.thread_id)
    assert any(turn.input == "inherited" for turn in projection.turns)
    assert any(item.content.get("text") == "inherited" for item in projection.items)
    assert all(turn.inherited for turn in projection.turns)
    assert all(item.inherited for item in projection.items)
