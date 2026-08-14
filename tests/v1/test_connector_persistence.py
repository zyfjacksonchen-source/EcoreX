from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

import pytest

from ecorex.connectors import (
    AuthChallenge,
    AuthGrant,
    ConnectorAuthError,
    ConnectorAuthKind,
    ConnectorHealth,
    ConnectorHealthResult,
    ConnectorIdempotencyConflict,
    ConnectorInvocationUncertain,
    ConnectorService,
    ConnectorUnavailable,
    InMemoryCredentialVault,
    SQLiteConnectorRepository,
    builtin_connector_registry,
)
from ecorex.runtime import SQLiteDatabase
from ecorex.runtime.errors import SchemaVersionError


class DurableFakeAdapter:
    def __init__(self) -> None:
        self.invocations: list[tuple[str, Mapping[str, Any], str | None]] = []
        self.auth_completions = 0
        self.invoke_started = threading.Event()
        self.invoke_release = threading.Event()
        self.block_invocation = False
        self.fail_invocation = False
        self.fail_health = False
        self.leak_secret = False
        self.result_override: Any | None = None
        self.access_token = "CREDENTIAL-MUST-NEVER-ENTER-SQLITE"
        self.refresh_token = "REFRESH-MUST-NEVER-ENTER-SQLITE"
        self.account_display_name = "产品团队"
        self.fail_revoke = False
        self.begin_auth_calls = 0
        self.health_checks = 0
        self.account_subject = "account-stable-id"
        self.block_begin_auth = False
        self.begin_auth_started = threading.Event()
        self.begin_auth_release = threading.Event()

    async def begin_auth(
        self,
        *,
        flow_id: str,
        auth_kind: ConnectorAuthKind,
        return_uri: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> AuthChallenge:
        del return_uri
        self.begin_auth_calls += 1
        self.begin_auth_started.set()
        if self.block_begin_auth:
            await asyncio.to_thread(self.begin_auth_release.wait, 5)
        assert code_challenge
        assert code_challenge_method == "S256"
        return AuthChallenge(
            flow_id=flow_id,
            connector_id="feishu",
            auth_kind=auth_kind,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            authorization_url=(
                "https://auth.example/authorize"
                f"?state={state}&code_challenge={code_challenge}"
                "&code_challenge_method=S256"
            ),
        )

    async def complete_auth(
        self,
        *,
        flow_id: str,
        response: Mapping[str, str],
        private_state: Mapping[str, str],
    ) -> AuthGrant:
        del flow_id
        assert response["state"] == private_state["state"]
        assert private_state["pkce_verifier"]
        self.auth_completions += 1
        return AuthGrant(
            account_subject=self.account_subject,
            account_display_name=self.account_display_name,
            granted_scopes=frozenset(
                {
                    "docx:document:readonly",
                    "docx:document",
                    "drive:drive:readonly",
                    "im:message",
                }
            ),
            credential_material={
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
            },
        )

    async def check_health(self, credentials: Mapping[str, str]) -> ConnectorHealthResult:
        self.health_checks += 1
        assert credentials["access_token"] == self.access_token
        if self.fail_health:
            raise RuntimeError("failed with " + credentials["access_token"])
        return ConnectorHealthResult(ConnectorHealth.CONNECTED)

    async def invoke(
        self,
        *,
        action_id: str,
        inputs: Mapping[str, Any],
        credentials: Mapping[str, str],
        idempotency_key: str | None,
    ) -> Any:
        assert credentials["access_token"] == self.access_token
        self.invocations.append((action_id, inputs, idempotency_key))
        self.invoke_started.set()
        if self.block_invocation:
            await asyncio.to_thread(self.invoke_release.wait, 5)
        if self.fail_invocation:
            raise RuntimeError("provider result was lost: " + credentials["access_token"])
        if self.leak_secret:
            return {"echo": credentials["access_token"]}
        if self.result_override is not None:
            return self.result_override
        return {"ok": True, "title": inputs.get("title")}

    async def revoke(
        self,
        *,
        credentials: Mapping[str, str],
        idempotency_key: str,
    ) -> bool:
        assert credentials["access_token"] == self.access_token
        assert idempotency_key.startswith("ecorex-disconnect:")
        if self.fail_revoke:
            raise RuntimeError("revoke failed with " + credentials["access_token"])
        return True


def _service(
    database: Path,
    vault: InMemoryCredentialVault,
    adapter: DurableFakeAdapter,
    *,
    publisher=None,
) -> ConnectorService:
    registry = builtin_connector_registry({"feishu": adapter})
    return ConnectorService(
        registry,
        allowed_return_uris=frozenset(
            {"http://127.0.0.1:8765/auth/callback"}
        ),
        vault=vault,
        repository=SQLiteConnectorRepository(database),
        outbox_publisher=publisher,
    )


def _connect(service: ConnectorService):
    challenge = asyncio.run(
        service.begin_connect(
            "feishu",
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri="http://127.0.0.1:8765/auth/callback",
        )
    )
    assert challenge.authorization_url is not None
    state = parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]
    instance = asyncio.run(service.complete_connect(challenge.flow_id, {"state": state}))
    return challenge, state, instance


