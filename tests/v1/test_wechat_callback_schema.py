from __future__ import annotations

import sqlite3

import pytest

from ecorex.control_plane import wechat_callback_schema as schema


def test_v2_migration_preserves_v1_rows(tmp_path) -> None:
    database = tmp_path / "callback.db"
    connection = sqlite3.connect(database)
    schema._execute_sql(connection, schema.WECHAT_CALLBACK_SCHEMA_SQL)
    connection.execute(
        "INSERT INTO wechat_callback_schema_migrations("
        "version,migration_name,migration_checksum,installed_at) VALUES(1,?,?,?)",
        (schema._MIGRATION_NAME, schema._MIGRATION_CHECKSUM, "2026-08-10T00:00:00Z"),
    )
    connection.execute(
        "INSERT INTO wechat_callback_bindings("
        "binding_id,channel_id,account_id,organization_id,app_id_sha256,"
        "credential_envelope_json,status,created_at,updated_at) "
        "VALUES('wxbind_a','wechatmp','account-a','organization-a','app-a',"
        "'ciphertext','enabled','now','now')"
    )
    connection.commit()
    connection.close()

    receipt = schema.WechatCallbackSchemaManager(database).migrate()

    assert receipt.migration_version == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT channel_id,credential_envelope_json FROM "
            "wechat_callback_bindings WHERE binding_id='wxbind_a'"
        ).fetchone() == ("wechatmp", "ciphertext")
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(wechat_callback_inbox)")
        }
    assert {
        "conversation_sha256",
        "passive_attempts",
        "passive_hard_deadline_at",
        "passive_hint_sent",
        "passive_original_replied",
    } <= columns


def test_v2_migration_rolls_back_partial_ddl(tmp_path, monkeypatch) -> None:
    database = tmp_path / "callback.db"
    schema.WechatCallbackSchemaManager(database).migrate()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM wechat_callback_schema_migrations WHERE version=2"
        )
        connection.execute(
            "ALTER TABLE wechat_callback_inbox DROP COLUMN passive_hard_deadline_at"
        )
        connection.execute(
            "ALTER TABLE wechat_callback_inbox DROP COLUMN passive_attempts"
        )
        connection.execute(
            "ALTER TABLE wechat_callback_inbox DROP COLUMN passive_hint_sent"
        )
        connection.execute(
            "ALTER TABLE wechat_callback_inbox DROP COLUMN passive_original_replied"
        )
        connection.execute(
            "ALTER TABLE wechat_callback_inbox DROP COLUMN conversation_sha256"
        )
        connection.commit()

    original = schema._execute_sql

    def fail_after_first_v2_statement(connection, sql):
        if sql == schema.WECHAT_CALLBACK_SCHEMA_V2_SQL:
            connection.execute(
                "ALTER TABLE wechat_callback_inbox ADD COLUMN conversation_sha256 TEXT"
            )
            raise RuntimeError("injected migration failure")
        original(connection, sql)

    monkeypatch.setattr(schema, "_execute_sql", fail_after_first_v2_statement)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        schema.WechatCallbackSchemaManager(database).migrate()

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(wechat_callback_inbox)")
        }
        assert "conversation_sha256" not in columns
        assert connection.execute(
            "SELECT COUNT(*) FROM wechat_callback_schema_migrations WHERE version=2"
        ).fetchone() == (0,)


def test_v2_schema_keeps_v1_binary_rollback_object_contract(tmp_path) -> None:
    database = tmp_path / "callback.db"
    schema.WechatCallbackSchemaManager(database).migrate()
    expected_v1_objects = {
        "wechat_callback_schema_migrations",
        "wechat_callback_bindings",
        "idx_wechat_callback_bindings_owner",
        "idx_wechat_callback_mp_mode",
        "wechat_callback_bindings_identity_immutable",
        "wechat_callback_inbox",
        "idx_wechat_callback_inbox_pull",
        "wechat_callback_deliveries",
        "idx_wechat_callback_deliveries_event",
        "wechat_callback_kf_state",
        "wechat_callback_audit_outbox",
        "idx_wechat_callback_audit_pending",
    }
    with sqlite3.connect(database) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE "
                "name LIKE 'wechat_callback_%' OR "
                "name LIKE 'idx_wechat_callback_%'"
            )
        }
        version1 = connection.execute(
            "SELECT migration_name,migration_checksum FROM "
            "wechat_callback_schema_migrations WHERE version=1"
        ).fetchone()
    assert names == expected_v1_objects
    assert version1 == (schema._MIGRATION_NAME, schema._MIGRATION_CHECKSUM)
