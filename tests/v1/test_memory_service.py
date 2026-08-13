from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ecorex.memory import MemoryConflict, MemoryService, MemoryUndoExpired
from ecorex.runtime import SQLiteDatabase
from ecorex.runtime import create_app
from ecorex.runtime.api import RuntimeSettings
from ecorex.runtime.errors import SchemaVersionError
from ecorex.runtime.schema_catalog import product_schema_inventory
from ecorex.runtime.schema_fragments.memory import MEMORY_SCHEMA_FRAGMENT


def test_cow_learning_switch_drives_idle_memory_and_persists_restart(
    tmp_path: Path, monkeypatch,
) -> None:
    import threading
    import time
    import config as config_module
    import agent.evolution.executor as executor
    import agent.evolution.trigger as trigger
    from agent.evolution.config import EvolutionConfig
    from agent.memory.config import MemoryConfig

    config_path = tmp_path / "config.json"
    config_path.write_text('{"unrelated":"kept"}\n', encoding="utf-8")
    monkeypatch.setattr(config_module, "config", config_module.Config({}))
    service = MemoryService(
        tmp_path / "runtime.db",
        workspace_root=tmp_path / "workspace",
        config_path=config_path,
    )
    assert service.learning_settings().enabled is True

    workspace = tmp_path / "workspace"
    agent = SimpleNamespace(
        messages=[
            {"role": "user", "content": "以后周报都用三点式。"},
            {"role": "assistant", "content": "记住了。"},
        ],
        messages_lock=threading.Lock(),
        tools=[],
        model=object(),
        memory_manager=SimpleNamespace(config=MemoryConfig(workspace_root=str(workspace))),
        skill_manager=None,
        _evo_last_active=time.time() - 61,
        _evo_turns=1,
    )

    review_runs = 0

    class ReviewAgent:
        def run_stream(self, *_args, **_kwargs):
            nonlocal review_runs
            review_runs += 1
            daily = workspace / "memory" / f"{datetime.now().date().isoformat()}.md"
            with daily.open("a", encoding="utf-8") as stream:
                stream.write(f"- 用户偏好三点式周报（学习 {review_runs}）。\n")
            return "已学习三点式周报偏好。"

    class Bridge:
        agents = {"thread-1": agent}
        default_agent = agent

        def create_agent(self, **_kwargs):
            return ReviewAgent()

        def remember_scheduled_output(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(executor, "_builtin_skill_names", lambda: set())
    monkeypatch.setattr(trigger, "run_evolution_for_session", executor.run_evolution_for_session)
    trigger._scan_once(Bridge(), EvolutionConfig(True, 1, 1, 12))
    evolution = workspace / "memory" / "evolution" / f"{datetime.now().date().isoformat()}.md"
    assert evolution.is_file()
    assert "已学习三点式周报偏好" in evolution.read_text(encoding="utf-8")

    assert service.set_learning_enabled(False).enabled is False
    before = evolution.read_text(encoding="utf-8")
    agent._evo_turns = 1
    trigger._scan_once(Bridge(), EvolutionConfig(False, 1, 1, 12))
    assert evolution.read_text(encoding="utf-8") == before
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "self_evolution_enabled": False,
        "unrelated": "kept",
    }
    assert service.set_learning_enabled(True).enabled is True
    agent._evo_turns = 1
    agent._evo_last_active = time.time() - 61
    agent._evo_done_msg_count = 0
    trigger._scan_once(Bridge(), EvolutionConfig(True, 1, 1, 12))
    after_reenable = evolution.read_text(encoding="utf-8")
    assert len(after_reenable) > len(before)
    assert review_runs == 2
    service.set_learning_enabled(False)
    trigger._scan_once(Bridge(), EvolutionConfig(False, 1, 1, 12))
    assert evolution.read_text(encoding="utf-8") == after_reenable
    restarted = MemoryService(
        tmp_path / "runtime.db",
        workspace_root=workspace,
        config_path=config_path,
    )
    assert restarted.learning_settings().enabled is False


