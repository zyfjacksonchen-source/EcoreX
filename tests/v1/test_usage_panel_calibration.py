from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import runpy
import sqlite3

import pytest

from ecorex import __version__ as PRODUCT_VERSION
from ecorex.control_plane import usage_panel_service
from ecorex.gateway import GatewayAccountUsageProjection


TZ = timezone(timedelta(hours=8))


def test_production_deployer_accepts_the_current_usage_contract(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    deployer = runpy.run_path(str(root / "scripts/deploy-v030-production-usage-panel.py"))
    payload = deployer["_materialize_source"](
        Path(usage_panel_service.__file__).read_bytes(),
        expected_version=PRODUCT_VERSION,
    )

    assert deployer["_source_contract_matches"](
        payload,
        expected_version=PRODUCT_VERSION,
        expected_projection=usage_panel_service.USAGE_PROJECTION_VERSION,
    )
    assert b"from ecorex import __version__" not in payload
    compile(payload, "usage_panel_api.py", "exec")
    assert repr(usage_panel_service.USAGE_PROJECTION_VERSION) in deployer[
        "_verify_program"
    ](
        "2026-08-01T00:00:00+08:00",
        "2026-08-10T00:00:00+08:00",
        usage_panel_service.USAGE_PROJECTION_VERSION,
    )


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


def _database_with_active_users(path: str, count: int) -> None:
    _database(path)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        DELETE FROM sync_artifacts;
        DELETE FROM sync_events;
        DELETE FROM usage_events;
        DELETE FROM users;
        """
    )
    connection.executemany(
        "INSERT INTO users VALUES(?,?,?,NULL)",
        [
            (
                f"user-{index:03d}",
                f"用户 {index:03d}",
                f"user-{index:03d}@example.test",
            )
            for index in range(count)
        ],
    )
    connection.commit()
    connection.close()


def _use_database(monkeypatch, path: str) -> None:
    monkeypatch.setattr(usage_panel_service, "DB_PATH", path)
    monkeypatch.setattr(usage_panel_service, "CONTROL_PLANE_DB_PATH", path)
    monkeypatch.setattr(usage_panel_service, "GATEWAY_DB_PATH", path)


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


def test_data_request_accepts_the_exact_maximum_date_span(tmp_path, monkeypatch):
    database = tmp_path / "bounded-range.sqlite3"
    _database_with_active_users(str(database), 1)
    _use_database(monkeypatch, str(database))

    payload = usage_panel_service.build_data_request_payload(
        {
            "start": ["2026-01-01"],
            "end": ["2026-04-01"],
        }
    )

    assert usage_panel_service.MAX_DATA_RANGE_DAYS == 90
    assert len(payload["dates"]) == usage_panel_service.MAX_DATA_RANGE_DAYS
    assert len(payload["summaryRows"]) == usage_panel_service.MAX_DATA_RANGE_DAYS


def test_data_request_rejects_an_oversized_range_before_building(
    monkeypatch,
):
    called = False

    def forbidden_build(_start, _end):
        nonlocal called
        called = True
        raise AssertionError("build_payload must not run for an unsafe range")

    monkeypatch.setattr(usage_panel_service, "build_payload", forbidden_build)
    handler = object.__new__(usage_panel_service.Handler)
    handler.path = "/api/data?start=2026-01-01&end=2026-04-02"
    responses = []
    handler.send_json = lambda status, payload: responses.append((status, payload))

    handler.do_GET()

    assert called is False
    assert responses == [
        (
            422,
            {
                "ok": False,
                "error": "range_too_large",
                "message": "单次最多查询 90 天",
                "actual": 91,
                "limit": 90,
            },
        )
    ]


def test_data_request_rejects_projected_response_size_before_building(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "response-limit.sqlite3"
    _database_with_active_users(str(database), 41)
    _use_database(monkeypatch, str(database))
    monkeypatch.setattr(usage_panel_service, "MAX_DATA_RESPONSE_ROWS", 300)
    called = False

    def forbidden_build(_start, _end):
        nonlocal called
        called = True
        raise AssertionError("build_payload must not run beyond the response budget")

    monkeypatch.setattr(usage_panel_service, "build_payload", forbidden_build)

    with pytest.raises(usage_panel_service.UsagePanelRequestError) as captured:
        usage_panel_service.build_data_request_payload(
            {
                "start": ["2026-07-13"],
                "end": ["2026-07-20"],
            }
        )

    assert called is False
    assert captured.value.status == 413
    assert captured.value.code == "response_too_large"
    assert captured.value.actual == 390
    assert captured.value.limit == 300


def test_data_request_keeps_41_users_visible_for_a_normal_week(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "normal-week.sqlite3"
    _database_with_active_users(str(database), 41)
    _use_database(monkeypatch, str(database))

    payload = usage_panel_service.build_data_request_payload(
        {
            "start": ["2026-07-13"],
            "end": ["2026-07-20"],
        }
    )

    assert len(payload["users"]) == 41
    assert len(payload["dates"]) == 7
    assert len(payload["summaryRows"]) == 287
    assert {row["email"] for row in payload["summaryRows"]} == {
        f"user-{index:03d}@example.test"
        for index in range(41)
    }


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


def test_task_statuses_keep_tool_failure_partial_and_running_out_of_success_rate(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "task-terminal-facts.sqlite3"
    _database(str(database))
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM sync_events")
    connection.execute("DELETE FROM usage_events")
    rows = []
    sequence = 1
    for request_id, events in {
        "plain": (("run.accepted", "running"), ("run.completed", "completed")),
        "tool-fallback": (
            ("run.accepted", "running"),
            ("tool.failed", "failed"),
            ("run.completed", "completed"),
        ),
        "failed": (("run.accepted", "running"), ("run.failed", "failed")),
        "running": (("run.accepted", "running"),),
    }.items():
        for minute, (event_type, status) in enumerate(events):
            created = f"2026-07-18T09:{sequence + minute:02d}:00+08:00"
            rows.append(
                (
                    sequence,
                    f"status-{sequence}",
                    event_type,
                    "org",
                    "one@example.test",
                    "",
                    "device",
                    f"session-{request_id}",
                    request_id,
                    "WebUI",
                    status,
                    json.dumps({"tool": "browser"}) if event_type == "tool.failed" else "{}",
                    created,
                    created,
                )
            )
            sequence += 1
    connection.executemany(
        "INSERT INTO sync_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.commit()
    connection.close()
    _use_database(monkeypatch, str(database))

    payload = usage_panel_service.build_payload(
        datetime(2026, 7, 18, tzinfo=TZ),
        datetime(2026, 7, 19, tzinfo=TZ),
    )
    statuses = {task["requestId"]: task["statusCategory"] for task in payload["tasks"]}

    assert statuses == {
        "plain": "成功",
        "tool-fallback": "部分完成",
        "failed": "失败",
        "running": "进行中",
    }
    assert payload["kpis"]["successTasks"] == 1
    assert payload["kpis"]["partialTasks"] == 1
    assert payload["kpis"]["failedTasks"] == 1
    assert payload["kpis"]["runningTasks"] == 1
    assert payload["kpis"]["terminalTasks"] == 3
    assert payload["kpis"]["successRate"] == 33.3


def test_gateway_model_rounds_roll_up_to_one_cow_turn(tmp_path, monkeypatch) -> None:
    database = tmp_path / "gateway-turn-rollup.sqlite3"
    _database(str(database))
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        DELETE FROM sync_events;
        DELETE FROM usage_events;
        CREATE TABLE gateway_requests(
            request_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            organization_id TEXT,
            model_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            status TEXT NOT NULL,
            terminal_event_type TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE gateway_model_attempts(
            request_id TEXT PRIMARY KEY,
            upstream_model_id TEXT NOT NULL,
            reasoning_effort TEXT,
            thread_id TEXT,
            turn_id TEXT
        );
        CREATE TABLE gateway_events(
            request_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(request_id, seq)
        );
        INSERT INTO gateway_requests VALUES
            ('round-tool','one@example.test','org','gpt-5.6-sol','thread-1','completed','tool_call.requested','2026-07-18T10:00:00+08:00','2026-07-18T10:01:00+08:00'),
            ('round-failed','one@example.test','org','gpt-5.6-sol','thread-1','completed','response.failed','2026-07-18T10:01:00+08:00','2026-07-18T10:02:00+08:00'),
            ('round-final','one@example.test','org','gpt-5.6-sol','thread-1','completed','response.completed','2026-07-18T10:02:00+08:00','2026-07-18T10:03:00+08:00');
        INSERT INTO gateway_model_attempts VALUES
            ('round-tool','gpt-5.6-sol','high','thread-1','turn-1'),
            ('round-failed','gpt-5.6-sol','high','thread-1','turn-1'),
            ('round-final','gpt-5.6-sol','high','thread-1','turn-1');
        """
    )
    connection.executemany(
        "INSERT INTO gateway_events VALUES(?,?,?,?)",
        [
            (
                "round-tool",
                1,
                json.dumps(
                    {
                        "event_type": "tool_call.requested",
                        "usage": {
                            "input_tokens": 5,
                            "output_tokens": 1,
                            "total_tokens": 6,
                        },
                    }
                ),
                "2026-07-18T10:01:00+08:00",
            ),
            (
                "round-final",
                1,
                json.dumps(
                    {
                        "event_type": "response.completed",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 2,
                            "total_tokens": 12,
                        },
                    }
                ),
                "2026-07-18T10:03:00+08:00",
            ),
        ],
    )
    connection.commit()
    connection.close()
    _use_database(monkeypatch, str(database))

    payload = usage_panel_service.build_payload(
        datetime(2026, 7, 18, tzinfo=TZ),
        datetime(2026, 7, 19, tzinfo=TZ),
    )

    assert len(payload["tasks"]) == 1
    task = payload["tasks"][0]
    assert task["requestId"] == "turn-1"
    assert task["statusCategory"] == "部分完成"
    assert (task["inputTokens"], task["outputTokens"], task["totalTokens"]) == (
        15,
        3,
        18,
    )
    assert payload["kpis"]["tasks"] == 1
    assert payload["kpis"]["partialTasks"] == 1
    assert payload["kpis"]["successRate"] == 0