def _all_database_bytes(database: Path) -> bytes:
    chunks = []
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(database) + suffix)
        if candidate.exists():
            chunks.append(candidate.read_bytes())
    return b"".join(chunks)


def test_connector_schema_fails_closed_before_mutating_newer_storage(tmp_path: Path) -> None:
    database = tmp_path / "future.sqlite3"
    SQLiteDatabase(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO connector_schema VALUES (1, 7)")
    with pytest.raises(RuntimeError, match="unsupported connector storage schema 7"):
        SQLiteConnectorRepository(database)
    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT schema_version FROM connector_schema WHERE singleton=1"
        ).fetchone()[0]
        definitions = connection.execute(
            "SELECT COUNT(*) FROM connector_definitions"
        ).fetchone()[0]
    assert version == 7
    assert definitions == 0


def test_connector_v4_schema_requires_signed_migration_without_repair(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v4.sqlite3"
    SQLiteDatabase(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO connector_schema VALUES (1, 4)")
        connection.execute("DROP INDEX uq_connector_active_reauthorization")
        connection.execute(
            "ALTER TABLE connector_auth_flows DROP COLUMN reauthorize_instance_id"
        )
    with pytest.raises(SchemaVersionError, match="product schema objects are missing"):
        SQLiteConnectorRepository(database)
    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT schema_version FROM connector_schema WHERE singleton=1"
        ).fetchone()[0]
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(connector_auth_flows)"
            ).fetchall()
        }
        old_index = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='index' "
            "AND name='uq_connector_active_reauthorization'"
        ).fetchone()
    assert version == 4
    assert "reauthorize_instance_id" not in columns
    assert old_index is None


def test_explicit_volatile_repository_keeps_schema_for_its_lifetime() -> None:
    repository = SQLiteConnectorRepository(":memory:")
    repository.sync_definitions(builtin_connector_registry().definitions())
    assert repository.pending_outbox_count() == 0


def test_connector_repository_coexists_with_real_migration_database(tmp_path: Path) -> None:
    from ecorex.migration.schema import initialize_target_database

    database = tmp_path / "migrated-v1.sqlite3"
    initialize_target_database(database)
    repository = SQLiteConnectorRepository(database)
    repository.sync_definitions(builtin_connector_registry().definitions())
    with sqlite3.connect(database) as connection:
        legacy_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(connector_instances)")
        }
        runtime_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(connector_runtime_instances)"
            )
        }
    assert "activation_status" in legacy_columns
    assert "credential_ref" in runtime_columns


def test_lifecycle_request_replays_across_restart_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    first = _service(database, vault, adapter)

    challenge = asyncio.run(
        first.begin_connect(
            "feishu",
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri="http://127.0.0.1:8765/auth/callback",
            client_request_id="connector-auth-stable",
        )
    )
    assert adapter.begin_auth_calls == 1
    restarted = _service(database, vault, adapter)
    replay = asyncio.run(
        restarted.begin_connect(
            "feishu",
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri="http://127.0.0.1:8765/auth/callback",
            client_request_id="connector-auth-stable",
        )
    )
    assert replay == challenge
    assert adapter.begin_auth_calls == 1
    assert restarted.repository.lifecycle_owner_has_flow(
        "connector-auth-", challenge.flow_id
    )
    assert not restarted.repository.lifecycle_owner_has_flow(
        "different-owner-", challenge.flow_id
    )
    with pytest.raises(ConnectorIdempotencyConflict):
        asyncio.run(
            restarted.begin_connect(
                "feishu",
                auth_kind=ConnectorAuthKind.DEVICE_CODE,
                return_uri="http://127.0.0.1:8765/auth/callback",
                client_request_id="connector-auth-stable",
            )
        )

    state = parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]
    instance = asyncio.run(
        restarted.complete_connect(challenge.flow_id, {"state": state})
    )
    assert restarted.repository.auth_completion_for_flow(challenge.flow_id) == (
        "feishu",
        instance.instance_id,
    )
    health = asyncio.run(
        restarted.refresh_health(
            instance.instance_id,
            client_request_id="connector-health-stable",
        )
    )
    assert health.health is ConnectorHealth.CONNECTED
    assert adapter.health_checks == 1
    again = _service(database, vault, adapter)
    replayed_health = asyncio.run(
        again.refresh_health(
            instance.instance_id,
            client_request_id="connector-health-stable",
        )
    )
    assert replayed_health.health is ConnectorHealth.CONNECTED
    assert adapter.health_checks == 1
    with pytest.raises(ConnectorIdempotencyConflict):
        asyncio.run(
            again.refresh_health(
                "conn_other",
                client_request_id="connector-health-stable",
            )
        )

    asyncio.run(
        again.disconnect(
            instance.instance_id,
            client_request_id="connector-disconnect-stable",
        )
    )
    after_disconnect = _service(database, vault, adapter)
    asyncio.run(
        after_disconnect.disconnect(
            instance.instance_id,
            client_request_id="connector-disconnect-stable",
        )
    )
    assert after_disconnect.repository.lifecycle_request_state(
        "connector-disconnect-stable"
    )["status"] == "completed"


