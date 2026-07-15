from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import threading

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from ecorex.runtime.database import SQLiteDatabase
from ecorex.session import (
    Ed25519SessionLeaseVerifier,
    LeaseSignatureError,
    LeaseValidationError,
    ManagedSessionLeaseClaims,
    ManagedSessionService,
    SessionConflict,
    SessionLeaseSignature,
    SessionUnavailable,
    SignedManagedSessionLease,
    StaleSessionRequest,
    token_digest,
)


ACCESS_1 = "access-token-secret-revision-one"
REFRESH_1 = "refresh-token-secret-revision-one"
ACCESS_2 = "access-token-secret-revision-two"
REFRESH_2 = "refresh-token-secret-revision-two"


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class InspectableVault:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, str]] = {}
        self._lock = threading.RLock()

    def put(self, reference: str, material) -> None:
        with self._lock:
            self.values[reference] = dict(material)

    def get(self, reference: str):
        with self._lock:
            if reference not in self.values:
                raise KeyError(reference)
            return dict(self.values[reference])

    def delete(self, reference: str) -> None:
        with self._lock:
            self.values.pop(reference, None)


@pytest.fixture
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _verifier(private_key: Ed25519PrivateKey) -> Ed25519SessionLeaseVerifier:
    return Ed25519SessionLeaseVerifier({"session-key-1": _public_key(private_key)})


def _lease(
    private_key: Ed25519PrivateKey,
    *,
    now: datetime,
    revision: int = 1,
    access_token: str = ACCESS_1,
    refresh_token: str = REFRESH_1,
    duration: timedelta = timedelta(hours=72),
    account_id: str = "account-sensitive-001",
    organization_id: str = "organization-sensitive-001",
) -> SignedManagedSessionLease:
    claims = ManagedSessionLeaseClaims(
        lease_id=f"lease-{revision}",
        account_id=account_id,
        organization_id=organization_id,
        display_name="EcoreX 测试用户",
        roles=("member", "workspace_admin"),
        model_allowlist=("ecorex-chat", "gpt-image-2"),
        quota={"model_tokens": 100_000, "image_jobs": 250},
        admin_denies=("dangerous_shell", "external_publish"),
        issued_at=now - timedelta(minutes=1),
        expires_at=now - timedelta(minutes=1) + duration,
        revision=revision,
        access_token_sha256=token_digest(access_token),
        refresh_token_sha256=token_digest(refresh_token),
    )
    signature = private_key.sign(claims.canonical_payload())
    return SignedManagedSessionLease(
        claims=claims,
        signature=SessionLeaseSignature(
            algorithm="ed25519",
            key_id="session-key-1",
            value=base64.b64encode(signature).decode("ascii"),
        ),
    )


def _service(tmp_path, private_key, clock, vault, *, phase_hook=None):
    return ManagedSessionService(
        tmp_path / "runtime.db",
        vault=vault,
        verifier=_verifier(private_key),
        clock=clock,
        phase_hook=phase_hook,
    )


def test_projection_only_session_converges_singleton_once(
    tmp_path,
    signing_key,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    vault = InspectableVault()

    with database.reader() as connection:
        before = connection.execute(
            "SELECT * FROM managed_session_state ORDER BY singleton"
        ).fetchall()
    service = ManagedSessionService(
        database,
        vault=vault,
        verifier=_verifier(signing_key),
        initialize=False,
    )

    with database.reader() as connection:
        projected = connection.execute(
            "SELECT * FROM managed_session_state ORDER BY singleton"
        ).fetchall()
    assert projected == before == []
    assert service.startup_converged is False
    with pytest.raises(SessionUnavailable, match="state is unavailable"):
        service.read_data_scope_snapshot()

    service.converge_startup()
    assert service.startup_converged is True
    with database.reader() as connection:
        converged = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM managed_session_state ORDER BY singleton"
            ).fetchall()
        ]
    assert len(converged) == 1

    service.converge_startup()
    with database.reader() as connection:
        restarted = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM managed_session_state ORDER BY singleton"
            ).fetchall()
        ]
    assert restarted == converged


