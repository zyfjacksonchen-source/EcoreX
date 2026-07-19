from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from ecorex.capabilities import (
    CapabilityEffect,
    CapabilityService,
    CapabilitySnapshotRepository,
    IdempotencyClass,
    ExecutionPolicy,
    PermissionProfile,
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
        for event in script:
            yield GatewayEvent.model_validate(event)


class _ProfiledShellHandler:
    sandbox_profile_availability = {
        "workspace-write": "windows_appcontainer_helper_not_configured",
        "danger-full-access": None,
        "read-only": "shell_read_only_profile_unsupported",
    }

    def __call__(self, _arguments, _context):
        return {"exit_code": 0}


class _PackRuntimeDouble:
    def __init__(self, handler) -> None:
        self.handlers = {"shell": handler}
        self.installed_pack_ids = frozenset({"sandbox"})


def test_builtin_shell_is_always_non_idempotent_and_not_concurrency_safe() -> None:
    shell = builtin_capability_registry().get("shell")
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


def test_handler_set_preserves_profile_specific_fail_closed_availability(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = builtin_capability_registry()
    # This test uses a trusted-core handler only to exercise the immutable
    # availability projection. Product shell handlers come from signed packs.
    handlers = build_capability_handler_set(
        registry,
        workspace_roots=(workspace,),
        pack_runtime=_PackRuntimeDouble(_ProfiledShellHandler()),  # type: ignore[arg-type]
    )
    assert handlers.sandbox_profile_availability["shell"] == {
        "workspace-write": "windows_appcontainer_helper_not_configured",
        "danger-full-access": None,
        "read-only": "shell_read_only_profile_unsupported",
    }
    with pytest.raises(ValueError, match="verified sandbox capability pack"):
        build_capability_handler_set(
            registry,
            workspace_roots=(workspace,),
            trusted_core_handlers={"shell": _ProfiledShellHandler()},
        )


def test_full_access_never_bypasses_an_admin_shell_deny() -> None:
    service = CapabilityService(builtin_capability_registry())
    plan = service.create_plan(
        intent="shell",
        explicit_tools=("shell",),
        availability=RuntimeAvailability(
            platform="windows", installed_packs=frozenset({"sandbox"})
        ),
        policy=ExecutionPolicy(
            snapshot_id="admin-deny",
            profile=PermissionProfile.FULL_ACCESS,
            admin_hard_denies=frozenset({"shell"}),
        ),
    )
    decision = plan.decision("shell")
    assert decision is not None and decision.eligible is False
    assert decision.reason_codes == ("admin_hard_deny", "explicit_reference")


def test_runtime_profile_switch_enables_only_future_full_access_shell_turns(
    tmp_path: Path,
) -> None:
    token = "r" * 32
    csrf = "c" * 32
    origin = "http://testserver"
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=token,
            csrf_token=csrf,
            webui_origins=(origin,),
            installed_capability_packs=frozenset({"sandbox"}),
            capability_handlers={"shell": _ProfiledShellHandler()},
            capability_sandbox_profile_availability={
                "shell": _ProfiledShellHandler.sandbox_profile_availability
            },
        )
    )
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {token}"}
    mutation = {**auth, "Origin": origin, "X-EcoreX-CSRF": csrf}
    thread_id = client.post(
        "/api/v1/threads", json={"title": "sandbox"}, headers=mutation
    ).json()["thread_id"]
    first = client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={
            "input": "run shell",
            "agent_model_id": "ecorex-chat",
            "client_message_id": "sandbox-default",
        },
        headers=mutation,
    )
    assert first.status_code == 202
    first_turn_id = first.json()["turn"]["turn_id"]
    first_event = next(
        event
        for event in app.state.runtime.events.page(thread_id).events
        if event.turn_id == first_turn_id and event.event_type == "turn.accepted"
    )
    first_plan = CapabilitySnapshotRepository(tmp_path / "runtime.db").get(
        first_event.capability_snapshot_id
    )
    first_shell = first_plan.decision("shell")
    assert first_shell is not None and first_shell.eligible is False
    assert (
        "disabled:windows_appcontainer_helper_not_configured"
        in first_shell.reason_codes
    )

    permission = client.get("/api/v1/bootstrap", headers=auth).json()["permissions"]
    changed = client.put(
        "/api/v1/settings/permissions",
        json={
            "profile": "full_access",
            "expected_revision": permission["revision"],
            "client_request_id": "enable-danger-profile",
        },
        headers=mutation,
    )
    assert changed.status_code == 200
    second = client.post(
        f"/api/v1/threads/{thread_id}/queue",
        json={
            "input": "run shell",
            "agent_model_id": "ecorex-chat",
            "client_message_id": "sandbox-full",
        },
        headers=mutation,
    )
    assert second.status_code == 202
    second_turn_id = second.json()["turn"]["turn_id"]
    second_event = next(
        event
        for event in app.state.runtime.events.page(thread_id).events
        if event.turn_id == second_turn_id and event.event_type == "turn.accepted"
    )
    second_plan = CapabilitySnapshotRepository(tmp_path / "runtime.db").get(
        second_event.capability_snapshot_id
    )
    second_shell = second_plan.decision("shell")
    assert second_shell is not None and second_shell.eligible is True
    assert second_shell.effective_sandbox.value == "danger-full-access"
    assert second_shell.requires_approval is False
    # The immutable first Turn keeps its original fail-closed snapshot.
    assert first_plan.decision("shell") == first_shell


def test_non_idempotent_shell_crash_persists_uncertain_hitl_and_never_auto_retries(
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
            capability_handlers={"shell": crashing_shell},
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
                    "tool_name": "shell",
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
    assert asyncio.run(worker.run_once("shell-worker")).outcome is WorkerOutcome.WAITING_HUMAN
    approval = kernel.list_interactions(thread.thread_id).interactions[0]
    kernel.respond_interaction(
        approval.interaction_id,
        {"action_id": "allow", "values": {}},
        client_request_id="allow-shell",
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


def test_shell_preflight_failure_does_not_create_false_uncertain_hitl(
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
            capability_handlers={"shell": unavailable_shell},
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
                    "tool_name": "shell",
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

    assert asyncio.run(worker.run_once("preflight-worker")).outcome is WorkerOutcome.WAITING_HUMAN
    approval = kernel.list_interactions(thread.thread_id).interactions[0]
    kernel.respond_interaction(
        approval.interaction_id,
        {"action_id": "allow", "values": {}},
        client_request_id="allow-preflight-shell",
    )

    failed = asyncio.run(worker.run_once("preflight-worker"))
    assert failed.outcome is WorkerOutcome.FAILED
    assert calls == 1
    execution_id = AgentTurnWorker._execution_id(created.turn.turn_id, "shell-call")
    execution = ToolExecutionRepository(kernel.database).get(execution_id)
    assert execution.status == "failed"
    assert execution.error_code == "capabilitypackprocesserror"
    assert not any(
        interaction.kind.value == "conflict_resolution"
        for interaction in kernel.list_interactions(thread.thread_id).interactions
    )
