from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import sqlite3

import pytest

from ecorex.control_plane import (
    CloudAuditIntegrityError,
    CloudAuditRepository,
    ControlPlaneError,
    ControlPlaneRepository,
    ControlPrincipal,
    migrate_cloud_audit_database,
    migrate_control_plane_database,
)


_ZERO = "0" * 64
_INTEGRITY_KEY = b"h" * 32
_ACTOR = ControlPrincipal(
    subject="integrity-auditor",
    client_id="integrity-client",
    account_id="integrity-account",
    roles=frozenset({"audit_admin", "release_admin"}),
)


class _Verifier:
    def verify(self, _payload, _signature) -> bool:
        return True


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mac(value: str) -> str:
    return hmac.new(_INTEGRITY_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _control_repository(path) -> ControlPlaneRepository:
    migrate_control_plane_database(path)
    return ControlPlaneRepository(path, verifier=_Verifier())


def _cloud_repository(path) -> CloudAuditRepository:
    migrate_cloud_audit_database(path)
    return CloudAuditRepository(
        path,
        encryption_key=b"e" * 32,
        integrity_key=_INTEGRITY_KEY,
    )


def _control_entry(
    sequence: int,
    previous: str,
    *,
    suffix: str | None = None,
) -> tuple[str, str, str, str, str, str, str]:
    marker = str(sequence) if suffix is None else suffix
    actor = "seed-actor"
    action = "seed.appended"
    target = f"seed-{marker}"
    payload = _sha(f"payload-{marker}")
    created = f"2026-07-11T00:00:{sequence % 60:02d}+00:00#{marker}"
    digest = _sha(
        "\0".join((str(sequence), previous, actor, action, target, payload, created))
    )
    return actor, action, target, payload, previous, digest, created


def _cloud_entry(
    sequence: int,
    previous: str,
    *,
    suffix: str | None = None,
) -> tuple[str, str, str, str, str, str, str]:
    marker = str(sequence) if suffix is None else suffix
    actor = "seed-actor"
    action = "audit.seed.appended"
    target = f"seed-{marker}"
    payload = _sha(f"payload-{marker}")
    created = f"2026-07-11T00:00:{sequence % 60:02d}+00:00#{marker}"
    entry_mac = _mac(
        "\0".join((str(sequence), previous, actor, action, target, payload, created))
    )
    return actor, action, target, payload, previous, entry_mac, created


def _seed_control_chain(path, count: int) -> None:
    previous = _ZERO

    def rows():
        nonlocal previous
        for sequence in range(1, count + 1):
            row = _control_entry(sequence, previous)
            previous = row[5]
            yield row

    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO control_admin_audit("
            "actor_subject,action,target_id,payload_sha256,previous_digest,"
            "entry_digest,created_at) VALUES(?,?,?,?,?,?,?)",
            rows(),
        )


def _seed_cloud_chain(path, count: int) -> None:
    previous = _ZERO

    def rows():
        nonlocal previous
        for sequence in range(1, count + 1):
            row = _cloud_entry(sequence, previous)
            previous = row[5]
            yield row

    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO cloud_audit_integrity("
            "actor_subject,action,target_id,payload_sha256,previous_mac,"
            "entry_mac,created_at) VALUES(?,?,?,?,?,?,?)",
            rows(),
        )


def _append_control(repository: ControlPlaneRepository, index: int) -> None:
    with repository._transaction() as connection:
        repository._audit(
            connection,
            _ACTOR,
            "test.appended",
            f"control-{index}",
            {"index": index},
        )


def _append_cloud(repository: CloudAuditRepository, index: int) -> None:
    with repository._transaction() as connection:
        repository._append_integrity(
            connection,
            actor_subject=_ACTOR.subject,
            action="test.appended",
            target_id=f"cloud-{index}",
            payload={"index": index},
        )


def test_control_hot_reads_are_constant_after_ten_thousand_entries(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "control.sqlite3"
    migrate_control_plane_database(path)
    _seed_control_chain(path, 10_000)
    repository = ControlPlaneRepository(path, verifier=_Verifier())
    statements: list[str] = []
    verified_rows = 0
    original_connect = repository._connect
    original_verify = repository._verify_audit_row

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    def counted_verify(row, expected_sequence: int, previous: str) -> str:
        nonlocal verified_rows
        verified_rows += 1
        return original_verify(row, expected_sequence, previous)

    monkeypatch.setattr(repository, "_connect", traced_connect)
    monkeypatch.setattr(repository, "_verify_audit_row", counted_verify)

    for _ in range(100):
        assert repository.distribution().total_clients == 0

    chain_selects = [
        " ".join(statement.lower().split())
        for statement in statements
        if "from control_admin_audit" in statement.lower()
    ]
    assert verified_rows == 100
    assert len(chain_selects) == 200
    assert sum("where sequence=" in statement for statement in chain_selects) == 100
    assert sum("where sequence>" in statement for statement in chain_selects) == 100
    assert not any(
        statement.endswith("from control_admin_audit order by sequence")
        for statement in chain_selects
    )


def test_cloud_audit_hot_reads_are_constant_after_ten_thousand_entries(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "audit.sqlite3"
    migrate_cloud_audit_database(path)
    _seed_cloud_chain(path, 10_000)
    repository = CloudAuditRepository(
        path,
        encryption_key=b"e" * 32,
        integrity_key=_INTEGRITY_KEY,
    )
    statements: list[str] = []
    verified_rows = 0
    original_connect = repository._connect
    original_verify = repository._verify_integrity_row

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    def counted_verify(row, expected_sequence: int, previous: str) -> str:
        nonlocal verified_rows
        verified_rows += 1
        return original_verify(row, expected_sequence, previous)

    monkeypatch.setattr(repository, "_connect", traced_connect)
    monkeypatch.setattr(repository, "_verify_integrity_row", counted_verify)

    for _ in range(100):
        assert repository.list_metadata(_ACTOR) == ()

    chain_selects = [
        " ".join(statement.lower().split())
        for statement in statements
        if "from cloud_audit_integrity" in statement.lower()
    ]
    assert verified_rows == 200
    assert len(chain_selects) == 400
    assert sum("where sequence=" in statement for statement in chain_selects) == 100
    assert sum("where sequence>" in statement for statement in chain_selects) == 200
    assert (
        sum(
            "order by sequence desc limit 1" in statement for statement in chain_selects
        )
        == 100
    )
    assert not any(
        statement.endswith("from cloud_audit_integrity order by sequence")
        for statement in chain_selects
    )


def test_control_checkpoint_is_thread_safe_for_concurrent_reads_and_writes(
    tmp_path,
) -> None:
    repository = _control_repository(tmp_path / "control.sqlite3")
    write_indexes = [index for index in range(60) if index % 3]

    def exercise(index: int) -> None:
        if index % 3:
            _append_control(repository, index)
        else:
            repository.distribution()

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(exercise, range(60)))

    assert repository.verify_full_integrity() == len(write_indexes)
    assert repository._checkpoint_snapshot()[0] == len(write_indexes)


def test_cloud_checkpoint_is_thread_safe_for_concurrent_reads_and_writes(
    tmp_path,
) -> None:
    repository = _cloud_repository(tmp_path / "audit.sqlite3")
    write_indexes = [index for index in range(40) if index % 2]

    def exercise(index: int) -> None:
        if index % 2:
            _append_cloud(repository, index)
        else:
            repository.integrity_entries()

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(exercise, range(40)))

    assert repository.verify_full_integrity() == len(write_indexes)
    assert repository._integrity_checkpoint_snapshot()[0] == len(write_indexes)


def test_control_two_instances_incrementally_follow_each_other(tmp_path) -> None:
    path = tmp_path / "control.sqlite3"
    first = _control_repository(path)
    second = ControlPlaneRepository(path, verifier=_Verifier())

    _append_control(first, 1)
    second.distribution()
    _append_control(second, 2)
    first.distribution()

    assert first._checkpoint_snapshot()[0] == 2
    assert second._checkpoint_snapshot()[0] == 2
    assert first.verify_full_integrity() == 2


def test_cloud_two_instances_incrementally_follow_each_other(tmp_path) -> None:
    path = tmp_path / "audit.sqlite3"
    first = _cloud_repository(path)
    second = CloudAuditRepository(
        path,
        encryption_key=b"e" * 32,
        integrity_key=_INTEGRITY_KEY,
    )

    _append_cloud(first, 1)
    assert second.list_metadata(_ACTOR) == ()
    assert first.list_metadata(_ACTOR) == ()

    assert first._integrity_checkpoint_snapshot()[0] == 3
    assert second._integrity_checkpoint_snapshot()[0] == 2
    assert second.verify_full_integrity() == 3


def test_control_rollback_never_advances_checkpoint(tmp_path) -> None:
    path = tmp_path / "control.sqlite3"
    repository = _control_repository(path)
    before = repository._checkpoint_snapshot()

    with pytest.raises(RuntimeError, match="rollback"):
        with repository._transaction() as connection:
            repository._audit(
                connection,
                _ACTOR,
                "test.rolled-back",
                "control-rollback",
                {},
            )
            raise RuntimeError("rollback")

    assert repository._checkpoint_snapshot() == before
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM control_admin_audit").fetchone()[0]
            == 0
        )


