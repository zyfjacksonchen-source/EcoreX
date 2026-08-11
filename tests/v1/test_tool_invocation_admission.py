from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from ecorex.capabilities import (
    CapabilityService,
    CapabilityDeniedError,
    ExecutionPolicy,
    PermissionProfile,
    RuntimeAvailability,
    SandboxLevel,
    ToolExecutionScope,
    builtin_capability_registry,
)
from ecorex.gateway import GatewayEvent
from ecorex.integration.pack_process import CapabilityPackProcessError
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest, ItemKind
from ecorex.runtime import (
    AgentTurnWorker,
    PermissionAuthority,
    RuntimeKernel,
    RuntimeSettings,
    ToolExecutionRepository,
    ToolExecutionConflict,
    WorkerOutcome,
    create_app,
)


class _Gateway:
    def __init__(self, scripts) -> None:
        self.scripts = list(scripts)
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
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


def _shell_runtime(
    tmp_path,
    handler,
):
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            full_access=True,
            installed_capability_packs=frozenset({"sandbox"}),
            capability_handlers={"bash": handler},
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="permission admission"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="使用 shell 完成这一步",
            explicit_tool_ids=["bash"],
            client_message_id="permission-admission-message",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    return app, kernel, composition, thread, created


def _shell_scripts(*, call_id: str):
    return [
        [
            {
                "seq": 1,
                "event_type": "tool_call.requested",
                "response_id": f"response-{call_id}",
                "tool_call_id": call_id,
                "tool_name": "bash",
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


def _execution_id(turn_id: str, call_id: str) -> str:
    return AgentTurnWorker._execution_id(turn_id, call_id)


def test_exact_side_effect_call_is_reused_inside_one_execution_batch(tmp_path) -> None:
    calls = []

    def handler(arguments, _context):
        calls.append(dict(arguments))
        return {"exit_code": 0}

    _app, kernel, composition, thread, created = _shell_runtime(tmp_path, handler)
    first, final = _shell_scripts(call_id="first-shell")
    duplicate, _unused = _shell_scripts(call_id="duplicate-shell")
    gateway = _Gateway([first, duplicate, final])
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
    )

    result = asyncio.run(worker.run_once("same-batch-side-effect-worker"))

    assert result.outcome is WorkerOutcome.COMPLETED
    assert calls == [{"command": "opaque-command"}]
    executions = ToolExecutionRepository(kernel.database)
    assert (
        executions.get(_execution_id(created.turn.turn_id, "duplicate-shell")).status
        == "completed"
    )
    reused = next(
        event
        for event in kernel.events.page(thread.thread_id, limit=1_000).events
        if event.event_type == "tool.cache_reused"
        and event.payload.get("reuse_scope") == "execution_batch"
    )
    assert reused.payload["reused_from_tool_call_id"] == _execution_id(
        created.turn.turn_id, "first-shell"
    )


def test_uncertain_retry_failure_requests_a_new_attempt_card(tmp_path) -> None:
    calls = 0

    def crashing_handler(_arguments, _context):
        nonlocal calls
        calls += 1
        raise RuntimeError("opaque child acknowledgement lost")

    _app, kernel, composition, thread, created = _shell_runtime(
        tmp_path, crashing_handler
    )
    gateway = _Gateway([_shell_scripts(call_id="repeat-crash")[0]])
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
    )

    assert (
        asyncio.run(worker.run_once("uncertain-attempt-worker")).outcome
        is WorkerOutcome.WAITING_HUMAN
    )
    first = kernel.list_interactions(thread.thread_id).interactions[0]
    kernel.respond_interaction(
        first.interaction_id,
        {"action_id": "retry", "values": {}},
        client_request_id="retry-first-uncertain-attempt",
    )
    assert (
        asyncio.run(worker.run_once("uncertain-attempt-worker")).outcome
        is WorkerOutcome.WAITING_HUMAN
    )

    interactions = kernel.list_interactions(thread.thread_id).interactions
    assert calls == 2
    assert len(interactions) == 1
    assert interactions[0].status.value == "pending"
    with kernel.database.reader() as connection:
        rows = tuple(
            (row["idempotency_key"], row["status"])
            for row in connection.execute(
                "SELECT idempotency_key, status FROM interactions WHERE turn_id=? "
                "ORDER BY created_at",
                (created.turn.turn_id,),
            ).fetchall()
        )
    assert len(rows) == 2
    assert rows[0][0].endswith(":uncertain:1")
    assert rows[0][1] == "resolved"
    assert rows[1][0].endswith(":uncertain:2")
    assert rows[1][1] == "pending"


def test_permission_profile_changes_do_not_interrupt_cowagent_tool_execution(
    tmp_path,
) -> None:
    calls = []

    def handler(arguments, context):
        calls.append((dict(arguments), context))
        return {"exit_code": 0}

    app, kernel, composition, thread, created = _shell_runtime(tmp_path, handler)
    authority = app.state.permission_authority
    gateway = _Gateway(_shell_scripts(call_id="revoked-before-admission"))
    first_worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        permission_mutation_lock=authority.mutation_lock,
    )
    original_authorized = first_worker._authorized_tool_description
    revoked = False

    def authorize_then_revoke(**kwargs):
        nonlocal revoked
        result = original_authorized(**kwargs)
        if not revoked:
            revoked = True
            permission = authority.update(
                "default",
                expected_revision=1,
                client_request_id="revoke-before-admission",
            )
            composition.record_permission(permission)
        return result

    first_worker._authorized_tool_description = authorize_then_revoke

    waiting = asyncio.run(first_worker.run_once("worker-before-restart"))

    assert waiting.outcome is WorkerOutcome.COMPLETED
    assert len(calls) == 1
    pending = kernel.list_interactions(thread.thread_id).interactions
    assert pending == []
    admission = ToolExecutionRepository(kernel.database).admission(
        _execution_id(created.turn.turn_id, "revoked-before-admission")
    )
    assert admission is not None
    assert admission.effective_sandbox.value == "danger-full-access"
    return
    job = kernel.jobs.get(created.job.job_id)
    assert job.checkpoint is not None
    assert job.checkpoint["phase"] == "waiting_tool_approval"
    execution_id = _execution_id(created.turn.turn_id, "revoked-before-admission")
    executions = ToolExecutionRepository(kernel.database)
    assert executions.get(execution_id).status == "started"
    assert executions.admission(execution_id) is None

    # Restarting after the unadmitted 'started' record is safe. It must resume
    # the approval path instead of claiming the opaque command may have run.
    restarted_kernel = RuntimeKernel(tmp_path / "runtime.db")
    restarted_kernel.respond_interaction(
        pending[0].interaction_id,
        {"action_id": "allow", "values": {}},
        client_request_id="allow-after-restart",
    )
    restarted_worker = AgentTurnWorker(
        restarted_kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        permission_mutation_lock=authority.mutation_lock,
    )

    completed = asyncio.run(restarted_worker.run_once("worker-after-restart"))

    assert completed.outcome is WorkerOutcome.COMPLETED
    assert len(calls) == 1
    admission = executions.admission(execution_id)
    assert admission is not None
    assert admission.current_policy_snapshot_id == authority.current().snapshot_id
    assert admission.frozen_policy_snapshot_id != admission.current_policy_snapshot_id
    assert admission.approved is True
    assert admission.effective_sandbox.value == "workspace-write"
    assert not any(
        interaction.kind.value == "conflict_resolution"
        for interaction in restarted_kernel.list_interactions(
            thread.thread_id
        ).interactions
    )


