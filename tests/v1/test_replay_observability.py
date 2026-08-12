from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import threading

from fastapi.testclient import TestClient
import pytest

from ecorex.capabilities import CapabilityRegistry
from ecorex.capabilities.builtin import builtin_tool_specs
from ecorex.observability import (
    AuditOutbox,
    AuditPayloadCipher,
    AuditRedactor,
    AuditRetentionPolicy,
)
from ecorex.protocol import (
    EventEnvelope,
    ItemKind,
    ItemStatus,
    LiveReplayRequest,
    SteerTurnRequest,
    TurnStatus,
)
from ecorex.runtime import RuntimeSettings, create_app
from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.public_tools import PublicToolActivityProjector


RUNTIME_TOKEN = "r" * 43
CSRF_TOKEN = "c" * 43
AUTH = {"Authorization": f"Bearer {RUNTIME_TOKEN}"}
MUTATION = {
    **AUTH,
    "Origin": "http://testserver",
    "X-EcoreX-CSRF": CSRF_TOKEN,
}


def _settings(tmp_path, **updates) -> RuntimeSettings:
    values = {
        "database_path": tmp_path / "runtime.db",
        "runtime_bearer_token": RUNTIME_TOKEN,
        "csrf_token": CSRF_TOKEN,
        "webui_origins": ("http://testserver",),
    }
    values.update(updates)
    return RuntimeSettings(**values)


def _thread_and_turn(client: TestClient, *, input_text: str = "prepare report"):
    thread_id = client.post(
        "/api/v1/threads", json={"title": "Replay"}, headers=MUTATION
    ).json()["thread_id"]
    response = client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={"input": input_text, "client_message_id": "source-message"},
        headers=MUTATION,
    ).json()
    return thread_id, response["turn"]["turn_id"], response["job"]["job_id"]


def _complete_turn(app, turn_id: str) -> None:
    kernel = app.state.runtime
    kernel.transition_turn(turn_id, TurnStatus.PREPARING)
    kernel.transition_turn(turn_id, TurnStatus.MODEL_REQUESTED)
    kernel.transition_turn(turn_id, TurnStatus.STREAMING)
    assistant = kernel.create_item(
        turn_id=turn_id,
        kind=ItemKind.MESSAGE,
        status=ItemStatus.IN_PROGRESS,
        content={"role": "assistant", "text": ""},
    )
    kernel.append_message_delta(
        assistant.item_id, "first ", idempotency_key=f"{turn_id}:delta:1"
    )
    kernel.append_message_delta(
        assistant.item_id, "answer", idempotency_key=f"{turn_id}:delta:2"
    )
    kernel.transition_item(assistant.item_id, ItemStatus.COMPLETED)
    kernel.transition_turn(turn_id, TurnStatus.FINALIZING)
    kernel.transition_turn(turn_id, TurnStatus.COMPLETED)


def _turn_with_user_revisions_and_authority_refresh(app, client: TestClient):
    thread_id = client.post(
        "/api/v1/threads", json={"title": "Revision Replay"}, headers=MUTATION
    ).json()["thread_id"]
    created = client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={
            "input": "生成首版插图",
            "explicit_tool_ids": ["imagegen"],
            "client_message_id": "revision-source-initial",
            "metadata": {
                "stage": "initial",
                "_replay": {"source_marker": "must-survive"},
            },
        },
        headers=MUTATION,
    ).json()
    turn_id = created["turn"]["turn_id"]
    kernel = app.state.runtime
    kernel.steer_turn(
        turn_id,
        SteerTurnRequest(
            input="把背景改为蓝色",
            explicit_tool_ids=["vision", "imagegen"],
            client_message_id="revision-source-steer-one",
            metadata={"stage": "steer-one"},
        ),
    )
    source_turn = kernel.get_turn(turn_id)
    with kernel.database.transaction() as connection:
        kernel.turn_inputs.append_authority_refresh_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            request=SteerTurnRequest(
                input="连接器权限已刷新",
                agent_model_id=source_turn.agent_model_id,
                image_model_id=source_turn.image_model_id,
                client_message_id="revision-source-authority-refresh",
                metadata={"authority_refresh": {"kind": "connector_login"}},
            ),
        )
    kernel.steer_turn(
        turn_id,
        SteerTurnRequest(
            input="最后补充标题文字",
            explicit_tool_ids=["shell"],
            client_message_id="revision-source-steer-two",
            metadata={"stage": "steer-two", "nested": {"kept": True}},
        ),
    )
    revisions = kernel.turn_inputs.list_for_turn(turn_id)
    _complete_turn(app, turn_id)
    return thread_id, turn_id, created["job"]["job_id"], revisions


