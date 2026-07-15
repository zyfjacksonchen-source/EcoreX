from __future__ import annotations

from fastapi.testclient import TestClient

from ecorex.connectors import InMemoryCredentialVault
from ecorex.runtime import RuntimeSettings, create_app


TOKEN = "runtime-token-" + "r" * 32
CSRF = "csrf-token-" + "c" * 32


def headers(*, mutate: bool = False):
    result = {"Authorization": f"Bearer {TOKEN}"}
    if mutate:
        result.update({"Origin": "http://testserver", "X-EcoreX-CSRF": CSRF})
    return result


def client(tmp_path):
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=("http://testserver",),
            connector_vault=InMemoryCredentialVault(),
        )
    )
    return TestClient(app), app


def create_task(http: TestClient, index: int, *, titled: bool = False) -> str:
    created = http.post(
        "/api/v1/threads",
        headers=headers(mutate=True),
        json={
            "title": f"显式标题 {index}" if titled else None,
            "client_request_id": f"thread-{index}",
        },
    )
    assert created.status_code == 201
    thread_id = created.json()["thread_id"]
    turn = http.post(
        f"/api/v1/threads/{thread_id}/turns",
        headers=headers(mutate=True),
        json={
            "input": f"  第 {index} 个任务\n需要整理成报告  ",
            "agent_model_id": "ecorex-chat",
            "image_model_id": "gpt-image-2",
            "client_message_id": f"message-{index}",
        },
    )
    assert turn.status_code == 202
    return thread_id


def test_thread_catalog_keyset_cursor_auto_title_and_tamper_rejection(tmp_path) -> None:
    http, _app = client(tmp_path)
    ids = [create_task(http, index, titled=index == 0) for index in range(5)]

    first = http.get(
        "/api/v1/threads?limit=2", headers=headers()
    )
    assert first.status_code == 200
    assert len(first.json()["items"]) == 2
    assert first.json()["next_cursor"]
    seen = [item["thread_id"] for item in first.json()["items"]]
    assert first.json()["items"][0]["title"].startswith("第 4 个任务")

    second = http.get(
        "/api/v1/threads",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
        headers=headers(),
    )
    assert second.status_code == 200
    seen.extend(item["thread_id"] for item in second.json()["items"])
    third = http.get(
        "/api/v1/threads",
        params={"limit": 2, "cursor": second.json()["next_cursor"]},
        headers=headers(),
    )
    assert third.status_code == 200
    seen.extend(item["thread_id"] for item in third.json()["items"])
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == set(ids)
    assert third.json()["next_cursor"] is None

    cursor = first.json()["next_cursor"]
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    assert http.get(
        "/api/v1/threads", params={"cursor": tampered}, headers=headers()
    ).status_code == 400
    assert http.get(
        "/api/v1/threads",
        params={"status": "archived", "cursor": cursor},
        headers=headers(),
    ).status_code == 400


def test_thread_rename_archive_restore_are_idempotent_without_stale_rollback(tmp_path) -> None:
    http, app = client(tmp_path)
    thread_id = create_task(http, 1)

    first = http.put(
        f"/api/v1/threads/{thread_id}",
        headers=headers(mutate=True),
        json={"title": "第一版标题", "client_request_id": "rename-1"},
    )
    second = http.put(
        f"/api/v1/threads/{thread_id}",
        headers=headers(mutate=True),
        json={"title": "第二版标题", "client_request_id": "rename-2"},
    )
    delayed_first = http.put(
        f"/api/v1/threads/{thread_id}",
        headers=headers(mutate=True),
        json={"title": "第一版标题", "client_request_id": "rename-1"},
    )
    assert first.status_code == second.status_code == delayed_first.status_code == 200
    assert delayed_first.json()["title"] == "第二版标题"

    archived = http.post(
        f"/api/v1/threads/{thread_id}/archive",
        headers=headers(mutate=True),
        json={"client_request_id": "archive-1"},
    )
    restored = http.post(
        f"/api/v1/threads/{thread_id}/restore",
        headers=headers(mutate=True),
        json={"client_request_id": "restore-1"},
    )
    stale_archive = http.post(
        f"/api/v1/threads/{thread_id}/archive",
        headers=headers(mutate=True),
        json={"client_request_id": "archive-1"},
    )
    assert archived.json()["status"] == "archived"
    assert restored.json()["status"] == "active"
    assert stale_archive.json()["status"] == "active"

    archived_again = http.post(
        f"/api/v1/threads/{thread_id}/archive",
        headers=headers(mutate=True),
        json={"client_request_id": "archive-2"},
    )
    assert archived_again.json()["status"] == "archived"
    active = http.get("/api/v1/threads", headers=headers()).json()["items"]
    archived_items = http.get(
        "/api/v1/threads?status=archived", headers=headers()
    ).json()["items"]
    assert all(item["thread_id"] != thread_id for item in active)
    assert [item["thread_id"] for item in archived_items] == [thread_id]

    replay = app.state.replay_service.mock_replay(thread_id)
    assert replay.projection.thread.status.value == "archived"
    assert replay.projection.thread.title == "第二版标题"
    assert (
        replay.projection.thread.updated_at
        == app.state.runtime.get_thread(thread_id).updated_at
    )


def test_thread_mutations_require_csrf_and_title_validation(tmp_path) -> None:
    http, _app = client(tmp_path)
    thread_id = create_task(http, 2)
    assert http.put(
        f"/api/v1/threads/{thread_id}",
        headers=headers(),
        json={"title": "blocked", "client_request_id": "rename"},
    ).status_code == 403
    invalid = http.put(
        f"/api/v1/threads/{thread_id}",
        headers=headers(mutate=True),
        json={"title": "   ", "client_request_id": "rename-invalid"},
    )
    assert invalid.status_code == 422


def test_thread_pin_and_active_turn_status_are_backend_authoritative(tmp_path) -> None:
    http, _app = client(tmp_path)
    thread_id = create_task(http, 9)

    catalog_item = next(
        item
        for item in http.get("/api/v1/threads", headers=headers()).json()["items"]
        if item["thread_id"] == thread_id
    )
    assert catalog_item["active_turn_status"] in {
        "accepted", "queued", "preparing", "model_requested", "streaming",
        "tool_pending", "waiting_human", "tool_running", "retry_wait", "finalizing",
    }

    pinned = http.put(
        f"/api/v1/threads/{thread_id}/pin",
        headers=headers(mutate=True),
        json={"pinned": True, "client_request_id": "pin-thread-9"},
    )
    assert pinned.status_code == 200
    assert pinned.json()["pinned"] is True
    assert pinned.json()["metadata"]["pinned"] is True

    unpinned = http.put(
        f"/api/v1/threads/{thread_id}/pin",
        headers=headers(mutate=True),
        json={"pinned": False, "client_request_id": "unpin-thread-9"},
    )
    replayed_old_pin = http.put(
        f"/api/v1/threads/{thread_id}/pin",
        headers=headers(mutate=True),
        json={"pinned": True, "client_request_id": "pin-thread-9"},
    )
    assert unpinned.json()["pinned"] is False
    assert replayed_old_pin.json()["pinned"] is False
