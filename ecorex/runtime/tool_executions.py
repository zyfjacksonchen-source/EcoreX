"""Durable fencing records around model-requested tool executions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Literal, Mapping

from ecorex.capabilities.models import SandboxLevel
from ecorex.capabilities.service import (
    ToolExecutionScope,
    ToolInvocationAdmission,
)
from ecorex.protocol import PermissionSnapshot

from .database import SQLiteDatabase, json_dumps, json_loads


class ToolExecutionError(RuntimeError):
    pass


class ToolExecutionConflict(ToolExecutionError):
    pass


class StaleInvocationAdmission(ToolExecutionConflict):
    """Current permission changed before the admission INSERT linearized."""


class UncertainToolExecution(ToolExecutionError):
    pass


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    tool_call_id: str
    job_id: str
    turn_id: str
    execution_batch_id: str
    capability_snapshot_id: str
    policy_snapshot_id: str
    tool_id: str
    arguments: dict[str, Any]
    arguments_sha256: str
    idempotency_key: str | None
    status: Literal["started", "completed", "failed", "skipped"]
    attempt: int
    result: Any = None
    error_code: str | None = None

    @property
    def result_sha256(self) -> str | None:
        if self.result is None:
            return None
        return hashlib.sha256(json_dumps(self.result).encode("utf-8")).hexdigest()


class ToolExecutionRepository:
    def __init__(self, database: SQLiteDatabase | str | Path) -> None:
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )

    def begin(
        self,
        *,
        tool_call_id: str,
        job_id: str,
        turn_id: str,
        execution_batch_id: str,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        idempotency_key: str | None,
    ) -> tuple[ToolExecutionRecord, bool]:
        execution_identities = (
            tool_call_id,
            job_id,
            turn_id,
            execution_batch_id,
            capability_snapshot_id,
            policy_snapshot_id,
            tool_id,
        )
        if any(
            not isinstance(value, str) or not value.strip() or len(value) > 256
            for value in execution_identities
        ):
            raise ValueError("tool execution identity is invalid")
        arguments_json = json_dumps(arguments)
        digest = hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()
        now = datetime.now(UTC).isoformat()
        identity = (
            job_id,
            turn_id,
            execution_batch_id,
            capability_snapshot_id,
            policy_snapshot_id,
            tool_id,
            arguments_json,
            digest,
            idempotency_key,
        )
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM tool_executions WHERE tool_call_id = ?",
                (tool_call_id,),
            ).fetchone()
            if row is not None:
                actual = tuple(
                    row[key]
                    for key in (
                        "job_id",
                        "turn_id",
                        "execution_batch_id",
                        "capability_snapshot_id",
                        "policy_snapshot_id",
                        "tool_id",
                        "arguments_json",
                        "arguments_sha256",
                        "idempotency_key",
                    )
                )
                if actual != identity:
                    raise ToolExecutionConflict(
                        "tool_call_id was reused with different execution identity"
                    )
                return self._from_row(row), False
            scope = connection.execute(
                "SELECT job.turn_id AS job_turn_id, batch.turn_id AS batch_turn_id "
                "FROM jobs AS job JOIN turn_execution_batches AS batch "
                "ON batch.batch_id = ? WHERE job.job_id = ?",
                (execution_batch_id, job_id),
            ).fetchone()
            if (
                scope is None
                or scope["job_turn_id"] != turn_id
                or scope["batch_turn_id"] != turn_id
            ):
                raise ToolExecutionConflict(
                    "tool execution does not match its durable execution batch"
                )
            connection.execute(
                "INSERT INTO tool_executions("
                "tool_call_id, job_id, turn_id, execution_batch_id, capability_snapshot_id, "
                "policy_snapshot_id, tool_id, arguments_json, arguments_sha256, "
                "idempotency_key, status, attempt, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', 1, ?, ?)",
                (tool_call_id, *identity, now, now),
            )
            row = connection.execute(
                "SELECT * FROM tool_executions WHERE tool_call_id = ?",
                (tool_call_id,),
            ).fetchone()
            return self._from_row(row), True

    def resume_uncertain(self, tool_call_id: str) -> ToolExecutionRecord:
        with self.database.transaction() as connection:
            row = self._require(connection, tool_call_id)
            if row["status"] != "started":
                return self._from_row(row)
            connection.execute(
                "UPDATE tool_executions SET attempt = attempt + 1, updated_at = ? "
                "WHERE tool_call_id = ?",
                (datetime.now(UTC).isoformat(), tool_call_id),
            )
            return self._from_row(self._require(connection, tool_call_id))

    def admit(
        self,
        *,
        tool_call_id: str,
        job_id: str,
        thread_id: str,
        turn_id: str,
        execution_batch_id: str,
        capability_snapshot_id: str,
        permission_account_id: str,
        frozen_permission_snapshot_id: str,
        current_permission_snapshot_id: str,
        current_permission_state_digest: str,
        current_admin_hard_denies: tuple[str, ...],
        current_availability_digest: str | None,
        tool_id: str,
        tool_version: str,
        approved: bool,
        approval_interaction_id: str | None,
        effective_sandbox: SandboxLevel,
    ) -> ToolInvocationAdmission:
        """Persist the one append-only side-effect admission for a tool call.

        Callers must hold the product PermissionAuthority mutation lock while
        computing current governance and invoking this method.  The INSERT is
        the dispatch linearization point: a crash before it is provably safe to
        resume, while a non-idempotent crash after it is conservatively
        uncertain.
        """

        identities = (
            tool_call_id,
            job_id,
            thread_id,
            turn_id,
            execution_batch_id,
            capability_snapshot_id,
            permission_account_id,
            frozen_permission_snapshot_id,
            current_permission_snapshot_id,
            tool_id,
            tool_version,
        )
        if any(
            not isinstance(value, str) or not value.strip() or len(value) > 256
            for value in identities
        ):
            raise ValueError("tool invocation admission identity is invalid")
        if not isinstance(approved, bool) or not isinstance(
            effective_sandbox, SandboxLevel
        ):
            raise ValueError("tool invocation admission authority is invalid")
        for label, digest in (
            ("permission state", current_permission_state_digest),
            ("availability", current_availability_digest),
        ):
            if digest is not None and (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"tool invocation {label} digest is invalid")
        if current_permission_state_digest is None:
            raise ValueError("tool invocation permission state digest is required")
        if (
            not isinstance(current_admin_hard_denies, tuple)
            or tuple(sorted(set(current_admin_hard_denies)))
            != current_admin_hard_denies
        ):
            raise ValueError("administrator hard-deny authority is invalid")
        if approval_interaction_id is not None and (
            not isinstance(approval_interaction_id, str)
            or not approval_interaction_id.strip()
            or len(approval_interaction_id) > 256
        ):
            raise ValueError("tool invocation approval identity is invalid")
        if approved and approval_interaction_id is None:
            raise ValueError("approved invocation requires its durable interaction")
        if not approved and approval_interaction_id is not None:
            raise ValueError("unapproved invocation cannot bind an approval interaction")

        with self.database.transaction() as connection:
            execution = self._require(connection, tool_call_id)
            expected_execution = (
                job_id,
                turn_id,
                execution_batch_id,
                capability_snapshot_id,
                frozen_permission_snapshot_id,
                tool_id,
            )
            actual_execution = tuple(
                execution[key]
                for key in (
                    "job_id",
                    "turn_id",
                    "execution_batch_id",
                    "capability_snapshot_id",
                    "policy_snapshot_id",
                    "tool_id",
                )
            )
            if actual_execution != expected_execution:
                raise ToolExecutionConflict(
                    "invocation admission does not match its tool execution"
                )
            if execution["status"] != "started":
                raise ToolExecutionConflict(
                    "only a started tool execution can be admitted"
                )
            scope = connection.execute(
                "SELECT job.thread_id AS job_thread_id, job.turn_id AS job_turn_id, "
                "job.checkpoint_json AS job_checkpoint_json, "
                "turn.thread_id AS turn_thread_id "
                "FROM jobs AS job JOIN turns AS turn ON turn.turn_id = ? "
                "WHERE job.job_id = ?",
                (turn_id, job_id),
            ).fetchone()
            if (
                scope is None
                or scope["job_thread_id"] != thread_id
                or scope["job_turn_id"] != turn_id
                or scope["turn_thread_id"] != thread_id
            ):
                raise ToolExecutionConflict("invocation admission scope is invalid")
            permission_state = connection.execute(
                "SELECT state.profile, state.revision, state.updated_at, "
                "state.state_digest FROM runtime_permission_state AS state "
                "JOIN permission_state_ledger AS ledger "
                "ON ledger.account_id = state.account_id "
                "AND ledger.revision = state.revision "
                "AND ledger.profile = state.profile "
                "AND ledger.state_digest = state.state_digest "
                "AND ledger.created_at = state.updated_at "
                "WHERE state.account_id = ?",
                (permission_account_id,),
            ).fetchone()
            if permission_state is None:
                raise ToolExecutionConflict(
                    "current permission ledger state is unavailable"
                )
            if permission_state["state_digest"] != current_permission_state_digest:
                raise StaleInvocationAdmission(
                    "current permission changed before invocation admission"
                )
            try:
                expected_permission_snapshot_id = PermissionSnapshot.issue(
                    profile=str(permission_state["profile"]),
                    revision=int(permission_state["revision"]),
                    updated_at=datetime.fromisoformat(
                        str(permission_state["updated_at"])
                    ),
                    admin_hard_denies=current_admin_hard_denies,
                ).snapshot_id
            except (TypeError, ValueError) as error:
                raise ToolExecutionConflict(
                    "current permission snapshot cannot be reconstructed"
                ) from error
            if expected_permission_snapshot_id != current_permission_snapshot_id:
                raise StaleInvocationAdmission(
                    "current permission snapshot changed before invocation admission"
                )
            batch = connection.execute(
                "SELECT turn_id FROM turn_execution_batches WHERE batch_id = ?",
                (execution_batch_id,),
            ).fetchone()
            if batch is None or batch["turn_id"] != turn_id:
                raise ToolExecutionConflict(
                    "invocation admission execution batch is invalid"
                )
            if approved:
                interaction = connection.execute(
                    "SELECT job_id, thread_id, turn_id, status, kind, response_json "
                    "FROM interactions "
                    "WHERE interaction_id = ?",
                    (approval_interaction_id,),
                ).fetchone()
                if (
                    interaction is None
                    or interaction["job_id"] != job_id
                    or interaction["thread_id"] != thread_id
                    or interaction["turn_id"] != turn_id
                    or interaction["status"] != "resolved"
                    or interaction["kind"] != "permission_approval"
                ):
                    raise ToolExecutionConflict(
                        "invocation approval interaction is not authoritative"
                    )
                response = json_loads(interaction["response_json"], {})
                checkpoint = json_loads(scope["job_checkpoint_json"], {})
                raw_tool_call = checkpoint.get("tool_call")
                if (
                    not isinstance(response, dict)
                    or response.get("action_id") != "allow"
                    or not isinstance(raw_tool_call, dict)
                    or checkpoint.get("phase")
                    not in {"waiting_tool_approval", "tool_running"}
                    or checkpoint.get("approved") is not True
                    or checkpoint.get(
                        "approval_interaction_id",
                        checkpoint.get("interaction_id"),
                    )
                    != approval_interaction_id
                    or checkpoint.get("execution_batch_id") != execution_batch_id
                    or raw_tool_call.get("tool_name") != tool_id
                    or not isinstance(raw_tool_call.get("tool_call_id"), str)
                    or "arguments" not in raw_tool_call
                ):
                    raise ToolExecutionConflict(
                        "invocation approval does not bind this tool call"
                    )
                expected_execution_id = "tool_exec_" + hashlib.sha256(
                    (
                        f"{turn_id}\0{raw_tool_call['tool_call_id']}"
                    ).encode("utf-8")
                ).hexdigest()
                checkpoint_arguments_sha256 = hashlib.sha256(
                    json_dumps(raw_tool_call["arguments"]).encode("utf-8")
                ).hexdigest()
                if (
                    expected_execution_id != tool_call_id
                    or checkpoint_arguments_sha256
                    != execution["arguments_sha256"]
                ):
                    raise ToolExecutionConflict(
                        "invocation approval arguments do not match execution"
                    )

            existing = connection.execute(
                "SELECT * FROM invocation_admissions WHERE tool_call_id = ?",
                (tool_call_id,),
            ).fetchone()
            if existing is not None:
                admission = self._admission_from_row(existing)
                expected = (
                    job_id,
                    thread_id,
                    turn_id,
                    execution_batch_id,
                    capability_snapshot_id,
                    permission_account_id,
                    frozen_permission_snapshot_id,
                    current_permission_snapshot_id,
                    current_permission_state_digest,
                    current_availability_digest,
                    tool_id,
                    tool_version,
                    str(execution["arguments_sha256"]),
                    execution["idempotency_key"],
                    approved,
                    approval_interaction_id,
                    effective_sandbox,
                )
                actual = (
                    admission.execution_scope.job_id,
                    admission.execution_scope.thread_id,
                    admission.execution_scope.turn_id,
                    str(existing["execution_batch_id"]),
                    admission.capability_snapshot_id,
                    str(existing["permission_account_id"]),
                    admission.frozen_policy_snapshot_id,
                    admission.current_policy_snapshot_id,
                    admission.current_permission_state_digest,
                    admission.current_availability_digest,
                    admission.tool_id,
                    admission.tool_version,
                    admission.arguments_sha256,
                    admission.idempotency_key,
                    admission.approved,
                    existing["approval_interaction_id"],
                    admission.effective_sandbox,
                )
                if actual != expected:
                    raise ToolExecutionConflict(
                        "tool call already has a different invocation admission"
                    )
                return admission

            admitted_at = datetime.now(UTC).isoformat()
            permit_payload = {
                "schema_version": 1,
                "tool_call_id": tool_call_id,
                "job_id": job_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "execution_batch_id": execution_batch_id,
                "capability_snapshot_id": capability_snapshot_id,
                "permission_account_id": permission_account_id,
                "frozen_permission_snapshot_id": frozen_permission_snapshot_id,
                "current_permission_snapshot_id": current_permission_snapshot_id,
                "current_permission_state_digest": current_permission_state_digest,
                "current_availability_digest": current_availability_digest,
                "tool_id": tool_id,
                "tool_version": tool_version,
                "arguments_sha256": str(execution["arguments_sha256"]),
                "idempotency_key": execution["idempotency_key"],
                "approved": approved,
                "approval_interaction_id": approval_interaction_id,
                "effective_sandbox": effective_sandbox.value,
                "admitted_at": admitted_at,
            }
            permit_digest = hashlib.sha256(
                b"ecorex-invocation-admission-v1\0"
                + json_dumps(permit_payload).encode("utf-8")
            ).hexdigest()
            permit_id = "permit_" + permit_digest
            connection.execute(
                "INSERT INTO invocation_admissions("
                "tool_call_id, permit_id, job_id, thread_id, turn_id, "
                "execution_batch_id, capability_snapshot_id, "
                "permission_account_id, frozen_permission_snapshot_id, "
                "current_permission_snapshot_id, current_permission_state_digest, "
                "current_availability_digest, "
                "tool_id, tool_version, arguments_sha256, idempotency_key, "
                "approved, approval_interaction_id, effective_sandbox, admitted_at, "
                "permit_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tool_call_id,
                    permit_id,
                    job_id,
                    thread_id,
                    turn_id,
                    execution_batch_id,
                    capability_snapshot_id,
                    permission_account_id,
                    frozen_permission_snapshot_id,
                    current_permission_snapshot_id,
                    current_permission_state_digest,
                    current_availability_digest,
                    tool_id,
                    tool_version,
                    str(execution["arguments_sha256"]),
                    execution["idempotency_key"],
                    int(approved),
                    approval_interaction_id,
                    effective_sandbox.value,
                    admitted_at,
                    permit_digest,
                ),
            )
            row = connection.execute(
                "SELECT * FROM invocation_admissions WHERE tool_call_id = ?",
                (tool_call_id,),
            ).fetchone()
            assert row is not None
            return self._admission_from_row(row)

    def admission(self, tool_call_id: str) -> ToolInvocationAdmission | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM invocation_admissions WHERE tool_call_id = ?",
                (tool_call_id,),
            ).fetchone()
        return None if row is None else self._admission_from_row(row)

    def complete(self, tool_call_id: str, result: Any) -> ToolExecutionRecord:
        result_json = json_dumps(result)
        with self.database.transaction() as connection:
            row = self._require(connection, tool_call_id)
            if row["status"] == "completed":
                if row["result_json"] != result_json:
                    raise ToolExecutionConflict(
                        "completed tool call was replayed with a different result"
                    )
                return self._from_row(row)
            if row["status"] != "started":
                raise ToolExecutionConflict(
                    f"tool call cannot complete from {row['status']}"
                )
            connection.execute(
                "UPDATE tool_executions SET status = 'completed', result_json = ?, "
                "error_code = NULL, updated_at = ? WHERE tool_call_id = ?",
                (result_json, datetime.now(UTC).isoformat(), tool_call_id),
            )
            return self._from_row(self._require(connection, tool_call_id))

    def skip(self, tool_call_id: str, *, reason: str) -> ToolExecutionRecord:
        with self.database.transaction() as connection:
            row = self._require(connection, tool_call_id)
            if row["status"] not in {"started", "skipped"}:
                raise ToolExecutionConflict(
                    f"tool call cannot be skipped from {row['status']}"
                )
            connection.execute(
                "UPDATE tool_executions SET status = 'skipped', result_json = ?, "
                "error_code = ?, updated_at = ? WHERE tool_call_id = ?",
                (
                    json_dumps({"skipped": True, "reason": reason}),
                    reason[:128],
                    datetime.now(UTC).isoformat(),
                    tool_call_id,
                ),
            )
            return self._from_row(self._require(connection, tool_call_id))

    def fail(self, tool_call_id: str, *, error_code: str) -> ToolExecutionRecord:
        with self.database.transaction() as connection:
            row = self._require(connection, tool_call_id)
            if row["status"] == "completed":
                raise ToolExecutionConflict("completed tool calls cannot fail")
            connection.execute(
                "UPDATE tool_executions SET status = 'failed', error_code = ?, "
                "updated_at = ? WHERE tool_call_id = ?",
                (
                    error_code[:128],
                    datetime.now(UTC).isoformat(),
                    tool_call_id,
                ),
            )
            return self._from_row(self._require(connection, tool_call_id))

    def get(self, tool_call_id: str) -> ToolExecutionRecord:
        with self.database.reader() as connection:
            row = self._require(connection, tool_call_id)
            return self._from_row(row)

    def completed_for_job(
        self,
        job_id: str,
        *,
        execution_batch_id: str,
        tool_ids: tuple[str, ...],
        limit: int = 256,
    ) -> tuple[ToolExecutionRecord, ...]:
        """Return bounded, durable discovery facts for one leased Job.

        Progressive disclosure is reconstructed from completed tool records,
        not an in-memory set.  A Runtime restart between search and the next
        model round therefore exposes exactly the same snapshot-bound tools.
        """

        if (
            not isinstance(job_id, str)
            or not job_id
            or not isinstance(execution_batch_id, str)
            or not execution_batch_id
            or not isinstance(tool_ids, tuple)
            or not 1 <= len(tool_ids) <= 16
            or any(not isinstance(tool_id, str) or not tool_id for tool_id in tool_ids)
            or not 1 <= limit <= 1024
        ):
            raise ValueError("tool execution discovery query is invalid")
        placeholders = ",".join("?" for _ in tool_ids)
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_executions "
                "WHERE job_id = ? AND execution_batch_id = ? "
                "AND status = 'completed' "
                f"AND tool_id IN ({placeholders}) "
                "ORDER BY created_at, tool_call_id LIMIT ?",
                (job_id, execution_batch_id, *tool_ids, limit),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def completed_search_for_discovery(
        self,
        *,
        execution_scope: ToolExecutionScope,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        discovery_id: str,
    ) -> ToolExecutionRecord | None:
        """Return the durable search fact that emitted one exact discovery ID."""

        parsed = self._parse_discovery_id(discovery_id)
        if (
            not self._durable_scope(execution_scope)
            or parsed is None
            or any(
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 256
                for value in (capability_snapshot_id, policy_snapshot_id)
            )
        ):
            return None
        with self.database.reader() as connection:
            rows = self._completed_scope_rows(
                connection,
                execution_scope=execution_scope,
                capability_snapshot_id=capability_snapshot_id,
                policy_snapshot_id=policy_snapshot_id,
                tool_id="tool_search",
            )
        for row in reversed(rows):
            record = self._from_row(row)
            if self._record_search_contains(
                record,
                capability_snapshot_id=capability_snapshot_id,
                discovery_id=discovery_id,
            ):
                return record
        return None

    def completed_skill_search_for_discovery(
        self,
        *,
        execution_scope: ToolExecutionScope,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        extension_snapshot_id: str,
        extension_contribution_snapshot_id: str,
        discovery_id: str,
    ) -> ToolExecutionRecord | None:
        """Resolve the exact durable Skill search fact for one batch.

        This lookup proves only that the immutable result emitted the exact
        resource identity.  The Skill Runtime subsequently recomputes the
        entire search projection before reading any content.
        """

        parsed = self._parse_skill_discovery_id(discovery_id)
        identities = (
            capability_snapshot_id,
            policy_snapshot_id,
            extension_snapshot_id,
            extension_contribution_snapshot_id,
        )
        if (
            not self._durable_scope(execution_scope)
            or parsed is None
            or any(
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 256
                for value in identities
            )
        ):
            return None
        with self.database.reader() as connection:
            rows = self._completed_scope_rows(
                connection,
                execution_scope=execution_scope,
                capability_snapshot_id=capability_snapshot_id,
                policy_snapshot_id=policy_snapshot_id,
                tool_id="skill_search",
            )
            batch = connection.execute(
                "SELECT extension_snapshot_id FROM turn_execution_batches "
                "WHERE batch_id = ? AND turn_id = ? AND thread_id = ?",
                (
                    execution_scope.execution_batch_id,
                    execution_scope.turn_id,
                    execution_scope.thread_id,
                ),
            ).fetchone()
        if batch is None or batch["extension_snapshot_id"] != extension_snapshot_id:
            return None
        for row in reversed(rows):
            record = self._from_row(row)
            if self._record_skill_search_contains(
                record,
                extension_snapshot_id=extension_snapshot_id,
                extension_contribution_snapshot_id=(
                    extension_contribution_snapshot_id
                ),
                discovery_id=discovery_id,
            ):
                return record
        return None

    def has_completed_skill_search(
        self,
        *,
        execution_scope: ToolExecutionScope,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
    ) -> bool:
        """Expose the generic Skill read endpoint after a real bounded search.

        Resource authority remains exact and is enforced by
        ``completed_skill_search_for_discovery`` plus Runtime recomputation.
        """

        if not self._durable_scope(execution_scope):
            return False
        with self.database.reader() as connection:
            batch = connection.execute(
                "SELECT extension_snapshot_id FROM turn_execution_batches "
                "WHERE batch_id = ? AND turn_id = ? AND thread_id = ?",
                (
                    execution_scope.execution_batch_id,
                    execution_scope.turn_id,
                    execution_scope.thread_id,
                ),
            ).fetchone()
            if batch is None:
                return False
            rows = self._completed_scope_rows(
                connection,
                execution_scope=execution_scope,
                capability_snapshot_id=capability_snapshot_id,
                policy_snapshot_id=policy_snapshot_id,
                tool_id="skill_search",
            )
        extension_snapshot_id = str(batch["extension_snapshot_id"])
        return any(
            self._record_skill_search_shape(
                self._from_row(row),
                extension_snapshot_id=extension_snapshot_id,
            )
            for row in rows
        )

    def completed_connector_search_for_discovery(
        self,
        *,
        execution_scope: ToolExecutionScope,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        connector_catalog_snapshot_id: str,
        discovery_id: str,
    ) -> ToolExecutionRecord | None:
        """Resolve one exact Connector action emitted by a same-batch search."""

        if (
            not self._durable_scope(execution_scope)
            or self._parse_connector_discovery_id(discovery_id) is None
            or any(
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 256
                for value in (
                    capability_snapshot_id,
                    policy_snapshot_id,
                    connector_catalog_snapshot_id,
                )
            )
        ):
            return None
        with self.database.reader() as connection:
            if self._batch_connector_snapshot_id(
                connection,
                execution_scope=execution_scope,
            ) != connector_catalog_snapshot_id:
                return None
            rows = self._completed_scope_rows(
                connection,
                execution_scope=execution_scope,
                capability_snapshot_id=capability_snapshot_id,
                policy_snapshot_id=policy_snapshot_id,
                tool_id="connector_search",
            )
        for row in reversed(rows):
            record = self._from_row(row)
            if self._record_connector_search_contains(
                record,
                connector_catalog_snapshot_id=connector_catalog_snapshot_id,
                discovery_id=discovery_id,
            ):
                return record
        return None

    def completed_connector_describe_for_discovery(
        self,
        *,
        execution_scope: ToolExecutionScope,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        connector_catalog_snapshot_id: str,
        discovery_id: str,
        call_tool_id: str,
    ) -> tuple[ToolExecutionRecord, ToolExecutionRecord] | None:
        """Return the exact Describe grant and its causative Search fact."""

        if call_tool_id not in {"connector_read", "connector_write"}:
            return None
        if self._parse_connector_discovery_id(discovery_id) is None:
            return None
        with self.database.reader() as connection:
            if self._batch_connector_snapshot_id(
                connection,
                execution_scope=execution_scope,
            ) != connector_catalog_snapshot_id:
                return None
            rows = self._completed_scope_rows(
                connection,
                execution_scope=execution_scope,
                capability_snapshot_id=capability_snapshot_id,
                policy_snapshot_id=policy_snapshot_id,
                tool_id="connector_describe",
            )
            for row in reversed(rows):
                describe = self._from_row(row)
                result = describe.result
                search_tool_call_id = (
                    result.get("search_tool_call_id")
                    if isinstance(result, dict)
                    else None
                )
                if not isinstance(search_tool_call_id, str):
                    continue
                search_row = self._completed_scope_tool_call_row(
                    connection,
                    execution_scope=execution_scope,
                    capability_snapshot_id=capability_snapshot_id,
                    policy_snapshot_id=policy_snapshot_id,
                    tool_call_id=search_tool_call_id,
                    tool_id="connector_search",
                )
                if search_row is None:
                    continue
                search = self._from_row(search_row)
                if self._record_connector_describes(
                    describe,
                    search_record=search,
                    connector_catalog_snapshot_id=connector_catalog_snapshot_id,
                    discovery_id=discovery_id,
                    call_tool_id=call_tool_id,
                ):
                    return describe, search
        return None

    def connector_approval_description(
        self,
        *,
        execution_scope: ToolExecutionScope,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        discovery_id: str,
        call_tool_id: str,
    ) -> Mapping[str, Any] | None:
        """Return one verified same-batch action descriptor for informed HITL."""

        with self.database.reader() as connection:
            connector_snapshot_id = self._batch_connector_snapshot_id(
                connection,
                execution_scope=execution_scope,
            )
        if connector_snapshot_id is None:
            return None
        disclosure = self.completed_connector_describe_for_discovery(
            execution_scope=execution_scope,
            capability_snapshot_id=capability_snapshot_id,
            policy_snapshot_id=policy_snapshot_id,
            connector_catalog_snapshot_id=connector_snapshot_id,
            discovery_id=discovery_id,
            call_tool_id=call_tool_id,
        )
        if disclosure is None:
            return None
        describe, _search = disclosure
        result = describe.result
        action = result.get("action") if isinstance(result, dict) else None
        return dict(action) if isinstance(action, dict) else None

    def has_completed_connector_disclosure(
        self,
        *,
        execution_scope: ToolExecutionScope,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        tool_id: str,
    ) -> bool:
        """Expose one generic call endpoint after an exact action Describe.

        This grants only the model-visible generic schema.  The Connector
        handler separately recomputes Search/Describe and binds one exact
        instance/action/contract before it can call the service.
        """

        if tool_id not in {"connector_read", "connector_write"}:
            return False
        if not self._durable_scope(execution_scope):
            return False
        with self.database.reader() as connection:
            connector_snapshot_id = self._batch_connector_snapshot_id(
                connection,
                execution_scope=execution_scope,
            )
            if connector_snapshot_id is None:
                return False
            rows = self._completed_scope_rows(
                connection,
                execution_scope=execution_scope,
                capability_snapshot_id=capability_snapshot_id,
                policy_snapshot_id=policy_snapshot_id,
                tool_id="connector_describe",
            )
            for row in rows:
                describe = self._from_row(row)
                result = describe.result
                if not isinstance(result, dict):
                    continue
                discovery_id = result.get("discovery_id")
                search_tool_call_id = result.get("search_tool_call_id")
                if not isinstance(discovery_id, str) or not isinstance(
                    search_tool_call_id, str
                ):
                    continue
                search_row = self._completed_scope_tool_call_row(
                    connection,
                    execution_scope=execution_scope,
                    capability_snapshot_id=capability_snapshot_id,
                    policy_snapshot_id=policy_snapshot_id,
                    tool_call_id=search_tool_call_id,
                    tool_id="connector_search",
                )
                if search_row is None:
                    continue
                if self._record_connector_describes(
                    describe,
                    search_record=self._from_row(search_row),
                    connector_catalog_snapshot_id=connector_snapshot_id,
                    discovery_id=discovery_id,
                    call_tool_id=tool_id,
                ):
                    return True
        return False

    def has_completed_disclosure(
        self,
        *,
        execution_scope: ToolExecutionScope,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        tool_id: str,
        tool_version: str,
    ) -> bool:
        """Verify an exact deferred-tool grant from durable execution facts.

        Caller-provided flags and in-memory discovery sets are intentionally
        absent.  The completed ``tool_describe`` execution, immutable job/Turn
        identities, and both frozen policy snapshots must all agree.
        """

        if tool_id == "skill_read":
            return tool_version == "1.0.0" and self.has_completed_skill_search(
                execution_scope=execution_scope,
                capability_snapshot_id=capability_snapshot_id,
                policy_snapshot_id=policy_snapshot_id,
            )
        if tool_id in {"connector_read", "connector_write"}:
            return tool_version == "1.0.0" and self.has_completed_connector_disclosure(
                execution_scope=execution_scope,
                capability_snapshot_id=capability_snapshot_id,
                policy_snapshot_id=policy_snapshot_id,
                tool_id=tool_id,
            )
        identities = (
            capability_snapshot_id,
            policy_snapshot_id,
            tool_id,
            tool_version,
        )
        if (
            not self._durable_scope(execution_scope)
            or any(
            not isinstance(value, str) or not value.strip() or len(value) > 256
            for value in identities
            )
        ):
            return False
        with self.database.reader() as connection:
            rows = self._completed_scope_rows(
                connection,
                execution_scope=execution_scope,
                capability_snapshot_id=capability_snapshot_id,
                policy_snapshot_id=policy_snapshot_id,
                tool_id="tool_describe",
            )
            for row in rows:
                describe = self._from_row(row)
                result = describe.result
                search_tool_call_id = (
                    result.get("search_tool_call_id")
                    if isinstance(result, dict)
                    else None
                )
                if not isinstance(search_tool_call_id, str):
                    continue
                search_row = connection.execute(
                    "SELECT execution.* FROM tool_executions AS execution "
                    "JOIN jobs AS job ON job.job_id = execution.job_id "
                    "JOIN turns AS turn ON turn.turn_id = execution.turn_id "
                    "JOIN turn_execution_batches AS batch "
                    "ON batch.batch_id = execution.execution_batch_id "
                    "WHERE execution.tool_call_id = ? "
                    "AND execution.job_id = ? AND execution.turn_id = ? "
                    "AND execution.execution_batch_id = ? "
                    "AND execution.capability_snapshot_id = ? "
                    "AND execution.policy_snapshot_id = ? "
                    "AND execution.status = 'completed' "
                    "AND execution.tool_id = 'tool_search' "
                    "AND job.turn_id = execution.turn_id "
                    "AND job.thread_id = ? AND turn.thread_id = ? "
                    "AND batch.turn_id = execution.turn_id "
                    "AND batch.thread_id = ? "
                    "AND batch.capability_snapshot_id = execution.capability_snapshot_id "
                    "AND batch.permission_snapshot_id = execution.policy_snapshot_id "
                    "AND job.kind = 'agent_turn'",
                    (
                        search_tool_call_id,
                        execution_scope.job_id,
                        execution_scope.turn_id,
                        execution_scope.execution_batch_id,
                        capability_snapshot_id,
                        policy_snapshot_id,
                        execution_scope.thread_id,
                        execution_scope.thread_id,
                        execution_scope.thread_id,
                    ),
                ).fetchone()
                if search_row is None:
                    continue
                if self._record_discloses(
                    describe,
                    search_record=self._from_row(search_row),
                    capability_snapshot_id=capability_snapshot_id,
                    tool_id=tool_id,
                    tool_version=tool_version,
                ):
                    return True
        return False

    @staticmethod
    def _record_discloses(
        record: ToolExecutionRecord,
        *,
        search_record: ToolExecutionRecord,
        capability_snapshot_id: str,
        tool_id: str,
        tool_version: str,
    ) -> bool:
        arguments = record.arguments
        result = record.result
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"discovery_id"}
            or not isinstance(arguments.get("discovery_id"), str)
            or not isinstance(result, dict)
            or result.get("schema_version") != 1
            or result.get("capability_snapshot_id") != capability_snapshot_id
            or result.get("found") is not True
            or result.get("available") is not True
            or result.get("discovery_id")
            != f"tool:{tool_id}@{tool_version}"
            or result.get("search_tool_call_id") != search_record.tool_call_id
            or result.get("search_result_sha256") != search_record.result_sha256
        ):
            return False
        tool = result.get("tool")
        if not isinstance(tool, dict):
            return False
        spec = tool.get("spec")
        decision = tool.get("decision")
        if not isinstance(spec, dict) or not isinstance(decision, dict):
            return False
        if (
            spec.get("tool_id") != tool_id
            or spec.get("version") != tool_version
            or decision.get("tool_id") != tool_id
            or decision.get("tool_version") != tool_version
            or decision.get("eligible") is not True
            or decision.get("exposure") != "deferred"
        ):
            return False
        discovery_id = f"tool:{tool_id}@{tool_version}"
        return (
            arguments["discovery_id"] == discovery_id
            and ToolExecutionRepository._record_search_contains(
                search_record,
                capability_snapshot_id=capability_snapshot_id,
                discovery_id=discovery_id,
            )
        )

    @staticmethod
    def _record_search_contains(
        record: ToolExecutionRecord,
        *,
        capability_snapshot_id: str,
        discovery_id: str,
    ) -> bool:
        parsed = ToolExecutionRepository._parse_discovery_id(discovery_id)
        arguments = record.arguments
        result = record.result
        if (
            parsed is None
            or record.tool_id != "tool_search"
            or record.status != "completed"
            or not isinstance(arguments, dict)
            or set(arguments) not in ({"query"}, {"query", "limit"})
            or not isinstance(arguments.get("query"), str)
            or not isinstance(result, dict)
            or result.get("schema_version") != 1
            or result.get("capability_snapshot_id") != capability_snapshot_id
            or result.get("query") != arguments["query"]
            or not isinstance(result.get("tools"), list)
        ):
            return False
        tool_id, tool_version = parsed
        return any(
            isinstance(candidate, dict)
            and candidate.get("discovery_id") == discovery_id
            and candidate.get("tool_id") == tool_id
            and candidate.get("tool_version") == tool_version
            and candidate.get("exposure") == "deferred"
            for candidate in result["tools"]
        )

    @staticmethod
    def _record_skill_search_shape(
        record: ToolExecutionRecord,
        *,
        extension_snapshot_id: str,
    ) -> bool:
        arguments = record.arguments
        result = record.result
        return (
            record.tool_id == "skill_search"
            and record.status == "completed"
            and isinstance(arguments, dict)
            and set(arguments) in ({"query"}, {"query", "limit"})
            and isinstance(arguments.get("query"), str)
            and isinstance(result, dict)
            and result.get("schema_version") == 1
            and result.get("extension_snapshot_id") == extension_snapshot_id
            and isinstance(result.get("extension_contribution_snapshot_id"), str)
            and result["extension_contribution_snapshot_id"].startswith("extcontrib_")
            and result.get("query") == arguments["query"]
            and isinstance(result.get("skills"), list)
            and bool(result["skills"])
        )

    @staticmethod
    def _record_skill_search_contains(
        record: ToolExecutionRecord,
        *,
        extension_snapshot_id: str,
        extension_contribution_snapshot_id: str,
        discovery_id: str,
    ) -> bool:
        parsed = ToolExecutionRepository._parse_skill_discovery_id(discovery_id)
        if (
            parsed is None
            or not ToolExecutionRepository._record_skill_search_shape(
                record,
                extension_snapshot_id=extension_snapshot_id,
            )
            or not isinstance(record.result, dict)
            or record.result.get("extension_contribution_snapshot_id")
            != extension_contribution_snapshot_id
        ):
            return False
        extension_id, revision_id = parsed
        return any(
            isinstance(candidate, dict)
            and candidate.get("discovery_id") == discovery_id
            and candidate.get("discovery_id")
            == f"skill:{extension_id}@{revision_id}"
            and isinstance(candidate.get("name"), str)
            and isinstance(candidate.get("description"), str)
            and isinstance(candidate.get("tags"), list)
            for candidate in record.result["skills"]
        )

    @staticmethod
    def _record_connector_search_contains(
        record: ToolExecutionRecord,
        *,
        connector_catalog_snapshot_id: str,
        discovery_id: str,
    ) -> bool:
        parsed = ToolExecutionRepository._parse_connector_discovery_id(discovery_id)
        arguments = record.arguments
        result = record.result
        if (
            parsed is None
            or record.tool_id != "connector_search"
            or record.status != "completed"
            or not isinstance(arguments, dict)
            or set(arguments) not in ({"query"}, {"query", "limit"})
            or not isinstance(arguments.get("query"), str)
            or not isinstance(result, dict)
            or result.get("schema_version") != 1
            or result.get("connector_catalog_snapshot_id")
            != connector_catalog_snapshot_id
            or result.get("query") != arguments["query"]
            or not isinstance(result.get("connector_catalog_sha256"), str)
            or not isinstance(result.get("actions"), list)
            or not isinstance(result.get("waiting"), list)
        ):
            return False
        instance_id, connector_id, action_id, contract_sha256 = parsed
        return any(
            isinstance(candidate, dict)
            and candidate.get("discovery_id") == discovery_id
            and candidate.get("instance_id") == instance_id
            and candidate.get("connector_id") == connector_id
            and candidate.get("action_id") == action_id
            and candidate.get("call_tool_id")
            in {"connector_read", "connector_write"}
            and discovery_id.endswith("@" + contract_sha256)
            for candidate in result["actions"]
        )

    @staticmethod
    def _record_connector_describes(
        record: ToolExecutionRecord,
        *,
        search_record: ToolExecutionRecord,
        connector_catalog_snapshot_id: str,
        discovery_id: str,
        call_tool_id: str,
    ) -> bool:
        arguments = record.arguments
        result = record.result
        if (
            record.tool_id != "connector_describe"
            or record.status != "completed"
            or not isinstance(arguments, dict)
            or set(arguments) != {"discovery_id"}
            or arguments.get("discovery_id") != discovery_id
            or not isinstance(result, dict)
            or result.get("schema_version") != 1
            or result.get("connector_catalog_snapshot_id")
            != connector_catalog_snapshot_id
            or result.get("found") is not True
            or result.get("available") is not True
            or result.get("discovery_id") != discovery_id
            or result.get("search_tool_call_id") != search_record.tool_call_id
            or result.get("search_result_sha256") != search_record.result_sha256
        ):
            return False
        action = result.get("action")
        parsed = ToolExecutionRepository._parse_connector_discovery_id(discovery_id)
        if not isinstance(action, dict) or parsed is None:
            return False
        instance_id, connector_id, action_id, contract_sha256 = parsed
        return (
            action.get("instance_id") == instance_id
            and action.get("connector_id") == connector_id
            and action.get("action_id") == action_id
            and action.get("action_contract_sha256") == contract_sha256
            and action.get("call_tool_id") == call_tool_id
            and isinstance(action.get("input_schema"), dict)
            and isinstance(action.get("output_schema"), dict)
            and ToolExecutionRepository._record_connector_search_contains(
                search_record,
                connector_catalog_snapshot_id=connector_catalog_snapshot_id,
                discovery_id=discovery_id,
            )
        )

    @staticmethod
    def _parse_discovery_id(discovery_id: object) -> tuple[str, str] | None:
        if not isinstance(discovery_id, str) or not discovery_id.startswith("tool:"):
            return None
        tool_id, separator, tool_version = discovery_id[5:].rpartition("@")
        if (
            not separator
            or not tool_id
            or not tool_version
            or discovery_id != f"tool:{tool_id}@{tool_version}"
            or len(discovery_id) > 512
        ):
            return None
        return tool_id, tool_version

    @staticmethod
    def _parse_skill_discovery_id(discovery_id: object) -> tuple[str, str] | None:
        if not isinstance(discovery_id, str) or not discovery_id.startswith("skill:"):
            return None
        extension_id, separator, revision_id = discovery_id[6:].rpartition("@")
        if (
            not separator
            or not extension_id
            or not revision_id
            or discovery_id != f"skill:{extension_id}@{revision_id}"
            or len(discovery_id) > 512
        ):
            return None
        return extension_id, revision_id

    @staticmethod
    def _parse_connector_discovery_id(
        discovery_id: object,
    ) -> tuple[str, str, str, str] | None:
        if (
            not isinstance(discovery_id, str)
            or not discovery_id.startswith("connector:")
            or len(discovery_id) > 512
        ):
            return None
        prefix, separator, contract_sha256 = discovery_id.rpartition("@")
        if not separator or len(contract_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in contract_sha256
        ):
            return None
        instance_target = prefix[len("connector:") :]
        instance_id, separator, target = instance_target.partition("@")
        connector_id, slash, action_id = target.partition("/")
        if (
            not separator
            or not slash
            or not instance_id
            or len(instance_id) > 256
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for character in instance_id)
            or not connector_id
            or not action_id
            or discovery_id
            != f"connector:{instance_id}@{connector_id}/{action_id}@{contract_sha256}"
        ):
            return None
        return instance_id, connector_id, action_id, contract_sha256

    @staticmethod
    def _batch_connector_snapshot_id(
        connection: sqlite3.Connection,
        *,
        execution_scope: ToolExecutionScope,
    ) -> str | None:
        row = connection.execute(
            "SELECT config.snapshot_id, config.kind, config.payload_json, "
            "config.payload_sha256 "
            "FROM turn_execution_batches AS batch "
            "JOIN jobs AS job ON job.job_id = ? "
            "JOIN turns AS turn ON turn.turn_id = batch.turn_id "
            "JOIN runtime_snapshots AS config "
            "ON config.snapshot_id = batch.config_snapshot_id "
            "WHERE batch.batch_id = ? AND batch.turn_id = ? "
            "AND batch.thread_id = ? AND job.turn_id = batch.turn_id "
            "AND job.thread_id = batch.thread_id "
            "AND turn.thread_id = batch.thread_id AND job.kind = 'agent_turn'",
            (
                execution_scope.job_id,
                execution_scope.execution_batch_id,
                execution_scope.turn_id,
                execution_scope.thread_id,
            ),
        ).fetchone()
        if row is None or row["kind"] != "config":
            return None
        payload_json = str(row["payload_json"])
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != str(
            row["payload_sha256"]
        ):
            return None
        payload = json_loads(payload_json, {})
        snapshot_id = payload.get("connector_catalog_snapshot_id") if isinstance(
            payload, dict
        ) else None
        return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None

    @staticmethod
    def _completed_scope_tool_call_row(
        connection: sqlite3.Connection,
        *,
        execution_scope: ToolExecutionScope,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        tool_call_id: str,
        tool_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT execution.* FROM tool_executions AS execution "
            "JOIN jobs AS job ON job.job_id = execution.job_id "
            "JOIN turns AS turn ON turn.turn_id = execution.turn_id "
            "JOIN turn_execution_batches AS batch "
            "ON batch.batch_id = execution.execution_batch_id "
            "WHERE execution.tool_call_id = ? "
            "AND execution.job_id = ? AND execution.turn_id = ? "
            "AND execution.execution_batch_id = ? "
            "AND execution.capability_snapshot_id = ? "
            "AND execution.policy_snapshot_id = ? "
            "AND execution.status = 'completed' AND execution.tool_id = ? "
            "AND job.turn_id = execution.turn_id AND job.thread_id = ? "
            "AND turn.thread_id = ? AND batch.turn_id = execution.turn_id "
            "AND batch.thread_id = ? "
            "AND batch.capability_snapshot_id = execution.capability_snapshot_id "
            "AND batch.permission_snapshot_id = execution.policy_snapshot_id "
            "AND job.kind = 'agent_turn'",
            (
                tool_call_id,
                execution_scope.job_id,
                execution_scope.turn_id,
                execution_scope.execution_batch_id,
                capability_snapshot_id,
                policy_snapshot_id,
                tool_id,
                execution_scope.thread_id,
                execution_scope.thread_id,
                execution_scope.thread_id,
            ),
        ).fetchone()

    @staticmethod
    def _durable_scope(execution_scope: object) -> bool:
        return (
            isinstance(execution_scope, ToolExecutionScope)
            and isinstance(execution_scope.execution_batch_id, str)
            and bool(execution_scope.execution_batch_id)
        )

    @staticmethod
    def _completed_scope_rows(
        connection: sqlite3.Connection,
        *,
        execution_scope: ToolExecutionScope,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        tool_id: str,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT execution.* FROM tool_executions AS execution "
            "JOIN jobs AS job ON job.job_id = execution.job_id "
            "JOIN turns AS turn ON turn.turn_id = execution.turn_id "
            "JOIN turn_execution_batches AS batch "
            "ON batch.batch_id = execution.execution_batch_id "
            "WHERE execution.job_id = ? AND execution.turn_id = ? "
            "AND execution.execution_batch_id = ? "
            "AND execution.capability_snapshot_id = ? "
            "AND execution.policy_snapshot_id = ? "
            "AND execution.status = 'completed' "
            "AND execution.tool_id = ? "
            "AND job.turn_id = execution.turn_id "
            "AND job.thread_id = ? AND turn.thread_id = ? "
            "AND batch.turn_id = execution.turn_id "
            "AND batch.thread_id = ? "
            "AND batch.capability_snapshot_id = execution.capability_snapshot_id "
            "AND batch.permission_snapshot_id = execution.policy_snapshot_id "
            "AND job.kind = 'agent_turn' "
            "ORDER BY execution.created_at, execution.tool_call_id LIMIT 1024",
            (
                execution_scope.job_id,
                execution_scope.turn_id,
                execution_scope.execution_batch_id,
                capability_snapshot_id,
                policy_snapshot_id,
                tool_id,
                execution_scope.thread_id,
                execution_scope.thread_id,
                execution_scope.thread_id,
            ),
        ).fetchall()

    @staticmethod
    def _require(connection: sqlite3.Connection, tool_call_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tool_executions WHERE tool_call_id = ?",
            (tool_call_id,),
        ).fetchone()
        if row is None:
            raise KeyError(tool_call_id)
        return row

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ToolExecutionRecord:
        return ToolExecutionRecord(
            tool_call_id=str(row["tool_call_id"]),
            job_id=str(row["job_id"]),
            turn_id=str(row["turn_id"]),
            execution_batch_id=str(row["execution_batch_id"]),
            capability_snapshot_id=str(row["capability_snapshot_id"]),
            policy_snapshot_id=str(row["policy_snapshot_id"]),
            tool_id=str(row["tool_id"]),
            arguments=json_loads(row["arguments_json"], {}),
            arguments_sha256=str(row["arguments_sha256"]),
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            attempt=int(row["attempt"]),
            result=json_loads(row["result_json"]),
            error_code=row["error_code"],
        )

    @staticmethod
    def _admission_from_row(row: sqlite3.Row) -> ToolInvocationAdmission:
        payload = {
            "schema_version": 1,
            "tool_call_id": str(row["tool_call_id"]),
            "job_id": str(row["job_id"]),
            "thread_id": str(row["thread_id"]),
            "turn_id": str(row["turn_id"]),
            "execution_batch_id": str(row["execution_batch_id"]),
            "capability_snapshot_id": str(row["capability_snapshot_id"]),
            "permission_account_id": str(row["permission_account_id"]),
            "frozen_permission_snapshot_id": str(
                row["frozen_permission_snapshot_id"]
            ),
            "current_permission_snapshot_id": str(
                row["current_permission_snapshot_id"]
            ),
            "current_permission_state_digest": str(
                row["current_permission_state_digest"]
            ),
            "current_availability_digest": row["current_availability_digest"],
            "tool_id": str(row["tool_id"]),
            "tool_version": str(row["tool_version"]),
            "arguments_sha256": str(row["arguments_sha256"]),
            "idempotency_key": row["idempotency_key"],
            "approved": bool(row["approved"]),
            "approval_interaction_id": row["approval_interaction_id"],
            "effective_sandbox": str(row["effective_sandbox"]),
            "admitted_at": str(row["admitted_at"]),
        }
        expected_digest = hashlib.sha256(
            b"ecorex-invocation-admission-v1\0"
            + json_dumps(payload).encode("utf-8")
        ).hexdigest()
        if (
            row["permit_digest"] != expected_digest
            or row["permit_id"] != "permit_" + expected_digest
        ):
            raise ToolExecutionConflict("tool invocation admission digest is invalid")
        try:
            sandbox = SandboxLevel(str(row["effective_sandbox"]))
        except ValueError as error:
            raise ToolExecutionConflict(
                "tool invocation admission sandbox is invalid"
            ) from error
        return ToolInvocationAdmission(
            permit_id=str(row["permit_id"]),
            tool_call_id=str(row["tool_call_id"]),
            execution_scope=ToolExecutionScope(
                job_id=str(row["job_id"]),
                thread_id=str(row["thread_id"]),
                turn_id=str(row["turn_id"]),
                execution_batch_id=str(row["execution_batch_id"]),
            ),
            capability_snapshot_id=str(row["capability_snapshot_id"]),
            frozen_policy_snapshot_id=str(row["frozen_permission_snapshot_id"]),
            current_policy_snapshot_id=str(row["current_permission_snapshot_id"]),
            current_permission_state_digest=str(
                row["current_permission_state_digest"]
            ),
            current_availability_digest=row["current_availability_digest"],
            tool_id=str(row["tool_id"]),
            tool_version=str(row["tool_version"]),
            arguments_sha256=str(row["arguments_sha256"]),
            idempotency_key=row["idempotency_key"],
            approved=bool(row["approved"]),
            effective_sandbox=sandbox,
            admitted_at=str(row["admitted_at"]),
        )


class DurableDeferredDisclosureAuthority:
    """Capability-layer adapter backed by immutable Runtime execution scope."""

    def __init__(self, repository: ToolExecutionRepository) -> None:
        if not isinstance(repository, ToolExecutionRepository):
            raise ValueError("tool execution repository is required")
        self.repository = repository

    def verify(
        self,
        *,
        execution_scope: ToolExecutionScope,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        tool_id: str,
        tool_version: str,
    ) -> bool:
        return self.repository.has_completed_disclosure(
            execution_scope=execution_scope,
            capability_snapshot_id=capability_snapshot_id,
            policy_snapshot_id=policy_snapshot_id,
            tool_id=tool_id,
            tool_version=tool_version,
        )


class DurableInvocationAdmissionAuthority:
    """Resolve exact append-only permits for CapabilityService dispatch."""

    def __init__(self, repository: ToolExecutionRepository) -> None:
        if not isinstance(repository, ToolExecutionRepository):
            raise ValueError("tool execution repository is required")
        self.repository = repository

    def resolve(
        self,
        *,
        execution_scope: ToolExecutionScope,
        tool_call_id: str,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        tool_id: str,
        tool_version: str,
        arguments_sha256: str,
        idempotency_key: str | None,
    ) -> ToolInvocationAdmission | None:
        admission = self.repository.admission(tool_call_id)
        if admission is None:
            return None
        expected = (
            execution_scope,
            capability_snapshot_id,
            policy_snapshot_id,
            tool_id,
            tool_version,
            arguments_sha256,
            idempotency_key,
        )
        actual = (
            admission.execution_scope,
            admission.capability_snapshot_id,
            admission.frozen_policy_snapshot_id,
            admission.tool_id,
            admission.tool_version,
            admission.arguments_sha256,
            admission.idempotency_key,
        )
        return admission if actual == expected else None
