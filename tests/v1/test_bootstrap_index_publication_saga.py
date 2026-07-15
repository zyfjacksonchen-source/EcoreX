from __future__ import annotations

import base64
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from ecorex.control_plane.app import create_control_plane_app
from ecorex.control_plane.bootstrap_index_service import (
    BootstrapIndexPublicationError,
    BootstrapIndexPublicationService,
)
from ecorex.control_plane.bootstrap_freshness import (
    BootstrapFreshnessConfig,
    BootstrapFreshnessRefresher,
)
from ecorex.control_plane.models import ControlPrincipal
from ecorex.control_plane.repository import (
    ControlPlaneConflict,
    ControlPlaneNotFound,
    ControlPlaneRepository,
)
from ecorex.control_plane.schema import migrate_control_plane_database
from ecorex.release.public_index import (
    public_bootstrap_authority_signing_bytes,
)


class AcceptingVerifier:
    def verify(self, payload, signature) -> bool:
        return bool(payload and signature.algorithm == "ed25519")


class Authenticator:
    def authenticate(self, bearer_token: str) -> ControlPrincipal:
        if bearer_token != "bootstrap-admin-token-1234567890":
            raise PermissionError("invalid token")
        return _principal()


class MemoryObjectStore:
    def __init__(self) -> None:
        self.payload: bytes | None = None
        self.calls = 0
        self._lock = threading.Lock()

    def activate(
        self,
        payload: bytes,
        *,
        expected_previous_sha256: str | None,
        candidate_sha256: str,
    ) -> str:
        with self._lock:
            self.calls += 1
            current = (
                hashlib.sha256(self.payload).hexdigest()
                if self.payload is not None
                else None
            )
            if current == candidate_sha256:
                return "pobj_" + candidate_sha256[:32]
            if current != expected_previous_sha256:
                raise BootstrapIndexPublicationError("public object diverged")
            self.payload = payload
            return "pobj_" + candidate_sha256[:32]


class MemoryReader:
    def __init__(self, store: MemoryObjectStore) -> None:
        self.store = store
        self.failure: Exception | None = None

    def read_exact(self, public_url: str) -> bytes:
        assert public_url.endswith("/public-bootstrap-index.json")
        if self.failure is not None:
            raise self.failure
        assert self.store.payload is not None
        return self.store.payload


class StaticFreshnessSigner:
    key_id = "online-publication-key"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def sign(self, payload: bytes) -> bytes:
        self.calls += 1
        if self.fail:
            raise RuntimeError("KMS unavailable")
        assert payload.startswith(b"ecorex.public-bootstrap-freshness.v1\0")
        return b"n" * 64


class FailingObjectStore(MemoryObjectStore):
    def activate(self, *args, **kwargs):
        raise BootstrapIndexPublicationError("object store unavailable")


def _principal() -> ControlPrincipal:
    return ControlPrincipal(
        subject="release-admin",
        client_id="release-client",
        account_id="release-account",
        roles=frozenset({"release_admin"}),
    )


