from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ecorex.gateway import GatewayEvent
from ecorex.protocol import (
    CreateThreadRequest,
    CreateTurnRequest,
    ItemKind,
    ReasoningItemContent,
    ReasoningPresentation,
    TurnStatus,
)
from ecorex.runtime import AgentTurnWorker, RuntimeKernel, RuntimeSettings, create_app
from ecorex.runtime.errors import ConflictError
from ecorex.runtime.reasoning import archive_visible_reasoning_in_transaction
from ecorex.replay import ReplayService


class ScriptedGateway:
    def __init__(self, events: list[dict[str, object]]):
        self.events = events

    async def stream(self, _request):
        for event in self.events:
            yield GatewayEvent.model_validate(event)


def _turn(kernel: RuntimeKernel):
    thread = kernel.create_thread(CreateThreadRequest(title="reasoning"))
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(
            input="检查季度报告",
            agent_model_id="ecorex-chat",
            client_message_id="reasoning-message",
        ),
    )
    return thread, created


def test_gateway_accepts_only_provider_approved_reasoning_summary_atoms() -> None:
    event = GatewayEvent(
        seq=1,
        event_type="reasoning_summary.delta",
        response_id="response-1",
        reasoning_id="summary-1",
        delta="正在检查输入。",
    )
    assert event.reasoning_id == "summary-1"
    with pytest.raises(ValidationError):
        GatewayEvent(
            seq=1,
            event_type="reasoning_summary.delta",
            response_id="response-1",
            delta="正在检查输入。",
        )
    with pytest.raises(ValidationError):
        GatewayEvent(
            seq=1,
            event_type="reasoning_summary.delta",
            response_id="response-1",
            reasoning_id="summary-1",
            delta="   ",
        )


def test_next_non_empty_atom_atomically_replaces_the_visible_item(tmp_path) -> None:
    database_path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(database_path)
    thread, created = _turn(kernel)

    first = kernel.reasoning.apply_delta(
        turn_id=created.turn.turn_id,
        atom_id="atom-a",
        delta="先读取文件。",
        idempotency_key="reasoning-a-1",
    )
    appended = kernel.reasoning.apply_delta(
        turn_id=created.turn.turn_id,
        atom_id="atom-a",
        delta="再检查表格。",
        idempotency_key="reasoning-a-2",
    )
    assert appended.item_id == first.item_id
    assert ReasoningItemContent.model_validate(appended.content).revision == 2

    # Ordinary lifecycle facts must not own reasoning presentation.
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    kernel.events.append(
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        event_type="tool.discovery_completed",
        payload={"tools": ["read"]},
    )
    still_visible = next(
        item for item in kernel.projection(thread.thread_id).items
        if item.item_id == first.item_id
    )
    assert ReasoningItemContent.model_validate(
        still_visible.content
    ).presentation is ReasoningPresentation.VISIBLE

    second = kernel.reasoning.apply_delta(
        turn_id=created.turn.turn_id,
        atom_id="atom-b",
        delta="数据已读取，开始汇总。",
        idempotency_key="reasoning-b-1",
    )
    projection = kernel.projection(thread.thread_id)
    by_id = {item.item_id: item for item in projection.items}
    old_content = ReasoningItemContent.model_validate(by_id[first.item_id].content)
    new_content = ReasoningItemContent.model_validate(by_id[second.item_id].content)
    assert old_content.presentation is ReasoningPresentation.ARCHIVED
    assert old_content.archived_reason == "replaced_by_next_atom"
    assert new_content.presentation is ReasoningPresentation.VISIBLE
    assert new_content.text == "数据已读取，开始汇总。"

    replacement = next(
        event
        for event in kernel.events.page(thread.thread_id).events
        if event.event_type == "reasoning.replaced" and event.item_id == second.item_id
    )
    assert replacement.payload["previous_item_id"] == first.item_id
    assert replacement.payload["previous_presentation"] == "archived"

    restarted = RuntimeKernel(database_path)
    restarted_projection = restarted.projection(thread.thread_id)
    assert [
        item.item_id
        for item in restarted_projection.items
        if item.kind is ItemKind.REASONING
        and item.content["presentation"] == "visible"
    ] == [second.item_id]
    assert restarted_projection.watermark == projection.watermark
    replayed = ReplayService(restarted).mock_replay(thread.thread_id).projection
    replayed_reasoning = {
        item.item_id: item.content
        for item in replayed.items
        if item.kind is ItemKind.REASONING
    }
    persisted_reasoning = {
        item.item_id: item.content
        for item in restarted_projection.items
        if item.kind is ItemKind.REASONING
    }
    assert replayed_reasoning == persisted_reasoning


