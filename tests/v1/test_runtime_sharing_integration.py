from __future__ import annotations

import time

from fastapi.testclient import TestClient

from ecorex.connectors import InMemoryCredentialVault
from ecorex.runtime import RuntimeSettings, create_app
from ecorex.sharing import PublishedShare


TOKEN = "runtime-token-" + "r" * 32
CSRF = "csrf-token-" + "c" * 32


class Publisher:
    def __init__(self) -> None:
        self.published = []
        self.revoked = []

    async def publish(self, payload, *, idempotency_key):
        self.published.append((payload, idempotency_key))
        return PublishedShare(
            remote_snapshot_id="remote_" + payload.share_id,
            public_url=f"https://share.ecorex.test/s/{payload.share_id}",
        )

    async def revoke(self, remote_snapshot_id, *, idempotency_key):
        self.revoked.append((remote_snapshot_id, idempotency_key))


def headers(*, mutate: bool = False):
    result = {"Authorization": f"Bearer {TOKEN}"}
    if mutate:
        result.update({"Origin": "http://testserver", "X-EcoreX-CSRF": CSRF})
    return result


def wait_for_share(client: TestClient, share_id: str, status: str):
    for _ in range(100):
        response = client.get(f"/api/v1/shares/{share_id}", headers=headers())
        assert response.status_code == 200
        projection = response.json()
        if projection["status"] == status:
            return projection
        time.sleep(0.02)
    raise AssertionError(f"share {share_id} did not reach {status}")


def test_runtime_mounts_backend_authoritative_unique_share_and_revoke(tmp_path) -> None:
    publisher = Publisher()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=("http://testserver",),
            connector_vault=InMemoryCredentialVault(),
            share_publisher=publisher,
            share_public_hosts=frozenset({"share.ecorex.test"}),
        )
    )
    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/bootstrap", headers=headers())
        assert bootstrap.status_code == 200
        assert bootstrap.json()["share_service"] == {"state": "ready", "reason": None}

        thread_ids = []
        for index in range(2):
            created = client.post(
                "/api/v1/threads",
                headers=headers(mutate=True),
                json={
                    "title": f"会话 {index}",
                    "client_request_id": f"thread-{index}",
                },
            )
            assert created.status_code == 201
            thread_ids.append(created.json()["thread_id"])

        missing_csrf = client.post(
        f"/api/v1/threads/{thread_ids[0]}/shares",
        headers=headers(),
        json={"expires_in_hours": 24, "client_request_id": "share-denied"},
    )
        assert missing_csrf.status_code == 403

        shares = []
        for index, thread_id in enumerate(thread_ids):
            response = client.post(
            f"/api/v1/threads/{thread_id}/shares",
            headers=headers(mutate=True),
            json={"expires_in_hours": 24, "client_request_id": f"share-{index}"},
        )
            assert response.status_code == 201
            assert response.json()["status"] == "publishing"
            shares.append(
                wait_for_share(client, response.json()["share_id"], "published")
            )
        assert shares[0]["share_id"] != shares[1]["share_id"]
        assert shares[0]["public_url"] != shares[1]["public_url"]
        assert len(publisher.published) == 2

        duplicate = client.post(
        f"/api/v1/threads/{thread_ids[0]}/shares",
        headers=headers(mutate=True),
        json={"expires_in_hours": 24, "client_request_id": "share-0"},
    )
        assert duplicate.status_code == 201
        assert duplicate.json() == shares[0]
        assert len(publisher.published) == 2

        listed = client.get(
        f"/api/v1/threads/{thread_ids[0]}/shares",
        headers=headers(),
    )
        assert listed.status_code == 200
        assert listed.json() == {"items": [shares[0]], "count": 1}
        other_list = client.get(
        f"/api/v1/threads/{thread_ids[1]}/shares",
        headers=headers(),
    )
        assert other_list.json() == {"items": [shares[1]], "count": 1}
        assert client.get(
        "/api/v1/threads/thread_missing/shares", headers=headers()
        ).status_code == 404
        assert client.get(
        f"/api/v1/threads/{thread_ids[0]}/shares?limit=0", headers=headers()
        ).status_code == 422
        assert client.get(
        f"/api/v1/threads/{thread_ids[0]}/shares?limit=201", headers=headers()
        ).status_code == 422

        events = client.get(
        f"/api/v1/threads/{thread_ids[0]}/events",
        headers=headers(),
        ).json()["events"]
        assert len([event for event in events if event["event_type"] == "share.created"]) == 1
        assert all("public_url" not in event["payload"] for event in events)

        revoked = client.post(
        f"/api/v1/shares/{shares[0]['share_id']}/revoke",
        headers=headers(mutate=True),
        json={"client_request_id": "revoke-0"},
    )
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoking"
        settled = wait_for_share(client, shares[0]["share_id"], "revoked")
        assert settled["public_url"] is None
        assert len(publisher.revoked) == 1
        after_revoke = client.get(
        f"/api/v1/threads/{thread_ids[0]}/shares",
        headers=headers(),
        ).json()
        assert after_revoke["items"][0]["status"] == "revoked"
        assert after_revoke["items"][0]["public_url"] is None


def test_bootstrap_reports_share_unavailable_without_cloud_publisher(tmp_path) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=("http://testserver",),
            connector_vault=InMemoryCredentialVault(),
        )
    )
    response = TestClient(app).get("/api/v1/bootstrap", headers=headers())
    assert response.status_code == 200
    assert response.json()["share_service"] == {
        "state": "unavailable",
        "reason": "share_service_not_configured",
    }
