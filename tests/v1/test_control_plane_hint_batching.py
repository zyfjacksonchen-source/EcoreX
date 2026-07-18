from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
import hashlib
import math
import sqlite3
import threading

import pytest

from ecorex.control_plane import (
    MAX_UPDATE_HINT_BATCH_SIZE,
    ControlPlaneRepository,
    ControlPrincipal,
    UpdateHintClient,
    UpdateSignalHub,
    migrate_control_plane_database,
    required_publication_gates,
    required_release_gates,
)
from ecorex.control_plane.app import _ClientConnection
from ecorex.release import build_unsigned_gate_bundle
from ecorex.update import (
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
)


class _CountingVerifier:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def verify(self, payload, signature) -> bool:
        assert payload and signature.key_id == "release-key"
        with self._lock:
            self.calls += 1
        return True

    def reset(self) -> None:
        with self._lock:
            self.calls = 0


@pytest.fixture(autouse=True)
def _isolate_bootstrap_publication_proof(monkeypatch) -> None:
    """Keep batching tests scoped to fanout, not stable pointer publication.

    The signed pointer/CAS/readback proof is exercised adversarially in the
    Bootstrap publication saga suite.  These tests use a synthetic stable
    manifest and must not forge a trusted proof token just to reach fanout.
    """

    monkeypatch.setattr(
        ControlPlaneRepository,
        "_require_bootstrap_index_proof",
        lambda *_args, **_kwargs: None,
    )


_ADMIN = ControlPrincipal(
    subject="release-admin",
    client_id="release-admin-client",
    account_id="release-admin-account",
    roles=frozenset({"release_admin"}),
)


def _signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="release-key",
        value=base64.b64encode(b"test-signature").decode(),
    )


def _manifest() -> ReleaseManifest:
    windows = b"windows core"
    macos = b"macos core"
    return ReleaseManifest(
        schema_version=1,
        release_id="release-hint-batch-1.0.1",
        version="1.0.1",
        build_digest=hashlib.sha256(b"hint-batch-build").hexdigest(),
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-11T10:00:00+08:00",
        sources=(
            ReleaseSource(
                "mirror",
                SourceKind.GITHUB_CN_MIRROR,
                0,
                "https://mirror.example/hint-batch",
            ),
            ReleaseSource(
                "github",
                SourceKind.GITHUB_RELEASE,
                1,
                "https://github.example/hint-batch",
            ),
            ReleaseSource(
                "cdn",
                SourceKind.ECOREX_CDN,
                2,
                "https://cdn.example/hint-batch",
            ),
        ),
        artifacts=(
            ReleaseArtifact(
                artifact_id="core-windows-x64",
                platform="windows",
                architecture="x64",
                file_name="ecorex-windows.zip",
                size_bytes=len(windows),
                sha256=hashlib.sha256(windows).hexdigest(),
                signature=_signature(),
            ),
            ReleaseArtifact(
                artifact_id="core-macos-arm64",
                platform="macos",
                architecture="arm64",
                file_name="ecorex-macos.zip",
                size_bytes=len(macos),
                sha256=hashlib.sha256(macos).hexdigest(),
                signature=_signature(),
            ),
        ),
        signature=_signature(),
    )


def _repository(tmp_path) -> tuple[ControlPlaneRepository, _CountingVerifier]:
    database = tmp_path / "control.sqlite3"
    migrate_control_plane_database(database)
    verifier = _CountingVerifier()
    return ControlPlaneRepository(database, verifier=verifier), verifier


def _gate_bundle(manifest: ReleaseManifest) -> dict:
    publication = "publication-receipt:sha256:" + "a" * 64
    gates = {}
    for gate in required_release_gates(manifest.channel):
        if gate in required_publication_gates(manifest.channel):
            evidence = publication
        elif gate == "bootstrap-index":
            evidence = (
                "bootstrap-index-proof:bread_"
                + "b" * 32
                + ":sha256:"
                + "c" * 64
            )
        else:
            evidence = "gate-receipt:sha256:" + hashlib.sha256(
                gate.encode()
            ).hexdigest()
        gates[gate] = {"status": "passed", "evidence": evidence}
    unsigned = build_unsigned_gate_bundle(
        phase="finalize",
        commit_sha="d" * 40,
        workflow_run_id=1,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest.to_json().encode()).hexdigest(),
        gates=gates,
    )
    return {**unsigned, "signature": _signature().to_dict()}


