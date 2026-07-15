from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ecorex.artifacts import (
    ArtifactScope,
    ArtifactService,
    InspectionRegion,
    QualityEvidence,
    QualityStatus,
    RetouchAnnotation,
    RetouchJobStatus,
    RetouchRequest,
)
from ecorex.artifacts.api import create_artifact_router
from ecorex.integration.retouch import (
    RETOUCH_JOB_KIND,
    RetouchCoordinator,
    RetouchWorker,
    RetouchWorkerOutcome,
    RetouchWorkerSupervisor,
    RuntimeRetouchBridge,
)
from ecorex.integration.retouch_adapter import (
    RetouchAdapterError,
    StructuredRetouchAdapterRequest,
    StructuredRetouchAdapterResult,
)
from ecorex.protocol import CreateTurnRequest, ItemKind, JobStatus, TurnStatus
from ecorex.replay import ReplayService
from ecorex.runtime import (
    PermissionAuthority,
    RuntimeComposition,
    RuntimeSnapshotStale,
)
from ecorex.runtime.invariant_guard import RuntimeExecutionGate
from ecorex.runtime.kernel import RuntimeKernel


PNG = b"\x89PNG\r\n\x1a\nTOP_SECRET_SOURCE_BYTES"
OUTPUT = b"\x89PNG\r\n\x1a\nRETOUCHED_OUTPUT_BYTES"


def _snapshot_provider(
    database, *, admin_hard_denies: frozenset[str] = frozenset()
):
    authority = PermissionAuthority(
        database,
        account_id="local-user",
        initial_full_access=False,
        admin_hard_denies=admin_hard_denies,
    )
    permission = authority.current()
    composition = RuntimeComposition(
        database_path=str(database),
        product_version="1.0.0",
        permission_snapshot_id=permission.snapshot_id,
        permission_payload=permission.model_dump(mode="json"),
        full_access=False,
        admin_hard_denies=admin_hard_denies,
        platform="windows",
        installed_packs=frozenset({"image"}),
        connected_connectors=frozenset(),
        online=True,
        permission_provider=authority.current,
        permission_mutation_lock=authority.mutation_lock,
    )

    def provide(*, thread_id, request, turn_request):
        assert thread_id
        assert request.client_request_id
        prepared = composition.prepare_turn(turn_request)
        assert prepared.request.agent_model_id == turn_request.agent_model_id
        assert prepared.request.image_model_id == "gpt-image-2"
        return prepared.snapshot_context

    provide.authority = authority
    provide.composition = composition
    return provide


def _complete_source_turn(kernel: RuntimeKernel, turn_id: str) -> None:
    for status in (
        TurnStatus.PREPARING,
        TurnStatus.MODEL_REQUESTED,
        TurnStatus.STREAMING,
        TurnStatus.FINALIZING,
        TurnStatus.COMPLETED,
    ):
        kernel.transition_turn(turn_id, status)


def _environment(tmp_path):
    database = tmp_path / "runtime.db"
    artifact_root = tmp_path / "artifacts"
    kernel = RuntimeKernel(database)
    thread = kernel.create_thread()
    source = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="create image", client_message_id="source-image"),
    )
    _complete_source_turn(kernel, source.turn.turn_id)
    service = ArtifactService(artifact_root, database_path=database)
    image = service.create_artifact(
        PNG,
        requested_name="poster.png",
        mime_type="image/png",
        scope=ArtifactScope(
            account_id="local-user",
            thread_id=thread.thread_id,
            turn_id=source.turn.turn_id,
            created_by_tool_id="imagegen",
        ),
    )
    snapshot_provider = _snapshot_provider(database)
    bridge = RuntimeRetouchBridge(
        kernel,
        snapshot_context_provider=snapshot_provider,
        permission_mutation_lock=(
            snapshot_provider.composition.permission_mutation_lock
        ),
        deadline_seconds=60,
    )
    coordinator = RetouchCoordinator(service, bridge)
    request = RetouchRequest(
        base_revision_id=image.revision_id,
        selected_artifact_ids=(image.artifact_id,),
        agent_model_id="ecorex-chat",
        image_model_id="gpt-image-2",
        annotations=(
            RetouchAnnotation(
                "rectangle",
                {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2},
                "修正文字",
            ),
        ),
        global_instruction="保持其他区域不变",
        client_request_id="retouch-client-1",
    )
    return kernel, service, coordinator, image, request, artifact_root, database


