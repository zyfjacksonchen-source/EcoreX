from __future__ import annotations

import builtins
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ecorex.protocol import (
    CreateThreadRequest,
    CreateTurnRequest,
    ForkThreadRequest,
    ItemKind,
    ItemStatus,
    PublicToolActivity,
    ReplaceTurnRequest,
    SteerTurnRequest,
    TurnStatus,
)
from ecorex.runtime import RuntimeKernel, create_app
from ecorex.runtime.api import RuntimeSettings
from ecorex.runtime.errors import ConflictError, InvalidTransitionError


RUNTIME_TOKEN = "r" * 32
CSRF_TOKEN = "c" * 32


def test_kernel_turn_projection_state_constraints_and_idempotency(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread(CreateThreadRequest(title="方案"))
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(
            input="制定方案", agent_model_id="ecorex-chat", client_message_id="message-one"
        ),
    )
    duplicate = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(
            input="制定方案", agent_model_id="ecorex-chat", client_message_id="message-one"
        ),
    )
    assert duplicate.turn.turn_id == created.turn.turn_id
    assert duplicate.job.job_id == created.job.job_id
    assert created.turn.status == TurnStatus.QUEUED
    assert [event.event_type for event in kernel.events.page(thread.thread_id).events] == [
        "thread.created",
        "turn.accepted",
        "item.created",
        "turn.queued",
        "job.queued",
    ]

    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    with pytest.raises(InvalidTransitionError):
        kernel.transition_turn(created.turn.turn_id, TurnStatus.COMPLETED)
    item = kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.TOOL_CALL,
        content=PublicToolActivity(
            tool_call_id="kernel-state-tool-call",
            tool_id="read",
            tool_name="read",
            display_label="读取工作资料",
            phase="requested",
            status="created",
            risk="low",
            argument_summary="正在读取工作资料",
            argument_sha256="0" * 64,
        ).model_dump(mode="json"),
    )
    assert (
        kernel.transition_item(item.item_id, ItemStatus.IN_PROGRESS).status
        == ItemStatus.IN_PROGRESS
    )
    assert kernel.transition_item(item.item_id, ItemStatus.COMPLETED).status == ItemStatus.COMPLETED
    with pytest.raises(InvalidTransitionError):
        kernel.transition_item(item.item_id, ItemStatus.IN_PROGRESS)

    projection = kernel.projection(thread.thread_id)
    assert projection.watermark == kernel.events.watermark(thread.thread_id)
    assert len(projection.turns) == 1
    assert len(projection.items) == 2


def test_task_list_is_durable_idempotent_and_cannot_fake_completion(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="完成多步任务", client_message_id="task-list-1"),
    )
    leased = kernel.jobs.lease_next("worker-1", lease_seconds=120)
    assert leased is not None and leased.lease_token
    kernel.jobs.start(leased.job_id, "worker-1", leased.lease_token)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    pending = [
        {"id": "collect", "title": "收集资料", "status": "completed"},
        {"id": "deliver", "title": "交付结果", "status": "in_progress"},
    ]
    first = kernel.update_task_list(
        turn_id=created.turn.turn_id,
        items=pending,
        idempotency_key="task-list-update-1",
        job_id=leased.job_id,
        lease_token=leased.lease_token,
    )
    repeated = kernel.update_task_list(
        turn_id=created.turn.turn_id,
        items=pending,
        idempotency_key="task-list-update-1",
        job_id=leased.job_id,
        lease_token=leased.lease_token,
    )
    assert repeated.item_id == first.item_id
    assert first.kind is ItemKind.TASK_LIST
    assert first.status is ItemStatus.IN_PROGRESS
    assert [event.event_type for event in kernel.events.page(thread.thread_id).events].count(
        "task_list.updated"
    ) == 1

    with pytest.raises(ConflictError, match="invalid"):
        kernel.update_task_list(
            turn_id=created.turn.turn_id,
            items=[
                {"id": "one", "title": "第一项", "status": "in_progress"},
                {"id": "two", "title": "第二项", "status": "in_progress"},
            ],
            idempotency_key="task-list-update-invalid",
            job_id=leased.job_id,
            lease_token=leased.lease_token,
        )


def test_projection_orders_opaque_items_by_durable_event_sequence(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread(CreateThreadRequest(title="稳定顺序"))
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="第一条", client_message_id="ordered-message"),
    )
    tied_time = datetime.now(timezone.utc)
    with kernel.database.transaction() as connection:
        kernel._create_item_in_transaction(
            connection,
            thread_id=thread.thread_id,
            turn_id=created.turn.turn_id,
            kind=ItemKind.MESSAGE,
            status=ItemStatus.COMPLETED,
            content={"role": "assistant", "text": "先创建"},
            item_id="itm_z_created_first",
            now=tied_time,
        )
        kernel._create_item_in_transaction(
            connection,
            thread_id=thread.thread_id,
            turn_id=created.turn.turn_id,
            kind=ItemKind.MESSAGE,
            status=ItemStatus.COMPLETED,
            content={"role": "assistant", "text": "后创建"},
            item_id="itm_a_created_second",
            now=tied_time,
        )

    ordered_ids = [item.item_id for item in kernel.projection(thread.thread_id).items]
    assert ordered_ids[-2:] == ["itm_z_created_first", "itm_a_created_second"]