def test_concurrent_lifecycle_request_has_one_authorization_side_effect(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    adapter = DurableFakeAdapter()
    adapter.block_begin_auth = True
    service = _service(database, InMemoryCredentialVault(), adapter)

    def begin():
        return asyncio.run(
            service.begin_connect(
                "feishu",
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri="http://127.0.0.1:8765/auth/callback",
                client_request_id="concurrent-auth-stable",
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(begin)
        assert adapter.begin_auth_started.wait(3)
        second = executor.submit(begin)
        with pytest.raises(ConnectorUnavailable, match="in progress"):
            second.result(timeout=3)
        adapter.begin_auth_release.set()
        challenge = first.result(timeout=5)
    assert challenge.flow_id.startswith("connflow_")
    assert adapter.begin_auth_calls == 1
    assert service.repository.lifecycle_request_state("concurrent-auth-stable")[
        "status"
    ] == "completed"


def test_reauthorization_keeps_instance_and_atomically_rotates_vault_reference(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _initial_challenge, _state, instance = _connect(service)
    old_credential_ref = instance.credential_ref
    old_material = vault.get(old_credential_ref)

    challenge = asyncio.run(
        service.begin_reauthorize(
            instance.instance_id,
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri="http://127.0.0.1:8765/auth/callback",
            client_request_id="reauthorize-stable",
        )
    )
    adapter.access_token = "ROTATED-CREDENTIAL-MUST-STAY-IN-VAULT"
    adapter.refresh_token = "ROTATED-REFRESH-MUST-STAY-IN-VAULT"
    state = parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]
    updated = asyncio.run(
        service.complete_connect(challenge.flow_id, {"state": state})
    )

    assert updated.instance_id == instance.instance_id
    assert updated.credential_ref != old_credential_ref
    assert updated.health is ConnectorHealth.CONNECTED
    assert old_material["access_token"] != adapter.access_token
    with pytest.raises(KeyError):
        vault.get(old_credential_ref)
    assert vault.get(updated.credential_ref)["access_token"] == adapter.access_token
    assert adapter.access_token.encode() not in _all_database_bytes(database)
    assert state.encode() not in _all_database_bytes(database)


def test_reauthorization_old_credential_cleanup_recovers_after_restart(
    tmp_path: Path,
) -> None:
    class CleanupFailingVault(InMemoryCredentialVault):
        fail_reference: str | None = None

        def delete(self, reference: str) -> None:
            if reference == self.fail_reference:
                raise RuntimeError("private cleanup detail")
            super().delete(reference)

    database = tmp_path / "runtime.sqlite3"
    vault = CleanupFailingVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _initial_challenge, _state, instance = _connect(service)
    vault.fail_reference = instance.credential_ref
    challenge = asyncio.run(
        service.begin_reauthorize(
            instance.instance_id,
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri="http://127.0.0.1:8765/auth/callback",
            client_request_id="reauthorize-cleanup-recovery",
        )
    )
    adapter.access_token = "RECOVERY-ROTATED-CREDENTIAL"
    state = parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]
    degraded = asyncio.run(
        service.complete_connect(challenge.flow_id, {"state": state})
    )
    assert degraded.instance_id == instance.instance_id
    assert degraded.health is ConnectorHealth.DEGRADED
    assert degraded.last_error_code == "credential_cleanup_pending"
    assert vault.get(instance.credential_ref)

    vault.fail_reference = None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE connector_vault_transitions SET operation_lease_expires_at=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )
    recovered = _service(database, vault, adapter)
    current = recovered.repository.get_instance(instance.instance_id)
    assert current is not None
    assert current.health is ConnectorHealth.CONNECTED
    assert current.last_error_code is None
    with pytest.raises(KeyError):
        vault.get(instance.credential_ref)
    assert vault.get(current.credential_ref)["access_token"] == adapter.access_token


def test_reauthorization_account_mismatch_preserves_existing_credentials(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _initial_challenge, _state, instance = _connect(service)
    old_material = vault.get(instance.credential_ref)
    challenge = asyncio.run(
        service.begin_reauthorize(
            instance.instance_id,
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri="http://127.0.0.1:8765/auth/callback",
            client_request_id="reauthorize-wrong-account",
        )
    )
    adapter.account_subject = "different-provider-account"
    adapter.access_token = "FOREIGN-ACCOUNT-CREDENTIAL"
    state = parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]
    with pytest.raises(ConnectorAuthError, match="does not match"):
        asyncio.run(service.complete_connect(challenge.flow_id, {"state": state}))

    unchanged = service.repository.get_instance(instance.instance_id)
    assert unchanged is not None
    assert unchanged.credential_ref == instance.credential_ref
    assert vault.get(instance.credential_ref) == old_material
    assert adapter.access_token.encode() not in _all_database_bytes(database)


def test_instances_definitions_health_and_write_replay_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    first = _service(database, vault, adapter)
    _challenge, oauth_state, instance = _connect(first)
    result = asyncio.run(
        first.invoke(
            instance.instance_id,
            "documents.write",
            {"title": "正式版计划"},
            idempotency_key="turn-1:write-1",
        )
    )
    assert result["title"] == "正式版计划"
    assert len(adapter.invocations) == 1

    second = _service(database, vault, adapter)
    projection = next(
        item for item in second.catalog() if item.definition.connector_id == "feishu"
    )
    assert projection.instances[0].instance_id == instance.instance_id
    replay = asyncio.run(
        second.invoke(
            instance.instance_id,
            "documents.write",
            {"title": "正式版计划"},
            idempotency_key="turn-1:write-1",
        )
    )
    assert replay == result
    assert len(adapter.invocations) == 1
    refreshed = asyncio.run(second.refresh_health(instance.instance_id))
    assert refreshed.health is ConnectorHealth.CONNECTED

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "wal"
        assert connection.execute("SELECT COUNT(*) FROM connector_definitions").fetchone()[0] >= 2
        assert connection.execute(
            "SELECT COUNT(*) FROM connector_runtime_instances"
        ).fetchone()[0] == 1
        stored_result = connection.execute(
            "SELECT result_json FROM connector_idempotency"
        ).fetchone()[0]
        assert json.loads(stored_result) == result
    raw = _all_database_bytes(database)
    assert b"CREDENTIAL-MUST-NEVER-ENTER-SQLITE" not in raw
    assert b"REFRESH-MUST-NEVER-ENTER-SQLITE" not in raw
    assert oauth_state.encode() not in raw
    assert b"turn-1:write-1" not in raw


def test_oauth_state_is_consumed_once_under_concurrency(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    challenge = asyncio.run(
        service.begin_connect(
            "feishu",
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri="http://127.0.0.1:8765/auth/callback",
        )
    )
    state = parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]

    def complete():
        return asyncio.run(service.complete_connect(challenge.flow_id, {"state": state}))

    outcomes: list[Any] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(complete) for _ in range(2)]
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as exc:  # asserted below
                outcomes.append(exc)
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, ConnectorAuthError) for item in outcomes) == 1
    assert adapter.auth_completions == 1