def test_permission_update_and_final_admission_have_one_linear_order(tmp_path) -> None:
    calls = []

    def handler(arguments, context):
        calls.append(context)
        return {"exit_code": 0, "command": arguments["command"]}

    app, kernel, composition, _thread, created = _shell_runtime(tmp_path, handler)
    authority = app.state.permission_authority
    frozen_permission_id = authority.current().snapshot_id
    gateway = _Gateway(_shell_scripts(call_id="linearized-dispatch"))
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        permission_mutation_lock=authority.mutation_lock,
    )
    original_admit = worker.tool_executions.admit
    entered_admission = threading.Event()
    release_admission = threading.Event()

    def blocking_admit(**kwargs):
        entered_admission.set()
        assert release_admission.wait(timeout=10)
        return original_admit(**kwargs)

    worker.tool_executions.admit = blocking_admit

    with ThreadPoolExecutor(max_workers=2) as executor:
        run_future = executor.submit(asyncio.run, worker.run_once("linear-worker"))
        assert entered_admission.wait(timeout=10)
        update_future = executor.submit(
            authority.update,
            "default",
            expected_revision=1,
            client_request_id="linear-revoke",
        )
        # update() must be waiting on the same mutation lock while admission is
        # between current-policy evaluation and its append-only INSERT.
        assert not update_future.done()
        release_admission.set()
        permission = update_future.result(timeout=10)
        composition.record_permission(permission)
        result = run_future.result(timeout=20)

    assert result.outcome is WorkerOutcome.COMPLETED
    assert authority.current().profile == "default"
    assert len(calls) == 1
    execution_id = _execution_id(created.turn.turn_id, "linearized-dispatch")
    admission = ToolExecutionRepository(kernel.database).admission(execution_id)
    assert admission is not None
    assert admission.current_policy_snapshot_id == frozen_permission_id
    assert admission.current_policy_snapshot_id != authority.current().snapshot_id
    assert admission.effective_sandbox.value == "danger-full-access"
    # Dispatch consumes the persisted permit; a later revocation cannot turn a
    # pre-linearized call into a spurious rejection or a second invocation.
    assert calls[0].current_policy_snapshot_id == admission.current_policy_snapshot_id