def test_cloud_rollback_never_advances_checkpoint(tmp_path) -> None:
    path = tmp_path / "audit.sqlite3"
    repository = _cloud_repository(path)
    before = repository._integrity_checkpoint_snapshot()

    with pytest.raises(RuntimeError, match="rollback"):
        with repository._transaction() as connection:
            repository._append_integrity(
                connection,
                actor_subject=_ACTOR.subject,
                action="test.rolled-back",
                target_id="cloud-rollback",
                payload={},
            )
            raise RuntimeError("rollback")

    assert repository._integrity_checkpoint_snapshot() == before
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM cloud_audit_integrity").fetchone()[
                0
            ]
            == 0
        )


@pytest.mark.parametrize("corruption", ["gap", "previous", "new_digest"])
def test_control_new_chain_corruption_fails_closed_immediately(
    tmp_path, corruption: str
) -> None:
    path = tmp_path / f"control-{corruption}.sqlite3"
    repository = _control_repository(path)
    _append_control(repository, 1)
    checkpoint_sequence, checkpoint_digest = repository._checkpoint_snapshot()
    assert checkpoint_sequence == 1
    sequence = 3 if corruption == "gap" else 2
    previous = "e" * 64 if corruption == "previous" else checkpoint_digest
    row = _control_entry(sequence, previous, suffix=corruption)
    if corruption == "new_digest":
        row = (*row[:5], "f" * 64, row[6])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO control_admin_audit("
            "sequence,actor_subject,action,target_id,payload_sha256,previous_digest,"
            "entry_digest,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (sequence, *row),
        )

    with pytest.raises(ControlPlaneError, match="audit"):
        repository.distribution()


