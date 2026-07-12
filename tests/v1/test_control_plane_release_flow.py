from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import ecorex.control_plane.repository as repository_module

from ecorex.control_plane import (
    REQUIRED_RELEASE_GATES,
    ControlPlaneConflict,
    ControlPlaneRepository,
    ControlPrincipal,
    create_control_plane_app,
    migrate_control_plane_database,
)
from ecorex.control_plane.app import UpdateSignalHub, _ClientConnection
from ecorex.release.signing import Ed25519MemorySigner
from ecorex.update import (
    Ed25519SignatureVerifier,
    ROLLBACK_AUTHORIZATION_HEADER,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
    RollbackAuthorizationVerifier,
)


class AcceptingVerifier:
    def verify(self, payload, signature) -> bool:
        assert payload and signature.key_id == "release-key"
        return True


class Authenticator:
    principals = {
        "admin-token-12345678901234567890": ControlPrincipal(
            subject="admin-1",
            client_id="admin-client",
            account_id="admin-account",
            organization_id="ops",
            roles=frozenset({"release_admin"}),
        ),
        "client-one-token-123456789012345": ControlPrincipal(
            subject="user-1",
            client_id="client-1",
            account_id="account-1",
            organization_id="org-1",
        ),
        "client-two-token-123456789012345": ControlPrincipal(
            subject="user-2",
            client_id="client-2",
            account_id="account-2",
            organization_id="org-2",
        ),
    }

    def authenticate(self, bearer_token):
        try:
            return self.principals[bearer_token]
        except KeyError as error:
            raise PermissionError("invalid token") from error


@pytest.fixture(autouse=True)
def _migrated_control_plane_database(tmp_path, monkeypatch) -> None:
    migrate_control_plane_database(tmp_path / "control.db")
    # These tests exercise generic release/rollout behavior with an accepting
    # signature verifier. The real stable pointer proof, replay, CAS and
    # readback boundary is covered by test_bootstrap_index_publication_saga.py.
    monkeypatch.setattr(
        ControlPlaneRepository,
        "_require_bootstrap_index_proof",
        lambda *_args, **_kwargs: None,
    )


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
            ReleaseSource("mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://mirror.example/v1"),
            ReleaseSource("github", SourceKind.GITHUB_RELEASE, 1, "https://github.example/v1"),
            ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.example/v1"),
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


def _manifest_variant(*, release_id: str, version: str) -> ReleaseManifest:
    payload = _manifest().to_dict()
    payload["release_id"] = release_id
    payload["version"] = version
    payload["build_digest"] = hashlib.sha256(f"build-{version}".encode()).hexdigest()
    return ReleaseManifest.from_dict(payload)


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _published_rollout(client: TestClient):
    manifest = _manifest()
    admin = _headers("admin-token-12345678901234567890")
    created = client.post(
        "/api/v1/admin/releases",
        json={"manifest": manifest.to_dict(), "client_request_id": "candidate-1"},
        headers=admin,
    )
    assert created.status_code == 201
    blocked = client.post(
        f"/api/v1/admin/releases/{manifest.release_id}/publish",
        json={"client_request_id": "publish-too-early"},
        headers=admin,
    )
    assert blocked.status_code == 409
    for gate in sorted(REQUIRED_RELEASE_GATES):
        response = client.put(
            f"/api/v1/admin/releases/{manifest.release_id}/gates/{gate}",
            json={
                "status": "passed",
                "evidence": f"ci://run/{gate}",
                "client_request_id": f"gate-{gate}",
            },
            headers=admin,
        )
        assert response.status_code == 200
    published = client.post(
        f"/api/v1/admin/releases/{manifest.release_id}/publish",
        json={"client_request_id": "publish-1"},
        headers=admin,
    )
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    rollout = client.post(
        "/api/v1/admin/rollouts",
        json={
            "release_id": manifest.release_id,
            "percentage": 100,
            "target_organization_ids": ["org-1"],
            "target_account_ids": [],
            "minimum_compatible_version": "0.9.0",
            "client_request_id": "rollout-1",
        },
        headers=admin,
    )
    assert rollout.status_code == 201
    return manifest, rollout.json(), admin


