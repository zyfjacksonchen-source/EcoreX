from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ecorex.observability import AuditOutbox, AuditPayloadCipher
from ecorex.runtime import RuntimeKernel, RuntimeSettings
from ecorex.runtime import api as runtime_api


class _MissingVault:
    def __init__(self) -> None:
        self.put_calls = 0

    def get(self, _reference: str):
        raise KeyError("missing")

    def put(self, _reference: str, _payload: object) -> None:
        self.put_calls += 1


def _settings(tmp_path) -> RuntimeSettings:
    return RuntimeSettings(
        database_path=tmp_path / "runtime.db",
        runtime_bearer_token="r" * 43,
        csrf_token="c" * 43,
        webui_origins=("http://testserver",),
        account_id="audit-projection-account",
    )


def _persist_encrypted_audit_row(kernel: RuntimeKernel, account_id: str) -> None:
    outbox = AuditOutbox(
        kernel.database,
        account_id=account_id,
        cipher=AuditPayloadCipher(b"a" * 32),
    )
    with kernel.database.transaction() as connection:
        outbox._persist_view_in_transaction(
            connection,
            source_event_id="event-key-boundary",
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
    with pytest.raises(RuntimeError, match="cannot unlock the encrypted audit outbox"):
        runtime_api._resolve_audit_encryption_key(
            settings,
            kernel=kernel,
            credential_vault=vault,
            create=False,
        )
    assert vault.put_calls == 0