def test_composer_calendar_uses_only_settled_gateway_chat_facts(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "settled-account-usage.sqlite3"
    _database(str(database))
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE admin_ops_users(
            account_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            email TEXT,
            organization_id TEXT,
            status TEXT NOT NULL,
            token_limit INTEGER NOT NULL,
            tokens_used INTEGER NOT NULL,
            image_limit INTEGER NOT NULL,
            images_used INTEGER NOT NULL
        );
        CREATE TABLE admin_ops_provider_usage_facts(
            fact_id TEXT PRIMARY KEY,
            source_service TEXT NOT NULL,
            source_id TEXT NOT NULL,
            usage_kind TEXT NOT NULL,
            account_id TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            image_count INTEGER NOT NULL,
            provider_created_at TEXT NOT NULL
        );
        INSERT INTO admin_ops_users VALUES(
            'account-one','测试用户','one@example.test','org','active',0,400040,0,1
        );
        INSERT INTO admin_ops_provider_usage_facts VALUES
            ('baseline','legacy_baseline','account-one','baseline','account-one',0,0,400000,0,'2026-07-18T08:00:00+08:00'),
            ('chat-1','managed_gateway','request-1','chat','account-one',10,5,15,0,'2026-07-18T09:00:00+08:00'),
            ('chat-2','managed_gateway','request-2','chat','account-one',20,5,25,0,'2026-07-18T10:00:00+08:00'),
            ('image-1','image_service','image-1','image','account-one',0,0,0,1,'2026-07-18T11:00:00+08:00');
        """
    )
    connection.commit()
    connection.close()
    _use_database(monkeypatch, str(database))

    projection = usage_panel_service.build_account_usage_projection(
        "account-one",
        timezone_name="Asia/Shanghai",
        now=datetime(2026, 7, 18, 18, 0, tzinfo=TZ),
    )

    assert projection["today"] == {
        "input_tokens": 30,
        "output_tokens": 10,
        "total_tokens": 40,
    }
    assert projection["week"] == projection["today"]


def test_usage_panel_web_exposes_partial_and_running_without_folding_into_failure() -> None:
    source = (
        Path(usage_panel_service.__file__).with_name("usage_panel_web") / "app.js"
    ).read_text(encoding="utf-8")

    assert "const statusOptions = ['成功', '部分完成', '失败', '中止', '进行中'];" in source
    assert "partialTasksForRow" in source
    assert "runningTasksForRow" in source


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


def test_usage_and_audit_share_one_projection_and_reconciliation(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "usage-audit.sqlite3"
    _database(str(database))
    _add_v1_facts(str(database))
    _use_database(monkeypatch, str(database))
    panel = usage_panel_service.build_payload(
        datetime(2026, 7, 18, tzinfo=TZ),
        datetime(2026, 7, 19, tzinfo=TZ),
    )

    class Store:
        def connect(self):
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            return connection

        @staticmethod
        def runtime_audit(_connection, _filters):
            return {"summary": {"userActions": 2}}

    monkeypatch.setattr(usage_panel_service, "load_admin_store", Store)
    audit = usage_panel_service.build_runtime_audit(
        {"start": ["2026-07-18"], "end": ["2026-07-19"]}
    )
    iso_panel = usage_panel_service.build_data_request_payload(
        {
            "start": ["2026-07-18T00:00:00 08:00"],
            "end": ["2026-07-19T00:00:00 08:00"],
        }
    )

    assert audit["projection_version"] == usage_panel_service.USAGE_PROJECTION_VERSION
    assert audit["kpis"] == panel["kpis"]
    assert iso_panel["kpis"] == panel["kpis"]
    assert audit["runtimeAudit"]["usageKpis"] == panel["kpis"]
    assert audit["reconciliation"] == {
        "canonical_record_count": 2,
        "replaced_duplicate_count": 1,
        "unassociated_record_count": 1,
        "missing_provider_usage_count": 0,
        "account_balance_mismatch_count": 0,
        "token_balance_delta": 0,
        "image_balance_delta": 0,
        "account_balances": [],
        "by_model": [
            {
                "model": "gpt-5.6-sol",
                "records": 2,
                "tokens": 39,
                "reasoning_efforts": [],
            }
        ],
        "by_tenant_model": [],
        "image_provider_fact_count": 0,
        "image_provider_facts_truncated": False,
        "image_provider_facts": [],
    }


def test_usage_panel_reconciles_management_counters_with_provider_facts(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "usage-balances.sqlite3"
    _database(str(database))
    _add_v1_facts(str(database))
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        ALTER TABLE admin_ops_users ADD COLUMN token_limit INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE admin_ops_users ADD COLUMN tokens_used INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE admin_ops_users ADD COLUMN image_limit INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE admin_ops_users ADD COLUMN images_used INTEGER NOT NULL DEFAULT 0;
        CREATE TABLE admin_ops_provider_usage_facts(
            fact_id TEXT PRIMARY KEY,
            source_service TEXT NOT NULL,
            source_id TEXT NOT NULL,
            usage_kind TEXT NOT NULL,
            account_id TEXT NOT NULL,
            total_tokens INTEGER NOT NULL,
            image_count INTEGER NOT NULL,
            provider_created_at TEXT NOT NULL
        );
        UPDATE admin_ops_users SET tokens_used=25,images_used=1
        WHERE account_id='account-one';
        UPDATE admin_ops_users SET tokens_used=7 WHERE account_id='account-zero';
        INSERT INTO admin_ops_provider_usage_facts VALUES(
            'fact-1','managed_gateway','gateway-request-1','chat','account-one',25,0,
            '2026-07-18T10:02:00+08:00'
        );
        INSERT INTO admin_ops_provider_usage_facts VALUES(
            'fact-image-old','image_service','image-job-old','image','account-one',0,1,
            '2026-07-18T10:03:00+08:00'
        );
        """
    )
    connection.commit()
    connection.close()
    _use_database(monkeypatch, str(database))

    payload = usage_panel_service.build_payload(
        datetime(2026, 7, 18, tzinfo=TZ),
        datetime(2026, 7, 19, tzinfo=TZ),
    )

    assert payload["reconciliation"]["account_balance_mismatch_count"] == 1
    assert payload["reconciliation"]["token_balance_delta"] == 7
    assert next(
        row
        for row in payload["reconciliation"]["account_balances"]
        if row["account_id"] == "account-one"
    )["balanced"] is True
    assert payload["reconciliation"]["image_provider_facts"] == [
        {
            "job_id": "image-job-old",
            "account_id": "account-one",
            "user": "同名用户 · one@example.test",
            "organization_id": "",
            "requested_model_id": "",
            "provider_reported_model_id": "",
            "actual_model_id": "未公开",
            "actual_provider_id": "",
            "fallback_from_model_id": "",
            "fallback_used": None,
            "job_status": "",
            "result_status": "",
            "image_count": 1,
            "provider_created_at": "2026-07-18T10:03:00+08:00",
        }
    ]


