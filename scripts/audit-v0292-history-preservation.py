"""Read-only aggregate audit for a local v0.2.9.2 -> v1 history migration.

The report intentionally omits conversation identifiers, titles and message content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable

from ecorex.migration.legacy import read_conversations, snapshot_sqlite


def _digest(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{path.resolve(strict=True).as_posix()}?mode=ro&immutable=1",
        uri=True,
    )


def audit(
    *, legacy_database: Path, ui_state: Path, v1_database: Path
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ecorex-v0292-history-audit-") as temp:
        snapshot_root = Path(temp)
        legacy_snapshot = snapshot_root / "legacy.sqlite3"
        v1_snapshot = snapshot_root / "v1.sqlite3"
        snapshot_sqlite(
            legacy_database,
            legacy_snapshot,
            subject="legacy conversation database",
        )
        snapshot_sqlite(v1_database, v1_snapshot, subject="v1 runtime database")
        return _audit_snapshots(
            legacy_database=legacy_snapshot,
            ui_state=ui_state,
            v1_database=v1_snapshot,
        )


def _audit_snapshots(
    *, legacy_database: Path, ui_state: Path, v1_database: Path
) -> dict[str, object]:
    conversations = read_conversations(legacy_database)
    canonical_sessions = {str(row["session_id"]) for row in conversations.sessions}
    canonical_messages = {
        (str(row["session_id"]), str(int(row["seq"]))) for row in conversations.messages
    }

    raw_state = json.loads(ui_state.read_text(encoding="utf-8"))
    session_titles = raw_state.get("sessionTitles")
    session_ui_state = raw_state.get("sessionUiState")
    cached_sessions = {
        *(
            str(value)
            for value in (session_titles if isinstance(session_titles, dict) else {})
        ),
        *(
            str(value)
            for value in (
                session_ui_state if isinstance(session_ui_state, dict) else {}
            )
        ),
    }
    deleted_cache_only = cached_sessions - canonical_sessions

    connection = _readonly(v1_database)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        mapped_sessions = {
            str(row[0])
            for row in connection.execute(
                "SELECT legacy_id FROM legacy_id_map WHERE entity_kind = 'session'"
            )
        }
        mapped_messages = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                """
                SELECT legacy_parent_id, legacy_id
                FROM legacy_id_map
                WHERE entity_kind = 'message'
                """
            )
        }
        mapped_session_targets_missing = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM legacy_id_map AS legacy
                LEFT JOIN threads AS target ON target.thread_id = legacy.target_id
                WHERE legacy.entity_kind = 'session' AND target.thread_id IS NULL
                """
            ).fetchone()[0]
        )
        mapped_message_targets_missing = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM legacy_id_map AS legacy
                LEFT JOIN items AS target ON target.item_id = legacy.target_id
                WHERE legacy.entity_kind = 'message' AND target.item_id IS NULL
                """
            ).fetchone()[0]
        )
        v1_threads = int(
            connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        )
        v1_messages = int(
            connection.execute(
                "SELECT COUNT(*) FROM items WHERE kind = 'message'"
            ).fetchone()[0]
        )
        v1_skill_states = int(
            connection.execute("SELECT COUNT(*) FROM skill_states").fetchone()[0]
        )
        v1_connectors = int(
            connection.execute("SELECT COUNT(*) FROM connector_instances").fetchone()[0]
        )
        v1_artifacts = int(
            connection.execute("SELECT COUNT(*) FROM artifact_entities").fetchone()[0]
        )
    finally:
        connection.close()

    missing_sessions = canonical_sessions - mapped_sessions
    missing_messages = canonical_messages - mapped_messages
    restored_deleted = deleted_cache_only & mapped_sessions
    passed = (
        integrity == "ok"
        and not missing_sessions
        and not missing_messages
        and not restored_deleted
        and mapped_session_targets_missing == 0
        and mapped_message_targets_missing == 0
    )
    return {
        "status": "passed" if passed else "failed",
        "aggregate_only": True,
        "database_integrity": integrity,
        "legacy": {
            "sessions": len(canonical_sessions),
            "messages": len(canonical_messages),
            "canonical_session_set_sha256": _digest(canonical_sessions),
            "canonical_message_set_sha256": _digest(
                f"{session_id}:{sequence}"
                for session_id, sequence in canonical_messages
            ),
            "deleted_cache_only_sessions": len(deleted_cache_only),
        },
        "v1": {
            "threads": v1_threads,
            "messages": v1_messages,
            "skill_states": v1_skill_states,
            "connectors": v1_connectors,
            "artifacts": v1_artifacts,
            "mapped_sessions": len(mapped_sessions),
            "mapped_messages": len(mapped_messages),
            "missing_canonical_sessions": len(missing_sessions),
            "missing_canonical_messages": len(missing_messages),
            "restored_deleted_sessions": len(restored_deleted),
            "mapped_session_targets_missing": mapped_session_targets_missing,
            "mapped_message_targets_missing": mapped_message_targets_missing,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-database", type=Path, required=True)
    parser.add_argument("--ui-state", type=Path, required=True)
    parser.add_argument("--v1-database", type=Path, required=True)
    args = parser.parse_args()
    report = audit(
        legacy_database=args.legacy_database,
        ui_state=args.ui_state,
        v1_database=args.v1_database,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