def _table_counts(app) -> dict[str, int]:
    with app.state.runtime.database.reader() as connection:
        counts: dict[str, int] = {}
        for table in (
            "events",
            "jobs",
            "tool_executions",
            "observability_audit_outbox",
        ):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            counts[table] = (
                int(
                    connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ).fetchone()["count"]
                )
                if exists is not None
                else 0
            )
        return counts


def test_mock_replay_is_deterministic_matches_projection_and_has_no_side_effects(
    tmp_path,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    thread_id, turn_id, _job_id = _thread_and_turn(client)
    _complete_turn(app, turn_id)
    before = _table_counts(app)

    first = client.get(f"/api/v1/threads/{thread_id}/replay", headers=AUTH)
    second = client.get(f"/api/v1/threads/{thread_id}/replay", headers=AUTH)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    body = first.json()
    live = client.get(
        f"/api/v1/threads/{thread_id}/projection", headers=AUTH
    ).json()
    assert body["projection"] == live
    assert body["projection"]["turns"][0]["status"] == "completed"
    assert body["live_replay_turn_ids"] == [turn_id]
    assert body["projection"]["items"][-1]["content"]["text"] == "first answer"
    assert body["event_count"] == body["through_seq"]
    assert _table_counts(app) == before

    at_acceptance = client.get(
        f"/api/v1/threads/{thread_id}/replay",
        params={"through_seq": 2},
        headers=AUTH,
    ).json()
    assert at_acceptance["projection"]["turns"][0]["status"] == "accepted"
    assert at_acceptance["projection"]["items"] == []
    assert at_acceptance["live_replay_turn_ids"] == []


def test_mock_replay_follows_fork_lineage_from_self_describing_events(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    thread_id, turn_id, _job_id = _thread_and_turn(client)
    _complete_turn(app, turn_id)
    fork = client.post(
        f"/api/v1/threads/{thread_id}/fork",
        json={
            "from_turn_id": turn_id,
            "title": "Forked Replay",
            "metadata": {"purpose": "branch"},
            "client_request_id": "fork-replay-test",
        },
        headers=MUTATION,
    ).json()
    replay = client.get(
        f"/api/v1/threads/{fork['thread_id']}/replay", headers=AUTH
    ).json()
    assert replay["projection"]["thread"]["title"] == "Forked Replay"
    assert replay["projection"]["thread"]["metadata"]["purpose"] == "branch"
    assert replay["projection"]["turns"][0]["inherited"] is True
    assert replay["projection"]["items"][0]["inherited"] is True
    assert replay["event_count"] > replay["through_seq"]
    assert replay["live_replay_turn_ids"] == []


def test_replay_rejects_watermark_tamper_fail_closed(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app, raise_server_exceptions=False)
    thread_id, _turn_id, _job_id = _thread_and_turn(client)
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            "UPDATE thread_heads SET last_seq = last_seq + 1 WHERE thread_id = ?",
            (thread_id,),
        )
    response = client.get(f"/api/v1/threads/{thread_id}/replay", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["code"] == "replay_integrity_error"


def test_live_replay_requires_confirmation_and_replans_current_permissions(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    thread_id, source_turn_id, source_job_id = _thread_and_turn(
        client, input_text="run shell to prepare the report"
    )
    _complete_turn(app, source_turn_id)
    initial = client.get("/api/v1/bootstrap", headers=AUTH).json()["permissions"]
    enabled = client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "full_access",
            "expected_revision": initial["revision"],
            "client_request_id": "enable-before-live-replay",
        },
        headers=MUTATION,
    ).json()["permissions"]

    rejected = client.post(
        f"/api/v1/threads/{thread_id}/replay/live",
        json={
            "source_turn_id": source_turn_id,
            "confirmed": False,
            "client_request_id": "live-replay-one",
        },
        headers=MUTATION,
    )
    assert rejected.status_code == 422

    before_executions = _table_counts(app)["tool_executions"]
    request = {
        "source_turn_id": source_turn_id,
        "confirmed": True,
        "client_request_id": "live-replay-one",
    }
    created = client.post(
        f"/api/v1/threads/{thread_id}/replay/live",
        json=request,
        headers=MUTATION,
    )
    assert created.status_code == 202
    body = created.json()
    replay_turn = body["replay"]["turn"]
    assert replay_turn["turn_id"] != source_turn_id
    assert body["replay"]["job"]["job_id"] != source_job_id
    assert body["permission_snapshot_id"] == enabled["snapshot_id"]
    assert replay_turn["metadata"]["_replay"]["reuse_external_side_effects"] is False
    assert _table_counts(app)["tool_executions"] == before_executions

    events = app.state.runtime.events.page(thread_id, limit=1000).events
    source_accepted = next(
        event
        for event in events
        if event.turn_id == source_turn_id and event.event_type == "turn.accepted"
    )
    replay_accepted = next(
        event
        for event in events
        if event.turn_id == replay_turn["turn_id"] and event.event_type == "turn.accepted"
    )
    assert replay_accepted.causation_id == source_accepted.event_id
    assert replay_accepted.correlation_id == "live-replay-one"
    assert replay_accepted.permission_snapshot_id == enabled["snapshot_id"]
    replay_turn_events = [
        event for event in events if event.turn_id == replay_turn["turn_id"]
    ]
    assert replay_accepted.trace_id
    assert {event.trace_id for event in replay_turn_events} == {
        replay_accepted.trace_id
    }
    assert {event.correlation_id for event in replay_turn_events} == {
        "live-replay-one"
    }
    plan = app.state.runtime_composition.capability_service.get_plan(
        replay_accepted.capability_snapshot_id
    )
    assert plan.decision("shell").requires_approval is False

    duplicate = client.post(
        f"/api/v1/threads/{thread_id}/replay/live",
        json=request,
        headers=MUTATION,
    ).json()
    assert duplicate["replay"]["turn"]["turn_id"] == replay_turn["turn_id"]

    revoked = client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "default",
            "expected_revision": enabled["revision"],
            "client_request_id": "revoke-before-second-replay",
        },
        headers=MUTATION,
    ).json()["permissions"]
    second = client.post(
        f"/api/v1/threads/{thread_id}/replay/live",
        json={
            "source_turn_id": source_turn_id,
            "confirmed": True,
            "client_request_id": "live-replay-two",
        },
        headers=MUTATION,
    ).json()
    assert second["permission_snapshot_id"] == revoked["snapshot_id"]
    second_events = app.state.runtime.events.page(thread_id, limit=1000).events
    second_accepted = next(
        event
        for event in second_events
        if event.turn_id == second["replay"]["turn"]["turn_id"]
        and event.event_type == "turn.accepted"
    )
    second_plan = app.state.runtime_composition.capability_service.get_plan(
        second_accepted.capability_snapshot_id
    )
    assert second_plan.decision("shell").requires_approval is True