def test_real_ed25519_lease_projects_cloud_authority_and_keeps_tokens_out_of_db(
    tmp_path,
    signing_key,
) -> None:
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = InspectableVault()
    service = _service(tmp_path, signing_key, clock, vault)
    lease = _lease(signing_key, now=now)

    snapshot = service.install(
        lease,
        access_token=ACCESS_1,
        refresh_token=REFRESH_1,
        client_request_id="login-request-stable-001",
    )

    assert snapshot.account_id == "account-sensitive-001"
    assert snapshot.organization_id == "organization-sensitive-001"
    assert snapshot.display_name == "EcoreX 测试用户"
    assert snapshot.roles == ("member", "workspace_admin")
    assert snapshot.model_allowlist == ("ecorex-chat", "gpt-image-2")
    assert snapshot.quota == {"image_jobs": 250, "model_tokens": 100_000}
    assert snapshot.admin_denies == ("dangerous_shell", "external_publish")
    assert snapshot.revision == 1
    assert service.bearer_token() == ACCESS_1
    assert len(vault.values) == 1

    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        audit_wire = json.dumps(
            connection.execute(
                "SELECT event_type,outcome,reason_code,client_request_hash,account_hash,"
                "organization_hash,lease_digest,details_json FROM managed_session_audit"
            ).fetchall()
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM managed_session_audit")
    database_wire = database.read_bytes()
    assert ACCESS_1.encode() not in database_wire
    assert REFRESH_1.encode() not in database_wire
    assert ACCESS_1 not in audit_wire
    assert REFRESH_1 not in audit_wire
    assert "account-sensitive-001" not in audit_wire
    assert "organization-sensitive-001" not in audit_wire


def test_strict_read_snapshots_validate_identity_without_writing_audit(
    tmp_path,
    signing_key,
) -> None:
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = InspectableVault()
    service = _service(tmp_path, signing_key, clock, vault)
    lease = _lease(signing_key, now=now)
    installed = service.install(
        lease,
        access_token=ACCESS_1,
        refresh_token=REFRESH_1,
        client_request_id="pure-read-session-install",
    )

    def audit_count() -> int:
        with sqlite3.connect(tmp_path / "runtime.db") as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM managed_session_audit"
                ).fetchone()[0]
            )

    before = audit_count()
    assert service.read_snapshot() == installed
    assert service.read_data_scope_snapshot() == installed
    assert service.read_snapshot() == installed
    assert audit_count() == before

    credential_ref = next(iter(vault.values))
    vault.values[credential_ref]["access_token"] = "tampered-read-token"
    with pytest.raises(LeaseValidationError):
        service.read_snapshot()
    assert audit_count() == before
    vault.values[credential_ref]["access_token"] = ACCESS_1

    assert service.snapshot() == installed
    assert audit_count() == before + 1


def test_tamper_token_hash_expiry_and_duration_all_fail_closed(
    tmp_path,
    signing_key,
) -> None:
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = InspectableVault()
    service = _service(tmp_path, signing_key, clock, vault)
    lease = _lease(signing_key, now=now)

    tampered_claims = replace(lease.claims, roles=("cloud_owner",))
    tampered = SignedManagedSessionLease(tampered_claims, lease.signature)
    with pytest.raises(LeaseSignatureError):
        service.install(
            tampered,
            access_token=ACCESS_1,
            refresh_token=REFRESH_1,
            client_request_id="tampered-login-request",
        )

    with pytest.raises(LeaseValidationError, match="duration"):
        _lease(
            signing_key,
            now=now,
            duration=timedelta(hours=72, seconds=1),
        )

    service.install(
        lease,
        access_token=ACCESS_1,
        refresh_token=REFRESH_1,
        client_request_id="valid-login-request-001",
    )
    credential_ref = next(iter(vault.values))
    vault.values[credential_ref]["access_token"] = "tampered-access-token"
    with pytest.raises(LeaseValidationError, match="access token"):
        service.bearer_token()
    vault.values[credential_ref]["access_token"] = ACCESS_1

    clock.value = lease.claims.expires_at
    with pytest.raises(LeaseValidationError, match="expired"):
        service.snapshot()
    with pytest.raises(LeaseValidationError, match="expired"):
        service.bearer_token()


def test_persisted_lease_digest_and_signature_are_rechecked_on_every_read(
    tmp_path,
    signing_key,
) -> None:
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = InspectableVault()
    service = _service(tmp_path, signing_key, clock, vault)
    lease = _lease(signing_key, now=now)
    service.install(
        lease,
        access_token=ACCESS_1,
        refresh_token=REFRESH_1,
        client_request_id="digest-recheck-login",
    )

    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER managed_session_install_identity_immutable")
        connection.execute(
            "UPDATE managed_session_installs SET lease_digest=? WHERE status='committed'",
            ("0" * 64,),
        )
        connection.commit()
    with pytest.raises(LeaseValidationError, match="storage was modified"):
        service.snapshot()

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE managed_session_installs SET lease_digest=? WHERE status='committed'",
            (lease.digest,),
        )
        row = connection.execute(
            "SELECT intent_id,lease_json FROM managed_session_installs WHERE status='committed'"
        ).fetchone()
        payload = json.loads(row[1])
        payload["claims"]["admin_denies"] = []
        connection.execute(
            "UPDATE managed_session_installs SET lease_json=? WHERE intent_id=?",
            (json.dumps(payload, separators=(",", ":")), row[0]),
        )
        connection.commit()
    with pytest.raises(LeaseValidationError):
        service.bearer_token()


