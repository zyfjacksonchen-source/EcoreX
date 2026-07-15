from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from ecorex.artifacts import ArtifactService
from ecorex.protocol import CreateTurnRequest
from ecorex.migration import (
    DuplicateLegacyIdError,
    LegacyDatabaseError,
    LegacySchemaError,
    MigrationOptions,
    QuarantineKeyRequired,
    QUARANTINE_NAME,
    SourceLayoutError,
    SourceChangedError,
    TARGET_ARTIFACT_ROOT_NAME,
    TARGET_DATABASE_NAME,
    TargetConflictError,
    V030ToV1Migrator,
    decrypt_quarantine,
    inventory_source,
    migrate_v030_to_v1,
)
from ecorex.runtime.schema_fragments.memory import MEMORY_SCHEMA_FRAGMENT
from ecorex.runtime import intent_fingerprint
from ecorex.runtime.database import SCHEMA_VERSION
from ecorex.migration.schema import IMPORT_LAYOUT_VERSION


QUARANTINE_KEY = b"q" * 32
FAKE_PROVIDER_KEY = "fake-provider-key-for-migration-test"
FAKE_FEISHU_SECRET = "fake-feishu-secret-for-migration-test"
FAKE_TENCENT_TOKEN = "Bearer fake-tencent-token-for-migration-test"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _create_conversation_database(
    root: Path,
    *,
    optional_columns: bool = True,
    missing_role: bool = False,
    duplicate_sessions: bool = False,
    traversal_artifact: str | None = None,
) -> Path:
    path = root / "sessions" / "conversations.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    if optional_columns:
        session_pk = "" if duplicate_sessions else " PRIMARY KEY"
        connection.executescript(
            f"""
            CREATE TABLE sessions (
                session_id TEXT{session_pk},
                channel_type TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                title_locked INTEGER NOT NULL DEFAULT 0,
                context_start_seq INTEGER NOT NULL DEFAULT 0,
                project_id TEXT NOT NULL DEFAULT '',
                project_name TEXT NOT NULL DEFAULT '',
                project_path TEXT NOT NULL DEFAULT '',
                project_memory_path TEXT NOT NULL DEFAULT '',
                project_dreams_path TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                last_active INTEGER NOT NULL,
                msg_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
    else:
        connection.execute(
            "CREATE TABLE sessions(session_id TEXT PRIMARY KEY, created_at INTEGER, last_active INTEGER)"
        )
    if missing_role:
        connection.execute(
            "CREATE TABLE messages(session_id TEXT, seq INTEGER, content TEXT, created_at INTEGER)"
        )
    elif optional_columns:
        connection.execute(
            """
            CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                extras TEXT NOT NULL DEFAULT '',
                UNIQUE(session_id, seq)
            )
            """
        )
    else:
        connection.execute(
            "CREATE TABLE messages(session_id TEXT, seq INTEGER, role TEXT, content TEXT, created_at INTEGER)"
        )

    if optional_columns:
        session_row = (
            "legacy-session",
            "web",
            "制定 v1.0 产品化方案",
            1,
            0,
            "project-alpha",
            "Alpha",
            str(root / "projects" / "alpha"),
            str(root / "projects" / "alpha" / ".ecorex" / "project-memory.md"),
            str(root / "projects" / "alpha" / ".ecorex" / "dreams"),
            json.dumps({"theme": "dark"}),
            1_700_000_000,
            1_700_000_100,
            2,
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            session_row,
        )
        if duplicate_sessions:
            conflicting = list(session_row)
            conflicting[2] = "Conflicting title"
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                conflicting,
            )
    else:
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?)",
            ("legacy-session", 1_700_000_000, 1_700_000_100),
        )

    if not missing_role:
        user_content = json.dumps(
            [{"type": "text", "text": "请制定产品化方案"}], ensure_ascii=False
        )
        artifacts = [
            {
                "path": str(root / "outputs" / "plan.pdf"),
                "title": "plan.pdf",
                "kind": "file",
                "intent": "deliverable",
                "operation": "exported",
                "status": "ready",
                "mimeType": "application/pdf",
            },
            {
                "path": str(root / "outputs" / "data.csv"),
                "title": "data.csv",
                "kind": "file",
                "intent": "deliverable",
                "operation": "created",
                "status": "ready",
            },
            {
                "path": str(root / "outputs" / "worker.py"),
                "title": "worker.py",
                "kind": "file",
                "intent": "deliverable",
                "operation": "created",
                "status": "ready",
            },
            {
                "path": str(root / "outputs" / "worker.log"),
                "title": "worker.log",
                "kind": "file",
                "intent": "deliverable",
                "operation": "created",
                "status": "ready",
            },
        ]
        if traversal_artifact:
            artifacts.append(
                {
                    "path": traversal_artifact,
                    "title": "outside.pdf",
                    "kind": "file",
                    "intent": "deliverable",
                    "operation": "created",
                    "status": "ready",
                }
            )
        assistant_extras = json.dumps({"artifacts": artifacts}, ensure_ascii=False)
        rows = [
            ("legacy-session", 0, "user", user_content, 1_700_000_001, ""),
            (
                "legacy-session",
                1,
                "assistant",
                json.dumps("方案已完成", ensure_ascii=False),
                1_700_000_002,
                assistant_extras,
            ),
        ]
        if optional_columns:
            connection.executemany(
                "INSERT INTO messages(session_id, seq, role, content, created_at, extras) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        else:
            connection.executemany(
                "INSERT INTO messages(session_id, seq, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                [row[:5] for row in rows],
            )
    connection.commit()
    connection.close()
    return path


def _create_memory_database(root: Path) -> Path:
    memory_file = root / "memory" / "MEMORY.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# Canonical memory\n用户偏好中文。\n", encoding="utf-8")
    path = root / "memory" / "long-term" / "index.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE chunks(
            id TEXT PRIMARY KEY,
            user_id TEXT,
            scope TEXT NOT NULL DEFAULT 'shared',
            source TEXT NOT NULL DEFAULT 'memory',
            path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding TEXT,
            hash TEXT NOT NULL,
            metadata TEXT,
            created_at INTEGER,
            updated_at INTEGER
        );
        CREATE TABLE files(
            path TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT 'memory',
            hash TEXT NOT NULL,
            mtime INTEGER NOT NULL,
            size INTEGER NOT NULL,
            updated_at INTEGER
        );
        """
    )
    connection.execute(
        "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "memory-chunk-1",
            "local-user",
            "shared",
            "memory",
            "MEMORY.md",
            1,
            2,
            "用户偏好中文。",
            json.dumps([0.1, 0.2]),
            "legacy-memory-hash",
            json.dumps({"category": "preference"}),
            1_700_000_000,
            1_700_000_100,
        ),
    )
    connection.execute(
        "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)",
        (
            "MEMORY.md",
            "memory",
            "legacy-file-hash",
            1_700_000_000,
            memory_file.stat().st_size,
            1_700_000_100,
        ),
    )
    connection.commit()
    connection.close()
    return path


