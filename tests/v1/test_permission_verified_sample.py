from __future__ import annotations

import sqlite3

import pytest

from ecorex.runtime.permissions import (
    PermissionAuthority,
    PermissionIntegrityError,
    VerifiedPermissionSample,
)


def _authority_with_history(tmp_path) -> PermissionAuthority:
    authority = PermissionAuthority(
        tmp_path / "runtime.db",
        account_id="local-user",
        initial_full_access=False,
    )
    for revision, profile in enumerate(
        ("full_access", "default", "full_access"),
        start=1,
    ):
        authority.update(
            profile,
            expected_revision=revision,
            client_request_id=f"permission-sample-{revision}",
        )
    return authority


def test_verified_sample_checks_full_history_without_per_audit_ledger_queries(
    tmp_path,
    monkeypatch,
) -> None:
    authority = _authority_with_history(tmp_path)
    statements: list[str] = []
    original_connect = authority.database.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(authority.database, "connect", traced_connect)
    sample = authority.verified_sample()

    selects = [
        " ".join(statement.upper().split())
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert isinstance(sample, VerifiedPermissionSample)
    assert sample.snapshot.profile == "full_access"
    assert sample.snapshot.revision == 4
    assert len(sample.state_digest) == 64
    assert len(selects) == 4
    # One constant-size chain-head query plus one full verification scan.
    assert sum("FROM PERMISSION_STATE_LEDGER" in query for query in selects) == 2
    assert all(
        not (
            "FROM PERMISSION_STATE_LEDGER" in query
            and "AND REVISION" in query
        )
        for query in selects
    )


def test_verified_chain_head_cache_avoids_linear_rescan_until_authority_changes(
    tmp_path,
    monkeypatch,
) -> None:
    authority = _authority_with_history(tmp_path)
    authority.verified_sample()
    statements: list[str] = []
    original_connect = authority.database.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(authority.database, "connect", traced_connect)
    for _ in range(32):
        assert authority.verified_sample().snapshot.revision == 4

    selects = [
        " ".join(statement.upper().split())
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 32
    assert not any(
        query.startswith("SELECT * FROM PERMISSION_STATE_LEDGER")
        for query in selects
    )


def test_verified_sample_is_fresh_and_fails_closed_after_audit_tamper(
    tmp_path,
) -> None:
    authority = _authority_with_history(tmp_path)
    first = authority.verified_sample()
    assert first.snapshot.revision == 4

    with sqlite3.connect(authority.database.path) as connection:
        connection.execute("DROP TRIGGER permission_change_requests_no_update")
        connection.execute(
            "UPDATE permission_change_requests SET response_json='{}' "
            "WHERE client_request_id='permission-sample-2'"
        )
        connection.commit()

    with pytest.raises(PermissionIntegrityError, match="audit digest"):
        authority.verified_sample()