def test_unexpired_auth_flow_survives_restart_and_wrong_state_burns_flow(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    first = _service(database, vault, adapter)
    challenge = asyncio.run(
        first.begin_connect(
            "feishu",
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri="http://127.0.0.1:8765/auth/callback",
        )
    )
    state = parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]
    restarted = _service(database, vault, adapter)
    instance = asyncio.run(restarted.complete_connect(challenge.flow_id, {"state": state}))
    assert instance.connector_id == "feishu"

    other_database = tmp_path / "wrong-state.sqlite3"
    other_vault = InMemoryCredentialVault()
    wrong_state_service = _service(other_database, other_vault, DurableFakeAdapter())
    wrong_challenge = asyncio.run(
        wrong_state_service.begin_connect(
            "feishu",
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri="http://127.0.0.1:8765/auth/callback",
        )
    )
    correct_state = parse_qs(urlsplit(wrong_challenge.authorization_url).query)["state"][0]
    with pytest.raises(ConnectorAuthError, match="state validation"):
        asyncio.run(
            wrong_state_service.complete_connect(wrong_challenge.flow_id, {"state": "wrong"})
        )
    with pytest.raises(ConnectorAuthError, match="already consumed"):
        asyncio.run(
            wrong_state_service.complete_connect(
                wrong_challenge.flow_id, {"state": correct_state}
            )
        )


def test_recovery_does_not_steal_live_preparing_flow(tmp_path: Path) -> None:
    class BlockingFlowVault(InMemoryCredentialVault):
        started = threading.Event()
        release = threading.Event()

        def put(self, reference: str, material: Mapping[str, str]) -> None:
            if reference.startswith("ecorex/connector-flow/"):
                self.started.set()
                assert self.release.wait(5)
            super().put(reference, material)

    database = tmp_path / "runtime.sqlite3"
    vault = BlockingFlowVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)

    def begin():
        return asyncio.run(
            service.begin_connect(
                "feishu",
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri="http://127.0.0.1:8765/auth/callback",
            )
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(begin)
        assert vault.started.wait(3)
        _service(database, vault, adapter)
        vault.release.set()
        challenge = future.result(timeout=5)
    state = parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]
    instance = asyncio.run(service.complete_connect(challenge.flow_id, {"state": state}))
    assert instance.connector_id == "feishu"