def test_live_replay_acceptance_linearizes_with_permission_update(
    tmp_path,
    monkeypatch,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    thread_id, source_turn_id, _source_job_id = _thread_and_turn(client)
    _complete_turn(app, source_turn_id)
    initial = client.get("/api/v1/bootstrap", headers=AUTH).json()["permissions"]
    composition = app.state.runtime_composition
    original_prepare = composition.prepare_turn
    prepared = threading.Event()
    release = threading.Event()

    def pause_prepared_replay(request):
        result = original_prepare(request)
        if not prepared.is_set():
            prepared.set()
            assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(composition, "prepare_turn", pause_prepared_replay)
    replay_path = f"/api/v1/threads/{thread_id}/replay/live"

    with ThreadPoolExecutor(max_workers=2) as executor:
        replay_future = executor.submit(
            client.post,
            replay_path,
            json={
                "source_turn_id": source_turn_id,
                "confirmed": True,
                "client_request_id": "linearized-live-replay-old",
            },
            headers=MUTATION,
        )
        assert prepared.wait(timeout=5)
        update_future = executor.submit(
            client.put,
            "/api/v1/settings/permissions",
            json={
                "profile": "full_access",
                "expected_revision": initial["revision"],
                "client_request_id": "linearized-live-replay-permission",
            },
            headers=MUTATION,
        )
        threading.Event().wait(0.1)
        assert not update_future.done()
        release.set()
        replay_response = replay_future.result(timeout=5)
        update_response = update_future.result(timeout=5)

    assert replay_response.status_code == 202
    assert update_response.status_code == 200
    assert (
        replay_response.json()["permission_snapshot_id"]
        == initial["snapshot_id"]
    )
    changed = update_response.json()["permissions"]

    second = client.post(
        replay_path,
        json={
            "source_turn_id": source_turn_id,
            "confirmed": True,
            "client_request_id": "linearized-live-replay-new",
        },
        headers=MUTATION,
    )
    assert second.status_code == 202
    assert second.json()["permission_snapshot_id"] == changed["snapshot_id"]


def test_live_replay_restores_all_user_revisions_after_restart_exactly_once(
    tmp_path,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings=settings)
    client = TestClient(app)
    thread_id, source_turn_id, source_job_id, source_revisions = (
        _turn_with_user_revisions_and_authority_refresh(app, client)
    )
    source_accepted = next(
        event
        for event in app.state.runtime.events.page(thread_id, limit=1000).events
        if event.turn_id == source_turn_id and event.event_type == "turn.accepted"
    )
    permissions = client.get("/api/v1/bootstrap", headers=AUTH).json()["permissions"]
    enabled = client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "full_access",
            "expected_revision": permissions["revision"],
            "client_request_id": "revision-replay-current-authority",
        },
        headers=MUTATION,
    ).json()["permissions"]

    restarted = create_app(settings=settings)
    restarted_client = TestClient(restarted)
    request = {
        "source_turn_id": source_turn_id,
        "confirmed": True,
        "client_request_id": "revision-live-replay",
    }
    response = restarted_client.post(
        f"/api/v1/threads/{thread_id}/replay/live",
        json=request,
        headers=MUTATION,
    )
    assert response.status_code == 202
    body = response.json()
    replay_turn_id = body["replay"]["turn"]["turn_id"]
    replay_job_id = body["replay"]["job"]["job_id"]
    assert replay_job_id != source_job_id
    assert body["permission_snapshot_id"] == enabled["snapshot_id"]

    kernel = restarted.state.runtime
    replay_revisions = kernel.turn_inputs.list_for_turn(replay_turn_id)
    user_source_revisions = tuple(
        revision
        for revision in source_revisions
        if revision.source in {"initial", "steer"}
    )
    assert [revision.ordinal for revision in source_revisions] == [0, 1, 2, 3]
    assert [revision.source for revision in source_revisions] == [
        "initial",
        "steer",
        "authority_refresh",
        "steer",
    ]
    assert [revision.ordinal for revision in replay_revisions] == [0, 1, 2]
    assert [revision.source for revision in replay_revisions] == [
        "initial",
        "steer",
        "steer",
    ]
    assert [revision.input for revision in replay_revisions] == [
        revision.input for revision in user_source_revisions
    ]
    assert [revision.explicit_tool_ids for revision in replay_revisions] == [
        ["imagegen"],
        ["vision", "imagegen"],
        ["shell"],
    ]
    assert [revision.metadata["stage"] for revision in replay_revisions] == [
        "initial",
        "steer-one",
        "steer-two",
    ]
    assert replay_revisions[0].metadata["_replay"]["source_replay"] == {
        "source_marker": "must-survive"
    }
    assert replay_revisions[2].metadata["nested"] == {"kept": True}
    assert [
        revision.metadata["_replay"]["source_revision_id"]
        for revision in replay_revisions
    ] == [revision.revision_id for revision in user_source_revisions]
    assert [
        revision.metadata["_replay"]["source_revision_ordinal"]
        for revision in replay_revisions
    ] == [0, 1, 3]
    assert len({revision.client_message_id for revision in replay_revisions}) == 3
    assert all(
        revision.agent_model_id == body["replay"]["turn"]["agent_model_id"]
        and revision.image_model_id == body["replay"]["turn"]["image_model_id"]
        for revision in replay_revisions
    )

    replay_accepted = next(
        event
        for event in kernel.events.page(thread_id, limit=1000).events
        if event.turn_id == replay_turn_id and event.event_type == "turn.accepted"
    )
    assert replay_accepted.permission_snapshot_id == enabled["snapshot_id"]
    assert replay_accepted.permission_snapshot_id != source_accepted.permission_snapshot_id
    with kernel.database.reader() as connection:
        user_items = connection.execute(
            "SELECT content_json FROM items WHERE turn_id = ? "
            "AND kind = 'message' AND json_extract(content_json, '$.role') = 'user' "
            "ORDER BY created_at, item_id",
            (replay_turn_id,),
        ).fetchall()
        before = {
            "turns": int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM turns WHERE turn_id = ?",
                    (replay_turn_id,),
                ).fetchone()["count"]
            ),
            "jobs": int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM jobs WHERE turn_id = ?",
                    (replay_turn_id,),
                ).fetchone()["count"]
            ),
            "items": len(user_items),
            "revisions": len(replay_revisions),
        }
    assert [json.loads(row["content_json"])["text"] for row in user_items] == [
        revision.input for revision in user_source_revisions
    ]

    duplicate = restarted_client.post(
        f"/api/v1/threads/{thread_id}/replay/live",
        json=request,
        headers=MUTATION,
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["replay"]["turn"]["turn_id"] == replay_turn_id
    with kernel.database.reader() as connection:
        after = {
            "turns": int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM turns WHERE turn_id = ?",
                    (replay_turn_id,),
                ).fetchone()["count"]
            ),
            "jobs": int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM jobs WHERE turn_id = ?",
                    (replay_turn_id,),
                ).fetchone()["count"]
            ),
            "items": int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM items WHERE turn_id = ? "
                    "AND kind = 'message' "
                    "AND json_extract(content_json, '$.role') = 'user'",
                    (replay_turn_id,),
                ).fetchone()["count"]
            ),
            "revisions": len(kernel.turn_inputs.list_for_turn(replay_turn_id)),
        }
    assert before == after == {"turns": 1, "jobs": 1, "items": 3, "revisions": 3}


