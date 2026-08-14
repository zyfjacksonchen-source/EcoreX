from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import inspect
import sqlite3

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from ecorex.protocol import PermissionSnapshot
from ecorex.runtime import RuntimeComposition, RuntimeSettings, create_app
from ecorex.runtime.errors import ConflictError, SchemaVersionError
from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.permissions import PermissionAuthority, PermissionIntegrityError
from ecorex.runtime.schema_catalog import product_schema_inventory
from ecorex.runtime.schema_fragments.permissions import PERMISSIONS_SCHEMA_FRAGMENT


RUNTIME_TOKEN = "r" * 43
CSRF_TOKEN = "c" * 43


def _settings(tmp_path, **updates) -> RuntimeSettings:
    values = {
        "database_path": tmp_path / "runtime.db",
        "runtime_bearer_token": RUNTIME_TOKEN,
        "csrf_token": CSRF_TOKEN,
        "webui_origins": ("http://testserver",),
    }
    values.update(updates)
    return RuntimeSettings(**values)


def _headers() -> tuple[dict[str, str], dict[str, str]]:
    auth = {"Authorization": f"Bearer {RUNTIME_TOKEN}"}
    mutation = {
        **auth,
        "Origin": "http://testserver",
        "X-EcoreX-CSRF": CSRF_TOKEN,
    }
    return auth, mutation


def _permission_schema_records(path) -> tuple[tuple[str, str], ...]:
    names = PERMISSIONS_SCHEMA_FRAGMENT.object_names
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


def test_permission_schema_fragment_is_static_product_inventory() -> None:
    assert dict(product_schema_inventory())[
        PERMISSIONS_SCHEMA_FRAGMENT.fragment_id
    ] == PERMISSIONS_SCHEMA_FRAGMENT.object_names


def test_product_has_one_cowagent_runtime_boundary_and_no_permission_setting(
    tmp_path,
) -> None:
    from ecorex.server.app import ProductServerSettings

    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    auth, mutation = _headers()

    permissions = client.get("/api/v1/bootstrap", headers=auth).json()["permissions"]
    assert permissions["profile"] == "full_access"
    assert permissions["full_access"] is True
    assert permissions["sandbox"] == "danger-full-access"
    assert permissions["approval"] == "never"
    assert client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "default",
            "expected_revision": permissions["revision"],
            "client_request_id": "removed-permission-setting",
        },
        headers=mutation,
    ).status_code == 404

    assert "full_access" not in ProductServerSettings.__dataclass_fields__
    assert "admin_hard_denies" not in ProductServerSettings.__dataclass_fields__
    assert "enforce_admin_tool_denies" not in ProductServerSettings.__dataclass_fields__
    assert "enforce_admin_tool_denies" not in inspect.signature(
        RuntimeComposition
    ).parameters
    assert (
        "capability_sandbox_profile_availability"
        not in RuntimeSettings.__dataclass_fields__
    )

    restarted = TestClient(create_app(settings=_settings(tmp_path)))
    assert (
        restarted.get("/api/v1/bootstrap", headers=auth).json()["permissions"]
        == permissions
    )


def test_two_authorities_cannot_commit_the_same_expected_revision(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    first = PermissionAuthority(
        path, account_id="local-user", initial_full_access=False
    )
    second = PermissionAuthority(
        path, account_id="local-user", initial_full_access=False
    )

    def mutate(authority: PermissionAuthority, request_id: str):
        try:
            return authority.update(
                "full_access",
                expected_revision=1,
                client_request_id=request_id,
            )
        except ConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda pair: mutate(*pair),
                ((first, "concurrent-one"), (second, "concurrent-two")),
            )
        )
    snapshots = [result for result in results if isinstance(result, PermissionSnapshot)]
    conflicts = [result for result in results if isinstance(result, ConflictError)]
    assert len(snapshots) == 1
    assert len(conflicts) == 1
    assert first.current().profile == "full_access"
    assert first.current().revision == 2


