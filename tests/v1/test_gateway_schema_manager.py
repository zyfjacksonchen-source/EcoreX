from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from ecorex.gateway import (
    GatewayEvent,
    GatewayEventType,
    GatewaySchemaError,
    GatewaySchemaManager,
    SQLiteGatewayStore,
)
from ecorex.gateway.schema import (
    EMPTY_GATEWAY_SCHEMA_SHA256,
    GATEWAY_SCHEMA_HISTORY_SQL,
    GATEWAY_SCHEMA_SHA256,
    GATEWAY_SCHEMA_V1_SHA256,
    GATEWAY_SCHEMA_V1_SQL,
    GATEWAY_SCHEMA_V2_SHA256,
    GATEWAY_SCHEMA_V2_SQL,
    GATEWAY_SCHEMA_V3_SHA256,
    GATEWAY_SCHEMA_V3_SQL,
    GATEWAY_SCHEMA_RECEIPT_VERSION,
    LEGACY_GATEWAY_SCHEMA_SQL,
    MIGRATION_001_CHECKSUM,
    MIGRATION_001_NAME,
    MIGRATION_002_CHECKSUM,
    MIGRATION_002_NAME,
    MIGRATION_003_CHECKSUM,
    MIGRATION_003_NAME,
    GatewaySchemaReceipt,
    main as gateway_schema_main,
)


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def test_gateway_store_constructor_requires_explicit_migration_without_writing(
    tmp_path,
) -> None:
    database = tmp_path / "uninitialized-gateway.sqlite3"

    with pytest.raises(GatewaySchemaError, match="unavailable"):
        SQLiteGatewayStore(database)

    assert not database.exists()


def test_gateway_schema_migration_is_explicit_versioned_and_idempotent(
    tmp_path,
) -> None:
    database = tmp_path / "gateway.sqlite3"
    manager = GatewaySchemaManager(database)

    first = manager.migrate()
    second = manager.migrate()
    store = SQLiteGatewayStore(database)

    assert second == first == store.schema_receipt
    assert first.migration_version == 4
    assert first.target_schema_sha256 == GATEWAY_SCHEMA_SHA256
    assert first.transformed_rows == 0
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT migration_checksum,receipt_json,receipt_sha256 "
            "FROM gateway_schema_migrations WHERE version=1"
        ).fetchone()
    assert row is not None
    assert hashlib.sha256(row[1].encode("utf-8")).hexdigest() == row[2]
    assert json.loads(row[1])["migration_checksum"] == row[0]


def test_gateway_schema_deployment_cli_migrates_then_validates(
    tmp_path,
    capsys,
) -> None:
    database = tmp_path / "gateway-cli.sqlite3"

    assert gateway_schema_main(["migrate", str(database)]) == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated["migration_version"] == 4
    assert gateway_schema_main(["validate", str(database)]) == 0
    validated = json.loads(capsys.readouterr().out)

    assert validated == migrated


def test_existing_v1_gateway_is_upgraded_without_losing_ledger(tmp_path) -> None:
    database = tmp_path / "gateway-v1.sqlite3"
    installed_at = datetime.now(UTC).isoformat()
    receipt = GatewaySchemaReceipt(
        schema_version=GATEWAY_SCHEMA_RECEIPT_VERSION,
        migration_version=1,
        migration_name=MIGRATION_001_NAME,
        migration_checksum=MIGRATION_001_CHECKSUM,
        source_schema_sha256=EMPTY_GATEWAY_SCHEMA_SHA256,
        target_schema_sha256=GATEWAY_SCHEMA_V1_SHA256,
        transformed_rows=0,
        event_chain_sha256=hashlib.sha256(b"[]").hexdigest(),
        installed_at=installed_at,
    )
    encoded = _canonical(receipt.to_dict())
    with sqlite3.connect(database) as connection:
        connection.executescript(GATEWAY_SCHEMA_V1_SQL)
        connection.execute(
            "INSERT INTO gateway_schema_migrations VALUES(?,?,?,?,?,?,?,?,?)",
            (
                1,
                MIGRATION_001_NAME,
                MIGRATION_001_CHECKSUM,
                EMPTY_GATEWAY_SCHEMA_SHA256,
                GATEWAY_SCHEMA_V1_SHA256,
                0,
                encoded,
                hashlib.sha256(encoded.encode()).hexdigest(),
                installed_at,
            ),
        )

    upgraded = GatewaySchemaManager(database).migrate()
    assert upgraded.migration_version == 4
    with sqlite3.connect(database) as connection:
        versions = connection.execute(
            "SELECT version FROM gateway_schema_migrations ORDER BY version"
        ).fetchall()
        handoff_table = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='gateway_chat_handoffs'"
        ).fetchone()
    assert versions == [(1,), (2,), (3,), (4,)]
    assert handoff_table == (1,)


