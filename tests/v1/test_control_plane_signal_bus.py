from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import hashlib
import sqlite3

from fastapi.testclient import TestClient
import pytest

import ecorex.control_plane.repository as repository_module
from ecorex.control_plane import (
    REQUIRED_RELEASE_GATES,
    ControlPlaneRepository,
    ControlPrincipal,
    DurableUpdateSignalPoller,
    create_control_plane_app,
    migrate_control_plane_database,
)
from ecorex.release import build_unsigned_gate_bundle
from ecorex.update import (
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
)


ADMIN_TOKEN = "admin-token-12345678901234567890"
CLIENT_TOKEN = "client-token-1234567890123456789"


class AcceptingVerifier:
    def verify(self, payload, signature) -> bool:
        assert payload and signature.key_id == "release-key"
        return True


@pytest.fixture(autouse=True)
def _isolate_bootstrap_publication_proof(monkeypatch) -> None:
    """Keep durable-signal tests independent from pointer proof fixtures."""

    monkeypatch.setattr(
        ControlPlaneRepository,
        "_require_bootstrap_index_proof",
        lambda *_args, **_kwargs: None,
    )


class Authenticator:
    admin = ControlPrincipal(
        subject="admin",
        client_id="admin-client",
        account_id="admin-account",
        organization_id="ops",
        roles=frozenset({"release_admin"}),
    )
    client = ControlPrincipal(
        subject="client",
        client_id="client-1",
        account_id="account-1",
        organization_id="org-1",
    )

    def authenticate(self, bearer_token: str) -> ControlPrincipal:
        if bearer_token == ADMIN_TOKEN:
            return self.admin
        if bearer_token == CLIENT_TOKEN:
            return self.client
        raise PermissionError("invalid token")


@pytest.fixture(autouse=True)
def _migrated_control_plane_database(tmp_path) -> None:
    migrate_control_plane_database(tmp_path / "control.db")


def _signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="release-key",
        value=base64.b64encode(b"test-signature").decode(),
    )


def _manifest() -> ReleaseManifest:
    payload = b"signed core package"
    return ReleaseManifest(
        schema_version=1,
        release_id="release-1.0.1-stable",
        version="1.0.1",
        build_digest=hashlib.sha256(b"build-1.0.1").hexdigest(),
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T12:00:00+08:00",
        sources=(
            ReleaseSource(
                "mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://mirror.example/v1"
            ),
            ReleaseSource(
                "github", SourceKind.GITHUB_RELEASE, 1, "https://github.example/v1"
            ),
            ReleaseSource(
                "cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.example/v1"
            ),
        ),
        artifacts=(
            ReleaseArtifact(
                artifact_id="core-windows-x64",
                platform="windows",
                architecture="x64",
                file_name="ecorex-core.zip",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                signature=_signature(),
            ),
        ),
        signature=_signature(),
    )


def _gate_bundle(manifest: ReleaseManifest) -> dict:
    publication = "publication-receipt:sha256:" + "a" * 64
    gates = {}
    for gate in REQUIRED_RELEASE_GATES:
        if gate in {"github-release", "mirror-sync", "cdn-sync"}:
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


def _seed(repository: ControlPlaneRepository):
    manifest = _manifest()
    repository.create_candidate(
        manifest,
        manifest_file_sha256=hashlib.sha256(manifest.to_json().encode()).hexdigest(),
        actor=Authenticator.admin,
        client_request_id="candidate-signal-bus",
    )
    repository.record_gate_bundle(
        manifest.release_id,
        _gate_bundle(manifest),
        actor=Authenticator.admin,
        client_request_id="signal-bus-gate-bundle",
    )
    repository.publish(
        manifest.release_id,
        actor=Authenticator.admin,
        client_request_id="publish-signal-bus",
    )
    rollout = repository.create_rollout(
        manifest.release_id,
        percentage=100,
        organizations=["org-1"],
        accounts=[],
        minimum_compatible_version=None,
        actor=Authenticator.admin,
        client_request_id="rollout-signal-bus",
    )
    return manifest, rollout


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _ws_url() -> str:
    return (
        "/api/v1/client/updates/ws?channel=stable&platform=windows&"
        "architecture=x64&current_version=1.0.0"
    )


