from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace

import pytest

from agent.tools.imagegen.imagegen import ImageGenTool
from ecorex.capabilities import (
    SandboxLevel,
    SchemaInstanceError,
    ToolExecutionScope,
    ToolInvocationContext,
    builtin_capability_registry,
)
from ecorex.capabilities.schema import validate_schema_instance
from ecorex.gateway import GatewayEvent, GatewayFunctionCallOutputInput
from ecorex.integration.image_tools import (
    ImageGenerationToolHandler,
    ImageToolError,
    RuntimeImageToolBackend,
)
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest, ItemKind
from ecorex.runtime import AgentTurnWorker, RuntimeSettings, WorkerOutcome, create_app


def _context() -> ToolInvocationContext:
    return ToolInvocationContext(
        invocation_id="invoke-imagegen",
        capability_snapshot_id="capabilities-1",
        policy_snapshot_id="policy-1",
        tool_id="imagegen",
        idempotency_key="turn-1:call-1",
        approved=True,
        effective_sandbox=SandboxLevel.READ_ONLY,
        execution_scope=ToolExecutionScope("job-1", "thread-1", "turn-1"),
        tool_call_id="call-1",
    )


def test_imagegen_public_contract_is_one_codex_style_output() -> None:
    expected = {"prompt", "image_url", "size", "quality"}
    schemas = [
        ImageGenTool.params,
        builtin_capability_registry().get("imagegen").input_schema,
    ]

    for schema in schemas:
        assert set(schema["properties"]) == expected
        assert list(schema["required"]) == ["prompt"]
        assert schema["additionalProperties"] is False
        validate_schema_instance(
            {
                "prompt": "combine the references",
                "image_url": ["first.png", "second.png"],
                "size": "1536x1024",
                "quality": "high",
            },
            schema,
            label="imagegen arguments",
        )
        with pytest.raises(SchemaInstanceError):
            validate_schema_instance(
                {"tasks": [{"prompt": "one"}, {"prompt": "two"}]},
                schema,
                label="imagegen arguments",
            )
        for hidden in ("provider", "model", "output_dir", "timeout", "concurrency"):
            with pytest.raises(SchemaInstanceError):
                validate_schema_instance(
                    {"prompt": "one", hidden: "forbidden"},
                    schema,
                    label="imagegen arguments",
                )
        for unsupported in (
            {"size": "800x1200"},
            {"size": "3:4"},
            {"aspect_ratio": "9:16"},
        ):
            with pytest.raises(SchemaInstanceError):
                validate_schema_instance(
                    {"prompt": "edit the reference", **unsupported},
                    schema,
                    label="imagegen arguments",
                )

    output = builtin_capability_registry().get("imagegen").output_schema
    assert output["properties"]["images"]["maxItems"] == 1
    assert "separate imagegen call" in ImageGenTool.description


def test_reference_edit_binds_once_normalizes_size_and_fences_terminal_failure() -> None:
    async def scenario() -> None:
        backend = object.__new__(RuntimeImageToolBackend)
        backend.kernel = SimpleNamespace(
            get_turn=lambda turn_id: SimpleNamespace(
                metadata={
                    "input_attachments": [
                        {
                            "attachment_id": "att_reference_poster",
                            "media_kind": "image",
                            "mime_type": "image/png",
                        }
                    ]
                }
            )
        )
        attempts: list[dict] = []

        async def rejected(self, arguments, context):
            attempts.append(dict(arguments))
            raise ImageToolError("provider_rejected")

        backend._generate_single = MethodType(rejected, backend)
        prompt = "Keep every word and rearrange this poster"
        for arguments in (
            {"prompt": prompt, "size": "800x1200"},
            {"prompt": prompt, "size": "3:4"},
            {"prompt": prompt, "aspect_ratio": "9:16"},
        ):
            with pytest.raises(ImageToolError) as failure:
                await backend.generate_image(arguments, _context())
            assert failure.value.code == "provider_rejected"

        assert attempts == [
            {
                "prompt": prompt,
                "image_url": "att_reference_poster",
                "size": "auto",
            }
        ]

        next_turn = ToolInvocationContext(
            invocation_id="invoke-imagegen-2",
            capability_snapshot_id="capabilities-1",
            policy_snapshot_id="policy-1",
            tool_id="imagegen",
            idempotency_key="turn-2:call-1",
            approved=True,
            effective_sandbox=SandboxLevel.READ_ONLY,
            execution_scope=ToolExecutionScope("job-2", "thread-1", "turn-2"),
            tool_call_id="call-2",
        )
        with pytest.raises(ImageToolError):
            await backend.generate_image({"prompt": prompt}, next_turn)
        assert len(attempts) == 2

    asyncio.run(scenario())


def test_pro_provider_uses_auto_for_non_codex_size() -> None:
    from ecorex.image_orchestrator.openai_provider import OpenAICompatibleImageProvider

    assert OpenAICompatibleImageProvider._provider_size(
        "gpt-image-2-pro", 800, 1200
    ) == "auto"
    assert OpenAICompatibleImageProvider._provider_size(
        "gpt-image-2-pro", 1024, 1536
    ) == "1024x1536"
    assert OpenAICompatibleImageProvider._request_size(
        SimpleNamespace(
            model_id="gpt-image-2-pro",
            width=1024,
            height=1024,
            metadata={"size": "auto"},
        )
    ) == "auto"


def test_runtime_rejects_removed_tasks_before_any_generation_or_artifact() -> None:
    async def scenario() -> None:
        backend = object.__new__(RuntimeImageToolBackend)
        generated = False

        async def fake_single(self, arguments, context):
            nonlocal generated
            generated = True
            return {"images": [{"url": "/unexpected.png"}]}

        backend._generate_single = MethodType(fake_single, backend)
        with pytest.raises(ImageToolError) as rejected:
            await backend.generate_image(
                {"tasks": [{"prompt": "one"}, {"prompt": "two"}]},
                _context(),
            )
        assert rejected.value.code == "imagegen_tasks_unsupported"
        assert generated is False

    asyncio.run(scenario())


