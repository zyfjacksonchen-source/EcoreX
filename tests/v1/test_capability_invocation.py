from __future__ import annotations

import asyncio

import pytest

from ecorex.capabilities import (
    ApprovalRequirement,
    ApprovalRequiredError,
    CapabilityEffect,
    CapabilityDeniedError,
    CapabilityRegistry,
    CapabilityService,
    ExecutionPolicy,
    Exposure,
    IdempotencyClass,
    IdempotencyKeyRequiredError,
    PermissionProfile,
    RuntimeAvailability,
    SandboxLevel,
    StaleCapabilitySnapshotError,
    ToolSpec,
    UnknownCapabilityError,
)


def _specs() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            tool_id="read",
            version="1.0.0",
            display_name="Read",
            description="Read a document",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            default_exposure=Exposure.DIRECT,
        ),
        ToolSpec(
            tool_id="write-doc",
            version="1.0.0",
            display_name="Write document",
            description="Write to an external document",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            effects=frozenset({CapabilityEffect.WRITE, CapabilityEffect.NETWORK}),
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            required_sandbox=SandboxLevel.WORKSPACE_WRITE,
            approval_requirement=ApprovalRequirement.ON_REQUEST,
        ),
    )


def test_tool_call_uses_immutable_policy_snapshot_and_redacted_audit() -> None:
    audit = []
    service = CapabilityService(
        CapabilityRegistry(_specs()),
        handlers={
            "read": lambda args: {"title": args["title"]},
            "write-doc": lambda args, context: {
                "written": args["title"],
                "idempotency_key": context.idempotency_key,
            },
        },
        audit_sink=audit.append,
    )
    policy = ExecutionPolicy(snapshot_id="perm_1")
    plan = service.create_plan(
        intent="read",
        availability=RuntimeAvailability(platform="windows"),
        policy=policy,
    )

    result = asyncio.run(
        service.tool_call(
            plan.snapshot_id,
            "read",
            {"title": "Quarterly plan", "secret": "not-in-audit"},
            policy_snapshot_id="perm_1",
        )
    )

    assert result.value == {"title": "Quarterly plan"}
    assert result.record.tool_id == "read"
    assert result.record.arguments_sha256
    assert "secret" not in repr(result.record)
    assert audit == [result.record]
    with pytest.raises(StaleCapabilitySnapshotError):
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "read",
                {},
                policy_snapshot_id="perm_2",
            )
        )


def test_write_tool_requires_approval_and_idempotency_key() -> None:
    service = CapabilityService(
        CapabilityRegistry(_specs()),
        handlers={"write-doc": lambda args, context: {
            "title": args["title"],
            "idempotency_key": context.idempotency_key,
        }},
    )
    plan = service.create_plan(
        intent="publish document",
        explicit_tools=("write-doc",),
        availability=RuntimeAvailability(platform="windows"),
        policy=ExecutionPolicy(snapshot_id="perm_default"),
    )

    with pytest.raises(ApprovalRequiredError):
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "write-doc",
                {"title": "Plan"},
                policy_snapshot_id="perm_default",
            )
        )
    with pytest.raises(IdempotencyKeyRequiredError):
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "write-doc",
                {"title": "Plan"},
                policy_snapshot_id="perm_default",
                approved=True,
            )
        )
    result = asyncio.run(
        service.tool_call(
            plan.snapshot_id,
            "write-doc",
            {"title": "Plan"},
            policy_snapshot_id="perm_default",
            approved=True,
            idempotency_key="turn_1:tool_1",
        )
    )
    assert result.value == {"title": "Plan", "idempotency_key": "turn_1:tool_1"}

    full_plan = service.create_plan(
        intent="publish document",
        explicit_tools=("write-doc",),
        availability=RuntimeAvailability(platform="windows"),
        policy=ExecutionPolicy(
            snapshot_id="perm_full",
            profile=PermissionProfile.FULL_ACCESS,
        ),
    )
    full_result = asyncio.run(
        service.tool_call(
            full_plan.snapshot_id,
            "write-doc",
            {"title": "Full"},
            policy_snapshot_id="perm_full",
            idempotency_key="turn_2:tool_1",
        )
    )
    assert full_result.value == {"title": "Full", "idempotency_key": "turn_2:tool_1"}
    assert full_result.record.approved is False


def test_unknown_tool_call_fails_closed() -> None:
    service = CapabilityService(CapabilityRegistry(_specs()))
    plan = service.create_plan(
        intent="do something",
        explicit_tools=("made-up-tool",),
        availability=RuntimeAvailability(platform="windows"),
        policy=ExecutionPolicy(snapshot_id="perm"),
    )
    assert plan.unresolved_explicit == ("made-up-tool",)
    with pytest.raises(UnknownCapabilityError):
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "made-up-tool",
                {},
                policy_snapshot_id="perm",
            )
        )


def test_deferred_tool_requires_a_runtime_disclosure_grant() -> None:
    calls = []
    service = CapabilityService(
        CapabilityRegistry(_specs()),
        handlers={
            "write-doc": lambda args, context: calls.append(args["title"])
            or {"title": args["title"]}
        },
    )
    plan = service.create_plan(
        intent="prepare a report",
        availability=RuntimeAvailability(platform="windows"),
        policy=ExecutionPolicy(
            snapshot_id="perm_full_deferred",
            profile=PermissionProfile.FULL_ACCESS,
        ),
    )
    decision = plan.decision("write-doc")
    assert decision is not None and decision.exposure is Exposure.DEFERRED

    with pytest.raises(CapabilityDeniedError, match="not been disclosed"):
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "write-doc",
                {"title": "Hidden"},
                policy_snapshot_id="perm_full_deferred",
                idempotency_key="turn_hidden:tool_1",
            )
        )
    assert calls == []

    # The legacy/model-facing boolean is only a compatibility hint.  It cannot
    # manufacture authority without a Runtime execution scope and a matching
    # durable tool_describe fact.
    with pytest.raises(CapabilityDeniedError, match="not been disclosed"):
        asyncio.run(
            service.tool_call(
                plan.snapshot_id,
                "write-doc",
                {"title": "Forged"},
                policy_snapshot_id="perm_full_deferred",
                idempotency_key="turn_forged:tool_1",
                disclosure_granted=True,
            )
        )
    assert calls == []