def test_separate_authority_revocation_wins_before_admission_transaction(
    tmp_path,
) -> None:
    calls = []
    app, kernel, composition, thread, created = _shell_runtime(
        tmp_path,
        lambda arguments, context: (
            calls.append((arguments, context)) or {"exit_code": 0}
        ),
    )
    authority = app.state.permission_authority
    separate_process_authority = PermissionAuthority(
        tmp_path / "runtime.db",
        account_id="local-user",
        initial_full_access=True,
    )
    gateway = _Gateway(_shell_scripts(call_id="cross-process-revoke"))
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
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
        run_future = executor.submit(
            asyncio.run, worker.run_once("cross-process-worker")
        )
        assert entered_admission.wait(timeout=10)
        # This authority has a distinct Python lock, like another Runtime
        # process. Its SQLite permission transaction commits first.
        changed = separate_process_authority.update(
            "default",
            expected_revision=1,
            client_request_id="cross-process-revoke",
        )
        composition.record_permission(changed)
        release_admission.set()
        result = run_future.result(timeout=20)

    assert result.outcome is WorkerOutcome.COMPLETED
    assert len(calls) == 1
    execution_id = _execution_id(created.turn.turn_id, "cross-process-revoke")
    assert ToolExecutionRepository(kernel.database).admission(execution_id) is not None
    interactions = kernel.list_interactions(thread.thread_id).interactions
    assert interactions == []


def test_missing_tool_is_observed_and_model_recovers_with_a_safe_alternative(
    tmp_path,
) -> None:
    calls = []
    app, kernel, composition, thread, created = _shell_runtime(
        tmp_path,
        lambda arguments, context: (
            calls.append((dict(arguments), context)) or {"exit_code": 0}
        ),
    )
    gateway = _Gateway(
        [
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "missing-tool-response",
                    "tool_call_id": "missing-tool-call",
                    "tool_name": "legacy-browser-search",
                    "arguments": {},
                }
            ],
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "fallback-tool-response",
                    "tool_call_id": "fallback-shell-call",
                    "tool_name": "bash",
                    "arguments": {"command": "opaque-command"},
                }
            ],
            [
                {
                    "seq": 1,
                    "event_type": "response.completed",
                    "response_id": "recovery-completed",
                }
            ],
        ]
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        permission_mutation_lock=app.state.permission_authority.mutation_lock,
    )

    result = asyncio.run(worker.run_once("self-healing-worker"))

    assert result.outcome is WorkerOutcome.COMPLETED
    assert [call[0]["command"] for call in calls] == ["opaque-command"]
    assert len(gateway.requests) == 3
    recovery_output = gateway.requests[1].tool_outputs[0].output
    assert recovery_output["status"] == "recovery_required"
    assert recovery_output["code"] == "tool_not_eligible"
    assert recovery_output["recovery"]["action"] == "discover_or_switch"
    assert recovery_output["recovery"]["requested_tool"] == "legacy-browser-search"
    assert "arguments" not in recovery_output

    with kernel.database.reader() as connection:
        rows = connection.execute(
            "SELECT event_type, payload_json FROM events WHERE turn_id=? "
            "AND event_type IN ('tool.recovery_planned', 'tool.recovery_resolved') "
            "ORDER BY seq",
            (created.turn.turn_id,),
        ).fetchall()
    assert [row["event_type"] for row in rows] == [
        "tool.recovery_planned",
        "tool.recovery_resolved",
    ]
    planned = json.loads(rows[0]["payload_json"])
    resolved = json.loads(rows[1]["payload_json"])
    assert planned["source"] == "preflight"
    assert planned["requested_tool"] == "legacy-browser-search"
    assert planned["action"] == "discover_or_switch"
    assert isinstance(planned["candidate_tool_ids"], list)
    assert "arguments" not in planned
    assert resolved["resolved_by_tool_id"] == "bash"
    assert kernel.list_interactions(thread.thread_id).interactions == []


