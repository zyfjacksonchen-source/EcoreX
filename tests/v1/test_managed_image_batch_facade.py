from __future__ import annotations

import asyncio
from dataclasses import replace
from types import MethodType, SimpleNamespace

import pytest

from ecorex.capabilities import (
    SandboxLevel,
    SchemaInstanceError,
    ToolExecutionScope,
    ToolInvocationContext,
    builtin_capability_registry,
)
from ecorex.capabilities.schema import validate_schema_instance
from ecorex.gateway import GatewayEvent
from ecorex.integration.image_tools import (
    ImageGenerationToolHandler,
    ImageToolError,
    RuntimeImageToolBackend,
)
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest, ItemKind
from ecorex.runtime import (
    AgentTurnWorker,
    RuntimeSettings,
    ToolExecutionRepository,
    WorkerOutcome,
    create_app,
)


def _context(parent: str = "turn-1:batch-call") -> ToolInvocationContext:
    return ToolInvocationContext(
        invocation_id="invoke_parent",
        capability_snapshot_id="capabilities_1",
        policy_snapshot_id="policy_1",
        tool_id="imagegen",
        idempotency_key=parent,
        approved=True,
        effective_sandbox=SandboxLevel.READ_ONLY,
        execution_scope=ToolExecutionScope("job-1", "thread-1", "turn-1"),
        tool_call_id="batch-call",
    )


def _backend(max_parallel: int = 2) -> RuntimeImageToolBackend:
    backend = object.__new__(RuntimeImageToolBackend)
    backend.batch_max_parallel = max_parallel
    backend._image_slots = asyncio.Semaphore(max_parallel)
    backend._emit_batch_failure = lambda *_args: None
    backend._emit_batch_settled = lambda *_args: None
    return backend


def test_image_batch_is_bounded_ordered_idempotent_and_reports_partial_failure() -> None:
    async def scenario() -> None:
        backend = _backend()
        active = 0
        peak = 0
        child_keys: dict[str, list[str]] = {}

        async def fake_single(self, arguments, context, *, image_batch=None):
            nonlocal active, peak
            instruction = arguments["instruction"]
            child_keys.setdefault(instruction, []).append(context.idempotency_key)
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep({"first": 0.03, "fails": 0, "last": 0.01}[instruction])
                if instruction == "fails":
                    raise ImageToolError("managed_image_unavailable", retryable=True)
                return {
                    "artifact_id": "artifact-" + instruction,
                    "preview_url": "/preview/" + instruction,
                }
            finally:
                active -= 1

        backend._generate_single = MethodType(fake_single, backend)
        tasks = [
            {"instruction": "first", "quality": "high"},
            {"instruction": "fails"},
            {"instruction": "last", "size": "square"},
        ]
        first = await backend.generate_image({"tasks": tasks}, _context())
        second = await backend.generate_image(
            {
                "tasks": [
                    {"quality": "high", "instruction": "first"},
                    {"instruction": "fails"},
                    {"size": "square", "instruction": "last"},
                ]
            },
            _context(),
        )

        assert peak == 2
        assert first["result_type"] == "image_gallery"
        assert first["status"] == "partial_failed"
        assert first["completed_count"] == 2
        assert first["failed_count"] == 1
        assert first["batch_id"].startswith("imgbatch_")
        assert first["parent_execution_id"] == "batch-call"
        assert [item["index"] for item in first["items"]] == [0, 1, 2]
        assert [item["status"] for item in first["items"]] == [
            "completed",
            "failed",
            "completed",
        ]
        assert first["items"][1]["error"] == {
            "code": "managed_image_unavailable",
            "retryable": True,
        }
        assert [item["task_id"] for item in first["items"]] == [
            item["task_id"] for item in second["items"]
        ]
        assert all(values[0] == values[1] for values in child_keys.values())
        assert all(item["batch_id"] == first["batch_id"] for item in first["items"])

    asyncio.run(scenario())