class RecordingAdapter:
    def __init__(self) -> None:
        self.edit_requests: list[StructuredRetouchAdapterRequest] = []
        self.recover_keys: list[str] = []
        self.results: dict[str, StructuredRetouchAdapterResult] = {}
        self.executions = 0

    async def edit(
        self, request: StructuredRetouchAdapterRequest
    ) -> StructuredRetouchAdapterResult:
        self.edit_requests.append(request)
        if request.idempotency_key not in self.results:
            self.executions += 1
            self.results[request.idempotency_key] = StructuredRetouchAdapterResult(
                result_id="managed-image-result-1",
                content=OUTPUT,
                mime_type="image/png",
                requested_name="poster-retouched.png",
                change_summary="已修正标注区域文字",
                inspection_regions=(
                    InspectionRegion(
                        {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2},
                        "文字区域已检查",
                    ),
                ),
                quality_evidence=QualityEvidence(status=QualityStatus.PASSED),
            )
        return self.results[request.idempotency_key]

    async def recover(
        self, idempotency_key: str
    ) -> StructuredRetouchAdapterResult | None:
        self.recover_keys.append(idempotency_key)
        return self.results.get(idempotency_key)


def test_retouch_request_atomically_binds_one_unified_durable_job_under_concurrency(
    tmp_path,
) -> None:
    kernel, service, coordinator, image, request, _root, database = _environment(
        tmp_path
    )
    coordinators = [coordinator]
    source_scope = service.get_artifact_scope(image.artifact_id)
    source_turn_before = kernel.get_turn(source_scope.turn_id)
    source_items_before = tuple(
        item
        for item in kernel.projection(source_scope.thread_id).items
        if item.turn_id == source_scope.turn_id
    )
    for _index in range(7):
        other_service = ArtifactService(tmp_path / "artifacts", database_path=database)
        coordinators.append(
            RetouchCoordinator(
                other_service,
                RuntimeRetouchBridge(
                    RuntimeKernel(database),
                    snapshot_context_provider=(
                        coordinator.bridge.snapshot_context_provider
                    ),
                ),
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(
            pool.map(
                lambda item: item.request(image.artifact_id, request), coordinators
            )
        )

    assert len({job.job_id for job in jobs}) == 1

    # A process restart followed by another concurrent delivery of the same
    # client request returns the committed binding without recapturing policy.
    restarted_coordinators = []
    for _index in range(8):
        restarted_kernel = RuntimeKernel(database)
        restarted_service = ArtifactService(
            tmp_path / "artifacts", database_path=database
        )
        restarted_coordinators.append(
            RetouchCoordinator(
                restarted_service,
                RuntimeRetouchBridge(
                    restarted_kernel,
                    snapshot_context_provider=_snapshot_provider(database),
                ),
            )
        )
    with ThreadPoolExecutor(max_workers=8) as pool:
        replayed = list(
            pool.map(
                lambda item: item.request(image.artifact_id, request),
                restarted_coordinators,
            )
        )
    assert {job.job_id for job in replayed} == {jobs[0].job_id}

    internal = service.get_internal_retouch_job(jobs[0].job_id)
    assert internal.durable_job_id
    with kernel.database.reader() as connection:
        rows = connection.execute(
            "SELECT * FROM jobs WHERE kind = ?", (RETOUCH_JOB_KIND,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["job_id"] == internal.durable_job_id
    assert rows[0]["thread_id"] == service.get_artifact_scope(image.artifact_id).thread_id
    assert rows[0]["turn_id"] == internal.execution_turn_id
    assert internal.annotation_layer_artifact_id not in rows[0]["payload_json"]
    assert internal.execution_turn_id != service.get_artifact_scope(image.artifact_id).turn_id
    with kernel.database.reader() as connection:
        operation_turns = connection.execute(
            "SELECT * FROM turns WHERE thread_id = ? AND metadata_json LIKE ?",
            (internal.execution_thread_id, '%"operation":"artifact_retouch"%'),
        ).fetchall()
        assert len(operation_turns) == 1
        assert operation_turns[0]["turn_id"] == internal.execution_turn_id
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE turn_id = ? AND kind = 'agent_turn'",
            (internal.execution_turn_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM items WHERE turn_id = ? AND kind = 'message'",
            (internal.execution_turn_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind = ?",
            (RETOUCH_JOB_KIND,),
        ).fetchone()[0] == 1
        context = connection.execute(
            "SELECT * FROM job_runtime_contexts WHERE job_id = ?",
            (internal.durable_job_id,),
        ).fetchone()
        assert context is not None
        assert all(context[key] for key in context.keys())
    assert kernel.get_turn(source_scope.turn_id) == source_turn_before
    assert tuple(
        item
        for item in kernel.projection(source_scope.thread_id).items
        if item.turn_id == source_scope.turn_id
    ) == source_items_before


def test_supervised_retouch_success_is_atomic_public_and_structured(tmp_path) -> None:
    kernel, service, coordinator, image, request, _root, _database = _environment(
        tmp_path
    )
    job = coordinator.request(image.artifact_id, request)
    internal_before = service.get_internal_retouch_job(job.job_id)
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    assert gate.snapshot().healthy
    adapter = RecordingAdapter()

    result = asyncio.run(
        RetouchWorker(coordinator, adapter, lease_seconds=5).run_once("retouch-worker")
    )

    assert result.outcome is RetouchWorkerOutcome.COMPLETED
    completed = service.get_retouch_job(job.job_id)
    assert completed.status is RetouchJobStatus.COMPLETED
    assert completed.result_revision_id != image.revision_id
    assert completed.change_summary == "已修正标注区域文字"
    assert completed.inspection_regions[0].summary == "文字区域已检查"
    assert service.read_user_content(image.artifact_id) == OUTPUT
    assert kernel.jobs.get(internal_before.durable_job_id).status is JobStatus.COMPLETED
    assert kernel.jobs._execution_permits == {}

    assert len(adapter.edit_requests) == 1
    structured = adapter.edit_requests[0]
    assert not hasattr(structured, "prompt")
    assert structured.base.revision_id == image.revision_id
    assert structured.annotations == request.annotations
    assert structured.global_instruction == request.global_instruction
    assert "TOP_SECRET_SOURCE_BYTES" not in repr(structured)

    scope = service.get_artifact_scope(image.artifact_id)
    projection = kernel.projection(scope.thread_id)
    artifact_items = [item for item in projection.items if item.kind is ItemKind.ARTIFACT]
    assert len(artifact_items) == 1
    item = artifact_items[0]
    assert item.turn_id == internal_before.execution_turn_id
    assert item.content["artifact"]["revision_id"] == completed.result_revision_id
    assert item.content["preview"]["artifact_id"] == image.artifact_id
    assert item.content["change_summary"] == completed.change_summary

    events = kernel.events.page(scope.thread_id, limit=1000).events
    completed_event = next(
        event for event in events if event.event_type == "artifact.retouch.completed"
    )
    retouch_events = [
        event
        for event in events
        if event.turn_id == internal_before.execution_turn_id
    ]
    assert [
        event.payload["to"]
        for event in retouch_events
        if event.event_type == "turn.status_changed"
    ] == [
        "preparing",
        "tool_pending",
        "tool_running",
        "finalizing",
        "completed",
    ]
    assert all(
        event.config_snapshot_id
        and event.capability_snapshot_id
        and event.permission_snapshot_id
        for event in retouch_events
    )
    wire = json.dumps(
        {
            "item": item.model_dump(mode="json"),
            "event": completed_event.model_dump(mode="json"),
        },
        ensure_ascii=False,
    )
    assert internal_before.annotation_layer_artifact_id not in wire
    assert internal_before.annotation_layer_revision_id not in wire
    assert "TOP_SECRET_SOURCE_BYTES" not in wire
    assert "RETOUCHED_OUTPUT_BYTES" not in wire

    source_items = [
        candidate
        for candidate in projection.items
        if candidate.turn_id == scope.turn_id and candidate.kind is ItemKind.ARTIFACT
    ]
    assert source_items == []
    retouch_turn = next(
        turn
        for turn in projection.turns
        if turn.turn_id == internal_before.execution_turn_id
    )
    assert retouch_turn.status is TurnStatus.COMPLETED
    user_message = next(
        candidate
        for candidate in projection.items
        if candidate.turn_id == retouch_turn.turn_id
        and candidate.kind is ItemKind.MESSAGE
    )
    assert user_message.content["metadata"]["operation"] == RETOUCH_JOB_KIND
    assert user_message.content["metadata"]["annotations"][0]["instruction"] == "修正文字"

    replay = ReplayService(kernel).mock_replay(scope.thread_id).projection
    replay_turn = next(
        turn for turn in replay.turns if turn.turn_id == retouch_turn.turn_id
    )
    replay_item = next(
        candidate for candidate in replay.items if candidate.item_id == item.item_id
    )
    assert replay_turn.status is TurnStatus.COMPLETED
    assert replay_item.content == item.content

    coordinator.bridge.snapshot_context_provider = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("idempotent replay recaptured Runtime snapshots")
    )
    duplicate = coordinator.request(image.artifact_id, request)
    assert duplicate.job_id == job.job_id
    assert asyncio.run(
        RetouchWorker(coordinator, adapter, lease_seconds=5).run_once("idle-worker")
    ).outcome is RetouchWorkerOutcome.IDLE
    assert adapter.executions == 1


def test_stale_permission_context_rolls_back_artifact_turn_message_and_job(tmp_path) -> None:
    kernel, service, coordinator, image, request, _root, _database = _environment(
        tmp_path
    )
    provider = coordinator.bridge.snapshot_context_provider
    authority = provider.authority
    composition = provider.composition

    def stale_provider(*, thread_id, request, turn_request):
        prepared = composition.prepare_turn(turn_request)
        current = authority.current()
        changed = authority.update(
            "full_access",
            expected_revision=current.revision,
            client_request_id="permission-changed-during-retouch",
        )
        composition.record_permission(changed)
        return prepared.snapshot_context

    coordinator.bridge.snapshot_context_provider = stale_provider
    with kernel.database.reader() as connection:
        before_turns = connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    with pytest.raises(RuntimeSnapshotStale):
        coordinator.request(image.artifact_id, request)
    with kernel.database.reader() as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == before_turns
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_retouch_jobs"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind = ?", (RETOUCH_JOB_KIND,)
        ).fetchone()[0] == 0


def test_retouch_turn_acceptance_linearizes_with_permission_update(
    tmp_path,
    monkeypatch,
) -> None:
    kernel, service, coordinator, image, request, _root, _database = _environment(
        tmp_path
    )
    provider = coordinator.bridge.snapshot_context_provider
    authority = provider.authority
    composition = provider.composition
    initial = authority.current()
    original_prepare = composition.prepare_turn
    prepared = threading.Event()
    release = threading.Event()

    def pause_prepared_retouch(turn_request):
        result = original_prepare(turn_request)
        if (
            turn_request.metadata.get("client_request_id")
            == request.client_request_id
        ):
            prepared.set()
            assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(composition, "prepare_turn", pause_prepared_retouch)

    with ThreadPoolExecutor(max_workers=2) as executor:
        retouch_future = executor.submit(
            coordinator.request,
            image.artifact_id,
            request,
        )
        assert prepared.wait(timeout=5)
        update_future = executor.submit(
            authority.update,
            "full_access",
            expected_revision=initial.revision,
            client_request_id="linearized-retouch-permission",
        )
        threading.Event().wait(0.1)
        assert not update_future.done()
        release.set()
        first = retouch_future.result(timeout=5)
        changed = update_future.result(timeout=5)

    first_internal = service.get_internal_retouch_job(first.job_id)
    with kernel.database.reader() as connection:
        first_runtime = connection.execute(
            "SELECT turn_id FROM jobs WHERE job_id = ?",
            (first_internal.durable_job_id,),
        ).fetchone()
    assert first_runtime is not None
    first_event = next(
        event
        for event in kernel.events.page(
            service.get_artifact_scope(image.artifact_id).thread_id,
            limit=1000,
        ).events
        if event.event_type == "turn.accepted"
        and event.turn_id == first_runtime["turn_id"]
    )
    assert first_event.permission_snapshot_id == initial.snapshot_id

    second_request = RetouchRequest.from_dict(
        {
            **request.to_dict(),
            "client_request_id": "linearized-retouch-new",
        }
    )
    second = coordinator.request(image.artifact_id, second_request)
    second_internal = service.get_internal_retouch_job(second.job_id)
    with kernel.database.reader() as connection:
        second_runtime = connection.execute(
            "SELECT turn_id FROM jobs WHERE job_id = ?",
            (second_internal.durable_job_id,),
        ).fetchone()
    assert second_runtime is not None
    second_event = next(
        event
        for event in kernel.events.page(
            service.get_artifact_scope(image.artifact_id).thread_id,
            limit=1000,
        ).events
        if event.event_type == "turn.accepted"
        and event.turn_id == second_runtime["turn_id"]
    )
    assert second_event.permission_snapshot_id == changed.snapshot_id


def test_admin_hard_deny_blocks_retouch_before_any_product_state(tmp_path) -> None:
    kernel, service, coordinator, image, request, _root, database = _environment(
        tmp_path
    )
    coordinator.bridge.snapshot_context_provider = _snapshot_provider(
        database, admin_hard_denies=frozenset({"imagegen"})
    )
    with kernel.database.reader() as connection:
        before_turns = connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    with pytest.raises(RuntimeSnapshotStale, match="image editing is unavailable"):
        coordinator.request(image.artifact_id, request)
    with kernel.database.reader() as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == before_turns
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_retouch_jobs"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind = ?", (RETOUCH_JOB_KIND,)
        ).fetchone()[0] == 0


def test_retouch_turns_obey_thread_fifo_without_agent_turn_jobs(tmp_path) -> None:
    kernel, service, coordinator, image, request, _root, _database = _environment(
        tmp_path
    )
    first = coordinator.request(image.artifact_id, request)
    second = coordinator.request(
        image.artifact_id,
        RetouchRequest(
            base_revision_id=image.revision_id,
            selected_artifact_ids=(image.artifact_id,),
            global_instruction="第二个排队修图请求",
            client_request_id="retouch-client-2",
        ),
    )
    first_internal = service.get_internal_retouch_job(first.job_id)
    second_internal = service.get_internal_retouch_job(second.job_id)
    assert first_internal.execution_turn_id != second_internal.execution_turn_id

    leased = kernel.jobs.lease_next("fifo-first", kinds=[RETOUCH_JOB_KIND])
    assert leased is not None and leased.job_id == first_internal.durable_job_id
    assert kernel.jobs.lease_next("fifo-blocked", kinds=[RETOUCH_JOB_KIND]) is None

    coordinator.cancel(first.job_id, reason="fifo_test_cancel")
    next_job = kernel.jobs.lease_next("fifo-second", kinds=[RETOUCH_JOB_KIND])
    assert next_job is not None and next_job.job_id == second_internal.durable_job_id
    assert kernel.get_turn(first_internal.execution_turn_id).status is TurnStatus.CANCELLED
    with kernel.database.reader() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE turn_id IN (?, ?) AND kind = 'agent_turn'",
            (first_internal.execution_turn_id, second_internal.execution_turn_id),
        ).fetchone()[0] == 0


def test_adapter_uses_request_time_reference_revision_snapshot(tmp_path) -> None:
    _kernel, service, coordinator, image, _request, _root, _database = _environment(
        tmp_path
    )
    scope = service.get_artifact_scope(image.artifact_id)
    reference_v1_bytes = b"\x89PNG\r\n\x1a\nREFERENCE_VERSION_ONE"
    reference = service.create_artifact(
        reference_v1_bytes,
        requested_name="reference.png",
        mime_type="image/png",
        scope=scope,
    )
    request = RetouchRequest(
        base_revision_id=image.revision_id,
        selected_artifact_ids=(image.artifact_id,),
        reference_artifact_ids=(reference.artifact_id,),
        global_instruction="参考配色",
        client_request_id="retouch-reference-snapshot",
    )
    job = coordinator.request(image.artifact_id, request)

    advance = service.request_retouch(
        reference.artifact_id,
        RetouchRequest(
            base_revision_id=reference.revision_id,
            selected_artifact_ids=(reference.artifact_id,),
            global_instruction="advance reference",
            client_request_id="advance-reference",
        ),
    )
    service.complete_retouch(
        advance.job_id,
        b"\x89PNG\r\n\x1a\nREFERENCE_VERSION_TWO",
        mime_type="image/png",
        change_summary="reference advanced",
    )

    structured = coordinator.adapter_request(job.job_id)
    assert structured.references[0].revision_id == reference.revision_id
    assert structured.references[0].content == reference_v1_bytes
    internal = service.get_internal_retouch_job(job.job_id)
    assert internal.input_revision_ids[reference.artifact_id] == reference.revision_id
    assert "input_revision_ids" not in json.dumps(job.to_dict())


def test_staged_result_recovers_after_crash_without_second_external_edit(
    tmp_path, monkeypatch
) -> None:
    kernel, service, coordinator, image, request, artifact_root, database = _environment(
        tmp_path
    )
    job = coordinator.request(image.artifact_id, request)
    adapter = RecordingAdapter()
    original_complete = service.complete_staged_retouch

    def crash_after_stage(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(service, "complete_staged_retouch", crash_after_stage)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            RetouchWorker(coordinator, adapter, lease_seconds=5).run_once("crashed")
        )
    monkeypatch.setattr(service, "complete_staged_retouch", original_complete)
    staged = service.get_internal_retouch_job(job.job_id)
    assert staged.staged_result is not None
    assert adapter.executions == 1
    checkpoint = kernel.jobs.get(staged.durable_job_id).checkpoint
    assert checkpoint["phase"] == "result_staged"
    assert checkpoint["external_idempotency_key"] == staged.external_idempotency_key
    execution_turn_id = staged.execution_turn_id
    assert execution_turn_id is not None
    assert kernel.get_turn(execution_turn_id).status is TurnStatus.TOOL_RUNNING

    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE job_id = ?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                staged.durable_job_id,
            ),
        )
    kernel.jobs.reclaim_expired(now=datetime.now(UTC))
    restarted_kernel = RuntimeKernel(database)
    restarted_service = ArtifactService(artifact_root, database_path=database)
    restarted = RetouchCoordinator(
        restarted_service,
        RuntimeRetouchBridge(
            restarted_kernel,
            snapshot_context_provider=coordinator.bridge.snapshot_context_provider,
            deadline_seconds=60,
        ),
    )

    class MustNotEdit(RecordingAdapter):
        async def edit(self, request):  # pragma: no cover - assertion path
            raise AssertionError("staged recovery called external image editing")

        async def recover(self, idempotency_key):  # pragma: no cover
            raise AssertionError("staged recovery queried external image editing")

    recovered = asyncio.run(
        RetouchWorker(restarted, MustNotEdit(), lease_seconds=5).run_once("restarted")
    )
    assert recovered.outcome is RetouchWorkerOutcome.COMPLETED
    assert restarted_service.get_retouch_job(job.job_id).status is RetouchJobStatus.COMPLETED
    assert restarted_service.read_user_content(image.artifact_id) == OUTPUT
    assert restarted_kernel.get_turn(execution_turn_id).status is TurnStatus.COMPLETED
    with restarted_kernel.database.reader() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE turn_id = ?",
            (execution_turn_id,),
        ).fetchone()[0] == 1