def test_expired_and_transitional_records_recover_without_exposing_credentials(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    challenge = asyncio.run(
        service.begin_connect(
            "feishu",
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri="http://127.0.0.1:8765/auth/callback",
        )
    )
    flow_ref = f"ecorex/connector-flow/{challenge.flow_id}"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE connector_auth_flows SET expires_at=? WHERE flow_id=?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), challenge.flow_id),
        )
    _service(database, vault, adapter)
    with pytest.raises(KeyError):
        vault.get(flow_ref)
    with pytest.raises(ConnectorAuthError):
        asyncio.run(service.complete_connect(challenge.flow_id, {"state": "anything"}))

    # Simulate a crash after the account uniqueness claim but before activation.
    repository = SQLiteConnectorRepository(database)
    now = datetime.now(UTC)
    from ecorex.connectors import ConnectorInstance

    pending = ConnectorInstance(
        instance_id="conn_pending_recovery",
        connector_id="feishu",
        account_subject="pending-account",
        account_display_name="Pending",
        credential_ref="ecorex/connectors/conn_pending_recovery",
        granted_scopes=frozenset(),
        health=ConnectorHealth.AUTHENTICATING,
        created_at=now,
        updated_at=now,
    )
    transition_token = "pending-test-token"
    repository.insert_pending_instance(
        pending,
        transition_token=transition_token,
        lease_seconds=30,
    )
    vault.put(pending.credential_ref, {"access_token": "PENDING-SECRET"})
    restarted = _service(database, vault, adapter)
    assert repository.get_instance(pending.instance_id, include_transitional=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE connector_runtime_instances SET transition_lease_expires_at=?
            WHERE instance_id=?
            """,
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), pending.instance_id),
        )
    asyncio.run(restarted.maintenance_once())
    assert repository.get_instance(pending.instance_id, include_transitional=True) is None
    with pytest.raises(KeyError):
        vault.get(pending.credential_ref)


def test_failed_external_write_is_never_reissued_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    first = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(first)
    adapter.fail_invocation = True
    with pytest.raises(ConnectorInvocationUncertain) as error:
        asyncio.run(
            first.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "可能已写入"},
                idempotency_key="durable-key",
            )
        )
    assert "CREDENTIAL-MUST-NEVER-ENTER-SQLITE" not in str(error.value)
    assert len(adapter.invocations) == 1

    adapter.fail_invocation = False
    second = _service(database, vault, adapter)
    with pytest.raises(ConnectorInvocationUncertain):
        asyncio.run(
            second.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "可能已写入"},
                idempotency_key="durable-key",
            )
        )
    assert len(adapter.invocations) == 1


def test_health_failure_is_sanitized_persisted_and_recovered(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    adapter.fail_health = True
    from ecorex.connectors import ConnectorUnavailable

    with pytest.raises(ConnectorUnavailable) as error:
        asyncio.run(service.refresh_health(instance.instance_id))
    assert str(error.value) == "connector health check failed"
    assert "CREDENTIAL-MUST-NEVER-ENTER-SQLITE" not in str(error.value)

    restarted = _service(database, vault, adapter)
    projection = next(
        item for item in restarted.catalog() if item.definition.connector_id == "feishu"
    ).instances[0]
    assert projection.health is ConnectorHealth.ERROR
    assert projection.last_error_code == "health_check_failed"


def test_connector_repository_wait_never_stalls_event_loop(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    original = service.repository.acquire_instance_operation

    def slow_acquire(*args, **kwargs):
        time.sleep(0.15)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        service.repository,
        "acquire_instance_operation",
        slow_acquire,
    )

    async def scenario():
        started = asyncio.get_running_loop().time()
        pending = asyncio.create_task(
            service.refresh_health(
                instance.instance_id,
                client_request_id="responsive-health-1",
            )
        )
        await asyncio.sleep(0.02)
        loop_delay = asyncio.get_running_loop().time() - started
        result = await pending
        return loop_delay, result

    loop_delay, updated = asyncio.run(scenario())
    assert loop_delay < 0.1
    assert updated.health is ConnectorHealth.CONNECTED


def test_connector_adapter_timeout_is_bounded_and_late_success_replays(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    service.adapter_timeout_seconds = 0.05
    _challenge, _state, instance = _connect(service)
    adapter.block_invocation = True

    async def scenario():
        started = asyncio.get_running_loop().time()
        try:
            with pytest.raises(ConnectorInvocationUncertain):
                await service.invoke(
                    instance.instance_id,
                    "documents.write",
                    {"title": "timeout"},
                    idempotency_key="timeout-write",
                )
        finally:
            adapter.invoke_release.set()
            await asyncio.sleep(0.05)
        return asyncio.get_running_loop().time() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < 0.5
    assert len(adapter.invocations) == 1
    replay = asyncio.run(
        service.invoke(
            instance.instance_id,
            "documents.write",
            {"title": "timeout"},
            idempotency_key="timeout-write",
        )
    )
    assert replay["title"] == "timeout"
    assert len(adapter.invocations) == 1

def test_concurrent_idempotent_write_has_one_provider_call(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    adapter.block_invocation = True

    def invoke():
        return asyncio.run(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "并发"},
                idempotency_key="one-write",
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(invoke)
        assert adapter.invoke_started.wait(3)
        second = executor.submit(invoke)
        with pytest.raises(ConnectorInvocationUncertain):
            second.result(timeout=3)
        adapter.invoke_release.set()
        result = first.result(timeout=3)
    assert result["title"] == "并发"
    assert len(adapter.invocations) == 1
    assert asyncio.run(
        service.invoke(
            instance.instance_id,
            "documents.write",
            {"title": "并发"},
            idempotency_key="one-write",
        )
    ) == result


def test_disconnect_drains_inflight_write_before_revocation_and_keeps_tombstone(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    adapter.block_invocation = True

    def invoke():
        return asyncio.run(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "drain-me"},
                idempotency_key="drain-write",
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        write = executor.submit(invoke)
        assert adapter.invoke_started.wait(3)
        disconnect = executor.submit(
            lambda: asyncio.run(service.disconnect(instance.instance_id))
        )
        time.sleep(0.1)
        assert not disconnect.done()
        adapter.invoke_release.set()
        assert write.result(timeout=5)["title"] == "drain-me"
        disconnect.result(timeout=5)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM connector_runtime_instances"
        ).fetchone()[0] == 0
        status = connection.execute(
            """
            SELECT status FROM connector_idempotency
            WHERE idempotency_key_sha256=?
            """,
            (hashlib.sha256(b"drain-write").hexdigest(),),
        ).fetchone()[0]
    assert status == "completed"


def test_expired_operation_lease_blocks_disconnect_until_explicit_reconciliation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    adapter.block_invocation = True

    def invoke():
        return asyncio.run(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "suspended"},
                idempotency_key="suspended-write",
            )
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        write = executor.submit(invoke)
        assert adapter.invoke_started.wait(3)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE connector_operation_leases SET expires_at=?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )
        from ecorex.connectors import ConnectorUnavailable

        with pytest.raises(ConnectorUnavailable, match="draining"):
            asyncio.run(service.disconnect(instance.instance_id, drain_timeout=0.2))
        uncertain = service.repository.uncertain_operation_ids(instance.instance_id)
        assert len(uncertain) == 1
        adapter.invoke_release.set()
        with pytest.raises(ConnectorInvocationUncertain):
            write.result(timeout=5)
    # Mutating provider work keeps the durable drain fence until the exact
    # invocation is explicitly reconciled; a generic lease delete cannot
    # bypass invocation/idempotency authority.
    with pytest.raises(ConnectorUnavailable, match="draining"):
        asyncio.run(service.disconnect(instance.instance_id, drain_timeout=0.2))
    with sqlite3.connect(database) as connection:
        invocation_id = str(
            connection.execute(
                "SELECT invocation_id FROM connector_invocations "
                "WHERE operation_id=?",
                (uncertain[0],),
            ).fetchone()[0]
        )
    service.repository.resolve_uncertain_invocation(
        invocation_id, "manually_reconciled"
    )
    asyncio.run(service.disconnect(instance.instance_id))


def test_outbox_is_durable_leased_and_recoverable(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, _instance = _connect(service)
    repository_a = SQLiteConnectorRepository(database)
    repository_b = SQLiteConnectorRepository(database)
    assert repository_a.pending_outbox_count() >= 1

    claimed = repository_a.claim_outbox(limit=1, lease_seconds=30)
    assert len(claimed) == 1
    assert all(item.event_id != claimed[0].event_id for item in repository_b.claim_outbox())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE connector_outbox SET lease_expires_at=? WHERE event_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), claimed[0].event_id),
        )
    recovered = repository_b.claim_outbox(limit=100)
    assert claimed[0].event_id in {event.event_id for event in recovered}
    for event in recovered:
        repository_b.release_outbox(event.event_id, event.lease_token)

    published: list[str] = []
    delivery = _service(
        database,
        vault,
        adapter,
        publisher=lambda event: published.append(event.event_id),
    )
    delivery.drain_outbox(limit=100)
    assert published
    assert len(published) == len(set(published))
    assert repository_b.pending_outbox_count() == 0


def test_outbox_preserves_aggregate_order_and_poison_does_not_starve_others(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    asyncio.run(service.refresh_health(instance.instance_id))
    asyncio.run(
        service.invoke(
            instance.instance_id,
            "documents.write",
            {"title": "outbox"},
            idempotency_key="outbox-write",
        )
    )
    repository_a = SQLiteConnectorRepository(database)
    repository_b = SQLiteConnectorRepository(database)
    first = repository_a.claim_outbox(limit=1)[0]
    assert first.event_type == "connector.instance.connected"
    parallel = repository_b.claim_outbox(limit=1)[0]
    assert parallel.event_type == "connector.invocation.completed"
    assert parallel.aggregate_id != first.aggregate_id
    repository_a.release_outbox(first.event_id, first.lease_token)
    repository_b.release_outbox(parallel.event_id, parallel.lease_token)

    published_types: list[str] = []

    def publisher(event) -> None:
        if event.event_type == "connector.instance.connected":
            raise RuntimeError("poison")
        published_types.append(event.event_type)

    delivery = _service(database, vault, adapter, publisher=publisher)
    delivery.drain_outbox(limit=20)
    assert "connector.invocation.completed" in published_types
    assert "connector.instance.health_changed" not in published_types

    for _ in range(5):
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                UPDATE connector_outbox SET next_attempt_at=?
                WHERE event_type='connector.instance.connected'
                """,
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )
        delivery.drain_outbox(limit=20)
    assert repository_a.dead_letter_outbox_count() == 1
    assert "connector.instance.health_changed" not in published_types