def test_permission_sqlite_edit_is_rejected_or_detected_fail_closed(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    authority = PermissionAuthority(
        path, account_id="local-user", initial_full_access=False
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA recursive_triggers = ON")
        with pytest.raises(sqlite3.IntegrityError, match="ledger-backed"):
            connection.execute(
                "UPDATE runtime_permission_state "
                "SET profile = 'full_access', revision = 2, "
                "updated_at = '2099-01-01T00:00:00+00:00', state_digest = ? "
                "WHERE account_id = 'local-user'",
                ("0" * 64,),
            )

    # Even if a local database editor removes the first-line trigger, the next
    # authority read verifies the append-only chain and refuses the forged row.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER runtime_permission_state_guard_update")
        connection.execute(
            "UPDATE runtime_permission_state SET profile = 'full_access' "
            "WHERE account_id = 'local-user'"
        )
        connection.commit()
    with pytest.raises(PermissionIntegrityError, match="does not match"):
        authority.current()


def test_permission_schema_tamper_is_rejected_without_startup_repair(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    authority = PermissionAuthority(
        path, account_id="local-user", initial_full_access=False
    )
    expected = authority.current()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TRIGGER runtime_permission_state_guard_update;
            CREATE TRIGGER runtime_permission_state_guard_update
            BEFORE UPDATE ON runtime_permission_state
            BEGIN
                SELECT 1;
            END;
            """
        )
    tampered = _permission_schema_records(path)

    with pytest.raises(SchemaVersionError, match="runtime-permissions is incompatible"):
        PermissionAuthority(path, account_id="local-user", initial_full_access=False)

    assert _permission_schema_records(path) == tampered
    assert authority.current() == expected


def test_permission_audit_tamper_is_detected_by_internal_authority(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    authority = PermissionAuthority(
        path, account_id="local-user", initial_full_access=False
    )
    authority.update(
        "full_access",
        expected_revision=1,
        client_request_id="audited-change",
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER permission_change_requests_no_update")
        connection.execute(
            "UPDATE permission_change_requests SET response_json = '{}' "
            "WHERE client_request_id = 'audited-change'"
        )
        connection.commit()
    with pytest.raises(PermissionIntegrityError, match="audit digest"):
        authority.current()


def test_permission_snapshot_rejects_semantic_or_digest_forgery() -> None:
    issued = PermissionSnapshot.issue(
        profile="default",
        revision=1,
        updated_at=datetime.now(UTC),
        admin_hard_denies=["shell"],
    )
    with pytest.raises(ValidationError, match="profile and full_access disagree"):
        PermissionSnapshot.model_validate(
            {**issued.model_dump(mode="python"), "full_access": True}
        )
    with pytest.raises(ValidationError, match="snapshot digest is invalid"):
        PermissionSnapshot.model_validate(
            {**issued.model_dump(mode="python"), "revision": 2}
        )


def test_preledger_permission_state_requires_signed_migration_without_repair(
    tmp_path,
) -> None:
    path = tmp_path / "runtime.db"
    SQLiteDatabase(path)
    updated_at = "2026-07-10T08:00:00Z"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE runtime_permission_state;
            DROP TABLE permission_change_requests;
            DROP TABLE permission_state_ledger;

            CREATE TABLE runtime_permission_state (
                account_id TEXT PRIMARY KEY,
                profile TEXT NOT NULL CHECK (profile IN ('default', 'full_access')),
                revision INTEGER NOT NULL CHECK (revision > 0),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE permission_change_requests (
                account_id TEXT NOT NULL,
                client_request_id TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (account_id, client_request_id)
            );
            """
        )
        connection.execute(
            "INSERT INTO runtime_permission_state VALUES (?, 'full_access', 3, ?)",
            ("local-user", updated_at),
        )
    tampered_schema = _permission_schema_records(path)

    with pytest.raises(SchemaVersionError, match="product schema objects are missing"):
        PermissionAuthority(
            path,
            account_id="local-user",
            initial_full_access=False,
            admin_hard_denies=frozenset({"shell"}),
        )

    assert _permission_schema_records(path) == tampered_schema
    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(runtime_permission_state)"
            ).fetchall()
        }
        state = connection.execute(
            "SELECT profile, revision, updated_at FROM runtime_permission_state "
            "WHERE account_id = 'local-user'"
        ).fetchone()
        ledger = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' "
            "AND name = 'permission_state_ledger'"
        ).fetchone()
    assert "state_digest" not in columns
    assert state == ("full_access", 3, updated_at)
    assert ledger is None