def _index_bytes(
    *,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    build_digest: str = "b" * 64,
) -> bytes:
    now = (issued_at or datetime.now(UTC)).replace(microsecond=0)
    expires = (expires_at or now + timedelta(hours=1)).replace(microsecond=0)
    release_id = "release-stable-" + "a" * 24
    target = {
        "manifest_sha256": "d" * 64,
        "release_id": release_id,
        "version": "1.0.0",
        "build_digest": build_digest,
    }
    authority_payload = public_bootstrap_authority_signing_bytes(
        sequence=1,
        revision=release_id,
        target=target,
    )
    release_signature = {
        "algorithm": "ed25519",
        "key_id": "offline-release-key",
        "value": base64.b64encode(b"r" * 64).decode("ascii"),
    }
    freshness_signature = {
        "algorithm": "ed25519",
        "key_id": "online-publication-key",
        "value": base64.b64encode(b"f" * 64).decode("ascii"),
    }
    sources = (
        ("mirror", "github-cn-mirror", 0, "https://mirror.example/release"),
        ("github", "github-release", 1, "https://github.example/release"),
        ("cdn", "ecorex-cdn", 2, "https://cdn.example/release"),
    )

    def links(name: str) -> list[dict[str, object]]:
        return [
            {
                "source_id": source_id,
                "kind": kind,
                "priority": priority,
                "url": f"{root}/{name}",
            }
            for source_id, kind, priority, root in sources
        ]

    targets = (
        ("bootstrap-windows-x64", "windows", "x64", "bootstrap-windows-x64.zip"),
        ("bootstrap-macos-arm64", "macos", "arm64", "bootstrap-macos-arm64.zip"),
        ("bootstrap-macos-x64", "macos", "x64", "bootstrap-macos-x64.zip"),
    )
    value = {
        "schema_version": 1,
        "document_type": "ecorex.public-bootstrap-discovery",
        "trust": "untrusted-discovery-hint",
        "status": "published",
        "authority": {
            "sequence": 1,
            "revision": release_id,
            "target": target,
            "signature": release_signature,
        },
        "freshness": {
            "authority_sha256": hashlib.sha256(authority_payload).hexdigest(),
            "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "signature": freshness_signature,
        },
        "release": {
            "release_id": release_id,
            "version": "1.0.0",
            "channel": "stable",
            "created_at": "2026-07-11T12:00:00+08:00",
            "build_digest": build_digest,
            "publication_receipt_sha256": "c" * 64,
            "manifest": {
                "file_name": "release-manifest.json",
                "sha256": "d" * 64,
                "signature": release_signature,
                "sources": links("release-manifest.json"),
            },
            "bootstrap_artifacts": [
                {
                    "artifact_id": artifact_id,
                    "platform": platform,
                    "architecture": architecture,
                    "file_name": file_name,
                    "size_bytes": 1024,
                    "sha256": hashlib.sha256(artifact_id.encode()).hexdigest(),
                    "signature": release_signature,
                    "sources": links(file_name),
                }
                for artifact_id, platform, architecture, file_name in targets
            ],
        },
    }
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    )


def _system(tmp_path):
    database = tmp_path / "control.db"
    migrate_control_plane_database(database)
    verifier = AcceptingVerifier()
    repository = ControlPlaneRepository(
        database,
        verifier=verifier,
        bootstrap_freshness_verifier=verifier,
    )
    store = MemoryObjectStore()
    reader = MemoryReader(store)
    service = BootstrapIndexPublicationService(
        repository,
        public_url="https://download.example/stable/public-bootstrap-index.json",
        object_store=store,
        public_reader=reader,
    )
    return repository, store, reader, service


def _activation_request(staged: dict[str, object]) -> dict[str, object]:
    return {
        "revision_id": staged["revision_id"],
        "index_sha256": staged["index_sha256"],
        "expected_previous_activation_record_id": staged["active_activation_record_id"],
        "expected_previous_sequence": staged["active_sequence"],
        "expected_previous_authority_revision_id": staged[
            "active_authority_revision_id"
        ],
        "expected_previous_index_sha256": staged["active_index_sha256"],
        "expected_previous_target": staged["active_target"],
    }


def _activate_initial(service, payload: bytes, actor: ControlPrincipal):
    staged = service.stage(
        payload, actor=actor, client_request_id="initial-pointer-stage"
    )
    return service.activate(
        release_id=str(staged["release_id"]),
        request=_activation_request(staged),
        actor=actor,
        client_request_id="initial-pointer-activate",
    )


def _refresher(repository, service, signer, *, owner="refresh-owner"):
    return BootstrapFreshnessRefresher(
        repository,
        service,
        signer=signer,
        config=BootstrapFreshnessConfig(
            owner_id=owner,
            lead_seconds=8 * 60 * 60,
            check_interval_seconds=60 * 60,
            lease_seconds=10 * 60,
        ),
    )


