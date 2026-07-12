from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import pytest

from ecorex.capabilities import SandboxLevel, ToolExecutionScope, ToolInvocationContext
from ecorex.capabilities.errors import CapabilityDeniedError, ToolArgumentsValidationError
from ecorex.connectors import (
    ConnectorInvocationUncertain,
    ConnectorReconciliationPending,
    ConnectorUnavailable,
    InMemoryCredentialVault,
)
from ecorex.connectors import (
    ConnectorActionSpec,
    ConnectorAuthKind,
    ConnectorDefinition,
    ConnectorEffect,
    ConnectorRegistry,
    ConnectorService,
    ConnectorTier,
    SQLiteConnectorRepository,
)
from ecorex.integration.connector_results import (
    ARTIFACT_ENVELOPE_LIMIT_BYTES,
    INLINE_ENVELOPE_LIMIT_BYTES,
)
from ecorex.replay import ReplayService
from tests.v1.test_connector_agent_progressive_disclosure import (
    _ConnectorAdapter,
    _connect,
    _context,
    _describe,
    _runtime,
    _search,
    _turn,
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _recovery_tool_items(kernel: Any, thread_id: str):
    recovery_item_ids = {
        event.item_id
        for event in kernel.events.page(thread_id, limit=1000).events
        if event.event_type
        in {"connector.result.completed", "connector.result_unavailable"}
        and event.payload.get("recovery_delivery") is True
        and event.item_id is not None
    }
    return [
        item
        for item in kernel.projection(thread_id).items
        if item.kind.value == "tool_call"
        and item.item_id in recovery_item_ids
    ]


def _recovery_result_event(kernel: Any, thread_id: str, item_id: str):
    return next(
        event
        for event in kernel.events.page(thread_id, limit=1000).events
        if event.event_type
        in {"connector.result.completed", "connector.result_unavailable"}
        and event.item_id == item_id
    )


class _ResultAdapter(_ConnectorAdapter):
    def __init__(self, *, content_size: int = 0, leak_secret: bool = False) -> None:
        super().__init__(
            "feishu",
            frozenset({"docx:document:readonly", "docx:document"}),
        )
        self.content_size = content_size
        self.leak_secret = leak_secret

    async def invoke(
        self,
        *,
        action_id: str,
        inputs: Mapping[str, Any],
        credentials: Mapping[str, str],
        idempotency_key: str | None,
    ) -> Any:
        self.invocations.append((action_id, dict(inputs), idempotency_key))
        if self.leak_secret:
            return {
                "ok": True,
                "action_id": action_id,
                "title": credentials["access_token"],
            }
        return {
            "ok": True,
            "action_id": action_id,
            "document_id": str(inputs["document_id"]),
            "content": "数" * self.content_size,
        }


class _RootResultAdapter(_ConnectorAdapter):
    def __init__(self, result: Any) -> None:
        super().__init__("root-test", frozenset())
        self.result = result

    async def invoke(self, **kwargs: Any) -> Any:
        self.invocations.append(
            (
                str(kwargs["action_id"]),
                dict(kwargs["inputs"]),
                kwargs.get("idempotency_key"),
            )
        )
        return self.result


class _LateWriteAdapter(_ResultAdapter):
    async def invoke(
        self,
        *,
        action_id: str,
        inputs: Mapping[str, Any],
        credentials: Mapping[str, str],
        idempotency_key: str | None,
    ) -> Any:
        del credentials
        self.invocations.append((action_id, dict(inputs), idempotency_key))
        await asyncio.sleep(0.15)
        return {
            "ok": True,
            "action_id": action_id,
            "document_id": str(inputs["document_id"]),
            "content": "迟" * self.content_size,
        }


def _root_result_service(
    tmp_path: Path,
    *,
    result: Any,
    output_schema: Mapping[str, Any],
):
    adapter = _RootResultAdapter(result)
    registry = ConnectorRegistry()
    registry.register(
        ConnectorDefinition(
            connector_id="root-test",
            contract_version="1.0",
            display_name="Root result",
            description="Tests legal JSON root results",
            tier=ConnectorTier.STABLE,
            auth_kinds=(ConnectorAuthKind.OAUTH2,),
            config_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            actions=(
                ConnectorActionSpec(
                    action_id="values.read",
                    display_name="Read value",
                    description="Returns one legal JSON root",
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    output_schema=output_schema,
                    effects=frozenset({ConnectorEffect.READ}),
                ),
            ),
        ),
        adapter,
    )
    registry.seal()
    service = ConnectorService(
        registry,
        allowed_return_uris=frozenset(
            {"http://127.0.0.1:8765/api/v1/connectors/oauth/callback"}
        ),
        vault=InMemoryCredentialVault(),
        repository=SQLiteConnectorRepository(tmp_path / "root-runtime.db"),
    )
    instance = _connect(service, "root-test")
    return service, instance, adapter


def _disclosed_read(app, adapter: _ResultAdapter, suffix: str = "result"):
    del adapter
    _kernel, composition, thread, created, prepared, batch = _turn(
        app,
        "使用飞书读取文档",
        suffix,
    )
    search = _search(
        composition,
        thread,
        created,
        prepared,
        batch,
        "读取飞书文档",
    )
    candidate = next(
        item for item in search["actions"] if item["action_id"] == "documents.read"
    )
    _describe(
        composition,
        thread,
        created,
        prepared,
        batch,
        candidate["discovery_id"],
    )
    context = _context(
        thread,
        created,
        prepared,
        batch,
        tool_id="connector_read",
        call_id=f"connector-read-{suffix}",
    )
    arguments = {
        "discovery_id": candidate["discovery_id"],
        "input": {"document_id": f"doc-{suffix}"},
    }
    return composition, thread, created, context, arguments


def _disclosed_write(app, suffix: str = "late"):
    _kernel, composition, thread, created, prepared, batch = _turn(
        app,
        "使用飞书编辑文档",
        suffix,
    )
    search = _search(
        composition,
        thread,
        created,
        prepared,
        batch,
        "编辑飞书文档",
    )
    candidate = next(
        item for item in search["actions"] if item["action_id"] == "documents.write"
    )
    _describe(
        composition,
        thread,
        created,
        prepared,
        batch,
        candidate["discovery_id"],
    )
    context = _context(
        thread,
        created,
        prepared,
        batch,
        tool_id="connector_write",
        call_id=f"connector-write-{suffix}",
    )
    return composition, context, {
        "discovery_id": candidate["discovery_id"],
        "input": {"document_id": f"doc-{suffix}", "title": "正式方案"},
    }


def test_inline_result_uses_exact_envelope_and_replays_without_provider(
    tmp_path: Path,
) -> None:
    adapter = _ResultAdapter(content_size=128)
    app, service = _runtime(tmp_path, feishu=adapter)
    _connect(service, "feishu")
    composition, _thread, _created, context, arguments = _disclosed_read(
        app, adapter, "inline"
    )

    first = asyncio.run(composition.connector_agent_runtime.read(arguments, context))
    replay = asyncio.run(composition.connector_agent_runtime.read(arguments, context))

    assert first == replay
    assert first["schema_version"] == 1
    assert first["status"] == "completed"
    assert first["delivery"] == "inline"
    assert first["data"]["document_id"] == "doc-inline"
    raw = _canonical(first["data"])
    assert first["size_bytes"] == len(raw)
    assert first["result_sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(_canonical(first)) <= INLINE_ENVELOPE_LIMIT_BYTES
    assert len(adapter.invocations) == 1
    assert adapter.invocations[0][2]

    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        row = connection.execute(
            "SELECT status, delivery_hint, inline_data_json, result_json "
            "FROM connector_result_staging"
        ).fetchone()
    assert row is not None
    assert row[0:2] == ("finalized", "inline")
    assert json.loads(row[2]) == first["data"]
    assert json.loads(row[3]) == first
    assert _recovery_tool_items(app.state.runtime, _thread.thread_id) == []


def test_inline_startup_recovery_delivers_item_before_later_interrupt(
    tmp_path: Path,
) -> None:
    vault = InMemoryCredentialVault()
    adapter = _ResultAdapter(content_size=32)
    app, service = _runtime(tmp_path, feishu=adapter, vault=vault)
    _connect(service, "feishu")
    composition, thread, _created, context, arguments = _disclosed_read(
        app, adapter, "inline-recovery"
    )

    def crash(point: str, _invocation_id: str) -> None:
        if point == "before_finalize_commit":
            raise RuntimeError("simulated inline publication loss")

    app.state.connector_result_coordinator.fault_hook = crash
    with pytest.raises(ConnectorUnavailable):
        asyncio.run(composition.connector_agent_runtime.read(arguments, context))
    assert len(adapter.invocations) == 1

    restarted, _service = _runtime(
        tmp_path,
        feishu=adapter,
        vault=vault,
    )
    assert restarted.state.connector_result_recovery == {
        "completed": 1,
        "deferred": 0,
    }
    recovery_items = _recovery_tool_items(
        restarted.state.runtime, thread.thread_id
    )
    assert len(recovery_items) == 1
    recovery_item = recovery_items[0]
    assert recovery_item.status.value == "completed"
    assert recovery_item.content["tool_name"] == "connector_read"
    assert recovery_item.content["result_summary"] == (
        "连接器结果已恢复并可继续使用"
    )
    recovery_event = _recovery_result_event(
        restarted.state.runtime, thread.thread_id, recovery_item.item_id
    )
    assert recovery_event.payload["delivery"] == "inline"
    assert recovery_event.payload["recovered_after_terminal"] is False

    # Recovery delivery must not depend only on observing a terminal Turn at
    # finalize time. The result remains visible if interruption follows it.
    restarted.state.runtime.interrupt_turn(
        context.execution_scope.turn_id,
        reason="interrupt after local recovery committed",
    )
    replay = asyncio.run(
        restarted.state.runtime_composition.connector_agent_runtime.read(
            arguments,
            context,
        )
    )
    assert recovery_item.content["result_sha256"] == hashlib.sha256(
        _canonical(replay)
    ).hexdigest()
    assert len(adapter.invocations) == 1
    assert len(_recovery_tool_items(restarted.state.runtime, thread.thread_id)) == 1
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        event = connection.execute(
            "SELECT item_id, payload_json FROM events "
            "WHERE event_type='connector.result.completed'"
        ).fetchone()
    assert event is not None and event[0] == recovery_item.item_id
    assert json.loads(event[1])["recovery_delivery"] is True
    restarted.state.runtime.invariants.audit().raise_if_invalid()
    replay_projection = ReplayService(restarted.state.runtime).mock_replay(
        thread.thread_id
    ).projection
    replayed_item = next(
        item for item in replay_projection.items if item.item_id == recovery_item.item_id
    )
    assert replayed_item.status.value == "completed"
    assert replayed_item.content == recovery_item.content


def test_unavailable_terminal_recovery_delivers_secret_free_receipt_item(
    tmp_path: Path,
) -> None:
    vault = InMemoryCredentialVault()
    adapter = _ResultAdapter(leak_secret=True)
    app, service = _runtime(tmp_path, feishu=adapter, vault=vault)
    _connect(service, "feishu")
    composition, thread, _created, context, arguments = _disclosed_read(
        app, adapter, "unavailable-recovery"
    )

    def crash(point: str, _invocation_id: str) -> None:
        if point == "before_finalize_commit":
            raise RuntimeError("simulated unavailable publication loss")

    app.state.connector_result_coordinator.fault_hook = crash
    with pytest.raises(ConnectorUnavailable):
        asyncio.run(composition.connector_agent_runtime.read(arguments, context))
    app.state.runtime.interrupt_turn(
        context.execution_scope.turn_id,
        reason="terminal before unavailable receipt recovery",
    )

    restarted, _service = _runtime(
        tmp_path,
        feishu=adapter,
        vault=vault,
    )
    recovery_items = _recovery_tool_items(
        restarted.state.runtime, thread.thread_id
    )
    assert len(recovery_items) == 1
    recovery_item = recovery_items[0]
    recovery_event = _recovery_result_event(
        restarted.state.runtime, thread.thread_id, recovery_item.item_id
    )
    assert recovery_event.payload["delivery"] == "result_unavailable"
    assert recovery_event.payload["size_bytes"] == 0
    assert recovery_event.payload["recovered_after_terminal"] is True
    receipt = asyncio.run(
        restarted.state.runtime_composition.connector_agent_runtime.read(
            arguments,
            context,
        )
    )
    assert receipt["delivery"] == "result_unavailable"
    assert receipt["identity_kind"] == "receipt"
    assert receipt["size_bytes"] == 0
    assert recovery_item.content["result_sha256"] == hashlib.sha256(
        _canonical(receipt)
    ).hexdigest()
    assert len(adapter.invocations) == 1
    persisted = (tmp_path / "runtime.db").read_bytes()
    assert b"secret-1" not in persisted
    assert hashlib.sha256(b"secret-1").hexdigest().encode("ascii") not in persisted


@pytest.mark.parametrize(
    ("result", "output_schema"),
    (
        (
            [1, 2, 3],
            {
                "type": "array",
                "items": {"type": "integer"},
                "maxItems": 10,
            },
        ),
        (7, {"type": "integer"}),
        (True, {"type": "boolean"}),
        (None, {"type": "null"}),
    ),
)
def test_connector_action_schema_validates_legal_provider_inner_json_roots(
    tmp_path: Path,
    result: Any,
    output_schema: Mapping[str, Any],
) -> None:
    service, instance, adapter = _root_result_service(
        tmp_path,
        result=result,
        output_schema=output_schema,
    )
    observed = asyncio.run(service.invoke(instance.instance_id, "values.read", {}))
    assert observed == result
    assert len(adapter.invocations) == 1


def test_connector_action_schema_rejects_invalid_provider_inner_array(
    tmp_path: Path,
) -> None:
    service, instance, adapter = _root_result_service(
        tmp_path,
        result=[1, "wrong"],
        output_schema={
            "type": "array",
            "items": {"type": "integer"},
            "maxItems": 10,
        },
    )
    with pytest.raises(ConnectorUnavailable, match="result failed validation"):
        asyncio.run(service.invoke(instance.instance_id, "values.read", {}))
    assert len(adapter.invocations) == 1


@pytest.mark.parametrize(
    ("result", "output_schema"),
    (
        (3, {"type": "integer", "enum": [1, 2]}),
        (11, {"type": "integer", "minimum": 0, "maximum": 10}),
    ),
)
def test_connector_provider_inner_schema_enforces_enum_and_numeric_bounds(
    tmp_path: Path,
    result: Any,
    output_schema: Mapping[str, Any],
) -> None:
    service, instance, adapter = _root_result_service(
        tmp_path,
        result=result,
        output_schema=output_schema,
    )
    with pytest.raises(ConnectorUnavailable, match="result failed validation"):
        asyncio.run(service.invoke(instance.instance_id, "values.read", {}))
    assert len(adapter.invocations) == 1


def test_large_read_crash_rolls_back_then_restart_finalizes_once_without_provider(
    tmp_path: Path,
) -> None:
    vault = InMemoryCredentialVault()
    adapter = _ResultAdapter(content_size=190_000)  # UTF-8 JSON is > 512 KiB.
    app, service = _runtime(tmp_path, feishu=adapter, vault=vault)
    _connect(service, "feishu")
    composition, thread, _created, context, arguments = _disclosed_read(
        app, adapter, "crash"
    )

    def crash(point: str, _invocation_id: str) -> None:
        if point == "before_finalize_commit":
            raise RuntimeError("simulated-process-loss-before-commit")

    app.state.connector_result_coordinator.fault_hook = crash
    with pytest.raises(ConnectorUnavailable):
        asyncio.run(composition.connector_agent_runtime.read(arguments, context))

    assert len(adapter.invocations) == 1
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        stage_before = connection.execute(
            "SELECT invocation_id, status, delivery_hint "
            "FROM connector_result_staging"
        ).fetchone()
        assert stage_before is not None
        assert stage_before[1:] == ("staged", "artifact")
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_entities"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM items WHERE kind='artifact'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE event_type='artifact.connector_result.created'"
        ).fetchone()[0] == 0

    with pytest.raises(ConnectorReconciliationPending):
        app.state.connector_composition.repository.resolve_uncertain_invocation(
            str(stage_before[0]),
            "confirmed_not_executed",
            wait_seconds=0,
        )

    app.state.runtime.interrupt_turn(
        context.execution_scope.turn_id,
        reason="simulate terminal turn before restart recovery",
    )

    restarted, _restarted_service = _runtime(
        tmp_path,
        feishu=adapter,
        vault=vault,
    )
    assert restarted.state.connector_result_recovery == {
        "completed": 1,
        "deferred": 0,
    }
    restarted_runtime = restarted.state.runtime_composition.connector_agent_runtime
    replay = asyncio.run(restarted_runtime.read(arguments, context))
    assert replay["delivery"] == "artifact"
    assert len(_canonical(replay)) <= ARTIFACT_ENVELOPE_LIMIT_BYTES
    assert len(adapter.invocations) == 1

    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        stage = connection.execute(
            "SELECT status, artifact_id, revision_id, result_json "
            "FROM connector_result_staging"
        ).fetchone()
        assert stage is not None and stage[0] == "finalized"
        assert json.loads(stage[3]) == replay
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_entities WHERE thread_id=?",
            (thread.thread_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM items WHERE turn_id=? AND kind='artifact'",
            (context.execution_scope.turn_id,),
        ).fetchone()[0] == 1
        item_content = json.loads(
            connection.execute(
                "SELECT content_json FROM items WHERE turn_id=? AND kind='artifact'",
                (context.execution_scope.turn_id,),
            ).fetchone()[0]
        )
        assert item_content["source"]["recovered_after_terminal"] is True
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE turn_id=? "
            "AND event_type='artifact.connector_result.created'",
            (context.execution_scope.turn_id,),
        ).fetchone()[0] == 1