def test_corrupt_outbox_event_is_quarantined_without_blocking_other_aggregates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    asyncio.run(
        service.invoke(
            instance.instance_id,
            "documents.write",
            {"title": "survives-corruption"},
            idempotency_key="corrupt-outbox",
        )
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE connector_outbox SET payload_sha256='bad-digest'
            WHERE event_type='connector.instance.connected'
            """
        )
    published: list[str] = []
    delivery = _service(
        database,
        vault,
        adapter,
        publisher=lambda event: published.append(event.event_type),
    )
    delivery.drain_outbox(limit=20)
    assert "connector.invocation.completed" in published
    assert SQLiteConnectorRepository(database).dead_letter_outbox_count() == 1


def test_disconnect_failure_is_visible_disabled_and_recovery_finishes_delete(
    tmp_path: Path,
) -> None:
    class FailingDeleteVault(InMemoryCredentialVault):
        fail_delete = False

        def delete(self, reference: str) -> None:
            if self.fail_delete and reference.startswith("ecorex/connectors/"):
                raise RuntimeError("simulated keychain failure")
            super().delete(reference)

    database = tmp_path / "runtime.sqlite3"
    vault = FailingDeleteVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    vault.fail_delete = True
    with pytest.raises(RuntimeError, match="connector disconnect failed") as error:
        asyncio.run(service.disconnect(instance.instance_id))
    assert "simulated" not in str(error.value)
    pending = next(
        projected
        for item in service.catalog()
        for projected in item.instances
        if projected.instance_id == instance.instance_id
    )
    assert pending.health is ConnectorHealth.DISABLED
    assert pending.available_actions == ()
    vault.fail_delete = False
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE connector_runtime_instances SET transition_lease_expires_at=?
            WHERE instance_id=?
            """,
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), instance.instance_id),
        )
    recovered = _service(database, vault, adapter)
    assert all(
        projected.instance_id != instance.instance_id
        for item in recovered.catalog()
        for projected in item.instances
    )
    with pytest.raises(KeyError):
        vault.get(instance.credential_ref)