def test_publish_then_finalize_crash_is_resumed_from_durable_intent(
    tmp_path, monkeypatch
) -> None:
    repository, store, _reader, service = _system(tmp_path)
    payload = _index_bytes()
    actor = _principal()
    staged = service.stage(payload, actor=actor, client_request_id="stage-crash")
    request = _activation_request(staged)
    original = repository.finalize_bootstrap_index_activation
    attempts = 0

    def crash_once(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated process crash")
        return original(**kwargs)

    monkeypatch.setattr(repository, "finalize_bootstrap_index_activation", crash_once)
    with pytest.raises(OSError, match="simulated process crash"):
        service.activate(
            release_id=str(staged["release_id"]),
            request=request,
            actor=actor,
            client_request_id="activate-crash",
        )
    assert store.payload == payload
    assert repository.active_bootstrap_index_bytes() is None
    with pytest.raises(ControlPlaneNotFound):
        repository.trusted_bootstrap_index_proof(str(staged["release_id"]))

    completed = service.activate(
        release_id=str(staged["release_id"]),
        request=request,
        actor=actor,
        client_request_id="activate-crash",
    )
    assert completed["state"] == "active-and-read-back"
    assert completed["readback"]["proof_token"].startswith(
        "bootstrap-index-proof:bread_"
    )
    assert repository.active_bootstrap_index_bytes() == payload


def test_readback_timeout_keeps_intent_publishing_and_gate_locked(tmp_path) -> None:
    repository, store, reader, service = _system(tmp_path)
    payload = _index_bytes()
    actor = _principal()
    staged = service.stage(payload, actor=actor, client_request_id="stage-timeout")
    reader.failure = BootstrapIndexPublicationError("timeout")
    with pytest.raises(BootstrapIndexPublicationError, match="timeout"):
        service.activate(
            release_id=str(staged["release_id"]),
            request=_activation_request(staged),
            actor=actor,
            client_request_id="activate-timeout",
        )
    assert store.payload == payload
    assert repository.active_bootstrap_index_bytes() is None
    with pytest.raises(ControlPlaneNotFound):
        repository.trusted_bootstrap_index_proof(str(staged["release_id"]))


def test_concurrent_activate_converges_on_one_activation_and_proof(tmp_path) -> None:
    repository, store, _reader, service = _system(tmp_path)
    payload = _index_bytes()
    actor = _principal()
    staged = service.stage(payload, actor=actor, client_request_id="stage-race")
    request = _activation_request(staged)

    def activate():
        return service.activate(
            release_id=str(staged["release_id"]),
            request=request,
            actor=actor,
            client_request_id="activate-race",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _item: activate(), range(2)))
    assert (
        results[0]["active_activation_record_id"]
        == results[1]["active_activation_record_id"]
    )
    assert (
        results[0]["readback"]["proof_token"] == results[1]["readback"]["proof_token"]
    )
    assert repository.active_bootstrap_index_bytes() == payload
    assert store.calls == 2


def test_divergent_public_digest_fails_closed_and_keeps_proof_absent(
    tmp_path,
) -> None:
    repository, store, reader, service = _system(tmp_path)
    payload = _index_bytes()
    actor = _principal()
    staged = service.stage(payload, actor=actor, client_request_id="stage-diverge")
    reader.failure = BootstrapIndexPublicationError("timeout")
    with pytest.raises(BootstrapIndexPublicationError):
        service.activate(
            release_id=str(staged["release_id"]),
            request=_activation_request(staged),
            actor=actor,
            client_request_id="activate-diverge",
        )
    store.payload = b"unexpected third-party bytes"
    reader.failure = None
    with pytest.raises(BootstrapIndexPublicationError, match="diverged"):
        service.activate(
            release_id=str(staged["release_id"]),
            request=_activation_request(staged),
            actor=actor,
            client_request_id="activate-diverge",
        )
    assert repository.active_bootstrap_index_bytes() is None
    with pytest.raises(ControlPlaneNotFound):
        repository.trusted_bootstrap_index_proof(str(staged["release_id"]))