def test_late_successful_timed_out_write_uses_same_stage_and_exact_replay(
    tmp_path: Path,
) -> None:
    adapter = _LateWriteAdapter(content_size=190_000)
    app, service = _runtime(tmp_path, feishu=adapter, full_access=True)
    _connect(service, "feishu")
    service.adapter_timeout_seconds = 0.05
    composition, context, arguments = _disclosed_write(app)

    async def scenario():
        with pytest.raises(ConnectorInvocationUncertain):
            await composition.connector_agent_runtime.write(arguments, context)
        await asyncio.sleep(0.3)
        return await composition.connector_agent_runtime.write(arguments, context)

    replay = asyncio.run(scenario())
    assert replay["delivery"] == "artifact"
    assert len(adapter.invocations) == 1
    assert adapter.invocations[0][2]
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        assert connection.execute(
            "SELECT status, completion_path FROM connector_result_staging"
        ).fetchone() == ("finalized", "late_provider_result")
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_entities"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM items WHERE kind='artifact'"
        ).fetchone()[0] == 1


def test_late_inline_success_always_creates_recovery_delivery_item(
    tmp_path: Path,
) -> None:
    adapter = _LateWriteAdapter(content_size=16)
    app, service = _runtime(tmp_path, feishu=adapter, full_access=True)
    _connect(service, "feishu")
    service.adapter_timeout_seconds = 0.05
    composition, context, arguments = _disclosed_write(app, "late-inline")

    async def scenario():
        with pytest.raises(ConnectorInvocationUncertain):
            await composition.connector_agent_runtime.write(arguments, context)
        await asyncio.sleep(0.3)
        return await composition.connector_agent_runtime.write(arguments, context)

    replay = asyncio.run(scenario())
    assert replay["delivery"] == "inline"
    recovery_items = _recovery_tool_items(
        app.state.runtime, context.execution_scope.thread_id
    )
    assert len(recovery_items) == 1
    recovery_item = recovery_items[0]
    assert recovery_item.content["result_sha256"] == hashlib.sha256(
        _canonical(replay)
    ).hexdigest()
    recovery_event = _recovery_result_event(
        app.state.runtime,
        context.execution_scope.thread_id,
        recovery_item.item_id,
    )
    assert recovery_event.payload["recovered_after_terminal"] is False
    assert len(adapter.invocations) == 1


