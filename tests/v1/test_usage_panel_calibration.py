from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3

from ecorex.control_plane import usage_panel_service


TZ = timezone(timedelta(hours=8))


def _database(path: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users(
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT,
            deleted_at TEXT
        );
        CREATE TABLE sync_events(
            id INTEGER PRIMARY KEY,
            sync_key TEXT,
            event_type TEXT,
            org_id TEXT,
            user_email TEXT,
            user_key TEXT,
            device_id TEXT,
            session_id TEXT,
            request_id TEXT,
            source TEXT,
            status TEXT,
            detail TEXT,
            created_at TEXT,
            ingested_at TEXT
        );
        CREATE TABLE sync_artifacts(
            id INTEGER PRIMARY KEY,
            user_email TEXT,
            user_key TEXT,
            status TEXT,
            metadata TEXT,
            created_at TEXT
        );
        CREATE TABLE usage_events(
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            label TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            user_email TEXT,
            detail TEXT,
            created_at TEXT NOT NULL,
            device_id TEXT,
            session_id TEXT,
            model TEXT,
            provider TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    connection.executemany(
        "INSERT INTO users VALUES(?,?,?,?)",
        [
            ("u1", "同名用户", "one@example.test", None),
            ("u2", "同名用户", "two@example.test", None),
            ("u3", "已删除", "deleted@example.test", "2026-07-01T00:00:00+08:00"),
        ],
    )
    accepted = "2026-07-18T09:00:00+08:00"
    completed = "2026-07-18T09:01:00+08:00"
    detail = json.dumps(
        {
            "usage": {
                "input_tokens": 999,
                "output_tokens": 999,
                "total_tokens": 1998,
            }
        }
    )
    connection.executemany(
        "INSERT INTO sync_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                1,
                "s1",
                "run.accepted",
                "org",
                "one@example.test",
                "",
                "device",
                "session-1",
                "request-1",
                "WebUI",
                "running",
                detail,
                accepted,
                accepted,
            ),
            (
                2,
                "s2",
                "run.completed",
                "org",
                "one@example.test",
                "",
                "device",
                "session-1",
                "request-1",
                "WebUI",
                "completed",
                detail,
                completed,
                completed,
            ),
        ],
    )
    connection.execute(
        "INSERT INTO usage_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "usage-1",
            "chat",
            "message",
            0,
            "ONE@example.test",
            json.dumps({"usageSource": "provider", "cached_tokens": 3}),
            completed,
            "device",
            "session-1",
            "gpt-5.6-sol",
            "managed",
            10,
            4,
            14,
        ),
    )
    connection.commit()
    connection.close()


def test_usage_panel_uses_ledger_and_keeps_zero_usage_users(tmp_path, monkeypatch):
    database = tmp_path / "admin.sqlite3"
    _database(str(database))
    monkeypatch.setattr(usage_panel_service, "DB_PATH", str(database))

    payload = usage_panel_service.build_payload(
        datetime(2026, 7, 18, tzinfo=TZ),
        datetime(2026, 7, 19, tzinfo=TZ),
    )

    assert len(payload["users"]) == 2
    assert all("同名用户" in user for user in payload["users"])
    assert payload["kpis"]["inputTokens"] == 10
    assert payload["kpis"]["outputTokens"] == 4
    assert payload["kpis"]["totalTokens"] == 14
    assert len(payload["summaryRows"]) == 2

    rows = {row["email"]: row for row in payload["summaryRows"]}
    assert rows["one@example.test"]["totalTasks"] == 1
    assert rows["one@example.test"]["tokenUsageRecords"] == 1
    assert rows["one@example.test"]["cacheReadTokens"] == 3
    assert rows["two@example.test"]["totalTasks"] == 0
    assert rows["two@example.test"]["totalTokens"] == 0