def test_two_app_instances_deliver_activation_and_kill_from_durable_signal_order(
    tmp_path,
) -> None:
    database = tmp_path / "control.db"
    writer = ControlPlaneRepository(database, verifier=AcceptingVerifier())
    reader = ControlPlaneRepository(database, verifier=AcceptingVerifier())
    manifest, rollout = _seed(writer)
    authenticator = Authenticator()
    app_a = create_control_plane_app(
        writer,
        authenticator=authenticator,
        signal_consumer_id="control-node-a",
        signal_poll_interval_seconds=0.01,
    )
    app_b = create_control_plane_app(
        reader,
        authenticator=authenticator,
        signal_consumer_id="control-node-b",
        signal_poll_interval_seconds=0.01,
    )

    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        with client_b.websocket_connect(
            _ws_url(), headers=_headers(CLIENT_TOKEN)
        ) as socket:
            response = client_a.post(
                f"/api/v1/admin/rollouts/{rollout.rollout_id}/activate",
                headers=_headers(ADMIN_TOKEN),
                json={"client_request_id": "activate-on-node-a"},
            )
            assert response.status_code == 200
            activation_signal = socket.receive_json()
            killed = client_a.post(
                "/api/v1/admin/channels/stable/kill-switch",
                headers=_headers(ADMIN_TOKEN),
                json={"client_request_id": "kill-on-node-a"},
            )
            assert killed.status_code == 200
            assert killed.json()["halted_rollout_ids"] == [rollout.rollout_id]
            halted_signal = socket.receive_json()
        feed_after_kill = client_a.get(
            "/api/v1/releases/latest",
            params={
                "channel": "stable",
                "platform": "windows",
                "architecture": "x64",
                "current_version": "1.0.0",
            },
            headers=_headers(CLIENT_TOKEN),
        )

    durable = reader.read_update_signals(after_sequence=0).signals
    assert [item.signal_type for item in durable] == [
        "rollout.activated",
        "rollout.halted",
        "channel.killed",
    ]
    assert activation_signal == {
        "schema_version": 1,
        "event_id": durable[0].event_id,
        "event_type": "update.available",
        "release_id": manifest.release_id,
        "version": manifest.version,
        "build_digest": manifest.build_digest,
        "channel": "stable",
    }
    assert halted_signal == {
        **activation_signal,
        "event_id": durable[1].event_id,
    }
    assert activation_signal["event_id"].startswith("control_signal_")
    assert feed_after_kill.status_code == 204
    assert reader.update_signal_consumer_cursor("control-node-b") >= 1
    assert not app_a.state.update_signal_poller.running
    assert not app_b.state.update_signal_poller.running


class RecordingBroadcaster:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.event_ids: list[str] = []
        self.fail_once = fail_once

    async def broadcast_signal(self, repository, signal) -> int:
        del repository
        self.event_ids.append(signal.event_id)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated crash before cursor acknowledgement")
        return 1