def test_remote_revocation_is_required_and_uncertain_revoke_is_retryable(
    tmp_path: Path,
) -> None:
    class NonRevocableAdapter:
        __init__ = DurableFakeAdapter.__init__
        begin_auth = DurableFakeAdapter.begin_auth
        complete_auth = DurableFakeAdapter.complete_auth
        check_health = DurableFakeAdapter.check_health
        invoke = DurableFakeAdapter.invoke

    unsupported_database = tmp_path / "unsupported.sqlite3"
    unsupported_vault = InMemoryCredentialVault()
    unsupported_adapter = NonRevocableAdapter()
    unsupported = ConnectorService(
        builtin_connector_registry({"feishu": unsupported_adapter}),
        allowed_return_uris=frozenset(
            {"http://127.0.0.1:8765/auth/callback"}
        ),
        vault=unsupported_vault,
        repository=SQLiteConnectorRepository(unsupported_database),
    )
    _challenge, _state, active = _connect(unsupported)
    from ecorex.connectors import ConnectorUnavailable

    with pytest.raises(ConnectorUnavailable, match="provider-side"):
        asyncio.run(unsupported.disconnect(active.instance_id))
    assert unsupported_vault.get(active.credential_ref)

    database = tmp_path / "retry.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    adapter.fail_revoke = True
    with pytest.raises(ConnectorInvocationUncertain) as error:
        asyncio.run(service.disconnect(instance.instance_id))
    assert adapter.access_token not in str(error.value)
    pending = next(
        projected
        for item in service.catalog()
        for projected in item.instances
        if projected.instance_id == instance.instance_id
    )
    assert pending.last_error_code == "remote_revocation_uncertain"
    assert vault.get(instance.credential_ref)
    adapter.fail_revoke = False
    asyncio.run(service.disconnect(instance.instance_id))
    with pytest.raises(KeyError):
        vault.get(instance.credential_ref)