def _seed(
    repository: ControlPlaneRepository,
    *,
    percentage: int = 100,
    organizations: list[str] | None = None,
    accounts: list[str] | None = None,
    minimum_compatible_version: str | None = None,
):
    manifest = _manifest()
    repository.create_candidate(
        manifest,
        manifest_file_sha256=hashlib.sha256(manifest.to_json().encode()).hexdigest(),
        actor=_ADMIN,
        client_request_id="hint-batch-candidate",
    )
    repository.record_gate_bundle(
        manifest.release_id,
        _gate_bundle(manifest),
        actor=_ADMIN,
        client_request_id="hint-batch-gate-bundle",
    )
    repository.publish(
        manifest.release_id,
        actor=_ADMIN,
        client_request_id="hint-batch-publish",
    )
    rollout = repository.create_rollout(
        manifest.release_id,
        percentage=percentage,
        organizations=organizations or [],
        accounts=accounts or [],
        minimum_compatible_version=minimum_compatible_version,
        actor=_ADMIN,
        client_request_id="hint-batch-rollout",
    )
    repository.rollout_action(
        rollout.rollout_id,
        "activate",
        actor=_ADMIN,
        client_request_id="hint-batch-activate",
    )
    activation = repository.rollout_signal_for_request(
        actor=_ADMIN,
        client_request_id="hint-batch-activate",
        rollout_id=rollout.rollout_id,
        action="activate",
    )
    return manifest, rollout, activation


def _principal(
    index: int,
    *,
    organization_id: str = "org-1",
    account_id: str | None = None,
) -> ControlPrincipal:
    return ControlPrincipal(
        subject=f"subject-{index}",
        client_id=f"client-{index}",
        account_id=account_id or f"account-{index}",
        organization_id=organization_id,
    )


def _client(
    principal: ControlPrincipal,
    *,
    platform: str = "windows",
    architecture: str = "x64",
    current_version: str = "1.0.0",
    update_state: str = "idle",
) -> UpdateHintClient:
    return UpdateHintClient(
        principal=principal,
        channel=ReleaseChannel.STABLE,
        platform=platform,
        architecture=architecture,
        current_version=current_version,
        update_state=update_state,
    )


def _connection(client: UpdateHintClient, *, queue_size: int = 1) -> _ClientConnection:
    return _ClientConnection(
        principal=client.principal,
        channel=client.channel,
        platform=client.platform,
        architecture=client.architecture,
        current_version=client.current_version,
        queue=asyncio.Queue(maxsize=queue_size),
    )


def _pause_signal(repository, rollout):
    repository.rollout_action(
        rollout.rollout_id,
        "pause",
        actor=_ADMIN,
        client_request_id="hint-batch-pause",
    )
    return repository.rollout_signal_for_request(
        actor=_ADMIN,
        client_request_id="hint-batch-pause",
        rollout_id=rollout.rollout_id,
        action="pause",
    )


@pytest.mark.parametrize("client_count", [500, 2000])
def test_hub_batches_database_snapshots_and_matches_single_client_contract(
    tmp_path, monkeypatch, client_count: int
) -> None:
    repository, verifier = _repository(tmp_path)
    manifest, rollout, _activation = _seed(repository, organizations=["org-1"])
    signal = _pause_signal(repository, rollout)
    clients = tuple(_client(_principal(index)) for index in range(client_count))

    # Establish the compatibility API as an item-by-item oracle before
    # transaction instrumentation is enabled.
    expected = tuple(
        repository.hint_manifest_for_client(
            signal,
            client.principal,
            platform=client.platform,
            architecture=client.architecture,
            current_version=client.current_version,
        )
        for client in clients
    )
    assert all(item == manifest for item in expected)

    counts = {"read": 0, "write": 0}
    batch_sizes: list[int] = []
    lock = threading.Lock()
    original_read = repository._read_transaction
    original_write = repository._transaction
    original_batch = repository.hint_manifests_for_clients

    @contextmanager
    def counted_read():
        with lock:
            counts["read"] += 1
        with original_read() as connection:
            yield connection

    @contextmanager
    def counted_write():
        with lock:
            counts["write"] += 1
        with original_write() as connection:
            yield connection

    def counted_batch(signal_value, clients_value):
        with lock:
            batch_sizes.append(len(clients_value))
        return original_batch(signal_value, clients_value)

    monkeypatch.setattr(repository, "_read_transaction", counted_read)
    monkeypatch.setattr(repository, "_transaction", counted_write)
    monkeypatch.setattr(repository, "hint_manifests_for_clients", counted_batch)
    verifier.reset()
    hub = UpdateSignalHub()
    connections = tuple(_connection(client) for client in clients)

    async def exercise() -> int:
        for connection in connections:
            await hub.add(connection)
        return await hub.broadcast_signal(repository, signal)

    delivered = asyncio.run(exercise())
    expected_batches = math.ceil(client_count / MAX_UPDATE_HINT_BATCH_SIZE)

    assert delivered == client_count
    assert counts == {"read": expected_batches, "write": 0}
    assert batch_sizes == (
        [client_count]
        if client_count <= MAX_UPDATE_HINT_BATCH_SIZE
        else [MAX_UPDATE_HINT_BATCH_SIZE, client_count - MAX_UPDATE_HINT_BATCH_SIZE]
    )
    assert verifier.calls == expected_batches * 2
    for index, connection in enumerate(connections):
        payload = connection.queue.get_nowait()
        assert payload == {
            "schema_version": 1,
            "event_id": signal.event_id,
            "event_type": "update.available",
            "release_id": expected[index].release_id,
            "version": expected[index].version,
            "build_digest": expected[index].build_digest,
            "channel": "stable",
        }
        assert connection.queue.empty()


