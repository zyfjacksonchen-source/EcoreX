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


def _add_v1_facts(path: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE admin_ops_users(
            account_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            email TEXT,
            organization_id TEXT,
            status TEXT NOT NULL
        );
        CREATE TABLE gateway_requests(
            request_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            status TEXT NOT NULL,
            terminal_event_type TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE gateway_events(
            request_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(request_id, seq)
        );
        """
    )
    connection.executemany(
        "INSERT INTO admin_ops_users VALUES(?,?,?,?,?)",
        [
            (
                "account-one",
                "同名用户",
                "ONE@example.test",
                "org",
                "active",
            ),
            (
                "account-zero",
                "v1 零活动用户",
                "zero@example.test",
                "org",
                "active",
            ),
        ],
    )
    accepted = "2026-07-18T10:00:00+08:00"
    completed = "2026-07-18T10:02:00+08:00"
    connection.executemany(
        "INSERT INTO sync_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                3,
                "s3",
                "run.accepted",
                "org",
                "one@example.test",
                "",
                "device",
                "trace-gateway",
                "gateway-request-1",
                "WebUI",
                "running",
                "{}",
                accepted,
                accepted,
            ),
            (
                4,
                "s4",
                "run.completed",
                "org",
                "one@example.test",
                "",
                "device",
                "trace-gateway",
                "gateway-request-1",
                "WebUI",
                "completed",
                "{}",
                completed,
                completed,
            ),
        ],
    )
    # This is the legacy copy of the same immutable provider fact. The Gateway
    # completion below must replace it rather than add a second charge.
    connection.execute(
        "INSERT INTO usage_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "legacy-gateway-copy",
            "chat",
            "message",
            0,
            "one@example.test",
            json.dumps(
                {
                    "usageSource": "provider",
                    "requestId": "gateway-request-1",
                }
            ),
            completed,
            "device",
            "trace-gateway",
            "gpt-5.6-sol",
            "managed",
            900,
            99,
            999,
        ),
    )
    connection.execute(
        "INSERT INTO gateway_requests VALUES(?,?,?,?,?,?,?,?)",
        (
            "gateway-request-1",
            "account-one",
            "gpt-5.6-sol",
            "trace-gateway",
            "completed",
            "response.completed",
            accepted,
            completed,
        ),
    )
    connection.execute(
        "INSERT INTO gateway_events VALUES(?,?,?,?)",
        (
            "gateway-request-1",
            1,
            json.dumps(
                {
                    "schema_version": 1,
                    "seq": 1,
                    "event_type": "response.completed",
                    "response_id": "response-1",
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 5,
                        "total_tokens": 25,
                    },
                }
            ),
            completed,
        ),
    )
    connection.commit()
    connection.close()


def _v1_payload(tmp_path, monkeypatch):
    database = tmp_path / "admin-v1.sqlite3"
    _database(str(database))
    _add_v1_facts(str(database))
    monkeypatch.setattr(usage_panel_service, "DB_PATH", str(database))
    monkeypatch.setattr(
        usage_panel_service,
        "CONTROL_PLANE_DB_PATH",
        str(database),
    )
    monkeypatch.setattr(usage_panel_service, "GATEWAY_DB_PATH", str(database))
    return usage_panel_service.build_payload(
        datetime(2026, 7, 18, tzinfo=TZ),
        datetime(2026, 7, 19, tzinfo=TZ),
    )


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


def test_v1_control_plane_user_with_zero_activity_remains_visible(tmp_path, monkeypatch):
    payload = _v1_payload(tmp_path, monkeypatch)

    rows = {row["email"]: row for row in payload["summaryRows"]}
    assert "v1 零活动用户" in payload["users"]
    assert rows["zero@example.test"]["totalTasks"] == 0
    assert rows["zero@example.test"]["totalTokens"] == 0


def test_gateway_completion_supplies_usage_and_task_detail(tmp_path, monkeypatch):
    payload = _v1_payload(tmp_path, monkeypatch)

    gateway_tasks = [
        task for task in payload["tasks"]
        if task["requestId"] == "gateway-request-1"
    ]
    assert len(gateway_tasks) == 1
    assert gateway_tasks[0]["email"] == "one@example.test"
    assert gateway_tasks[0]["success"] is True
    assert gateway_tasks[0]["inputTokens"] == 20
    assert gateway_tasks[0]["outputTokens"] == 5
    assert gateway_tasks[0]["totalTokens"] == 25


def test_same_request_is_deduplicated_across_legacy_and_gateway_ledgers(
    tmp_path,
    monkeypatch,
):
    payload = _v1_payload(tmp_path, monkeypatch)

    # The original legacy-only request contributes 14 and the cross-ledger
    # request contributes the authoritative Gateway total of 25, not 999+25.
    assert payload["kpis"]["inputTokens"] == 30
    assert payload["kpis"]["outputTokens"] == 9
    assert payload["kpis"]["totalTokens"] == 39
    assert payload["kpis"]["tasks"] == 2
    rows = {row["email"]: row for row in payload["summaryRows"]}
    assert rows["one@example.test"]["tokenUsageRecords"] == 2
    assert rows["one@example.test"]["tokenUsageTasks"] == 2