@pytest.mark.parametrize("drift", ["revision", "accepted"])
def test_live_replay_rejects_source_input_drift_fail_closed(tmp_path, drift) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app, raise_server_exceptions=False)
    thread_id, turn_id, _job_id = _thread_and_turn(client)
    _complete_turn(app, turn_id)
    with app.state.runtime.database.transaction() as connection:
        if drift == "revision":
            connection.execute("DROP TRIGGER turn_input_revisions_no_update")
            connection.execute(
                "UPDATE turn_input_revisions SET input_text = ? "
                "WHERE turn_id = ? AND ordinal = 0",
                ("tampered revision", turn_id),
            )
        else:
            connection.execute("DROP TRIGGER events_are_append_only_update")
            accepted = connection.execute(
                "SELECT payload_json FROM events WHERE turn_id = ? "
                "AND event_type = 'turn.accepted'",
                (turn_id,),
            ).fetchone()
            payload = json.loads(accepted["payload_json"])
            payload["input"] = "tampered acceptance"
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE turn_id = ? "
                "AND event_type = 'turn.accepted'",
                (json.dumps(payload), turn_id),
            )
    response = client.post(
        f"/api/v1/threads/{thread_id}/replay/live",
        json={
            "source_turn_id": turn_id,
            "confirmed": True,
            "client_request_id": f"drift-{drift}",
        },
        headers=MUTATION,
    )
    assert response.status_code == 503
    assert response.json()["code"] == "replay_integrity_error"