def test_existing_v2_gateway_backfills_only_billable_terminal_usage(
    tmp_path,
) -> None:
    database = tmp_path / "gateway-v2.sqlite3"
    installed_at = datetime.now(UTC).isoformat()
    empty_chain = hashlib.sha256(b"[]").hexdigest()
    receipt_v1 = GatewaySchemaReceipt(
        schema_version=GATEWAY_SCHEMA_RECEIPT_VERSION,
        migration_version=1,
        migration_name=MIGRATION_001_NAME,
        migration_checksum=MIGRATION_001_CHECKSUM,
        source_schema_sha256=EMPTY_GATEWAY_SCHEMA_SHA256,
        target_schema_sha256=GATEWAY_SCHEMA_V1_SHA256,
        transformed_rows=0,
        event_chain_sha256=empty_chain,
        installed_at=installed_at,
    )
    receipt_v2 = GatewaySchemaReceipt(
        schema_version=GATEWAY_SCHEMA_RECEIPT_VERSION,
        migration_version=2,
        migration_name=MIGRATION_002_NAME,
        migration_checksum=MIGRATION_002_CHECKSUM,
        source_schema_sha256=GATEWAY_SCHEMA_V1_SHA256,
        target_schema_sha256=GATEWAY_SCHEMA_V2_SHA256,
        transformed_rows=0,
        event_chain_sha256=empty_chain,
        installed_at=installed_at,
    )
    events = {
        "request-completed": GatewayEvent(
            seq=1,
            event_type=GatewayEventType.RESPONSE_COMPLETED,
            response_id="response-completed",
            usage={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        ),
        "request-tool": GatewayEvent(
            seq=1,
            event_type=GatewayEventType.TOOL_CALL_REQUESTED,
            response_id="response-tool",
            tool_call_id="call-tool",
            tool_name="read",
            arguments={"path": "report.docx"},
            usage={"input_tokens": 7, "output_tokens": 11, "total_tokens": 18},
        ),
        "request-failed": GatewayEvent(
            seq=1,
            event_type=GatewayEventType.RESPONSE_FAILED,
            response_id="response-failed",
            error_code="provider_response_failed",
            error_message="The managed model provider rejected the request.",
        ),
    }
    with sqlite3.connect(database) as connection:
        connection.executescript(GATEWAY_SCHEMA_V2_SQL)
        for receipt in (receipt_v1, receipt_v2):
            encoded = _canonical(receipt.to_dict())
            connection.execute(
                "INSERT INTO gateway_schema_migrations VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    receipt.migration_version,
                    receipt.migration_name,
                    receipt.migration_checksum,
                    receipt.source_schema_sha256,
                    receipt.target_schema_sha256,
                    receipt.transformed_rows,
                    encoded,
                    hashlib.sha256(encoded.encode()).hexdigest(),
                    receipt.installed_at,
                ),
            )
        for request_id, event in events.items():
            payload = _canonical(event.model_dump(mode="json"))
            payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
            entry_digest = hashlib.sha256(
                "\0".join(
                    (
                        request_id,
                        "1",
                        payload_sha256,
                        installed_at,
                        "0" * 64,
                    )
                ).encode()
            ).hexdigest()
            connection.execute(
                "INSERT INTO gateway_requests("
                "request_id,account_id,quota_period,request_fingerprint,model_id,"
                "trace_id,status,lease_token,lease_expires_at,response_id,"
                "terminal_event_type,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,'completed',NULL,NULL,?,?,?,?)",
                (
                    request_id,
                    "account-1",
                    "2026-07",
                    hashlib.sha256(request_id.encode()).hexdigest(),
                    "ecorex-chat",
                    f"trace-{request_id}",
                    event.response_id,
                    event.event_type.value,
                    installed_at,
                    installed_at,
                ),
            )
            connection.execute(
                "INSERT INTO gateway_events("
                "request_id,seq,payload_json,payload_sha256,previous_digest,"
                "entry_digest,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    request_id,
                    1,
                    payload,
                    payload_sha256,
                    "0" * 64,
                    entry_digest,
                    installed_at,
                ),
            )

    upgraded = GatewaySchemaManager(database).migrate()

    assert upgraded.migration_version == 4
    with sqlite3.connect(database) as connection:
        settlements = connection.execute(
            "SELECT request_id,state FROM gateway_usage_settlements "
            "ORDER BY request_id"
        ).fetchall()
        request_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(gateway_requests)")
        }
        attempt_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(gateway_model_attempts)")
        }
    assert settlements == [
        ("request-completed", "pending"),
        ("request-tool", "pending"),
    ]
    assert "organization_id" in request_columns
    assert {"organization_id", "reasoning_effort"} <= attempt_columns


