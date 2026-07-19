from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from ecorex.observability.audit import AuditIntegrityError
from ecorex.observability.recovery import (
    is_unreadable_observability_error,
    quarantine_unreadable_observability,
)


def _count(path: Path, table: str) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        connection.close()


def test_quarantine_unreadable_observability_preserves_product_rows(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "runtime.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO threads(thread_id) VALUES ('thread-1')")
        for table in (
            "observability_audit_outbox",
            "observability_audit_daily",
            "observability_audit_cursors",
            "observability_trace_outbox",
            "observability_trace_segments",
            "observability_trace_cursors",
        ):
            connection.execute(f'CREATE TABLE "{table}" (value TEXT)')
            connection.execute(f'INSERT INTO "{table}"(value) VALUES ("x")')
        connection.commit()
    finally:
        connection.close()

    receipt = quarantine_unreadable_observability(database)

    assert receipt.backup_path.is_file()
    assert receipt.receipt_path.is_file()
    assert _count(database, "threads") == 1
    assert _count(receipt.backup_path, "threads") == 1
    assert _count(receipt.backup_path, "observability_audit_outbox") == 1
    for table, count in receipt.removed_rows.items():
        assert count == 1
        assert _count(database, table) == 0
    value = json.loads(receipt.receipt_path.read_text(encoding="utf-8"))
    assert value["reason"] == "audit_key_mismatch"
    assert value["integrity"] == "ok"
    assert "database" not in value
    assert "backup" not in value


def test_recovery_classifies_only_the_known_aes_authentication_failure() -> None:
    assert is_unreadable_observability_error(
        AuditIntegrityError("stored audit payload authentication failed")
    )
    assert not is_unreadable_observability_error(
        AuditIntegrityError("audit payload storage requires a signed encryption migration")
    )
    assert not is_unreadable_observability_error(RuntimeError("same text"))