def test_usage_panel_projects_image_provider_audit_metadata(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "usage-image-audit.sqlite3"
    _database(str(database))
    _add_v1_facts(str(database))
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        ALTER TABLE admin_ops_users ADD COLUMN token_limit INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE admin_ops_users ADD COLUMN tokens_used INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE admin_ops_users ADD COLUMN image_limit INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE admin_ops_users ADD COLUMN images_used INTEGER NOT NULL DEFAULT 0;
        CREATE TABLE admin_ops_provider_usage_facts(
            fact_id TEXT PRIMARY KEY,
            source_service TEXT NOT NULL,
            source_id TEXT NOT NULL,
            usage_kind TEXT NOT NULL,
            account_id TEXT NOT NULL,
            total_tokens INTEGER NOT NULL,
            image_count INTEGER NOT NULL,
            provider_created_at TEXT NOT NULL,
            organization_id TEXT,
            requested_model_id TEXT,
            provider_reported_model_id TEXT,
            actual_model_id TEXT,
            actual_provider_id TEXT,
            fallback_from_model_id TEXT,
            fallback_used INTEGER,
            job_status TEXT,
            result_status TEXT
        );
        UPDATE admin_ops_users SET images_used=1 WHERE account_id='account-one';
        INSERT INTO admin_ops_provider_usage_facts VALUES(
            'fact-image','image_service','image-job-1','image','account-one',0,1,
            '2026-07-18T10:03:00+08:00','org-image','gpt-image-2',
            'gpt-image-2','gpt-image-2','managed-image','gpt-image-2-pro',1,
            'running','completed'
        );
        """
    )
    connection.commit()
    connection.close()
    _use_database(monkeypatch, str(database))

    fact = usage_panel_service.build_payload(
        datetime(2026, 7, 18, tzinfo=TZ),
        datetime(2026, 7, 19, tzinfo=TZ),
    )["reconciliation"]["image_provider_facts"][0]

    assert fact["organization_id"] == "org-image"
    assert fact["requested_model_id"] == "gpt-image-2"
    assert fact["provider_reported_model_id"] == "gpt-image-2"
    assert fact["actual_model_id"] == "gpt-image-2"
    assert fact["actual_provider_id"] == "managed-image"
    assert fact["fallback_from_model_id"] == "gpt-image-2-pro"
    assert fact["fallback_used"] is True
    assert fact["job_id"] == "image-job-1"
    assert fact["job_status"] == "running"
    assert fact["result_status"] == "completed"


def test_usage_panel_reconciles_actual_upstream_model_and_reasoning(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "usage-model-attempt.sqlite3"
    _database(str(database))
    _add_v1_facts(str(database))
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        ALTER TABLE gateway_requests ADD COLUMN organization_id TEXT;
        UPDATE gateway_requests SET organization_id='org-luna';
        CREATE TABLE gateway_model_attempts(
            request_id TEXT PRIMARY KEY,
            upstream_model_id TEXT NOT NULL,
            reasoning_effort TEXT,
            thread_id TEXT,
            turn_id TEXT
        );
        INSERT INTO gateway_model_attempts VALUES(
            'gateway-request-1','gpt-5.6-luna','max','thread-1','turn-1'
        );
        """
    )
    connection.commit()
    connection.close()
    _use_database(monkeypatch, str(database))

    payload = usage_panel_service.build_payload(
        datetime(2026, 7, 18, tzinfo=TZ),
        datetime(2026, 7, 19, tzinfo=TZ),
    )

    assert payload["reconciliation"]["by_model"] == [
        {
            "model": "gpt-5.6-luna",
            "records": 1,
            "tokens": 25,
            "reasoning_efforts": [{"level": "max", "records": 1}],
        },
        {
            "model": "gpt-5.6-sol",
            "records": 1,
            "tokens": 14,
            "reasoning_efforts": [],
        },
    ]
    assert payload["reconciliation"]["by_tenant_model"] == [
        {
            "organization_id": "org-luna",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "records": 1,
            "tokens": 25,
        }
    ]


