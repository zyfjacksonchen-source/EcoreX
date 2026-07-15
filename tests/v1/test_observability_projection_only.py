from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
from typing import Any

import pytest

from ecorex.observability import (
    AuditDispatcher,
    AuditIntegrityError,
    AuditOutbox,
    AuditPayloadCipher,
    TraceDispatcher,
    TraceExportBatch,
    TraceOutbox,
    TraceOutboxIntegrityError,
)
from ecorex.runtime import SQLiteDatabase


def _table_rows(
    database: SQLiteDatabase,
    *,
    prefix: str,
) -> tuple[tuple[str, tuple[tuple[Any, ...], ...]], ...]:
    with database.reader() as connection:
        tables = sorted(
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' AND name LIKE ? ORDER BY name",
                (prefix + "%",),
            ).fetchall()
        )
        snapshot: list[tuple[str, tuple[tuple[Any, ...], ...]]] = []
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [
                str(row["name"])
                for row in connection.execute(
                    f"PRAGMA table_info({quoted})"
                ).fetchall()
            ]
            ordering = ",".join(
                '"' + column.replace('"', '""') + '"' for column in columns
            )
            rows = connection.execute(
                f"SELECT * FROM {quoted} ORDER BY {ordering}"
            ).fetchall()
            snapshot.append((table, tuple(tuple(row) for row in rows)))
    return tuple(snapshot)


def test_audit_projection_only_is_zero_write_then_converges_once(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "audit.db")
    cipher = AuditPayloadCipher(b"a" * 32)
    before = _table_rows(database, prefix="observability_audit_")

    outbox = AuditOutbox(
        database,
        account_id="projection-account",
        cipher=cipher,
        initialize=False,
    )

    assert outbox.startup_converged is False
    assert outbox.list() == ()
    assert outbox.count() == 0
    with pytest.raises(AuditIntegrityError, match="has not converged"):
        outbox.backfill_events()
    with pytest.raises(AuditIntegrityError, match="has not converged"):
        outbox.enforce_retention()
    with pytest.raises(AuditIntegrityError, match="has not converged"):
        asyncio.run(AuditDispatcher(outbox).start())
    assert _table_rows(database, prefix="observability_audit_") == before

    outbox.converge_startup()
    assert outbox.startup_converged is True
    converged = _table_rows(database, prefix="observability_audit_")
    assert converged != before
    with database.reader() as connection:
        cursor = connection.execute(
            "SELECT * FROM observability_audit_cursors WHERE account_id = ?",
            ("projection-account",),
        ).fetchone()
    assert cursor is not None
    outbox.initialize()
    outbox.converge_startup()
    assert _table_rows(database, prefix="observability_audit_") == converged


def test_audit_projection_only_reads_existing_encrypted_records(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "audit-records.db")
    cipher = AuditPayloadCipher(b"b" * 32)
    writer = AuditOutbox(
        database,
        account_id="account-existing",
        cipher=cipher,
    )
    with database.transaction() as connection:
        writer._persist_view_in_transaction(
            connection,
            source_event_id="event-existing",
            category="task",
            event_type="job.completed",
            thread_id="thread-existing",
            turn_id="turn-existing",
            trace_id="1" * 32,
            payload={"status": "completed", "summary": "encrypted"},
            created_at=datetime(2026, 7, 12, tzinfo=UTC),
        )
    before = _table_rows(database, prefix="observability_audit_")

    reader = AuditOutbox(
        database,
        account_id="account-existing",
        cipher=cipher,
        initialize=False,
    )
    records = reader.list()

    assert len(records) == 1
    assert reader.get(records[0].audit_id) == records[0]
    assert records[0].payload == {"status": "completed", "summary": "encrypted"}
    assert reader.startup_converged is False
    assert _table_rows(database, prefix="observability_audit_") == before