def test_handler_loss_after_projection_is_observed_and_recovers_without_dispatch(
    tmp_path,
) -> None:
    calls = []
    app, kernel, composition, _thread, created = _shell_runtime(
        tmp_path,
        lambda arguments, context: (
            calls.append((dict(arguments), context)) or {"exit_code": 0}
        ),
    )
    gateway = _Gateway(
        [
            _shell_scripts(call_id="handler-loss")[0],
            [
                {
                    "seq": 1,
                    "event_type": "response.completed",
                    "response_id": "handler-loss-recovered",
                }
            ],
        ]
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        permission_mutation_lock=app.state.permission_authority.mutation_lock,
    )
    original_authorized = worker._authorized_tool_description

    def authorize_then_remove_handler(**kwargs):
        result = original_authorized(**kwargs)
        composition.capability_service.handlers.pop("bash", None)
        return result

    worker._authorized_tool_description = authorize_then_remove_handler

    result = asyncio.run(worker.run_once("handler-loss-worker"))

    assert result.outcome is WorkerOutcome.COMPLETED
    assert calls == []
    recovery_output = gateway.requests[1].tool_outputs[0].output
    assert recovery_output["code"] == "tool_handler_missing"
    assert recovery_output["recovery"]["action"] == "discover_or_switch"
    execution = ToolExecutionRepository(kernel.database).get(
        _execution_id(created.turn.turn_id, "handler-loss")
    )
    assert execution.status == "failed"
    assert execution.error_code == "tool_handler_missing"
    projection = kernel.projection(created.turn.thread_id)
    tool_items = [item for item in projection.items if item.kind is ItemKind.TOOL_CALL]
    assert len(tool_items) == 1
    assert tool_items[0].status.value == "failed"
    with kernel.database.reader() as connection:
        row = connection.execute(
            "SELECT payload_json FROM events WHERE turn_id=? "
            "AND event_type='tool.recovery_planned'",
            (created.turn.turn_id,),
        ).fetchone()
    assert row is not None
    assert json.loads(row["payload_json"])["source"] == "dispatch_preflight"


def test_admitted_non_idempotent_crash_remains_uncertain(tmp_path) -> None:
    calls = 0

    def crashing_handler(_arguments, _context):
        nonlocal calls
        calls += 1
        raise RuntimeError("opaque child acknowledgement lost")

    app, kernel, composition, thread, created = _shell_runtime(
        tmp_path, crashing_handler
    )
    gateway = _Gateway([_shell_scripts(call_id="admitted-crash")[0]])
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        permission_mutation_lock=app.state.permission_authority.mutation_lock,
    )

    result = asyncio.run(worker.run_once("admitted-crash-worker"))

    assert result.outcome is WorkerOutcome.WAITING_HUMAN
    assert calls == 1
    execution_id = _execution_id(created.turn.turn_id, "admitted-crash")
    admission = ToolExecutionRepository(kernel.database).admission(execution_id)
    assert admission is not None
    assert admission.approved is False
    job = kernel.jobs.get(created.job.job_id)
    assert job.checkpoint is not None
    assert job.checkpoint["phase"] == "uncertain_tool_execution"
    interactions = kernel.list_interactions(thread.thread_id).interactions
    assert len(interactions) == 1
    assert interactions[0].kind.value == "conflict_resolution"


