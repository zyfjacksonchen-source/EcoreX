from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading

import pytest

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecorex.observability.recovery as recovery_module

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

    fsynced: list[Path] = []
    original_atomic_json = recovery_module._atomic_json
    pending_checked = False

    def record_fsync(path: Path) -> None:
        fsynced.append(path)

    def assert_directory_fence(path: Path, value: object) -> None:
        nonlocal pending_checked
        if not pending_checked:
            assert fsynced == [state, state / "observability-quarantine"]
        original_atomic_json(path, value)
        if not pending_checked:
            assert fsynced[-1] == path.parent
            pending_checked = True

    monkeypatch.setattr(recovery_module, "_fsync_directory", record_fsync)
    monkeypatch.setattr(recovery_module, "_atomic_json", assert_directory_fence)
    receipt = quarantine_unreadable_observability(database)

    assert pending_checked
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
    assert value["state"] == "completed"
    assert value["live_cleanup_committed"] is True
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


def test_recovery_persists_pending_receipt_before_destructive_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecorex.observability.recovery as recovery_module

    state = tmp_path / "state"
    state.mkdir()
    database = state / "runtime.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO threads(thread_id) VALUES ('thread-1')")
        connection.execute("CREATE TABLE observability_audit_outbox (value TEXT)")
        connection.execute(
            "INSERT INTO observability_audit_outbox(value) VALUES ('encrypted')"
        )
        connection.commit()
    finally:
        connection.close()

    original = recovery_module._atomic_json
    calls = 0

    def fail_completed_receipt(path: Path, value: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected completed receipt failure")
        original(path, value)

    monkeypatch.setattr(recovery_module, "_atomic_json", fail_completed_receipt)
    with pytest.raises(OSError, match="completed receipt failure"):
        quarantine_unreadable_observability(database)

    receipts = list((state / "observability-quarantine").glob("*/recovery-receipt.json"))
    assert len(receipts) == 1
    value = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert value["state"] == "pending"
    assert value["live_cleanup_committed"] is None
    assert _count(database, "threads") == 1
    assert _count(database, "observability_audit_outbox") == 0
    backup = receipts[0].with_name("runtime-before-observability-recovery.sqlite3")
    assert _count(backup, "observability_audit_outbox") == 1


def test_recovery_backup_and_cleanup_share_one_writer_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecorex.observability.recovery as recovery_module

    state = tmp_path / "state"
    state.mkdir()
    database = state / "runtime.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO threads(thread_id) VALUES ('thread-1')")
        connection.execute("CREATE TABLE observability_audit_outbox (value TEXT)")
        connection.execute(
            "INSERT INTO observability_audit_outbox(value) VALUES ('before-backup')"
        )
        connection.commit()
    finally:
        connection.close()

    original = recovery_module._backup_database
    writer_started = threading.Event()
    writer_done = threading.Event()
    writer_errors: list[BaseException] = []

    def write_during_backup() -> None:
        writer = sqlite3.connect(database, timeout=5)
        try:
            writer_started.set()
            writer.execute(
                "INSERT INTO observability_audit_outbox(value) VALUES ('after-recovery')"
            )
            writer.commit()
        except BaseException as error:
            writer_errors.append(error)
        finally:
            writer.close()
            writer_done.set()

    thread: threading.Thread | None = None

    def backup_with_competing_writer(source: Path, destination: Path) -> None:
        nonlocal thread
        thread = threading.Thread(target=write_during_backup)
        thread.start()
        assert writer_started.wait(1)
        assert not writer_done.wait(0.1)
        original(source, destination)
        assert not writer_done.is_set()

    monkeypatch.setattr(recovery_module, "_backup_database", backup_with_competing_writer)
    receipt = quarantine_unreadable_observability(database)
    assert thread is not None
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert writer_errors == []
    assert receipt.removed_rows["observability_audit_outbox"] == 1
    assert _count(receipt.backup_path, "observability_audit_outbox") == 1
    assert _count(database, "observability_audit_outbox") == 1