def test_steer_replace_interrupt_and_fork_are_transactional(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    current = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="first", client_message_id="m1"),
    )
    steer = SteerTurnRequest(input="add context", client_message_id="m2")
    first_steer = kernel.steer_turn(current.turn.turn_id, steer)
    repeated_steer = kernel.steer_turn(current.turn.turn_id, steer)
    assert repeated_steer.watermark == first_steer.watermark

    replacement = kernel.replace_turn(
        current.turn.turn_id,
        ReplaceTurnRequest(input="replacement", client_message_id="m3"),
    )
    assert replacement.superseded_turn.status == TurnStatus.SUPERSEDED
    assert replacement.replacement_turn.status == TurnStatus.QUEUED
    assert kernel.jobs.get(current.job.job_id).status.value == "cancelled"
    projection = kernel.projection(thread.thread_id)
    assert any(item.kind == ItemKind.CHECKPOINT for item in projection.items)

    interrupted = kernel.interrupt_turn(
        replacement.replacement_turn.turn_id, reason="user stopped"
    )
    assert interrupted.turn.status == TurnStatus.INTERRUPTED
    with pytest.raises(ConflictError):
        kernel.steer_turn(
            replacement.replacement_turn.turn_id,
            SteerTurnRequest(input="new", client_message_id="m4"),
        )

    fork = kernel.fork_thread(
        thread.thread_id,
        request=ForkThreadRequest(from_turn_id=current.turn.turn_id),
    )
    assert fork.forked_from_thread_id == thread.thread_id
    assert fork.forked_from_turn_id == current.turn.turn_id
    assert fork.forked_from_seq > 0


def test_model_selection_is_frozen_per_turn_and_survives_restart(tmp_path):
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    thread = kernel.create_thread()
    first = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(
            input="first",
            agent_model_id="ecorex-chat",
            image_model_id="gpt-image-2",
            client_message_id="model-freeze-1",
        ),
    ).turn

    with pytest.raises(ConflictError, match="model selection"):
        kernel.steer_turn(
            first.turn_id,
            SteerTurnRequest(
                input="do not mutate the active Turn",
                agent_model_id="ecorex-chat-next",
                image_model_id="gpt-image-3",
                client_message_id="model-freeze-steer",
            ),
        )

    second = kernel.queue_turn(
        thread.thread_id,
        CreateTurnRequest(
            input="second",
            agent_model_id="ecorex-chat-next",
            image_model_id="gpt-image-3",
            client_message_id="model-freeze-2",
        ),
    ).turn
    assert kernel.get_turn(first.turn_id).agent_model_id == "ecorex-chat"
    assert kernel.get_turn(first.turn_id).image_model_id == "gpt-image-2"
    assert second.agent_model_id == "ecorex-chat-next"
    assert second.image_model_id == "gpt-image-3"

    restarted = RuntimeKernel(path)
    assert restarted.get_turn(first.turn_id).agent_model_id == "ecorex-chat"
    assert restarted.get_turn(first.turn_id).image_model_id == "gpt-image-2"
    assert restarted.get_turn(second.turn_id).agent_model_id == "ecorex-chat-next"
    assert restarted.get_turn(second.turn_id).image_model_id == "gpt-image-3"