def test_existing_v3_gateway_adds_audit_columns_without_rewriting_facts(
    tmp_path,
) -> None:
    database = tmp_path / "gateway-v3.sqlite3"
    installed_at = datetime.now(UTC).isoformat()
    empty_chain = hashlib.sha256(b"[]").hexdigest()
    receipts = (
        GatewaySchemaReceipt(
            GATEWAY_SCHEMA_RECEIPT_VERSION,
            1,
            MIGRATION_001_NAME,
            MIGRATION_001_CHECKSUM,
            EMPTY_GATEWAY_SCHEMA_SHA256,
            GATEWAY_SCHEMA_V1_SHA256,
            0,
            empty_chain,
            installed_at,
        ),
        GatewaySchemaReceipt(
            GATEWAY_SCHEMA_RECEIPT_VERSION,
            2,
            MIGRATION_002_NAME,
            MIGRATION_002_CHECKSUM,
            GATEWAY_SCHEMA_V1_SHA256,
            GATEWAY_SCHEMA_V2_SHA256,
            0,
            empty_chain,
            installed_at,
        ),
        GatewaySchemaReceipt(
            GATEWAY_SCHEMA_RECEIPT_VERSION,
            3,
            MIGRATION_003_NAME,
            MIGRATION_003_CHECKSUM,
            GATEWAY_SCHEMA_V2_SHA256,
            GATEWAY_SCHEMA_V3_SHA256,
            0,
            empty_chain,
            installed_at,
        ),
    )
    with sqlite3.connect(database) as connection:
        connection.executescript(GATEWAY_SCHEMA_V3_SQL)
        for receipt in receipts:
            encoded = _canonical(receipt.to_dict())
            connection.execute(
                "INSERT INTO gateway_schema_migrations VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    receipt.migration_version,
                    receipt.migration_name,
                    receipt.migration_checksum,
                    receipt.source_schema_sha256,
                    receipt.target_schema_sha256,
                    receipt.transformed_rows,
                    encoded,
                    hashlib.sha256(encoded.encode()).hexdigest(),
                    receipt.installed_at,
                ),
            )
        connection.execute(
            "INSERT INTO gateway_requests("
            "request_id,account_id,quota_period,request_fingerprint,model_id,trace_id,"
            "status,lease_token,lease_expires_at,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,'active',?,?,?,?)",
            (
                "request-existing",
                "account-existing",
                "2026-08",
                "f" * 64,
                "ecorex-chat",
                "trace-existing",
                "lease-existing",
                installed_at,
                installed_at,
                installed_at,
            ),
        )
        connection.execute(
            "INSERT INTO gateway_model_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "request-existing",
                "thread-existing",
                "turn-existing",
                "config-existing",
                7,
                "ecorex-chat",
                "gpt-5.6-luna",
                "responses",
                "ecorex_chat",
                installed_at,
                installed_at,
            ),
        )

    receipt = GatewaySchemaManager(database).migrate()

    assert receipt.migration_version == 4
    with sqlite3.connect(database) as connection:
        request_fact = connection.execute(
            "SELECT account_id,organization_id,model_id,trace_id FROM gateway_requests"
        ).fetchone()
        attempt_fact = connection.execute(
            "SELECT thread_id,turn_id,upstream_model_id,organization_id,"
            "reasoning_effort FROM gateway_model_attempts"
        ).fetchone()
    assert request_fact == (
        "account-existing",
        None,
        "ecorex-chat",
        "trace-existing",
    )
    assert attempt_fact == (
        "thread-existing",
        "turn-existing",
        "gpt-5.6-luna",
        None,
        None,
    )