def test_terminal_fact_precedes_the_explicit_reasoning_collapse(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread, created = _turn(kernel)
    reasoning = kernel.reasoning.apply_delta(
        turn_id=created.turn.turn_id,
        atom_id="final-check",
        delta="正在核对最终结果。",
        idempotency_key="final-check-1",
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.MODEL_REQUESTED)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.STREAMING)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.FINALIZING)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.COMPLETED)

    events = kernel.events.page(thread.thread_id).events
    archived = next(event for event in events if event.event_type == "reasoning.archived")
    terminal = next(
        event
        for event in events
        if event.event_type == "turn.status_changed"
        and event.payload.get("to") == "completed"
    )
    assert terminal.seq < archived.seq
    assert archived.causation_id == terminal.event_id
    assert archived.item_id == reasoning.item_id
    assert archived.payload == {
        "revision": 2,
        "presentation": "collapsed",
        "reason": "completed",
        "terminal_status": "completed",
    }
    persisted = next(
        item for item in kernel.projection(thread.thread_id).items
        if item.item_id == reasoning.item_id
    )
    assert persisted.status.value == "completed"
    assert ReasoningItemContent.model_validate(
        persisted.content
    ).presentation is ReasoningPresentation.COLLAPSED


def test_terminal_archive_rejects_a_missing_or_mismatched_turn_fact(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread, created = _turn(kernel)
    kernel.reasoning.apply_delta(
        turn_id=created.turn.turn_id,
        atom_id="guarded-summary",
        delta="等待终态。",
        idempotency_key="guarded-summary-1",
    )

    with kernel.database.transaction() as connection:
        with pytest.raises(ConflictError, match="preceding terminal Turn fact"):
            archive_visible_reasoning_in_transaction(
                connection,
                kernel.events,
                thread_id=thread.thread_id,
                turn_id=created.turn.turn_id,
                terminal_event_id="evt_missing_terminal_fact",
                terminal_status=TurnStatus.COMPLETED,
                reason="completed",
                now=datetime.now(timezone.utc),
            )

    visible = next(
        item
        for item in kernel.projection(thread.thread_id).items
        if item.kind is ItemKind.REASONING
    )
    assert visible.content["presentation"] == "visible"


def test_sse_exposes_terminal_before_the_reasoning_archive(tmp_path) -> None:
    runtime_token = "r" * 32
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=runtime_token,
            csrf_token="c" * 32,
            webui_origins=("http://testserver",),
        )
    )
    kernel = app.state.runtime
    thread, created = _turn(kernel)
    kernel.reasoning.apply_delta(
        turn_id=created.turn.turn_id,
        atom_id="sse-summary",
        delta="终态到达前保持显示。",
        idempotency_key="sse-summary-1",
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.MODEL_REQUESTED)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.STREAMING)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.FINALIZING)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.COMPLETED)

    events = kernel.events.page(thread.thread_id).events
    terminal = next(
        event
        for event in events
        if event.event_type == "turn.status_changed"
        and event.payload.get("to") == "completed"
    )
    archived = next(event for event in events if event.event_type == "reasoning.archived")
    response = TestClient(app).get(
        f"/api/v1/threads/{thread.thread_id}/events",
        params={"after_seq": terminal.seq - 1, "follow": "false"},
        headers={
            "Authorization": f"Bearer {runtime_token}",
            "Accept": "text/event-stream",
        },
    )

    assert response.status_code == 200
    terminal_marker = f"id: {terminal.seq}\nevent: turn.status_changed"
    archive_marker = f"id: {archived.seq}\nevent: reasoning.archived"
    assert response.text.index(terminal_marker) < response.text.index(archive_marker)


def test_worker_persists_reasoning_before_answer_and_terminal_archive(tmp_path) -> None:
    app = create_app(settings=RuntimeSettings(database_path=tmp_path / "runtime.db"))
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="worker reasoning"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="总结报告",
            agent_model_id="ecorex-chat",
            client_message_id="worker-reasoning",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    gateway = ScriptedGateway(
        [
            {
                "seq": 1,
                "event_type": "reasoning_summary.delta",
                "response_id": "response-1",
                "reasoning_id": "summary-1",
                "delta": "先提取关键指标。",
            },
            {
                "seq": 2,
                "event_type": "output_text.delta",
                "response_id": "response-1",
                "delta": "已完成总结。",
            },
            {
                "seq": 3,
                "event_type": "response.completed",
                "response_id": "response-1",
            },
        ]
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
    )
    result = asyncio.run(worker.run_once("reasoning-worker"))
    assert result.outcome.value == "completed"
    projection = kernel.projection(thread.thread_id)
    reasoning = next(item for item in projection.items if item.kind is ItemKind.REASONING)
    assert ReasoningItemContent.model_validate(reasoning.content).text == "先提取关键指标。"
    assert reasoning.content["presentation"] == "collapsed"
    assert kernel.get_turn(created.turn.turn_id).status is TurnStatus.COMPLETED