def _create_legacy_fixture(
    root: Path,
    *,
    optional_columns: bool = True,
    missing_role: bool = False,
    duplicate_sessions: bool = False,
    traversal_artifact: str | None = None,
) -> None:
    (root / "outputs").mkdir(parents=True)
    (root / "outputs" / "plan.pdf").write_bytes(b"%PDF-1.7\nlegacy office plan\n%%EOF")
    (root / "outputs" / "data.csv").write_text("name,value\nalpha,1\n", encoding="utf-8")
    (root / "outputs" / "worker.py").write_text("print('internal')\n", encoding="utf-8")
    (root / "outputs" / "worker.log").write_text("internal diagnostic\n", encoding="utf-8")
    _create_conversation_database(
        root,
        optional_columns=optional_columns,
        missing_role=missing_role,
        duplicate_sessions=duplicate_sessions,
        traversal_artifact=traversal_artifact,
    )
    _create_memory_database(root)
    _write_json(
        root / "config.json",
        {
            "channel_type": "feishu,telegram",
            "provider_api_key": FAKE_PROVIDER_KEY,
            "feishu_app_id": "cli_fake_app_id",
            "feishu_app_secret": FAKE_FEISHU_SECRET,
            "telegram_token": "fake-telegram-token-for-migration-test",
        },
    )
    _write_json(
        root / "mcp.json",
        {
            "mcpServers": {
                "tencent-docs": {
                    "type": "streamable-http",
                    "url": "https://docs.qq.com/openapi/mcp?should_be_removed=1",
                    "headers": {"Authorization": FAKE_TENCENT_TOKEN},
                }
            }
        },
    )
    _write_json(
        root / ".ecorex" / "ui-state.json",
        {
            "activeProjectId": "project-alpha",
            "projects": [
                {
                    "id": "project-alpha",
                    "name": "Alpha",
                    "path": str(root / "projects" / "alpha"),
                }
            ],
            "sessionProjects": {"legacy-session": "project-alpha"},
            "pinnedProjects": {"project-alpha": True},
        },
    )
    _write_json(
        root / "skills" / "skills_config.json",
        {
            "office-report": {
                "name": "office-report",
                "description": "Office reporting",
                "source": "custom",
                "enabled": False,
                "default_enabled": True,
                "category": "office",
            }
        },
    )