def test_public_imagegen_keeps_only_first_unexpected_provider_result(
    monkeypatch, tmp_path,
) -> None:
    from agent.tools.imagegen import imagegen as imagegen_module

    generated = tmp_path / "generated.png"
    generated.write_bytes(b"not-a-real-image")
    monkeypatch.setattr(
        imagegen_module,
        "_authorize_file_access",
        lambda *_args, **_kwargs: (True, ""),
    )
    monkeypatch.setattr(
        imagegen_module,
        "run_image_generation_payload",
        lambda *_args, **_kwargs: {
            "returncode": 0,
            "payload": {
                "provider": "OpenAI",
                "model": "gpt-image-2-pro",
                "attempted_provider_count": 1,
                "images": [
                    {"url": "https://safe.example/first.png"},
                    {"url": "https://safe.example/unexpected-second.png"},
                ],
            },
            "stderr": "",
        },
    )

    result = imagegen_module.ImageGenTool().execute(
        {"prompt": "one image", "output_dir": str(tmp_path)}
    )

    assert result.status == "success"
    assert result.result["model"] == "gpt-image-2-pro"
    assert result.result["fallbackUsed"] is False
    assert [image["url"] for image in result.result["images"]] == [
        "https://safe.example/first.png"
    ]


def test_cow_executes_three_independent_image_calls_and_only_successes_publish(
    tmp_path,
) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.requests = []
            self.scripts = [
                [
                    {
                        "seq": index,
                        "event_type": "tool_call.requested",
                        "response_id": "response-images",
                        "tool_call_id": f"call-{name}",
                        "tool_name": "imagegen",
                        "arguments": {"prompt": name},
                    }
                    for index, name in enumerate(("first", "fails", "third"), 1)
                ] + [
                    {
                        "seq": 4,
                        "event_type": "response.completed",
                        "response_id": "response-images",
                    }
                ],
                [
                    {
                        "seq": 1,
                        "event_type": "output_text.delta",
                        "response_id": "response-final",
                        "delta": "Two images were generated; one call failed.",
                    },
                    {
                        "seq": 2,
                        "event_type": "response.completed",
                        "response_id": "response-final",
                    },
                ],
            ]

        async def stream(self, request):
            self.requests.append(request)
            for raw in self.scripts.pop(0):
                yield GatewayEvent.model_validate(raw)

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
        calls: list[str] = []

        async def fake_single(self, arguments, context):
            prompt = arguments["prompt"]
            calls.append(prompt)
            if prompt == "fails":
                raise ImageToolError("provider_rejected")
            artifact = SimpleNamespace(
                artifact_id=f"art_{prompt}",
                revision_id=f"rev_{prompt}",
                mime_type="image/png",
                size_bytes=5,
                sha256=("1" if prompt == "first" else "3") * 64,
                to_dict=lambda prompt=prompt: {
                    "artifact_id": f"art_{prompt}",
                    "revision_id": f"rev_{prompt}",
                    "family": "image",
                    "role": "deliverable",
                    "visibility": "primary",
                    "status": "ready",
                    "display_name": f"{prompt}.png",
                    "mime_type": "image/png",
                    "size_bytes": 5,
                    "sha256": ("1" if prompt == "first" else "3") * 64,
                    "created_at": "2026-08-13T00:00:00+00:00",
                    "renditions": [],
                    "actions": ["preview", "download"],
                    "feedback": None,
                    "lineage": {"source_artifact_ids": [], "supersedes_revision_id": None},
                    "quality_evidence": {
                        "status": "not_checked", "checks": [], "score": None, "summary": None,
                    },
                },
            )
            result = self._emit_artifact_item(
                artifact,
                context,
                f"test:{context.idempotency_key}",
                f"job-{prompt}",
            )
            return self._cow_result(result, "gpt-image-2-pro")

        backend._generate_single = MethodType(fake_single, backend)
        thread = kernel.create_thread(CreateThreadRequest(title="independent images"))
        prepared = composition.prepare_turn(
            CreateTurnRequest(
                input="Generate three independent images",
                agent_model_id="ecorex-chat",
                image_model_id="gpt-image-2",
                explicit_tool_ids=["imagegen"],
                client_message_id="independent-image-calls",
            )
        )
        kernel.create_turn(
            thread.thread_id,
            prepared.request,
            snapshot_context=prepared.snapshot_context,
        )
        gateway = Gateway()
        worker = AgentTurnWorker(
            kernel,
            gateway=gateway,
            capabilities=composition.capability_service,
        )

        completed = await worker.run_once("worker-independent-images")
        assert completed.outcome is WorkerOutcome.COMPLETED
        assert calls == ["first", "fails", "third"]
        outputs = [
            item
            for item in gateway.requests[1].ordered_input_items()
            if isinstance(item, GatewayFunctionCallOutputInput)
        ]
        assert len(outputs) == 3
        artifacts = [
            item
            for item in kernel.projection(thread.thread_id).items
            if item.kind is ItemKind.ARTIFACT
        ]
        assert [item.content["artifact"]["artifact_id"] for item in artifacts] == [
            "art_first",
            "art_third",
        ]
        assert all("image_batch" not in item.content for item in artifacts)
        assert not any(
            event.event_type.startswith("artifact.image.batch_")
            for event in kernel.events.page(thread.thread_id, limit=200).events
        )
        await worker.close()

    asyncio.run(scenario())
