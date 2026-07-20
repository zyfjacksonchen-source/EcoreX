from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import sqlite3
import threading

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from ecorex.protocol import CreateTurnRequest, PermissionSnapshot
from ecorex.runtime import RuntimeSettings, RuntimeSnapshotStale, create_app
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


def test_permission_profile_is_persistent_idempotent_and_only_affects_future_turns(
    tmp_path,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    auth, mutation = _headers()
    initial = client.get("/api/v1/bootstrap", headers=auth).json()["permissions"]
    assert initial["full_access"] is False

    thread_id = client.post(
        "/api/v1/threads", json={"title": "permissions"}, headers=mutation
    ).json()["thread_id"]
    first = client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={"input": "run shell", "client_message_id": "permission-1"},
        headers=mutation,
    ).json()

    request = {
        "profile": "full_access",
        "expected_revision": initial["revision"],
        "client_request_id": "profile-change-1",
    }
    changed = client.put(
        "/api/v1/settings/permissions", json=request, headers=mutation
    )
    assert changed.status_code == 200
    full = changed.json()["permissions"]
    assert full["full_access"] is True
    assert full["sandbox"] == "danger-full-access"
    assert full["approval"] == "never"
    assert full["snapshot_id"] != initial["snapshot_id"]
    assert client.put(
        "/api/v1/settings/permissions", json=request, headers=mutation
    ).json()["permissions"] == full
    conflict = client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "default",
            "expected_revision": initial["revision"],
            "client_request_id": "profile-change-1",
        },
        headers=mutation,
    )
    assert conflict.status_code == 409

    second = client.post(
        f"/api/v1/threads/{thread_id}/queue",
        json={"input": "run shell", "client_message_id": "permission-2"},
        headers=mutation,
    ).json()
    events = app.state.runtime.events.page(thread_id).events
    accepted = {
        event.turn_id: event
        for event in events
        if event.event_type == "turn.accepted"
    }
    assert accepted[first["turn"]["turn_id"]].permission_snapshot_id == initial["snapshot_id"]
    assert accepted[second["turn"]["turn_id"]].permission_snapshot_id == full["snapshot_id"]
    first_plan = app.state.runtime_composition.capability_service.get_plan(
        accepted[first["turn"]["turn_id"]].capability_snapshot_id
    )
    second_plan = app.state.runtime_composition.capability_service.get_plan(
        accepted[second["turn"]["turn_id"]].capability_snapshot_id
    )
    assert first_plan.decision("shell").requires_approval is True
    assert second_plan.decision("shell").requires_approval is False

    restarted = TestClient(create_app(settings=_settings(tmp_path)))
    persisted = restarted.get("/api/v1/bootstrap", headers=auth).json()["permissions"]
    assert persisted == full


