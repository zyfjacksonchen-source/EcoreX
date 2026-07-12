from __future__ import annotations

import asyncio

import pytest

from ecorex.capabilities import (
    CapabilityDeniedError,
    Exposure,
    SandboxLevel,
    ToolExecutionScope,
    ToolInvocationContext,
)
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest, SteerTurnRequest
from ecorex.runtime import RuntimeSettings, ToolExecutionConflict, create_app


def _runtime(tmp_path):
    calls: list[dict[str, object]] = []

    def vision(arguments):
        calls.append(dict(arguments))
        return {"summary": "checked"}

    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            installed_capability_packs=frozenset({"image"}),
            capability_handlers={"vision": vision},
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="disclosure"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="检查附件",
            client_message_id="disclosure-message",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    batch = kernel.turn_execution_batches.create(
        turn_id=created.turn.turn_id,
        first_revision_ordinal=0,
        last_revision_ordinal=0,
        snapshot_context=prepared.snapshot_context,
    )
    return app, kernel, composition, thread, created, prepared, batch, calls


def _context(created, thread, prepared, batch, *, tool_id: str):
    return ToolInvocationContext(
        invocation_id=f"invoke-{tool_id}",
        capability_snapshot_id=prepared.snapshot_context.capability_snapshot_id,
        policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
        tool_id=tool_id,
        idempotency_key=None,
        approved=False,
        effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
        execution_scope=ToolExecutionScope(
            job_id=created.job.job_id,
            thread_id=thread.thread_id,
            turn_id=created.turn.turn_id,
            execution_batch_id=batch.batch_id,
        ),
    )


def _complete_search(composition, created, thread, prepared, batch):
    repository = composition.tool_execution_repository
    context = _context(created, thread, prepared, batch, tool_id="tool_search")
    repository.begin(
        tool_call_id="search_vision_exact",
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
        capability_snapshot_id=context.capability_snapshot_id,
        policy_snapshot_id=context.policy_snapshot_id,
        tool_id="tool_search",
        arguments={"query": "inspect-image", "limit": 5},
        idempotency_key=None,
    )
    result = composition._tool_search(
        {"query": "inspect-image", "limit": 5},
        context,
    )
    repository.complete("search_vision_exact", result)
    return repository.get("search_vision_exact")


def test_forged_execution_scope_cannot_authorize_deferred_tool(tmp_path) -> None:
    app, _kernel, composition, thread, created, prepared, batch, calls = _runtime(tmp_path)
    del app
    service = composition.capability_service
    capability_snapshot_id = prepared.snapshot_context.capability_snapshot_id
    policy_snapshot_id = prepared.snapshot_context.permission_snapshot_id
    decision = service.get_plan(capability_snapshot_id).decision("vision")
    assert decision is not None and decision.exposure is Exposure.DEFERRED

    with pytest.raises(CapabilityDeniedError, match="not been disclosed"):
        asyncio.run(
            service.tool_call(
                capability_snapshot_id,
                "vision",
                {"artifact_ids": ["art_1"], "instruction": "检查"},
                policy_snapshot_id=policy_snapshot_id,
                execution_scope=ToolExecutionScope(
                    job_id=created.job.job_id,
                    thread_id=f"{thread.thread_id}-forged",
                    turn_id=created.turn.turn_id,
                    execution_batch_id=batch.batch_id,
                ),
                disclosure_granted=True,
            )
        )

    assert calls == []