@pytest.mark.parametrize("corruption", ["gap", "previous", "new_mac"])
def test_cloud_new_chain_corruption_fails_closed_immediately(
    tmp_path, corruption: str
) -> None:
    path = tmp_path / f"audit-{corruption}.sqlite3"
    repository = _cloud_repository(path)
    _append_cloud(repository, 1)
    checkpoint_sequence, checkpoint_mac = repository._integrity_checkpoint_snapshot()
    assert checkpoint_sequence == 1
    sequence = 3 if corruption == "gap" else 2
    previous = "e" * 64 if corruption == "previous" else checkpoint_mac
    row = _cloud_entry(sequence, previous, suffix=corruption)
    if corruption == "new_mac":
        row = (*row[:5], "f" * 64, row[6])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO cloud_audit_integrity("
            "sequence,actor_subject,action,target_id,payload_sha256,previous_mac,"
            "entry_mac,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (sequence, *row),
        )

    with pytest.raises(CloudAuditIntegrityError, match="integrity"):
        repository.list_metadata(_ACTOR)


@pytest.mark.parametrize("corruption", ["tail_deleted", "tail_digest"])
def test_control_checkpoint_tail_corruption_fails_closed(
    tmp_path, corruption: str
) -> None:
    path = tmp_path / f"control-{corruption}.sqlite3"
    repository = _control_repository(path)
    _append_control(repository, 1)
    with sqlite3.connect(path) as connection:
        if corruption == "tail_deleted":
            connection.execute("DROP TRIGGER control_admin_audit_no_delete")
            connection.execute("DELETE FROM control_admin_audit WHERE sequence=1")
        else:
            connection.execute("DROP TRIGGER control_admin_audit_no_update")
            connection.execute(
                "UPDATE control_admin_audit SET payload_sha256=? WHERE sequence=1",
                ("f" * 64,),
            )

    with pytest.raises(ControlPlaneError, match="audit"):
        repository.distribution()


@pytest.mark.parametrize("corruption", ["tail_deleted", "tail_mac"])
def test_cloud_checkpoint_tail_corruption_fails_closed(
    tmp_path, corruption: str
) -> None:
    path = tmp_path / f"audit-{corruption}.sqlite3"
    repository = _cloud_repository(path)
    _append_cloud(repository, 1)
    with sqlite3.connect(path) as connection:
        if corruption == "tail_deleted":
            connection.execute("DROP TRIGGER cloud_audit_integrity_no_delete")
            connection.execute("DELETE FROM cloud_audit_integrity WHERE sequence=1")
        else:
            connection.execute("DROP TRIGGER cloud_audit_integrity_no_update")
            connection.execute(
                "UPDATE cloud_audit_integrity SET payload_sha256=? WHERE sequence=1",
                ("f" * 64,),
            )

    with pytest.raises(CloudAuditIntegrityError, match="integrity"):
        repository.list_metadata(_ACTOR)