def _publish_release(
    client: TestClient,
    manifest: ReleaseManifest,
    *,
    prefix: str,
    admin: dict[str, str],
) -> None:
    assert client.post(
        "/api/v1/admin/releases",
        json={
            "manifest": manifest.to_dict(),
            "client_request_id": f"{prefix}-candidate",
        },
        headers=admin,
    ).status_code == 201
    for gate in sorted(REQUIRED_RELEASE_GATES):
        assert client.put(
            f"/api/v1/admin/releases/{manifest.release_id}/gates/{gate}",
            json={
                "status": "passed",
                "evidence": f"ci://{prefix}/{gate}",
                "client_request_id": f"{prefix}-gate-{gate}",
            },
            headers=admin,
        ).status_code == 200
    assert client.post(
        f"/api/v1/admin/releases/{manifest.release_id}/publish",
        json={"client_request_id": f"{prefix}-publish"},
        headers=admin,
    ).status_code == 200


def test_admin_rollback_issues_exact_signed_client_authorization(tmp_path) -> None:
    repository = ControlPlaneRepository(
        tmp_path / "control.db", verifier=AcceptingVerifier()
    )
    rollback_signer = Ed25519MemorySigner(
        "rollback-key", Ed25519PrivateKey.generate()
    )
    app = create_control_plane_app(
        repository,
        authenticator=Authenticator(),
        rollback_signer=rollback_signer,
    )
    admin = _headers("admin-token-12345678901234567890")
    client_headers = _headers("client-one-token-123456789012345")
    target = _manifest()
    source = _manifest_variant(
        release_id="release-1.0.2-stable", version="1.0.2"
    )

    with TestClient(app) as client:
        _publish_release(client, target, prefix="known-good", admin=admin)
        known_good = client.post(
            "/api/v1/admin/rollouts",
            json={
                "release_id": target.release_id,
                "percentage": 100,
                "target_organization_ids": [],
                "target_account_ids": [],
                "minimum_compatible_version": None,
                "client_request_id": "known-good-rollout",
            },
            headers=admin,
        )
        assert known_good.status_code == 201
        assert client.post(
            f"/api/v1/admin/rollouts/{known_good.json()['rollout_id']}/activate",
            json={"client_request_id": "known-good-activate"},
            headers=admin,
        ).status_code == 200
        _publish_release(client, source, prefix="source", admin=admin)

        request = {
            "source_release_id": source.release_id,
            "target_release_id": target.release_id,
            "percentage": 100,
            "target_organization_ids": ["org-1"],
            "target_account_ids": [],
            "authorization_ttl_seconds": 300,
            "client_request_id": "rollback-create-1",
        }
        created = client.post(
            "/api/v1/admin/rollbacks", json=request, headers=admin
        )
        assert created.status_code == 201
        rollback = created.json()
        assert client.post(
            "/api/v1/admin/rollbacks", json=request, headers=admin
        ).json() == rollback
        activated = client.post(
            f"/api/v1/admin/rollbacks/{rollback['rollback_id']}/activate",
            json={"client_request_id": "rollback-activate-1"},
            headers=admin,
        )
        assert activated.status_code == 200
        assert activated.json()["status"] == "active"

        without_identity = client.get(
            "/api/v1/releases/latest",
            params={
                "channel": "stable",
                "platform": "windows",
                "architecture": "x64",
                "current_version": source.version,
            },
            headers=client_headers,
        )
        assert without_identity.status_code == 204
        nonce = "n" * 43
        selected = client.get(
            "/api/v1/releases/latest",
            params={
                "channel": "stable",
                "platform": "windows",
                "architecture": "x64",
                "current_version": source.version,
                "current_release_id": source.release_id,
                "current_build_digest": source.build_digest,
                "rollback_nonce": nonce,
            },
            headers=client_headers,
        )
        assert selected.status_code == 200
        assert ReleaseManifest.from_json(selected.content) == target
        token = selected.headers[ROLLBACK_AUTHORIZATION_HEADER]

    source_artifact = source.artifact("core-windows-x64")
    claims = RollbackAuthorizationVerifier(
        Ed25519SignatureVerifier(
            {"rollback-key": rollback_signer.public_key_bytes}
        )
    ).verify(
        token,
        current={
            "release_id": source.release_id,
            "version": source.version,
            "build_digest": source.build_digest,
            "artifact_id": source_artifact.artifact_id,
            "artifact_sha256": source_artifact.sha256,
            "channel": source.channel.value,
        },
        target=target,
        platform="windows",
        architecture="x64",
        expected_nonce=nonce,
        expected_client_id="client-1",
    )
    assert claims.rollback_id == rollback["rollback_id"]

    async def push_hint() -> dict:
        hub = UpdateSignalHub()
        connection = _ClientConnection(
            principal=Authenticator.principals[
                "client-one-token-123456789012345"
            ],
            channel=ReleaseChannel.STABLE,
            platform="windows",
            architecture="x64",
            current_version=source.version,
            current_release_id=source.release_id,
            current_build_digest=source.build_digest,
            queue=asyncio.Queue(maxsize=4),
        )
        assert await hub.add(connection)
        signal = repository.rollback_signal_for_request(
            actor=Authenticator.principals[
                "admin-token-12345678901234567890"
            ],
            client_request_id="rollback-activate-1",
            rollback_id=rollback["rollback_id"],
            action="activate",
        )
        assert await hub.broadcast_signal(repository, signal) == 1
        return await connection.queue.get()

    hint = asyncio.run(push_hint())
    assert hint["release_id"] == target.release_id
    assert hint["version"] == target.version
    with sqlite3.connect(tmp_path / "control.db") as connection:
        actions = {
            row[0]
            for row in connection.execute(
                "SELECT action FROM control_admin_audit WHERE target_id=?",
                (rollback["rollback_id"],),
            )
        }
    assert actions == {"rollback.create", "rollback.activate"}