@pytest.mark.parametrize("operation", ["create", "queue", "replace"])
def test_permission_update_linearizes_with_every_http_turn_acceptance(
    tmp_path,
    monkeypatch,
    operation,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    auth, mutation = _headers()
    initial = client.get("/api/v1/bootstrap", headers=auth).json()["permissions"]
    thread_id = client.post(
        "/api/v1/threads",
        json={"title": f"linearize-{operation}"},
        headers=mutation,
    ).json()["thread_id"]
    source_turn_id = None
    if operation == "replace":
        source_turn_id = client.post(
            f"/api/v1/threads/{thread_id}/turns",
            json={
                "input": "source",
                "client_message_id": "linearize-replace-source",
            },
            headers=mutation,
        ).json()["turn"]["turn_id"]

    composition = app.state.runtime_composition
    original_prepare = composition.prepare_turn
    prepared = threading.Event()
    release = threading.Event()
    target_message_id = f"linearize-{operation}-old"

    def pause_after_permission_capture(request):
        result = original_prepare(request)
        if request.client_message_id == target_message_id:
            prepared.set()
            assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(composition, "prepare_turn", pause_after_permission_capture)

    def submit_turn():
        payload = {
            "input": f"{operation} under old permission",
            "client_message_id": target_message_id,
        }
        if operation == "create":
            path = f"/api/v1/threads/{thread_id}/turns"
        elif operation == "queue":
            path = f"/api/v1/threads/{thread_id}/queue"
        else:
            assert source_turn_id is not None
            path = f"/api/v1/turns/{source_turn_id}/replace"
            payload["reason"] = "permission linearization"
        return client.post(path, json=payload, headers=mutation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        turn_future = executor.submit(submit_turn)
        assert prepared.wait(timeout=5)
        update_future = executor.submit(
            client.put,
            "/api/v1/settings/permissions",
            json={
                "profile": "full_access",
                "expected_revision": initial["revision"],
                "client_request_id": f"linearize-{operation}-permission",
            },
            headers=mutation,
        )
        # The update cannot return 200 while accepted persistence still owns
        # the shared permission admission.
        assert not update_future.done()
        threading.Event().wait(0.1)
        assert not update_future.done()
        release.set()
        turn_response = turn_future.result(timeout=5)
        update_response = update_future.result(timeout=5)

    assert turn_response.status_code == 202
    assert update_response.status_code == 200
    changed = update_response.json()["permissions"]
    assert changed["snapshot_id"] != initial["snapshot_id"]
    body = turn_response.json()
    accepted_turn_id = (
        body["replacement_turn"]["turn_id"]
        if operation == "replace"
        else body["turn"]["turn_id"]
    )
    old_event = next(
        event
        for event in app.state.runtime.events.page(thread_id, limit=1000).events
        if event.event_type == "turn.accepted" and event.turn_id == accepted_turn_id
    )
    assert old_event.permission_snapshot_id == initial["snapshot_id"]

    after_update = client.post(
        f"/api/v1/threads/{thread_id}/queue",
        json={
            "input": "must use new permission",
            "client_message_id": f"linearize-{operation}-new",
        },
        headers=mutation,
    )
    assert after_update.status_code == 202
    new_turn_id = after_update.json()["turn"]["turn_id"]
    new_event = next(
        event
        for event in app.state.runtime.events.page(thread_id, limit=1000).events
        if event.event_type == "turn.accepted" and event.turn_id == new_turn_id
    )
    assert new_event.permission_snapshot_id == changed["snapshot_id"]


def test_kernel_rejects_a_prepared_permission_after_another_writer_commits(
    tmp_path,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    auth, mutation = _headers()
    initial = client.get("/api/v1/bootstrap", headers=auth).json()["permissions"]
    thread_id = client.post(
        "/api/v1/threads",
        json={"title": "cross-process permission fence"},
        headers=mutation,
    ).json()["thread_id"]
    request = CreateTurnRequest(
        input="prepared before permission commit",
        client_message_id="cross-process-stale-turn",
    )
    prepared = app.state.runtime_composition.prepare_turn(request)

    changed = client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "full_access",
            "expected_revision": initial["revision"],
            "client_request_id": "cross-process-permission-update",
        },
        headers=mutation,
    )
    assert changed.status_code == 200

    with pytest.raises(RuntimeSnapshotStale, match="no longer current"):
        app.state.runtime.create_turn(
            thread_id,
            prepared.request,
            snapshot_context=prepared.snapshot_context,
            permission_account_id=(
                app.state.runtime_composition.permission_account_id
            ),
        )
    assert not any(
        event.event_type == "turn.accepted"
        and event.permission_snapshot_id == initial["snapshot_id"]
        for event in app.state.runtime.events.page(thread_id, limit=1000).events
    )


def test_permission_snapshot_and_audit_failure_roll_back_one_authority_unit(
    tmp_path,
    monkeypatch,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app, raise_server_exceptions=False)
    auth, mutation = _headers()
    initial = client.get("/api/v1/bootstrap", headers=auth).json()["permissions"]

    def reject_audit(*_args, **_kwargs):
        raise RuntimeError("injected permission audit failure")

    monkeypatch.setattr(
        app.state.audit_outbox,
        "_persist_view_in_transaction",
        reject_audit,
    )
    response = client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "full_access",
            "expected_revision": initial["revision"],
            "client_request_id": "permission-atomic-failure",
        },
        headers=mutation,
    )

    assert response.status_code == 500
    assert app.state.permission_authority.current().model_dump(mode="json") == initial
    assert app.state.runtime_composition.permission_snapshot.snapshot_id == initial[
        "snapshot_id"
    ]
    with app.state.runtime.database.reader() as connection:
        assert connection.execute(
            "SELECT 1 FROM permission_change_requests "
            "WHERE client_request_id='permission-atomic-failure'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM runtime_snapshots WHERE snapshot_id != ? "
            "AND kind='permission'",
            (initial["snapshot_id"],),
        ).fetchone() is None


def test_full_access_keeps_admin_audit_denies_without_blocking_local_tools(tmp_path) -> None:
    app = create_app(
        settings=_settings(
            tmp_path,
            admin_hard_denies=[" SHELL "],
            installed_capability_packs=frozenset({"sandbox"}),
            capability_handlers={"shell": lambda arguments, context: {"exit_code": 0}},
        )
    )
    client = TestClient(app)
    auth, mutation = _headers()
    client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "full_access",
            "expected_revision": 1,
            "client_request_id": "enable-full",
        },
        headers=mutation,
    )
    thread_id = client.post(
        "/api/v1/threads", json={"title": "hard deny"}, headers=mutation
    ).json()["thread_id"]
    created = client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={"input": "run shell", "client_message_id": "hard-deny"},
        headers=mutation,
    ).json()
    accepted = next(
        event
        for event in app.state.runtime.events.page(thread_id).events
        if event.turn_id == created["turn"]["turn_id"]
        and event.event_type == "turn.accepted"
    )
    plan = app.state.runtime_composition.capability_service.get_plan(
        accepted.capability_snapshot_id
    )
    decision = plan.decision("shell")
    assert decision.eligible is True
    assert "admin_hard_deny" not in decision.reason_codes
    full_revision = client.get(
        "/api/v1/bootstrap", headers=auth
    ).json()["permissions"]["revision"]
    weakened = PermissionSnapshot.issue(
        profile="full_access",
        revision=full_revision,
        updated_at=datetime.now(UTC),
        admin_hard_denies=[],
    )
    assert full_revision == 2
    with pytest.raises(ValueError, match="cannot weaken"):
        app.state.runtime_composition.record_permission(weakened)

    revoked = client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "default",
            "expected_revision": 2,
            "client_request_id": "revoke-full",
        },
        headers=mutation,
    ).json()["permissions"]
    assert revoked["full_access"] is False
    assert revoked["admin_hard_denies"] == ["shell"]
    delayed_retry = client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "full_access",
            "expected_revision": 1,
            "client_request_id": "enable-full",
        },
        headers=mutation,
    ).json()["permissions"]
    assert delayed_retry == revoked
    assert client.get("/api/v1/bootstrap", headers=auth).json()["permissions"] == revoked