def test_gateway_store_rejects_tampered_object_without_repair(tmp_path) -> None:
    database = tmp_path / "tampered-gateway.sqlite3"
    GatewaySchemaManager(database).migrate()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER gateway_events_no_delete")

    with pytest.raises(GatewaySchemaError, match="fingerprint"):
        SQLiteGatewayStore(database)

    with sqlite3.connect(database) as connection:
        missing = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='gateway_events_no_delete'"
        ).fetchone()
    assert missing is None


def test_gateway_store_rejects_history_row_tamper_with_restored_objects(
    tmp_path,
) -> None:
    database = tmp_path / "tampered-history.sqlite3"
    GatewaySchemaManager(database).migrate()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER gateway_schema_migrations_no_update")
        connection.execute(
            "UPDATE gateway_schema_migrations SET source_schema_sha256=? WHERE version=1",
            ("a" * 64,),
        )
        connection.executescript(GATEWAY_SCHEMA_HISTORY_SQL)

    with pytest.raises(GatewaySchemaError, match="history"):
        SQLiteGatewayStore(database)


def test_gateway_store_rejects_future_schema_history_without_writing(tmp_path) -> None:
    database = tmp_path / "future-gateway.sqlite3"
    GatewaySchemaManager(database).migrate()
    future_receipt = _canonical({"future": True})
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO gateway_schema_migrations("
            "version,migration_name,migration_checksum,source_schema_sha256,"
            "target_schema_sha256,transformed_rows,receipt_json,receipt_sha256,"
            "installed_at) VALUES(5,?,?,?,?,?,?,?,?)",
            (
                "future-gateway-schema",
                "f" * 64,
                GATEWAY_SCHEMA_SHA256,
                GATEWAY_SCHEMA_SHA256,
                0,
                future_receipt,
                hashlib.sha256(future_receipt.encode("utf-8")).hexdigest(),
                datetime.now(UTC).isoformat(),
            ),
        )

    with pytest.raises(GatewaySchemaError, match="newer"):
        SQLiteGatewayStore(database)

    with sqlite3.connect(database) as connection:
        versions = connection.execute(
            "SELECT version FROM gateway_schema_migrations ORDER BY version"
        ).fetchall()
    assert versions == [(1,), (2,), (3,), (4,), (5,)]


def test_explicit_gateway_migration_backfills_known_legacy_event_chain(
    tmp_path,
) -> None:
    database = tmp_path / "legacy-gateway.sqlite3"
    event = GatewayEvent(
        seq=1,
        event_type=GatewayEventType.RESPONSE_COMPLETED,
        response_id="response-legacy",
    )
    payload = _canonical(event.model_dump(mode="json"))
    created_at = "2026-07-10T00:00:00+00:00"
    with sqlite3.connect(database) as connection:
        connection.executescript(LEGACY_GATEWAY_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO gateway_requests("
            "request_id,account_id,quota_period,request_fingerprint,model_id,trace_id,"
            "status,response_id,terminal_event_type,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,'completed',?,?,?,?)",
            (
                "request-legacy",
                "account-legacy",
                "2026-07",
                "fingerprint-legacy",
                "ecorex-chat",
                "trace-legacy",
                "response-legacy",
                GatewayEventType.RESPONSE_COMPLETED.value,
                created_at,
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO gateway_events("
            "request_id,seq,payload_json,payload_sha256,created_at"
            ") VALUES(?,?,?,?,?)",
            (
                "request-legacy",
                1,
                payload,
                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                created_at,
            ),
        )

    receipt = GatewaySchemaManager(database).migrate()
    store = SQLiteGatewayStore(database)

    assert receipt.transformed_rows == 1
    assert store.events("request-legacy") == (event,)
    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(gateway_events)")
        }
        history = connection.execute(
            "SELECT transformed_rows,receipt_json FROM gateway_schema_migrations "
            "WHERE version=2"
        ).fetchone()
    assert {"previous_digest", "entry_digest"} <= columns
    assert history[0] == 1
    assert json.loads(history[1])["event_chain_sha256"] == receipt.event_chain_sha256


def test_gateway_migration_rejects_unknown_shape_without_repair(tmp_path) -> None:
    database = tmp_path / "unknown-gateway.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unknown_gateway_state(value TEXT NOT NULL)")

    with pytest.raises(GatewaySchemaError, match="source shape is unknown"):
        GatewaySchemaManager(database).migrate()

    with sqlite3.connect(database) as connection:
        objects = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            )
        }
    assert objects == {"unknown_gateway_state"}
