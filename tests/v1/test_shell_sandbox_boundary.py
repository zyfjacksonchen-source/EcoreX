from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ecorex.capabilities import (
    CapabilityEffect,
    CapabilityService,
    IdempotencyClass,
    ExecutionPolicy,
    RuntimeAvailability,
    build_capability_handler_set,
    builtin_capability_registry,
    ToolSpec,
)
from ecorex.gateway import GatewayEvent
from ecorex.integration.pack_process import CapabilityPackProcessError
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest
from ecorex.runtime import AgentTurnWorker, RuntimeKernel, RuntimeSettings, WorkerOutcome, create_app
from ecorex.runtime.tool_executions import ToolExecutionRepository


class _ScriptedGateway:
    def __init__(self, scripts):
        self.scripts = list(scripts)

    async def stream(self, _request):
        script = self.scripts.pop(0)
        if len(script) == 1 and script[0]["event_type"] == "response.completed":
            yield GatewayEvent.model_validate(
                {
                    "seq": 1,
                    "event_type": "output_text.delta",
                    "response_id": script[0]["response_id"],
                    "delta": "done",
                }
            )
            script = [{**script[0], "seq": 2}]
        for event in script:
            yield GatewayEvent.model_validate(event)


class _ProfiledShellHandler:
    def __call__(self, _arguments, _context):
        return {"exit_code": 0}


class _PackRuntimeDouble:
    def __init__(self, handler) -> None:
        self.handlers = {"bash": handler}
        self.installed_pack_ids = frozenset({"sandbox"})


def test_builtin_shell_is_always_non_idempotent_and_not_concurrency_safe() -> None:
    shell = builtin_capability_registry().get("bash")
    assert shell.idempotency is IdempotencyClass.NON_IDEMPOTENT
    assert shell.concurrency_safe is False
    with pytest.raises(ValueError, match="opaque execute"):
        ToolSpec(
            tool_id="unsafe-command",
            version="1.0.0",
            display_name="Unsafe command",
            description="Opaque command without proof",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            effects=frozenset({CapabilityEffect.EXECUTE}),
            idempotency=IdempotencyClass.IDEMPOTENT,
        )


def test_handler_set_does_not_add_a_second_permission_profile_layer(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = builtin_capability_registry()
    handlers = build_capability_handler_set(
        registry,
        workspace_roots=(workspace,),
        pack_runtime=_PackRuntimeDouble(_ProfiledShellHandler()),  # type: ignore[arg-type]
    )
    assert "sandbox_profile_availability" not in handlers.__dataclass_fields__
    with pytest.raises(ValueError, match="core handler is duplicated: bash"):
        build_capability_handler_set(
            registry,
            workspace_roots=(workspace,),
            trusted_core_handlers={"bash": _ProfiledShellHandler()},
        )

def test_shell_matches_cowagent_as_a_direct_builtin_without_approval() -> None:
    service = CapabilityService(builtin_capability_registry())
    plan = service.create_plan(
        intent="bash",
        explicit_tools=("bash",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(snapshot_id="cowagent-direct"),
    )
    decision = plan.decision("bash")
    assert decision is not None
    assert decision.eligible is True
    assert decision.requires_approval is False


# Retired: public Cow shell execution has no legacy uncertain-HITL pipeline.
def retired_legacy_non_idempotent_shell_crash_persists_uncertain_hitl_and_never_auto_retries(
    tmp_path: Path,
) -> None:
    calls = 0

    def crashing_shell(_arguments, _context):
        nonlocal calls
        calls += 1
        raise RuntimeError("child acknowledgement lost")

    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            installed_capability_packs=frozenset({"sandbox"}),
            capability_handlers={"bash": crashing_shell},
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="uncertain shell"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="run shell",
            agent_model_id="ecorex-chat",
            client_message_id="uncertain-shell",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    gateway = _ScriptedGateway(
        [
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "shell-response",
                    "tool_call_id": "shell-call",
                    "tool_name": "bash",
                    "arguments": {"command": "opaque-command"},
                }
            ],
            [
                {
                    "seq": 1,
                    "event_type": "response.completed",
                    "response_id": "after-skip",
                }
            ],
        ]
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        retry_delay_seconds=0,
    )
    crashed = asyncio.run(worker.run_once("shell-worker"))
    assert crashed.outcome is WorkerOutcome.WAITING_HUMAN
    assert calls == 1
    job = kernel.jobs.get(created.job.job_id)
    assert job.status.value == "waiting_human"
    assert job.checkpoint and job.checkpoint["phase"] == "uncertain_tool_execution"
    assert not any(
        event.event_type == "job.retry_scheduled"
        for event in kernel.events.page(thread.thread_id).events
    )

    restarted = RuntimeKernel(tmp_path / "runtime.db")
    restarted_worker = AgentTurnWorker(
        restarted,
        gateway=gateway,
        capabilities=composition.capability_service,
        retry_delay_seconds=0,
    )
    still_waiting = asyncio.run(restarted_worker.run_once("shell-worker-restarted"))
    assert still_waiting.outcome is WorkerOutcome.IDLE
    assert restarted.jobs.get(created.job.job_id).status.value == "waiting_human"
    assert calls == 1
    interactions = restarted.list_interactions(thread.thread_id).interactions
    uncertain = next(item for item in interactions if item.status.value == "pending")
    restarted.respond_interaction(
        uncertain.interaction_id,
        {"action_id": "skip", "values": {}},
        client_request_id="skip-uncertain-shell",
    )
    completed = asyncio.run(restarted_worker.run_once("shell-worker-restarted"))
    assert completed.outcome is WorkerOutcome.COMPLETED
    assert calls == 1


def retired_legacy_shell_preflight_failure_does_not_create_false_uncertain_hitl(
    tmp_path: Path,
) -> None:
    calls = 0

    def unavailable_shell(_arguments, _context):
        nonlocal calls
        calls += 1
        # This is returned before a capability pack can start a command.
        raise CapabilityPackProcessError("workspace_sandbox_unavailable")

    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            installed_capability_packs=frozenset({"sandbox"}),
            capability_handlers={"bash": unavailable_shell},
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="failed shell preflight"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="run shell",
            agent_model_id="ecorex-chat",
            client_message_id="failed-shell-preflight",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    gateway = _ScriptedGateway(
        [
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "shell-response",
                    "tool_call_id": "shell-call",
                    "tool_name": "bash",
                    "arguments": {"command": "opaque-command"},
                }
            ]
        ]
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        retry_delay_seconds=0,
    )

    failed = asyncio.run(worker.run_once("preflight-worker"))
    assert failed.outcome is WorkerOutcome.FAILED
    assert calls == 1
    execution_id = AgentTurnWorker._execution_id(created.turn.turn_id, "shell-call")
    execution = ToolExecutionRepository(kernel.database).get(execution_id)
    assert execution.status == "failed"
    assert execution.error_code == "workspace_sandbox_unavailable"
    assert not any(
        interaction.kind.value == "conflict_resolution"
        for interaction in kernel.list_interactions(thread.thread_id).interactions
    )