def test_completed_exact_describe_authorizes_same_scope_after_restart_safe_lookup(
    tmp_path,
) -> None:
    app, _kernel, composition, thread, created, prepared, batch, calls = _runtime(tmp_path)
    del app
    service = composition.capability_service
    capability_snapshot_id = prepared.snapshot_context.capability_snapshot_id
    policy_snapshot_id = prepared.snapshot_context.permission_snapshot_id
    repository = composition.tool_execution_repository
    search = _complete_search(composition, created, thread, prepared, batch)
    describe_context = _context(
        created,
        thread,
        prepared,
        batch,
        tool_id="tool_describe",
    )
    description_result = composition._tool_describe(
        {"discovery_id": "tool:vision@1.0.0"},
        describe_context,
    )
    _record, was_created = repository.begin(
        tool_call_id="describe_vision_exact",
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
        capability_snapshot_id=capability_snapshot_id,
        policy_snapshot_id=policy_snapshot_id,
        tool_id="tool_describe",
        arguments={"discovery_id": "tool:vision@1.0.0"},
        idempotency_key=None,
    )
    assert was_created is True
    repository.complete(
        "describe_vision_exact",
        description_result,
    )
    assert description_result["search_tool_call_id"] == search.tool_call_id

    # Recompose the product against the same database.  Authorization must be
    # reconstructed from the durable fact, not an in-memory disclosure set.
    del repository, service, composition
    restarted_app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            installed_capability_packs=frozenset({"image"}),
            capability_handlers={
                "vision": lambda arguments: calls.append(dict(arguments))
                or {"summary": "checked"}
            },
        )
    )
    service = restarted_app.state.runtime_composition.capability_service

    result = asyncio.run(
        service.tool_call(
            capability_snapshot_id,
            "vision",
            {"artifact_ids": ["art_1"], "instruction": "检查"},
            policy_snapshot_id=policy_snapshot_id,
            execution_scope=ToolExecutionScope(
                job_id=created.job.job_id,
                thread_id=thread.thread_id,
                turn_id=created.turn.turn_id,
                execution_batch_id=batch.batch_id,
            ),
        )
    )

    assert result.value == {"summary": "checked"}
    assert result.record.disclosure_granted is True
    assert calls == [{"artifact_ids": ["art_1"], "instruction": "检查"}]


def test_describe_fact_is_bound_to_policy_snapshot_and_tool_version(tmp_path) -> None:
    app, _kernel, composition, thread, created, prepared, batch, calls = _runtime(tmp_path)
    del app
    service = composition.capability_service
    capability_snapshot_id = prepared.snapshot_context.capability_snapshot_id
    policy_snapshot_id = prepared.snapshot_context.permission_snapshot_id
    description = service.tool_describe(capability_snapshot_id, "vision")
    repository = composition.tool_execution_repository
    search = _complete_search(composition, created, thread, prepared, batch)
    repository.begin(
        tool_call_id="describe_vision_forged_version",
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
        capability_snapshot_id=capability_snapshot_id,
        policy_snapshot_id=policy_snapshot_id,
        tool_id="tool_describe",
        arguments={"discovery_id": "tool:vision@9.9.9"},
        idempotency_key=None,
    )
    repository.complete(
        "describe_vision_forged_version",
        {
            "schema_version": 1,
            "capability_snapshot_id": capability_snapshot_id,
            "found": True,
            "available": True,
            "discovery_id": "tool:vision@9.9.9",
            "search_tool_call_id": search.tool_call_id,
            "search_result_sha256": search.result_sha256,
            "tool": description,
        },
    )

    with pytest.raises(CapabilityDeniedError, match="not been disclosed"):
        asyncio.run(
            service.tool_call(
                capability_snapshot_id,
                "vision",
                {"artifact_ids": ["art_1"], "instruction": "检查"},
                policy_snapshot_id=policy_snapshot_id,
                execution_scope=ToolExecutionScope(
                    job_id=created.job.job_id,
                    thread_id=thread.thread_id,
                    turn_id=created.turn.turn_id,
                    execution_batch_id=batch.batch_id,
                ),
            )
        )

    assert calls == []


@pytest.mark.parametrize("reference", ("vision", "inspect-image", "tool:vision@9.9.9"))
def test_model_describe_rejects_bare_alias_guessed_and_stale_references(
    tmp_path,
    reference: str,
) -> None:
    app, _kernel, composition, thread, created, prepared, batch, calls = _runtime(tmp_path)
    del app
    _complete_search(composition, created, thread, prepared, batch)

    # Internal Runtime/UI code retains canonical lookup without granting the
    # model a bypass around Search -> exact discovery ID -> Describe.
    assert composition.capability_service.tool_describe(
        prepared.snapshot_context.capability_snapshot_id,
        "vision",
    )["spec"]["tool_id"] == "vision"
    result = composition._tool_describe(
        {"discovery_id": reference},
        _context(created, thread, prepared, batch, tool_id="tool_describe"),
    )

    assert result["found"] is False
    assert result["reason"] in {"invalid_discovery_id", "search_result_required"}
    assert calls == []