def test_live_replay_job_is_invisible_to_concurrent_lease_until_commit(
    tmp_path, monkeypatch
) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    thread_id, source_turn_id, _source_job_id, _source_revisions = (
        _turn_with_user_revisions_and_authority_refresh(app, client)
    )
    kernel = app.state.runtime
    service = app.state.replay_service
    request = LiveReplayRequest(
        source_turn_id=source_turn_id,
        confirmed=True,
        client_request_id="lease-visibility-replay",
    )
    replay_client_message_id = service._live_replay_client_message_id(
        request.client_request_id
    )
    transaction_paused = threading.Event()
    release_transaction = threading.Event()
    lease_started = threading.Event()
    lease_finished = threading.Event()
    original_steer = kernel._steer_turn_in_transaction

    def paused_steer(connection, *, turn_id, request, now=None):
        if not transaction_paused.is_set():
            transaction_paused.set()
            if not release_transaction.wait(timeout=5):
                raise TimeoutError("test did not release replay transaction")
        return original_steer(
            connection,
            turn_id=turn_id,
            request=request,
            now=now,
        )

    monkeypatch.setattr(kernel, "_steer_turn_in_transaction", paused_steer)

    def lease_next():
        lease_started.set()
        try:
            return kernel.jobs.lease_next(
                "concurrent-replay-worker", kinds=["agent_turn"]
            )
        finally:
            lease_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        replay_future = executor.submit(service.live_replay, thread_id, request)
        assert transaction_paused.wait(timeout=5)
        lease_future = executor.submit(lease_next)
        assert lease_started.wait(timeout=5)
        assert not lease_finished.wait(timeout=0.1)
        with kernel.database.reader() as connection:
            assert connection.execute(
                "SELECT 1 FROM turns WHERE thread_id = ? AND client_message_id = ?",
                (thread_id, replay_client_message_id),
            ).fetchone() is None
            assert connection.execute(
                "SELECT 1 FROM jobs WHERE turn_id IN ("
                "SELECT turn_id FROM turns WHERE thread_id = ? "
                "AND client_message_id = ?)",
                (thread_id, replay_client_message_id),
            ).fetchone() is None
        release_transaction.set()
        replay = replay_future.result(timeout=5)
        leased = lease_future.result(timeout=5)

    assert leased is not None
    assert replay.replay.job is not None
    assert leased.job_id == replay.replay.job.job_id
    assert leased.turn_id == replay.replay.turn.turn_id