def test_cow_learning_switch_stops_nightly_dream(tmp_path: Path, monkeypatch) -> None:
    import threading
    import config as config_module
    from agent.memory.summarizer import MemoryFlushManager
    from bridge.agent_initializer import AgentInitializer

    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    today = datetime.now().date().isoformat()
    (workspace / "memory" / f"{today}.md").write_text(
        "- 用户偏好简洁回答。\n", encoding="utf-8"
    )

    class DreamModel:
        def call(self, _request):
            return {
                "content": [{
                    "type": "text",
                    "text": "[MEMORY]\n- 用户偏好简洁回答。\n[DREAM]\n整理了简洁回答偏好。",
                }]
            }

    flush = MemoryFlushManager(workspace, llm_model=DreamModel())
    flush.create_daily_summary = lambda _messages: False
    agent = SimpleNamespace(
        memory_manager=SimpleNamespace(flush_manager=flush),
        messages=[{"role": "user", "content": "记住这个偏好"}],
        messages_lock=threading.Lock(),
    )
    initializer = AgentInitializer(object(), SimpleNamespace(default_agent=agent, agents={}))
    monkeypatch.setattr(config_module, "config", config_module.Config({"self_evolution_enabled": True}))
    initializer._flush_all_agents()
    dream = workspace / "memory" / "dreams" / f"{today}.md"
    assert "用户偏好简洁回答" in (workspace / "MEMORY.md").read_text(encoding="utf-8")
    assert "整理了简洁回答偏好" in dream.read_text(encoding="utf-8")
    before = dream.read_text(encoding="utf-8")
    config_module.conf()["self_evolution_enabled"] = False
    (workspace / "memory" / f"{today}.md").write_text(
        "- 用户偏好详细回答。\n", encoding="utf-8"
    )
    initializer._flush_all_agents()
    assert dream.read_text(encoding="utf-8") == before


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _memory_schema_records(path) -> tuple[tuple[str, str], ...]:
    names = MEMORY_SCHEMA_FRAGMENT.object_names
    placeholders = ",".join("?" for _ in names)
    with sqlite3.connect(path) as connection:
        return tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_schema "
                f"WHERE name IN ({placeholders}) ORDER BY name",
                names,
            ).fetchall()
        )


def test_memory_schema_fragment_is_static_product_inventory() -> None:
    assert dict(product_schema_inventory())[MEMORY_SCHEMA_FRAGMENT.fragment_id] == (
        MEMORY_SCHEMA_FRAGMENT.object_names
    )


def _seed(service: MemoryService) -> None:
    with service.database.transaction() as connection:
        for record_id, origin in (
            ("mem_factory", "factory"),
            ("mem_learned", "learned"),
            ("mem_imported", "imported"),
        ):
            connection.execute(
                "INSERT INTO memory_canonical_records("
                "record_id,legacy_chunk_id,user_id,scope,source,path,start_line,end_line,"
                "text,legacy_hash,memory_origin) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    "legacy_" + record_id,
                    "local-user",
                    "user",
                    origin,
                    f"memory/{record_id}.md",
                    1,
                    1,
                    record_id,
                    "hash_" + record_id,
                    origin,
                ),
            )
        connection.execute(
            "INSERT INTO memory_files(path,source,legacy_hash,mtime,size_bytes,availability,"
            "memory_origin) VALUES(?,?,?,?,?,?,?)",
            ("memory/user.md", "learned", "hash-file", 1, 20, "stored", "learned"),
        )


def test_reset_is_transactional_idempotent_and_never_touches_factory_memory(tmp_path) -> None:
    clock = Clock()
    service = MemoryService(tmp_path / "runtime.db", clock=clock)
    _seed(service)

    reset = service.reset_learned(
        confirmed=True,
        client_request_id="memory_reset_request_0001",
    )
    duplicate = service.reset_learned(
        confirmed=True,
        client_request_id="memory_reset_request_0001",
    )
    assert duplicate == reset
    assert reset.affected_records == 2
    assert reset.affected_files == 1
    assert reset.can_undo is True

    snapshot = service.snapshot()
    assert snapshot.active_learned_records == 0
    assert snapshot.active_user_files == 0
    assert snapshot.factory_records == 1
    assert snapshot.tombstoned_records == 2
    assert snapshot.tombstoned_files == 1
    assert snapshot.revision == 1

    with service.database.reader() as connection:
        factory = connection.execute(
            "SELECT memory_state,reset_id FROM memory_canonical_records "
            "WHERE record_id='mem_factory'"
        ).fetchone()
        assert dict(factory) == {"memory_state": "active", "reset_id": None}
        assert connection.execute("SELECT COUNT(*) FROM memory_audit_events").fetchone()[0] == 1

    with pytest.raises(MemoryConflict):
        service.undo_reset(
            reset.reset_id,
            confirmed=True,
            client_request_id="memory_reset_request_0001",
        )