def test_admin_gates_rollout_targeting_pause_and_distribution(tmp_path) -> None:
    repository = ControlPlaneRepository(
        tmp_path / "control.db", verifier=AcceptingVerifier()
    )
    client = TestClient(
        create_control_plane_app(repository, authenticator=Authenticator())
    )
    assert client.post("/api/v1/admin/releases", json={}).status_code == 401
    assert client.get(
        "/api/v1/admin/distribution",
        headers=_headers("client-one-token-123456789012345"),
    ).status_code == 403

    manifest, rollout, admin = _published_rollout(client)
    activated = client.post(
        f"/api/v1/admin/rollouts/{rollout['rollout_id']}/activate",
        json={"client_request_id": "activate-rollout-1"},
        headers=admin,
    )
    assert activated.status_code == 200

    params = {
        "channel": "stable",
        "platform": "windows",
        "architecture": "x64",
        "current_version": "1.0.0",
    }
    eligible = client.get(
        "/api/v1/releases/latest",
        params=params,
        headers=_headers("client-one-token-123456789012345"),
    )
    assert eligible.status_code == 200
    assert ReleaseManifest.from_json(eligible.content) == manifest
    assert eligible.headers["content-type"].startswith(
        "application/vnd.ecorex.release+json"
    )
    not_targeted = client.get(
        "/api/v1/releases/latest",
        params=params,
        headers=_headers("client-two-token-123456789012345"),
    )
    assert not_targeted.status_code == 204
    current = client.get(
        "/api/v1/releases/latest",
        params={**params, "current_version": "1.0.1"},
        headers=_headers("client-one-token-123456789012345"),
    )
    assert current.status_code == 204

    distribution = client.get("/api/v1/admin/distribution", headers=admin).json()
    assert distribution["total_clients"] == 2
    assert distribution["versions"] == {"1.0.0": 1, "1.0.1": 1}

    paused = client.post(
        f"/api/v1/admin/rollouts/{rollout['rollout_id']}/pause",
        json={"client_request_id": "pause-rollout-1"},
        headers=admin,
    )
    assert paused.status_code == 200
    assert client.get(
        "/api/v1/releases/latest",
        params=params,
        headers=_headers("client-one-token-123456789012345"),
    ).status_code == 204
    killed = client.post(
        "/api/v1/admin/channels/stable/kill-switch",
        json={"client_request_id": "kill-stable"},
        headers=admin,
    )
    assert killed.status_code == 200
    assert killed.json()["halted_rollout_ids"] == [rollout["rollout_id"]]
    assert killed.json()["kill_switch_active"] is True
    assert client.post(
        f"/api/v1/admin/rollouts/{rollout['rollout_id']}/activate",
        json={"client_request_id": "activate-after-kill"},
        headers=admin,
    ).status_code == 409

    replacement = client.post(
        "/api/v1/admin/rollouts",
        json={
            "release_id": manifest.release_id,
            "percentage": 100,
            "target_organization_ids": ["org-1"],
            "target_account_ids": [],
            "minimum_compatible_version": None,
            "client_request_id": "rollout-after-kill",
        },
        headers=admin,
    ).json()
    assert client.post(
        f"/api/v1/admin/rollouts/{replacement['rollout_id']}/activate",
        json={"client_request_id": "activate-new-while-killed"},
        headers=admin,
    ).status_code == 409
    cleared = client.post(
        "/api/v1/admin/channels/stable/kill-switch/clear",
        json={"client_request_id": "clear-stable-kill"},
        headers=admin,
    )
    assert cleared.status_code == 200
    assert cleared.json()["kill_switch_active"] is False
    assert client.post(
        f"/api/v1/admin/rollouts/{replacement['rollout_id']}/activate",
        json={"client_request_id": "activate-after-clear"},
        headers=admin,
    ).status_code == 200


