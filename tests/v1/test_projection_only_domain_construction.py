from __future__ import annotations

from datetime import UTC, datetime
import sqlite3
from typing import Any

import pytest

from ecorex.connectors.composition import build_connector_composition
from ecorex.connectors.service import ConnectorService
from ecorex.extensions import ExtensionService, SQLiteExtensionRepository
from ecorex.memory import MemoryService
from ecorex.runtime import SQLiteDatabase
from ecorex.runtime.permissions import PermissionAuthority, PermissionIntegrityError


def _table_rows(
    database: SQLiteDatabase,
    *,
    prefixes: tuple[str, ...] = (),
    names: tuple[str, ...] = (),
) -> tuple[tuple[str, tuple[tuple[Any, ...], ...]], ...]:
    with database.reader() as connection:
        available = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        selected = sorted(
            table
            for table in available
            if table in names or any(table.startswith(prefix) for prefix in prefixes)
        )
        snapshot: list[tuple[str, tuple[tuple[Any, ...], ...]]] = []
        for table in selected:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({quoted})").fetchall()
            ]
            ordering = ",".join(
                '"' + column.replace('"', '""') + '"' for column in columns
            )
            rows = connection.execute(
                f"SELECT * FROM {quoted} ORDER BY {ordering}"
            ).fetchall()
            snapshot.append((table, tuple(tuple(row) for row in rows)))
    return tuple(snapshot)


def test_permission_projection_only_is_deterministic_and_converges_explicitly(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    tables = (
        "permission_change_requests",
        "permission_state_ledger",
        "runtime_permission_state",
    )
    before = _table_rows(database, names=tables)

    authority = PermissionAuthority(
        database,
        account_id="local-user",
        initial_full_access=False,
        admin_hard_denies=frozenset({"shell"}),
        initialize=False,
    )
    first = authority.current()
    second = PermissionAuthority(
        database,
        account_id="local-user",
        initial_full_access=False,
        admin_hard_denies=frozenset({"shell"}),
        initialize=False,
    ).current()

    assert first == second
    assert first.revision == 1
    assert first.updated_at == datetime(1970, 1, 1, tzinfo=UTC)
    assert authority.current_state_digest() == authority.current_state_digest()
    with pytest.raises(PermissionIntegrityError, match="state is missing"):
        authority.update(
            "full_access",
            expected_revision=1,
            client_request_id="projection-only-mutation",
        )
    assert _table_rows(database, names=tables) == before

    persisted = authority.converge_startup()
    assert persisted.profile == "default"
    converged = _table_rows(database, names=tables)
    assert converged != before
    assert authority.current() == persisted
    reader = PermissionAuthority(
        database,
        account_id="local-user",
        initial_full_access=True,
        admin_hard_denies=frozenset({"shell"}),
        initialize=False,
    )
    assert reader.current() == persisted
    assert _table_rows(database, names=tables) == converged
    authority.converge_startup()
    assert _table_rows(database, names=tables) == converged


class _UntouchedVault:
    def get(self, _reference: str) -> dict[str, str]:
        raise AssertionError("projection-only Connector construction touched the vault")

    def put(self, _reference: str, _material: dict[str, str]) -> None:
        raise AssertionError("projection-only Connector construction touched the vault")

    def delete(self, _reference: str) -> None:
        raise AssertionError("projection-only Connector construction touched the vault")


def test_connector_projection_only_skips_catalog_sync_and_recovery_then_converges(
    tmp_path,
    monkeypatch,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    before = _table_rows(database, prefixes=("connector_",))
    recovery_calls = 0
    original_recovery = ConnectorService._recover_transitional_state

    def observe_recovery(service: ConnectorService) -> None:
        nonlocal recovery_calls
        recovery_calls += 1
        original_recovery(service)

    monkeypatch.setattr(
        ConnectorService,
        "_recover_transitional_state",
        observe_recovery,
    )

    composition = build_connector_composition(
        database_path=database.path,
        oauth_return_uri=(
            "http://127.0.0.1:8765/api/v1/connectors/oauth/callback"
        ),
        vault=_UntouchedVault(),
        initialize=False,
    )
    assert composition.service.catalog()
    assert recovery_calls == 0
    assert _table_rows(database, prefixes=("connector_",)) == before

    composition.service.converge_startup()
    assert recovery_calls == 1
    converged = _table_rows(database, prefixes=("connector_",))
    assert converged != before
    with database.reader() as connection:
        assert connection.execute(
            "SELECT schema_version FROM connector_schema WHERE singleton=1"
        ).fetchone() is not None
        assert int(
            connection.execute("SELECT COUNT(*) FROM connector_definitions").fetchone()[0]
        ) > 0
    composition.service.converge_startup()
    assert recovery_calls == 1
    assert _table_rows(database, prefixes=("connector_",)) == converged


def test_extension_projection_only_validates_without_meta_then_converges(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    before = _table_rows(database, prefixes=("extension_",))

    repository = SQLiteExtensionRepository(database, initialize=False)
    service = ExtensionService(
        repository,
        runtime_api_version="1.0.0",
        platform="win32",
        architecture="x86_64",
    )
    assert service.project_snapshot().items == ()
    assert _table_rows(database, prefixes=("extension_",)) == before

    repository.converge_startup()
    converged = _table_rows(database, prefixes=("extension_",))
    assert converged != before
    repository.converge_startup()
    assert _table_rows(database, prefixes=("extension_",)) == converged


def test_memory_projection_only_uses_virtual_revision_then_converges(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    before = _table_rows(database, prefixes=("memory_",))

    service = MemoryService(database, initialize=False)
    assert service.snapshot().revision == 0
    assert _table_rows(database, prefixes=("memory_",)) == before

    service.converge_startup()
    converged = _table_rows(database, prefixes=("memory_",))
    assert converged != before
    assert service.snapshot().revision == 0
    service.converge_startup()
    assert _table_rows(database, prefixes=("memory_",)) == converged