def test_control_old_history_tamper_requires_explicit_full_verification(
    tmp_path,
) -> None:
    path = tmp_path / "control.sqlite3"
    repository = _control_repository(path)
    _append_control(repository, 1)
    _append_control(repository, 2)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER control_admin_audit_no_update")
        connection.execute(
            "UPDATE control_admin_audit SET payload_sha256=? WHERE sequence=1",
            ("f" * 64,),
        )

    assert repository.distribution().total_clients == 0
    with pytest.raises(ControlPlaneError, match="digest"):
        repository.verify_full_integrity()
    with pytest.raises(ControlPlaneError, match="full verification"):
        repository.distribution()


def test_cloud_old_history_tamper_requires_explicit_full_verification(
    tmp_path,
) -> None:
    path = tmp_path / "audit.sqlite3"
    repository = _cloud_repository(path)
    _append_cloud(repository, 1)
    _append_cloud(repository, 2)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER cloud_audit_integrity_no_update")
        connection.execute(
            "UPDATE cloud_audit_integrity SET payload_sha256=? WHERE sequence=1",
            ("f" * 64,),
        )

    assert repository.list_metadata(_ACTOR) == ()
    with pytest.raises(CloudAuditIntegrityError, match="integrity"):
        repository.verify_full_integrity()
    with pytest.raises(CloudAuditIntegrityError, match="full verification"):
        repository.list_metadata(_ACTOR)


def test_control_post_commit_checkpoint_conflict_does_not_report_rollback(
    tmp_path,
) -> None:
    path = tmp_path / "control.sqlite3"
    repository = _control_repository(path)
    transaction = repository._transaction()
    connection = transaction.__enter__()
    repository._audit(
        connection,
        _ACTOR,
        "test.committed",
        "control-committed",
        {},
    )
    with repository._audit_checkpoint_lock:
        repository._audit_checkpoint = (1, "f" * 64)

    assert transaction.__exit__(None, None, None) is False
    with sqlite3.connect(path) as reader:
        assert (
            reader.execute("SELECT COUNT(*) FROM control_admin_audit").fetchone()[0]
            == 1
        )
    with pytest.raises(ControlPlaneError, match="full verification"):
        repository.distribution()
    assert repository.verify_full_integrity() == 1
    assert repository.distribution().total_clients == 0


def test_cloud_post_commit_checkpoint_conflict_does_not_report_rollback(
    tmp_path,
) -> None:
    path = tmp_path / "audit.sqlite3"
    repository = _cloud_repository(path)
    transaction = repository._transaction()
    connection = transaction.__enter__()
    repository._append_integrity(
        connection,
        actor_subject=_ACTOR.subject,
        action="test.committed",
        target_id="cloud-committed",
        payload={},
    )
    with repository._integrity_checkpoint_lock:
        repository._integrity_checkpoint = (1, "f" * 64)

    assert transaction.__exit__(None, None, None) is False
    with sqlite3.connect(path) as reader:
        assert (
            reader.execute("SELECT COUNT(*) FROM cloud_audit_integrity").fetchone()[0]
            == 1
        )
    with pytest.raises(CloudAuditIntegrityError, match="full verification"):
        repository.list_metadata(_ACTOR)
    assert repository.verify_full_integrity() == 1
    assert repository.list_metadata(_ACTOR) == ()


def test_integrity_entries_uses_one_select_and_one_verified_pass(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "audit.sqlite3"
    migrate_cloud_audit_database(path)
    _seed_cloud_chain(path, 32)
    repository = CloudAuditRepository(
        path,
        encryption_key=b"e" * 32,
        integrity_key=_INTEGRITY_KEY,
    )
    statements: list[str] = []
    verified_rows = 0
    original_connect = repository._connect
    original_verify = repository._verify_integrity_row

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    def counted_verify(row, expected_sequence: int, previous: str) -> str:
        nonlocal verified_rows
        verified_rows += 1
        return original_verify(row, expected_sequence, previous)

    monkeypatch.setattr(repository, "_connect", traced_connect)
    monkeypatch.setattr(repository, "_verify_integrity_row", counted_verify)

    entries = repository.integrity_entries()

    chain_selects = [
        " ".join(statement.lower().split())
        for statement in statements
        if "from cloud_audit_integrity order by sequence" in statement.lower()
    ]
    assert len(entries) == 32
    assert verified_rows == 32
    assert len(chain_selects) == 1