def test_active_rollout_hub_pushes_hint_but_feed_remains_authority(tmp_path) -> None:
    repository = ControlPlaneRepository(
        tmp_path / "control.db", verifier=AcceptingVerifier()
    )
    app = create_control_plane_app(repository, authenticator=Authenticator())
    client = TestClient(app)
    manifest, rollout, admin = _published_rollout(client)
    principal = Authenticator.principals["client-one-token-123456789012345"]

    async def exercise_hub():
        hub = UpdateSignalHub()
        connection = _ClientConnection(
            principal=principal,
            channel=ReleaseChannel.STABLE,
            platform="windows",
            architecture="x64",
            current_version="1.0.0",
            queue=asyncio.Queue(maxsize=16),
        )
        await hub.add(connection)
        activated = repository.rollout_action(
            rollout["rollout_id"],
            "activate",
            actor=Authenticator.principals["admin-token-12345678901234567890"],
            client_request_id="activate-push",
        )
        committed = repository.rollout_signal_for_request(
            actor=Authenticator.principals["admin-token-12345678901234567890"],
            client_request_id="activate-push",
            rollout_id=activated.rollout_id,
            action="activate",
        )
        delivered = await hub.broadcast_signal(repository, committed)
        outbound = connection.queue.get_nowait()
        forged = committed.model_copy(update={"event_id": "forged_signal"})
        with pytest.raises(ControlPlaneConflict, match="committed durable fact"):
            await hub.broadcast_signal(repository, forged)
        assert connection.queue.empty()
        await hub.remove(principal.client_id, connection)
        return delivered, committed, outbound

    delivered, committed, signal = asyncio.run(exercise_hub())

    assert delivered == 1
    assert "broadcast_rollout" not in UpdateSignalHub.__dict__
    assert signal == {
        "schema_version": 1,
        "event_id": committed.event_id,
        "event_type": "update.available",
        "release_id": manifest.release_id,
        "version": manifest.version,
        "build_digest": manifest.build_digest,
        "channel": "stable",
    }
    assert signal["event_id"].startswith("control_signal_")


