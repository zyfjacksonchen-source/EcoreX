from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

from ecorex.gateway import GatewayEvent
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest
from ecorex.runtime import (
    AgentTurnWorker,
    PermissionAuthority,
    RuntimeSettings,
    ToolExecutionRepository,
    WorkerOutcome,
    create_app,
)


def _permission_selects(statements: list[str]) -> list[str]:
    tables = (
        "RUNTIME_PERMISSION_STATE",
        "PERMISSION_STATE_LEDGER",
        "PERMISSION_CHANGE_REQUESTS",
    )
    return [
        normalized
        for statement in statements
        if (normalized := " ".join(statement.upper().split())).startswith("SELECT")
        and any(table in normalized for table in tables)
    ]


def _runtime(tmp_path, handler):
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            full_access=True,
            installed_capability_packs=frozenset({"sandbox"}),
            capability_handlers={"shell": handler},
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="governance sample"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="使用 shell 完成这一步",
            explicit_tool_ids=["shell"],
            client_message_id="permission-governance-sample",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    return (
        app,
        kernel,
        composition,
        thread,
        created,
        prepared.snapshot_context.capability_snapshot_id,
    )


def test_one_governance_scope_verifies_permission_once_and_next_call_is_fresh(
    tmp_path,
    monkeypatch,
) -> None:
    app, _kernel, composition, _thread, _created, capability_snapshot_id = _runtime(
        tmp_path,
        lambda arguments, context: {"exit_code": 0},
    )
    authority = app.state.permission_authority
    changed = authority.update(
        "default",
        expected_revision=1,
        client_request_id="governance-history-default",
    )
    composition.record_permission(changed)
    changed = authority.update(
        "full_access",
        expected_revision=2,
        client_request_id="governance-history-full",
    )
    composition.record_permission(changed)
    statements: list[str] = []
    original_connect = authority.database.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(authority.database, "connect", traced_connect)

    first = composition.capability_service.invocation_governance(
        capability_snapshot_id,
        "shell",
    )
    first_queries = _permission_selects(statements)
    assert first.allowed is True
    assert first.requires_approval is False
    assert first.current_permission_state_digest is not None
    assert len(first_queries) == 4
    assert sum("PERMISSION_STATE_LEDGER" in query for query in first_queries) == 2

    statements.clear()
    revoked = authority.update(
        "default",
        expected_revision=3,
        client_request_id="governance-next-call-revoked",
    )
    composition.record_permission(revoked)
    statements.clear()
    second = composition.capability_service.invocation_governance(
        capability_snapshot_id,
        "shell",
    )
    second_queries = _permission_selects(statements)
    assert second.allowed is True
    assert second.requires_approval is True
    assert second.current_permission_state_digest is not None
    assert second.current_permission_state_digest != first.current_permission_state_digest
    assert len(second_queries) == 4
    assert sum("PERMISSION_STATE_LEDGER" in query for query in second_queries) == 2


class _Gateway:
    def __init__(self, scripts) -> None:
        self.scripts = list(scripts)

    async def stream(self, _request):
        for event in self.scripts.pop(0):
            yield GatewayEvent.model_validate(event)


def _shell_scripts(call_id: str):
    return [
        [
            {
                "seq": 1,
                "event_type": "tool_call.requested",
                "response_id": f"response-{call_id}",
                "tool_call_id": call_id,
                "tool_name": "shell",
                "arguments": {"command": "opaque-command"},
            }
        ],
        [
            {
                "seq": 1,
                "event_type": "response.completed",
                "response_id": f"completed-{call_id}",
            }
        ],
    ]


def test_cross_process_revocation_invalidates_scoped_sample_before_dispatch(
    tmp_path,
) -> None:
    calls = []
    app, kernel, composition, thread, created, _capability_snapshot_id = _runtime(
        tmp_path,
        lambda arguments, context: calls.append((arguments, context))
        or {"exit_code": 0},
    )
    authority = app.state.permission_authority
    separate_authority = PermissionAuthority(
        tmp_path / "runtime.db",
        account_id="local-user",
        initial_full_access=True,
    )
    call_id = "scoped-sample-cross-process-revoke"
    worker = AgentTurnWorker(
        kernel,
        gateway=_Gateway([_shell_scripts(call_id)[0]]),
        capabilities=composition.capability_service,
        permission_mutation_lock=authority.mutation_lock,
    )
    original_admit = worker.tool_executions.admit
    entered_admission = threading.Event()
    release_admission = threading.Event()

    def delayed_admit(**kwargs):
        entered_admission.set()
        assert release_admission.wait(timeout=10)
        return original_admit(**kwargs)

    worker.tool_executions.admit = delayed_admit
    with ThreadPoolExecutor(max_workers=1) as executor:
        execution = executor.submit(asyncio.run, worker.run_once("scope-worker"))
        assert entered_admission.wait(timeout=10)
        revoked = separate_authority.update(
            "default",
            expected_revision=1,
            client_request_id="scoped-sample-separate-revoke",
        )
        composition.record_permission(revoked)
        release_admission.set()
        result = execution.result(timeout=20)

    assert result.outcome is WorkerOutcome.WAITING_HUMAN
    assert calls == []
    execution_id = AgentTurnWorker._execution_id(created.turn.turn_id, call_id)
    assert ToolExecutionRepository(kernel.database).admission(execution_id) is None
    interactions = kernel.list_interactions(thread.thread_id).interactions
    assert len(interactions) == 1
    assert interactions[0].kind.value == "permission_approval"