def test_trace_projection_is_otlp_compatible_and_excludes_sensitive_bodies(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    thread_id, turn_id, job_id = _thread_and_turn(client)
    store = app.state.runtime.events
    projector = PublicToolActivityProjector()
    shell = CapabilityRegistry(builtin_tool_specs()).get("shell")
    arguments = {"password": "never-export", "path": "C:\\Users\\secret"}
    requested_activity = projector.requested(
        shell,
        tool_call_id="tool-call-1",
        arguments=arguments,
    )
    completed_activity = projector.completed(
        shell,
        tool_call_id="tool-call-1",
        arguments=arguments,
        result={"token": "never-export"},
    )
    store.append(
        thread_id=thread_id,
        turn_id=turn_id,
        event_type="model.requested",
        payload={
            "request_id": "request-1",
            "agent_model_id": "ecorex-chat",
            "round": 0,
        },
        idempotency_key="trace:model:start",
    )
    store.append(
        thread_id=thread_id,
        turn_id=turn_id,
        item_id="tool-item-1",
        tool_call_id="tool-call-1",
        event_type="tool.call_requested",
        payload={"activity": requested_activity.model_dump(mode="json")},
        idempotency_key="trace:tool:start",
    )
    recovery = store.append(
        thread_id=thread_id,
        turn_id=turn_id,
        tool_call_id="tool-recovery-call",
        event_type="tool.recovery_planned",
        payload={
            "schema_version": 1,
            "source": "preflight",
            "code": "tool_not_eligible",
            "requested_tool": "legacy-browser-search",
            "reason_codes": ["unknown_tool"],
            "action": "discover_or_switch",
            "retry_allowed": False,
            "automatic_attempt": 1,
            "automatic_attempt_limit": 3,
            "candidate_tool_ids": ["fetch", "cdp"],
            "capability_snapshot_id": "cap_trace",
            "execution_batch_id": "batch_trace",
        },
        idempotency_key="trace:tool:recovery",
    )
    store.append(
        thread_id=thread_id,
        turn_id=turn_id,
        tool_call_id="tool-recovery-fallback",
        event_type="tool.recovery_resolved",
        payload={
            "schema_version": 1,
            "recovery_event_id": recovery.event_id,
            "resolved_by_tool_id": "fetch",
        },
        idempotency_key="trace:tool:recovery:resolved",
    )
    store.append(
        thread_id=thread_id,
        turn_id=turn_id,
        job_id=job_id,
        item_id="human-1",
        event_type="interaction.requested",
        payload={"kind": "permission_approval", "prompt": "allow?", "options": []},
        idempotency_key="trace:human:start",
    )
    store.append(
        thread_id=thread_id,
        turn_id=turn_id,
        item_id="tool-item-1",
        tool_call_id="tool-call-1",
        event_type="tool.result",
        payload={"activity": completed_activity.model_dump(mode="json")},
        idempotency_key="trace:tool:end",
    )
    store.append(
        thread_id=thread_id,
        turn_id=turn_id,
        job_id=job_id,
        item_id="human-1",
        event_type="interaction.resolved",
        payload={"response": {"choice": "allow"}},
        idempotency_key="trace:human:end",
    )
    store.append(
        thread_id=thread_id,
        turn_id=turn_id,
        event_type="artifact.created",
        payload={"artifact_id": "art_1", "revision_id": "rev_1"},
        idempotency_key="trace:artifact",
    )
    store.append(
        thread_id=thread_id,
        turn_id=turn_id,
        event_type="model.response_completed",
        payload={"response_id": "response-1", "round": 0, "usage": {"input_tokens": 9}},
        idempotency_key="trace:model:end",
    )

    response = client.get(f"/api/v1/threads/{thread_id}/trace", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    names = {span["name"] for span in body["spans"]}
    assert {
        "ecorex.thread",
        "ecorex.turn",
        "gen_ai.model_attempt",
        "ecorex.tool",
        "ecorex.tool_recovery",
        "ecorex.human_interaction",
        "ecorex.artifact",
    } <= names
    assert len(body["trace_id"]) == 32
    assert all(len(span["span_id"]) == 16 for span in body["spans"])
    assert body["otlp"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    recovery_span = next(
        span for span in body["spans"] if span["name"] == "ecorex.tool_recovery"
    )
    assert recovery_span["status"] == "OK"
    assert recovery_span["attributes"]["ecorex.recovery.code"] == "tool_not_eligible"
    assert recovery_span["attributes"]["ecorex.recovery.action"] == "discover_or_switch"
    assert recovery_span["attributes"]["ecorex.recovery.resolved_by_tool"] == "fetch"
    wire = json.dumps(body, ensure_ascii=False)
    assert "never-export" not in wire
    assert "C:\\\\Users\\\\secret" not in wire


def test_audit_redacts_secrets_paths_binary_from_the_sidecar(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    thread_id, turn_id, _job_id = _thread_and_turn(
        client,
        input_text=(
            "use api_key=super-secret and read C:\\Users\\alice\\private.txt"
        ),
    )
    arguments = {
        "password": "plain-secret",
        "apiKey": "camel-case-secret",
        "path": "C:\\Users\\alice\\private.txt",
        "image_data": "data:image/png;base64,AAAABBBB",
    }
    redacted_fixture = AuditRedactor().redact(arguments)
    assert redacted_fixture["password"] == "[REDACTED:SECRET]"
    assert redacted_fixture["apiKey"] == "[REDACTED:SECRET]"
    assert str(redacted_fixture["path"]).startswith("[REDACTED:PATH:")
    assert redacted_fixture["image_data"]["omitted"] == "binary"

    read_activity = PublicToolActivityProjector().requested(
        CapabilityRegistry(builtin_tool_specs()).get("read"),
        tool_call_id="audit-call",
        arguments=arguments,
    )
    app.state.runtime.events.append(
        thread_id=thread_id,
        turn_id=turn_id,
        item_id="audit-tool",
        tool_call_id="audit-call",
        event_type="tool.call_requested",
        payload={"activity": read_activity.model_dump(mode="json")},
        idempotency_key="audit:redaction",
    )
    records = client.get(
        "/api/v1/observability/audit",
        params={"thread_id": thread_id, "limit": 1000},
        headers=AUTH,
    )
    assert records.status_code == 200
    body = records.json()
    wire = json.dumps(body, ensure_ascii=False)
    assert body["count"] > 0
    assert "super-secret" not in wire
    assert "plain-secret" not in wire
    assert "camel-case-secret" not in wire
    assert "private.txt" not in wire
    assert "data:image/png" not in wire
    assert "[REDACTED:SECRET]" in wire
    assert "[REDACTED:PATH:" in wire
    assert all(record["binary_included"] is False for record in body["records"])
    with app.state.runtime.database.reader() as connection:
        stored_payloads = connection.execute(
            "SELECT payload_json, payload_format FROM observability_audit_outbox "
            "WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
    assert stored_payloads
    assert all(row["payload_format"] == "aesgcm-v1" for row in stored_payloads)
    encrypted_wire = "".join(str(row["payload_json"]) for row in stored_payloads)
    assert "super-secret" not in encrypted_wire
    assert "[REDACTED" not in encrypted_wire
    assert '"input"' not in encrypted_wire

    # Reading the sidecar backfills committed source Events before projection.
    source_event = next(
        event
        for event in app.state.runtime.events.page(thread_id, limit=1000).events
        if event.event_type == "tool.call_requested"
    )
    assert any(
        record["source_event_id"] == source_event.event_id
        for record in body["records"]
    )
    assert client.get("/api/v1/observability/audit").status_code == 401

    # Retention is final: the incremental Event Store cursor must not recreate
    # a deliberately expired raw audit record during restart recovery.
    tool_audit = next(
        record
        for record in body["records"]
        if record["event_type"] == "tool.call_requested"
    )
    old_published = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            "UPDATE observability_audit_outbox SET published_at = ? "
            "WHERE audit_id = ?",
            (old_published, tool_audit["audit_id"]),
        )
    assert app.state.audit_outbox.enforce_retention().raw_deleted >= 1
    assert app.state.audit_outbox.backfill_events() == 0
    with app.state.runtime.database.reader() as connection:
        resurrected = connection.execute(
            "SELECT 1 FROM observability_audit_outbox WHERE audit_id = ?",
            (tool_audit["audit_id"],),
        ).fetchone()
    assert resurrected is None


def test_audit_sidecar_failure_does_not_rollback_agent_events(
    tmp_path, monkeypatch,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    outbox = app.state.audit_outbox
    original = outbox.record_in_transaction

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit-sidecar-down")

    monkeypatch.setattr(outbox, "record_in_transaction", fail_audit)
    assert app.state.runtime.events.event_sink is None
    with TestClient(app) as client:
        thread_id, turn_id, _job_id = _thread_and_turn(client)

    events = app.state.runtime.events.page(thread_id, limit=1000).events
    assert any(event.turn_id == turn_id for event in events)

    monkeypatch.setattr(outbox, "record_in_transaction", original)
    assert outbox.backfill_events() > 0
    assert outbox.list(thread_id=thread_id)


def test_trace_and_audit_integrity_fail_closed_without_leaking_payloads(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app, raise_server_exceptions=False)
    thread_id, turn_id, _job_id = _thread_and_turn(client)
    app.state.runtime.events.append(
        thread_id=thread_id,
        turn_id=turn_id,
        event_type="model.requested",
        payload={"request_id": "bad-round", "round": "secret-invalid-round"},
        idempotency_key="trace:invalid-round",
    )
    trace = client.get(f"/api/v1/threads/{thread_id}/trace", headers=AUTH)
    assert trace.status_code == 503
    assert trace.json()["code"] == "replay_integrity_error"
    assert "secret-invalid-round" not in trace.text

    assert app.state.audit_outbox.backfill_events() > 0
    record = app.state.audit_outbox.list(thread_id=thread_id, limit=1)[0]
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            "UPDATE observability_audit_outbox SET payload_json = ? WHERE audit_id = ?",
            ('{"leaked":"never-return-this"}', record.audit_id),
        )
    audit = client.get(
        "/api/v1/observability/audit",
        params={"thread_id": thread_id},
        headers=AUTH,
    )
    assert audit.status_code == 503
    assert audit.json()["code"] == "audit_integrity_error"
    assert "never-return-this" not in audit.text


class _FlakyPublisher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def publish(self, record) -> None:
        self.calls.append(record.audit_id)
        if len(self.calls) == 1:
            raise ConnectionError("offline")


def test_audit_outbox_retries_disconnect_and_retains_pending_raw_records(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "audit.db")
    publisher = _FlakyPublisher()
    outbox = AuditOutbox(
        database,
        account_id="local-user",
        cipher=AuditPayloadCipher(b"a" * 32),
        publisher=publisher,
        retention=AuditRetentionPolicy(raw_days=30, aggregate_days=180),
    )
    old = datetime.now(UTC) - timedelta(days=200)
    event = EventEnvelope(
        event_id="evt_audit_retry",
        seq=1,
        thread_id="thr_audit",
        turn_id="trn_audit",
        event_type="artifact.created",
        created_at=old,
        payload={
            "artifact_id": "art_retry",
            "binary": "data:application/octet-stream;base64,AAAA",
        },
    )
    with database.transaction() as connection:
        outbox.record_in_transaction(connection, event)
    first = asyncio.run(outbox.drain(limit=1))
    assert first.retry_scheduled == 1
    record = outbox.list(limit=1)[0]
    assert record.delivery_status == "retry_wait"
    assert record.last_error_code == "ConnectionError"

    with database.transaction() as connection:
        connection.execute(
            "UPDATE observability_audit_outbox SET next_attempt_at = ? "
            "WHERE audit_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), record.audit_id),
        )
    second = asyncio.run(outbox.drain(limit=1))
    assert second.published == 1
    published = outbox.get(record.audit_id)
    assert published.delivery_status == "published"
    assert published.attempts == 2
    assert publisher.calls == [record.audit_id, record.audit_id]

    pending_event = EventEnvelope(
        event_id="evt_audit_pending",
        seq=2,
        thread_id="thr_audit",
        event_type="job.queued",
        created_at=old,
        payload={"kind": "agent_turn"},
    )
    with database.transaction() as connection:
        outbox.record_in_transaction(connection, pending_event)
        connection.execute(
            "UPDATE observability_audit_outbox SET published_at = ? "
            "WHERE audit_id = ?",
            (old.isoformat(), record.audit_id),
        )
    retention = outbox.enforce_retention(now=datetime.now(UTC))
    assert retention.raw_deleted == 1
    assert retention.aggregate_deleted >= 1
    assert outbox.count(pending_only=True) == 1