def test_same_key_concurrent_model_calls_wait_and_never_dispatch_twice(
    tmp_path: Path,
) -> None:
    adapter = _LateWriteAdapter(content_size=190_000)
    app, service = _runtime(tmp_path, feishu=adapter, full_access=True)
    _connect(service, "feishu")
    composition, context, arguments = _disclosed_write(app, "concurrent")

    async def scenario():
        first, second = await asyncio.gather(
            composition.connector_agent_runtime.write(arguments, context),
            composition.connector_agent_runtime.write(arguments, context),
        )
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second
    assert first["delivery"] == "artifact"
    assert len(adapter.invocations) == 1
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM connector_invocations"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_entities"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM items WHERE kind='artifact'"
        ).fetchone()[0] == 1


def test_unavailable_receipt_is_secret_free_exact_replay_and_not_an_oracle(
    tmp_path: Path,
) -> None:
    adapter = _ResultAdapter(leak_secret=True)
    app, service = _runtime(tmp_path, feishu=adapter)
    _connect(service, "feishu")
    composition, _thread, _created, context, arguments = _disclosed_read(
        app, adapter, "secret"
    )

    first = asyncio.run(composition.connector_agent_runtime.read(arguments, context))
    replay = asyncio.run(composition.connector_agent_runtime.read(arguments, context))
    assert first == replay
    assert first["delivery"] == "result_unavailable"
    assert first["identity_kind"] == "receipt"
    assert first["error_code"] == "connector_result_secret_rejected"
    assert first["size_bytes"] == 0
    assert len(adapter.invocations) == 1
    assert _recovery_tool_items(app.state.runtime, _thread.thread_id) == []

    rejected = {
        "ok": True,
        "action_id": "documents.read",
        "title": "secret-1",
    }
    rejected_digest = hashlib.sha256(_canonical(rejected)).hexdigest()
    database_bytes = (tmp_path / "runtime.db").read_bytes()
    assert b"secret-1" not in database_bytes
    assert rejected_digest.encode("ascii") not in database_bytes