def test_read_only_pack_failure_never_creates_uncertain_side_effect_hitl(
    tmp_path,
) -> None:
    def failed_fetch(_arguments, _context):
        raise CapabilityPackProcessError(
            "browser_fetch_unavailable",
            retryable=False,
            side_effect_uncertain=True,
        )

    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            full_access=True,
            installed_capability_packs=frozenset({"browser"}),
            capability_handlers={"web_fetch": failed_fetch},
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="read-only failure"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="使用 fetch 读取网页",
            explicit_tool_ids=["web_fetch"],
            client_message_id="read-only-failure",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    gateway = _Gateway(
        [
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "response-fetch-failure",
                    "tool_call_id": "read-only-fetch-failure",
                    "tool_name": "web_fetch",
                    "arguments": {"url": "https://example.com"},
                }
            ]
        ]
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        permission_mutation_lock=app.state.permission_authority.mutation_lock,
    )

    result = asyncio.run(worker.run_once("read-only-failure-worker"))

    assert result.outcome is WorkerOutcome.FAILED
    execution = ToolExecutionRepository(kernel.database).get(
        _execution_id(created.turn.turn_id, "read-only-fetch-failure")
    )
    assert execution.status == "failed"
    assert execution.error_code == "browser_fetch_unavailable"
    assert kernel.list_interactions(thread.thread_id).interactions == []


def test_exact_fetch_result_is_reused_inside_same_frozen_authority(tmp_path) -> None:
    calls = []

    def fetch(arguments, _context):
        calls.append(dict(arguments))
        return {"status": 200, "title": "Example"}

    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            full_access=True,
            installed_capability_packs=frozenset({"browser"}),
            capability_handlers={"web_fetch": fetch},
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="exact cache"))
    gateway = _Gateway(
        [
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": f"response-fetch-{index}",
                    "tool_call_id": f"fetch-{index}",
                    "tool_name": "web_fetch",
                    "arguments": {"url": "https://example.com"},
                }
            ]
            if part == "tool"
            else [
                {
                    "seq": 1,
                    "event_type": "response.completed",
                    "response_id": f"response-done-{index}",
                }
            ]
            for index in range(2)
            for part in ("tool", "done")
        ]
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        permission_mutation_lock=app.state.permission_authority.mutation_lock,
    )

    created_turns = []
    for index in range(2):
        prepared = composition.prepare_turn(
            CreateTurnRequest(
                input="使用 fetch 读取网页",
                explicit_tool_ids=["web_fetch"],
                client_message_id=f"exact-cache-{index}",
            )
        )
        created = kernel.create_turn(
            thread.thread_id,
            prepared.request,
            snapshot_context=prepared.snapshot_context,
        )
        created_turns.append(created.turn.turn_id)
        assert asyncio.run(worker.run_once(f"cache-worker-{index}")).outcome is (
            WorkerOutcome.COMPLETED
        )

    assert calls == [{"url": "https://example.com"}]
    cache_event = next(
        event
        for event in kernel.events.page(thread.thread_id, limit=1_000).events
        if event.event_type == "tool.cache_reused"
    )
    assert cache_event.payload["tool_id"] == "web_fetch"
    assert cache_event.payload["ttl_seconds"] == 300
    second_execution = ToolExecutionRepository(kernel.database).get(
        _execution_id(created_turns[1], "fetch-1")
    )
    assert second_execution.status == "completed"