def test_reset_can_be_undone_once_and_retries_return_the_same_projection(tmp_path) -> None:
    clock = Clock()
    service = MemoryService(tmp_path / "runtime.db", clock=clock)
    _seed(service)
    reset = service.reset_learned(
        confirmed=True,
        client_request_id="memory_reset_request_0002",
    )

    undone = service.undo_reset(
        reset.reset_id,
        confirmed=True,
        client_request_id="memory_undo_request_0002",
    )
    duplicate = service.undo_reset(
        reset.reset_id,
        confirmed=True,
        client_request_id="memory_undo_request_0002",
    )
    assert duplicate == undone
    assert undone.status == "undone"
    assert undone.can_undo is False
    snapshot = service.snapshot()
    assert snapshot.active_learned_records == 2
    assert snapshot.active_user_files == 1
    assert snapshot.factory_records == 1
    assert snapshot.tombstoned_records == 0
    assert snapshot.revision == 2

    with service.database.reader() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_audit_events").fetchone()[0] == 2


def test_injected_crash_rolls_back_records_files_batch_request_and_audit(tmp_path) -> None:
    class InjectedCrash(BaseException):
        pass

    clock = Clock()

    def fault(phase: str, _reset_id: str) -> None:
        if phase == "after_records_tombstoned":
            raise InjectedCrash

    service = MemoryService(tmp_path / "runtime.db", clock=clock, fault_hook=fault)
    _seed(service)
    with pytest.raises(InjectedCrash):
        service.reset_learned(
            confirmed=True,
            client_request_id="memory_reset_request_crash",
        )

    snapshot = service.snapshot()
    assert snapshot.active_learned_records == 2
    assert snapshot.active_user_files == 1
    assert snapshot.tombstoned_records == 0
    assert snapshot.revision == 0
    with service.database.reader() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_reset_batches").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_mutation_requests").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_audit_events").fetchone()[0] == 0


def test_expired_reset_cannot_be_undone_and_purge_keeps_factory_memory(tmp_path) -> None:
    clock = Clock()
    service = MemoryService(
        tmp_path / "runtime.db",
        clock=clock,
        undo_window=timedelta(hours=1),
    )
    _seed(service)
    reset = service.reset_learned(
        confirmed=True,
        client_request_id="memory_reset_request_0003",
    )
    clock.value += timedelta(hours=2)

    with pytest.raises(MemoryUndoExpired):
        service.undo_reset(
            reset.reset_id,
            confirmed=True,
            client_request_id="memory_undo_request_0003",
        )
    assert service.purge_expired() == 1
    snapshot = service.snapshot()
    assert snapshot.factory_records == 1
    assert snapshot.active_learned_records == 0
    assert snapshot.tombstoned_records == 0
    assert snapshot.latest_reset and snapshot.latest_reset.status == "purged"
    assert snapshot.revision == 2


