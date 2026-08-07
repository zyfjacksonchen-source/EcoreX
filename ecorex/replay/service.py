"""Side-effect-free UI replay and explicitly confirmed live replay."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sqlite3
import threading
from typing import Any

from ecorex.protocol import (
    ITEM_TRANSITIONS,
    JOB_TRANSITIONS,
    TERMINAL_TURN_STATUSES,
    TURN_TRANSITIONS,
    CreateTurnRequest,
    EventEnvelope,
    InteractionKind,
    InteractionContract,
    InteractionResponse,
    InteractionStatus,
    ItemKind,
    ItemProjection,
    ItemStatus,
    JobProjection,
    JobStatus,
    PublicToolActivity,
    RuntimeTiming,
    ReasoningItemContent,
    ReasoningPresentation,
    LiveReplayRequest,
    LiveReplayResponse,
    MockReplayResponse,
    ReplayInteractionProjection,
    SteerTurnRequest,
    ThreadProjection,
    ThreadProjectionResponse,
    ThreadStatus,
    TurnInputRevision,
    TurnMutationResponse,
    TurnProjection,
    TurnStatus,
)
from ecorex.runtime.composition import RuntimeComposition
from ecorex.runtime.database import json_dumps, json_loads
from ecorex.runtime.errors import ConflictError, NotFoundError
from ecorex.runtime.kernel import RuntimeKernel
from ecorex.runtime.public_tools import PublicToolActivityProjector
from ecorex.runtime.turn_inputs import intent_fingerprint


class ReplayIntegrityError(RuntimeError):
    """The Event Store cannot be replayed as one contiguous deterministic log."""


@dataclass(frozen=True, slots=True)
class _LoadedStream:
    events: tuple[EventEnvelope, ...]
    source_watermark: int
    through_seq: int


@dataclass(frozen=True, slots=True)
class _LiveReplaySource:
    accepted: EventEnvelope
    revisions: tuple[TurnInputRevision, ...]
    user_revisions: tuple[TurnInputRevision, ...]
    integrity_digest: str


@dataclass(slots=True)
class _ReducedState:
    thread: ThreadProjection
    turns: dict[str, TurnProjection]
    items: dict[str, ItemProjection]
    jobs: dict[str, JobProjection]
    interactions: dict[str, ReplayInteractionProjection]
    event_facts: list[dict[str, Any]]


def _copy_turn(turn: TurnProjection) -> TurnProjection:
    return turn.model_copy(update={"inherited": True})


def _copy_item(item: ItemProjection) -> ItemProjection:
    return item.model_copy(update={"inherited": True})


def _with_turn_timing(turn: TurnProjection) -> TurnProjection:
    if turn.timing is not None:
        return turn
    terminal = turn.status in TERMINAL_TURN_STATUSES
    return turn.model_copy(update={
        "timing": RuntimeTiming(
            started_at=turn.created_at,
            finished_at=turn.updated_at if terminal else None,
            duration_ms=(
                max(0, int((turn.updated_at - turn.created_at).total_seconds() * 1000))
                if terminal
                else None
            ),
        )
    })


def _with_server_timing(item: ItemProjection) -> ItemProjection:
    if item.kind is not ItemKind.TOOL_CALL:
        return item
    activity = PublicToolActivity.model_validate(item.content)
    if activity.timing is not None:
        return item
    terminal = item.status in {
        ItemStatus.COMPLETED,
        ItemStatus.FAILED,
        ItemStatus.CANCELLED,
    }
    activity = activity.model_copy(update={
        "timing": RuntimeTiming(
            started_at=item.created_at,
            finished_at=item.updated_at if terminal else None,
            duration_ms=(
                max(0, int((item.updated_at - item.created_at).total_seconds() * 1000))
                if terminal
                else None
            ),
        )
    })
    return item.model_copy(update={"content": activity.model_dump(mode="json")})


class ReplayService:
    """Replay one thread without running jobs, models, tools, or connectors."""

    def __init__(
        self,
        kernel: RuntimeKernel,
        *,
        composition: RuntimeComposition | None = None,
    ) -> None:
        self.kernel = kernel
        self.composition = composition
        self._live_lock = threading.RLock()

    def mock_replay(
        self, thread_id: str, *, through_seq: int | None = None
    ) -> MockReplayResponse:
        self.kernel.get_thread(thread_id)
        with self.kernel.database.reader() as connection:
            reduced, stream = self._reduce_thread(
                connection,
                thread_id,
                through_seq=through_seq,
                seen=set(),
            )
            local_turn_ids = {
                str(row["turn_id"])
                for row in connection.execute(
                    "SELECT turn_id FROM turns WHERE thread_id = ?",
                    (thread_id,),
                ).fetchall()
            }
        digest = hashlib.sha256(
            json_dumps(reduced.event_facts).encode("utf-8")
        ).hexdigest()
        projection = ThreadProjectionResponse(
            thread=reduced.thread,
            turns=[_with_turn_timing(turn) for turn in reduced.turns.values()],
            items=[_with_server_timing(item) for item in reduced.items.values()],
            jobs=list(reduced.jobs.values()),
            interactions=list(reduced.interactions.values()),
            watermark=stream.through_seq,
        )
        return MockReplayResponse(
            projection=projection,
            interactions=list(reduced.interactions.values()),
            live_replay_turn_ids=[
                turn.turn_id
                for turn in projection.turns
                if turn.turn_id in local_turn_ids
                and turn.status in TERMINAL_TURN_STATUSES
            ],
            source_watermark=stream.source_watermark,
            through_seq=stream.through_seq,
            event_count=len(reduced.event_facts),
            event_digest=digest,
        )

    def verified_events(
        self, thread_id: str, *, through_seq: int | None = None
    ) -> tuple[tuple[EventEnvelope, ...], int, int, str]:
        """Return one contiguous read-only event snapshot for derived projections."""

        with self.kernel.database.reader() as connection:
            stream = self._load_stream(connection, thread_id, through_seq)
        digest = hashlib.sha256(
            json_dumps(
                [event.model_dump(mode="json") for event in stream.events]
            ).encode("utf-8")
        ).hexdigest()
        return stream.events, stream.source_watermark, stream.through_seq, digest

    def live_replay(
        self, thread_id: str, request: LiveReplayRequest
    ) -> LiveReplayResponse:
        if self.composition is None:
            raise RuntimeError("live replay requires Runtime composition")
        self.kernel.get_thread(thread_id)
        client_message_id = self._live_replay_client_message_id(
            request.client_request_id
        )
        with self._live_lock:
            with self.kernel.database.reader() as connection:
                source = self._load_live_replay_source(
                    connection,
                    thread_id=thread_id,
                    source_turn_id=request.source_turn_id,
                )
                existing = self._existing_live_replay_in_transaction(
                    connection,
                    thread_id=thread_id,
                    source=source,
                    request=request,
                    client_message_id=client_message_id,
                )
            if existing is not None:
                return existing
            initial = source.user_revisions[0]
            turn_request = CreateTurnRequest(
                input=initial.input,
                agent_model_id=initial.agent_model_id,
                image_model_id=initial.image_model_id,
                explicit_tool_ids=list(initial.explicit_tool_ids),
                client_message_id=client_message_id,
                metadata=self._live_replay_metadata(
                    initial.metadata,
                    thread_id=thread_id,
                    source=source,
                    revision=initial,
                    client_request_id=request.client_request_id,
                ),
            )

            def accept(prepared):
                with self.kernel.jobs.control_transaction(
                    scope="live_replay",
                    subject=request.client_request_id,
                ) as connection:
                    durable_source = self._load_live_replay_source(
                        connection,
                        thread_id=thread_id,
                        source_turn_id=request.source_turn_id,
                    )
                    if durable_source.integrity_digest != source.integrity_digest:
                        raise ReplayIntegrityError(
                            "live replay source changed while authority was prepared"
                        )
                    existing = self._existing_live_replay_in_transaction(
                        connection,
                        thread_id=thread_id,
                        source=durable_source,
                        request=request,
                        client_message_id=client_message_id,
                    )
                    if existing is not None:
                        return existing
                    turn, job = self.kernel._create_turn_in_transaction(
                        connection,
                        thread_id=thread_id,
                        request=prepared.request,
                        snapshot_context=prepared.snapshot_context,
                        permission_account_id=(
                            self.composition.permission_account_id
                        ),
                        causation_id=durable_source.accepted.event_id,
                        correlation_id=request.client_request_id,
                    )
                    for revision in durable_source.user_revisions[1:]:
                        self.kernel._steer_turn_in_transaction(
                            connection,
                            turn_id=turn.turn_id,
                            request=SteerTurnRequest(
                                input=revision.input,
                                agent_model_id=prepared.request.agent_model_id,
                                image_model_id=prepared.request.image_model_id,
                                explicit_tool_ids=list(revision.explicit_tool_ids),
                                client_message_id=(
                                    self._live_replay_steer_client_message_id(
                                        request.client_request_id,
                                        revision.revision_id,
                                    )
                                ),
                                metadata=self._live_replay_metadata(
                                    revision.metadata,
                                    thread_id=thread_id,
                                    source=durable_source,
                                    revision=revision,
                                    client_request_id=request.client_request_id,
                                ),
                            ),
                        )
                    replay = TurnMutationResponse(
                        turn=turn,
                        job=job,
                        watermark=self.kernel.events.watermark(
                            thread_id, connection
                        ),
                    )
                return LiveReplayResponse(
                    source_thread_id=thread_id,
                    source_turn_id=request.source_turn_id,
                    causation_event_id=source.accepted.event_id,
                    replay=replay,
                    permission_snapshot_id=(
                        prepared.snapshot_context.permission_snapshot_id
                    ),
                    extension_snapshot_id=(
                        prepared.snapshot_context.extension_snapshot_id
                    ),
                )

            return self.composition.admit_turn(
                turn_request, accept, thread_id=thread_id
            )

    @staticmethod
    def _live_replay_client_message_id(client_request_id: str) -> str:
        digest = hashlib.sha256(client_request_id.encode("utf-8")).hexdigest()
        return f"live_replay:{digest}"

    @staticmethod
    def _live_replay_steer_client_message_id(
        client_request_id: str,
        source_revision_id: str,
    ) -> str:
        digest = hashlib.sha256(
            f"{client_request_id}\0{source_revision_id}".encode("utf-8")
        ).hexdigest()
        return f"live_replay_steer:{digest}"

    @staticmethod
    def _live_replay_metadata(
        metadata: dict[str, Any],
        *,
        thread_id: str,
        source: _LiveReplaySource,
        revision: TurnInputRevision,
        client_request_id: str,
    ) -> dict[str, Any]:
        provenance: dict[str, Any] = {
            "mode": "live",
            "source_thread_id": thread_id,
            "source_turn_id": source.accepted.turn_id,
            "source_event_id": source.accepted.event_id,
            "source_revision_id": revision.revision_id,
            "source_revision_ordinal": revision.ordinal,
            "client_request_id": client_request_id,
            "reuse_external_side_effects": False,
        }
        if "_replay" in metadata:
            # A replay of a replay retains the complete prior provenance instead
            # of silently replacing user/source metadata at the reserved key.
            provenance["source_replay"] = metadata["_replay"]
        return {**metadata, "_replay": provenance}

    def _load_live_replay_source(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        source_turn_id: str,
    ) -> _LiveReplaySource:
        turn = connection.execute(
            "SELECT * FROM turns WHERE thread_id = ? AND turn_id = ?",
            (thread_id, source_turn_id),
        ).fetchone()
        if turn is None:
            raise NotFoundError("live replay source Turn does not exist")
        try:
            status = TurnStatus(turn["status"])
        except (TypeError, ValueError) as error:
            raise ReplayIntegrityError("source Turn status is invalid") from error
        if status not in TERMINAL_TURN_STATUSES:
            raise ConflictError("only a terminal Turn can be live replayed")

        accepted_rows = connection.execute(
            "SELECT * FROM events WHERE thread_id = ? AND turn_id = ? "
            "AND event_type = 'turn.accepted' ORDER BY seq",
            (thread_id, source_turn_id),
        ).fetchall()
        if len(accepted_rows) != 1:
            raise ReplayIntegrityError(
                "source Turn must have exactly one acceptance fact"
            )
        try:
            accepted = self.kernel.events._from_row(accepted_rows[0])
            revisions = self.kernel.turn_inputs.list_for_turn_in_transaction(
                connection, source_turn_id
            )
            turn_metadata = json_loads(turn["metadata_json"], {})
        except (TypeError, ValueError) as error:
            raise ReplayIntegrityError("source Turn durable input is invalid") from error
        if not isinstance(turn_metadata, dict):
            raise ReplayIntegrityError("source Turn metadata is invalid")
        if not revisions:
            raise ReplayIntegrityError("source Turn has no initial input revision")
        if [revision.ordinal for revision in revisions] != list(range(len(revisions))):
            raise ReplayIntegrityError("source Turn revision ordinals are not contiguous")
        if sum(revision.source == "initial" for revision in revisions) != 1:
            raise ReplayIntegrityError(
                "source Turn must have exactly one initial input revision"
            )
        initial = revisions[0]
        if initial.source != "initial" or any(
            revision.source == "initial" for revision in revisions[1:]
        ):
            raise ReplayIntegrityError("source Turn initial revision order is invalid")
        if any(
            revision.thread_id != thread_id or revision.turn_id != source_turn_id
            for revision in revisions
        ):
            raise ReplayIntegrityError("source Turn revision identity is inconsistent")

        agent_model_id = turn["agent_model_id"]
        image_model_id = turn["image_model_id"]
        if not isinstance(agent_model_id, str) or not agent_model_id:
            raise ReplayIntegrityError("source Turn Agent model is invalid")
        if image_model_id is not None and (
            not isinstance(image_model_id, str) or not image_model_id
        ):
            raise ReplayIntegrityError("source Turn image model is invalid")
        if any(
            revision.agent_model_id != agent_model_id
            or revision.image_model_id != image_model_id
            for revision in revisions
        ):
            raise ReplayIntegrityError("source Turn revision model selection drifted")
        for revision in revisions:
            intent = (
                CreateTurnRequest(
                    input=revision.input,
                    agent_model_id=revision.agent_model_id,
                    image_model_id=revision.image_model_id,
                    explicit_tool_ids=list(revision.explicit_tool_ids),
                    client_message_id=revision.client_message_id,
                    metadata=revision.metadata,
                )
                if revision.source == "initial"
                else SteerTurnRequest(
                    input=revision.input,
                    agent_model_id=revision.agent_model_id,
                    image_model_id=revision.image_model_id,
                    explicit_tool_ids=list(revision.explicit_tool_ids),
                    client_message_id=revision.client_message_id,
                    metadata=revision.metadata,
                )
            )
            if intent_fingerprint(intent) != revision.intent_fingerprint:
                raise ReplayIntegrityError(
                    "source Turn revision fingerprint drifted"
                )

        payload = accepted.payload
        if (
            accepted.thread_id != thread_id
            or accepted.turn_id != source_turn_id
            or accepted.client_message_id != initial.client_message_id
            or turn["client_message_id"] != initial.client_message_id
            or turn["input_text"] != initial.input
            or turn_metadata != initial.metadata
            or payload.get("input") != initial.input
            or payload.get("agent_model_id") != agent_model_id
            or payload.get("image_model_id") != image_model_id
            or payload.get("explicit_tool_ids") != initial.explicit_tool_ids
            or payload.get("metadata") != initial.metadata
        ):
            raise ReplayIntegrityError(
                "source acceptance, Turn and initial revision drifted"
            )
        user_revisions = tuple(
            revision
            for revision in revisions
            if revision.source in {"initial", "steer"}
        )
        if not user_revisions or user_revisions[0] != initial:
            raise ReplayIntegrityError("source user input revision sequence is invalid")
        digest = hashlib.sha256(
            json_dumps(
                {
                    "turn": {
                        "turn_id": source_turn_id,
                        "thread_id": thread_id,
                        "input": turn["input_text"],
                        "agent_model_id": agent_model_id,
                        "image_model_id": image_model_id,
                        "client_message_id": turn["client_message_id"],
                        "metadata": turn_metadata,
                    },
                    "accepted": accepted.model_dump(mode="json"),
                    "revisions": [
                        revision.model_dump(mode="json") for revision in revisions
                    ],
                }
            ).encode("utf-8")
        ).hexdigest()
        return _LiveReplaySource(
            accepted=accepted,
            revisions=revisions,
            user_revisions=user_revisions,
            integrity_digest=digest,
        )

    def _existing_live_replay_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        source: _LiveReplaySource,
        request: LiveReplayRequest,
        client_message_id: str,
    ) -> LiveReplayResponse | None:
        row = connection.execute(
            "SELECT * FROM turns WHERE thread_id = ? AND client_message_id = ?",
            (thread_id, client_message_id),
        ).fetchone()
        if row is None:
            return None
        try:
            metadata = json_loads(row["metadata_json"], {})
        except (TypeError, ValueError) as error:
            raise ReplayIntegrityError("durable live replay metadata is invalid") from error
        replay_metadata = metadata.get("_replay", {}) if isinstance(metadata, dict) else {}
        if (
            not isinstance(replay_metadata, dict)
            or replay_metadata.get("mode") != "live"
            or replay_metadata.get("source_thread_id") != thread_id
            or replay_metadata.get("source_turn_id") != request.source_turn_id
        ):
            raise ConflictError(
                "live replay client_request_id was reused with different input"
            )

        accepted_rows = connection.execute(
            "SELECT * FROM events WHERE thread_id = ? AND turn_id = ? "
            "AND event_type = 'turn.accepted' ORDER BY seq",
            (thread_id, row["turn_id"]),
        ).fetchall()
        job_rows = connection.execute(
            "SELECT * FROM jobs WHERE turn_id = ? AND kind = 'agent_turn' "
            "ORDER BY created_at, job_id",
            (row["turn_id"],),
        ).fetchall()
        if len(accepted_rows) != 1 or len(job_rows) != 1:
            raise ReplayIntegrityError("durable live replay record is incomplete")
        try:
            accepted_event = self.kernel.events._from_row(accepted_rows[0])
            replay_revisions = self.kernel.turn_inputs.list_for_turn_in_transaction(
                connection, str(row["turn_id"])
            )
        except (TypeError, ValueError) as error:
            raise ReplayIntegrityError("durable live replay input is invalid") from error
        if len(replay_revisions) != len(source.user_revisions):
            raise ReplayIntegrityError("durable live replay revision count drifted")
        for replay_ordinal, (replayed, source_revision) in enumerate(
            zip(replay_revisions, source.user_revisions, strict=True)
        ):
            expected_source = "initial" if replay_ordinal == 0 else "steer"
            expected_client_message_id = (
                client_message_id
                if replay_ordinal == 0
                else self._live_replay_steer_client_message_id(
                    request.client_request_id,
                    source_revision.revision_id,
                )
            )
            expected_metadata = self._live_replay_metadata(
                source_revision.metadata,
                thread_id=thread_id,
                source=source,
                revision=source_revision,
                client_request_id=request.client_request_id,
            )
            replay_intent = (
                CreateTurnRequest(
                    input=source_revision.input,
                    agent_model_id=str(row["agent_model_id"]),
                    image_model_id=row["image_model_id"],
                    explicit_tool_ids=list(source_revision.explicit_tool_ids),
                    client_message_id=expected_client_message_id,
                    metadata=expected_metadata,
                )
                if replay_ordinal == 0
                else SteerTurnRequest(
                    input=source_revision.input,
                    agent_model_id=str(row["agent_model_id"]),
                    image_model_id=row["image_model_id"],
                    explicit_tool_ids=list(source_revision.explicit_tool_ids),
                    client_message_id=expected_client_message_id,
                    metadata=expected_metadata,
                )
            )
            if (
                replayed.ordinal != replay_ordinal
                or replayed.source != expected_source
                or replayed.thread_id != thread_id
                or replayed.turn_id != row["turn_id"]
                or replayed.input != source_revision.input
                or replayed.agent_model_id != row["agent_model_id"]
                or replayed.image_model_id != row["image_model_id"]
                or replayed.explicit_tool_ids != source_revision.explicit_tool_ids
                or replayed.metadata != expected_metadata
                or replayed.client_message_id != expected_client_message_id
                or replayed.intent_fingerprint != intent_fingerprint(replay_intent)
            ):
                raise ReplayIntegrityError("durable live replay revision drifted")
            item_rows = connection.execute(
                "SELECT * FROM items WHERE turn_id = ? AND client_message_id = ?",
                (row["turn_id"], expected_client_message_id),
            ).fetchall()
            if len(item_rows) != 1:
                raise ReplayIntegrityError("durable live replay user Item drifted")
            item = item_rows[0]
            expected_content = {
                "role": "user",
                "text": source_revision.input,
                "explicit_tool_ids": source_revision.explicit_tool_ids,
                "metadata": expected_metadata,
            }
            if replay_ordinal > 0:
                expected_content["steer"] = True
            if (
                item["kind"] != ItemKind.MESSAGE.value
                or item["status"] != ItemStatus.COMPLETED.value
                or json_loads(item["content_json"], {}) != expected_content
            ):
                raise ReplayIntegrityError("durable live replay user Item drifted")
            event_type = "item.created" if replay_ordinal == 0 else "turn.steered"
            item_events = connection.execute(
                "SELECT * FROM events WHERE thread_id = ? AND turn_id = ? "
                "AND item_id = ? AND event_type = ?",
                (thread_id, row["turn_id"], item["item_id"], event_type),
            ).fetchall()
            if len(item_events) != 1:
                raise ReplayIntegrityError("durable live replay user event drifted")
            item_event = self.kernel.events._from_row(item_events[0])
            expected_payload = (
                {
                    "kind": ItemKind.MESSAGE.value,
                    "status": ItemStatus.COMPLETED.value,
                    "content": expected_content,
                }
                if replay_ordinal == 0
                else {
                    "input": source_revision.input,
                    "agent_model_id": row["agent_model_id"],
                    "image_model_id": row["image_model_id"],
                    "explicit_tool_ids": source_revision.explicit_tool_ids,
                    "metadata": expected_metadata,
                }
            )
            if (
                item_event.client_message_id != expected_client_message_id
                or item_event.payload != expected_payload
            ):
                raise ReplayIntegrityError("durable live replay user event drifted")

        initial = replay_revisions[0]
        accepted_payload = accepted_event.payload
        if (
            accepted_event.causation_id != source.accepted.event_id
            or accepted_event.correlation_id != request.client_request_id
            or accepted_event.client_message_id != client_message_id
            or row["input_text"] != initial.input
            or row["metadata_json"] != json_dumps(initial.metadata)
            or accepted_payload.get("input") != initial.input
            or accepted_payload.get("agent_model_id") != row["agent_model_id"]
            or accepted_payload.get("image_model_id") != row["image_model_id"]
            or accepted_payload.get("explicit_tool_ids") != initial.explicit_tool_ids
            or accepted_payload.get("metadata") != initial.metadata
        ):
            raise ReplayIntegrityError("durable live replay acceptance drifted")

        job_row = job_rows[0]
        context = connection.execute(
            "SELECT * FROM job_runtime_contexts WHERE job_id = ?",
            (job_row["job_id"],),
        ).fetchone()
        snapshot_values = {
            "config_snapshot_id": accepted_event.config_snapshot_id,
            "capability_snapshot_id": accepted_event.capability_snapshot_id,
            "permission_snapshot_id": accepted_event.permission_snapshot_id,
            "model_catalog_snapshot_id": accepted_payload.get(
                "model_catalog_snapshot_id"
            ),
            "extension_snapshot_id": accepted_event.extension_snapshot_id,
        }
        if (
            context is None
            or any(
                not isinstance(value, str) or not value
                for value in snapshot_values.values()
            )
            or any(context[key] != value for key, value in snapshot_values.items())
        ):
            raise ReplayIntegrityError("durable live replay snapshot binding drifted")
        job = self.kernel.jobs._from_row(job_row)
        mutation = TurnMutationResponse(
            turn=self.kernel._turn_from_row(row),
            job=job,
            watermark=self.kernel.events.watermark(thread_id, connection),
        )
        return LiveReplayResponse(
            source_thread_id=thread_id,
            source_turn_id=request.source_turn_id,
            causation_event_id=source.accepted.event_id,
            replay=mutation,
            permission_snapshot_id=str(accepted_event.permission_snapshot_id),
            extension_snapshot_id=str(accepted_event.extension_snapshot_id),
        )

    def _load_stream(
        self,
        connection: sqlite3.Connection,
        thread_id: str,
        through_seq: int | None,
    ) -> _LoadedStream:
        head = connection.execute(
            "SELECT last_seq FROM thread_heads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if head is None:
            raise NotFoundError(f"thread {thread_id!r} has no Event Store stream")
        watermark = int(head["last_seq"])
        if watermark < 1:
            raise ReplayIntegrityError("thread Event Store watermark is empty")
        target = watermark if through_seq is None else through_seq
        if target < 1 or target > watermark:
            raise ConflictError(
                f"replay through_seq must be between 1 and source watermark {watermark}"
            )
        rows = connection.execute(
            "SELECT * FROM events WHERE thread_id = ? AND seq <= ? ORDER BY seq",
            (thread_id, target),
        ).fetchall()
        if len(rows) != target:
            raise ReplayIntegrityError(
                "thread Event Store contains a sequence gap or watermark mismatch"
            )
        events = tuple(self.kernel.events._from_row(row) for row in rows)
        for expected_seq, event in enumerate(events, start=1):
            if event.seq != expected_seq or event.thread_id != thread_id:
                raise ReplayIntegrityError("thread Event Store sequence is not contiguous")
        return _LoadedStream(events, watermark, target)

    def _reduce_thread(
        self,
        connection: sqlite3.Connection,
        thread_id: str,
        *,
        through_seq: int | None,
        seen: set[str],
    ) -> tuple[_ReducedState, _LoadedStream]:
        if thread_id in seen:
            raise ReplayIntegrityError("fork replay lineage contains a cycle")
        seen.add(thread_id)
        stream = self._load_stream(connection, thread_id, through_seq)
        first = stream.events[0]
        inherited_turns: dict[str, TurnProjection] = {}
        inherited_items: dict[str, ItemProjection] = {}
        inherited_interactions: dict[str, ReplayInteractionProjection] = {}
        inherited_facts: list[dict[str, Any]] = []
        if first.event_type == "thread.created":
            thread = ThreadProjection(
                thread_id=thread_id,
                status=ThreadStatus.ACTIVE,
                title=self._optional_string(first.payload.get("title")),
                metadata=self._object(first.payload.get("metadata")),
                created_at=first.created_at,
                updated_at=first.created_at,
            )
        elif first.event_type == "thread.forked":
            source_thread_id = self._required_string(
                first.payload.get("source_thread_id"), "fork source_thread_id"
            )
            source_seq = self._required_int(
                first.payload.get("source_seq"), "fork source_seq"
            )
            source_state, _source_stream = self._reduce_thread(
                connection,
                source_thread_id,
                through_seq=source_seq,
                seen=seen,
            )
            inherited_turns = {
                key: _copy_turn(value) for key, value in source_state.turns.items()
            }
            inherited_items = {
                key: _copy_item(value) for key, value in source_state.items.items()
            }
            inherited_interactions = dict(source_state.interactions)
            inherited_facts = source_state.event_facts
            thread = ThreadProjection(
                thread_id=thread_id,
                status=ThreadStatus.ACTIVE,
                title=self._optional_string(first.payload.get("title")),
                metadata=self._object(first.payload.get("metadata")),
                forked_from_thread_id=source_thread_id,
                forked_from_turn_id=self._optional_string(
                    first.payload.get("source_turn_id")
                ),
                forked_from_seq=source_seq,
                created_at=first.created_at,
                updated_at=first.created_at,
            )
        else:
            raise ReplayIntegrityError(
                "thread Event Store must begin with thread.created or thread.forked"
            )

        state = _ReducedState(
            thread=thread,
            turns=inherited_turns,
            items=inherited_items,
            jobs={},
            interactions=inherited_interactions,
            event_facts=[*inherited_facts],
        )
        for event in stream.events:
            state.event_facts.append(event.model_dump(mode="json"))
            self._apply_event(state, event)
        seen.remove(thread_id)
        return state, stream

    def _apply_event(self, state: _ReducedState, event: EventEnvelope) -> None:
        payload = event.payload
        if event.event_type in {"thread.created", "thread.forked"}:
            return
        state.thread = state.thread.model_copy(
            update={"updated_at": event.created_at}
        )
        if event.event_type == "thread.archived":
            state.thread = state.thread.model_copy(
                update={"status": ThreadStatus.ARCHIVED, "updated_at": event.created_at}
            )
            return
        if event.event_type == "thread.restored":
            state.thread = state.thread.model_copy(
                update={"status": ThreadStatus.ACTIVE}
            )
            return
        if event.event_type == "thread.deleted":
            state.thread = state.thread.model_copy(
                update={"status": ThreadStatus.DELETED}
            )
            return
        if event.event_type in {"thread.renamed", "thread.title_generated"}:
            state.thread = state.thread.model_copy(
                update={
                    "title": self._required_string(
                        payload.get("title"), "Thread title"
                    )
                }
            )
            return
        if event.event_type == "turn.accepted":
            if not event.turn_id or event.turn_id in state.turns:
                raise ReplayIntegrityError("turn.accepted identity is invalid")
            state.turns[event.turn_id] = TurnProjection(
                turn_id=event.turn_id,
                thread_id=event.thread_id,
                status=TurnStatus.ACCEPTED,
                input=self._required_string(payload.get("input"), "Turn input"),
                agent_model_id=self._required_string(
                    payload.get("agent_model_id"), "Turn Agent model ID"
                ),
                image_model_id=self._optional_string(
                    payload.get("image_model_id")
                ),
                client_message_id=event.client_message_id,
                metadata=self._object(payload.get("metadata")),
                created_at=event.created_at,
                updated_at=event.created_at,
            )
            return
        if event.event_type == "turn.queued":
            self._transition_turn(state, event, TurnStatus.QUEUED)
            return
        if event.event_type == "turn.status_changed":
            try:
                target = TurnStatus(self._required_string(payload.get("to"), "Turn status"))
            except ValueError as error:
                raise ReplayIntegrityError("Turn status fact is invalid") from error
            self._transition_turn(state, event, target)
            return
        if event.event_type == "job.queued":
            if not event.job_id or event.job_id in state.jobs:
                raise ReplayIntegrityError("job.queued identity is invalid")
            if event.turn_id is not None and event.turn_id not in state.turns:
                raise ReplayIntegrityError("job.queued references an unknown Turn")
            attempt = self._nonnegative_int(payload.get("attempt"), "job attempt")
            if attempt != 0:
                raise ReplayIntegrityError("new job attempt must be zero")
            try:
                state.jobs[event.job_id] = JobProjection.model_validate(
                    {
                        "job_id": event.job_id,
                        "kind": self._required_string(payload.get("kind"), "job kind"),
                        "status": JobStatus.QUEUED,
                        "priority": self._integer(payload.get("priority"), "job priority"),
                        "attempt": attempt,
                        "max_attempts": self._positive_int(
                            payload.get("max_attempts"), "job max attempts"
                        ),
                        "thread_id": event.thread_id,
                        "turn_id": event.turn_id,
                        "available_at": self._required_string(
                            payload.get("available_at"), "job availability"
                        ),
                        "deadline": self._optional_string(payload.get("deadline")),
                        "reason_code": None,
                        "created_at": event.created_at,
                        "updated_at": event.created_at,
                    }
                )
            except ValueError as error:
                raise ReplayIntegrityError("job.queued fact is invalid") from error
            return
        job_target = {
            "job.leased": JobStatus.LEASED,
            "job.started": JobStatus.RUNNING,
            "job.waiting_human": JobStatus.WAITING_HUMAN,
            "job.retry_scheduled": JobStatus.RETRY_SCHEDULED,
            "job.completed": JobStatus.COMPLETED,
            "job.failed": JobStatus.FAILED,
            "job.deadline_exceeded": JobStatus.FAILED,
            "job.cancelled": JobStatus.CANCELLED,
            "job.dead_lettered": JobStatus.DEAD_LETTER,
            "job.reclaimed": JobStatus.QUEUED,
            "job.resumed": JobStatus.QUEUED,
        }.get(event.event_type)
        if event.event_type == "job.heartbeat" or job_target is not None:
            if not event.job_id or event.job_id not in state.jobs:
                raise ReplayIntegrityError("job lifecycle fact is orphaned")
            job = state.jobs[event.job_id]
            target = job.status if job_target is None else job_target
            # Turn terminal settlement may close a queued dependent directly.
            # The event type is the public authority; the old internal
            # ``settled_by_turn`` flag is intentionally not exposed over SSE.
            terminal_settlement = (
                target
                in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
                and job.status
                not in {
                    JobStatus.COMPLETED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                    JobStatus.DEAD_LETTER,
                }
            )
            if (
                target != job.status
                and target not in JOB_TRANSITIONS[job.status]
                and not terminal_settlement
            ):
                raise ReplayIntegrityError("job status transition is invalid")
            updates: dict[str, Any] = {
                "status": target,
                "updated_at": event.created_at,
                "reason_code": self._job_reason_code(event.event_type, target),
            }
            if "attempt" in payload:
                updates["attempt"] = self._nonnegative_int(
                    payload.get("attempt"), "job attempt"
                )
            if "max_attempts" in payload:
                updates["max_attempts"] = self._positive_int(
                    payload.get("max_attempts"), "job max attempts"
                )
            if "available_at" in payload:
                updates["available_at"] = self._required_string(
                    payload.get("available_at"), "job availability"
                )
            try:
                state.jobs[event.job_id] = JobProjection.model_validate(
                    {**job.model_dump(), **updates}
                )
            except ValueError as error:
                raise ReplayIntegrityError("job lifecycle fact is invalid") from error
            return
        if event.event_type == "item.created":
            if not event.item_id or not event.turn_id or event.item_id in state.items:
                raise ReplayIntegrityError("item.created identity is invalid")
            if event.turn_id not in state.turns:
                raise ReplayIntegrityError("item.created references an unknown Turn")
            try:
                kind = ItemKind(self._required_string(payload.get("kind"), "Item kind"))
                status = ItemStatus(
                    self._required_string(payload.get("status"), "Item status")
                )
            except ValueError as error:
                raise ReplayIntegrityError("Item creation fact is invalid") from error
            content = self._object(payload.get("content"))
            if kind is ItemKind.TOOL_CALL:
                try:
                    activity = PublicToolActivity.model_validate(content)
                except ValueError:
                    raise ReplayIntegrityError(
                        "Tool Item public activity is invalid"
                    ) from None
                if activity.status != status.value:
                    raise ReplayIntegrityError(
                        "Tool Item public status is inconsistent"
                    )
                content = activity.model_dump(mode="json")
            state.items[event.item_id] = ItemProjection(
                item_id=event.item_id,
                thread_id=event.thread_id,
                turn_id=event.turn_id,
                kind=kind,
                status=status,
                content=content,
                created_seq=event.seq,
                created_at=event.created_at,
                updated_at=event.created_at,
            )
            return
        if event.event_type == "turn.steered":
            if not event.item_id or not event.turn_id or event.item_id in state.items:
                raise ReplayIntegrityError("turn.steered identity is invalid")
            turn = state.turns.get(event.turn_id)
            if turn is None or turn.status in TERMINAL_TURN_STATUSES:
                raise ReplayIntegrityError("turn.steered references an inactive Turn")
            state.items[event.item_id] = ItemProjection(
                item_id=event.item_id,
                thread_id=event.thread_id,
                turn_id=event.turn_id,
                kind=ItemKind.MESSAGE,
                status=ItemStatus.COMPLETED,
                content={
                    "role": "user",
                    "text": self._required_string(payload.get("input"), "steer input"),
                    "metadata": self._object(payload.get("metadata")),
                    "steer": True,
                },
                created_seq=event.seq,
                created_at=event.created_at,
                updated_at=event.created_at,
            )
            return
        if event.event_type == "reasoning.replaced":
            if not event.item_id or not event.turn_id or event.item_id in state.items:
                raise ReplayIntegrityError("reasoning.replaced identity is invalid")
            if event.turn_id not in state.turns:
                raise ReplayIntegrityError("reasoning.replaced references an unknown Turn")
            previous_item_id = self._optional_string(payload.get("previous_item_id"))
            if previous_item_id is not None:
                previous = state.items.get(previous_item_id)
                if previous is None or previous.kind is not ItemKind.REASONING:
                    raise ReplayIntegrityError(
                        "reasoning.replaced references an unknown reasoning Item"
                    )
                try:
                    previous_content = ReasoningItemContent.model_validate(previous.content)
                except ValueError as error:
                    raise ReplayIntegrityError(
                        "reasoning Item content is invalid"
                    ) from error
                previous_revision = self._required_int(
                    payload.get("previous_revision"), "previous reasoning revision"
                )
                if previous_revision != previous_content.revision + 1:
                    raise ReplayIntegrityError("previous reasoning revision is not contiguous")
                if payload.get("previous_presentation") != "archived":
                    raise ReplayIntegrityError(
                        "replaced reasoning must become explicitly archived"
                    )
                archived = previous_content.model_copy(
                    update={
                        "revision": previous_revision,
                        "presentation": ReasoningPresentation.ARCHIVED,
                        "archived_reason": "replaced_by_next_atom",
                    }
                )
                state.items[previous_item_id] = previous.model_copy(
                    update={
                        "status": ItemStatus.COMPLETED,
                        "content": archived.model_dump(mode="json"),
                        "updated_at": event.created_at,
                    }
                )
            try:
                content = ReasoningItemContent(
                    atom_id=self._required_string(
                        payload.get("atom_id"), "reasoning atom identity"
                    ),
                    text=self._required_string(
                        payload.get("delta"), "reasoning summary delta"
                    ),
                    revision=self._required_int(
                        payload.get("revision"), "reasoning revision"
                    ),
                    presentation=ReasoningPresentation(
                        self._required_string(
                            payload.get("presentation"), "reasoning presentation"
                        )
                    ),
                )
            except ValueError as error:
                raise ReplayIntegrityError("reasoning replacement fact is invalid") from error
            if (
                content.revision != 1
                or content.presentation is not ReasoningPresentation.VISIBLE
            ):
                raise ReplayIntegrityError("new reasoning Item must begin visible at revision 1")
            state.items[event.item_id] = ItemProjection(
                item_id=event.item_id,
                thread_id=event.thread_id,
                turn_id=event.turn_id,
                kind=ItemKind.REASONING,
                status=ItemStatus.IN_PROGRESS,
                content=content.model_dump(mode="json"),
                created_seq=event.seq,
                created_at=event.created_at,
                updated_at=event.created_at,
            )
            return
        if event.event_type == "reasoning.delta":
            item = self._item(state, event)
            if item.kind is not ItemKind.REASONING:
                raise ReplayIntegrityError("reasoning.delta references a non-reasoning Item")
            try:
                content = ReasoningItemContent.model_validate(item.content)
            except ValueError as error:
                raise ReplayIntegrityError("reasoning Item content is invalid") from error
            atom_id = self._required_string(payload.get("atom_id"), "reasoning atom")
            delta = self._required_string(payload.get("delta"), "reasoning delta")
            revision = self._required_int(payload.get("revision"), "reasoning revision")
            if (
                atom_id != content.atom_id
                or content.presentation is not ReasoningPresentation.VISIBLE
                or revision != content.revision + 1
            ):
                raise ReplayIntegrityError("reasoning delta violates atom or revision order")
            updated = content.model_copy(
                update={"text": content.text + delta, "revision": revision}
            )
            state.items[item.item_id] = item.model_copy(
                update={
                    "content": updated.model_dump(mode="json"),
                    "updated_at": event.created_at,
                }
            )
            return
        if event.event_type == "reasoning.archived":
            item = self._item(state, event)
            if item.kind is not ItemKind.REASONING:
                raise ReplayIntegrityError("reasoning.archive references a non-reasoning Item")
            try:
                content = ReasoningItemContent.model_validate(item.content)
                presentation = ReasoningPresentation(
                    self._required_string(
                        payload.get("presentation"), "reasoning presentation"
                    )
                )
            except ValueError as error:
                raise ReplayIntegrityError("reasoning archive fact is invalid") from error
            revision = self._required_int(payload.get("revision"), "reasoning revision")
            if (
                content.presentation is not ReasoningPresentation.VISIBLE
                or presentation is ReasoningPresentation.VISIBLE
                or revision != content.revision + 1
            ):
                raise ReplayIntegrityError("reasoning archive transition is invalid")
            updated = content.model_copy(
                update={
                    "revision": revision,
                    "presentation": presentation,
                    "archived_reason": self._required_string(
                        payload.get("reason"), "reasoning archive reason"
                    ),
                }
            )
            state.items[item.item_id] = item.model_copy(
                update={
                    "content": updated.model_dump(mode="json"),
                    "updated_at": event.created_at,
                }
            )
            return
        if event.event_type == "item.delta":
            item = self._item(state, event)
            delta = self._required_string(payload.get("delta"), "message delta")
            content = dict(item.content)
            content["text"] = str(content.get("text") or "") + delta
            state.items[item.item_id] = item.model_copy(
                update={"content": content, "updated_at": event.created_at}
            )
            return
        if event.event_type == "tool.result":
            item = self._item(state, event)
            if item.kind is not ItemKind.TOOL_CALL:
                raise ReplayIntegrityError("tool.result references a non-Tool Item")
            try:
                current = PublicToolActivity.model_validate(item.content)
                activity = PublicToolActivity.model_validate(payload.get("activity"))
            except ValueError:
                raise ReplayIntegrityError(
                    "Tool result public activity is invalid"
                ) from None
            if (
                current.tool_call_id != activity.tool_call_id
                or current.tool_id != activity.tool_id
                or current.argument_sha256 != activity.argument_sha256
                or event.tool_call_id != activity.tool_call_id
                or activity.phase != "completed"
            ):
                raise ReplayIntegrityError("Tool result public identity changed")
            state.items[item.item_id] = item.model_copy(
                update={
                    "content": activity.model_dump(mode="json"),
                    "updated_at": event.created_at,
                }
            )
            return
        if event.event_type == "item.status_changed":
            item = self._item(state, event)
            try:
                target = ItemStatus(
                    self._required_string(payload.get("to"), "Item status")
                )
            except ValueError as error:
                raise ReplayIntegrityError("Item status fact is invalid") from error
            self._assert_source_status(payload, item.status.value, "Item")
            if target != item.status and target not in ITEM_TRANSITIONS[item.status]:
                raise ReplayIntegrityError("Item status transition is invalid")
            updates: dict[str, Any] = {
                "status": target,
                "updated_at": event.created_at,
            }
            if item.kind is ItemKind.TOOL_CALL:
                try:
                    activity = PublicToolActivity.model_validate(item.content)
                    activity = PublicToolActivityProjector.transition(activity, target)
                except ValueError:
                    raise ReplayIntegrityError(
                        "Tool Item public lifecycle is invalid"
                    ) from None
                updates["content"] = activity.model_dump(mode="json")
            state.items[item.item_id] = item.model_copy(update=updates)
            return
        if event.event_type == "interaction.requested":
            if not event.item_id or event.item_id in state.interactions:
                raise ReplayIntegrityError("interaction.requested identity is invalid")
            if event.turn_id is not None and event.turn_id not in state.turns:
                raise ReplayIntegrityError(
                    "interaction.requested references an unknown Turn"
                )
            try:
                kind = InteractionKind(
                    self._required_string(payload.get("kind"), "interaction kind")
                )
            except ValueError as error:
                raise ReplayIntegrityError("interaction kind is invalid") from error
            options = payload.get("options", [])
            if not isinstance(options, list) or any(
                not isinstance(option, dict) for option in options
            ):
                raise ReplayIntegrityError("interaction options are invalid")
            try:
                contract = InteractionContract.model_validate(
                    self._object(payload.get("contract"))
                ).validate_for_kind(kind)
            except ValueError as error:
                raise ReplayIntegrityError("interaction contract is invalid") from error
            state.interactions[event.item_id] = ReplayInteractionProjection(
                interaction_id=event.item_id,
                kind=kind,
                status=InteractionStatus.PENDING,
                prompt=self._required_string(payload.get("prompt"), "interaction prompt"),
                contract=contract,
                options=options,
                response_client_request_id=None,
                thread_id=event.thread_id,
                turn_id=event.turn_id,
                job_id=event.job_id,
                expires_at=self._optional_string(payload.get("expires_at")),
                created_seq=event.seq,
                created_at=event.created_at,
                updated_at=event.created_at,
            )
            return
        interaction_status = {
            "interaction.resolved": InteractionStatus.RESOLVED,
            "interaction.cancelled": InteractionStatus.CANCELLED,
            "interaction.expired": InteractionStatus.EXPIRED,
        }.get(event.event_type)
        if interaction_status is not None:
            if not event.item_id or event.item_id not in state.interactions:
                raise ReplayIntegrityError("interaction terminal fact is orphaned")
            interaction = state.interactions[event.item_id]
            response = None
            response_client_request_id = None
            if interaction_status is InteractionStatus.RESOLVED:
                try:
                    response = interaction.contract.validate_response(
                        InteractionResponse.model_validate(
                            self._object(payload.get("response"))
                        )
                    )
                except ValueError as error:
                    raise ReplayIntegrityError(
                        "interaction response is invalid"
                    ) from error
                response_client_request_id = self._required_string(
                    payload.get("client_request_id"),
                    "interaction client request ID",
                )
                if event.correlation_id != response_client_request_id:
                    raise ReplayIntegrityError(
                        "interaction response correlation is inconsistent"
                    )
            state.interactions[event.item_id] = interaction.model_copy(
                update={
                    "status": interaction_status,
                    "response": response,
                    "response_client_request_id": response_client_request_id,
                    "updated_at": event.created_at,
                }
            )

    @staticmethod
    def _transition_turn(
        state: _ReducedState, event: EventEnvelope, target: TurnStatus
    ) -> None:
        if not event.turn_id or event.turn_id not in state.turns:
            raise ReplayIntegrityError("Turn status fact references an unknown Turn")
        turn = state.turns[event.turn_id]
        ReplayService._assert_source_status(event.payload, turn.status.value, "Turn")
        if target != turn.status and target not in TURN_TRANSITIONS[turn.status]:
            raise ReplayIntegrityError("Turn status transition is invalid")
        state.turns[event.turn_id] = turn.model_copy(
            update={
                "status": target,
                "terminal_reason": (
                    event.payload.get("reason")
                    if target in TERMINAL_TURN_STATUSES
                    else None
                ),
                "updated_at": event.created_at,
            }
        )

    @staticmethod
    def _item(state: _ReducedState, event: EventEnvelope) -> ItemProjection:
        if not event.item_id or event.item_id not in state.items:
            raise ReplayIntegrityError("Item fact references an unknown Item")
        return state.items[event.item_id]

    @staticmethod
    def _required_string(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value:
            raise ReplayIntegrityError(f"{label} is invalid")
        return value

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ReplayIntegrityError("optional Event Store string is invalid")
        return value

    @staticmethod
    def _required_int(value: Any, label: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ReplayIntegrityError(f"{label} is invalid")
        return value

    @staticmethod
    def _integer(value: Any, label: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ReplayIntegrityError(f"{label} is invalid")
        return value

    @staticmethod
    def _nonnegative_int(value: Any, label: str) -> int:
        parsed = ReplayService._integer(value, label)
        if parsed < 0:
            raise ReplayIntegrityError(f"{label} is invalid")
        return parsed

    @staticmethod
    def _positive_int(value: Any, label: str) -> int:
        parsed = ReplayService._integer(value, label)
        if parsed < 1:
            raise ReplayIntegrityError(f"{label} is invalid")
        return parsed

    @staticmethod
    def _job_reason_code(event_type: str, status: JobStatus) -> str | None:
        if event_type == "job.deadline_exceeded":
            return "deadline_exceeded"
        if status is JobStatus.CANCELLED:
            return "cancelled"
        if status is JobStatus.RETRY_SCHEDULED:
            return "retry_scheduled"
        if status is JobStatus.DEAD_LETTER:
            return "attempts_exhausted"
        if status is JobStatus.FAILED:
            return "execution_failed"
        return None

    @staticmethod
    def _object(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ReplayIntegrityError("Event Store object payload is invalid")
        return dict(value)

    @staticmethod
    def _assert_source_status(
        payload: dict[str, Any], expected: str, label: str
    ) -> None:
        source = payload.get("from")
        if not isinstance(source, str) or source != expected:
            raise ReplayIntegrityError(f"{label} transition source is invalid")