def test_admin_page_and_resume_restore_persisted_state_after_app_rebuild(tmp_path) -> None:
    database = tmp_path / "control.db"
    repository = ControlPlaneRepository(database, verifier=AcceptingVerifier())
    admin_headers = _headers("admin-token-12345678901234567890")
    with TestClient(
        create_control_plane_app(repository, authenticator=Authenticator())
    ) as first_client:
        manifest, first_rollout, _admin = _published_rollout(first_client)
        second_rollout_response = first_client.post(
            "/api/v1/admin/rollouts",
            json={
                "release_id": manifest.release_id,
                "percentage": 100,
                "target_organization_ids": [],
                "target_account_ids": [],
                "minimum_compatible_version": None,
                "client_request_id": "rollout-second",
            },
            headers=admin_headers,
        )
        assert second_rollout_response.status_code == 201
        second_rollout = second_rollout_response.json()
        assert first_client.post(
            f"/api/v1/admin/rollouts/{second_rollout['rollout_id']}/activate",
            json={"client_request_id": "activate-second"},
            headers=admin_headers,
        ).status_code == 200
        assert first_client.get(
            "/api/v1/releases/latest",
            params={
                "channel": "stable",
                "platform": "windows",
                "architecture": "x64",
                "current_version": "1.0.0",
            },
            headers=_headers("client-one-token-123456789012345"),
        ).status_code == 200
        killed = first_client.post(
            "/api/v1/admin/channels/stable/kill-switch",
            json={"client_request_id": "kill-before-rebuild"},
            headers=admin_headers,
        )
        assert killed.status_code == 200
        assert set(killed.json()["halted_rollout_ids"]) == {
            first_rollout["rollout_id"],
            second_rollout["rollout_id"],
        }
        next_manifest = _manifest_variant(
            release_id="release-1.0.2-stable",
            version="1.0.2",
        )
        assert first_client.post(
            "/api/v1/admin/releases",
            json={
                "manifest": next_manifest.to_dict(),
                "client_request_id": "candidate-after-kill",
            },
            headers=admin_headers,
        ).status_code == 201

    rebuilt_repository = ControlPlaneRepository(
        database,
        verifier=AcceptingVerifier(),
    )
    with TestClient(
        create_control_plane_app(rebuilt_repository, authenticator=Authenticator())
    ) as rebuilt_client:
        for _refresh in range(2):
            page = rebuilt_client.get("/admin")
            assert page.status_code == 200
            assert "管理员令牌" in page.text
            assert "no-store" in page.headers["cache-control"]
            assert "default-src 'none'" in page.headers["content-security-policy"]
            assert page.headers["x-frame-options"] == "DENY"

        unauthorized = rebuilt_client.get("/api/v1/admin/resume")
        assert unauthorized.status_code == 401
        forbidden = rebuilt_client.get(
            "/api/v1/admin/resume",
            headers=_headers("client-one-token-123456789012345"),
        )
        assert forbidden.status_code == 403

        response = rebuilt_client.get(
            "/api/v1/admin/resume",
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["x-content-type-options"] == "nosniff"
        restored = response.json()
        assert restored["latest_candidate_id"] == next_manifest.release_id
        assert restored["latest_rollout_id"] == second_rollout["rollout_id"]
        candidates = {item["release_id"]: item for item in restored["candidates"]}
        assert candidates[manifest.release_id]["status"] == "published"
        assert candidates[next_manifest.release_id]["status"] == "candidate"
        rollouts = {item["rollout_id"]: item for item in restored["rollouts"]}
        assert rollouts[first_rollout["rollout_id"]]["status"] == "halted"
        assert rollouts[second_rollout["rollout_id"]]["status"] == "halted"
        channels = {
            item["channel"]: item for item in restored["channel_kill_switches"]
        }
        assert channels["stable"]["kill_switch_active"] is True
        assert set(channels["stable"]["halted_rollout_ids"]) == set(rollouts)
        assert channels["canary"]["kill_switch_active"] is False
        assert restored["distribution"] == {
            "total_clients": 1,
            "versions": {"1.0.0": 1},
            "update_states": {"idle": 1},
        }


def test_admin_resume_uses_one_read_snapshot_during_concurrent_commit(tmp_path) -> None:
    database = tmp_path / "control.db"
    seed_repository = ControlPlaneRepository(database, verifier=AcceptingVerifier())
    with TestClient(
        create_control_plane_app(seed_repository, authenticator=Authenticator())
    ) as client:
        manifest_value, rollout, _admin = _published_rollout(client)
        assert client.post(
            f"/api/v1/admin/rollouts/{rollout['rollout_id']}/activate",
            json={"client_request_id": "activate-before-snapshot"},
            headers=_headers("admin-token-12345678901234567890"),
        ).status_code == 200

    reader = ControlPlaneRepository(database, verifier=AcceptingVerifier())
    writer = ControlPlaneRepository(database, verifier=AcceptingVerifier())
    snapshot_pinned = threading.Event()
    writer_committed = threading.Event()
    original_candidate = reader._candidate
    original_connect = reader._connect
    connection_count = 0

    def counted_connect():
        nonlocal connection_count
        connection_count += 1
        return original_connect()

    def pause_after_snapshot(connection, release_id):
        snapshot_pinned.set()
        if not writer_committed.wait(timeout=10):
            raise TimeoutError("concurrent writer did not commit")
        return original_candidate(connection, release_id)

    reader._connect = counted_connect  # type: ignore[method-assign]
    reader._candidate = pause_after_snapshot  # type: ignore[method-assign]
    admin_principal = Authenticator.principals["admin-token-12345678901234567890"]
    concurrent_manifest = _manifest_variant(
        release_id="release-1.0.2-concurrent",
        version="1.0.2",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        old_snapshot_future = executor.submit(reader.admin_resume_facts)
        assert snapshot_pinned.wait(timeout=10)
        try:
            killed = writer.kill_channel(
                ReleaseChannel.STABLE,
                actor=admin_principal,
                client_request_id="kill-during-read-snapshot",
            )
            assert killed.kill_switch_active is True
            assert writer.latest_for_client(
                Authenticator.principals["client-one-token-123456789012345"],
                channel=ReleaseChannel.STABLE,
                platform="windows",
                architecture="x64",
                current_version="1.0.0",
            ) is None
            writer.create_candidate(
                concurrent_manifest,
                actor=admin_principal,
                client_request_id="candidate-during-read-snapshot",
            )
        finally:
            writer_committed.set()
        old_snapshot = old_snapshot_future.result(timeout=10)

    assert connection_count == 1
    old_rollouts = {item.rollout_id: item for item in old_snapshot["rollouts"]}
    old_channels = {
        item.channel: item for item in old_snapshot["channel_kill_switches"]
    }
    assert old_rollouts[rollout["rollout_id"]].status == "active"
    assert old_channels["stable"].kill_switch_active is False
    assert old_snapshot["latest_candidate_id"] == manifest_value.release_id
    assert old_snapshot["distribution"].total_clients == 0

    fresh_snapshot = reader.admin_resume_facts()
    assert connection_count == 2
    fresh_rollouts = {item.rollout_id: item for item in fresh_snapshot["rollouts"]}
    fresh_channels = {
        item.channel: item for item in fresh_snapshot["channel_kill_switches"]
    }
    assert fresh_rollouts[rollout["rollout_id"]].status == "halted"
    assert fresh_channels["stable"].kill_switch_active is True
    assert fresh_snapshot["latest_candidate_id"] == concurrent_manifest.release_id
    assert fresh_snapshot["distribution"].total_clients == 1


def test_admin_resume_latest_ids_use_persisted_sequence_when_times_tie(
    tmp_path,
    monkeypatch,
) -> None:
    fixed_time = "2026-07-10T08:30:00+00:00"
    monkeypatch.setattr(repository_module, "_now", lambda: fixed_time)
    database = tmp_path / "control.db"
    repository = ControlPlaneRepository(database, verifier=AcceptingVerifier())
    admin = Authenticator.principals["admin-token-12345678901234567890"]
    first_manifest = _manifest()
    second_manifest = _manifest_variant(
        release_id="release-1.0.2-stable",
        version="1.0.2",
    )
    repository.create_candidate(
        first_manifest,
        actor=admin,
        client_request_id="tied-candidate-first",
    )
    repository.create_candidate(
        second_manifest,
        actor=admin,
        client_request_id="tied-candidate-second",
    )
    for gate in sorted(REQUIRED_RELEASE_GATES):
        repository.record_gate(
            first_manifest.release_id,
            gate,
            status="passed",
            evidence=f"ci://tied/{gate}",
            actor=admin,
            client_request_id=f"tied-gate-{gate}",
        )
    repository.publish(
        first_manifest.release_id,
        actor=admin,
        client_request_id="tied-publish",
    )
    first_rollout = repository.create_rollout(
        first_manifest.release_id,
        percentage=10,
        organizations=[],
        accounts=[],
        minimum_compatible_version=None,
        actor=admin,
        client_request_id="tied-rollout-first",
    )
    second_rollout = repository.create_rollout(
        first_manifest.release_id,
        percentage=20,
        organizations=[],
        accounts=[],
        minimum_compatible_version=None,
        actor=admin,
        client_request_id="tied-rollout-second",
    )

    first_snapshot = repository.admin_resume_facts()
    assert first_snapshot["latest_candidate_id"] == second_manifest.release_id
    assert first_snapshot["latest_rollout_id"] == second_rollout.rollout_id
    assert first_snapshot["latest_rollout_id"] != first_rollout.rollout_id

    rebuilt = ControlPlaneRepository(database, verifier=AcceptingVerifier())
    rebuilt_snapshot = rebuilt.admin_resume_facts()
    assert rebuilt_snapshot["latest_candidate_id"] == second_manifest.release_id
    assert rebuilt_snapshot["latest_rollout_id"] == second_rollout.rollout_id


def test_repeated_publish_preserves_original_publication_time(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "control.db"
    repository = ControlPlaneRepository(database, verifier=AcceptingVerifier())
    admin = Authenticator.principals["admin-token-12345678901234567890"]
    manifest = _manifest()
    repository.create_candidate(
        manifest,
        actor=admin,
        client_request_id="immutable-time-candidate",
    )
    for gate in sorted(REQUIRED_RELEASE_GATES):
        repository.record_gate(
            manifest.release_id,
            gate,
            status="passed",
            evidence=f"ci://immutable/{gate}",
            actor=admin,
            client_request_id=f"immutable-time-{gate}",
        )
    clock = {"value": "2026-07-10T08:00:00+00:00"}
    monkeypatch.setattr(repository_module, "_now", lambda: clock["value"])
    repository.publish(
        manifest.release_id,
        actor=admin,
        client_request_id="immutable-time-publish-one",
    )
    clock["value"] = "2026-07-10T09:00:00+00:00"
    repository.publish(
        manifest.release_id,
        actor=admin,
        client_request_id="immutable-time-publish-two",
    )
    with sqlite3.connect(database) as connection:
        published_at = connection.execute(
            "SELECT published_at FROM control_releases WHERE release_id = ?",
            (manifest.release_id,),
        ).fetchone()[0]
    assert published_at == "2026-07-10T08:00:00+00:00"


def test_repository_bounds_gate_evidence_even_without_http_validation(
    tmp_path,
) -> None:
    repository = ControlPlaneRepository(
        tmp_path / "control.db", verifier=AcceptingVerifier()
    )
    admin = Authenticator.principals["admin-token-12345678901234567890"]
    manifest = _manifest()
    repository.create_candidate(
        manifest,
        actor=admin,
        client_request_id="bounded-evidence-candidate",
    )
    with pytest.raises(ValueError, match="evidence"):
        repository.record_gate(
            manifest.release_id,
            "lint",
            status="passed",
            evidence="x" * 4097,
            actor=admin,
            client_request_id="bounded-evidence-gate",
        )