def test_crash_recovery_finishes_vault_written_install_and_cleans_old_tokens(
    tmp_path,
    signing_key,
) -> None:
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = InspectableVault()
    phase = {"crash": "vault_put"}

    def failpoint(name: str, _identity: str) -> None:
        if name == phase["crash"]:
            raise RuntimeError(f"crash at {name}")

    service = _service(
        tmp_path, signing_key, clock, vault, phase_hook=failpoint
    )
    lease1 = _lease(signing_key, now=now)
    with pytest.raises(RuntimeError, match="vault_put"):
        service.install(
            lease1,
            access_token=ACCESS_1,
            refresh_token=REFRESH_1,
            client_request_id="crash-login-revision-1",
        )
    assert len(vault.values) == 1
    restarted = _service(tmp_path, signing_key, clock, vault)
    report = restarted.recover()
    assert report.finalized_installs == 1
    assert restarted.snapshot().revision == 1

    old_ref = next(iter(vault.values))
    lease2 = _lease(
        signing_key,
        now=now,
        revision=2,
        access_token=ACCESS_2,
        refresh_token=REFRESH_2,
    )
    phase["crash"] = "committed"
    service2 = _service(
        tmp_path, signing_key, clock, vault, phase_hook=failpoint
    )
    with pytest.raises(RuntimeError, match="committed"):
        service2.install(
            lease2,
            access_token=ACCESS_2,
            refresh_token=REFRESH_2,
            client_request_id="crash-login-revision-2",
        )
    assert len(vault.values) == 2
    restarted2 = _service(tmp_path, signing_key, clock, vault)
    cleanup = restarted2.recover()
    assert cleanup.cleaned_credentials == 1
    assert old_ref not in vault.values
    assert restarted2.bearer_token() == ACCESS_2


def test_crash_before_vault_write_is_aborted_and_same_request_can_resume(
    tmp_path,
    signing_key,
) -> None:
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = InspectableVault()

    def failpoint(name: str, _identity: str) -> None:
        if name == "staged":
            raise RuntimeError("crash before vault")

    lease = _lease(signing_key, now=now)
    crashed = _service(
        tmp_path, signing_key, clock, vault, phase_hook=failpoint
    )
    with pytest.raises(RuntimeError, match="before vault"):
        crashed.install(
            lease,
            access_token=ACCESS_1,
            refresh_token=REFRESH_1,
            client_request_id="retryable-crash-request",
        )
    assert not vault.values
    restarted = _service(tmp_path, signing_key, clock, vault)
    report = restarted.recover()
    assert report.aborted_installs == 1
    snapshot = restarted.install(
        lease,
        access_token=ACCESS_1,
        refresh_token=REFRESH_1,
        client_request_id="retryable-crash-request",
    )
    assert snapshot.revision == 1
    assert len(vault.values) == 1


def test_stale_logout_retry_cannot_remove_a_newer_login(
    tmp_path,
    signing_key,
) -> None:
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = InspectableVault()
    service = _service(tmp_path, signing_key, clock, vault)
    lease1 = _lease(signing_key, now=now)
    first = service.install(
        lease1,
        access_token=ACCESS_1,
        refresh_token=REFRESH_1,
        client_request_id="logout-protection-login-1",
    )
    logout = service.logout(
        client_request_id="stable-logout-request-001",
        expected_lease_digest=first.lease_digest,
    )
    assert logout.already_applied is False
    lease2 = _lease(
        signing_key,
        now=now,
        revision=2,
        access_token=ACCESS_2,
        refresh_token=REFRESH_2,
    )
    second = service.install(
        lease2,
        access_token=ACCESS_2,
        refresh_token=REFRESH_2,
        client_request_id="logout-protection-login-2",
    )
    retry = service.logout(
        client_request_id="stable-logout-request-001",
        expected_lease_digest=first.lease_digest,
    )
    assert retry.already_applied is True
    assert retry.generation == logout.generation
    assert service.snapshot().lease_digest == second.lease_digest
    assert service.bearer_token() == ACCESS_2