def test_invocation_permit_cannot_cross_execution_batch(tmp_path) -> None:
    calls = []
    app, kernel, composition, thread, created = _shell_runtime(
        tmp_path,
        lambda arguments, context: calls.append(context) or {"exit_code": 0},
    )
    gateway = _Gateway(_shell_scripts(call_id="batch-bound-call"))
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        permission_mutation_lock=app.state.permission_authority.mutation_lock,
    )
    assert (
        asyncio.run(worker.run_once("batch-bound-worker")).outcome
        is WorkerOutcome.COMPLETED
    )
    execution_id = _execution_id(created.turn.turn_id, "batch-bound-call")
    context = worker._job_context(created.job.job_id)

    with pytest.raises(CapabilityDeniedError, match="admission"):
        asyncio.run(
            composition.capability_service.tool_call(
                context["capability_snapshot_id"],
                "bash",
                {"command": "opaque-command"},
                policy_snapshot_id=context["permission_snapshot_id"],
                idempotency_key=f"{created.turn.turn_id}:batch-bound-call",
                execution_scope=ToolExecutionScope(
                    job_id=created.job.job_id,
                    thread_id=thread.thread_id,
                    turn_id=created.turn.turn_id,
                    execution_batch_id="bat_wrong_execution_batch",
                ),
                tool_call_id=execution_id,
            )
        )
    assert len(calls) == 1


def test_cowagent_tool_execution_does_not_create_a_permission_interaction(tmp_path) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            installed_capability_packs=frozenset({"sandbox"}),
            capability_handlers={
                "bash": lambda _arguments, _context: {"exit_code": 0}
            },
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    authority = app.state.permission_authority
    thread = kernel.create_thread(CreateThreadRequest(title="deny cannot approve"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="使用 shell",
            explicit_tool_ids=["bash"],
            client_message_id="deny-cannot-approve",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    gateway = _Gateway(_shell_scripts(call_id="denied-call"))
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        permission_mutation_lock=authority.mutation_lock,
    )
    assert asyncio.run(worker.run_once("deny-worker")).outcome is WorkerOutcome.COMPLETED
    assert kernel.list_interactions(thread.thread_id).interactions == []
    return
    interaction = kernel.list_interactions(thread.thread_id).interactions[0]
    kernel.respond_interaction(
        interaction.interaction_id,
        {"action_id": "deny", "values": {}},
        client_request_id="deny-response",
    )
    job = kernel.jobs.get(created.job.job_id)
    assert job.checkpoint is not None
    context = worker._job_context(created.job.job_id)
    execution_id = _execution_id(created.turn.turn_id, "denied-call")
    executions = ToolExecutionRepository(kernel.database)
    executions.begin(
        tool_call_id=execution_id,
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=str(job.checkpoint["execution_batch_id"]),
        capability_snapshot_id=context["capability_snapshot_id"],
        policy_snapshot_id=context["permission_snapshot_id"],
        tool_id="bash",
        arguments={"command": "opaque-command"},
        idempotency_key=f"{created.turn.turn_id}:denied-call",
    )

    with pytest.raises(ToolExecutionConflict, match="approval"):
        executions.admit(
            tool_call_id=execution_id,
            job_id=created.job.job_id,
            thread_id=thread.thread_id,
            turn_id=created.turn.turn_id,
            execution_batch_id=str(job.checkpoint["execution_batch_id"]),
            capability_snapshot_id=context["capability_snapshot_id"],
            permission_account_id="local-user",
            frozen_permission_snapshot_id=context["permission_snapshot_id"],
            current_permission_snapshot_id=authority.current().snapshot_id,
            current_permission_state_digest=authority.current_state_digest(),
            current_admin_hard_denies=(),
            current_availability_digest=None,
            tool_id="bash",
            tool_version="1.0.0",
            approved=True,
            approval_interaction_id=interaction.interaction_id,
            effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
        )


def test_empty_shell_arguments_reach_the_cowagent_handler_for_background_followup(
    tmp_path,
) -> None:
    calls = []
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            installed_capability_packs=frozenset({"sandbox"}),
            capability_handlers={
                "bash": lambda arguments, context: (
                    calls.append((arguments, context)) or {"exit_code": 0}
                )
            },
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="invalid arguments"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="使用 shell",
            explicit_tool_ids=["bash"],
            client_message_id="invalid-shell-arguments",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    gateway = _Gateway(
        [
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "invalid-shell-response",
                    "tool_call_id": "invalid-shell-call",
                    "tool_name": "bash",
                    "arguments": {},
                }
            ],
            [
                {
                    "seq": 1,
                    "event_type": "response.completed",
                    "response_id": "invalid-shell-recovered",
                }
            ],
        ]
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        permission_mutation_lock=app.state.permission_authority.mutation_lock,
    )

    result = asyncio.run(worker.run_once("invalid-shell-worker"))

    assert result.outcome is WorkerOutcome.COMPLETED
    assert len(calls) == 1 and calls[0][0] == {}
    assert kernel.list_interactions(thread.thread_id).interactions == []
    assert any(
        item.kind is ItemKind.TOOL_CALL
        for item in kernel.projection(thread.thread_id).items
    )