def _add_released_runtime_state(root: Path) -> None:
    conversations = sqlite3.connect(root / "sessions" / "conversations.db")
    conversations.execute(
        "UPDATE messages SET extras = ? WHERE session_id = 'legacy-session' AND seq = 0",
        (json.dumps({"request_id": "parent-run", "turn_id": "parent-run"}),),
    )
    conversations.execute(
        """
        INSERT INTO sessions(
            session_id, channel_type, title, title_locked, context_start_seq,
            project_id, project_name, project_path, project_memory_path,
            project_dreams_path, metadata_json, created_at, last_active, msg_count
        ) VALUES (?, 'web', '分支会话', 0, 0, '', '', '', '', '', '', ?, ?, 2)
        """,
        ("branch-session", 1_700_000_200, 1_700_000_230),
    )
    conversations.executemany(
        """
        INSERT INTO messages(session_id, seq, role, content, created_at, extras)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "branch-session",
                0,
                "user",
                json.dumps([{"type": "text", "text": "继续生成分支报告"}], ensure_ascii=False),
                1_700_000_201,
                json.dumps({"request_id": "child-run", "turn_id": "child-run"}),
            ),
            (
                "branch-session",
                1,
                "assistant",
                json.dumps("尚未完成", ensure_ascii=False),
                1_700_000_202,
                "",
            ),
        ],
    )
    conversations.commit()
    conversations.close()

    runtime = sqlite3.connect(root / "memory" / "long-term" / "index.db")
    runtime.executescript(
        """
        CREATE TABLE agent_runs (
            request_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            parent_id TEXT,
            run_type TEXT NOT NULL DEFAULT 'message',
            status TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT '',
            terminal_reason TEXT,
            error_code TEXT,
            error_message TEXT,
            model TEXT,
            provider TEXT,
            created_at REAL NOT NULL,
            started_at REAL,
            updated_at REAL NOT NULL,
            terminal_at REAL,
            lease_owner TEXT,
            lease_expires_at REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE agent_run_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            turn_id TEXT NOT NULL DEFAULT '',
            event_seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'runtime',
            created_at REAL NOT NULL,
            UNIQUE(idempotency_key),
            UNIQUE(request_id, event_seq)
        );
        """
    )
    runtime.executemany(
        """
        INSERT INTO agent_runs(
            request_id, session_id, parent_id, run_type, status, phase,
            terminal_reason, error_code, error_message, model, provider,
            created_at, started_at, updated_at, terminal_at, lease_owner,
            lease_expires_at, metadata_json
        ) VALUES (?, ?, NULL, 'message', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        """,
        [
            (
                "parent-run",
                "legacy-session",
                "completed",
                "finalizing",
                "completed",
                None,
                "managed-chat",
                "managed",
                1_700_000_001,
                1_700_000_001,
                1_700_000_010,
                1_700_000_010,
                json.dumps({"interrupt_mode": "replace"}),
            ),
            (
                "child-run",
                "branch-session",
                "running",
                "streaming",
                None,
                None,
                "managed-chat",
                "managed",
                1_700_000_201,
                1_700_000_201,
                1_700_000_220,
                None,
                json.dumps(
                    {
                        "interrupt_mode": "branch",
                        "interrupts_request_id": "parent-run",
                        "visible_message": "继续生成分支报告",
                    },
                    ensure_ascii=False,
                ),
            ),
        ],
    )
    runtime.executemany(
        """
        INSERT INTO agent_run_events(
            request_id, session_id, turn_id, event_seq, event_type,
            payload_json, idempotency_key, source, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'runtime', ?)
        """,
        [
            (
                "parent-run",
                "legacy-session",
                "parent-run",
                1,
                "run.accepted",
                json.dumps({"content": "Authorization: Bearer secret-run-token"}),
                "parent-run:accepted",
                1_700_000_001,
            ),
            (
                "child-run",
                "branch-session",
                "child-run",
                1,
                "run.accepted",
                json.dumps({"content": "继续生成分支报告"}, ensure_ascii=False),
                "child-run:accepted",
                1_700_000_201,
            ),
        ],
    )
    runtime.commit()
    runtime.close()

    _write_json(
        root / ".ecorex" / "queued-requests" / "child-run.json",
        {
            "schemaVersion": 1,
            "request_id": "child-run",
            "session_id": "branch-session",
            "created_at": 1_700_000_201,
            "payload": {
                "request_id": "child-run",
                "session_id": "branch-session",
                "visible_message": "继续生成分支报告",
                "hidden_context": "Bearer hidden-context-must-not-migrate",
                "attachments": [
                    {"file_name": "输入.docx", "file_path": str(root / "inputs" / "输入.docx")}
                ],
            },
        },
    )
    _write_json(
        root / "scheduler" / "tasks.json",
        {
            "version": 1,
            "updated_at": "2026-07-08T00:00:00",
            "tasks": {
                "daily-brief": {
                    "id": "daily-brief",
                    "name": "每日简报",
                    "enabled": True,
                    "created_at": "2026-07-08T00:00:00",
                    "updated_at": "2026-07-08T00:00:00",
                    "schedule": {"type": "cron", "expression": "0 9 * * *"},
                    "action": {
                        "type": "agent_task",
                        "task_description": "生成每日简报",
                        "channel_type": "web",
                        "receiver": "branch-session",
                        "api_key": "scheduler-secret-must-not-migrate",
                    },
                    "next_run_at": "2026-07-12T09:00:00",
                }
            },
        },
    )

    ui_state_path = root / ".ecorex" / "ui-state.json"
    ui_state = json.loads(ui_state_path.read_text(encoding="utf-8"))
    ui_state["sessionTitles"] = {"cached-session": "仅在 Web 缓存中的会话"}
    ui_state["sessionUiState"] = {
        "cached-session": {
            "title": "仅在 Web 缓存中的会话",
            "messages": [
                {
                    "id": "u-cache",
                    "role": "user",
                    "content": "恢复这条消息",
                    "requestId": "cached-run",
                    "createdAt": "2026-07-08T01:00:00Z",
                },
                {
                    "id": "a-cache",
                    "role": "assistant",
                    "content": "已恢复",
                    "requestId": "cached-run",
                    "createdAt": "2026-07-08T01:00:01Z",
                },
            ],
        }
    }
    _write_json(ui_state_path, ui_state)


def _query_one(database: Path, statement: str):
    connection = sqlite3.connect(database)
    try:
        return connection.execute(statement).fetchone()[0]
    finally:
        connection.close()


def _query_all(database: Path, statement: str):
    connection = sqlite3.connect(database)
    try:
        return connection.execute(statement).fetchall()
    finally:
        connection.close()


def _assert_imported_turn_input_contract(database: Path) -> list[tuple[str, str]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT turn.turn_id,
                   turn.input_text AS turn_input_text,
                   turn.agent_model_id AS turn_agent_model_id,
                   turn.image_model_id AS turn_image_model_id,
                   turn.client_message_id AS turn_client_message_id,
                   turn.metadata_json AS turn_metadata_json,
                   turn.created_at AS turn_created_at,
                   revision.revision_id,
                   revision.ordinal,
                   revision.source,
                   revision.input_text AS revision_input_text,
                   revision.agent_model_id AS revision_agent_model_id,
                   revision.image_model_id AS revision_image_model_id,
                   revision.client_message_id AS revision_client_message_id,
                   revision.explicit_tool_ids_json,
                   revision.metadata_json AS revision_metadata_json,
                   revision.intent_fingerprint,
                   revision.created_at AS revision_created_at,
                   accepted.payload_json,
                   accepted.config_snapshot_id,
                   accepted.capability_snapshot_id,
                   accepted.permission_snapshot_id,
                   accepted.extension_snapshot_id
            FROM legacy_id_map AS legacy
            JOIN turns AS turn ON turn.turn_id = legacy.target_id
            JOIN turn_input_revisions AS revision
              ON revision.turn_id = turn.turn_id AND revision.ordinal = 0
            JOIN events AS accepted
              ON accepted.turn_id = turn.turn_id
             AND accepted.event_type = 'turn.accepted'
            WHERE legacy.entity_kind = 'turn'
            ORDER BY turn.turn_id
            """
        ).fetchall()
        assert len(rows) == connection.execute(
            "SELECT COUNT(*) FROM legacy_id_map WHERE entity_kind = 'turn'"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_execution_batches"
        ).fetchone()[0] == 0
        identities: list[tuple[str, str]] = []
        for row in rows:
            metadata = json.loads(row["turn_metadata_json"])
            explicit_tool_ids = json.loads(row["explicit_tool_ids_json"])
            request = CreateTurnRequest(
                input=row["turn_input_text"],
                agent_model_id=row["turn_agent_model_id"],
                image_model_id=row["turn_image_model_id"],
                explicit_tool_ids=explicit_tool_ids,
                client_message_id=row["turn_client_message_id"],
                metadata=metadata,
            )
            assert row["source"] == "initial"
            assert row["ordinal"] == 0
            assert explicit_tool_ids == []
            assert row["revision_input_text"] == request.input
            assert row["revision_agent_model_id"] == request.agent_model_id
            assert row["revision_image_model_id"] == request.image_model_id
            assert row["revision_client_message_id"] == request.client_message_id
            assert json.loads(row["revision_metadata_json"]) == request.metadata
            assert row["intent_fingerprint"] == intent_fingerprint(request)
            assert row["revision_created_at"] == row["turn_created_at"]
            payload = json.loads(row["payload_json"])
            assert payload == {
                "agent_model_id": request.agent_model_id,
                "explicit_tool_ids": [],
                "image_model_id": request.image_model_id,
                "input": request.input,
                "metadata": request.metadata,
                "model_catalog_snapshot_id": None,
            }
            assert all(
                row[name] is None
                for name in (
                    "config_snapshot_id",
                    "capability_snapshot_id",
                    "permission_snapshot_id",
                    "extension_snapshot_id",
                )
            )
            identities.append((row["revision_id"], row["intent_fingerprint"]))
        return identities
    finally:
        connection.close()


def test_copy_on_write_imports_canonical_domains_and_is_idempotent(tmp_path):
    source = tmp_path / "legacy"
    target = tmp_path / "v1"
    source.mkdir()
    _create_legacy_fixture(source)
    before = inventory_source(source)

    report = migrate_v030_to_v1(
        source, target, quarantine_key=QUARANTINE_KEY, sample_size=2
    )

    assert report.status == "completed"
    assert target.is_dir()
    assert inventory_source(source) == before
    database = target / TARGET_DATABASE_NAME
    assert _query_one(database, "SELECT COUNT(*) FROM threads") == 1
    assert _query_one(database, "SELECT COUNT(*) FROM turns") == 1
    assert report.counts["turn_input_revisions"] == report.counts["turns"] == 1
    input_identities = _assert_imported_turn_input_contract(database)
    assert _query_one(database, "SELECT COUNT(*) FROM items WHERE kind = 'message'") == 2
    assert _query_one(database, "SELECT COUNT(*) FROM projects") == 1
    assert _query_one(database, "SELECT COUNT(*) FROM project_thread_bindings") == 1
    assert _query_one(database, "SELECT COUNT(*) FROM memory_canonical_records") == 1
    assert _query_one(database, "SELECT COUNT(*) FROM memory_files WHERE availability = 'stored'") == 1
    assert _query_all(
        database,
        "SELECT memory_origin, memory_state FROM memory_canonical_records",
    ) == [("imported", "active")]
    assert _query_all(
        database,
        "SELECT memory_origin, memory_state FROM memory_files",
    ) == [("imported", "active")]
    memory_names = ",".join(
        "'" + name + "'" for name in MEMORY_SCHEMA_FRAGMENT.object_names
    )
    assert {
        str(row[0])
        for row in _query_all(
            database,
            "SELECT name FROM sqlite_schema WHERE name IN (" + memory_names + ")",
        )
    } == set(MEMORY_SCHEMA_FRAGMENT.object_names)
    assert _query_one(database, "SELECT COUNT(*) FROM connector_instances") == 3
    assert _query_one(database, "SELECT COUNT(*) FROM skill_states WHERE enabled = 0") == 1
    assert _query_one(database, "SELECT COUNT(*) FROM jobs") == 0

    service = ArtifactService(target / TARGET_ARTIFACT_ROOT_NAME, database_path=database)
    artifacts = service.list_user_artifacts()
    assert {Path(item.display_name).suffix for item in artifacts} == {".pdf", ".csv"}
    assert all(item.sha256 == service.blobs.digest(service.read_user_content(item.artifact_id)) for item in artifacts)
    assert report.counts["artifacts_excluded_internal"] == 2
    assert len(report.sampled_artifact_ids) == 2

    database_bytes = database.read_bytes()
    assert FAKE_PROVIDER_KEY.encode() not in database_bytes
    assert FAKE_FEISHU_SECRET.encode() not in database_bytes
    quarantine = decrypt_quarantine(target / QUARANTINE_NAME, key=QUARANTINE_KEY)
    secret_paths = {item["key_path"] for item in quarantine["entries"]}
    assert "provider_api_key" in secret_paths
    assert "feishu_app_secret" in secret_paths
    assert "mcpServers.tencent-docs.headers.Authorization" in secret_paths

    replay = migrate_v030_to_v1(source, target, quarantine_key=QUARANTINE_KEY)
    assert replay.idempotent_replay is True
    assert replay.migration_id == report.migration_id
    assert replay.counts["turn_input_revisions"] == 1
    assert _assert_imported_turn_input_contract(database) == input_identities
    assert inventory_source(source) == before


def test_real_shared_memory_and_conversation_database_is_snapshotted_once(tmp_path):
    source = tmp_path / "legacy"
    target = tmp_path / "v1"
    source.mkdir()
    _create_legacy_fixture(source)
    conversation_path = source / "sessions" / "conversations.db"
    shared_path = source / "memory" / "long-term" / "index.db"
    connection = sqlite3.connect(shared_path)
    connection.execute("ATTACH DATABASE ? AS conversations", (str(conversation_path),))
    connection.execute("CREATE TABLE sessions AS SELECT * FROM conversations.sessions")
    connection.execute("CREATE TABLE messages AS SELECT * FROM conversations.messages")
    connection.commit()
    connection.close()
    conversation_path.unlink()

    report = migrate_v030_to_v1(source, target, quarantine_key=QUARANTINE_KEY)

    assert report.counts["threads"] == 1
    assert report.counts["messages"] == 2
    assert report.counts["memory_records"] == 1
    assert len(report.backups) == 1
    assert report.backups[0].source_relative_path == "memory/long-term/index.db"


def test_legacy_image_alias_is_quarantined_into_canonical_model_slots(tmp_path):
    source = tmp_path / "legacy-image-model"
    target = tmp_path / "v1-image-model"
    source.mkdir()
    _create_legacy_fixture(source)
    _add_released_runtime_state(source)
    runtime = sqlite3.connect(source / "memory" / "long-term" / "index.db")
    runtime.execute(
        "UPDATE agent_runs SET model = 'image2' WHERE request_id = 'parent-run'"
    )
    runtime.commit()
    runtime.close()

    report = migrate_v030_to_v1(source, target, quarantine_key=QUARANTINE_KEY)

    database = target / TARGET_DATABASE_NAME
    assert report.counts["turn_input_revisions"] == report.counts["turns"]
    assert report.counts["turns"] >= 2
    row = _query_all(
        database,
        """
        SELECT turn.agent_model_id, turn.image_model_id,
               revision.agent_model_id, revision.image_model_id,
               turn.metadata_json
        FROM legacy_id_map AS legacy
        JOIN turns AS turn ON turn.turn_id = legacy.target_id
        JOIN turn_input_revisions AS revision
          ON revision.turn_id = turn.turn_id AND revision.ordinal = 0
        WHERE legacy.entity_kind = 'turn'
          AND legacy.legacy_parent_id = 'legacy-session'
          AND legacy.legacy_id = '0'
        """,
    )[0]
    assert row[:4] == ("ecorex-chat", "gpt-image-2", "ecorex-chat", "gpt-image-2")
    assert json.loads(row[4])["migration"]["legacy_model"] == "image2"
    _assert_imported_turn_input_contract(database)


def test_assistant_only_legacy_history_gets_explicit_nonempty_input_recovery(tmp_path):
    source = tmp_path / "legacy-assistant-only"
    target = tmp_path / "v1-assistant-only"
    source.mkdir()
    _create_legacy_fixture(source)
    conversations = sqlite3.connect(source / "sessions" / "conversations.db")
    conversations.execute(
        "DELETE FROM messages WHERE session_id = 'legacy-session' AND role = 'user'"
    )
    conversations.commit()
    conversations.close()

    migrate_v030_to_v1(source, target, quarantine_key=QUARANTINE_KEY)

    database = target / TARGET_DATABASE_NAME
    row = _query_all(
        database,
        "SELECT input_text, metadata_json FROM turns",
    )[0]
    assert row[0] == "（从 v0.3 导入：原始用户指令不可用）"
    assert json.loads(row[1])["migration"]["input_recovery"] == (
        "missing_or_empty_user_message"
    )
    _assert_imported_turn_input_contract(database)


def test_live_wal_is_copied_into_staging_without_touching_source_sidecars(tmp_path):
    source = tmp_path / "legacy"
    target = tmp_path / "v1"
    source.mkdir()
    _create_legacy_fixture(source)
    conversation_path = source / "sessions" / "conversations.db"
    connection = sqlite3.connect(conversation_path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute(
        """
        INSERT INTO sessions(
            session_id, channel_type, title, title_locked, context_start_seq,
            project_id, project_name, project_path, project_memory_path,
            project_dreams_path, metadata_json, created_at, last_active, msg_count
        ) VALUES ('wal-session', 'web', 'WAL session', 0, 0, '', '', '', '', '', '',
                  1700000300, 1700000300, 1)
        """
    )
    connection.execute(
        "INSERT INTO messages(session_id, seq, role, content, created_at, extras) "
        "VALUES ('wal-session', 0, 'user', 'from wal', 1700000300, '')"
    )
    connection.commit()
    wal_path = Path(str(conversation_path) + "-wal")
    shm_path = Path(str(conversation_path) + "-shm")
    assert wal_path.is_file()
    before = inventory_source(source)
    sidecars_before = {
        item.relative_path: (item.size_bytes, item.mtime_ns, item.sha256)
        for item in before.entries
        if item.relative_path
        in {
            "sessions/conversations.db",
            "sessions/conversations.db-wal",
            "sessions/conversations.db-shm",
        }
    }
    try:
        report = migrate_v030_to_v1(source, target, quarantine_key=QUARANTINE_KEY)
        after = inventory_source(source)
        sidecars_after = {
            item.relative_path: (item.size_bytes, item.mtime_ns, item.sha256)
            for item in after.entries
            if item.relative_path in sidecars_before
        }
        assert sidecars_after == sidecars_before
    finally:
        connection.close()

    assert report.counts["threads"] == 2
    assert _query_one(
        target / TARGET_DATABASE_NAME,
        "SELECT COUNT(*) FROM threads WHERE title = 'WAL session'",
    ) == 1
    assert not list(target.rglob("*.source-copy*"))


def test_external_install_config_is_pinned_hashed_and_quarantined(tmp_path):
    source = tmp_path / "legacy-workspace"
    install = tmp_path / "legacy-install"
    target = tmp_path / "v1"
    source.mkdir()
    install.mkdir()
    _create_legacy_fixture(source)
    config = install / "config.json"
    mcp = install / "mcp.json"
    (source / "config.json").replace(config)
    (source / "mcp.json").replace(mcp)
    config_before = config.read_bytes()
    mcp_before = mcp.read_bytes()
    workspace_entries = len(inventory_source(source).entries)

    report = migrate_v030_to_v1(
        source,
        target,
        quarantine_key=QUARANTINE_KEY,
        config_file=config,
        mcp_file=mcp,
    )

    assert report.counts["source_inventory_entries"] == workspace_entries + 2
    quarantine = decrypt_quarantine(target / QUARANTINE_NAME, key=QUARANTINE_KEY)
    assert {item["source_relative_path"] for item in quarantine["entries"]} == {
        "@pinned/config",
        "@pinned/mcp",
    }
    assert config.read_bytes() == config_before
    assert mcp.read_bytes() == mcp_before

    replay = migrate_v030_to_v1(
        source,
        target,
        quarantine_key=QUARANTINE_KEY,
        config_file=config,
        mcp_file=mcp,
    )
    assert replay.idempotent_replay is True


def test_idempotent_replay_rejects_corrupt_cas_without_overwriting_target(tmp_path):
    source = tmp_path / "legacy"
    target = tmp_path / "v1"
    source.mkdir()
    _create_legacy_fixture(source)
    migrate_v030_to_v1(source, target, quarantine_key=QUARANTINE_KEY)
    database = target / TARGET_DATABASE_NAME
    service = ArtifactService(target / TARGET_ARTIFACT_ROOT_NAME, database_path=database)
    projection = service.list_user_artifacts()[0]
    corrupt_blob = service.blobs.path_for(projection.sha256)
    corrupt_blob.write_bytes(b"corrupt")

    with pytest.raises(TargetConflictError, match="CAS blob"):
        migrate_v030_to_v1(source, target, quarantine_key=QUARANTINE_KEY)

    assert target.exists()
    assert corrupt_blob.read_bytes() == b"corrupt"


def test_idempotent_replay_rejects_missing_initial_turn_revision(tmp_path):
    source = tmp_path / "legacy-missing-input-revision"
    target = tmp_path / "v1-missing-input-revision"
    source.mkdir()
    _create_legacy_fixture(source)
    migrate_v030_to_v1(source, target, quarantine_key=QUARANTINE_KEY)
    database = target / TARGET_DATABASE_NAME
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER turn_input_revisions_no_delete")
    connection.execute("DELETE FROM turn_input_revisions")
    connection.commit()
    connection.close()

    with pytest.raises(TargetConflictError, match="integrity verification"):
        migrate_v030_to_v1(source, target, quarantine_key=QUARANTINE_KEY)

    assert target.exists()
    assert _query_one(database, "SELECT COUNT(*) FROM turn_input_revisions") == 0


def test_dry_run_leaves_no_target_and_reports_required_quarantine_key(tmp_path):
    source = tmp_path / "legacy"
    target = tmp_path / "v1"
    source.mkdir()
    _create_legacy_fixture(source)
    before = inventory_source(source)

    report = migrate_v030_to_v1(source, target, dry_run=True)

    assert report.status == "dry_run_verified"
    assert report.counts["turn_input_revisions"] == report.counts["turns"] == 1
    assert not target.exists()
    assert inventory_source(source) == before
    assert any(item.code == "quarantine_key_required_for_commit" for item in report.warnings)


def test_commit_requires_external_quarantine_key_and_keeps_source_unchanged(tmp_path):
    source = tmp_path / "legacy"
    target = tmp_path / "v1"
    source.mkdir()
    _create_legacy_fixture(source)
    before = inventory_source(source)

    with pytest.raises(QuarantineKeyRequired):
        migrate_v030_to_v1(source, target)

    assert not target.exists()
    assert inventory_source(source) == before


def test_fault_discards_staging_and_preserves_source(tmp_path):
    source = tmp_path / "legacy"
    target = tmp_path / "v1"
    source.mkdir()
    _create_legacy_fixture(source)
    before = inventory_source(source)

    def fail(stage: str) -> None:
        if stage == "target.initialized":
            raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        V030ToV1Migrator(
            MigrationOptions(
                source_root=source,
                target_root=target,
                quarantine_key=QUARANTINE_KEY,
                fault_injector=fail,
            )
        ).run()

    assert not target.exists()
    assert not list(tmp_path.glob(".v1.*.staging"))
    assert inventory_source(source) == before


def test_failure_after_turn_input_backfill_rolls_back_then_retries_cleanly(tmp_path):
    source = tmp_path / "legacy-after-input"
    target = tmp_path / "v1-after-input"
    source.mkdir()
    _create_legacy_fixture(source)
    before = inventory_source(source)

    def fail_after_import(stage: str) -> None:
        if stage == "import.completed":
            raise RuntimeError("failure after initial input backfill")

    with pytest.raises(RuntimeError, match="after initial input backfill"):
        V030ToV1Migrator(
            MigrationOptions(
                source_root=source,
                target_root=target,
                quarantine_key=QUARANTINE_KEY,
                fault_injector=fail_after_import,
            )
        ).run()

    assert not target.exists()
    assert not list(tmp_path.glob(".v1.*.staging"))
    assert inventory_source(source) == before

    report = migrate_v030_to_v1(source, target, quarantine_key=QUARANTINE_KEY)
    assert report.counts["turn_input_revisions"] == report.counts["turns"] == 1
    _assert_imported_turn_input_contract(target / TARGET_DATABASE_NAME)


def test_source_change_aborts_publication_and_discards_target(tmp_path):
    source = tmp_path / "legacy"
    target = tmp_path / "v1"
    source.mkdir()
    _create_legacy_fixture(source)

    def mutate_source(stage: str) -> None:
        if stage == "import.completed":
            (source / "changed-during-migration.txt").write_text("changed", encoding="utf-8")

    with pytest.raises(SourceChangedError):
        V030ToV1Migrator(
            MigrationOptions(
                source_root=source,
                target_root=target,
                quarantine_key=QUARANTINE_KEY,
                fault_injector=mutate_source,
            )
        ).run()

    assert not target.exists()
    assert not list(tmp_path.glob(".v1.*.staging"))


def test_old_optional_columns_are_supported_but_required_column_is_enforced(tmp_path):
    source = tmp_path / "legacy-optional"
    target = tmp_path / "v1-optional"
    source.mkdir()
    _create_legacy_fixture(source, optional_columns=False)
    report = migrate_v030_to_v1(source, target, quarantine_key=QUARANTINE_KEY)
    assert report.counts["messages"] == 2

    broken = tmp_path / "legacy-broken"
    broken_target = tmp_path / "v1-broken"
    broken.mkdir()
    _create_legacy_fixture(broken, missing_role=True)
    before = inventory_source(broken)
    with pytest.raises(LegacySchemaError, match="role"):
        migrate_v030_to_v1(broken, broken_target, quarantine_key=QUARANTINE_KEY)
    assert not broken_target.exists()
    assert inventory_source(broken) == before


def test_corrupt_database_and_duplicate_ids_fail_without_publishing(tmp_path):
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    _create_legacy_fixture(corrupt)
    (corrupt / "sessions" / "conversations.db").write_bytes(b"not-a-sqlite-database")
    corrupt_before = inventory_source(corrupt)
    with pytest.raises(LegacyDatabaseError):
        migrate_v030_to_v1(corrupt, tmp_path / "corrupt-v1", quarantine_key=QUARANTINE_KEY)
    assert inventory_source(corrupt) == corrupt_before

    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    _create_legacy_fixture(duplicate, duplicate_sessions=True)
    duplicate_before = inventory_source(duplicate)
    with pytest.raises(DuplicateLegacyIdError):
        migrate_v030_to_v1(duplicate, tmp_path / "duplicate-v1", quarantine_key=QUARANTINE_KEY)
    assert inventory_source(duplicate) == duplicate_before


def test_path_traversal_artifact_is_never_read_or_copied(tmp_path):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.7\nEXTERNAL-SENTINEL-DO-NOT-COPY\n")
    source = tmp_path / "legacy"
    target = tmp_path / "v1"
    source.mkdir()
    _create_legacy_fixture(source, traversal_artifact=str(outside))

    report = migrate_v030_to_v1(source, target, quarantine_key=QUARANTINE_KEY)

    assert report.counts["artifacts_skipped_unsafe"] >= 1
    for path in target.rglob("*"):
        if path.is_file() and not path.as_posix().endswith("conversations.sqlite3"):
            assert b"EXTERNAL-SENTINEL-DO-NOT-COPY" not in path.read_bytes()


def test_target_inside_source_is_rejected_before_any_write(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    _create_legacy_fixture(source)
    before = inventory_source(source)
    with pytest.raises(SourceLayoutError):
        migrate_v030_to_v1(source, source / "v1", quarantine_key=QUARANTINE_KEY)
    assert inventory_source(source) == before


def test_released_v030_runtime_state_is_preserved_but_never_auto_executes(tmp_path):
    source = tmp_path / "legacy"
    target = tmp_path / "v1"
    external = tmp_path / "old-install"
    source.mkdir()
    external.mkdir()
    _create_legacy_fixture(source)
    _add_released_runtime_state(source)
    permission_file = external / "permissions.json"
    release_file = external / "runtime-manifest.json"
    _write_json(
        permission_file,
        {
            "mode": "full-access",
            "alwaysAllow": {"tool-execution:bash": True},
            "filesystem": {
                "default": "deny",
                "workspaceRoots": [str(source)],
                "rules": [{"path": str(source), "access": "write"}],
            },
            "updatedAt": "2026-07-08T00:00:00Z",
        },
    )
    _write_json(
        release_file,
        {
            "schemaVersion": "v0.2.5-runtime-manifest-v1",
            "product": "EcoreX",
            "version": "0.3.0",
            "sourceCommit": "f0750d247bfe52ffb95c137cadc9983a03010690",
            "packageSha256": "a" * 64,
        },
    )
    permission_before = permission_file.read_bytes()
    release_before = release_file.read_bytes()
    source_before = inventory_source(source)

    report = migrate_v030_to_v1(
        source,
        target,
        quarantine_key=QUARANTINE_KEY,
        permission_file=permission_file,
        release_evidence_file=release_file,
    )

    assert inventory_source(source) == source_before
    assert permission_file.read_bytes() == permission_before
    assert release_file.read_bytes() == release_before
    assert report.storage_schema_version == SCHEMA_VERSION
    assert report.import_layout_version == IMPORT_LAYOUT_VERSION
    assert len(report.target_schema_sha256) == 64
    assert report.data_generation_id.startswith("gen_")
    assert report.source_evidence["evidence_level"] == "release_marker_and_schema"
    assert report.source_evidence["marker_label"] == "@pinned/release-evidence"
    assert report.source_evidence["declared_commit"] == "f0750d247bfe52ffb95c137cadc9983a03010690"
    assert set(report.source_evidence["schema_tables"]) >= {
        "sessions",
        "messages",
        "chunks",
        "files",
        "agent_runs",
        "agent_run_events",
    }
    database = target / TARGET_DATABASE_NAME
    assert _query_one(database, "SELECT COUNT(*) FROM threads") == 3
    assert _query_one(database, "SELECT COUNT(*) FROM legacy_run_records") == 2
    assert _query_one(database, "SELECT COUNT(*) FROM legacy_run_event_records") == 2
    assert _query_one(database, "SELECT COUNT(*) FROM legacy_pending_work") == 1
    assert _query_one(database, "SELECT COUNT(*) FROM jobs") == 0
    assert _query_one(database, "SELECT COUNT(*) FROM legacy_scheduler_tasks") == 1
    assert _query_one(
        database,
        "SELECT COUNT(*) FROM legacy_scheduler_tasks "
        "WHERE legacy_enabled = 1 AND activation_status = 'requires_user_confirmation'",
    ) == 1
    assert _query_one(
        database,
        "SELECT COUNT(*) FROM turns WHERE status = 'interrupted' "
        "AND terminal_reason = 'legacy_migration_requires_user_confirmation'",
    ) == 1
    assert _query_one(
        database,
        "SELECT COUNT(*) FROM threads WHERE forked_from_thread_id IS NOT NULL "
        "AND forked_from_turn_id IS NOT NULL AND forked_from_seq > 0",
    ) == 1
    assert _query_all(
        database,
        "SELECT source_mode, target_profile, activation_status "
        "FROM legacy_permission_preferences",
    ) == [("full-access", "full_access", "staged_for_account_binding")]
    # The complete product schema is configuration-independent. Migration may
    # stage the legacy intent, but it must not activate a permission row.
    assert _query_one(
        database,
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
        "AND name = 'runtime_permission_state'",
    ) == 1
    assert _query_one(database, "SELECT COUNT(*) FROM runtime_permission_state") == 0
    cached_thread = _query_one(
        database,
        "SELECT COUNT(*) FROM threads WHERE title = '仅在 Web 缓存中的会话'",
    )
    assert cached_thread == 1
    database_bytes = database.read_bytes()
    assert b"scheduler-secret-must-not-migrate" not in database_bytes
    assert b"hidden-context-must-not-migrate" not in database_bytes
    assert b"secret-run-token" not in database_bytes
    assert b"[redacted]" in database_bytes


def test_release_evidence_version_mismatch_fails_closed(tmp_path):
    source = tmp_path / "legacy"
    target = tmp_path / "v1"
    evidence = tmp_path / "release.json"
    source.mkdir()
    _create_legacy_fixture(source)
    _write_json(evidence, {"version": "0.2.9", "sha256": "b" * 64})
    before = inventory_source(source)

    with pytest.raises(LegacySchemaError, match="0.3.0"):
        migrate_v030_to_v1(
            source,
            target,
            quarantine_key=QUARANTINE_KEY,
            release_evidence_file=evidence,
        )

    assert not target.exists()
    assert inventory_source(source) == before


def test_malformed_scheduler_and_queue_records_abort_without_source_mutation(tmp_path):
    malformed_scheduler = tmp_path / "legacy-scheduler"
    malformed_scheduler.mkdir()
    _create_legacy_fixture(malformed_scheduler)
    _write_json(
        malformed_scheduler / "scheduler" / "tasks.json",
        {"version": 1, "tasks": {"bad": {"id": "other", "name": "bad"}}},
    )
    scheduler_before = inventory_source(malformed_scheduler)
    with pytest.raises(LegacySchemaError, match="scheduler"):
        migrate_v030_to_v1(
            malformed_scheduler,
            tmp_path / "scheduler-target",
            quarantine_key=QUARANTINE_KEY,
        )
    assert inventory_source(malformed_scheduler) == scheduler_before

    malformed_queue = tmp_path / "legacy-queue"
    malformed_queue.mkdir()
    _create_legacy_fixture(malformed_queue)
    _write_json(
        malformed_queue / ".ecorex" / "queued-requests" / "req-a.json",
        {
            "schemaVersion": 1,
            "request_id": "req-b",
            "session_id": "legacy-session",
            "payload": {"request_id": "req-b", "session_id": "legacy-session"},
        },
    )
    queue_before = inventory_source(malformed_queue)
    with pytest.raises(LegacySchemaError, match="queued-request"):
        migrate_v030_to_v1(
            malformed_queue,
            tmp_path / "queue-target",
            quarantine_key=QUARANTINE_KEY,
        )
    assert inventory_source(malformed_queue) == queue_before