def test_signal_replay_is_idempotent_across_crash_and_restart(tmp_path) -> None:
    repository = ControlPlaneRepository(
        tmp_path / "control.db", verifier=AcceptingVerifier()
    )
    _manifest_value, rollout = _seed(repository)
    first = repository.rollout_action(
        rollout.rollout_id,
        "activate",
        actor=Authenticator.admin,
        client_request_id="activate-replay",
    )
    replay = repository.rollout_action(
        rollout.rollout_id,
        "activate",
        actor=Authenticator.admin,
        client_request_id="activate-replay",
    )
    assert replay == first
    batch = repository.read_update_signals(after_sequence=0)
    assert len(batch.signals) == 1

    async def exercise() -> tuple[list[str], list[str], list[str]]:
        failed = RecordingBroadcaster(fail_once=True)
        first_poller = DurableUpdateSignalPoller(
            repository,
            failed,
            consumer_id="restartable-node",
            poll_interval_seconds=0.01,
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            await first_poller.poll_once()
        assert repository.update_signal_consumer_cursor("restartable-node") == 0

        recovered = RecordingBroadcaster()
        second_poller = DurableUpdateSignalPoller(
            repository,
            recovered,
            consumer_id="restartable-node",
            poll_interval_seconds=0.01,
        )
        await second_poller.poll_once()
        assert repository.update_signal_consumer_cursor("restartable-node") == 1

        after_restart = RecordingBroadcaster()
        third_poller = DurableUpdateSignalPoller(
            repository,
            after_restart,
            consumer_id="restartable-node",
            poll_interval_seconds=0.01,
        )
        await third_poller.start()
        await asyncio.sleep(0.04)
        await third_poller.close()
        return failed.event_ids, recovered.event_ids, after_restart.event_ids

    failed_ids, recovered_ids, after_restart_ids = asyncio.run(exercise())
    assert failed_ids == recovered_ids == [batch.signals[0].event_id]
    assert after_restart_ids == []


def test_missed_hint_never_changes_signed_feed_authority(tmp_path) -> None:
    repository = ControlPlaneRepository(
        tmp_path / "control.db", verifier=AcceptingVerifier()
    )
    manifest, rollout = _seed(repository)
    repository.rollout_action(
        rollout.rollout_id,
        "activate",
        actor=Authenticator.admin,
        client_request_id="activate-without-signal-consumer",
    )
    # No app or WSS poller was running when the signal was committed.  A later
    # client still recovers from the canonical signed feed.
    app = create_control_plane_app(
        repository,
        authenticator=Authenticator(),
        signal_consumer_id="late-node",
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/releases/latest",
            params={
                "channel": "stable",
                "platform": "windows",
                "architecture": "x64",
                "current_version": "1.0.0",
            },
            headers=_headers(CLIENT_TOKEN),
        )
    assert response.status_code == 200
    assert ReleaseManifest.from_json(response.content) == manifest


def test_signal_retention_detects_cursor_gap_and_preserves_monotonic_ids(
    tmp_path, monkeypatch
) -> None:
    old_time = "2026-06-01T00:00:00+00:00"
    monkeypatch.setattr(repository_module, "_now", lambda: old_time)
    repository = ControlPlaneRepository(
        tmp_path / "control.db", verifier=AcceptingVerifier()
    )
    _manifest_value, rollout = _seed(repository)
    for index, action in enumerate(("activate", "pause", "activate"), start=1):
        repository.rollout_action(
            rollout.rollout_id,
            action,
            actor=Authenticator.admin,
            client_request_id=f"retention-{index}-{action}",
        )
    before = repository.read_update_signals(after_sequence=0)
    assert [item.sequence for item in before.signals] == [1, 2, 3]

    deleted = repository.prune_update_signals(
        before=datetime(2026, 7, 1, tzinfo=UTC), retain_latest=1
    )
    assert deleted == 2
    retained = repository.read_update_signals(after_sequence=0)
    assert retained.gap_detected is True
    assert retained.retained_floor_sequence == 3
    assert [item.sequence for item in retained.signals] == [3]

    gap_broadcaster = RecordingBroadcaster()
    gap_poller = DurableUpdateSignalPoller(
        repository,
        gap_broadcaster,
        consumer_id="retention-gap-node",
        poll_interval_seconds=0.01,
    )
    asyncio.run(gap_poller.poll_once())
    assert gap_broadcaster.event_ids == [retained.signals[0].event_id]

    repository.rollout_action(
        rollout.rollout_id,
        "pause",
        actor=Authenticator.admin,
        client_request_id="retention-after-prune",
    )
    latest = repository.read_update_signals(after_sequence=3)
    assert [item.sequence for item in latest.signals] == [4]

    connection = sqlite3.connect(repository.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE control_update_signals SET event_id='tampered' WHERE sequence=4"
            )
        row = connection.execute(
            "SELECT event_id,dedupe_key,signal_type,channel,rollout_id,release_id,"
            "created_at FROM control_update_signals WHERE sequence=4"
        ).fetchone()
    finally:
        connection.close()
    serialized = "\0".join(str(value) for value in row)
    assert "account-1" not in serialized
    assert "org-1" not in serialized
    assert ADMIN_TOKEN not in serialized


def test_kill_switch_emits_bounded_policy_facts_in_same_transaction(tmp_path) -> None:
    repository = ControlPlaneRepository(
        tmp_path / "control.db", verifier=AcceptingVerifier()
    )
    _manifest_value, rollout = _seed(repository)
    repository.rollout_action(
        rollout.rollout_id,
        "activate",
        actor=Authenticator.admin,
        client_request_id="activate-before-kill-signals",
    )
    killed = repository.kill_channel(
        ReleaseChannel.STABLE,
        actor=Authenticator.admin,
        client_request_id="kill-signal-bus",
    )
    assert killed.halted_rollout_ids == [rollout.rollout_id]
    repository.kill_channel(
        ReleaseChannel.STABLE,
        actor=Authenticator.admin,
        client_request_id="kill-signal-bus",
    )
    repository.clear_channel_kill(
        ReleaseChannel.STABLE,
        actor=Authenticator.admin,
        client_request_id="clear-signal-bus",
    )
    signals = repository.read_update_signals(after_sequence=0).signals
    assert [item.signal_type for item in signals] == [
        "rollout.activated",
        "rollout.halted",
        "channel.killed",
        "channel.kill_cleared",
    ]


def test_revocation_hint_reuses_rollout_targeting_without_exposing_targets(
    tmp_path,
) -> None:
    repository = ControlPlaneRepository(
        tmp_path / "control.db", verifier=AcceptingVerifier()
    )
    manifest, rollout = _seed(repository)
    repository.rollout_action(
        rollout.rollout_id,
        "activate",
        actor=Authenticator.admin,
        client_request_id="activate-target-check",
    )
    repository.rollout_action(
        rollout.rollout_id,
        "pause",
        actor=Authenticator.admin,
        client_request_id="pause-target-check",
    )
    signal = repository.read_update_signals(after_sequence=1).signals[0]
    targeted = repository.hint_manifest_for_client(
        signal,
        Authenticator.client,
        platform="windows",
        architecture="x64",
        current_version="1.0.0",
    )
    outsider = repository.hint_manifest_for_client(
        signal,
        ControlPrincipal(
            subject="outsider",
            client_id="client-2",
            account_id="account-2",
            organization_id="org-2",
        ),
        platform="windows",
        architecture="x64",
        current_version="1.0.0",
    )
    assert targeted == manifest
    assert outsider is None
    assert signal.model_dump() == {
        "sequence": 2,
        "event_id": signal.event_id,
        "signal_type": "rollout.paused",
        "channel": "stable",
        "rollout_id": rollout.rollout_id,
        "release_id": manifest.release_id,
        "created_at": signal.created_at,
    }


def test_signal_and_rollout_state_rollback_together_on_transaction_failure(
    tmp_path, monkeypatch
) -> None:
    repository = ControlPlaneRepository(
        tmp_path / "control.db", verifier=AcceptingVerifier()
    )
    _manifest_value, rollout = _seed(repository)

    def fail_receipt(*_args, **_kwargs):
        raise RuntimeError("simulated idempotency persistence failure")

    monkeypatch.setattr(repository, "_remember", fail_receipt)
    with pytest.raises(RuntimeError, match="idempotency persistence"):
        repository.rollout_action(
            rollout.rollout_id,
            "activate",
            actor=Authenticator.admin,
            client_request_id="activate-must-rollback",
        )

    restored = {
        item.rollout_id: item for item in repository.admin_resume_facts()["rollouts"]
    }
    assert restored[rollout.rollout_id].status == "draft"
    assert repository.read_update_signals(after_sequence=0).signals == []


def test_signal_instance_identity_fails_closed(tmp_path, monkeypatch) -> None:
    repository = ControlPlaneRepository(
        tmp_path / "control.db", verifier=AcceptingVerifier()
    )
    with pytest.raises(ValueError, match="consumer identity"):
        create_control_plane_app(
            repository,
            authenticator=Authenticator(),
            signal_consumer_id="",
        )
    monkeypatch.setenv("ECOREX_CONTROL_PLANE_INSTANCE_ID", "contains spaces")
    with pytest.raises(ValueError, match="consumer identity"):
        create_control_plane_app(repository, authenticator=Authenticator())
