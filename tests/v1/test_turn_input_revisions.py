from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from ecorex.protocol import (
    CreateTurnRequest,
    SteerTurnRequest,
    TurnStatus,
)
from ecorex.runtime import RuntimeKernel, TurnSnapshotContext
from ecorex.runtime.errors import ConflictError


def _turn(kernel: RuntimeKernel, *, explicit_tool_ids: list[str] | None = None):
    thread = kernel.create_thread()
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(
            input="生成一张办公插图",
            explicit_tool_ids=explicit_tool_ids or [],
            client_message_id="initial-message",
        ),
    )
    return thread, created


def _context(suffix: str = "one") -> TurnSnapshotContext:
    return TurnSnapshotContext(
        config_snapshot_id=f"config-{suffix}",
        capability_snapshot_id=f"capability-{suffix}",
        permission_snapshot_id=f"permission-{suffix}",
        model_catalog_snapshot_id=f"models-{suffix}",
        extension_snapshot_id=f"extensions-{suffix}",
    )


def _advance_to_streaming(kernel: RuntimeKernel, turn_id: str) -> None:
    for status in (
        TurnStatus.PREPARING,
        TurnStatus.MODEL_REQUESTED,
        TurnStatus.STREAMING,
    ):
        kernel.transition_turn(turn_id, status)