class FlakyIdempotentAdapter(RecordingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def edit(self, request):
        self.calls += 1
        self.edit_requests.append(request)
        if self.calls == 1:
            raise RetouchAdapterError("gateway_unavailable", retryable=True)
        return await super().edit(request)


def test_retry_uses_stable_external_key_and_terminal_failure_cancel_reconcile(
    tmp_path,
) -> None:
    kernel, service, coordinator, image, request, _root, _database = _environment(
        tmp_path
    )
    job = coordinator.request(image.artifact_id, request)
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    assert gate.snapshot().healthy
    adapter = FlakyIdempotentAdapter()
    worker = RetouchWorker(
        coordinator, adapter, lease_seconds=5, retry_delay_seconds=0
    )

    first = asyncio.run(worker.run_once("retry-worker"))
    assert first.outcome is RetouchWorkerOutcome.RETRY_SCHEDULED
    internal = service.get_internal_retouch_job(job.job_id)
    assert internal.status is RetouchJobStatus.QUEUED
    assert kernel.jobs.get(internal.durable_job_id).status is JobStatus.RETRY_SCHEDULED
    assert kernel.get_turn(internal.execution_turn_id).status is TurnStatus.RETRY_WAIT
    assert kernel.jobs._execution_permits == {}

    second = asyncio.run(worker.run_once("retry-worker"))
    assert second.outcome is RetouchWorkerOutcome.COMPLETED
    keys = {request.idempotency_key for request in adapter.edit_requests}
    assert keys == {internal.external_idempotency_key}
    assert adapter.executions == 1
    assert kernel.get_turn(internal.execution_turn_id).status is TurnStatus.COMPLETED
    assert kernel.jobs._execution_permits == {}

    next_request = RetouchRequest(
        base_revision_id=service.get_user_artifact(image.artifact_id).revision_id,
        selected_artifact_ids=(image.artifact_id,),
        global_instruction="再调整一次",
        client_request_id="retouch-cancel",
    )
    cancelled = coordinator.request(image.artifact_id, next_request)
    cancelled_internal = service.get_internal_retouch_job(cancelled.job_id)
    projection = coordinator.cancel(cancelled.job_id)
    assert projection.status is RetouchJobStatus.CANCELLED
    assert kernel.jobs.get(cancelled_internal.durable_job_id).status is JobStatus.CANCELLED
    assert (
        kernel.get_turn(cancelled_internal.execution_turn_id).status
        is TurnStatus.CANCELLED
    )

    third_request = RetouchRequest(
        base_revision_id=service.get_user_artifact(image.artifact_id).revision_id,
        selected_artifact_ids=(image.artifact_id,),
        global_instruction="会过期的任务",
        client_request_id="retouch-expire",
    )
    expired = coordinator.request(image.artifact_id, third_request)
    expired_internal = service.get_internal_retouch_job(expired.job_id)
    assert kernel.jobs.expire_deadline(
        expired_internal.durable_job_id,
        now=datetime.now(UTC) + timedelta(minutes=2),
    )
    assert coordinator.reconcile() == 1
    assert service.get_retouch_job(expired.job_id).status is RetouchJobStatus.FAILED
    assert kernel.get_turn(expired_internal.execution_turn_id).status is TurnStatus.FAILED


def test_artifact_post_uses_mountable_coordinator_instead_of_orphan_queue(
    tmp_path,
) -> None:
    kernel, service, coordinator, image, request, _root, _database = _environment(
        tmp_path
    )
    app = FastAPI()
    app.include_router(
        create_artifact_router(service, retouch_coordinator=coordinator)
    )
    response = TestClient(app).post(
        f"/api/v1/artifacts/{image.artifact_id}/retouch",
        json=request.to_dict(),
    )
    assert response.status_code == 202
    internal = service.get_internal_retouch_job(response.json()["job_id"])
    assert internal.durable_job_id
    assert kernel.jobs.get(internal.durable_job_id).kind == RETOUCH_JOB_KIND


def test_nonretryable_adapter_failure_atomically_terminates_domain_and_runtime(
    tmp_path,
) -> None:
    kernel, service, coordinator, image, request, _root, _database = _environment(
        tmp_path
    )
    job = coordinator.request(image.artifact_id, request)

    class RejectedAdapter:
        async def edit(self, _request):
            raise RetouchAdapterError("image_policy_rejected", retryable=False)

        async def recover(self, _idempotency_key):
            return None

    outcome = asyncio.run(
        RetouchWorker(coordinator, RejectedAdapter(), lease_seconds=5).run_once(
            "rejected"
        )
    )
    internal = service.get_internal_retouch_job(job.job_id)
    assert outcome.outcome is RetouchWorkerOutcome.FAILED
    assert internal.status is RetouchJobStatus.FAILED
    assert internal.failure_reason == "image_policy_rejected"
    assert kernel.jobs.get(internal.durable_job_id).status is JobStatus.FAILED
    assert kernel.get_turn(internal.execution_turn_id).status is TurnStatus.FAILED
    events = kernel.events.page(internal.execution_thread_id, limit=1000).events
    assert any(event.event_type == "artifact.retouch.failed" for event in events)


def test_retouch_late_provider_result_is_rejected_after_epoch_close(tmp_path) -> None:
    kernel, service, coordinator, image, request, _root, _database = _environment(
        tmp_path
    )
    job = coordinator.request(image.artifact_id, request)
    internal_before = service.get_internal_retouch_job(job.job_id)
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    assert gate.snapshot().healthy

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingAdapter(RecordingAdapter):
            async def edit(self, adapter_request):
                entered.set()
                await release.wait()
                return await super().edit(adapter_request)

        adapter = BlockingAdapter()
        task = asyncio.create_task(
            RetouchWorker(coordinator, adapter, lease_seconds=5).run_once(
                "late-result-worker"
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        gate.request_critical(error_code="retouch_provider_epoch_closed")
        release.set()
        return await asyncio.wait_for(task, timeout=5), adapter

    outcome, adapter = asyncio.run(scenario())
    assert adapter.executions == 1
    assert outcome.outcome is RetouchWorkerOutcome.FAILED
    assert outcome.reason == "execution_epoch_closed"

    internal_after = service.get_internal_retouch_job(job.job_id)
    assert internal_after.status is RetouchJobStatus.RUNNING
    assert internal_after.staged_result is None
    assert internal_after.result_revision_id is None
    assert service.get_user_artifact(image.artifact_id).revision_id == image.revision_id
    assert service.read_user_content(image.artifact_id) == PNG
    assert kernel.jobs.get(internal_before.durable_job_id).status is JobStatus.RUNNING
    assert kernel.get_turn(internal_before.execution_turn_id).status is TurnStatus.TOOL_RUNNING
    projection = kernel.projection(internal_before.execution_thread_id)
    assert not any(item.kind is ItemKind.ARTIFACT for item in projection.items)


def test_retouch_result_commit_rolls_back_when_epoch_closes_after_provider(
    tmp_path, monkeypatch
) -> None:
    kernel, service, coordinator, image, request, _root, _database = _environment(
        tmp_path
    )
    job = coordinator.request(image.artifact_id, request)
    internal_before = service.get_internal_retouch_job(job.job_id)
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    assert gate.snapshot().healthy
    original_stage = service.repository.stage_retouch_result
    closed = False

    def close_before_stage_commit(
        job_id,
        result,
        *,
        now,
        before_commit=None,
    ):
        def validate_after_close():
            nonlocal closed
            closed = True
            gate.request_critical(error_code="retouch_result_precommit_closed")
            assert before_commit is not None
            before_commit()

        return original_stage(
            job_id,
            result,
            now=now,
            before_commit=validate_after_close,
        )

    monkeypatch.setattr(
        service.repository,
        "stage_retouch_result",
        close_before_stage_commit,
    )
    adapter = RecordingAdapter()
    outcome = asyncio.run(
        RetouchWorker(coordinator, adapter, lease_seconds=5).run_once(
            "precommit-worker"
        )
    )

    assert closed
    assert adapter.executions == 1
    assert outcome.outcome is RetouchWorkerOutcome.FAILED
    assert outcome.reason == "execution_epoch_closed"
    internal_after = service.get_internal_retouch_job(job.job_id)
    assert internal_after.status is RetouchJobStatus.RUNNING
    assert internal_after.staged_result is None
    assert internal_after.result_revision_id is None
    assert service.get_user_artifact(image.artifact_id).revision_id == image.revision_id
    assert service.read_user_content(image.artifact_id) == PNG
    assert kernel.jobs.get(internal_before.durable_job_id).status is JobStatus.RUNNING
    assert kernel.get_turn(internal_before.execution_turn_id).status is TurnStatus.TOOL_RUNNING


def test_critical_retouch_reconcile_and_supervisor_do_not_write_or_call_provider(
    tmp_path,
) -> None:
    kernel, service, coordinator, image, request, _root, _database = _environment(
        tmp_path
    )
    job = coordinator.request(image.artifact_id, request)
    internal = service.get_internal_retouch_job(job.job_id)
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    kernel.jobs.cancel(internal.durable_job_id, reason="terminal_before_critical")
    gate.mark_critical(error_code="retouch_supervisor_closed")
    before = service.get_internal_retouch_job(job.job_id)
    with kernel.database.reader() as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    class MustNotCallProvider(RecordingAdapter):
        async def edit(self, _request):  # pragma: no cover - assertion path
            raise AssertionError("critical supervisor called the retouch provider")

        async def recover(self, _idempotency_key):  # pragma: no cover
            raise AssertionError("critical supervisor called provider recovery")

    adapter = MustNotCallProvider()
    assert coordinator.reconcile() == 0

    async def scenario():
        supervisor = RetouchWorkerSupervisor(
            RetouchWorker(coordinator, adapter, lease_seconds=5),
            concurrency=2,
            idle_poll_seconds=0.01,
            shutdown_timeout_seconds=1,
            close_adapter_on_stop=False,
        )
        await supervisor.start()
        await asyncio.sleep(0.05)
        await supervisor.stop()
        return supervisor.snapshot()

    snapshot = asyncio.run(scenario())
    after = service.get_internal_retouch_job(job.job_id)
    assert after == before
    assert after.status is RetouchJobStatus.QUEUED
    assert adapter.executions == 0
    assert snapshot.completed_runs == 0
    assert snapshot.retry_runs == 0
    with kernel.database.reader() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == event_count


def test_retouch_supervisor_wakes_processes_and_stops_cleanly(tmp_path) -> None:
    _kernel, service, coordinator, image, request, _root, _database = _environment(
        tmp_path
    )
    adapter = RecordingAdapter()
    supervisor = RetouchWorkerSupervisor(
        RetouchWorker(coordinator, adapter, lease_seconds=5),
        concurrency=2,
        idle_poll_seconds=0.01,
        shutdown_timeout_seconds=1,
        close_adapter_on_stop=False,
    )
    coordinator.notify = supervisor.notify

    async def scenario():
        await supervisor.start()
        job = coordinator.request(image.artifact_id, request)
        deadline = asyncio.get_running_loop().time() + 5
        while service.get_retouch_job(job.job_id).status is not RetouchJobStatus.COMPLETED:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("retouch supervisor did not complete queued work")
            await asyncio.sleep(0.01)
        await supervisor.stop()
        return supervisor.snapshot()

    snapshot = asyncio.run(scenario())
    assert snapshot.running is False
    assert snapshot.completed_runs == 1
    assert snapshot.failed_runs == 0
