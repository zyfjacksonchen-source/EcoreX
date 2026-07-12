from __future__ import annotations

import asyncio

import pytest

from ecorex.gateway import GatewayEvent
from ecorex.protocol import ItemKind, ItemStatus, TurnStatus
from ecorex.runtime import (
    AgentTurnWorker,
    RuntimeSettings,
    ToolExecutionRepository,
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
        )


def test_gate_close_after_first_token_discards_every_later_model_fact(tmp_path) -> None:
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

        assert result.outcome is WorkerOutcome.FAILED
        assert result.reason == "lease_lost"
        persisted = kernel.projection(thread.thread_id)
        durable = next(item for item in persisted.items if item.item_id == assistant.item_id)
        assert durable.content["text"] == "第一段"
        assert durable.status is ItemStatus.IN_PROGRESS
        assert kernel.jobs.get(created.job.job_id).status.value == "running"
        assert kernel.get_turn(created.turn.turn_id).status is TurnStatus.STREAMING
        events = kernel.events.page(thread.thread_id, limit=1000).events
        assert not any(event.event_type == "model.response_completed" for event in events)
        assert "不应提交" not in "".join(
            str(event.payload.get("delta") or "") for event in events
        )

    asyncio.run(scenario())


def test_gate_close_during_tool_keeps_started_record_and_never_continues_provider(
    tmp_path,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[dict] = []
        runtime_facts: dict[str, object] = {}

        async def read(arguments):
            calls.append(dict(arguments))
            started.set()
            await release.wait()

            def late_repository_write() -> None:
                runtime_facts["kernel"].events.append(
                    thread_id=runtime_facts["thread_id"],
                    turn_id=runtime_facts["turn_id"],
                    event_type="test.late_tool_commit",
                    payload={"must_rollback": True},
                    idempotency_key="test-late-tool-commit",
                )

            # ``asyncio.to_thread`` copies the capability Task context.  Its
            # repository commit must therefore see the same execution Permit.
            await asyncio.to_thread(late_repository_write)
            return {"title": "迟到结果"}

        class Gateway:
            def __init__(self) -> None:
                self.requests = []

            async def stream(self, request):
                self.requests.append(request)
                yield GatewayEvent(
                    seq=1,
                    event_type="tool_call.requested",
                    response_id="permit-tool-response",
                    tool_call_id="permit-tool-call",
                    tool_name="read",
                    arguments={"path": "report.docx"},
                )

        gateway = Gateway()
        app, kernel, composition, thread, created = _runtime(
            tmp_path,
            input_text="读取报告",
            capability_handlers={"read": read},
        )
        runtime_facts.update(
            {
                "kernel": kernel,
                "thread_id": thread.thread_id,
                "turn_id": created.turn.turn_id,
            }
        )
        worker = AgentTurnWorker(
            kernel,
            gateway=gateway,
            capabilities=composition.capability_service,
        )
        running = asyncio.create_task(worker.run_once("permit-tool-worker"))
        await asyncio.wait_for(started.wait(), timeout=3)

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

        assert result.outcome is WorkerOutcome.FAILED
        assert result.reason == "lease_lost"
        assert calls == [{"path": "report.docx"}]
        assert len(gateway.requests) == 1
        execution_id = worker._execution_id(created.turn.turn_id, "permit-tool-call")
        assert ToolExecutionRepository(kernel.database).get(execution_id).status == "started"
        projection = kernel.projection(thread.thread_id)
        tool_item = next(item for item in projection.items if item.kind is ItemKind.TOOL_CALL)
        assert tool_item.status is ItemStatus.IN_PROGRESS
        assert not any(item.kind is ItemKind.ARTIFACT for item in projection.items)
        events = kernel.events.page(thread.thread_id, limit=1000).events
        assert not any(event.event_type == "tool.result" for event in events)
        assert not any(event.event_type == "test.late_tool_commit" for event in events)
        assert not any(
            event.event_type == "model.continuation_requested" for event in events
        )
        assert kernel.jobs.get(created.job.job_id).status.value == "running"

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
