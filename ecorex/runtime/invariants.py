"""Cross-domain consistency audit for the durable Runtime state graph.

The Runtime stores projections for efficient reads, but immutable events remain
the facts that produced them.  This module checks both representations from one
SQLite read snapshot.  It intentionally does not repair data: a silent repair
would destroy the evidence needed to diagnose a crash, stale owner, or broken
migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ecorex.protocol import (
    InteractionStatus,
    ItemStatus,
    JobStatus,
    TERMINAL_ITEM_STATUSES,
    TERMINAL_JOB_STATUSES,
    TERMINAL_TURN_STATUSES,
    TurnStatus,
)

from .database import SQLiteDatabase, json_loads


@dataclass(frozen=True, slots=True)
class RuntimeInvariantViolation:
    """One non-sensitive consistency failure.

    Details contain only protocol states and durable identifiers.  Prompts,
    tool arguments, model output, local paths, and credentials are never copied
    into the report.
    """

    code: str
    entity_type: str
    entity_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class RuntimeInvariantReport:
    checked_at: datetime
    event_count: int
    thread_count: int
    turn_count: int
    item_count: int
    job_count: int
    interaction_count: int
    violations: tuple[RuntimeInvariantViolation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    def raise_if_invalid(self) -> None:
        if self.violations:
            raise RuntimeInvariantError(self)


class RuntimeInvariantError(RuntimeError):
    def __init__(self, report: RuntimeInvariantReport) -> None:
        self.report = report
        codes = ", ".join(sorted({value.code for value in report.violations}))
        super().__init__(
            f"Runtime durable state violates {len(report.violations)} "
            f"invariant(s): {codes}"
        )


_JOB_EVENT_STATUS: dict[str, JobStatus] = {
    "job.queued": JobStatus.QUEUED,
    "job.leased": JobStatus.LEASED,
    "job.started": JobStatus.RUNNING,
    "job.waiting_human": JobStatus.WAITING_HUMAN,
    "job.resumed": JobStatus.QUEUED,
    "job.reclaimed": JobStatus.QUEUED,
    "job.retry_scheduled": JobStatus.RETRY_SCHEDULED,
    "job.completed": JobStatus.COMPLETED,
    "job.failed": JobStatus.FAILED,
    "job.cancelled": JobStatus.CANCELLED,
    "job.dead_lettered": JobStatus.DEAD_LETTER,
    "job.deadline_exceeded": JobStatus.FAILED,
}

_INTERACTION_EVENT_STATUS: dict[str, InteractionStatus] = {
    "interaction.requested": InteractionStatus.PENDING,
    "interaction.resolved": InteractionStatus.RESOLVED,
    "interaction.cancelled": InteractionStatus.CANCELLED,
    "interaction.expired": InteractionStatus.EXPIRED,
}


class RuntimeInvariantAuditor:
    """Validate Event/Turn/Item/Job/HITL state from one WAL snapshot."""

    def __init__(self, database: SQLiteDatabase | str) -> None:
        self.database = (
            database
            if isinstance(database, SQLiteDatabase)
            else SQLiteDatabase(database)
        )

    def audit(self) -> RuntimeInvariantReport:
        violations: list[RuntimeInvariantViolation] = []

        def add(code: str, kind: str, entity_id: object, detail: str) -> None:
            violations.append(
                RuntimeInvariantViolation(code, kind, str(entity_id), detail)
            )

        with self.database.reader() as connection:
            quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
            if quick_check is None or str(quick_check[0]).casefold() != "ok":
                add(
                    "sqlite_integrity_failure",
                    "database",
                    self.database.path.name,
                    "SQLite quick_check did not return ok",
                )
            for foreign_key in connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall():
                add(
                    "sqlite_foreign_key_failure",
                    "database",
                    self.database.path.name,
                    f"table={foreign_key[0]}, rowid={foreign_key[1]}",
                )
            threads = {
                str(row["thread_id"]): row
                for row in connection.execute("SELECT * FROM threads").fetchall()
            }
            turns = {
                str(row["turn_id"]): row
                for row in connection.execute("SELECT * FROM turns").fetchall()
            }
            items = {
                str(row["item_id"]): row
                for row in connection.execute("SELECT * FROM items").fetchall()
            }
            jobs = {
                str(row["job_id"]): row
                for row in connection.execute("SELECT * FROM jobs").fetchall()
            }
            interactions = {
                str(row["interaction_id"]): row
                for row in connection.execute("SELECT * FROM interactions").fetchall()
            }
            input_revisions = connection.execute(
                "SELECT thread_id, turn_id, ordinal FROM turn_input_revisions "
                "ORDER BY turn_id, ordinal"
            ).fetchall()
            execution_batches = {
                str(row["batch_id"]): row
                for row in connection.execute(
                    "SELECT * FROM turn_execution_batches "
                    "ORDER BY turn_id, first_revision_ordinal"
                ).fetchall()
            }
            heads = {
                str(row["thread_id"]): int(row["last_seq"])
                for row in connection.execute("SELECT * FROM thread_heads").fetchall()
            }

            event_count = 0
            event_max: dict[str, int] = {}
            event_counts: dict[str, int] = {}
            accepted_count: dict[str, int] = {}
            turn_state: dict[str, TurnStatus] = {}
            item_state: dict[str, ItemStatus] = {}
            job_state: dict[str, JobStatus] = {}
            interaction_state: dict[str, InteractionStatus] = {}
            accepted_context: dict[str, tuple[str | None, ...]] = {}

            cursor = connection.execute("SELECT * FROM events ORDER BY thread_id, seq")
            while batch := cursor.fetchmany(2048):
                for event in batch:
                    event_count += 1
                    thread_id = str(event["thread_id"])
                    sequence = int(event["seq"])
                    event_counts[thread_id] = event_counts.get(thread_id, 0) + 1
                    previous = event_max.get(thread_id, 0)
                    if sequence != previous + 1:
                        add(
                            "event_sequence_gap",
                            "thread",
                            thread_id,
                            f"expected seq {previous + 1}, observed {sequence}",
                        )
                    event_max[thread_id] = sequence

                    event_type = str(event["event_type"])
                    payload = json_loads(event["payload_json"], {})
                    if not isinstance(payload, dict):
                        add(
                            "event_payload_invalid",
                            "event",
                            event["event_id"],
                            "event payload is not an object",
                        )
                        payload = {}
                    turn_id = event["turn_id"]
                    item_id = event["item_id"]
                    job_id = event["job_id"]

                    if turn_id is not None:
                        turn_id = str(turn_id)
                        turn = turns.get(turn_id)
                        if turn is None:
                            add(
                                "event_turn_missing",
                                "event",
                                event["event_id"],
                                f"turn {turn_id} does not exist",
                            )
                        elif str(turn["thread_id"]) != thread_id:
                            add(
                                "event_turn_thread_mismatch",
                                "event",
                                event["event_id"],
                                f"turn {turn_id} belongs to another thread",
                            )
                    if item_id is not None and str(item_id) not in items:
                        add(
                            "event_item_missing",
                            "event",
                            event["event_id"],
                            f"item {item_id} does not exist",
                        )
                    if job_id is not None and str(job_id) not in jobs:
                        add(
                            "event_job_missing",
                            "event",
                            event["event_id"],
                            f"job {job_id} does not exist",
                        )

                    execution_batch_id = payload.get("execution_batch_id")
                    if (
                        event_type == "turn.execution_batch.bound"
                        and execution_batch_id is None
                    ):
                        add(
                            "execution_batch_reference_missing",
                            "event",
                            event["event_id"],
                            "execution batch binding has no batch reference",
                        )
                    if execution_batch_id is not None:
                        if (
                            not isinstance(execution_batch_id, str)
                            or not execution_batch_id
                        ):
                            add(
                                "execution_batch_reference_invalid",
                                "event",
                                event["event_id"],
                                "execution batch reference is not a durable ID",
                            )
                        else:
                            execution_batch = execution_batches.get(execution_batch_id)
                            if execution_batch is None:
                                add(
                                    "execution_batch_reference_missing",
                                    "event",
                                    event["event_id"],
                                    "referenced execution batch does not exist",
                                )
                            else:
                                scope_mismatch = (
                                    turn_id is None
                                    or str(turn_id) != str(execution_batch["turn_id"])
                                    or thread_id != str(execution_batch["thread_id"])
                                )
                                if scope_mismatch:
                                    add(
                                        "execution_batch_scope_mismatch",
                                        "event",
                                        event["event_id"],
                                        "event and execution batch use different scope",
                                    )
                                expected_batch_payload = {
                                    "first_revision_ordinal": int(
                                        execution_batch["first_revision_ordinal"]
                                    ),
                                    "last_revision_ordinal": int(
                                        execution_batch["last_revision_ordinal"]
                                    ),
                                    "config_snapshot_id": str(
                                        execution_batch["config_snapshot_id"]
                                    ),
                                    "capability_snapshot_id": str(
                                        execution_batch["capability_snapshot_id"]
                                    ),
                                    "permission_snapshot_id": str(
                                        execution_batch["permission_snapshot_id"]
                                    ),
                                    "model_catalog_snapshot_id": str(
                                        execution_batch["model_catalog_snapshot_id"]
                                    ),
                                    "extension_snapshot_id": str(
                                        execution_batch["extension_snapshot_id"]
                                    ),
                                }
                                drifted_fields = sorted(
                                    field
                                    for field, expected_value in expected_batch_payload.items()
                                    if field in payload
                                    and (
                                        type(payload[field]) is not type(expected_value)
                                        or payload[field] != expected_value
                                    )
                                )
                                if drifted_fields:
                                    add(
                                        "execution_batch_payload_drift",
                                        "event",
                                        event["event_id"],
                                        "batch payload differs in: "
                                        + ", ".join(drifted_fields),
                                    )
                                required_fields: set[str] = set()
                                if event_type == "turn.execution_batch.bound":
                                    required_fields = set(expected_batch_payload)
                                elif event_type in {
                                    "model.requested",
                                    "model.continuation_requested",
                                    "model.continuation_recovery_requested",
                                }:
                                    required_fields = {
                                        "first_revision_ordinal",
                                        "last_revision_ordinal",
                                    }
                                if required_fields:
                                    missing_fields = sorted(
                                        required_fields - set(payload)
                                    )
                                    if missing_fields:
                                        add(
                                            "execution_batch_payload_incomplete",
                                            "event",
                                            event["event_id"],
                                            "batch binding omits: "
                                            + ", ".join(missing_fields),
                                        )

                    if event_type == "turn.accepted" and turn_id is not None:
                        accepted_count[turn_id] = accepted_count.get(turn_id, 0) + 1
                        turn_state[turn_id] = TurnStatus.ACCEPTED
                        accepted_context[turn_id] = (
                            event["config_snapshot_id"],
                            event["capability_snapshot_id"],
                            event["permission_snapshot_id"],
                            event["extension_snapshot_id"],
                            event["trace_id"],
                        )
                    elif event_type == "turn.queued" and turn_id is not None:
                        turn_state[turn_id] = TurnStatus.QUEUED
                    elif event_type == "turn.status_changed" and turn_id is not None:
                        try:
                            prior = TurnStatus(str(payload["from"]))
                            target = TurnStatus(str(payload["to"]))
                        except (KeyError, ValueError, TypeError):
                            add(
                                "turn_event_invalid",
                                "event",
                                event["event_id"],
                                "status payload is invalid",
                            )
                        else:
                            derived = turn_state.get(turn_id)
                            if derived is not None and derived is not prior:
                                add(
                                    "turn_event_stale_from",
                                    "turn",
                                    turn_id,
                                    f"event expected {prior.value}, derived {derived.value}",
                                )
                            turn_state[turn_id] = target

                    if turn_id is not None and turn_id in accepted_context:
                        expected = accepted_context[turn_id]
                        observed = (
                            event["config_snapshot_id"],
                            event["capability_snapshot_id"],
                            event["permission_snapshot_id"],
                            event["extension_snapshot_id"],
                            event["trace_id"],
                        )
                        if any(
                            actual is not None and actual != wanted
                            for actual, wanted in zip(observed, expected, strict=True)
                        ):
                            add(
                                "turn_snapshot_context_drift",
                                "event",
                                event["event_id"],
                                f"event context differs from turn {turn_id}",
                            )

                    if item_id is not None:
                        item_id = str(item_id)
                        if event_type == "item.created":
                            try:
                                item_state[item_id] = ItemStatus(str(payload["status"]))
                            except (KeyError, ValueError, TypeError):
                                add(
                                    "item_event_invalid",
                                    "event",
                                    event["event_id"],
                                    "created status is invalid",
                                )
                        elif event_type == "turn.steered":
                            item_state[item_id] = ItemStatus.COMPLETED
                        elif event_type == "reasoning.replaced":
                            previous_item_id = payload.get("previous_item_id")
                            if previous_item_id is not None:
                                item_state[str(previous_item_id)] = ItemStatus.COMPLETED
                            item_state[item_id] = ItemStatus.IN_PROGRESS
                        elif event_type == "item.status_changed":
                            try:
                                prior = ItemStatus(str(payload["from"]))
                                target = ItemStatus(str(payload["to"]))
                            except (KeyError, ValueError, TypeError):
                                add(
                                    "item_event_invalid",
                                    "event",
                                    event["event_id"],
                                    "status payload is invalid",
                                )
                            else:
                                derived = item_state.get(item_id)
                                if derived is not None and derived is not prior:
                                    add(
                                        "item_event_stale_from",
                                        "item",
                                        item_id,
                                        f"event expected {prior.value}, derived {derived.value}",
                                    )
                                item_state[item_id] = target

                    if job_id is not None and event_type in _JOB_EVENT_STATUS:
                        job_state[str(job_id)] = _JOB_EVENT_STATUS[event_type]
                    if item_id is not None and event_type in _INTERACTION_EVENT_STATUS:
                        interaction_state[str(item_id)] = _INTERACTION_EVENT_STATUS[
                            event_type
                        ]

            for thread_id, last_seq in heads.items():
                maximum = event_max.get(thread_id, 0)
                count = event_counts.get(thread_id, 0)
                if last_seq != maximum or count != maximum:
                    add(
                        "event_head_mismatch",
                        "thread",
                        thread_id,
                        f"head={last_seq}, max={maximum}, count={count}",
                    )
            for thread_id in event_max.keys() - heads.keys():
                add(
                    "event_head_missing",
                    "thread",
                    thread_id,
                    "events exist without a thread head",
                )

            self._check_execution_batches(
                turns,
                input_revisions,
                execution_batches,
                add,
            )
            self._check_entities(
                threads,
                turns,
                items,
                jobs,
                interactions,
                accepted_count,
                turn_state,
                item_state,
                job_state,
                interaction_state,
                add,
            )

        violations.sort(
            key=lambda value: (
                value.code,
                value.entity_type,
                value.entity_id,
                value.detail,
            )
        )
        return RuntimeInvariantReport(
            checked_at=datetime.now(UTC),
            event_count=event_count,
            thread_count=len(threads),
            turn_count=len(turns),
            item_count=len(items),
            job_count=len(jobs),
            interaction_count=len(interactions),
            violations=tuple(violations),
        )

    @staticmethod
    def _check_execution_batches(
        turns: dict[str, Any],
        input_revisions: list[Any],
        execution_batches: dict[str, Any],
        add,
    ) -> None:
        revision_ordinals: dict[str, set[int]] = {}
        for revision in input_revisions:
            turn_id = str(revision["turn_id"])
            revision_ordinals.setdefault(turn_id, set()).add(int(revision["ordinal"]))

        batches_by_turn: dict[str, list[Any]] = {}
        for batch_id, batch in execution_batches.items():
            turn_id = str(batch["turn_id"])
            turn = turns.get(turn_id)
            if turn is None:
                add(
                    "execution_batch_turn_missing",
                    "execution_batch",
                    batch_id,
                    "execution batch Turn does not exist",
                )
            elif str(turn["thread_id"]) != str(batch["thread_id"]):
                add(
                    "execution_batch_thread_mismatch",
                    "execution_batch",
                    batch_id,
                    "execution batch and Turn use different threads",
                )
            batches_by_turn.setdefault(turn_id, []).append(batch)

        for turn_id, batches in batches_by_turn.items():
            expected_first = 0
            ordinals = revision_ordinals.get(turn_id, set())
            for batch in sorted(
                batches,
                key=lambda value: (
                    int(value["first_revision_ordinal"]),
                    int(value["last_revision_ordinal"]),
                ),
            ):
                batch_id = str(batch["batch_id"])
                first = int(batch["first_revision_ordinal"])
                last = int(batch["last_revision_ordinal"])
                if first != expected_first:
                    add(
                        "execution_batch_range_discontinuity",
                        "execution_batch",
                        batch_id,
                        f"expected first ordinal {expected_first}, observed {first}",
                    )
                missing = [
                    ordinal
                    for ordinal in range(first, last + 1)
                    if ordinal not in ordinals
                ]
                if missing:
                    add(
                        "execution_batch_revision_missing",
                        "execution_batch",
                        batch_id,
                        f"revision facts missing in bound range={len(missing)}",
                    )
                expected_first = last + 1

    @staticmethod
    def _check_entities(
        threads: dict[str, Any],
        turns: dict[str, Any],
        items: dict[str, Any],
        jobs: dict[str, Any],
        interactions: dict[str, Any],
        accepted_count: dict[str, int],
        turn_state: dict[str, TurnStatus],
        item_state: dict[str, ItemStatus],
        job_state: dict[str, JobStatus],
        interaction_state: dict[str, InteractionStatus],
        add,
    ) -> None:
        jobs_by_turn: dict[str, list[Any]] = {}
        items_by_turn: dict[str, list[Any]] = {}
        interactions_by_turn: dict[str, list[Any]] = {}

        for turn_id, turn in turns.items():
            thread_id = str(turn["thread_id"])
            if thread_id not in threads:
                add(
                    "turn_thread_missing",
                    "turn",
                    turn_id,
                    f"thread {thread_id} does not exist",
                )
            if accepted_count.get(turn_id, 0) != 1:
                add(
                    "turn_acceptance_count",
                    "turn",
                    turn_id,
                    f"accepted facts={accepted_count.get(turn_id, 0)}",
                )
            derived = turn_state.get(turn_id)
            actual = TurnStatus(str(turn["status"]))
            if derived is None:
                add("turn_event_missing", "turn", turn_id, "no lifecycle fact exists")
            elif derived is not actual:
                add(
                    "turn_projection_drift",
                    "turn",
                    turn_id,
                    f"projection={actual.value}, events={derived.value}",
                )

        for item_id, item in items.items():
            thread_id = str(item["thread_id"])
            turn_id = str(item["turn_id"])
            turn = turns.get(turn_id)
            if turn is None:
                add(
                    "item_turn_missing",
                    "item",
                    item_id,
                    f"turn {turn_id} does not exist",
                )
            elif str(turn["thread_id"]) != thread_id:
                add(
                    "item_thread_mismatch",
                    "item",
                    item_id,
                    f"item and turn {turn_id} use different threads",
                )
            items_by_turn.setdefault(turn_id, []).append(item)
            derived = item_state.get(item_id)
            actual = ItemStatus(str(item["status"]))
            if derived is None:
                add("item_event_missing", "item", item_id, "no lifecycle fact exists")
            elif derived is not actual:
                add(
                    "item_projection_drift",
                    "item",
                    item_id,
                    f"projection={actual.value}, events={derived.value}",
                )

        for job_id, job in jobs.items():
            turn_id = None if job["turn_id"] is None else str(job["turn_id"])
            thread_id = None if job["thread_id"] is None else str(job["thread_id"])
            if turn_id is not None:
                turn = turns.get(turn_id)
                if turn is None:
                    add(
                        "job_turn_missing",
                        "job",
                        job_id,
                        f"turn {turn_id} does not exist",
                    )
                elif thread_id != str(turn["thread_id"]):
                    add(
                        "job_thread_mismatch",
                        "job",
                        job_id,
                        f"job and turn {turn_id} use different threads",
                    )
                jobs_by_turn.setdefault(turn_id, []).append(job)
            actual = JobStatus(str(job["status"]))
            derived = job_state.get(job_id)
            if thread_id is not None:
                if derived is None:
                    add("job_event_missing", "job", job_id, "no lifecycle fact exists")
                elif derived is not actual:
                    add(
                        "job_projection_drift",
                        "job",
                        job_id,
                        f"projection={actual.value}, events={derived.value}",
                    )
            lease_values = (
                job["lease_owner"],
                job["lease_token"],
                job["lease_expires_at"],
                job["heartbeat_at"],
            )
            if actual in {JobStatus.LEASED, JobStatus.RUNNING}:
                if any(value is None or value == "" for value in lease_values):
                    add(
                        "job_lease_incomplete",
                        "job",
                        job_id,
                        f"{actual.value} requires owner/token/expiry/heartbeat",
                    )
            elif any(value is not None for value in lease_values):
                add(
                    "job_stale_lease",
                    "job",
                    job_id,
                    f"{actual.value} retained lease authority",
                )
            if int(job["attempt"]) > int(job["max_attempts"]):
                add(
                    "job_attempt_overflow",
                    "job",
                    job_id,
                    f"attempt={job['attempt']}, max={job['max_attempts']}",
                )

        for interaction_id, interaction in interactions.items():
            thread_id = str(interaction["thread_id"])
            turn_id = (
                None if interaction["turn_id"] is None else str(interaction["turn_id"])
            )
            job_id = (
                None if interaction["job_id"] is None else str(interaction["job_id"])
            )
            if thread_id not in threads:
                add(
                    "interaction_thread_missing",
                    "interaction",
                    interaction_id,
                    f"thread {thread_id} does not exist",
                )
            if turn_id is not None:
                turn = turns.get(turn_id)
                if turn is None:
                    add(
                        "interaction_turn_missing",
                        "interaction",
                        interaction_id,
                        f"turn {turn_id} does not exist",
                    )
                elif str(turn["thread_id"]) != thread_id:
                    add(
                        "interaction_thread_mismatch",
                        "interaction",
                        interaction_id,
                        f"interaction and turn {turn_id} use different threads",
                    )
                interactions_by_turn.setdefault(turn_id, []).append(interaction)
            if job_id is not None:
                job = jobs.get(job_id)
                if job is None:
                    add(
                        "interaction_job_missing",
                        "interaction",
                        interaction_id,
                        f"job {job_id} does not exist",
                    )
                elif str(job["thread_id"]) != thread_id or (
                    turn_id is not None and str(job["turn_id"]) != turn_id
                ):
                    add(
                        "interaction_job_mismatch",
                        "interaction",
                        interaction_id,
                        f"job {job_id} belongs to another execution scope",
                    )
            actual = InteractionStatus(str(interaction["status"]))
            derived = interaction_state.get(interaction_id)
            if derived is None:
                add(
                    "interaction_event_missing",
                    "interaction",
                    interaction_id,
                    "no lifecycle fact exists",
                )
            elif derived is not actual:
                add(
                    "interaction_projection_drift",
                    "interaction",
                    interaction_id,
                    f"projection={actual.value}, events={derived.value}",
                )

        for turn_id, turn in turns.items():
            status = TurnStatus(str(turn["status"]))
            turn_jobs = jobs_by_turn.get(turn_id, [])
            turn_items = items_by_turn.get(turn_id, [])
            turn_interactions = interactions_by_turn.get(turn_id, [])
            pending = [
                value
                for value in turn_interactions
                if InteractionStatus(str(value["status"])) is InteractionStatus.PENDING
            ]
            if status in TERMINAL_TURN_STATUSES:
                active_jobs = [
                    str(value["job_id"])
                    for value in turn_jobs
                    if JobStatus(str(value["status"])) not in TERMINAL_JOB_STATUSES
                ]
                active_items = [
                    str(value["item_id"])
                    for value in turn_items
                    if ItemStatus(str(value["status"])) not in TERMINAL_ITEM_STATUSES
                ]
                if active_jobs:
                    add(
                        "terminal_turn_active_job",
                        "turn",
                        turn_id,
                        f"active jobs={','.join(active_jobs)}",
                    )
                if active_items:
                    add(
                        "terminal_turn_active_item",
                        "turn",
                        turn_id,
                        f"active items={','.join(active_items)}",
                    )
                if pending:
                    add(
                        "terminal_turn_pending_interaction",
                        "turn",
                        turn_id,
                        f"pending interactions={len(pending)}",
                    )
            if status is TurnStatus.WAITING_HUMAN and not pending:
                add(
                    "waiting_turn_without_interaction",
                    "turn",
                    turn_id,
                    "waiting_human has no pending request",
                )
            for job in turn_jobs:
                job_id = str(job["job_id"])
                job_status = JobStatus(str(job["status"]))
                if str(job["kind"]) != "agent_turn":
                    continue
                if job_status is JobStatus.WAITING_HUMAN and (
                    status is not TurnStatus.WAITING_HUMAN
                    or not any(str(value["job_id"]) == job_id for value in pending)
                ):
                    add(
                        "waiting_job_without_interaction",
                        "job",
                        job_id,
                        f"turn={status.value}, matching pending request missing",
                    )
                if (
                    job_status is JobStatus.RETRY_SCHEDULED
                    and status is not TurnStatus.RETRY_WAIT
                ):
                    add(
                        "retry_job_turn_mismatch",
                        "job",
                        job_id,
                        f"retry_scheduled job has {status.value} turn",
                    )
                if (
                    job_status is JobStatus.COMPLETED
                    and status is not TurnStatus.COMPLETED
                ):
                    add(
                        "completed_job_turn_mismatch",
                        "job",
                        job_id,
                        f"completed job has {status.value} turn",
                    )
                if (
                    job_status in {JobStatus.FAILED, JobStatus.DEAD_LETTER}
                    and status is not TurnStatus.FAILED
                ):
                    add(
                        "failed_job_turn_mismatch",
                        "job",
                        job_id,
                        f"{job_status.value} job has {status.value} turn",
                    )


__all__ = [
    "RuntimeInvariantAuditor",
    "RuntimeInvariantError",
    "RuntimeInvariantReport",
    "RuntimeInvariantViolation",
]