def test_memory_schema_tamper_is_rejected_without_startup_repair(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    service = MemoryService(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TRIGGER memory_audit_no_update;
            CREATE TRIGGER memory_audit_no_update
            BEFORE UPDATE ON memory_audit_events
            BEGIN
                SELECT 1;
            END;
            """
        )
    tampered = _memory_schema_records(path)

    with pytest.raises(SchemaVersionError, match="local-memory is incompatible"):
        MemoryService(path)

    assert _memory_schema_records(path) == tampered
    assert service.snapshot().revision == 0


def test_migration_era_memory_schema_requires_signed_migration_without_repair(
    tmp_path,
) -> None:
    path = tmp_path / "runtime.db"
    SQLiteDatabase(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE memory_canonical_records;
            DROP TABLE memory_files;
            DROP TABLE memory_reset_batches;
            DROP TABLE memory_mutation_requests;
            DROP TABLE memory_audit_events;
            DROP TABLE memory_meta;

            CREATE TABLE memory_canonical_records (
                record_id TEXT PRIMARY KEY,
                legacy_chunk_id TEXT NOT NULL UNIQUE,
                user_id TEXT,
                scope TEXT NOT NULL,
                source TEXT NOT NULL,
                path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                text TEXT NOT NULL,
                legacy_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                embedding_state TEXT NOT NULL DEFAULT 'rebuild_required',
                created_at INTEGER,
                updated_at INTEGER
            );
            CREATE TABLE memory_files (
                path TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                legacy_hash TEXT NOT NULL,
                mtime INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                updated_at INTEGER,
                blob_sha256 TEXT,
                availability TEXT NOT NULL
            );
            INSERT INTO memory_canonical_records(
                record_id,legacy_chunk_id,scope,source,path,start_line,end_line,text,legacy_hash
            ) VALUES('legacy-memory','legacy-chunk','user','memory','memory/MEMORY.md',1,1,'中文','h');
            """
        )
    tampered = _memory_schema_records(path)

    with pytest.raises(SchemaVersionError, match="product schema objects are missing"):
        MemoryService(path)

    assert _memory_schema_records(path) == tampered
    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(memory_canonical_records)"
            ).fetchall()
        }
        row = connection.execute(
            "SELECT record_id,text FROM memory_canonical_records"
        ).fetchone()
    assert {"memory_origin", "memory_state", "reset_id", "tombstoned_at"}.isdisjoint(
        columns
    )
    assert row == ("legacy-memory", "中文")


def test_memory_api_requires_product_security_and_returns_authoritative_snapshot(
    tmp_path, monkeypatch,
) -> None:
    import config as config_module

    monkeypatch.setattr(config_module, "config", config_module.Config({}))
    token = "r" * 32
    csrf = "c" * 32
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=token,
            csrf_token=csrf,
            webui_origins=("http://testserver",),
        )
    )
    _seed(app.state.memory_service)
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {token}"}
    mutation = {
        **auth,
        "Origin": "http://testserver",
        "X-EcoreX-CSRF": csrf,
    }

    learning = client.get("/api/v1/memory/learning", headers=auth)
    assert learning.json() == {"enabled": True}
    assert client.put(
        "/api/v1/memory/learning", headers=auth, json={"enabled": False}
    ).status_code == 403
    updated = client.put(
        "/api/v1/memory/learning", headers=mutation, json={"enabled": False}
    )
    assert updated.json() == {"enabled": False}
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {
        "self_evolution_enabled": False
    }
    config_module.conf()["self_evolution_enabled"] = True
    create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "restart.db",
            runtime_bearer_token=token,
            csrf_token=csrf,
            webui_origins=("http://testserver",),
        )
    )
    assert config_module.conf()["self_evolution_enabled"] is False

    snapshot = client.get("/api/v1/memory", headers=auth)
    assert snapshot.status_code == 200
    assert snapshot.json()["resettable_count"] == 3
    assert client.post(
        "/api/v1/memory/reset",
        headers=auth,
        json={"confirmed": True, "client_request_id": "memory_api_reset_0001"},
    ).status_code == 403

    reset = client.post(
        "/api/v1/memory/reset",
        headers=mutation,
        json={"confirmed": True, "client_request_id": "memory_api_reset_0001"},
    )
    assert reset.status_code == 200
    body = reset.json()
    assert body["memory"]["resettable_count"] == 0
    assert body["memory"]["factory_records"] == 1
    reset_id = body["reset"]["reset_id"]

    duplicate = client.post(
        "/api/v1/memory/reset",
        headers=mutation,
        json={"confirmed": True, "client_request_id": "memory_api_reset_0001"},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["reset"]["reset_id"] == reset_id

    undone = client.post(
        f"/api/v1/memory/resets/{reset_id}/undo",
        headers=mutation,
        json={"confirmed": True, "client_request_id": "memory_api_undo_0001"},
    )
    assert undone.status_code == 200
    assert undone.json()["memory"]["resettable_count"] == 3
    assert undone.json()["reset"]["status"] == "undone"
