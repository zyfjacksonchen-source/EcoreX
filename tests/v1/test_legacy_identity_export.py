from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3
import subprocess
import sys

from ecorex.migration import export_v0292_legacy_identities


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _database(path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users(
          id TEXT PRIMARY KEY,name TEXT NOT NULL,email TEXT NOT NULL,role TEXT NOT NULL,
          status TEXT NOT NULL,daily_token_limit INTEGER NOT NULL,
          weekly_token_limit INTEGER NOT NULL,deleted_at TEXT
        );
        CREATE TABLE client_sessions(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,token_hash TEXT NOT NULL,
          expires_at TEXT NOT NULL,revoked_at TEXT
        );
        CREATE TABLE messages(id TEXT PRIMARY KEY,content TEXT NOT NULL);
        """
    )
    users = [
        ("active", "Active", "active@example.com", "member", "active", 10, 20, None),
        (
            "deleted",
            "Deleted",
            "deleted@example.com",
            "member",
            "active",
            0,
            0,
            NOW.isoformat(),
        ),
        (
            "disabled",
            "Disabled",
            "disabled@example.com",
            "member",
            "disabled",
            0,
            0,
            None,
        ),
    ]
    connection.executemany("INSERT INTO users VALUES(?,?,?,?,?,?,?,?)", users)
    future = (NOW + timedelta(days=1)).isoformat()
    past = (NOW - timedelta(seconds=1)).isoformat()
    connection.executemany(
        "INSERT INTO client_sessions VALUES(?,?,?,?,?)",
        [
            ("eligible", "active", "a" * 64, future, None),
            ("revoked", "active", "b" * 64, future, NOW.isoformat()),
            ("expired", "active", "c" * 64, past, None),
            ("deleted-user", "deleted", "d" * 64, future, None),
            ("disabled-user", "disabled", "e" * 64, future, None),
        ],
    )
    connection.execute(
        "INSERT INTO messages VALUES('deleted-chat','TOP SECRET CHAT CONTENT')"
    )
    connection.commit()
    connection.close()


def test_export_is_read_only_excludes_ineligible_and_never_reads_chat(tmp_path) -> None:
    database = tmp_path / "admin.db"
    _database(database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    records, report = export_v0292_legacy_identities(database, as_of=NOW)
    after = hashlib.sha256(database.read_bytes()).hexdigest()
    assert before == after
    assert len(records) == 1
    assert records[0]["account_id"] == "active"
    assert records[0]["credential_sha256"] == "a" * 64
    assert "TOP SECRET" not in json.dumps(records)
    assert report.eligible_sessions == 1
    assert report.excluded_deleted_users == 1
    assert report.excluded_disabled_users == 1
    assert report.excluded_revoked_sessions == 1
    assert report.excluded_expired_sessions == 1


def test_export_cli_dry_run_emits_only_summary(tmp_path) -> None:
    database = tmp_path / "admin.db"
    _database(database)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/export-v0292-legacy-identities.py",
            "--database",
            str(database),
            "--as-of",
            NOW.isoformat(),
            "--dry-run",
        ],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    summary = json.loads(result.stdout)
    assert summary["eligible_sessions"] == 1
    assert "credential_sha256" not in result.stdout
    assert "TOP SECRET" not in result.stdout + result.stderr