def test_same_sequence_refresh_requires_newer_window_and_never_signals_rollout(
    tmp_path,
) -> None:
    repository, _store, _reader, service = _system(tmp_path)
    actor = _principal()
    now = datetime.now(UTC).replace(microsecond=0)
    initial = _index_bytes(
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
    )
    staged = service.stage(initial, actor=actor, client_request_id="stage-initial")
    active = service.activate(
        release_id=str(staged["release_id"]),
        request=_activation_request(staged),
        actor=actor,
        client_request_id="activate-initial",
    )

    replayed_issued = _index_bytes(
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=2),
    )
    with pytest.raises(ControlPlaneConflict, match="freshness replay"):
        service.stage(
            replayed_issued,
            actor=actor,
            client_request_id="stage-old-issued",
        )
    non_extending_expiry = _index_bytes(
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )
    with pytest.raises(ControlPlaneConflict, match="freshness replay"):
        service.stage(
            non_extending_expiry,
            actor=actor,
            client_request_id="stage-old-expiry",
        )
    same_sequence_new_target = _index_bytes(
        issued_at=now,
        expires_at=now + timedelta(hours=2),
        build_digest="e" * 64,
    )
    with pytest.raises(ControlPlaneConflict, match="reused for another target"):
        service.stage(
            same_sequence_new_target,
            actor=actor,
            client_request_id="stage-new-target",
        )

    refreshed = _index_bytes(
        issued_at=now,
        expires_at=now + timedelta(hours=2),
    )
    refresh_stage = service.stage(
        refreshed,
        actor=actor,
        client_request_id="stage-refresh",
    )
    refresh_active = service.activate(
        release_id=str(refresh_stage["release_id"]),
        request=_activation_request(refresh_stage),
        actor=actor,
        client_request_id="activate-refresh",
    )
    assert refresh_active["active_sequence"] == active["active_sequence"] == 1
    assert (
        refresh_active["active_activation_record_id"]
        != active["active_activation_record_id"]
    )
    with sqlite3.connect(repository.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM control_update_signals"
            ).fetchone()[0]
            == 0
        )


def test_http_api_stages_activates_and_returns_server_trusted_proof(tmp_path) -> None:
    repository, _store, _reader, service = _system(tmp_path)
    payload = _index_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    app = create_control_plane_app(
        repository,
        authenticator=Authenticator(),
        bootstrap_index_service=service,
    )
    authorization = {"Authorization": "Bearer bootstrap-admin-token-1234567890"}
    release_id = "release-stable-" + "a" * 24
    with TestClient(app) as client:
        staged_response = client.put(
            f"/api/v1/bootstrap-index/candidates/{release_id}",
            content=payload,
            headers={
                **authorization,
                "Content-Type": "application/json",
                "X-EcoreX-SHA256": digest,
                "X-EcoreX-Size": str(len(payload)),
                "Idempotency-Key": "bootstrap-index:stage:http-test",
            },
        )
        assert staged_response.status_code == 201
        staged = staged_response.json()
        activation_response = client.post(
            f"/api/v1/bootstrap-index/candidates/{release_id}/activate",
            content=json.dumps(
                _activation_request(staged),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={
                **authorization,
                "Content-Type": "application/json",
                "Idempotency-Key": "bootstrap-index:activate:http-test",
            },
        )
        assert activation_response.status_code == 200
        active = activation_response.json()
        assert active["state"] == "active-and-read-back"
        proof_response = client.get(
            f"/api/v1/admin/bootstrap-index/proofs/{release_id}",
            headers=authorization,
        )
        assert proof_response.status_code == 200
        assert proof_response.json()["proof_token"] == active["readback"]["proof_token"]


def test_freshness_refresher_renews_near_expiry_without_rollout_signal(
    tmp_path,
) -> None:
    repository, store, _reader, service = _system(tmp_path)
    actor = _principal()
    now = datetime.now(UTC).replace(microsecond=0)
    initial = _index_bytes(
        issued_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=7),
    )
    original = _activate_initial(service, initial, actor)
    signer = StaticFreshnessSigner()
    result = _refresher(repository, service, signer).run_once(now=now)

    assert result["run_state"] == "succeeded"
    assert signer.calls == 1
    proof = repository.trusted_bootstrap_index_proof(
        "release-stable-" + "a" * 24,
        now=now,
    )
    assert proof["sequence"] == original["active_sequence"] == 1
    assert proof["target"] == original["active_target"]
    assert proof["expires_at"] == (now + timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with sqlite3.connect(repository.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM control_update_signals"
            ).fetchone()[0]
            == 0
        )
    assert store.calls == 2