def test_api_bootstrap_polling_sse_and_mutations(tmp_path):
    settings = RuntimeSettings(
        database_path=tmp_path / "runtime.db",
        runtime_bearer_token=RUNTIME_TOKEN,
        csrf_token=CSRF_TOKEN,
        webui_origins=("http://testserver",),
    )
    client = TestClient(create_app(settings=settings))
    auth = {"Authorization": f"Bearer {RUNTIME_TOKEN}"}
    mutation = {
        **auth,
        "Origin": "http://testserver",
        "X-EcoreX-CSRF": CSRF_TOKEN,
    }
    bootstrap = client.get("/api/v1/bootstrap", headers=auth)
    assert bootstrap.status_code == 200
    body = bootstrap.json()
    repeated_bootstrap = client.get("/api/v1/bootstrap", headers=auth).json()
    assert repeated_bootstrap["policy_lease"]["lease_id"] == body["policy_lease"]["lease_id"]
    assert repeated_bootstrap["permissions"]["snapshot_id"] == body["permissions"]["snapshot_id"]
    restarted_client = TestClient(
        create_app(
            settings=RuntimeSettings(
                database_path=tmp_path / "runtime.db",
                runtime_bearer_token=RUNTIME_TOKEN,
                csrf_token=CSRF_TOKEN,
                webui_origins=("http://testserver",),
            )
        )
    )
    restarted_bootstrap = restarted_client.get("/api/v1/bootstrap", headers=auth).json()
    assert restarted_bootstrap["policy_lease"]["lease_id"] == body["policy_lease"]["lease_id"]
    assert restarted_bootstrap["permissions"]["snapshot_id"] == body["permissions"]["snapshot_id"]
    issued = datetime.fromisoformat(body["policy_lease"]["issued_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(body["policy_lease"]["expires_at"].replace("Z", "+00:00"))
    assert (expires - issued).total_seconds() == 72 * 3600
    assert body["models"]["chat"] and body["models"]["image"]
    assert "image2" in body["models"]["image"][0]["aliases"]

    version = client.get("/api/version")
    assert version.status_code == 200
    assert version.json()["version"] == "0.3.0"
    assert "core_version" not in version.json()
    update = client.get("/api/update-check", params={"platform": "win32"})
    assert update.status_code == 200
    assert update.json()["artifact"]["id"] == "webui-windows-x64"
    assert update.json()["update"]["webui"]["authority"] == "/api/v1/update"
    thread_response = client.post(
        "/api/v1/threads", json={"title": "Office"}, headers=mutation
    )
    assert thread_response.status_code == 201
    thread_id = thread_response.json()["thread_id"]
    turn_response = client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={"input": "hello", "client_message_id": "client-one"},
        headers=mutation,
    )
    assert turn_response.status_code == 202
    assert turn_response.json()["turn"]["agent_model_id"] == "ecorex-chat"
    assert turn_response.json()["turn"]["image_model_id"] == "gpt-image-2"
    turn_id = turn_response.json()["turn"]["turn_id"]

    first_page = client.get(
        f"/api/v1/threads/{thread_id}/events", params={"limit": 2}, headers=auth
    ).json()
    second_page = client.get(
        f"/api/v1/threads/{thread_id}/events",
        params={"after_seq": first_page["events"][-1]["seq"]},
        headers=auth,
    ).json()
    assert first_page["has_more"] is True
    assert {event["seq"] for event in first_page["events"]}.isdisjoint(
        {event["seq"] for event in second_page["events"]}
    )
    assert all(
        event["permission_snapshot_id"] == body["permissions"]["snapshot_id"]
        for event in first_page["events"] + second_page["events"]
    )
    sse = client.get(
        f"/api/v1/threads/{thread_id}/events",
        params={"follow": "false"},
        headers={**auth, "Accept": "text/event-stream", "Last-Event-ID": "4"},
    )
    assert sse.status_code == 200
    assert sse.headers["content-type"].startswith("text/event-stream")
    assert "id: 5" in sse.text and "event: watermark" in sse.text

    assert client.post(
        f"/api/v1/turns/{turn_id}/steer",
        json={"input": "more", "client_message_id": "client-two"},
        headers=mutation,
    ).status_code == 202
    assert client.get(
        f"/api/v1/threads/{thread_id}/projection", headers=auth
    ).status_code == 200
    assert client.post(
        f"/api/v1/turns/{turn_id}/interrupt",
        json={"reason": "stop"},
        headers=mutation,
    ).json()["turn"]["status"] == "interrupted"

    image_turn = client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={
            "input": "image",
            "agent_model_id": "ecorex-chat",
            "image_model_id": "image2",
            "client_message_id": "client-image",
        },
        headers=mutation,
    )
    assert image_turn.status_code == 202
    assert image_turn.json()["turn"]["agent_model_id"] == "ecorex-chat"
    assert image_turn.json()["turn"]["image_model_id"] == "gpt-image-2"
    assert (
        client.post(
            f"/api/v1/threads/{thread_id}/turns",
            json={"input": "bad", "agent_model_id": "external-model"},
            headers=mutation,
        ).status_code
        == 422
    )

    generic_model = client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={"input": "legacy", "model": "ecorex-chat"},
        headers=mutation,
    )
    assert generic_model.status_code == 422
    assert "model" in generic_model.text

    denied = client.post(
        "/api/v1/threads",
        json={},
        headers={**mutation, "Origin": "https://attacker.example"},
    )
    assert denied.status_code == 403
    assert bootstrap.headers["cache-control"] == "no-store"


def test_runtime_app_composes_without_legacy_common_package(tmp_path, monkeypatch):
    import_module = builtins.__import__

    def reject_legacy_common(name, *args, **kwargs):
        if name == "common" or name.startswith("common."):
            raise ModuleNotFoundError(name)
        return import_module(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_legacy_common)
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=RUNTIME_TOKEN,
            csrf_token=CSRF_TOKEN,
            webui_origins=("http://testserver",),
        )
    )
    assert app.title == "e-Mate Local Runtime"