def test_artifact_read_enforces_revision_thread_kind_utf8_and_character_bounds(
    tmp_path: Path,
) -> None:
    adapter = _ResultAdapter(content_size=190_000)
    app, service = _runtime(tmp_path, feishu=adapter)
    _connect(service, "feishu")
    composition, thread, created, context, arguments = _disclosed_read(
        app, adapter, "reader"
    )
    envelope = asyncio.run(
        composition.connector_agent_runtime.read(arguments, context)
    )
    artifact = envelope["artifact"]
    read_context = ToolInvocationContext(
        invocation_id="artifact-read-invocation",
        capability_snapshot_id=context.capability_snapshot_id,
        policy_snapshot_id=context.policy_snapshot_id,
        tool_id="artifact_read",
        idempotency_key=None,
        approved=True,
        effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
        execution_scope=context.execution_scope,
        tool_call_id="artifact-read-call",
    )
    reader = app.state.runtime_composition.artifact_read_runtime
    chunk = reader.read(
        {
            "artifact_id": artifact["artifact_id"],
            "revision_id": artifact["revision_id"],
            "offset_chars": 0,
            "max_chars": 19,
        },
        read_context,
    )
    assert chunk["content"].startswith("{")
    assert len(chunk["content"]) == 19
    assert chunk["next_offset_chars"] == 19
    assert chunk["eof"] is False
    assert "path" not in json.dumps(chunk)

    with pytest.raises(ToolArgumentsValidationError):
        reader.read(
            {
                "artifact_id": artifact["artifact_id"],
                "revision_id": artifact["revision_id"],
                "offset_chars": 10**9,
                "max_chars": 1,
            },
            read_context,
        )

    other_scope = ToolExecutionScope(
        job_id=created.job.job_id,
        thread_id="thr_other_thread",
        turn_id=created.turn.turn_id,
        execution_batch_id=context.execution_scope.execution_batch_id,
    )
    with pytest.raises(CapabilityDeniedError):
        reader.read(
            {
                "artifact_id": artifact["artifact_id"],
                "revision_id": artifact["revision_id"],
            },
            replace(read_context, execution_scope=other_scope),
        )

    projection = app.state.artifact_service.get_user_artifact(
        artifact["artifact_id"]
    )
    assert projection.role.value == "deliverable"
    assert projection.visibility.value == "secondary"
    assert app.state.artifact_service.get_artifact_scope(
        artifact["artifact_id"]
    ).thread_id == thread.thread_id