def test_freshness_refresher_not_due_does_not_sign_or_publish(tmp_path) -> None:
    repository, store, _reader, service = _system(tmp_path)
    actor = _principal()
    now = datetime.now(UTC).replace(microsecond=0)
    _activate_initial(
        service,
        _index_bytes(
            issued_at=now,
            expires_at=now + timedelta(hours=12),
        ),
        actor,
    )
    signer = StaticFreshnessSigner()
    result = _refresher(repository, service, signer).run_once(now=now)
    assert result["run_state"] == "not-due"
    assert signer.calls == 0
    assert store.calls == 1


def test_manual_freshness_request_is_durably_idempotent(tmp_path) -> None:
    repository, store, _reader, service = _system(tmp_path)
    actor = _principal()
    now = datetime.now(UTC).replace(microsecond=0)
    _activate_initial(
        service,
        _index_bytes(
            issued_at=now - timedelta(hours=20),
            expires_at=now + timedelta(hours=1),
        ),
        actor,
    )
    signer = StaticFreshnessSigner()
    refresher = _refresher(repository, service, signer)
    first = refresher.run_once(
        force=True,
        now=now,
        actor=actor,
        client_request_id="manual-freshness-idempotency",
    )
    replay = refresher.run_once(
        force=True,
        now=now + timedelta(minutes=1),
        actor=actor,
        client_request_id="manual-freshness-idempotency",
    )
    assert replay == first
    assert first["run_state"] == "succeeded"
    assert signer.calls == 1
    assert store.calls == 2


def test_freshness_refresher_restart_resumes_prepared_publication(tmp_path) -> None:
    repository, store, reader, service = _system(tmp_path)
    actor = _principal()
    now = datetime.now(UTC).replace(microsecond=0)
    _activate_initial(
        service,
        _index_bytes(
            issued_at=now - timedelta(hours=20),
            expires_at=now + timedelta(hours=1),
        ),
        actor,
    )
    reader.failure = BootstrapIndexPublicationError("readback timeout")
    first_signer = StaticFreshnessSigner()
    failed = _refresher(repository, service, first_signer).run_once(now=now)
    assert failed["run_state"] == "failed"
    assert repository.bootstrap_freshness_refresh_status(now=now)["status"] == (
        "degraded"
    )
    reader.failure = None
    second_signer = StaticFreshnessSigner()
    resumed = _refresher(
        repository, service, second_signer, owner="restart-owner"
    ).run_once(now=now + timedelta(minutes=1))
    assert resumed["run_state"] == "succeeded"
    # Exact prepared bytes are reused; restart does not ask KMS to create a
    # second freshness identity.
    assert second_signer.calls == 0
    assert store.calls == 3


def test_freshness_restart_recovers_activation_before_success_bookkeeping(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, store, _reader, service = _system(tmp_path)
    actor = _principal()
    now = datetime.now(UTC).replace(microsecond=0)
    _activate_initial(
        service,
        _index_bytes(
            issued_at=now - timedelta(hours=20),
            expires_at=now + timedelta(hours=1),
        ),
        actor,
    )
    complete = repository.complete_bootstrap_freshness_refresh

    def crash_before_bookkeeping(**_kwargs):
        raise RuntimeError("injected process exit after activation")

    monkeypatch.setattr(
        repository, "complete_bootstrap_freshness_refresh", crash_before_bookkeeping
    )
    first = _refresher(repository, service, StaticFreshnessSigner()).run_once(now=now)
    assert first["run_state"] == "failed"
    # Public readback and the canonical active row already agree; only the
    # refresh-attempt success event was interrupted.
    assert store.calls == 2

    monkeypatch.setattr(repository, "complete_bootstrap_freshness_refresh", complete)
    restart_signer = StaticFreshnessSigner()
    recovered = _refresher(
        repository, service, restart_signer, owner="post-activation-restart"
    ).run_once(now=now + timedelta(seconds=1))
    assert recovered["run_state"] == "succeeded"
    assert recovered["status"] == "healthy"
    assert recovered["signer_configured"] is True
    # Recovery trusts the exact durable readback proof and does not sign or
    # publish another pointer.
    assert restart_signer.calls == 0
    assert store.calls == 2


def test_freshness_refresher_concurrent_forced_runs_do_not_duplicate(tmp_path) -> None:
    repository, store, _reader, service = _system(tmp_path)
    actor = _principal()
    now = datetime.now(UTC).replace(microsecond=0)
    _activate_initial(
        service,
        _index_bytes(
            issued_at=now - timedelta(hours=20),
            expires_at=now + timedelta(hours=1),
        ),
        actor,
    )
    first = _refresher(
        repository, service, StaticFreshnessSigner(), owner="concurrent-one"
    )
    second = _refresher(
        repository, service, StaticFreshnessSigner(), owner="concurrent-two"
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda refresher: refresher.run_once(force=True, now=now),
                (first, second),
            )
        )
    assert sum(item["run_state"] == "succeeded" for item in results) == 1
    assert {item["run_state"] for item in results} <= {
        "succeeded",
        "busy",
        "not-due",
    }
    assert store.calls == 2


