from __future__ import annotations

import ast
from io import BytesIO
from pathlib import Path
import sqlite3
import subprocess
import tarfile

import pytest

from ecorex.migration import TARGET_DATABASE_NAME, migrate_v030_to_v1
from ecorex.migration.legacy import V030_RELEASE_SCHEMA_COMMIT


_RELEASE_SCHEMA_PATHS = (
    "agent/memory/conversation_store.py",
    "agent/protocol/run_ledger.py",
    "agent/protocol/run_event_ledger.py",
)


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=check,
        capture_output=True,
    )


def _ddl(source: bytes) -> str:
    tree = ast.parse(source.decode("utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_DDL" for target in node.targets)
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise AssertionError("released source has no literal _DDL contract")


def _released_sources(repository: Path) -> dict[str, bytes]:
    available = _git(
        repository,
        "cat-file",
        "-e",
        f"{V030_RELEASE_SCHEMA_COMMIT}^{{commit}}",
        check=False,
    )
    if available.returncode != 0:
        pytest.skip("v0.3 release-schema commit is absent from this checkout")
    archive = _git(
        repository,
        "archive",
        "--format=tar",
        V030_RELEASE_SCHEMA_COMMIT,
        *_RELEASE_SCHEMA_PATHS,
    ).stdout
    result: dict[str, bytes] = {}
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            if member.isdir() and any(
                path.startswith(member.name.rstrip("/") + "/")
                for path in _RELEASE_SCHEMA_PATHS
            ):
                continue
            if member.name not in _RELEASE_SCHEMA_PATHS or not member.isfile():
                raise AssertionError("release schema archive contains an unexpected member")
            stream = bundle.extractfile(member)
            assert stream is not None
            result[member.name] = stream.read()
    assert set(result) == set(_RELEASE_SCHEMA_PATHS)
    return result


def test_released_v030_schema_archive_fixture_is_migratable(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    sources = _released_sources(repository)
    source = tmp_path / "legacy"
    database = source / "sessions" / "conversations.db"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    try:
        for path in _RELEASE_SCHEMA_PATHS:
            connection.executescript(_ddl(sources[path]))
        connection.execute(
            """
            INSERT INTO sessions(
                session_id, channel_type, title, title_locked, context_start_seq,
                project_id, project_name, project_path, project_memory_path,
                project_dreams_path, metadata_json, created_at, last_active, msg_count
            ) VALUES ('release-session', 'web', '真实发布结构', 0, 0, '', '', '', '', '',
                      '{}', 1700000000, 1700000002, 2)
            """
        )
        connection.executemany(
            """
            INSERT INTO messages(session_id, seq, role, content, created_at, extras)
            VALUES (?, ?, ?, ?, ?, '{}')
            """,
            (
                ("release-session", 1, "user", "迁移这条消息", 1700000001),
                ("release-session", 2, "assistant", "已经迁移", 1700000002),
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_runs(
                request_id, session_id, parent_id, run_type, status, phase,
                terminal_reason, error_code, error_message, model, provider,
                created_at, started_at, updated_at, terminal_at, lease_owner,
                lease_expires_at, metadata_json
            ) VALUES (
                'release-request', 'release-session', NULL, 'message', 'completed',
                'completed', 'completed', NULL, NULL, 'release-model', 'managed',
                1700000001, 1700000001, 1700000002, 1700000002, NULL, NULL, '{}'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_run_events(
                request_id, session_id, turn_id, event_seq, event_type,
                payload_json, idempotency_key, source, created_at
            ) VALUES (
                'release-request', 'release-session', 'release-request', 1,
                'run.accepted', '{}', 'release-request:accepted', 'runtime', 1700000001
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    target = tmp_path / "v1"
    report = migrate_v030_to_v1(source, target)

    assert report.status == "completed"
    assert report.source_evidence["baseline_release_schema_commit"] == (
        V030_RELEASE_SCHEMA_COMMIT
    )
    assert report.source_evidence["evidence_level"] == (
        "release_schema_compatible_unattested"
    )
    migrated = sqlite3.connect(target / TARGET_DATABASE_NAME)
    try:
        assert migrated.execute("SELECT COUNT(*) FROM threads").fetchone() == (1,)
        assert migrated.execute("SELECT COUNT(*) FROM legacy_run_records").fetchone() == (1,)
    finally:
        migrated.close()