@pytest.mark.parametrize("client_count", [500, 2000])
def test_activation_fanout_uses_bounded_write_batches_and_bulk_heartbeats(
    tmp_path, monkeypatch, client_count: int
) -> None:
    repository, verifier = _repository(tmp_path)
    manifest, _rollout, activation = _seed(repository)
    clients = tuple(_client(_principal(index)) for index in range(client_count))
    connections = tuple(_connection(client) for client in clients)
    hub = UpdateSignalHub()
    write_transactions = 0
    counter_lock = threading.Lock()
    original_write = repository._transaction

    @contextmanager
    def counted_write():
        nonlocal write_transactions
        with counter_lock:
            write_transactions += 1
        with original_write() as connection:
            yield connection

    monkeypatch.setattr(repository, "_transaction", counted_write)
    verifier.reset()

    async def exercise() -> int:
        for connection in connections:
            await hub.add(connection)
        return await hub.broadcast_signal(repository, activation)

    delivered = asyncio.run(exercise())
    expected_batches = math.ceil(client_count / MAX_UPDATE_HINT_BATCH_SIZE)

    assert delivered == client_count
    assert write_transactions == expected_batches
    assert verifier.calls == expected_batches * 2
    with sqlite3.connect(repository.path) as connection:
        client_rows = connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT client_id),MIN(update_state),"
            "MAX(update_state) FROM control_clients"
        ).fetchone()
    assert client_rows == (client_count, client_count, "idle", "idle")
    for connection in connections:
        payload = connection.queue.get_nowait()
        assert payload["release_id"] == manifest.release_id
        assert payload["event_id"] == activation.event_id


def test_batch_targeting_platform_mandatory_revocation_and_heartbeat_semantics(
    tmp_path,
) -> None:
    repository, verifier = _repository(tmp_path)
    manifest, rollout, activation = _seed(
        repository,
        percentage=1,
        organizations=["org-allowed"],
        accounts=["account-allowed"],
        minimum_compatible_version="1.0.0",
    )

    def bucket(client_id: str) -> int:
        return (
            int.from_bytes(
                hashlib.sha256(f"{rollout.rollout_id}\0{client_id}".encode()).digest()[
                    :4
                ],
                "big",
            )
            % 100
        )

    inside_id = next(
        f"inside-{index}" for index in range(1000) if bucket(f"inside-{index}") < 1
    )
    outside_id = next(
        f"outside-{index}" for index in range(1000) if bucket(f"outside-{index}") >= 1
    )

    def named(client_id: str, organization: str, account: str) -> ControlPrincipal:
        return ControlPrincipal(
            subject=f"subject-{client_id}",
            client_id=client_id,
            account_id=account,
            organization_id=organization,
        )

    clients = (
        _client(named(inside_id, "org-allowed", "account-allowed")),
        _client(
            named(outside_id, "org-allowed", "account-allowed"),
            current_version="0.9.0",
            update_state="downloading",
        ),
        _client(
            named("wrong-org", "org-other", "account-allowed"),
            current_version="0.9.0",
        ),
        _client(
            named("wrong-account", "org-allowed", "account-other"),
            current_version="0.9.0",
        ),
        _client(
            named("mac-mandatory", "org-allowed", "account-allowed"),
            platform="macos",
            architecture="arm64",
            current_version="0.9.0",
        ),
        _client(
            named("linux-mandatory", "org-allowed", "account-allowed"),
            platform="linux",
            architecture="x64",
            current_version="0.9.0",
        ),
        _client(
            named("already-current", "org-allowed", "account-allowed"),
            current_version=manifest.version,
        ),
    )
    verifier.reset()
    activated = repository.hint_manifests_for_clients(activation, clients)

    assert activated == (manifest, manifest, None, None, manifest, None, None)
    # One manifest plus one verification for each supported unique target.
    assert verifier.calls == 3
    with sqlite3.connect(repository.path) as connection:
        heartbeat_rows = connection.execute(
            "SELECT client_id,update_state FROM control_clients ORDER BY client_id"
        ).fetchall()
    assert len(heartbeat_rows) == len(clients)
    assert dict(heartbeat_rows)[outside_id] == "downloading"

    pause = _pause_signal(repository, rollout)
    paused_clients = tuple(
        UpdateHintClient(
            principal=client.principal,
            channel=client.channel,
            platform=client.platform,
            architecture=client.architecture,
            current_version=client.current_version,
            update_state="failed",
        )
        for client in clients
    )
    assert repository.hint_manifests_for_clients(pause, paused_clients) == activated
    with sqlite3.connect(repository.path) as connection:
        retained_state = connection.execute(
            "SELECT update_state FROM control_clients WHERE client_id=?",
            (outside_id,),
        ).fetchone()[0]
    assert retained_state == "downloading"

    repository.rollout_action(
        rollout.rollout_id,
        "activate",
        actor=_ADMIN,
        client_request_id="hint-batch-reactivate",
    )
    repository.kill_channel(
        ReleaseChannel.STABLE,
        actor=_ADMIN,
        client_request_id="hint-batch-kill",
    )
    new_signals = repository.read_update_signals(after_sequence=pause.sequence).signals
    halted = next(item for item in new_signals if item.signal_type == "rollout.halted")
    killed = next(item for item in new_signals if item.signal_type == "channel.killed")
    assert repository.hint_manifests_for_clients(halted, clients) == activated
    assert repository.hint_manifests_for_clients(killed, clients) == (None,) * len(
        clients
    )

    repository.clear_channel_kill(
        ReleaseChannel.STABLE,
        actor=_ADMIN,
        client_request_id="hint-batch-clear",
    )
    cleared = repository.read_update_signals(
        after_sequence=max(item.sequence for item in new_signals)
    ).signals[0]
    assert cleared.signal_type == "channel.kill_cleared"
    assert repository.hint_manifests_for_clients(cleared, clients) == (None,) * len(
        clients
    )