@pytest.mark.parametrize("failure", ["sign", "publish", "readback"])
def test_freshness_refresher_failures_degrade_without_changing_db_active(
    tmp_path,
    failure: str,
) -> None:
    repository, store, reader, service = _system(tmp_path)
    actor = _principal()
    now = datetime.now(UTC).replace(microsecond=0)
    initial = _index_bytes(
        issued_at=now - timedelta(hours=20),
        expires_at=now + timedelta(hours=1),
    )
    _activate_initial(service, initial, actor)
    before = repository.active_bootstrap_index_bytes()
    signer = StaticFreshnessSigner(fail=failure == "sign")
    if failure == "publish":
        failing_store = FailingObjectStore()
        failing_store.payload = store.payload
        service = BootstrapIndexPublicationService(
            repository,
            public_url=("https://download.example/stable/public-bootstrap-index.json"),
            object_store=failing_store,
            public_reader=MemoryReader(failing_store),
        )
    elif failure == "readback":
        reader.failure = BootstrapIndexPublicationError("readback unavailable")
    result = _refresher(repository, service, signer).run_once(now=now)
    assert result["run_state"] == "failed"
    assert result["status"] == "degraded"
    assert repository.active_bootstrap_index_bytes() == before


def test_freshness_preparation_rejects_same_sequence_target_change(tmp_path) -> None:
    repository, _store, _reader, service = _system(tmp_path)
    actor = _principal()
    now = datetime.now(UTC).replace(microsecond=0)
    _activate_initial(
        service,
        _index_bytes(
            issued_at=now - timedelta(hours=20),
            expires_at=now + timedelta(hours=1),
        ),
        actor,
    )
    begun = repository.begin_bootstrap_freshness_refresh(
        owner_id="same-target-test",
        force=True,
        lead_seconds=8 * 60 * 60,
        check_interval_seconds=60 * 60,
        lease_seconds=10 * 60,
        actor=actor,
        now=now,
    )
    malicious = _index_bytes(
        issued_at=datetime.strptime(
            str(begun["issued_at"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC),
        expires_at=datetime.strptime(
            str(begun["expires_at"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC),
        build_digest="e" * 64,
    )
    with pytest.raises(ControlPlaneConflict, match="immutable authority"):
        repository.store_bootstrap_freshness_preparation(
            attempt_record_id=str(begun["attempt_record_id"]),
            owner_id="same-target-test",
            index_bytes=malicious,
            signer_key_id="online-publication-key",
            actor=actor,
            now=now,
        )


def test_freshness_startup_catchup_and_admin_status_refresh_api(tmp_path) -> None:
    repository, _store, _reader, service = _system(tmp_path)
    actor = _principal()
    now = datetime.now(UTC).replace(microsecond=0)
    _activate_initial(
        service,
        _index_bytes(
            issued_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=7),
        ),
        actor,
    )
    refresher = _refresher(repository, service, StaticFreshnessSigner())
    app = create_control_plane_app(
        repository,
        authenticator=Authenticator(),
        bootstrap_index_service=service,
        bootstrap_freshness_refresher=refresher,
    )
    headers = {"Authorization": "Bearer bootstrap-admin-token-1234567890"}
    with TestClient(app) as client:
        # Lifespan startup performs catch-up before accepting the admin call.
        status = client.get("/api/v1/admin/bootstrap-index/freshness", headers=headers)
        assert status.status_code == 200
        assert status.json()["status"] == "healthy"
        assert status.json()["last_success_at"] is not None
        refreshed = client.post(
            "/api/v1/admin/bootstrap-index/freshness/refresh",
            json={"client_request_id": "manual-refresh-once"},
            headers=headers,
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["run_state"] in {"succeeded", "not-due"}


def test_unconfigured_freshness_signer_is_explicitly_degraded_with_active_pointer(
    tmp_path,
) -> None:
    repository, _store, _reader, service = _system(tmp_path)
    actor = _principal()
    now = datetime.now(UTC).replace(microsecond=0)
    before = _index_bytes(
        issued_at=now - timedelta(hours=20),
        expires_at=now + timedelta(hours=1),
    )
    _activate_initial(service, before, actor)
    refresher = _refresher(repository, service, None)
    result = refresher.run_once(now=now)
    assert result["run_state"] == "unconfigured"
    assert result["status"] == "unconfigured"
    assert result["last_error_code"] == "signer-unavailable"
    assert refresher.ready is False
    assert repository.active_bootstrap_index_bytes() == before


def test_scheduler_readiness_tracks_signer_heartbeat_task_and_late_activation(
    tmp_path,
) -> None:
    repository, _store, _reader, service = _system(tmp_path)
    actor = _principal()
    now = datetime.now(UTC).replace(microsecond=0)

    async def exercise() -> None:
        missing = _refresher(repository, service, None)
        await missing.start()
        assert missing.running is True
        assert missing.ready is False
        _activate_initial(
            service,
            _index_bytes(
                issued_at=now - timedelta(hours=1),
                expires_at=now + timedelta(hours=1),
            ),
            actor,
        )
        assert missing.ready is False
        await missing.close()
        assert missing.running is False

        healthy = _refresher(
            repository,
            service,
            StaticFreshnessSigner(),
            owner="healthy-scheduler",
        )
        await healthy.start()
        assert healthy.ready is True
        heartbeat = healthy._last_heartbeat_at
        assert heartbeat is not None
        healthy._last_heartbeat_at = heartbeat - timedelta(
            seconds=healthy._heartbeat_max_age_seconds + 1
        )
        assert healthy.ready is False
        healthy._last_heartbeat_at = datetime.now(UTC).replace(microsecond=0)
        assert healthy.ready is True
        assert healthy._task is not None
        healthy._task.cancel()
        await asyncio.gather(healthy._task, return_exceptions=True)
        assert healthy.running is False
        assert healthy.ready is False
        await healthy.close()

    asyncio.run(exercise())


def test_scheduler_unexpected_failure_is_durable_and_retries_bounded(
    tmp_path,
) -> None:
    repository, _store, _reader, service = _system(tmp_path)
    refresher = _refresher(repository, service, StaticFreshnessSigner())
    calls = 0

    def fail_run_once(**_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("injected scheduler failure")

    refresher.run_once = fail_run_once  # type: ignore[method-assign]
    refresher._retry_delay_seconds = 0.01

    async def exercise() -> None:
        await refresher.start()
        await asyncio.sleep(0.035)
        status = refresher.status()
        assert calls >= 2
        assert status["scheduler_running"] is True
        assert status["scheduler_ready"] is False
        assert status["scheduler_last_error_code"] == "scheduler-runtimeerror"
        assert status["last_error_code"] == "scheduler-runtimeerror"
        assert refresher.ready is False
        await refresher.close()

    asyncio.run(exercise())
    with sqlite3.connect(repository.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM bootstrap_index_outbox "
                "WHERE event_type='bootstrap-freshness.refresh-failed'"
            ).fetchone()[0]
            >= 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM bootstrap_freshness_refresh_events "
                "WHERE status='failed' AND error_code='scheduler-runtimeerror'"
            ).fetchone()[0]
            >= 2
        )