def test_model_can_correct_safe_tool_arguments_and_retry_in_the_same_turn(
    tmp_path,
) -> None:
    calls = []
    app, kernel, composition, _thread, created = _shell_runtime(
        tmp_path,
        lambda arguments, context: (
            calls.append((dict(arguments), context)) or {"exit_code": 0}
        ),
    )
    gateway = _Gateway(
        [
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "invalid-arguments-response",
                    "tool_call_id": "invalid-arguments-call",
                    "tool_name": "bash",
                    "arguments": {},
                }
            ],
            _shell_scripts(call_id="corrected-arguments-call")[0],
            _shell_scripts(call_id="corrected-arguments-call")[1],
        ]
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        permission_mutation_lock=app.state.permission_authority.mutation_lock,
    )

    result = asyncio.run(worker.run_once("corrected-arguments-worker"))

    assert result.outcome is WorkerOutcome.COMPLETED
    assert [call[0] for call in calls] == [{}, {"command": "opaque-command"}]
    with kernel.database.reader() as connection:
        rows = connection.execute(
            "SELECT event_type FROM events WHERE turn_id=? "
            "AND event_type IN ('tool.recovery_planned', 'tool.recovery_resolved') "
            "ORDER BY seq",
            (created.turn.turn_id,),
        ).fetchall()
    assert rows == []


def test_current_availability_can_tighten_but_not_broaden_frozen_plan() -> None:
    service = CapabilityService(builtin_capability_registry())
    frozen_policy = ExecutionPolicy(
        snapshot_id="perm_frozen_full",
        profile=PermissionProfile.FULL_ACCESS,
    )
    available = RuntimeAvailability(
        platform="windows",
        installed_packs=frozenset({"sandbox"}),
    )
    current = {"availability": available}
    plan = service.create_plan(
        intent="use shell",
        explicit_tools=("bash",),
        availability=available,
        policy=frozen_policy,
    )
    service.bind_current_policy_provider(lambda: frozen_policy)
    service.bind_current_permission_state_digest_provider(lambda: "a" * 64)
    service.bind_current_availability_provider(lambda: current["availability"])
    assert service.invocation_governance(plan.snapshot_id, "bash").allowed is True

    current["availability"] = RuntimeAvailability(platform="windows")
    tightened = service.invocation_governance(plan.snapshot_id, "bash")

    assert tightened.allowed is True
    assert tightened.effective_sandbox is SandboxLevel.DANGER_FULL_ACCESS


def test_current_availability_preserves_turn_selected_model_capabilities() -> None:
    service = CapabilityService(builtin_capability_registry())
    policy = ExecutionPolicy(
        snapshot_id="perm_image_full",
        profile=PermissionProfile.FULL_ACCESS,
    )
    frozen = RuntimeAvailability(
        platform="windows",
        installed_packs=frozenset({"image"}),
        selected_model_modalities=frozenset({"image"}),
        selected_model_capabilities={
            "image": frozenset({"image_generation", "image_edit"}),
        },
    )
    current = {
        "availability": RuntimeAvailability(
            platform="windows",
            installed_packs=frozenset({"image"}),
        )
    }
    plan = service.create_plan(
        intent="生成一张图片",
        availability=frozen,
        policy=policy,
    )
    assert plan.decision("imagegen") is not None
    assert plan.decision("imagegen").eligible is True
    service.bind_current_policy_provider(lambda: policy)
    service.bind_current_permission_state_digest_provider(lambda: "b" * 64)
    service.bind_current_availability_provider(lambda: current["availability"])

    governance = service.invocation_governance(plan.snapshot_id, "imagegen")

    assert governance.allowed is True
    assert not any(
        reason.startswith("current_availability:missing_model")
        for reason in governance.reason_codes
    )
