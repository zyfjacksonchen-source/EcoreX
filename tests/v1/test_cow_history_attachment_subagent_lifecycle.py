from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

from agent.memory.config import MemoryConfig
from agent.memory.conversation_store import ConversationStore
from ecorex.gateway import (
    GatewayEvent,
    GatewayFunctionCallOutputInput,
    GatewayImageInput,
    GatewayUserMessageInput,
)
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest
from ecorex.runtime import AgentTurnWorker, RuntimeSettings, WorkerOutcome, create_app


def _png(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="PNG")
    return output.getvalue()


def _image(identity: str) -> GatewayImageInput:
    payload = _png((int(identity[-1]) * 20, 40, 80))
    import base64
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    return GatewayImageInput(
        attachment_id=f"att_{identity}",
        revision_id=f"rev_{identity}",
        mime_type="image/png",
        data_base64=base64.b64encode(payload).decode("ascii"),
        sha256=digest,
        source_sha256=digest,
    )


def test_gateway_image_bindings_are_fifo_for_identical_text_and_do_not_land_on_history(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from ecorex.runtime.worker import _CowGatewayModel

    loop = asyncio.new_event_loop()
    try:
        model = _CowGatewayModel(
            SimpleNamespace(),
            loop,
            thread_id="thr",
            turn_id="turn",
            model_id="ecorex-chat",
        )
        first = _image("1")
        second = _image("2")
        model.bind_user_images("same prompt", [first])
        model.bind_user_images("same prompt", [second])

        initial = model._request(
            SimpleNamespace(
                system="system",
                tools=[],
                messages=[
                    {"role": "user", "content": [{"type": "text", "text": "same prompt"}]},
                    {"role": "assistant", "content": [{"type": "text", "text": "old"}]},
                    {"role": "user", "content": [{"type": "text", "text": "same prompt"}]},
                ],
            )
        )
        users = [
            item
            for item in initial.ordered_input_items()
            if isinstance(item, GatewayUserMessageInput)
        ]
        assert users[0].images == []
        assert [image.attachment_id for image in users[-1].images] == [first.attachment_id]

        model.previous_response_id = "response-1"
        steered = model._request(
            SimpleNamespace(
                system="system",
                tools=[],
                messages=[
                    {"role": "user", "content": [{"type": "text", "text": "same prompt"}]}
                ],
            )
        )
        users = [
            item
            for item in steered.ordered_input_items()
            if isinstance(item, GatewayUserMessageInput)
        ]
        assert [image.attachment_id for image in users[-1].images] == [second.attachment_id]
    finally:
        loop.close()


def test_gateway_does_not_apply_a_second_96_message_cutoff() -> None:
    from types import SimpleNamespace

    from ecorex.runtime.worker import _CowGatewayModel

    loop = asyncio.new_event_loop()
    try:
        model = _CowGatewayModel(
            SimpleNamespace(),
            loop,
            thread_id="thr",
            turn_id="turn",
            model_id="ecorex-chat",
        )
        request = model._request(
            SimpleNamespace(
                system="system",
                tools=[],
                messages=[
                    {
                        "role": "user" if index % 2 == 0 else "assistant",
                        "content": [{"type": "text", "text": f"message-{index}"}],
                    }
                    for index in range(110)
                ],
            )
        )

        assert len(request.ordered_input_items()) == 110
        assert request.ordered_input_items()[0].content == "message-0"
    finally:
        loop.close()


def test_cow_gateway_cancel_closes_inflight_provider_stream() -> None:
    from agent.protocol.cancel import AgentCancelledError
    from ecorex.runtime.worker import _CowGatewayModel

    async def scenario() -> None:
        started = asyncio.Event()
        closed = asyncio.Event()

        class Gateway:
            async def stream(self, request):
                started.set()
                try:
                    yield GatewayEvent(
                        seq=1,
                        event_type="output_text.delta",
                        response_id=request.request_id,
                        delta="partial",
                    )
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        cancel_event = threading.Event()
        model = _CowGatewayModel(
            Gateway(),
            asyncio.get_running_loop(),
            thread_id="thread-cancel",
            turn_id="turn-cancel",
            model_id="ecorex-chat",
            cancel_event=cancel_event,
        )
        stream = model.call_stream(
            SimpleNamespace(
                system="system",
                tools=[],
                messages=[
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "cancel me"}],
                    }
                ],
            )
        )

        first = await asyncio.to_thread(next, stream)
        assert first["choices"][0]["delta"]["content"] == "partial"
        await started.wait()
        cancel_event.set()
        with pytest.raises(AgentCancelledError):
            await asyncio.wait_for(asyncio.to_thread(next, stream), timeout=0.5)
        await asyncio.wait_for(closed.wait(), timeout=0.5)

    asyncio.run(scenario())