def test_invalid_client_across_later_batch_fails_before_database_or_any_hint(
    tmp_path, monkeypatch
) -> None:
    repository, _verifier = _repository(tmp_path)
    _manifest_value, rollout, _activation = _seed(repository)
    signal = _pause_signal(repository, rollout)
    clients = [
        _client(_principal(index)) for index in range(MAX_UPDATE_HINT_BATCH_SIZE + 75)
    ]
    clients.append(
        _client(
            _principal(MAX_UPDATE_HINT_BATCH_SIZE + 75),
            current_version="invalid-version",
        )
    )
    hub = UpdateSignalHub()
    connections = tuple(_connection(client) for client in clients)
    counts = {"read": 0, "write": 0}
    original_read = repository._read_transaction
    original_write = repository._transaction

    @contextmanager
    def counted_read():
        counts["read"] += 1
        with original_read() as connection:
            yield connection

    @contextmanager
    def counted_write():
        counts["write"] += 1
        with original_write() as connection:
            yield connection

    monkeypatch.setattr(repository, "_read_transaction", counted_read)
    monkeypatch.setattr(repository, "_transaction", counted_write)

    async def exercise() -> None:
        for connection in connections:
            await hub.add(connection)
        await hub.broadcast_signal(repository, signal)

    with pytest.raises(ValueError, match="SemVer"):
        asyncio.run(exercise())

    assert counts == {"read": 0, "write": 0}
    assert all(connection.queue.empty() for connection in connections)


def test_concurrent_disconnect_is_excluded_and_slow_queue_drops_only_old_hint(
    tmp_path, monkeypatch
) -> None:
    repository, _verifier = _repository(tmp_path)
    manifest, rollout, _activation = _seed(repository)
    signal = _pause_signal(repository, rollout)
    survivor = _connection(_client(_principal(1)), queue_size=1)
    disconnected = _connection(_client(_principal(2)), queue_size=1)
    survivor.queue.put_nowait({"event_type": "old.hint"})
    started = threading.Event()
    release = threading.Event()
    original_batch = repository.hint_manifests_for_clients

    def blocked_batch(signal_value, clients_value):
        started.set()
        assert release.wait(timeout=5)
        return original_batch(signal_value, clients_value)

    monkeypatch.setattr(repository, "hint_manifests_for_clients", blocked_batch)
    hub = UpdateSignalHub()

    async def exercise() -> int:
        await hub.add(survivor)
        await hub.add(disconnected)
        broadcast = asyncio.create_task(hub.broadcast_signal(repository, signal))
        assert await asyncio.to_thread(started.wait, 5)
        await hub.remove(disconnected.principal.client_id, disconnected)
        release.set()
        return await broadcast

    delivered = asyncio.run(exercise())

    assert delivered == 1
    assert disconnected.queue.empty()
    assert survivor.queue.qsize() == 1
    assert survivor.queue.get_nowait() == {
        "schema_version": 1,
        "event_id": signal.event_id,
        "event_type": "update.available",
        "release_id": manifest.release_id,
        "version": manifest.version,
        "build_digest": manifest.build_digest,
        "channel": "stable",
    }