def test_composer_account_projection_uses_only_settled_gateway_facts(
    tmp_path,
    monkeypatch,
):
    _v1_payload(tmp_path, monkeypatch)
    database = tmp_path / "admin-v1.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE admin_ops_provider_usage_facts(
            fact_id TEXT PRIMARY KEY,
            source_service TEXT NOT NULL,
            source_id TEXT NOT NULL,
            usage_kind TEXT NOT NULL,
            account_id TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            image_count INTEGER NOT NULL,
            provider_created_at TEXT NOT NULL
        );
        INSERT INTO admin_ops_provider_usage_facts VALUES
            ('settled-legacy','managed_gateway','request-1','chat','account-one',10,4,14,0,'2026-07-18T09:01:00+08:00'),
            ('settled-gateway','managed_gateway','gateway-request-1','chat','account-one',20,5,25,0,'2026-07-18T10:02:00+08:00');
        """
    )
    connection.commit()
    connection.close()
    now = datetime(2026, 7, 18, 18, 0, tzinfo=TZ)

    projection = usage_panel_service.build_account_usage_projection(
        "account-one",
        timezone_name="Asia/Shanghai",
        now=now,
    )
    assert projection["today"] == {
        "input_tokens": 30,
        "output_tokens": 9,
        "total_tokens": 39,
    }
    assert projection["week"] == projection["today"]
    assert projection["week"]["total_tokens"] == 39
    assert projection["scope"] == "account"
    assert projection["coverage_started_at"] is not None
    assert set(projection) == set(GatewayAccountUsageProjection.model_fields)
    GatewayAccountUsageProjection.model_validate(projection)


def test_tool_handoff_usage_is_counted_as_a_terminal_provider_fact(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "admin-v1-tool.sqlite3"
    _database(str(database))
    _add_v1_facts(str(database))
    connection = sqlite3.connect(database)
    accepted = "2026-07-18T11:00:00+08:00"
    completed = "2026-07-18T11:00:05+08:00"
    connection.execute(
        "INSERT INTO gateway_requests VALUES(?,?,?,?,?,?,?,?)",
        (
            "gateway-tool-request",
            "account-one",
            "gpt-5.6-sol",
            "trace-tool",
            "completed",
            "tool_call.requested",
            accepted,
            completed,
        ),
    )
    connection.execute(
        "INSERT INTO gateway_events VALUES(?,?,?,?)",
        (
            "gateway-tool-request",
            1,
            json.dumps(
                {
                    "schema_version": 1,
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "response-tool",
                    "tool_call_id": "call-tool",
                    "tool_name": "read",
                    "arguments": {"path": "report.docx"},
                    "idempotency_key": "tool-call-idempotency",
                    "usage": {
                        "input_tokens": 6,
                        "output_tokens": 2,
                        "total_tokens": 8,
                    },
                }
            ),
            completed,
        ),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(usage_panel_service, "DB_PATH", str(database))
    monkeypatch.setattr(usage_panel_service, "CONTROL_PLANE_DB_PATH", str(database))
    monkeypatch.setattr(usage_panel_service, "GATEWAY_DB_PATH", str(database))

    payload = usage_panel_service.build_payload(
        datetime(2026, 7, 18, tzinfo=TZ),
        datetime(2026, 7, 19, tzinfo=TZ),
    )

    assert payload["kpis"]["totalTokens"] == 47
    task = next(
        item
        for item in payload["tasks"]
        if item["requestId"] == "gateway-tool-request"
    )
    assert task["totalTokens"] == 8
    assert task["statusCategory"] == "进行中"
