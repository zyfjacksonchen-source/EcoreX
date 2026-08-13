from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from ecorex.control_plane.management import (
    AdminManagementConflict,
    AdminManagementRepository,
)
from ecorex.control_plane.management_schema import AdminManagementSchemaManager
from ecorex.control_plane.models import ControlPrincipal
from ecorex.migration import (
    LegacyAdminManagementImportError,
    import_v0292_admin_management,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
KEY = b"k" * 32
SECRETS = {
    "openai": "sk-legacy-openai-secret",
    "deepseek": "sk-legacy-deepseek-secret",
    "gemini": "sk-legacy-gemini-secret",
    "doubao": "ark-legacy-doubao-secret",
    "image": "sk-legacy-image-secret",
}
ACTOR = ControlPrincipal(
    subject="migration-test-admin",
    client_id="migration-tests",
    account_id="migration-admin",
    roles=frozenset({"platform_admin"}),
)


def legacy_database(
    path: Path, *, duplicate_deepseek: bool = False, include_image: bool = True
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users(
          id TEXT PRIMARY KEY,name TEXT NOT NULL,email TEXT NOT NULL UNIQUE,
          role TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,password_hash TEXT,must_change_password INTEGER,
          daily_token_limit INTEGER NOT NULL,weekly_token_limit INTEGER NOT NULL,
          last_login_at TEXT,deleted_at TEXT
        );
        CREATE TABLE usage_events(
          id TEXT PRIMARY KEY,category TEXT NOT NULL,label TEXT NOT NULL,
          amount INTEGER NOT NULL DEFAULT 0,user_email TEXT,detail TEXT,
          created_at TEXT NOT NULL,device_id TEXT,session_id TEXT,model TEXT,
          provider TEXT,input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,total_tokens INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE model_credentials(
          id TEXT PRIMARY KEY,name TEXT NOT NULL,provider TEXT NOT NULL,
          model TEXT NOT NULL,bot_type TEXT NOT NULL,api_base TEXT NOT NULL,
          api_key TEXT NOT NULL,scope_type TEXT NOT NULL,scope_value TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
        );
        CREATE TABLE client_sessions(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,
          device_id TEXT,app_version TEXT,created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,revoked_at TEXT
        );
        CREATE TABLE messages(id TEXT PRIMARY KEY,content TEXT NOT NULL);
        """
    )
    timestamp = NOW.isoformat()
    connection.executemany(
        "INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("active", "Active", "active@example.com", "member", "active", timestamp, timestamp, "x", 0, 100, 1000, None, None),
            ("disabled", "Disabled", "disabled@example.com", "member", "disabled", timestamp, timestamp, "x", 0, 200, 2000, None, None),
            ("deleted", "Deleted", "deleted@example.com", "member", "active", timestamp, timestamp, "x", 0, 300, 3000, None, timestamp),
            ("invited", "Invited", "invited@example.com", "member", "invited", timestamp, timestamp, "x", 0, 400, 4000, None, None),
        ],
    )
    future = (NOW + timedelta(days=1)).isoformat()
    past = (NOW - timedelta(seconds=1)).isoformat()
    connection.executemany(
        "INSERT INTO client_sessions VALUES(?,?,?,?,?,?,?,?,?)",
        [
            ("eligible", "active", "a" * 64, None, "0.2.9.2", timestamp, future, timestamp, None),
            ("revoked", "active", "b" * 64, None, "0.2.9.2", timestamp, future, timestamp, timestamp),
            ("expired", "disabled", "c" * 64, None, "0.2.9.2", timestamp, past, timestamp, None),
            ("deleted-session", "deleted", "d" * 64, None, "0.2.9.2", timestamp, future, timestamp, None),
        ],
    )
    connection.executemany(
        "INSERT INTO usage_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("usage-chat", "chat", "chat", 10, "active@example.com", None, timestamp, None, None, "old", "openai", 40, 60, 100),
            ("usage-image", "imagegen", "image", 2, "active@example.com", None, timestamp, None, None, "image", "openai", 0, 0, 0),
            ("usage-disabled", "chat", "chat", 50, "disabled@example.com", None, timestamp, None, None, "old", "openai", 20, 30, 50),
            ("usage-deleted", "chat", "chat", 999, "deleted@example.com", None, timestamp, None, None, "old", "openai", 0, 0, 999),
            ("usage-global", "chat", "chat", 999, None, None, timestamp, None, None, "old", "openai", 0, 0, 999),
        ],
    )
    credentials = [
        ("openai", "OpenAI", "openai", "gpt-5.5", "openai", "https://old", SECRETS["openai"], "global", "", 1, timestamp, timestamp),
        ("deepseek", "DeepSeek", "deepseek", "deepseek-v4-pro", "openai", "https://old", SECRETS["deepseek"], "global", "", 1, timestamp, timestamp),
        ("gemini", "Gemini", "gemini", "gemini-3.1-pro-preview", "gemini", "https://old", SECRETS["gemini"], "global", "", 1, timestamp, timestamp),
        ("doubao", "Doubao", "doubao", "doubao-seed-2-0-pro-260215", "doubao", "https://old", SECRETS["doubao"], "global", "", 1, timestamp, timestamp),
        ("scoped", "Scoped", "deepseek", "deepseek-private", "openai", "https://old", "sk-scoped-secret", "user", "active", 1, timestamp, timestamp),
    ]
    if include_image:
        credentials.append(
            ("image", "Image", "openai", "gpt-image-2-pro", "image", "https://old", SECRETS["image"], "global", "", 1, timestamp, timestamp)
        )
    if duplicate_deepseek:
        credentials.append(
            ("deepseek-duplicate", "DeepSeek 2", "deepseek", "deepseek-v4-pro", "openai", "https://old", "sk-duplicate-secret", "global", "", 1, timestamp, timestamp)
        )
    connection.executemany(
        "INSERT INTO model_credentials VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", credentials
    )
    connection.execute(
        "INSERT INTO messages VALUES('private-chat','DO NOT IMPORT THIS CHAT')"
    )
    connection.commit()
    connection.close()


def target_database(path: Path) -> None:
    AdminManagementSchemaManager(path).migrate()


def test_copy_on_write_import_preserves_users_usage_and_model_secrets(tmp_path: Path) -> None:
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "v1.sqlite3"
    legacy_database(source)
    target_database(target)
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    report = import_v0292_admin_management(
        source, target, encryption_key=KEY, as_of=NOW
    )
    after = hashlib.sha256(source.read_bytes()).hexdigest()
    assert before == after
    assert report.users_imported == 2
    assert report.active_users == 1
    assert report.suspended_users == 1
    assert report.excluded_deleted_users == 1
    assert report.excluded_unsupported_users == 1
    assert report.eligible_sessions == 1
    assert report.excluded_revoked_sessions == 1
    assert report.excluded_expired_sessions == 1
    assert report.model_slots_imported == 7
    serialized = json.dumps(report.to_dict(), sort_keys=True)
    assert "DO NOT IMPORT" not in serialized
    assert all(secret not in serialized for secret in SECRETS.values())

    repository = AdminManagementRepository(target, encryption_key=KEY)
    active = repository.list_users(query="Active").items[0]
    assert (active.token_limit, active.tokens_used, active.images_used) == (1000, 100, 2)
    suspended = repository.list_users(query="Disabled").items[0]
    assert suspended.status == "suspended"
    assert (suspended.token_limit, suspended.tokens_used) == (2000, 50)
    models = repository.list_model_configurations()
    by_slot = {(item.draft or item.active).local_model_id: item for item in models}
    main = by_slot["ecorex-chat"].draft
    assert main is not None and main.upstream_model_id == "gpt-5.6-luna"
    sol = by_slot["ecorex-gpt-5.6-sol"].draft
    assert sol is not None and sol.upstream_model_id == "gpt-5.6-sol"
    assert main.enabled is False
    assert main.test_status == "failed"
    assert main.test_error_code == "rotation_required"
    for slot in ("ecorex-chat", "ecorex-gpt-5.6-sol", "ecorex-gemini-3.1-pro", "gpt-image-2", "gpt-image-2-edit"):
        draft = by_slot[slot].draft
        assert draft is not None and draft.enabled is False
        assert draft.test_error_code == "rotation_required"
        with pytest.raises(AdminManagementConflict, match="rotation"):
            repository.begin_model_test(
                by_slot[slot].config_id,
                1,
                actor=ACTOR,
                client_request_id=f"blocked-unrotated-{slot}",
            )
    deepseek_lease = repository.begin_model_test(
        by_slot["ecorex-deepseek-v4-pro"].config_id,
        1,
        actor=ACTOR,
        client_request_id="verify-safe-https-key",
    )
    assert deepseek_lease.configuration.api_key == SECRETS["deepseek"]
    assert b"DO NOT IMPORT THIS CHAT" not in target.read_bytes()
    assert all(secret.encode() not in target.read_bytes() for secret in SECRETS.values())
    repository.verify_integrity()


def test_dry_run_idempotency_and_conflict_are_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "v1.sqlite3"
    legacy_database(source)
    target_database(target)
    dry = import_v0292_admin_management(
        source, target, encryption_key=None, dry_run=True, as_of=NOW
    )
    assert dry.dry_run is True
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM admin_ops_users").fetchone()[0] == 0

    first = import_v0292_admin_management(source, target, encryption_key=KEY, as_of=NOW)
    repeated = import_v0292_admin_management(source, target, encryption_key=KEY, as_of=NOW)
    assert first.import_receipt_sha256 == repeated.import_receipt_sha256
    assert repeated.already_imported is True

    conflict_target = tmp_path / "conflict.sqlite3"
    target_database(conflict_target)
    with sqlite3.connect(conflict_target) as connection:
        connection.execute(
            "INSERT INTO admin_ops_users VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("existing", "Existing", "existing@example.com", None, "active", 0, 0, 0, 0, 1, NOW.isoformat(), NOW.isoformat(), 1),
        )
    with pytest.raises(LegacyAdminManagementImportError, match="business data"):
        import_v0292_admin_management(
            source, conflict_target, encryption_key=KEY, as_of=NOW
        )


def test_four_chat_rows_reuse_openai_key_for_missing_image_slots(tmp_path: Path) -> None:
    source = tmp_path / "legacy-four-chat.sqlite3"
    target = tmp_path / "v1.sqlite3"
    legacy_database(source, include_image=False)
    target_database(target)
    report = import_v0292_admin_management(
        source, target, encryption_key=KEY, as_of=NOW
    )
    assert report.model_slots_imported == 7
    repository = AdminManagementRepository(target, encryption_key=KEY)
    models = repository.list_model_configurations()
    by_slot = {(item.draft or item.active).local_model_id: item for item in models}
    assert {"gpt-image-2", "gpt-image-2-edit"} <= set(by_slot)
    for slot in ("gpt-image-2", "gpt-image-2-edit"):
        draft = by_slot[slot].draft
        assert draft is not None
        assert draft.enabled is False
        assert draft.test_error_code == "rotation_required"
        with pytest.raises(AdminManagementConflict, match="rotation"):
            repository.begin_model_test(
                by_slot[slot].config_id,
                1,
                actor=ACTOR,
                client_request_id=f"blocked-fallback-image-key-{slot}",
            )


def test_duplicate_legacy_slot_rolls_back_atomically(tmp_path: Path) -> None:
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "v1.sqlite3"
    legacy_database(source, duplicate_deepseek=True)
    target_database(target)
    with pytest.raises(LegacyAdminManagementImportError, match="more than once"):
        import_v0292_admin_management(source, target, encryption_key=KEY, as_of=NOW)
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM admin_ops_users").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM admin_ops_model_configs").fetchone()[0] == 0


def test_cli_uses_environment_key_and_emits_secret_free_summary(tmp_path: Path) -> None:
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "v1.sqlite3"
    legacy_database(source)
    target_database(target)
    environment = {
        **__import__("os").environ,
        "TEST_V1_MANAGEMENT_KEY": base64.b64encode(KEY).decode("ascii"),
    }
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/migrate-v0292-admin-management.py",
            "--source",
            str(source),
            "--target",
            str(target),
            "--encryption-key-env",
            "TEST_V1_MANAGEMENT_KEY",
            "--as-of",
            NOW.isoformat(),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["status"] == "ok"
    combined = completed.stdout + completed.stderr
    assert "DO NOT IMPORT" not in combined
    assert all(secret not in combined for secret in SECRETS.values())
    assert base64.b64encode(KEY).decode("ascii") not in combined