def test_deferred_stage_recovery_is_redacted_durable_and_maintenance_retries(
    tmp_path: Path,
) -> None:
    vault = InMemoryCredentialVault()
    adapter = _ResultAdapter(content_size=190_000)
    app, service = _runtime(tmp_path, feishu=adapter, vault=vault)
    _connect(service, "feishu")
    composition, _thread, _created, context, arguments = _disclosed_read(
        app, adapter, "deferred"
    )

    def crash(point: str, _invocation_id: str) -> None:
        if point == "after_stage":
            raise RuntimeError("simulated-process-loss-after-stage")

    app.state.connector_result_coordinator.fault_hook = crash
    with pytest.raises(ConnectorUnavailable):
        asyncio.run(composition.connector_agent_runtime.read(arguments, context))
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        digest = str(
            connection.execute(
                "SELECT result_sha256 FROM connector_result_staging"
            ).fetchone()[0]
        )
    blob_path = app.state.artifact_service.blobs.path_for(digest)
    content = blob_path.read_bytes()
    blob_path.unlink()

    restarted, restarted_service = _runtime(
        tmp_path,
        feishu=adapter,
        vault=vault,
    )
    assert restarted.state.connector_result_recovery == {
        "completed": 0,
        "deferred": 1,
    }
    asyncio.run(restarted_service.maintenance_once())
    asyncio.run(restarted_service.maintenance_once())
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM connector_outbox "
            "WHERE event_type='connector.result.recovery_deferred'"
        ).fetchone()[0] == 1
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM connector_outbox "
                "WHERE event_type='connector.result.recovery_deferred' "
                "ORDER BY aggregate_seq DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert set(payload) == {"invocation_id", "stage_status", "error_code"}
    assert payload["stage_status"] == "staged"
    assert payload["error_code"] == "artifact_cas_unavailable"
    assert "path" not in json.dumps(payload)
    assert "simulated" not in json.dumps(payload)

    restarted.state.artifact_service.blobs.put_bytes(content)
    asyncio.run(restarted_service.maintenance_once())
    replay = asyncio.run(
        restarted.state.runtime_composition.connector_agent_runtime.read(
            arguments,
            context,
        )
    )
    assert replay["delivery"] == "artifact"
    assert len(adapter.invocations) == 1