def test_cow_gateway_cancel_closes_inflight_vision_stream() -> None:
    from agent.protocol.cancel import AgentCancelledError
    from ecorex.runtime.worker import _CowGatewayModel

    async def scenario() -> None:
        started = asyncio.Event()
        closed = asyncio.Event()

        class Gateway:
            async def stream(self, request):
                started.set()
                try:
                    yield GatewayEvent(
                        seq=1,
                        event_type="output_text.delta",
                        response_id=request.request_id,
                        delta="partial",
                    )
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        cancel_event = threading.Event()
        model = _CowGatewayModel(
            Gateway(),
            asyncio.get_running_loop(),
            thread_id="thread-vision-cancel",
            turn_id="turn-vision-cancel",
            model_id="ecorex-chat",
            cancel_event=cancel_event,
        )
        image_url = (
            "data:image/png;base64,"
            + base64.b64encode(_png((20, 40, 60))).decode("ascii")
        )
        running = asyncio.create_task(
            asyncio.to_thread(
                model.call_vision,
                image_url=image_url,
                question="describe",
            )
        )
        await started.wait()
        cancel_event.set()
        with pytest.raises(AgentCancelledError):
            await asyncio.wait_for(running, timeout=0.5)
        await asyncio.wait_for(closed.wait(), timeout=0.5)

    asyncio.run(scenario())


def test_model_switch_keeps_failed_prompt_and_artifact_history(tmp_path: Path) -> None:
    from ecorex.runtime.worker import _CowGatewayModel

    store = ConversationStore(tmp_path / "switch-history.db")
    store.append_messages(
        "switch-session",
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": "create two deliverables"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "created"}],
                "extras": {
                    "artifacts": [
                        {
                            "title": "poster.png",
                            "type": "image/png",
                            "url": "/api/v1/artifacts/art_poster/preview",
                        },
                        {
                            "title": "report.docx",
                            "type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            "path": "/workspace/report.docx",
                        },
                    ]
                },
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "retry after provider_rejected"}
                ],
            },
        ],
        channel_type="web",
    )

    loop = asyncio.new_event_loop()
    try:
        request = _CowGatewayModel(
            SimpleNamespace(),
            loop,
            thread_id="switch-session",
            turn_id="switch-turn",
            model_id="ecorex-doubao-seed-2.0-pro",
        )._request(
            SimpleNamespace(
                system="system",
                tools=[],
                messages=store.load_messages("switch-session", max_turns=30),
            )
        )
    finally:
        loop.close()

    contents = [item.content for item in request.ordered_input_items()]
    assert request.model_id == "ecorex-doubao-seed-2.0-pro"
    assert contents[0] == "create two deliverables"
    assert "[历史图片产物: poster.png | /api/v1/artifacts/art_poster/preview]" in contents[1]
    assert "[历史文件产物: report.docx | /workspace/report.docx]" in contents[1]
    assert contents[2] == "retry after provider_rejected"


def test_duplicate_tool_call_id_reuses_result_without_repeating_side_effect() -> None:
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.tools.base_tool import ToolResult

    calls: list[dict] = []

    class Tool:
        name = "side_effect"
        params = {"type": "object"}

        def execute_tool(self, arguments):
            calls.append(dict(arguments))
            return ToolResult.success({"written": len(calls)})

    executor = AgentStreamExecutor(
        agent=SimpleNamespace(skill_manager=None),
        model=SimpleNamespace(),
        system_prompt="system",
        tools=[Tool()],
    )
    first = executor._execute_tool(
        {"id": "call-once", "name": "side_effect", "arguments": {"value": 1}}
    )
    replay = executor._execute_tool(
        {"id": "call-once", "name": "side_effect", "arguments": {"value": 1}}
    )
    conflict = executor._execute_tool(
        {"id": "call-once", "name": "side_effect", "arguments": {"value": 2}}
    )

    assert first == replay
    assert calls == [{"value": 1}]
    assert conflict["status"] == "error"
    assert "nothing was executed" in conflict["result"]