def test_trace_projection_only_is_zero_write_then_converges_once(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "trace.db")
    before = _table_rows(database, prefix="observability_trace_")
    outbox = TraceOutbox(
        database,
        account_id="projection-account",
        cipher=AuditPayloadCipher(b"c" * 32),
        projector=object(),  # type: ignore[arg-type]
        initialize=False,
    )

    assert outbox.startup_converged is False
    assert outbox.list() == ()
    assert outbox.count() == 0
    with pytest.raises(TraceOutboxIntegrityError, match="has not converged"):
        outbox.backfill_events()
    with pytest.raises(TraceOutboxIntegrityError, match="has not converged"):
        outbox.materialize()
    with pytest.raises(TraceOutboxIntegrityError, match="has not converged"):
        outbox.enforce_retention()
    with pytest.raises(TraceOutboxIntegrityError, match="has not converged"):
        asyncio.run(TraceDispatcher(outbox).start())
    assert _table_rows(database, prefix="observability_trace_") == before

    outbox.converge_startup()
    assert outbox.startup_converged is True
    converged = _table_rows(database, prefix="observability_trace_")
    assert converged != before
    with database.reader() as connection:
        cursor = connection.execute(
            "SELECT * FROM observability_trace_cursors WHERE account_id = ?",
            ("projection-account",),
        ).fetchone()
    assert cursor is not None
    outbox.initialize()
    outbox.converge_startup()
    assert _table_rows(database, prefix="observability_trace_") == converged


def test_trace_projection_only_reads_existing_encrypted_batches(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "trace-records.db")
    cipher = AuditPayloadCipher(b"d" * 32)
    writer = TraceOutbox(
        database,
        account_id="account-existing",
        cipher=cipher,
        projector=object(),  # type: ignore[arg-type]
    )
    batch = TraceExportBatch(
        batch_id="tracebatch_existing",
        account_id="account-existing",
        thread_id="thread-existing",
        segment_kind="turn",
        segment_id="turn-existing",
        through_seq=8,
        event_digest="e" * 64,
        chunk_index=0,
        chunk_count=1,
        span_count=1,
        payload={
            "resourceSpans": [
                {"scopeSpans": [{"spans": [{"name": "encrypted-span"}]}]}
            ]
        },
        payload_sha256="unused-by-persist",
        attempts=0,
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
    )
    # _persist_batch trusts the caller's digest because production batches are
    # created by the bounded projector. Supply the canonical digest explicitly.
    encoded = json.dumps(
        batch.payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    batch = replace(batch, payload_sha256=hashlib.sha256(encoded).hexdigest())
    with database.transaction() as connection:
        assert writer._persist_batch(connection, batch)
    before = _table_rows(database, prefix="observability_trace_")

    reader = TraceOutbox(
        database,
        account_id="account-existing",
        cipher=cipher,
        projector=object(),  # type: ignore[arg-type]
        initialize=False,
    )
    batches = reader.list(thread_id="thread-existing")

    assert batches == (batch,)
    assert reader.get(batch.batch_id) == batch
    assert reader.startup_converged is False
    assert _table_rows(database, prefix="observability_trace_") == before


def test_trace_projection_only_rejects_plaintext_without_repair(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "trace-plaintext.db")
    plaintext = '{"resourceSpans":[]}'
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO observability_trace_outbox("
            "batch_id,account_id,thread_id,segment_kind,segment_id,through_seq,"
            "event_digest,chunk_index,chunk_count,span_count,payload_json,"
            "payload_format,payload_sha256,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "tracebatch_plaintext",
                "account-plaintext",
                "thread-plaintext",
                "turn",
                "turn-plaintext",
                1,
                "f" * 64,
                0,
                1,
                1,
                plaintext,
                "plaintext-v0",
                digest,
                "2026-07-12T00:00:00+00:00",
            ),
        )
    before = _table_rows(database, prefix="observability_trace_")

    with pytest.raises(
        TraceOutboxIntegrityError,
        match="signed encryption migration",
    ):
        TraceOutbox(
            database,
            account_id="account-plaintext",
            cipher=AuditPayloadCipher(b"e" * 32),
            projector=object(),  # type: ignore[arg-type]
            initialize=False,
        )

    assert _table_rows(database, prefix="observability_trace_") == before