def test_concurrent_install_is_idempotent_and_pending_state_fails_closed(
    tmp_path,
    signing_key,
) -> None:
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = InspectableVault()
    lease = _lease(signing_key, now=now)
    services = [
        _service(tmp_path, signing_key, clock, vault) for _index in range(8)
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(
            pool.map(
                lambda service: service.install(
                    lease,
                    access_token=ACCESS_1,
                    refresh_token=REFRESH_1,
                    client_request_id="same-concurrent-login-request",
                ),
                services,
            )
        )
    assert {snapshot.lease_digest for snapshot in snapshots} == {lease.digest}
    assert len(vault.values) == 1
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM managed_session_installs"
        ).fetchone()[0] == 1

    # A distinct in-flight install blocks snapshots and bearer issuance until
    # it has atomically committed or been recovered.
    entered = threading.Event()
    release = threading.Event()
    lease2 = _lease(
        signing_key,
        now=now,
        revision=2,
        access_token=ACCESS_2,
        refresh_token=REFRESH_2,
    )

    def pause(name: str, _identity: str) -> None:
        if name == "staged":
            entered.set()
            assert release.wait(timeout=5)

    writer = _service(tmp_path, signing_key, clock, vault, phase_hook=pause)
    reader = _service(tmp_path, signing_key, clock, vault)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            writer.install,
            lease2,
            access_token=ACCESS_2,
            refresh_token=REFRESH_2,
            client_request_id="second-concurrent-login-request",
        )
        assert entered.wait(timeout=5)
        with pytest.raises(SessionConflict, match="in progress"):
            reader.snapshot()
        with pytest.raises(SessionConflict, match="in progress"):
            reader.bearer_token()
        release.set()
        assert future.result(timeout=5).revision == 2
    assert reader.snapshot().revision == 2


def test_monotonic_revision_and_stable_request_fingerprint_reject_replay(
    tmp_path,
    signing_key,
) -> None:
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = InspectableVault()
    service = _service(tmp_path, signing_key, clock, vault)
    lease1 = _lease(signing_key, now=now)
    service.install(
        lease1,
        access_token=ACCESS_1,
        refresh_token=REFRESH_1,
        client_request_id="monotonic-login-request-1",
    )
    replay = _lease(
        signing_key,
        now=now,
        revision=1,
        access_token=ACCESS_2,
        refresh_token=REFRESH_2,
    )
    with pytest.raises(StaleSessionRequest, match="monotonic"):
        service.install(
            replay,
            access_token=ACCESS_2,
            refresh_token=REFRESH_2,
            client_request_id="monotonic-login-replay",
        )
    lease2 = _lease(
        signing_key,
        now=now,
        revision=2,
        access_token=ACCESS_2,
        refresh_token=REFRESH_2,
    )
    with pytest.raises(SessionConflict, match="different session material"):
        service.install(
            lease2,
            access_token=ACCESS_2,
            refresh_token=REFRESH_2,
            client_request_id="monotonic-login-request-1",
        )


def test_recovery_deletes_a_superseded_writer_that_wrote_after_cleanup(
    tmp_path,
    signing_key,
) -> None:
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    clock = MutableClock(now)
    vault = InspectableVault()
    staged = threading.Event()
    release = threading.Event()

    def low_writer_failpoint(name: str, _identity: str) -> None:
        if name == "staged":
            staged.set()
            assert release.wait(timeout=5)
        if name == "vault_put":
            raise RuntimeError("superseded writer crashed after late vault write")

    low = _service(
        tmp_path,
        signing_key,
        clock,
        vault,
        phase_hook=low_writer_failpoint,
    )
    high = _service(tmp_path, signing_key, clock, vault)
    lease1 = _lease(signing_key, now=now)
    lease2 = _lease(
        signing_key,
        now=now,
        revision=2,
        access_token=ACCESS_2,
        refresh_token=REFRESH_2,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            low.install,
            lease1,
            access_token=ACCESS_1,
            refresh_token=REFRESH_1,
            client_request_id="late-superseded-writer",
        )
        assert staged.wait(timeout=5)
        assert high.install(
            lease2,
            access_token=ACCESS_2,
            refresh_token=REFRESH_2,
            client_request_id="winning-higher-revision",
        ).revision == 2
        release.set()
        with pytest.raises(RuntimeError, match="late vault write"):
            future.result(timeout=5)

    assert len(vault.values) == 2
    restarted = _service(tmp_path, signing_key, clock, vault)
    report = restarted.recover()
    assert report.cleaned_credentials >= 1
    assert len(vault.values) == 1
    assert restarted.bearer_token() == ACCESS_2
