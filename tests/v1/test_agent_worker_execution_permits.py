from __future__ import annotations

import asyncio
import threading

import pytest

from agent.tools.base_tool import ToolResult
from agent.tools.read.read import Read
from ecorex.gateway import GatewayEvent
from ecorex.protocol import ItemKind, ItemStatus, TurnStatus
from ecorex.runtime import (
    AgentTurnWorker,
    RuntimeSettings,
    WorkerOutcome,
    create_app,
)
from ecorex.runtime.errors import LeaseError
from tests.v1.test_agent_turn_worker import _runtime


async def _wait_for_projection(predicate, *, attempts: int = 200):
    for _attempt in range(attempts):
        value = await asyncio.to_thread(predicate)
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("durable Worker fact was not observed")


class _PausedTextGateway:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        yield GatewayEvent(
            seq=1,
            event_type="output_text.delta",
            response_id="permit-text-response",
            delta="第一段",
        )
        await self.release.wait()
        yield GatewayEvent(
            seq=2,
            event_type="output_text.delta",
            response_id="permit-text-response",
            delta="不应提交",
        )
        yield GatewayEvent(
            seq=3,
            event_type="response.completed",
            response_id="permit-text-response",
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


def test_gate_close_after_first_token_does_not_interrupt_started_cow_turn(
    tmp_path,
) -> None:
    async def scenario() -> None:
        app, kernel, composition, thread, created = _runtime(
            tmp_path,
            input_text="生成简短回复",
        )
        gateway = _PausedTextGateway()
        worker = AgentTurnWorker(
            kernel,
            gateway=gateway,
            capabilities=composition.capability_service,
        )
        running = asyncio.create_task(worker.run_once("permit-text-worker"))

        def first_token():
            return next(
                (
                    item
                    for item in kernel.projection(thread.thread_id).items
                    if item.kind is ItemKind.MESSAGE
                    and item.content.get("role") == "assistant"
                    and item.content.get("text") == "第一段"
                ),
                None,
            )

        assistant = await _wait_for_projection(first_token)
        await asyncio.wait_for(
            asyncio.to_thread(
                app.state.runtime_execution_gate.mark_critical,
                error_code="test_close_after_first_token",
            ),
            timeout=2,
        )
        gateway.release.set()
        result = await asyncio.wait_for(running, timeout=3)

        assert result.outcome is WorkerOutcome.COMPLETED
        assert result.reason is None
        persisted = kernel.projection(thread.thread_id)
        durable = next(item for item in persisted.items if item.item_id == assistant.item_id)
        assert durable.content["text"] == "第一段不应提交"
        assert durable.status is ItemStatus.COMPLETED
        assert kernel.jobs.get(created.job.job_id).status.value == "completed"
        assert kernel.get_turn(created.turn.turn_id).status is TurnStatus.COMPLETED
        events = kernel.events.page(thread.thread_id, limit=1000).events
        assert any(event.event_type == "model.response_completed" for event in events)
        assert "不应提交" in "".join(
            str(event.payload.get("delta") or "") for event in events
        )

    asyncio.run(scenario())


def test_gate_close_during_cow_tool_does_not_interrupt_started_turn(
    tmp_path, monkeypatch,
) -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        calls: list[dict] = []
        legacy_calls: list[dict] = []

        def read(_tool, arguments):
            calls.append(dict(arguments))
            started.set()
            assert release.wait(timeout=3)
            return ToolResult.success({"title": "报告"})

        monkeypatch.setattr(Read, "execute", read)

        def legacy_read(arguments):
            legacy_calls.append(dict(arguments))
            raise AssertionError("public Cow turns must not use legacy capability handlers")

        class Gateway:
            def __init__(self) -> None:
                self.requests = []

            async def stream(self, request):
                self.requests.append(request)
                response_id = f"permit-tool-response-{len(self.requests)}"
                if len(self.requests) == 1:
                    yield GatewayEvent(
                        seq=1,
                        event_type="tool_call.requested",
                        response_id=response_id,
                        tool_call_id="permit-tool-call",
                        tool_name="read",
                        arguments={"path": "report.docx"},
                    )
                else:
                    yield GatewayEvent(
                        seq=1,
                        event_type="output_text.delta",
                        response_id=response_id,
                        delta="读取完成",
                    )
                yield GatewayEvent(
                    seq=2,
                    event_type="response.completed",
                    response_id=response_id,
                    usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )

        gateway = Gateway()
        app, kernel, composition, thread, created = _runtime(
            tmp_path,
            input_text="读取报告",
            capability_handlers={"read": legacy_read},
        )
        worker = AgentTurnWorker(
            kernel,
            gateway=gateway,
            capabilities=composition.capability_service,
        )
        running = asyncio.create_task(worker.run_once("permit-tool-worker"))
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), timeout=4)

        progressed = asyncio.Event()

        async def ticker() -> None:
            await asyncio.sleep(0)
            progressed.set()

        await ticker()
        assert progressed.is_set()
        await asyncio.wait_for(
            asyncio.to_thread(
                app.state.runtime_execution_gate.mark_critical,
                error_code="test_close_during_tool",
            ),
            timeout=2,
        )
        release.set()
        result = await asyncio.wait_for(running, timeout=3)

        assert result.outcome is WorkerOutcome.COMPLETED
        assert result.reason is None
        assert calls == [{"path": "report.docx"}]
        assert legacy_calls == []
        assert len(gateway.requests) == 2
        projection = kernel.projection(thread.thread_id)
        tool_item = next(item for item in projection.items if item.kind is ItemKind.TOOL_CALL)
        assert tool_item.status is ItemStatus.COMPLETED
        assert any(
            item.kind is ItemKind.MESSAGE and item.content.get("text") == "读取完成"
            for item in projection.items
        )
        events = kernel.events.page(thread.thread_id, limit=1000).events
        assert sum(event.event_type == "model.response_completed" for event in events) == 2
        assert kernel.jobs.get(created.job.job_id).status.value == "completed"

    asyncio.run(scenario())