def test_permission_revision_prevents_lost_updates_and_bootstrap_stays_dynamic(
    tmp_path,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    auth, mutation = _headers()
    initial = client.get("/api/v1/bootstrap", headers=auth).json()["permissions"]

    first = client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "full_access",
            "expected_revision": initial["revision"],
            "client_request_id": "first-writer",
        },
        headers=mutation,
    )
    assert first.status_code == 200
    assert first.json()["permissions"]["revision"] == initial["revision"] + 1

    stale = client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "default",
            "expected_revision": initial["revision"],
            "client_request_id": "stale-writer",
        },
        headers=mutation,
    )
    assert stale.status_code == 409
    current = client.get("/api/v1/bootstrap", headers=auth).json()["permissions"]
    assert current == first.json()["permissions"]


def test_permission_mutation_requires_bearer_origin_csrf_and_revision(tmp_path) -> None:
    client = TestClient(create_app(settings=_settings(tmp_path)))
    auth, mutation = _headers()
    payload = {
        "profile": "full_access",
        "expected_revision": 1,
        "client_request_id": "security-boundary",
    }
    assert client.put("/api/v1/settings/permissions", json=payload).status_code == 401
    assert client.put(
        "/api/v1/settings/permissions", json=payload, headers=auth
    ).status_code == 403
    assert client.put(
        "/api/v1/settings/permissions",
        json=payload,
        headers={**mutation, "X-EcoreX-CSRF": "wrong" * 10},
    ).status_code == 403
    assert client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "full_access",
            "client_request_id": "missing-revision",
        },
        headers=mutation,
    ).status_code == 422


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


def test_permission_audit_tamper_is_detected_on_bootstrap(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    auth, mutation = _headers()
    client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "full_access",
            "expected_revision": 1,
            "client_request_id": "audited-change",
        },
        headers=mutation,
    ).raise_for_status()
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        connection.execute("DROP TRIGGER permission_change_requests_no_update")
        connection.execute(
            "UPDATE permission_change_requests SET response_json = '{}' "
            "WHERE client_request_id = 'audited-change'"
        )
        connection.commit()
    with pytest.raises(PermissionIntegrityError, match="audit digest"):
        app.state.permission_authority.current()


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


def test_admin_policy_restart_changes_only_future_turn_snapshots(tmp_path) -> None:
    pack_settings = {
        "installed_capability_packs": frozenset({"sandbox"}),
        "capability_handlers": {
            "shell": lambda arguments, context: {"exit_code": 0}
        },
    }
    first_app = create_app(settings=_settings(tmp_path, **pack_settings))
    first_client = TestClient(first_app)
    auth, mutation = _headers()
    initial_permission = first_client.get(
        "/api/v1/bootstrap", headers=auth
    ).json()["permissions"]
    thread_id = first_client.post(
        "/api/v1/threads", json={"title": "admin policy"}, headers=mutation
    ).json()["thread_id"]
    first_turn = first_client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={"input": "run shell", "client_message_id": "before-admin-policy"},
        headers=mutation,
    ).json()["turn"]["turn_id"]

    second_app = create_app(
        settings=_settings(
            tmp_path,
            admin_hard_denies=["shell"],
            **pack_settings,
        )
    )
    second_client = TestClient(second_app)
    changed_permission = second_client.get(
        "/api/v1/bootstrap", headers=auth
    ).json()["permissions"]
    assert changed_permission["revision"] == initial_permission["revision"]
    assert changed_permission["snapshot_id"] != initial_permission["snapshot_id"]
    second_turn = second_client.post(
        f"/api/v1/threads/{thread_id}/queue",
        json={"input": "run shell", "client_message_id": "after-admin-policy"},
        headers=mutation,
    ).json()["turn"]["turn_id"]

    events = second_app.state.runtime.events.page(thread_id).events
    accepted = {
        event.turn_id: event
        for event in events
        if event.event_type == "turn.accepted"
    }
    assert accepted[first_turn].permission_snapshot_id == initial_permission["snapshot_id"]
    assert accepted[second_turn].permission_snapshot_id == changed_permission["snapshot_id"]
    first_plan = second_app.state.runtime_composition.capability_service.get_plan(
        accepted[first_turn].capability_snapshot_id
    )
    second_plan = second_app.state.runtime_composition.capability_service.get_plan(
        accepted[second_turn].capability_snapshot_id
    )
    assert first_plan.decision("shell").eligible is True
    assert second_plan.decision("shell").eligible is True