def _create_turn(app, thread_id: str, text: str, message_id: str):
    prepared = app.state.runtime_composition.prepare_turn(
        CreateTurnRequest(input=text, client_message_id=message_id)
    )
    return app.state.runtime.create_turn(
        thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )


def _text_turn(index: int) -> tuple[dict, dict]:
    return (
        {"role": "user", "content": [{"type": "text", "text": f"history-user-{index}"}]},
        {"role": "assistant", "content": [{"type": "text", "text": f"history-assistant-{index}"}]},
    )


def test_failed_followup_preserves_completed_tool_chain_for_cow_history(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.protocol.agent import Agent
    from agent.protocol.agent_stream import AgentStreamExecutor

    query = "write exactly once"
    tool_use_id = "tool-once"
    completed_chain = [
        {"role": "user", "content": [{"type": "text", "text": query}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": "write", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "written"}],
        },
    ]

    def fail_after_tool(self, user_message: str) -> str:
        assert user_message == query
        self.messages.extend(completed_chain)
        raise RuntimeError("provider failed after successful tool")

    monkeypatch.setattr(AgentStreamExecutor, "run_stream", fail_after_tool)
    agent = Agent(system_prompt="test", model=object(), enable_skills=False)

    with pytest.raises(RuntimeError, match="provider failed after successful tool"):
        agent.run_stream(query)

    assert agent.messages == completed_chain
    assert agent._last_run_new_messages == completed_chain
    worker = object.__new__(AgentTurnWorker)
    worker._turn_image_artifact_history = lambda _turn_id: []
    store = ConversationStore(tmp_path / "history.db")
    worker._persist_cow_history(
        store=store,
        session_id="failed-tool-session",
        turn_id="failed-tool-turn",
        agent=agent,
        channel_type="web",
        project_context=None,
    )
    assert store.load_messages("failed-tool-session", max_turns=30) == completed_chain


def test_public_worker_restores_durable_cow_compaction_state_without_rewriting_history(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.memory.summarizer import MemoryFlushManager

    workspace = tmp_path / "workspace"
    app = create_app(settings=RuntimeSettings(database_path=tmp_path / "runtime.db"))
    thread = app.state.runtime.create_thread(CreateThreadRequest(title="history"))
    session_id = thread.thread_id
    store = ConversationStore(MemoryConfig(workspace_root=str(workspace)).get_db_path())
    original = [message for index in range(31) for message in _text_turn(index)]
    store.append_messages(session_id, original, channel_type="web")
    monkeypatch.setattr(
        MemoryFlushManager,
        "_summarize_messages",
        lambda _self, _messages, _max_messages: "durable cow compaction summary",
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            response_id = f"history-response-{len(self.requests)}"
            yield GatewayEvent(
                seq=1,
                event_type="output_text.delta",
                response_id=response_id,
                delta="stored answer",
            )
            yield GatewayEvent(
                seq=2,
                event_type="response.completed",
                response_id=response_id,
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    first_gateway = Gateway()
    _create_turn(app, thread.thread_id, "first fresh turn", "history-turn-1")
    first = asyncio.run(
        AgentTurnWorker(
            app.state.runtime,
            gateway=first_gateway,
            workspace_root=workspace,
        ).run_once("history-worker-1")
    )
    assert first.outcome is WorkerOutcome.COMPLETED

    with sqlite3.connect(store._db_path) as connection:
        raw_count = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        context_start = connection.execute(
            "SELECT context_start_seq FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()[0]
    assert raw_count > len(original)
    assert context_start > 0

    second_gateway = Gateway()
    _create_turn(app, thread.thread_id, "second fresh turn", "history-turn-2")
    second = asyncio.run(
        AgentTurnWorker(
            app.state.runtime,
            gateway=second_gateway,
            workspace_root=workspace,
        ).run_once("history-worker-2")
    )
    assert second.outcome is WorkerOutcome.COMPLETED
    restored = "\n".join(
        item.content
        for item in second_gateway.requests[0].ordered_input_items()
        if isinstance(item, GatewayUserMessageInput)
    )
    assert "durable cow compaction summary" in restored
    assert "first fresh turn" in restored
    assert "second fresh turn" in restored


async def _wait_until(predicate, timeout: float = 3) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.02)


def test_parent_completion_waits_for_real_started_subagent(tmp_path: Path) -> None:
    child_started = threading.Event()
    release_child = threading.Event()

    class Gateway:
        async def stream(self, request):
            response_id = request.request_id
            if ":subagent-" in request.request_id:
                child_started.set()
                await asyncio.to_thread(release_child.wait, 3)
                yield GatewayEvent(
                    seq=1,
                    event_type="output_text.delta",
                    response_id=response_id,
                    delta="child finished",
                )
            elif not any(
                isinstance(item, GatewayFunctionCallOutputInput)
                for item in request.ordered_input_items()
            ):
                yield GatewayEvent(
                    seq=1,
                    event_type="tool_call.requested",
                    response_id=response_id,
                    tool_call_id="start-child",
                    tool_name="subagent",
                    arguments={"action": "start", "task": "finish after release"},
                )
            else:
                yield GatewayEvent(
                    seq=1,
                    event_type="output_text.delta",
                    response_id=response_id,
                    delta="parent response is ready",
                )
            yield GatewayEvent(
                seq=2,
                event_type="response.completed",
                response_id=response_id,
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    async def scenario():
        workspace = tmp_path / "workspace"
        app = create_app(settings=RuntimeSettings(database_path=tmp_path / "runtime.db"))
        thread = app.state.runtime.create_thread(CreateThreadRequest(title="subagent complete"))
        _create_turn(app, thread.thread_id, "start a child", "subagent-complete-turn")
        worker = AgentTurnWorker(
            app.state.runtime, gateway=Gateway(), workspace_root=workspace
        )
        running = asyncio.create_task(worker.run_once("subagent-complete-worker"))
        await _wait_until(child_started.is_set)
        await asyncio.sleep(0.1)
        assert not running.done()
        release_child.set()
        result = await asyncio.wait_for(running, 5)
        state = json.loads((workspace / ".ecorex" / "subagents.json").read_text())
        assert {task["status"] for task in state["tasks"].values()} == {"completed"}
        return result

    assert asyncio.run(scenario()).outcome is WorkerOutcome.COMPLETED


def test_parent_interrupt_cascades_to_real_started_subagent(tmp_path: Path) -> None:
    child_started = threading.Event()

    class Gateway:
        async def stream(self, request):
            response_id = request.request_id
            if request.previous_response_id is None and ":subagent-" not in request.request_id:
                yield GatewayEvent(
                    seq=1,
                    event_type="tool_call.requested",
                    response_id=response_id,
                    tool_call_id="start-child",
                    tool_name="subagent",
                    arguments={"action": "start", "task": "run until parent cancellation"},
                )
                yield GatewayEvent(
                    seq=2,
                    event_type="response.completed",
                    response_id=response_id,
                    usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )
                return
            if ":subagent-" in request.request_id:
                child_started.set()
            for seq in range(1, 100):
                await asyncio.sleep(0.02)
                yield GatewayEvent(
                    seq=seq,
                    event_type="output_text.delta",
                    response_id=response_id,
                    delta=".",
                )

    async def scenario():
        workspace = tmp_path / "workspace"
        app = create_app(settings=RuntimeSettings(database_path=tmp_path / "runtime.db"))
        thread = app.state.runtime.create_thread(CreateThreadRequest(title="subagent cancel"))
        created = _create_turn(app, thread.thread_id, "start a child", "subagent-cancel-turn")
        worker = AgentTurnWorker(
            app.state.runtime, gateway=Gateway(), workspace_root=workspace
        )
        running = asyncio.create_task(worker.run_once("subagent-cancel-worker"))
        await _wait_until(child_started.is_set)
        app.state.runtime.interrupt_turn(created.turn.turn_id, reason="test_parent_cancel")
        result = await asyncio.wait_for(running, 5)
        state = json.loads((workspace / ".ecorex" / "subagents.json").read_text())
        assert {task["status"] for task in state["tasks"].values()} == {"cancelled"}
        return result

    assert asyncio.run(scenario()).outcome is WorkerOutcome.FAILED
