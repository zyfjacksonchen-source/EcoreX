from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from ecorex.bootstrap import preview_state


def _source(root: Path) -> None:
    state = root / "state"
    workspace = root / "workspace"
    (state / "artifacts").mkdir(parents=True)
    workspace.mkdir(parents=True)
    database = sqlite3.connect(state / "runtime.sqlite3")
    database.execute(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
    )
    database.execute("INSERT INTO messages(body) VALUES ('preserved')")
    database.execute("CREATE TABLE observability_audit_outbox(id INTEGER PRIMARY KEY)")
    database.execute("INSERT INTO observability_audit_outbox DEFAULT VALUES")
    database.execute(
        "CREATE TABLE managed_session_state("
        "singleton INTEGER PRIMARY KEY,generation INTEGER NOT NULL,"
        "active_intent_id TEXT,pending_intent_id TEXT)"
    )
    database.execute(
        "INSERT INTO managed_session_state VALUES(1,7,'active','pending')"
    )
    database.commit()
    database.close()
    (state / "artifacts" / "blob.bin").write_bytes(b"artifact")
    (workspace / "note.txt").write_text("source", encoding="utf-8")


def test_preview_checkpoint_is_independent_and_sqlite_consistent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "live"
    preview = source / "acceptance-preview" / "candidate"
    _source(source)
    (preview / "state").mkdir(parents=True)
    (preview / "state" / "old.txt").write_text("old", encoding="utf-8")

    receipt = preview_state.prepare_preview_state(source, preview)

    assert receipt["status"] == "ready"
    assert receipt["file_count"] == 3
    assert receipt["observability_rows_removed"] == {
        "observability_audit_outbox": 1
    }
    assert receipt["managed_session_cleared"] is True
    assert json.loads((preview / "acceptance-preview.json").read_text()) == receipt
    with sqlite3.connect(preview / "state" / "runtime.sqlite3") as database:
        assert database.execute("SELECT body FROM messages").fetchone() == (
            "preserved",
        )
        assert database.execute(
            "SELECT COUNT(*) FROM observability_audit_outbox"
        ).fetchone() == (0,)
        assert database.execute(
            "SELECT generation,active_intent_id,pending_intent_id "
            "FROM managed_session_state"
        ).fetchone() == (8, None, None)
    with sqlite3.connect(source / "state" / "runtime.sqlite3") as database:
        assert database.execute(
            "SELECT COUNT(*) FROM observability_audit_outbox"
        ).fetchone() == (1,)
        assert database.execute(
            "SELECT generation,active_intent_id,pending_intent_id "
            "FROM managed_session_state"
        ).fetchone() == (7, "active", "pending")
    source_note = source / "workspace" / "note.txt"
    preview_note = preview / "workspace" / "note.txt"
    assert source_note.stat().st_ino != preview_note.stat().st_ino
    preview_note.write_text("candidate", encoding="utf-8")
    assert source_note.read_text(encoding="utf-8") == "source"
    assert (preview / "state" / "old.txt").exists() is False


def test_preview_budget_failure_preserves_last_valid_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "live"
    preview = source / "acceptance-preview" / "candidate"
    _source(source)
    (preview / "state").mkdir(parents=True)
    (preview / "workspace").mkdir()
    (preview / "state" / "known-good.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(preview_state, "MAX_SNAPSHOT_BYTES", 1)

    with pytest.raises(preview_state.PreviewStateError, match="safety budget"):
        preview_state.prepare_preview_state(source, preview)

    assert (preview / "state" / "known-good.txt").read_text(encoding="utf-8") == "keep"


def test_preview_checkpoint_rejects_links(tmp_path: Path) -> None:
    source = tmp_path / "live"
    preview = source / "acceptance-preview" / "candidate"
    _source(source)
    (source / "state" / "linked").symlink_to(source / "workspace" / "note.txt")

    with pytest.raises(preview_state.PreviewStateError, match="links"):
        preview_state.prepare_preview_state(source, preview)


def test_preview_checkpoint_rejects_a_missing_source_root(tmp_path: Path) -> None:
    source = tmp_path / "missing"

    with pytest.raises(
        preview_state.PreviewStateError,
        match="source install root is unavailable",
    ):
        preview_state.prepare_preview_state(source, tmp_path / "preview")

    assert not source.exists()


def test_preview_checkpoint_rejects_a_root_inside_runtime_data(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "state").mkdir(parents=True)
    (source / "workspace").mkdir()

    with pytest.raises(preview_state.PreviewStateError, match="overlaps Runtime data"):
        preview_state.prepare_preview_state(source, source / "state" / "preview")