def test_initial_and_steer_intents_are_append_only_and_restart_safe(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    _, created = _turn(kernel, explicit_tool_ids=["imagegen"])
    kernel.steer_turn(
        created.turn.turn_id,
        SteerTurnRequest(
            input="把背景改成蓝色",
            explicit_tool_ids=["vision", "imagegen"],
            client_message_id="steer-message",
            metadata={"interaction_mode": "retouch"},
        ),
    )

    revisions = kernel.turn_inputs.list_for_turn(created.turn.turn_id)
    assert [revision.ordinal for revision in revisions] == [0, 1]
    assert [revision.source for revision in revisions] == ["initial", "steer"]
    assert revisions[0].explicit_tool_ids == ["imagegen"]
    assert revisions[1].explicit_tool_ids == ["vision", "imagegen"]
    assert revisions[0].intent_fingerprint != revisions[1].intent_fingerprint

    restarted = RuntimeKernel(path)
    assert restarted.turn_inputs.list_for_turn(created.turn.turn_id) == revisions
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with restarted.database.transaction() as connection:
            connection.execute(
                "UPDATE turn_input_revisions SET input_text = 'tampered' "
                "WHERE revision_id = ?",
                (revisions[0].revision_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with restarted.database.transaction() as connection:
            connection.execute(
                "DELETE FROM turn_input_revisions WHERE revision_id = ?",
                (revisions[1].revision_id,),
            )


def test_client_message_id_covers_the_complete_intent_fingerprint(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread, created = _turn(kernel, explicit_tool_ids=["imagegen"])
    duplicate = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(
            input="生成一张办公插图",
            explicit_tool_ids=["imagegen"],
            client_message_id="initial-message",
        ),
    )
    assert duplicate.turn.turn_id == created.turn.turn_id

    with pytest.raises(ConflictError, match="different Turn intent"):
        kernel.create_turn(
            thread.thread_id,
            CreateTurnRequest(
                input="生成一张办公插图",
                explicit_tool_ids=["shell"],
                client_message_id="initial-message",
            ),
        )

    steer = SteerTurnRequest(
        input="再生成一版",
        explicit_tool_ids=["imagegen"],
        client_message_id="steer-message",
    )
    first = kernel.steer_turn(created.turn.turn_id, steer)
    repeated = kernel.steer_turn(created.turn.turn_id, steer)
    assert repeated.watermark == first.watermark
    assert len(kernel.turn_inputs.list_for_turn(created.turn.turn_id)) == 2

    with pytest.raises(ConflictError, match="different Turn intent"):
        kernel.steer_turn(
            created.turn.turn_id,
            steer.model_copy(update={"explicit_tool_ids": ["shell"]}),
        )


def test_steer_is_atomic_when_revision_persistence_fails(tmp_path, monkeypatch) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread, created = _turn(kernel)
    watermark = kernel.events.watermark(thread.thread_id)
    item_ids = [item.item_id for item in kernel.projection(thread.thread_id).items]

    def fail_revision(*_args, **_kwargs):
        raise RuntimeError("revision write failed")

    monkeypatch.setattr(
        kernel.turn_inputs,
        "append_steer_in_transaction",
        fail_revision,
    )
    with pytest.raises(RuntimeError, match="revision write failed"):
        kernel.steer_turn(
            created.turn.turn_id,
            SteerTurnRequest(input="补充内容", client_message_id="steer-message"),
        )

    assert kernel.events.watermark(thread.thread_id) == watermark
    assert [item.item_id for item in kernel.projection(thread.thread_id).items] == item_ids
    assert [revision.ordinal for revision in kernel.turn_inputs.list_for_turn(
        created.turn.turn_id
    )] == [0]


def test_finalizing_and_terminal_turns_reject_new_steer_but_retry_is_idempotent(
    tmp_path,
) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    _, created = _turn(kernel)
    accepted = SteerTurnRequest(input="已接收", client_message_id="accepted-steer")
    first = kernel.steer_turn(created.turn.turn_id, accepted)
    for status in (
        TurnStatus.PREPARING,
        TurnStatus.MODEL_REQUESTED,
        TurnStatus.STREAMING,
        TurnStatus.FINALIZING,
    ):
        kernel.transition_turn(created.turn.turn_id, status)

    repeated = kernel.steer_turn(created.turn.turn_id, accepted)
    assert repeated.watermark == first.watermark + 4
    with pytest.raises(ConflictError, match="finalizing or terminal"):
        kernel.steer_turn(
            created.turn.turn_id,
            SteerTurnRequest(input="太晚了", client_message_id="late-steer"),
        )

    kernel.transition_turn(created.turn.turn_id, TurnStatus.COMPLETED)
    with pytest.raises(ConflictError, match="finalizing or terminal"):
        kernel.steer_turn(
            created.turn.turn_id,
            SteerTurnRequest(input="仍然太晚", client_message_id="later-steer"),
        )


def test_begin_finalizing_detects_pending_input_and_is_terminal_safe(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    _, created = _turn(kernel)
    _advance_to_streaming(kernel, created.turn.turn_id)
    kernel.steer_turn(
        created.turn.turn_id,
        SteerTurnRequest(input="并入当前回答", client_message_id="pending-steer"),
    )

    assert kernel.begin_finalizing_if_inputs_applied(
        created.turn.turn_id, applied_through_ordinal=0
    ) is False
    assert kernel.get_turn(created.turn.turn_id).status is TurnStatus.STREAMING
    assert kernel.begin_finalizing_if_inputs_applied(
        created.turn.turn_id, applied_through_ordinal=1
    ) is True
    assert kernel.begin_finalizing_if_inputs_applied(
        created.turn.turn_id, applied_through_ordinal=1
    ) is True

    with pytest.raises(ConflictError, match="durable head"):
        kernel.begin_finalizing_if_inputs_applied(
            created.turn.turn_id, applied_through_ordinal=0
        )

    kernel.transition_turn(created.turn.turn_id, TurnStatus.COMPLETED)
    with pytest.raises(ConflictError, match="terminal Turn"):
        kernel.begin_finalizing_if_inputs_applied(
            created.turn.turn_id, applied_through_ordinal=1
        )


def test_concurrent_steer_and_finalization_have_one_serial_order(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    _, created = _turn(kernel)
    turn_id = created.turn.turn_id
    _advance_to_streaming(kernel, turn_id)
    start = threading.Barrier(3)

    def finalize() -> bool:
        start.wait()
        return kernel.begin_finalizing_if_inputs_applied(
            turn_id, applied_through_ordinal=0
        )

    def steer() -> str:
        start.wait()
        try:
            kernel.steer_turn(
                turn_id,
                SteerTurnRequest(
                    input="并发补充", client_message_id="concurrent-steer"
                ),
            )
        except ConflictError:
            return "rejected"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        finalization = executor.submit(finalize)
        admission = executor.submit(steer)
        start.wait()
        finalized = finalization.result(timeout=10)
        steer_outcome = admission.result(timeout=10)

    revisions = kernel.turn_inputs.list_for_turn(turn_id)
    if finalized:
        assert steer_outcome == "rejected"
        assert [revision.ordinal for revision in revisions] == [0]
        assert kernel.get_turn(turn_id).status is TurnStatus.FINALIZING
    else:
        assert steer_outcome == "accepted"
        assert [revision.ordinal for revision in revisions] == [0, 1]
        assert kernel.get_turn(turn_id).status is TurnStatus.STREAMING


def test_execution_batch_immutably_binds_contiguous_revisions_and_context(
    tmp_path,
) -> None:
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    _, created = _turn(kernel)
    for index in range(1, 3):
        kernel.steer_turn(
            created.turn.turn_id,
            SteerTurnRequest(
                input=f"补充 {index}", client_message_id=f"steer-{index}"
            ),
        )

    first_batch = kernel.turn_execution_batches.create(
        turn_id=created.turn.turn_id,
        first_revision_ordinal=0,
        last_revision_ordinal=0,
        snapshot_context=_context(),
    )
    assert kernel.turn_execution_batches.create(
        turn_id=created.turn.turn_id,
        first_revision_ordinal=0,
        last_revision_ordinal=0,
        snapshot_context=_context(),
    ) == first_batch

    with pytest.raises(ConflictError, match="does not continue"):
        kernel.turn_execution_batches.create(
            turn_id=created.turn.turn_id,
            first_revision_ordinal=2,
            last_revision_ordinal=2,
            snapshot_context=_context("gap"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="range is not contiguous"):
        with kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO turn_execution_batches("
                "batch_id, thread_id, turn_id, first_revision_ordinal, "
                "last_revision_ordinal, config_snapshot_id, capability_snapshot_id, "
                "permission_snapshot_id, model_catalog_snapshot_id, "
                "extension_snapshot_id, identity_sha256, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "bat_direct_gap",
                    created.turn.thread_id,
                    created.turn.turn_id,
                    2,
                    2,
                    "config-gap",
                    "capability-gap",
                    "permission-gap",
                    "models-gap",
                    "extensions-gap",
                    "a" * 64,
                    first_batch.created_at.isoformat(),
                ),
            )

    batch = kernel.turn_execution_batches.create(
        turn_id=created.turn.turn_id,
        first_revision_ordinal=1,
        last_revision_ordinal=2,
        snapshot_context=_context("two"),
    )
    duplicate = kernel.turn_execution_batches.create(
        turn_id=created.turn.turn_id,
        first_revision_ordinal=1,
        last_revision_ordinal=2,
        snapshot_context=_context("two"),
    )
    assert duplicate == batch
    assert first_batch.identity_sha256 and batch.identity_sha256

    with pytest.raises(ConflictError, match="not contiguous"):
        kernel.turn_execution_batches.create(
            turn_id=created.turn.turn_id,
            first_revision_ordinal=0,
            last_revision_ordinal=3,
            snapshot_context=_context(),
        )
    with pytest.raises(ConflictError, match="overlaps"):
        kernel.turn_execution_batches.create(
            turn_id=created.turn.turn_id,
            first_revision_ordinal=0,
            last_revision_ordinal=2,
            snapshot_context=_context("overlap"),
        )
    kernel.steer_turn(
        created.turn.turn_id,
        SteerTurnRequest(input="补充 3", client_message_id="steer-3"),
    )
    with pytest.raises(ConflictError, match="reused"):
        kernel.turn_execution_batches.create(
            turn_id=created.turn.turn_id,
            first_revision_ordinal=3,
            last_revision_ordinal=3,
            snapshot_context=_context("reused"),
            batch_id=batch.batch_id,
        )

    restarted = RuntimeKernel(path)
    assert restarted.turn_execution_batches.get(first_batch.batch_id) == first_batch
    assert restarted.turn_execution_batches.get(batch.batch_id) == batch
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with restarted.database.transaction() as connection:
            connection.execute(
                "UPDATE turn_execution_batches SET last_revision_ordinal = 1 "
                "WHERE batch_id = ?",
                (batch.batch_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with restarted.database.transaction() as connection:
            connection.execute(
                "DELETE FROM turn_execution_batches WHERE batch_id = ?",
                (batch.batch_id,),
            )