def test_idempotency_scope_survives_disconnect_and_same_account_reconnect(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, first_instance = _connect(service)
    first_result = asyncio.run(
        service.invoke(
            first_instance.instance_id,
            "documents.write",
            {"title": "once"},
            idempotency_key="account-stable-key",
        )
    )
    asyncio.run(service.disconnect(first_instance.instance_id))
    _challenge, _state, second_instance = _connect(service)
    assert second_instance.instance_id != first_instance.instance_id
    replay = asyncio.run(
        service.invoke(
            second_instance.instance_id,
            "documents.write",
            {"title": "once"},
            idempotency_key="account-stable-key",
        )
    )
    assert replay == first_result
    assert len(adapter.invocations) == 1


def test_adapter_cannot_persist_credential_material_as_action_result(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    adapter.leak_secret = True
    with pytest.raises(ConnectorInvocationUncertain, match="rejected"):
        asyncio.run(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "secret leak"},
                idempotency_key="secret-result",
            )
        )
    assert b"CREDENTIAL-MUST-NEVER-ENTER-SQLITE" not in _all_database_bytes(database)


@pytest.mark.parametrize(
    "result",
    [
        {"token": "NEW-CREDENTIAL-NOT-IN-VAULT"},
        {"accessToken": "ROTATED-CREDENTIAL"},
        {"url": "https://example.test/callback?authorization=NEW-CREDENTIAL"},
    ],
)
def test_new_or_rotated_credential_fields_fail_closed(
    tmp_path: Path,
    result: Mapping[str, Any],
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    adapter.result_override = result
    with pytest.raises(ConnectorInvocationUncertain, match="rejected"):
        asyncio.run(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "secret field"},
                idempotency_key="rotated-secret",
            )
        )
    raw = _all_database_bytes(database)
    for value in result.values():
        assert str(value).encode() not in raw


def test_action_specific_schema_allows_documents_lists_ids_and_public_urls(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    document_id = "550e8400-e29b-41d4-a716-446655440000"
    public_url = (
        "https://docs.example.test/document/550e8400-e29b-41d4-a716-446655440000"
        "?from=ecorex-office-agent"
    )
    adapter.result_override = {
        "ok": True,
        "document_id": document_id,
        "revision_id": "rev_01JABCDEFGH1234567890",
        "title": "季度经营报告",
        "content": "这是可公开给办公 Agent 使用的文档正文。",
        "url": public_url,
        "updated_at": "2026-07-10T15:34:00+08:00",
    }
    result = asyncio.run(
        service.invoke(
            instance.instance_id,
            "documents.read",
            {"document_id": document_id},
        )
    )
    assert result["document_id"] == document_id
    assert result["url"] == public_url

    adapter.result_override = {
        "ok": True,
        "items": [
            {
                "file_id": document_id,
                "name": "季度经营报告",
                "kind": "document",
                "mime_type": "application/vnd.ecorex.document",
                "url": public_url,
                "modified_at": "2026-07-10T15:34:00+08:00",
            }
        ],
        "has_more": False,
        "next_cursor": None,
    }
    search = asyncio.run(
        service.invoke(
            instance.instance_id,
            "drive.search",
            {"query": "季度经营报告"},
        )
    )
    assert search["items"][0]["file_id"] == document_id


def test_escaped_credential_and_account_metadata_never_enter_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    vault = InMemoryCredentialVault()
    adapter = DurableFakeAdapter()
    adapter.access_token = 'LINE1\nLINE2"\\END'
    service = _service(database, vault, adapter)
    _challenge, _state, instance = _connect(service)
    adapter.result_override = {"echo": adapter.access_token}
    with pytest.raises(ConnectorInvocationUncertain, match="rejected"):
        asyncio.run(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "escaped"},
                idempotency_key="escaped-secret",
            )
        )
    assert adapter.access_token.encode() not in _all_database_bytes(database)

    other_database = tmp_path / "metadata.sqlite3"
    other_adapter = DurableFakeAdapter()
    other_adapter.account_display_name = other_adapter.access_token
    other_service = _service(other_database, InMemoryCredentialVault(), other_adapter)
    challenge = asyncio.run(
        other_service.begin_connect(
            "feishu",
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri="http://127.0.0.1:8765/auth/callback",
        )
    )
    state = parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]
    with pytest.raises(ConnectorAuthError, match="credential material"):
        asyncio.run(other_service.complete_connect(challenge.flow_id, {"state": state}))
    assert other_adapter.access_token.encode() not in _all_database_bytes(other_database)