def test_image_batch_parent_cancellation_fails_closed() -> None:
    async def scenario() -> None:
        backend = _backend()
        entered = asyncio.Event()
        cancelled = 0

        async def blocked(self, arguments, context, *, image_batch=None):
            nonlocal cancelled
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise

        backend._generate_single = MethodType(blocked, backend)
        pending = asyncio.create_task(
            backend.generate_image(
                {"tasks": [{"instruction": "one"}, {"instruction": "two"}]},
                _context(),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert cancelled == 2

        handler = ImageGenerationToolHandler()
        handler_context = replace(_context(), backend=backend)
        with pytest.raises(ImageToolError, match="cannot mix"):
            await handler(
                {
                    "instruction": "single",
                    "tasks": [{"instruction": "one"}, {"instruction": "two"}],
                },
                handler_context,
            )
        with pytest.raises(ImageToolError, match="cannot mix"):
            await handler(
                {"instruction": "single", "tasks": None},
                handler_context,
            )
        with pytest.raises(ImageToolError, match="instruction or tasks"):
            await handler({}, handler_context)

    asyncio.run(scenario())


def test_imagegen_toolspec_accepts_bounded_tasks_and_preserves_single_input() -> None:
    schema = builtin_capability_registry().get("imagegen").input_schema
    validate_schema_instance(
        {"instruction": "one image", "size": "square"},
        schema,
        label="imagegen arguments",
    )
    validate_schema_instance(
        {"tasks": [{"instruction": "one"}, {"instruction": "two"}]},
        schema,
        label="imagegen arguments",
    )
    with pytest.raises(SchemaInstanceError, match="too few"):
        validate_schema_instance(
            {"tasks": [{"instruction": "one"}]},
            schema,
            label="imagegen arguments",
        )
    with pytest.raises(SchemaInstanceError, match="too many"):
        validate_schema_instance(
            {"tasks": [{"instruction": str(index)} for index in range(9)]},
            schema,
            label="imagegen arguments",
        )


def test_real_worker_routes_one_batch_call_through_image_pool_and_public_facts(
    tmp_path,
) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.scripts = [
                [
                    {
                        "seq": 1,
                        "event_type": "tool_call.requested",
                        "response_id": "response-image-batch",
                        "tool_call_id": "call_image_batch",
                        "tool_name": "imagegen",
                        "arguments": {
                            "tasks": [
                                {"instruction": "first image"},
                                {"instruction": "fail second image"},
                            ]
                        },
                    }
                ],
                [
                    {
                        "seq": 1,
                        "event_type": "output_text.delta",
                        "response_id": "response-image-batch-final",
                        "delta": "图片批次已处理。",
                    },
                    {
                        "seq": 2,
                        "event_type": "response.completed",
                        "response_id": "response-image-batch-final",
                    }
                ],
            ]

        async def stream(self, request):
            for event in self.scripts.pop(0):
                yield GatewayEvent.model_validate(event)

    async def scenario() -> None:
        app = create_app(
            settings=RuntimeSettings(
                database_path=tmp_path / "runtime.db",
                installed_capability_packs=frozenset({"image"}),
                capability_handlers={"imagegen": ImageGenerationToolHandler()},
            )
        )
        kernel = app.state.runtime
        composition = app.state.runtime_composition
        backend = app.state.image_tool_backend

        async def fake_single(self, arguments, context, *, image_batch=None):
            if arguments["instruction"].startswith("fail"):
                raise ImageToolError("managed_image_unavailable", retryable=True)
            artifact = SimpleNamespace(
                artifact_id="artifact-batch-first",
                revision_id="revision-batch-first",
                mime_type="image/png",
                size_bytes=5,
                sha256="1" * 64,
                to_dict=lambda: {
                    "artifact_id": "artifact-batch-first",
                    "revision_id": "revision-batch-first",
                    "mime_type": "image/png",
                    "size_bytes": 5,
                    "sha256": "1" * 64,
                },
            )
            return self._emit_artifact_item(
                artifact,
                context,
                "test-publication:" + context.idempotency_key,
                "imgjob_" + "1" * 32,
                image_batch=image_batch,
            )

        backend._generate_single = MethodType(fake_single, backend)
        thread = kernel.create_thread(CreateThreadRequest(title="batch worker"))
        prepared = composition.prepare_turn(
            CreateTurnRequest(
                input="生成两张不同的图片",
                agent_model_id="ecorex-chat",
                image_model_id="gpt-image-2",
                explicit_tool_ids=["imagegen"],
                client_message_id="image-batch-message",
            )
        )
        created = kernel.create_turn(
            thread.thread_id,
            prepared.request,
            snapshot_context=prepared.snapshot_context,
        )
        worker = AgentTurnWorker(
            kernel,
            gateway=Gateway(),
            capabilities=composition.capability_service,
            image_execution_concurrency=2,
            image_execution_queue_capacity=2,
        )

        first = await worker.run_once("worker-image-batch")
        assert first.outcome is WorkerOutcome.RETRY_SCHEDULED
        execution_id = worker._execution_id(
            created.turn.turn_id, "call_image_batch"
        )
        executions = ToolExecutionRepository(kernel.database)
        for _ in range(100):
            if executions.get(execution_id).status == "completed":
                break
            await asyncio.sleep(0.01)
        execution = executions.get(execution_id)
        assert execution.status == "completed"
        assert execution.result["status"] == "partial_failed"
        batch_id = execution.result["batch_id"]

        events = kernel.events.page(thread.thread_id, limit=200).events
        failed = [
            event for event in events
            if event.event_type == "artifact.image.batch_task_failed"
        ]
        settled = [
            event for event in events
            if event.event_type == "artifact.image.batch_settled"
        ]
        assert failed[0].payload["image_batch"] == {
            "schema_version": 1,
            "batch_id": batch_id,
            "parent_execution_id": execution_id,
            "index": 1,
            "count": 2,
            "task_id": execution.result["items"][1]["task_id"],
        }
        assert settled[0].payload["status"] == "partial_failed"
        artifact_items = [
            item for item in kernel.projection(thread.thread_id).items
            if item.kind is ItemKind.ARTIFACT
        ]
        assert artifact_items[0].content["image_batch"]["batch_id"] == batch_id
        assert artifact_items[0].content["image_batch"]["index"] == 0

        await asyncio.sleep(1.01)
        resumed = await worker.run_once("worker-image-batch-resumed")
        assert resumed.outcome is WorkerOutcome.COMPLETED
        await worker.close()

    asyncio.run(scenario())