def test_forged_search_result_is_recomputed_and_cannot_grant(tmp_path) -> None:
    app, _kernel, composition, thread, created, prepared, batch, calls = _runtime(tmp_path)
    del app
    repository = composition.tool_execution_repository
    context = _context(created, thread, prepared, batch, tool_id="tool_search")
    forged_arguments = {"query": "zzzz-no-reviewed-match-123", "limit": 5}
    repository.begin(
        tool_call_id="search_forged_result",
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
        capability_snapshot_id=context.capability_snapshot_id,
        policy_snapshot_id=context.policy_snapshot_id,
        tool_id="tool_search",
        arguments=forged_arguments,
        idempotency_key=None,
    )
    valid_result = composition._tool_search(
        {"query": "inspect-image", "limit": 5},
        context,
    )
    repository.complete(
        "search_forged_result",
        {**valid_result, "query": forged_arguments["query"]},
    )

    describe_context = _context(
        created,
        thread,
        prepared,
        batch,
        tool_id="tool_describe",
    )
    result = composition._tool_describe(
        {"discovery_id": "tool:vision@1.0.0"},
        describe_context,
    )
    assert result["found"] is False
    assert result["reason"] == "search_result_invalid"
    assert calls == []


def test_completed_search_and_describe_cannot_cross_execution_batch(tmp_path) -> None:
    app, kernel, composition, thread, created, prepared, batch, calls = _runtime(tmp_path)
    del app
    repository = composition.tool_execution_repository
    search = _complete_search(composition, created, thread, prepared, batch)
    describe_result = composition._tool_describe(
        {"discovery_id": "tool:vision@1.0.0"},
        _context(created, thread, prepared, batch, tool_id="tool_describe"),
    )
    repository.begin(
        tool_call_id="describe_batch_one",
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
        capability_snapshot_id=prepared.snapshot_context.capability_snapshot_id,
        policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
        tool_id="tool_describe",
        arguments={"discovery_id": "tool:vision@1.0.0"},
        idempotency_key=None,
    )
    repository.complete("describe_batch_one", describe_result)
    assert describe_result["search_tool_call_id"] == search.tool_call_id

    kernel.steer_turn(
        created.turn.turn_id,
        SteerTurnRequest(
            input="补充一条新批次输入",
            client_message_id="disclosure-second-batch",
        ),
    )
    second_batch = kernel.turn_execution_batches.create(
        turn_id=created.turn.turn_id,
        first_revision_ordinal=1,
        last_revision_ordinal=1,
        snapshot_context=prepared.snapshot_context,
    )
    assert repository.completed_for_job(
        created.job.job_id,
        execution_batch_id=second_batch.batch_id,
        tool_ids=("tool_search", "tool_describe"),
    ) == ()
    with pytest.raises(ToolExecutionConflict, match="different execution identity"):
        repository.begin(
            tool_call_id=search.tool_call_id,
            job_id=created.job.job_id,
            turn_id=created.turn.turn_id,
            execution_batch_id=second_batch.batch_id,
            capability_snapshot_id=prepared.snapshot_context.capability_snapshot_id,
            policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
            tool_id="tool_search",
            arguments={"query": "inspect-image", "limit": 5},
            idempotency_key=None,
        )
    with pytest.raises(CapabilityDeniedError, match="not been disclosed"):
        asyncio.run(
            composition.capability_service.tool_call(
                prepared.snapshot_context.capability_snapshot_id,
                "vision",
                {"artifact_ids": ["art_1"], "instruction": "检查"},
                policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
                execution_scope=ToolExecutionScope(
                    job_id=created.job.job_id,
                    thread_id=thread.thread_id,
                    turn_id=created.turn.turn_id,
                    execution_batch_id=second_batch.batch_id,
                ),
            )
        )

    assert calls == []