def test_kernel_worker_precommit_close_rolls_back_item_and_event(tmp_path, monkeypatch) -> None:
    app, kernel, _composition, thread, created = _runtime(
        tmp_path,
        input_text="预提交回滚",
    )
    leased = kernel.jobs.lease_next("permit-precommit-worker", lease_seconds=30)
    assert leased is not None and leased.lease_token is not None
    kernel.jobs.start(leased.job_id, "permit-precommit-worker", leased.lease_token)
    kernel.transition_turn(
        created.turn.turn_id,
        TurnStatus.PREPARING,
        job_id=leased.job_id,
        lease_token=leased.lease_token,
    )
    assistant = kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.MESSAGE,
        status=ItemStatus.IN_PROGRESS,
        content={"role": "assistant", "text": ""},
        job_id=leased.job_id,
        lease_token=leased.lease_token,
    )
    before = kernel.events.watermark(thread.thread_id)
    gate = app.state.runtime_execution_gate
    original_assert = gate.assert_permit
    injected = False

    def close_before_commit(permit) -> None:
        nonlocal injected
        if not injected:
            injected = True
            gate.request_critical(error_code="test_worker_precommit_close")
        original_assert(permit)

    monkeypatch.setattr(gate, "assert_permit", close_before_commit)
    with pytest.raises(LeaseError, match="execution epoch"):
        kernel.append_message_delta(
            assistant.item_id,
            "不得落库",
            idempotency_key="permit-precommit-delta",
            job_id=leased.job_id,
            lease_token=leased.lease_token,
        )

    persisted = next(
        item
        for item in kernel.projection(thread.thread_id).items
        if item.item_id == assistant.item_id
    )
    assert persisted.content["text"] == ""
    assert kernel.events.watermark(thread.thread_id) == before
    assert kernel.jobs.get(leased.job_id).status.value == "running"


def test_restart_cannot_adopt_old_lease_or_append_with_no_process_permit(tmp_path) -> None:
    app, kernel, _composition, thread, created = _runtime(
        tmp_path,
        input_text="重启租约",
    )
    leased = kernel.jobs.lease_next("old-worker", lease_seconds=30)
    assert leased is not None and leased.lease_token is not None
    kernel.jobs.start(leased.job_id, "old-worker", leased.lease_token)
    assistant = kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.MESSAGE,
        status=ItemStatus.IN_PROGRESS,
        content={"role": "assistant", "text": ""},
        job_id=leased.job_id,
        lease_token=leased.lease_token,
    )
    del app, kernel

    restarted = create_app(
        settings=RuntimeSettings(database_path=tmp_path / "runtime.db")
    ).state.runtime
    with pytest.raises(LeaseError, match="no current execution permit"):
        restarted.append_message_delta(
            assistant.item_id,
            "旧进程迟到输出",
            idempotency_key="old-process-delta",
            job_id=leased.job_id,
            lease_token=leased.lease_token,
        )
    persisted = next(
        item
        for item in restarted.projection(thread.thread_id).items
        if item.item_id == assistant.item_id
    )
    assert persisted.content["text"] == ""
    assert not any(
        event.event_type == "item.delta"
        for event in restarted.events.page(thread.thread_id, limit=1000).events
    )
