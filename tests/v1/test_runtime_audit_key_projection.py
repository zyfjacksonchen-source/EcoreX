from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ecorex.connectors import (
    EphemeralEncryptedCredentialVault,
    InMemoryCredentialVault,
    RejectingCredentialVault,
)
from ecorex.observability import AuditIntegrityError, AuditOutbox, AuditPayloadCipher
from ecorex.runtime import RuntimeKernel, RuntimeSettings
from ecorex.runtime import api as runtime_api


class _MissingVault:
    def __init__(self) -> None:
        self.put_calls = 0

    def get(self, _reference: str):
        raise KeyError("missing")

    def put(self, _reference: str, _payload: object) -> None:
        self.put_calls += 1


class _MemoryVault:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, str]] = {}

    def get(self, reference: str):
        if reference not in self.values:
            raise KeyError(reference)
        return dict(self.values[reference])

    def put(self, reference: str, payload: object) -> None:
        assert isinstance(payload, dict)
        self.values[reference] = {str(key): str(value) for key, value in payload.items()}


def _settings(tmp_path) -> RuntimeSettings:
    return RuntimeSettings(
        database_path=tmp_path / "runtime.db",
        runtime_bearer_token="r" * 43,
        csrf_token="c" * 43,
        webui_origins=("http://testserver",),
        account_id="audit-projection-account",
    )


def _persist_encrypted_audit_row(
    kernel: RuntimeKernel, account_id: str, *, key: bytes = b"a" * 32
) -> None:
    outbox = AuditOutbox(
        kernel.database,
        account_id=account_id,
        cipher=AuditPayloadCipher(key),
    )
    with kernel.database.transaction() as connection:
        outbox._persist_view_in_transaction(
            connection,
            source_event_id=f"event-key-boundary-{account_id}",
            category="task",
            event_type="job.completed",
            thread_id=None,
            turn_id=None,
            trace_id="1" * 32,
            payload={"status": "encrypted"},
            created_at=datetime(2026, 7, 12, tzinfo=UTC),
        )


def test_projection_only_local_key_resolution_is_ephemeral_and_zero_write(
    tmp_path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    kernel = RuntimeKernel(settings.database_path)
    monkeypatch.setattr(runtime_api.sys, "platform", "linux")

    material = runtime_api._resolve_audit_encryption_key(
        settings,
        kernel=kernel,
        credential_vault=object(),
        create=False,
    )

    assert len(material) == 32
    assert list(tmp_path.glob(".*.audit-key")) == []


def test_projection_only_local_key_refuses_unrecoverable_encrypted_rows(
    tmp_path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    kernel = RuntimeKernel(settings.database_path)
    _persist_encrypted_audit_row(kernel, settings.account_id)
    monkeypatch.setattr(runtime_api.sys, "platform", "linux")

    with pytest.raises(RuntimeError, match="cannot unlock encrypted observability"):
        runtime_api._resolve_audit_encryption_key(
            settings,
            kernel=kernel,
            credential_vault=object(),
            create=False,
        )
    assert list(tmp_path.glob(".*.audit-key")) == []


def test_projection_only_os_vault_never_creates_missing_secret(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    kernel = RuntimeKernel(settings.database_path)
    vault = _MissingVault()
    monkeypatch.setattr(runtime_api.sys, "platform", "win32")

    material = runtime_api._resolve_audit_encryption_key(
        settings,
        kernel=kernel,
        credential_vault=vault,
        create=False,
    )

    assert len(material) == 32
    assert vault.put_calls == 0

    _persist_encrypted_audit_row(kernel, settings.account_id)
    with pytest.raises(AuditIntegrityError, match="payload authentication failed"):
        runtime_api._resolve_audit_encryption_key(
            settings,
            kernel=kernel,
            credential_vault=vault,
            create=False,
        )
    assert vault.put_calls == 0


@pytest.mark.parametrize("vault_factory", [InMemoryCredentialVault, RejectingCredentialVault])
def test_non_platform_vault_uses_a_restart_safe_local_key_on_macos(
    tmp_path, monkeypatch, vault_factory
) -> None:
    settings = _settings(tmp_path)
    kernel = RuntimeKernel(settings.database_path)
    monkeypatch.setattr(runtime_api.sys, "platform", "darwin")

    first = runtime_api._resolve_audit_encryption_key(
        settings,
        kernel=kernel,
        credential_vault=vault_factory(),
    )
    second = runtime_api._resolve_audit_encryption_key(
        settings,
        kernel=kernel,
        credential_vault=vault_factory(),
    )

    assert first == second
    assert len(list(tmp_path.glob(".*.audit-key"))) == 1


def test_acceptance_audit_key_survives_login_account_rebind(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    settings.acceptance_preview = True
    kernel = RuntimeKernel(settings.database_path)
    path = tmp_path / ".acceptance-credentials.vault"
    key = b"v" * 32
    monkeypatch.setattr(runtime_api.sys, "platform", "darwin")

    first = runtime_api._resolve_audit_encryption_key(
        settings,
        kernel=kernel,
        credential_vault=EphemeralEncryptedCredentialVault(path, key=key),
    )
    _persist_encrypted_audit_row(kernel, settings.account_id, key=first)
    settings.account_id = "authenticated-preview-account"
    second = runtime_api._resolve_audit_encryption_key(
        settings,
        kernel=kernel,
        credential_vault=EphemeralEncryptedCredentialVault(path, key=key),
    )

    assert first == second
    assert list(tmp_path.glob(".*.audit-key")) == []


def test_product_audit_key_defers_unreadable_rows_to_quarantine(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    kernel = RuntimeKernel(settings.database_path)
    _persist_encrypted_audit_row(kernel, "previous-account")
    settings.account_id = "current-account"
    target = _MemoryVault()
    monkeypatch.setattr(runtime_api.sys, "platform", "darwin")
    with kernel.database.reader() as connection:
        before = connection.execute(
            "SELECT payload_json FROM observability_audit_outbox"
        ).fetchall()

    with pytest.raises(AuditIntegrityError, match="payload authentication failed"):
        runtime_api._resolve_audit_encryption_key(
            settings, kernel=kernel, credential_vault=target
        )
    with kernel.database.reader() as connection:
        after = connection.execute(
            "SELECT payload_json FROM observability_audit_outbox"
        ).fetchall()
    assert target.values == {}
    assert before == after
