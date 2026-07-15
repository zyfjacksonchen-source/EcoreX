"""SQLite persistence for the artifact domain.

The repository owns product projection semantics: user reads always filter on
the persisted backend visibility and never accept an extension toggle from a
caller.  Internal reads are separate, explicit methods for workers/auditing.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Sequence

from ecorex.runtime.database import SQLiteDatabase

from .classification import ClassificationDecision
from .errors import (
    ArtifactActionUnavailable,
    ArtifactNotFound,
    IdempotencyConflict,
    RetouchConflict,
    RevisionNotFound,
)
from .identity import (
    filename_claim_key,
    isoformat_utc,
    minute_display_name,
    new_artifact_id,
    new_feedback_id,
    new_retouch_job_id,
    new_retouch_workspace_id,
    new_revision_id,
    sanitize_display_filename,
    utc_now,
)
from .models import (
    ArtifactAction,
    ArtifactExternalActionReceipt,
    ArtifactExternalActionStatus,
    ArtifactFamily,
    ArtifactLineage,
    ArtifactProjection,
    ArtifactRole,
    ArtifactScope,
    ArtifactStatus,
    ArtifactVisibility,
    FeedbackProjection,
    FeedbackRecord,
    FeedbackRequest,
    FeedbackSignal,
    InspectionRegion,
    QualityEvidence,
    RenditionKind,
    RenditionProjection,
    RetouchExecutionBinding,
    RetouchAnnotation,
    RetouchJob,
    RetouchJobStatus,
    RetouchRequest,
    RetouchStagedResult,
)
from .storage import StoredBlob
from .retouch_workspace import (
    RetouchEditSurface,
    RetouchReference,
    RetouchWorkspaceProjection,
    RetouchWorkspaceStatus,
)

if TYPE_CHECKING:
    from .service import PreparedArtifact


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


class ArtifactRepository:
    """Transactional artifact metadata repository backed by SQLite WAL."""

    def __init__(self, database_path: str | Path) -> None:
        self.database = SQLiteDatabase(database_path)
        self.database_path = self.database.path
        self._lock = threading.RLock()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            with self.database.transaction() as connection:
                yield connection

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            with self.database.reader() as connection:
                yield connection

    @staticmethod
    def _claim_display_name(
        connection: sqlite3.Connection,
        requested_name: str,
        now: datetime,
        recorded_at: str,
    ) -> str:
        for sequence in range(1, 1_000_000):
            candidate = minute_display_name(requested_name, now, sequence)
            claim_key = filename_claim_key(candidate)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO artifact_display_name_claims(claim_key, display_name, claimed_at) VALUES (?, ?, ?)",
                (claim_key, candidate, recorded_at),
            )
            if cursor.rowcount == 1:
                return candidate
        raise RuntimeError("artifact display-name sequence exhausted")

    def create_artifact(self, prepared: "PreparedArtifact") -> ArtifactProjection:
        """Commit one service-prepared Artifact in a repository-owned transaction.

        Classification and CAS publication happen at the :class:`ArtifactService`
        boundary.  Keeping this convenience method lets the existing public
        ``ArtifactService.create_artifact`` contract remain synchronous while
        Runtime integrations can use :meth:`create_artifact_in_transaction` to
        atomically commit their own completion facts alongside Artifact metadata.
        """

        with self._write() as connection:
            return self.create_artifact_in_transaction(connection, prepared)

    def create_artifact_in_transaction(
        self,
        connection: sqlite3.Connection,
        prepared: "PreparedArtifact",
    ) -> ArtifactProjection:
        """Create Artifact metadata inside the caller's authoritative transaction.

        The caller must supply an active connection to this repository's exact
        Runtime database.  The method never commits or rolls back that
        transaction.  This is the narrow seam used by cross-domain completion
        coordinators; it intentionally reuses the same classification, naming,
        lineage and projection logic as ordinary Artifact creation.
        """

        self._require_own_active_transaction(connection)
        with self._lock:
            return self._create_artifact_in_transaction(connection, prepared)

    def _create_artifact_in_transaction(
        self,
        connection: sqlite3.Connection,
        prepared: "PreparedArtifact",
    ) -> ArtifactProjection:
        artifact_id = new_artifact_id()
        revision_id = new_revision_id()
        created_at = isoformat_utc(prepared.prepared_at)
        lineage = prepared.lineage
        scope = prepared.scope
        decision = prepared.decision
        requested_name = sanitize_display_filename(prepared.requested_name)
        if lineage.supersedes_revision_id is not None:
            raise RetouchConflict(
                "a new artifact's first revision cannot supersede another revision"
            )
        self._require_source_artifacts(
            connection,
            lineage.source_artifact_ids,
            owner_account_id=scope.account_id,
        )
        display_name = self._claim_display_name(
            connection,
            requested_name,
            prepared.prepared_at,
            created_at,
        )
        connection.execute(
            """
            INSERT INTO artifact_entities(
                artifact_id, family, role, visibility, status, actions_json,
                classification_reasons_json, current_revision_id,
                owner_account_id, thread_id, turn_id, created_by_tool_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                decision.family.value,
                decision.role.value,
                decision.visibility.value,
                prepared.status.value,
                _canonical_json([action.value for action in decision.actions]),
                _canonical_json(list(decision.reasons)),
                scope.account_id,
                scope.thread_id,
                scope.turn_id,
                scope.created_by_tool_id,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO artifact_revisions(
                revision_id, artifact_id, revision_number, requested_name, display_name,
                mime_type, size_bytes, sha256, source_artifact_ids_json,
                supersedes_revision_id, quality_evidence_json, created_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                artifact_id,
                requested_name,
                display_name,
                prepared.mime_type,
                prepared.size_bytes,
                prepared.sha256,
                _canonical_json(list(lineage.source_artifact_ids)),
                lineage.supersedes_revision_id,
                _canonical_json(prepared.quality_evidence.to_dict()),
                created_at,
            ),
        )
        connection.execute(
            "UPDATE artifact_entities SET current_revision_id = ? WHERE artifact_id = ?",
            (revision_id, artifact_id),
        )
        self._insert_lineage_sources(
            connection, revision_id, lineage.source_artifact_ids
        )
        return self._projection_for_revision(connection, artifact_id, revision_id)

    def _require_own_active_transaction(
        self, connection: sqlite3.Connection
    ) -> None:
        try:
            active = connection.in_transaction
        except sqlite3.ProgrammingError as error:
            raise RuntimeError(
                "Artifact creation requires an open Runtime database transaction"
            ) from error
        if not active:
            raise RuntimeError(
                "Artifact creation requires an active Runtime database transaction"
            )
        try:
            databases = connection.execute("PRAGMA database_list").fetchall()
        except sqlite3.Error as error:
            raise RuntimeError(
                "Artifact creation cannot verify the Runtime database transaction"
            ) from error
        main_path = next(
            (str(row[2]) for row in databases if str(row[1]) == "main"),
            "",
        )
        if not main_path:
            raise RuntimeError(
                "Artifact creation requires the authoritative Runtime database"
            )
        observed = os.path.normcase(str(Path(main_path).resolve()))
        expected = os.path.normcase(str(Path(self.database_path).resolve()))
        if observed != expected:
            raise RuntimeError(
                "Artifact creation transaction belongs to a different database"
            )

    def get_scope(self, artifact_id: str) -> ArtifactScope:
        with self._read() as connection:
            row = connection.execute(
                "SELECT owner_account_id, thread_id, turn_id, created_by_tool_id "
                "FROM artifact_entities WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise ArtifactNotFound(f"artifact {artifact_id!r} was not found")
        return ArtifactScope(
            account_id=row["owner_account_id"],
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
            created_by_tool_id=row["created_by_tool_id"],
        )

    def get_user_projection(
        self,
        artifact_id: str,
        *,
        account_id: str = "local-user",
    ) -> ArtifactProjection:
        """Load one product-visible projection without revealing internal IDs."""

        with self._read() as connection:
            entity = connection.execute(
                """
                SELECT artifact_id, current_revision_id FROM artifact_entities
                WHERE artifact_id = ? AND owner_account_id = ?
                  AND visibility IN ('primary', 'secondary')
                """,
                (artifact_id, account_id),
            ).fetchone()
            if entity is None:
                # Internal and nonexistent artifacts intentionally share the
                # same user-facing outcome to avoid an implementation side channel.
                raise ArtifactNotFound(f"artifact {artifact_id!r} was not found")
            return self._projection_for_revision(
                connection,
                entity["artifact_id"],
                entity["current_revision_id"],
                include_internal_lineage=False,
            )

    def get_internal_projection(self, artifact_id: str) -> ArtifactProjection:
        with self._read() as connection:
            entity = connection.execute(
                "SELECT artifact_id, current_revision_id FROM artifact_entities WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if entity is None:
                raise ArtifactNotFound(f"artifact {artifact_id!r} was not found")
            return self._projection_for_revision(connection, entity["artifact_id"], entity["current_revision_id"])

    def get_revision_projection(
        self,
        artifact_id: str,
        revision_id: str,
        *,
        include_internal: bool = False,
        account_id: str = "local-user",
    ) -> ArtifactProjection:
        with self._read() as connection:
            entity = connection.execute(
                "SELECT visibility FROM artifact_entities "
                "WHERE artifact_id = ? AND owner_account_id = ?",
                (artifact_id, account_id),
            ).fetchone()
            if entity is None or (
                not include_internal and entity["visibility"] not in {"primary", "secondary"}
            ):
                raise ArtifactNotFound(f"artifact {artifact_id!r} was not found")
            return self._projection_for_revision(
                connection,
                artifact_id,
                revision_id,
                include_internal_lineage=include_internal,
            )

    def list_user_projections(
        self,
        *,
        account_id: str = "local-user",
        thread_id: str | None = None,
    ) -> tuple[ArtifactProjection, ...]:
        with self._read() as connection:
            query = (
                "SELECT artifact_id, current_revision_id FROM artifact_entities "
                "WHERE owner_account_id = ? "
                "AND visibility IN ('primary', 'secondary') AND status != 'deleted'"
            )
            parameters: list[str] = [account_id]
            if thread_id is not None:
                query += " AND thread_id = ?"
                parameters.append(thread_id)
            query += " ORDER BY created_order ASC"
            rows = connection.execute(query, parameters).fetchall()
            return tuple(
                self._projection_for_revision(
                    connection,
                    row["artifact_id"],
                    row["current_revision_id"],
                    include_internal_lineage=False,
                )
                for row in rows
            )

    def list_internal_projections(self) -> tuple[ArtifactProjection, ...]:
        """Explicit audit/worker view.  Never use this for a product endpoint."""

        with self._read() as connection:
            rows = connection.execute(
                "SELECT artifact_id, current_revision_id FROM artifact_entities ORDER BY created_order ASC"
            ).fetchall()
            return tuple(
                self._projection_for_revision(connection, row["artifact_id"], row["current_revision_id"])
                for row in rows
            )

    def revision_digest(self, revision_id: str) -> str:
        with self._read() as connection:
            row = connection.execute(
                "SELECT sha256 FROM artifact_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
            if row is None:
                raise RevisionNotFound(f"revision {revision_id!r} was not found")
            return row["sha256"]

    @staticmethod
    def _retouch_workspace(row: sqlite3.Row) -> RetouchWorkspaceProjection:
        return RetouchWorkspaceProjection(
            workspace_id=row["workspace_id"],
            artifact_id=row["artifact_id"],
            version=int(row["version"]),
            status=RetouchWorkspaceStatus(row["status"]),
            edit_surface=RetouchEditSurface.from_dict(
                _decode_json(row["edit_surface_json"], {})
            ),
            annotations=tuple(
                RetouchAnnotation.from_dict(item)
                for item in _decode_json(row["annotations_json"], [])
            ),
            references=tuple(
                RetouchReference.from_dict(item)
                for item in _decode_json(row["references_json"], [])
            ),
            global_instruction=row["global_instruction"],
            view_state=_decode_json(row["view_state_json"], {}),
            mask=(
                _decode_json(row["mask_metadata_json"], {})
                if row["mask_metadata_json"]
                else None
            ),
            submitted_job_id=row["submitted_job_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_or_get_retouch_workspace(
        self,
        *,
        artifact_id: str,
        base_revision_id: str,
        account_id: str,
        edit_surface: RetouchEditSurface,
        now: datetime,
    ) -> RetouchWorkspaceProjection:
        timestamp = isoformat_utc(now)
        with self._write() as connection:
            entity = connection.execute(
                "SELECT current_revision_id, visibility FROM artifact_entities "
                "WHERE artifact_id = ? AND owner_account_id = ?",
                (artifact_id, account_id),
            ).fetchone()
            if entity is None or entity["visibility"] not in {"primary", "secondary"}:
                raise ArtifactNotFound(f"artifact {artifact_id!r} was not found")
            self._require_revision_owner(connection, artifact_id, base_revision_id)
            existing = connection.execute(
                "SELECT * FROM artifact_retouch_workspaces "
                "WHERE owner_account_id = ? AND artifact_id = ? AND base_revision_id = ?",
                (account_id, artifact_id, base_revision_id),
            ).fetchone()
            if existing is not None:
                projection = self._retouch_workspace(existing)
                if projection.edit_surface != edit_surface:
                    raise RetouchConflict("immutable retouch edit surface metadata changed")
                return projection
            workspace_id = new_retouch_workspace_id()
            connection.execute(
                """
                INSERT INTO artifact_retouch_workspaces(
                    workspace_id, artifact_id, base_revision_id, owner_account_id,
                    version, status, edit_surface_json, annotations_json,
                    references_json, global_instruction, view_state_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, 'editing', ?, '[]', '[]', '', '{}', ?, ?)
                """,
                (
                    workspace_id,
                    artifact_id,
                    base_revision_id,
                    account_id,
                    _canonical_json(edit_surface.to_dict()),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM artifact_retouch_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            assert row is not None
            return self._retouch_workspace(row)

    def get_retouch_workspace(
        self, workspace_id: str, *, account_id: str
    ) -> RetouchWorkspaceProjection:
        # GET/projection paths stay pure even while the Runtime is serving in
        # critical read-only mode.  Crash-left submission claims are repaired
        # by the explicit controlled-startup transaction below.
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_retouch_workspaces "
                "WHERE workspace_id = ? AND owner_account_id = ?",
                (workspace_id, account_id),
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"retouch workspace {workspace_id!r} was not found")
            return self._retouch_workspace(row)

    def recover_interrupted_retouch_workspace_submissions(
        self,
        *,
        account_id: str | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> int:
        """Reconcile crash-left claims before request admission opens.

        Calling this from a GET or live background projection would race the
        interval between a durable workspace claim and durable job creation,
        so the Runtime invokes it only at controlled startup.
        """

        with self._write() as connection:
            query = (
                "SELECT * FROM artifact_retouch_workspaces "
                "WHERE status = 'submitting' AND submitted_job_id IS NULL"
            )
            parameters: tuple[str, ...] = ()
            if account_id is not None:
                query += " AND owner_account_id = ?"
                parameters = (account_id,)
            rows = connection.execute(query, parameters).fetchall()
            recovered = 0
            for row in rows:
                matching_job = connection.execute(
                    "SELECT job_id FROM artifact_retouch_jobs "
                    "WHERE artifact_id = ? AND client_request_id = ?",
                    (row["artifact_id"], row["submit_client_request_id"]),
                ).fetchone()
                if matching_job is None:
                    connection.execute(
                        "UPDATE artifact_retouch_workspaces SET status = 'editing', "
                        "submit_client_request_id = NULL WHERE workspace_id = ? "
                        "AND status = 'submitting' AND submitted_job_id IS NULL",
                        (row["workspace_id"],),
                    )
                else:
                    connection.execute(
                        "UPDATE artifact_retouch_workspaces SET status = 'submitted', "
                        "submitted_job_id = ? WHERE workspace_id = ? "
                        "AND status = 'submitting' AND submitted_job_id IS NULL",
                        (matching_job["job_id"], row["workspace_id"]),
                    )
                recovered += 1
            if before_commit is not None:
                before_commit()
            return recovered

    def update_retouch_workspace(
        self,
        workspace_id: str,
        *,
        account_id: str,
        expected_version: int,
        annotations: Sequence[RetouchAnnotation],
        references: Sequence[RetouchReference],
        global_instruction: str,
        view_state: Mapping[str, Any],
        mask_metadata: Mapping[str, Any] | None,
        client_request_id: str,
        request_digest: str,
        now: datetime,
    ) -> RetouchWorkspaceProjection:
        timestamp = isoformat_utc(now)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_retouch_workspaces "
                "WHERE workspace_id = ? AND owner_account_id = ?",
                (workspace_id, account_id),
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"retouch workspace {workspace_id!r} was not found")
            if row["last_client_request_id"] == client_request_id:
                if row["last_request_digest"] != request_digest:
                    raise IdempotencyConflict(
                        "retouch workspace client_request_id was reused with different content"
                    )
                return self._retouch_workspace(row)
            if row["status"] != RetouchWorkspaceStatus.EDITING.value:
                raise RetouchConflict("retouch workspace is not editable")
            if int(row["version"]) != expected_version:
                raise RetouchConflict("retouch workspace version is stale")
            cursor = connection.execute(
                """
                UPDATE artifact_retouch_workspaces
                SET version = version + 1, annotations_json = ?, references_json = ?,
                    global_instruction = ?, view_state_json = ?, mask_metadata_json = ?,
                    last_client_request_id = ?, last_request_digest = ?, updated_at = ?
                WHERE workspace_id = ? AND owner_account_id = ?
                  AND version = ? AND status = 'editing'
                """,
                (
                    _canonical_json([item.to_dict() for item in annotations]),
                    _canonical_json([item.to_dict() for item in references]),
                    global_instruction,
                    _canonical_json(dict(view_state)),
                    _canonical_json(dict(mask_metadata)) if mask_metadata is not None else None,
                    client_request_id,
                    request_digest,
                    timestamp,
                    workspace_id,
                    account_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RetouchConflict("retouch workspace update lost its version fence")
            updated = connection.execute(
                "SELECT * FROM artifact_retouch_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            assert updated is not None
            return self._retouch_workspace(updated)

    def claim_retouch_workspace_submission(
        self,
        workspace_id: str,
        *,
        account_id: str,
        expected_version: int,
        client_request_id: str,
        now: datetime,
    ) -> RetouchWorkspaceProjection:
        timestamp = isoformat_utc(now)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_retouch_workspaces "
                "WHERE workspace_id = ? AND owner_account_id = ?",
                (workspace_id, account_id),
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"retouch workspace {workspace_id!r} was not found")
            if row["submit_client_request_id"] == client_request_id and row["status"] in {
                RetouchWorkspaceStatus.SUBMITTING.value,
                RetouchWorkspaceStatus.SUBMITTED.value,
            }:
                return self._retouch_workspace(row)
            if row["status"] != RetouchWorkspaceStatus.EDITING.value:
                raise RetouchConflict("retouch workspace is already being submitted")
            if int(row["version"]) != expected_version:
                raise RetouchConflict("retouch workspace version is stale")
            connection.execute(
                "UPDATE artifact_retouch_workspaces SET status = 'submitting', "
                "version = version + 1, submit_client_request_id = ?, updated_at = ? "
                "WHERE workspace_id = ? AND owner_account_id = ? AND version = ?",
                (client_request_id, timestamp, workspace_id, account_id, expected_version),
            )
            updated = connection.execute(
                "SELECT * FROM artifact_retouch_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            assert updated is not None
            return self._retouch_workspace(updated)

    def complete_retouch_workspace_submission(
        self,
        workspace_id: str,
        *,
        account_id: str,
        client_request_id: str,
        job_id: str,
        now: datetime,
    ) -> RetouchWorkspaceProjection:
        with self._write() as connection:
            return self.complete_retouch_workspace_submission_in_transaction(
                connection,
                workspace_id,
                account_id=account_id,
                client_request_id=client_request_id,
                job_id=job_id,
                now=now,
            )

    def complete_retouch_workspace_submission_in_transaction(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        *,
        account_id: str,
        client_request_id: str,
        job_id: str,
        now: datetime,
    ) -> RetouchWorkspaceProjection:
        """Fence workspace completion inside the caller's Artifact transaction."""

        self._require_own_active_transaction(connection)
        timestamp = isoformat_utc(now)
        with self._lock:
            row = connection.execute(
                "SELECT * FROM artifact_retouch_workspaces "
                "WHERE workspace_id = ? AND owner_account_id = ?",
                (workspace_id, account_id),
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"retouch workspace {workspace_id!r} was not found")
            if row["status"] == RetouchWorkspaceStatus.SUBMITTED.value:
                if row["submitted_job_id"] != job_id:
                    raise RetouchConflict("retouch workspace was submitted as another job")
                return self._retouch_workspace(row)
            if row["status"] != RetouchWorkspaceStatus.SUBMITTING.value or row[
                "submit_client_request_id"
            ] != client_request_id:
                raise RetouchConflict("retouch workspace submission claim was lost")
            connection.execute(
                "UPDATE artifact_retouch_workspaces SET status = 'submitted', "
                "submitted_job_id = ?, updated_at = ? WHERE workspace_id = ?",
                (job_id, timestamp, workspace_id),
            )
            updated = connection.execute(
                "SELECT * FROM artifact_retouch_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            assert updated is not None
            return self._retouch_workspace(updated)

    def release_retouch_workspace_submission(
        self,
        workspace_id: str,
        *,
        account_id: str,
        client_request_id: str,
        now: datetime,
    ) -> RetouchWorkspaceProjection:
        timestamp = isoformat_utc(now)
        with self._write() as connection:
            connection.execute(
                "UPDATE artifact_retouch_workspaces SET status = 'editing', "
                "submit_client_request_id = NULL, updated_at = ? "
                "WHERE workspace_id = ? AND owner_account_id = ? "
                "AND status = 'submitting' AND submit_client_request_id = ? "
                "AND submitted_job_id IS NULL",
                (timestamp, workspace_id, account_id, client_request_id),
            )
            row = connection.execute(
                "SELECT * FROM artifact_retouch_workspaces "
                "WHERE workspace_id = ? AND owner_account_id = ?",
                (workspace_id, account_id),
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"retouch workspace {workspace_id!r} was not found")
            return self._retouch_workspace(row)

    def reopen_failed_retouch_workspace(
        self,
        workspace_id: str,
        *,
        account_id: str,
        expected_version: int,
        now: datetime,
    ) -> RetouchWorkspaceProjection:
        timestamp = isoformat_utc(now)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_retouch_workspaces "
                "WHERE workspace_id = ? AND owner_account_id = ?",
                (workspace_id, account_id),
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"retouch workspace {workspace_id!r} was not found")
            if int(row["version"]) != expected_version:
                raise RetouchConflict("retouch workspace version is stale")
            if row["status"] != RetouchWorkspaceStatus.SUBMITTED.value or not row[
                "submitted_job_id"
            ]:
                raise RetouchConflict("only a failed or cancelled submitted workspace can reopen")
            job = connection.execute(
                "SELECT status FROM artifact_retouch_jobs WHERE job_id = ?",
                (row["submitted_job_id"],),
            ).fetchone()
            if job is None or job["status"] not in {
                RetouchJobStatus.FAILED.value,
                RetouchJobStatus.CANCELLED.value,
            }:
                raise RetouchConflict("retouch job is not failed or cancelled")
            connection.execute(
                "UPDATE artifact_retouch_workspaces SET status = 'editing', "
                "version = version + 1, submitted_job_id = NULL, "
                "submit_client_request_id = NULL, updated_at = ? WHERE workspace_id = ?",
                (timestamp, workspace_id),
            )
            updated = connection.execute(
                "SELECT * FROM artifact_retouch_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            assert updated is not None
            return self._retouch_workspace(updated)

    def create_and_attach_rendition(
        self,
        *,
        parent_artifact_id: str,
        expected_parent_revision_id: str,
        blob: StoredBlob,
        requested_name: str,
        mime_type: str,
        decision: ClassificationDecision,
        kind: RenditionKind,
        now: datetime,
    ) -> ArtifactProjection:
        """Create the internal child and attach it under one SQLite write lock."""

        if decision.role is not ArtifactRole.RENDITION or decision.visibility is not ArtifactVisibility.INTERNAL:
            raise RetouchConflict("rendition classification must be role=rendition and internal")
        kind = RenditionKind(kind)
        requested_name = sanitize_display_filename(requested_name)
        created_at = isoformat_utc(now)
        with self._write() as connection:
            parent = connection.execute(
                """
                SELECT current_revision_id, visibility, owner_account_id,
                       thread_id, turn_id, created_by_tool_id
                FROM artifact_entities
                WHERE artifact_id = ?
                """,
                (parent_artifact_id,),
            ).fetchone()
            if parent is None or parent["visibility"] not in {"primary", "secondary"}:
                raise ArtifactNotFound(f"artifact {parent_artifact_id!r} was not found")
            if parent["current_revision_id"] != expected_parent_revision_id:
                raise RetouchConflict("parent_revision_id is stale")
            self._require_revision_owner(
                connection, parent_artifact_id, expected_parent_revision_id
            )

            rendition_artifact_id = new_artifact_id()
            rendition_revision_id = new_revision_id()
            display_name = self._claim_display_name(
                connection, requested_name, now, created_at
            )
            connection.execute(
                """
                INSERT INTO artifact_entities(
                    artifact_id, family, role, visibility, status, actions_json,
                    classification_reasons_json, current_revision_id,
                    owner_account_id, thread_id, turn_id, created_by_tool_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    rendition_artifact_id,
                    decision.family.value,
                    decision.role.value,
                    decision.visibility.value,
                    ArtifactStatus.READY.value,
                    _canonical_json([action.value for action in decision.actions]),
                    _canonical_json(list(decision.reasons)),
                    parent["owner_account_id"],
                    parent["thread_id"],
                    parent["turn_id"],
                    parent["created_by_tool_id"],
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO artifact_revisions(
                    revision_id, artifact_id, revision_number, requested_name, display_name,
                    mime_type, size_bytes, sha256, source_artifact_ids_json,
                    supersedes_revision_id, quality_evidence_json, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    rendition_revision_id,
                    rendition_artifact_id,
                    requested_name,
                    display_name,
                    mime_type,
                    blob.size_bytes,
                    blob.sha256,
                    _canonical_json([parent_artifact_id]),
                    _canonical_json(QualityEvidence().to_dict()),
                    created_at,
                ),
            )
            connection.execute(
                "UPDATE artifact_entities SET current_revision_id = ? WHERE artifact_id = ?",
                (rendition_revision_id, rendition_artifact_id),
            )
            self._insert_lineage_sources(
                connection, rendition_revision_id, (parent_artifact_id,)
            )
            connection.execute(
                """
                INSERT INTO artifact_renditions(
                    parent_revision_id, kind, rendition_artifact_id,
                    rendition_revision_id, attached_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(parent_revision_id, kind) DO UPDATE SET
                    rendition_artifact_id = excluded.rendition_artifact_id,
                    rendition_revision_id = excluded.rendition_revision_id,
                    attached_at = excluded.attached_at
                """,
                (
                    expected_parent_revision_id,
                    kind.value,
                    rendition_artifact_id,
                    rendition_revision_id,
                    created_at,
                ),
            )
            return self._projection_for_revision(
                connection,
                parent_artifact_id,
                expected_parent_revision_id,
                include_internal_lineage=False,
            )

    def attach_rendition(
        self,
        *,
        parent_artifact_id: str,
        parent_revision_id: str,
        rendition_artifact_id: str,
        rendition_revision_id: str,
        kind: RenditionKind,
        now: datetime,
    ) -> ArtifactProjection:
        kind = RenditionKind(kind)
        with self._write() as connection:
            current = connection.execute(
                "SELECT current_revision_id FROM artifact_entities WHERE artifact_id = ?",
                (parent_artifact_id,),
            ).fetchone()
            if current is None:
                raise ArtifactNotFound(f"artifact {parent_artifact_id!r} was not found")
            if current["current_revision_id"] != parent_revision_id:
                raise RetouchConflict("parent_revision_id is stale")
            self._require_revision_owner(connection, parent_artifact_id, parent_revision_id)
            rendition = self._require_revision_owner(
                connection, rendition_artifact_id, rendition_revision_id
            )
            rendition_entity = connection.execute(
                "SELECT visibility, role FROM artifact_entities WHERE artifact_id = ?",
                (rendition_artifact_id,),
            ).fetchone()
            if rendition_entity is None or rendition_entity["visibility"] != "internal" or rendition_entity["role"] != "rendition":
                raise RetouchConflict("rendition artifacts must be role=rendition and visibility=internal")
            connection.execute(
                """
                INSERT INTO artifact_renditions(
                    parent_revision_id, kind, rendition_artifact_id, rendition_revision_id, attached_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(parent_revision_id, kind) DO UPDATE SET
                    rendition_artifact_id = excluded.rendition_artifact_id,
                    rendition_revision_id = excluded.rendition_revision_id,
                    attached_at = excluded.attached_at
                """,
                (
                    parent_revision_id,
                    kind.value,
                    rendition_artifact_id,
                    rendition_revision_id,
                    isoformat_utc(now),
                ),
            )
            del rendition
            return self._projection_for_revision(
                connection,
                parent_artifact_id,
                parent_revision_id,
                include_internal_lineage=False,
            )

    def record_feedback(
        self,
        artifact_id: str,
        request: FeedbackRequest,
        *,
        now: datetime,
        on_recorded: Callable[
            [sqlite3.Connection, FeedbackRecord, ArtifactScope], None
        ]
        | None = None,
    ) -> FeedbackRecord:
        with self._write() as connection:
            entity = connection.execute(
                "SELECT visibility, actions_json, owner_account_id, thread_id, "
                "turn_id, created_by_tool_id FROM artifact_entities WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if entity is None or entity["visibility"] not in {"primary", "secondary"}:
                raise ArtifactNotFound(f"artifact {artifact_id!r} was not found")
            actions = set(_decode_json(entity["actions_json"], []))
            if ArtifactAction.FEEDBACK.value not in actions:
                raise ArtifactActionUnavailable("feedback is unavailable for this artifact")
            self._require_revision_owner(connection, artifact_id, request.revision_id)

            existing = connection.execute(
                """
                SELECT feedback_id, artifact_id, revision_id, signal, client_request_id, recorded_at
                FROM artifact_feedback WHERE artifact_id = ? AND client_request_id = ?
                """,
                (artifact_id, request.client_request_id),
            ).fetchone()
            if existing is not None:
                record = self._feedback_record(existing)
                if record.revision_id != request.revision_id or record.signal != request.signal:
                    raise IdempotencyConflict(
                        "client_request_id was already used with a different feedback payload"
                    )
                if on_recorded is not None:
                    on_recorded(
                        connection,
                        record,
                        ArtifactScope(
                            account_id=entity["owner_account_id"],
                            thread_id=entity["thread_id"],
                            turn_id=entity["turn_id"],
                            created_by_tool_id=entity["created_by_tool_id"],
                        ),
                    )
                return record

            record = FeedbackRecord(
                feedback_id=new_feedback_id(),
                artifact_id=artifact_id,
                revision_id=request.revision_id,
                signal=request.signal,
                client_request_id=request.client_request_id,
                recorded_at=isoformat_utc(now),
            )
            connection.execute(
                """
                INSERT INTO artifact_feedback(
                    feedback_id, artifact_id, revision_id, signal, client_request_id, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.feedback_id,
                    record.artifact_id,
                    record.revision_id,
                    record.signal.value,
                    record.client_request_id,
                    record.recorded_at,
                ),
            )
            if on_recorded is not None:
                on_recorded(
                    connection,
                    record,
                    ArtifactScope(
                        account_id=entity["owner_account_id"],
                        thread_id=entity["thread_id"],
                        turn_id=entity["turn_id"],
                        created_by_tool_id=entity["created_by_tool_id"],
                    ),
                )
            return record

    def prepare_external_action(
        self,
        artifact_id: str,
        *,
        revision_id: str,
        action: ArtifactAction,
        client_request_id: str,
        account_id: str,
        now: datetime,
        on_prepared: Callable[
            [sqlite3.Connection, ArtifactExternalActionReceipt, ArtifactScope], None
        ]
        | None = None,
    ) -> ArtifactExternalActionReceipt:
        """Validate authority and durably bind one non-replayable OS action."""

        action = ArtifactAction(action)
        if action not in {ArtifactAction.OPEN, ArtifactAction.REVEAL}:
            raise ArtifactActionUnavailable("artifact action is not an external OS action")
        request_payload = {
            "artifact_id": artifact_id,
            "revision_id": revision_id,
            "action": action.value,
            "client_request_id": client_request_id,
        }
        request_digest = hashlib.sha256(
            _canonical_json(request_payload).encode("utf-8")
        ).hexdigest()
        timestamp = isoformat_utc(now)
        with self._write() as connection:
            entity = connection.execute(
                "SELECT visibility, status, actions_json, current_revision_id, "
                "owner_account_id, thread_id, turn_id, created_by_tool_id "
                "FROM artifact_entities WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if (
                entity is None
                or entity["owner_account_id"] != account_id
                or entity["visibility"] not in {"primary", "secondary"}
            ):
                raise ArtifactNotFound(f"artifact {artifact_id!r} was not found")
            actions = set(_decode_json(entity["actions_json"], []))
            if action.value not in actions:
                raise ArtifactActionUnavailable(
                    f"{action.value} is unavailable for this artifact"
                )
            if entity["status"] != ArtifactStatus.READY.value:
                raise ArtifactActionUnavailable(
                    f"{action.value} requires a ready artifact"
                )
            if entity["current_revision_id"] != revision_id:
                raise IdempotencyConflict("artifact revision changed; refresh and try again")
            self._require_revision_owner(connection, artifact_id, revision_id)

            existing = connection.execute(
                "SELECT * FROM artifact_external_actions "
                "WHERE artifact_id = ? AND client_request_id = ?",
                (artifact_id, client_request_id),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise IdempotencyConflict(
                        "client_request_id was already used with a different artifact action"
                    )
                receipt = self._external_action_receipt(existing)
                if on_prepared is not None:
                    on_prepared(
                        connection,
                        receipt,
                        ArtifactScope(
                            account_id=entity["owner_account_id"],
                            thread_id=entity["thread_id"],
                            turn_id=entity["turn_id"],
                            created_by_tool_id=entity["created_by_tool_id"],
                        ),
                    )
                return receipt

            connection.execute(
                "INSERT INTO artifact_external_actions("
                "artifact_id, revision_id, action, client_request_id, request_digest, "
                "status, failure_code, requested_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    artifact_id,
                    revision_id,
                    action.value,
                    client_request_id,
                    request_digest,
                    ArtifactExternalActionStatus.PREPARED.value,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM artifact_external_actions "
                "WHERE artifact_id = ? AND client_request_id = ?",
                (artifact_id, client_request_id),
            ).fetchone()
            assert row is not None
            receipt = self._external_action_receipt(row)
            if on_prepared is not None:
                on_prepared(
                    connection,
                    receipt,
                    ArtifactScope(
                        account_id=entity["owner_account_id"],
                        thread_id=entity["thread_id"],
                        turn_id=entity["turn_id"],
                        created_by_tool_id=entity["created_by_tool_id"],
                    ),
                )
            return receipt

    def transition_external_action(
        self,
        receipt: ArtifactExternalActionReceipt,
        *,
        expected: ArtifactExternalActionStatus,
        target: ArtifactExternalActionStatus,
        now: datetime,
        failure_code: str | None = None,
    ) -> ArtifactExternalActionReceipt:
        """Compare-and-swap an action receipt without ever persisting a path."""

        expected = ArtifactExternalActionStatus(expected)
        target = ArtifactExternalActionStatus(target)
        allowed = {
            (ArtifactExternalActionStatus.PREPARED, ArtifactExternalActionStatus.LAUNCHING),
            (ArtifactExternalActionStatus.PREPARED, ArtifactExternalActionStatus.FAILED),
            (ArtifactExternalActionStatus.LAUNCHING, ArtifactExternalActionStatus.COMPLETED),
            (ArtifactExternalActionStatus.LAUNCHING, ArtifactExternalActionStatus.FAILED),
        }
        if (expected, target) not in allowed:
            raise ValueError("invalid external artifact action transition")
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_external_actions "
                "WHERE artifact_id = ? AND client_request_id = ?",
                (receipt.artifact_id, receipt.client_request_id),
            ).fetchone()
            if row is None:
                raise ArtifactNotFound("artifact action receipt was not found")
            current = self._external_action_receipt(row)
            if (
                current.revision_id != receipt.revision_id
                or current.action is not receipt.action
            ):
                raise IdempotencyConflict("artifact action receipt identity changed")
            if current.status is target:
                return current
            if current.status is not expected:
                return current
            connection.execute(
                "UPDATE artifact_external_actions SET status = ?, failure_code = ?, "
                "updated_at = ? WHERE artifact_id = ? AND client_request_id = ? "
                "AND status = ?",
                (
                    target.value,
                    failure_code,
                    isoformat_utc(now),
                    receipt.artifact_id,
                    receipt.client_request_id,
                    expected.value,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM artifact_external_actions "
                "WHERE artifact_id = ? AND client_request_id = ?",
                (receipt.artifact_id, receipt.client_request_id),
            ).fetchone()
            assert updated is not None
            return self._external_action_receipt(updated)

    def claim_external_action_launch(
        self,
        receipt: ArtifactExternalActionReceipt,
        *,
        now: datetime,
    ) -> tuple[ArtifactExternalActionReceipt, bool]:
        """Claim the single allowed OS-launch attempt for this receipt."""

        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE artifact_external_actions SET status = ?, updated_at = ? "
                "WHERE artifact_id = ? AND client_request_id = ? AND revision_id = ? "
                "AND action = ? AND status = ?",
                (
                    ArtifactExternalActionStatus.LAUNCHING.value,
                    isoformat_utc(now),
                    receipt.artifact_id,
                    receipt.client_request_id,
                    receipt.revision_id,
                    receipt.action.value,
                    ArtifactExternalActionStatus.PREPARED.value,
                ),
            )
            row = connection.execute(
                "SELECT * FROM artifact_external_actions "
                "WHERE artifact_id = ? AND client_request_id = ?",
                (receipt.artifact_id, receipt.client_request_id),
            ).fetchone()
            if row is None:
                raise ArtifactNotFound("artifact action receipt was not found")
            current = self._external_action_receipt(row)
            if (
                current.revision_id != receipt.revision_id
                or current.action is not receipt.action
            ):
                raise IdempotencyConflict("artifact action receipt identity changed")
            return current, cursor.rowcount == 1

    def validate_retouch_request(self, artifact_id: str, request: RetouchRequest) -> None:
        with self._read() as connection:
            self._validate_retouch_request_in_connection(connection, artifact_id, request)

    @staticmethod
    def _validate_retouch_request_in_connection(
        connection: sqlite3.Connection,
        artifact_id: str,
        request: RetouchRequest,
    ) -> sqlite3.Row:
        entity = connection.execute(
            """
            SELECT family, visibility, status, actions_json, current_revision_id,
                   owner_account_id, thread_id, turn_id, created_by_tool_id
            FROM artifact_entities WHERE artifact_id = ?
            """,
            (artifact_id,),
        ).fetchone()
        if entity is None or entity["visibility"] not in {"primary", "secondary"}:
            raise ArtifactNotFound(f"artifact {artifact_id!r} was not found")
        if entity["family"] != ArtifactFamily.IMAGE.value:
            raise ArtifactActionUnavailable("precise retouch is only available for image artifacts")
        if entity["status"] != ArtifactStatus.READY.value:
            raise ArtifactActionUnavailable("precise retouch requires a ready image artifact")
        actions = set(_decode_json(entity["actions_json"], []))
        if ArtifactAction.PRECISE_RETOUCH.value not in actions:
            raise ArtifactActionUnavailable("precise retouch is unavailable for this image")
        base_revision = ArtifactRepository._require_revision_owner(
            connection, artifact_id, request.base_revision_id
        )
        if request.edit_surface is not None and request.edit_surface[
            "raster_digest"
        ] != base_revision["sha256"]:
            raise RetouchConflict(
                "retouch edit_surface digest does not match the immutable base revision"
            )
        if entity["current_revision_id"] != request.base_revision_id:
            raise RetouchConflict("base_revision_id is stale; refresh the artifact before retouching")
        if artifact_id not in request.selected_artifact_ids:
            raise RetouchConflict("selected_artifact_ids must include the target artifact")
        for related_id in (*request.selected_artifact_ids, *request.reference_artifact_ids):
            related = connection.execute(
                """
                SELECT family, visibility, status, owner_account_id, current_revision_id
                FROM artifact_entities
                WHERE artifact_id = ?
                """,
                (related_id,),
            ).fetchone()
            # Internal and missing references deliberately have the same result.
            if related is None or related["visibility"] not in {"primary", "secondary"}:
                raise ArtifactNotFound(f"related artifact {related_id!r} was not found")
            if related["owner_account_id"] != entity["owner_account_id"]:
                raise ArtifactNotFound(f"related artifact {related_id!r} was not found")
            if related["family"] != ArtifactFamily.IMAGE.value:
                raise ArtifactActionUnavailable("retouch inputs must be image artifacts")
            if related["status"] != ArtifactStatus.READY.value:
                raise ArtifactActionUnavailable("retouch inputs must be ready")
            pinned_revision = request.pinned_reference_revision_ids.get(related_id)
            if pinned_revision is not None and related["current_revision_id"] != pinned_revision:
                raise RetouchConflict(
                    "a pinned retouch reference changed; refresh the workspace before submitting"
                )
        return entity

    def find_retouch_job(self, artifact_id: str, client_request_id: str) -> RetouchJob | None:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT * FROM artifact_retouch_jobs
                WHERE artifact_id = ? AND client_request_id = ?
                """,
                (artifact_id, client_request_id),
            ).fetchone()
            return self._retouch_job(row) if row is not None else None

    def create_retouch_job(
        self,
        *,
        artifact_id: str,
        request: RetouchRequest,
        annotation_blob: StoredBlob,
        annotation_requested_name: str,
        annotation_mime_type: str,
        now: datetime,
        execution_scope: ArtifactScope | None = None,
        on_created: Callable[
            [sqlite3.Connection, RetouchJob, ArtifactScope], RetouchExecutionBinding
        ]
        | None = None,
        on_persisted: Callable[
            [sqlite3.Connection, RetouchJob, ArtifactScope], None
        ]
        | None = None,
    ) -> RetouchJob:
        request_json = _canonical_json(request.to_dict())
        request_digest = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        with self._write() as connection:
            existing = connection.execute(
                """
                SELECT * FROM artifact_retouch_jobs
                WHERE artifact_id = ? AND client_request_id = ?
                """,
                (artifact_id, request.client_request_id),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise IdempotencyConflict(
                        "client_request_id was already used with a different retouch payload"
                    )
                job = self._retouch_job(existing)
                scope: ArtifactScope | None = None
                if on_created is not None or on_persisted is not None:
                    target = self._validate_retouch_request_in_connection(
                        connection, artifact_id, request
                    )
                    scope = execution_scope or ArtifactScope(
                        account_id=target["owner_account_id"],
                        thread_id=target["thread_id"],
                        turn_id=target["turn_id"],
                        created_by_tool_id=target["created_by_tool_id"],
                    )
                    if scope.account_id != target["owner_account_id"]:
                        raise RetouchConflict("retouch execution scope account mismatch")
                if on_created is not None and job.durable_job_id is None:
                    assert scope is not None
                    binding = on_created(connection, job, scope)
                    if binding.thread_id != scope.thread_id:
                        raise RetouchConflict(
                            "retouch execution binding changed the owning Thread"
                        )
                    connection.execute(
                        "UPDATE artifact_retouch_jobs SET durable_job_id = ?, "
                        "execution_thread_id = ?, execution_turn_id = ?, updated_at = ? "
                        "WHERE job_id = ? AND durable_job_id IS NULL",
                        (
                            binding.durable_job_id,
                            binding.thread_id,
                            binding.turn_id,
                            isoformat_utc(now),
                            job.job_id,
                        ),
                    )
                    existing = connection.execute(
                        "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?",
                        (job.job_id,),
                    ).fetchone()
                    assert existing is not None
                    job = self._retouch_job(existing)
                if on_persisted is not None:
                    assert scope is not None
                    on_persisted(connection, job, scope)
                return job

            target = self._validate_retouch_request_in_connection(
                connection, artifact_id, request
            )
            job_id = new_retouch_job_id()
            created_at = isoformat_utc(now)
            external_idempotency_key = (
                "retouch-edit:"
                + hashlib.sha256(job_id.encode("utf-8")).hexdigest()
            )
            input_revision_ids: dict[str, str] = {
                artifact_id: request.base_revision_id
            }
            for related_id in (
                *request.selected_artifact_ids,
                *request.reference_artifact_ids,
            ):
                if related_id in input_revision_ids:
                    continue
                related_revision = connection.execute(
                    "SELECT current_revision_id FROM artifact_entities "
                    "WHERE artifact_id = ?",
                    (related_id,),
                ).fetchone()
                if related_revision is None or not related_revision["current_revision_id"]:
                    raise ArtifactNotFound(
                        f"related artifact {related_id!r} was not found"
                    )
                input_revision_ids[related_id] = related_revision[
                    "current_revision_id"
                ]
            annotation_layer_artifact_id = new_artifact_id()
            annotation_layer_revision_id = new_revision_id()
            annotation_requested_name = sanitize_display_filename(annotation_requested_name)
            annotation_display_name = self._claim_display_name(
                connection, annotation_requested_name, now, created_at
            )
            connection.execute(
                """
                INSERT INTO artifact_entities(
                    artifact_id, family, role, visibility, status, actions_json,
                    classification_reasons_json, current_revision_id,
                    owner_account_id, thread_id, turn_id, created_by_tool_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, '[]', ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    annotation_layer_artifact_id,
                    ArtifactFamily.TEMPORARY.value,
                    ArtifactRole.INTERMEDIATE.value,
                    ArtifactVisibility.INTERNAL.value,
                    ArtifactStatus.READY.value,
                    _canonical_json(["retouch_annotation_layer", "role_forces_internal"]),
                    target["owner_account_id"],
                    target["thread_id"],
                    target["turn_id"],
                    target["created_by_tool_id"],
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO artifact_revisions(
                    revision_id, artifact_id, revision_number, requested_name, display_name,
                    mime_type, size_bytes, sha256, source_artifact_ids_json,
                    supersedes_revision_id, quality_evidence_json, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    annotation_layer_revision_id,
                    annotation_layer_artifact_id,
                    annotation_requested_name,
                    annotation_display_name,
                    annotation_mime_type,
                    annotation_blob.size_bytes,
                    annotation_blob.sha256,
                    _canonical_json(list(request.selected_artifact_ids)),
                    _canonical_json(QualityEvidence().to_dict()),
                    created_at,
                ),
            )
            connection.execute(
                "UPDATE artifact_entities SET current_revision_id = ? WHERE artifact_id = ?",
                (annotation_layer_revision_id, annotation_layer_artifact_id),
            )
            self._insert_lineage_sources(
                connection, annotation_layer_revision_id, request.selected_artifact_ids
            )
            connection.execute(
                """
                INSERT INTO artifact_retouch_jobs(
                    job_id, artifact_id, base_revision_id, client_request_id,
                    request_json, request_digest, annotation_layer_artifact_id,
                    annotation_layer_revision_id, status, created_at,
                    external_idempotency_key, input_revisions_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    artifact_id,
                    request.base_revision_id,
                    request.client_request_id,
                    request_json,
                    request_digest,
                    annotation_layer_artifact_id,
                    annotation_layer_revision_id,
                    RetouchJobStatus.QUEUED.value,
                    created_at,
                    external_idempotency_key,
                    _canonical_json(input_revision_ids),
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert row is not None
            job = self._retouch_job(row)
            scope = execution_scope or ArtifactScope(
                account_id=target["owner_account_id"],
                thread_id=target["thread_id"],
                turn_id=target["turn_id"],
                created_by_tool_id=target["created_by_tool_id"],
            )
            if scope.account_id != target["owner_account_id"]:
                raise RetouchConflict("retouch execution scope account mismatch")
            if on_created is not None:
                binding = on_created(connection, job, scope)
                if binding.thread_id != scope.thread_id:
                    raise RetouchConflict(
                        "retouch execution binding changed the owning Thread"
                    )
                connection.execute(
                    "UPDATE artifact_retouch_jobs SET durable_job_id = ?, "
                    "execution_thread_id = ?, execution_turn_id = ? WHERE job_id = ?",
                    (
                        binding.durable_job_id,
                        binding.thread_id,
                        binding.turn_id,
                        job_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                assert row is not None
                job = self._retouch_job(row)
            if on_persisted is not None:
                on_persisted(connection, job, scope)
            return job

    def get_retouch_job(self, job_id: str) -> RetouchJob:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"retouch job {job_id!r} was not found")
            return self._retouch_job(row)

    def mark_retouch_running(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
        on_running: Callable[[sqlite3.Connection, RetouchJob], None] | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> RetouchJob:
        now = now or utc_now()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"retouch job {job_id!r} was not found")
            status = RetouchJobStatus(row["status"])
            if status in {RetouchJobStatus.QUEUED, RetouchJobStatus.RUNNING}:
                job = self._retouch_job(row)
                if on_running is not None:
                    on_running(connection, job)
                connection.execute(
                    "UPDATE artifact_retouch_jobs SET status = ?, updated_at = ? "
                    "WHERE job_id = ?",
                    (RetouchJobStatus.RUNNING.value, isoformat_utc(now), job_id),
                )
            elif status is not RetouchJobStatus.COMPLETED:
                raise RetouchConflict(f"retouch job cannot run from status {status.value}")
            updated = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert updated is not None
            if before_commit is not None:
                before_commit()
            return self._retouch_job(updated)

    def stage_retouch_result(
        self,
        job_id: str,
        result: RetouchStagedResult,
        *,
        now: datetime,
        before_commit: Callable[[], None] | None = None,
    ) -> RetouchJob:
        payload = _canonical_json(result.to_dict())
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"retouch job {job_id!r} was not found")
            status = RetouchJobStatus(row["status"])
            if status in {RetouchJobStatus.FAILED, RetouchJobStatus.CANCELLED}:
                raise RetouchConflict(
                    f"retouch result cannot be staged from status {status.value}"
                )
            existing = row["staged_result_json"]
            if status is RetouchJobStatus.COMPLETED:
                if existing == payload:
                    if before_commit is not None:
                        before_commit()
                    return self._retouch_job(row)
                raise IdempotencyConflict(
                    "completed retouch job received a different staged result"
                )
            if existing is not None and existing != payload:
                raise IdempotencyConflict(
                    "retouch adapter returned a different staged result"
                )
            if existing is None:
                connection.execute(
                    "UPDATE artifact_retouch_jobs SET staged_result_json = ?, "
                    "status = ?, updated_at = ? WHERE job_id = ?",
                    (
                        payload,
                        RetouchJobStatus.RUNNING.value,
                        isoformat_utc(now),
                        job_id,
                    ),
                )
            updated = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert updated is not None
            if before_commit is not None:
                before_commit()
            return self._retouch_job(updated)

    def requeue_retouch_job(
        self,
        job_id: str,
        *,
        now: datetime,
        on_requeued: Callable[[sqlite3.Connection, RetouchJob], None],
        before_commit: Callable[[], None] | None = None,
    ) -> RetouchJob:
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"retouch job {job_id!r} was not found")
            status = RetouchJobStatus(row["status"])
            if status not in {RetouchJobStatus.QUEUED, RetouchJobStatus.RUNNING}:
                raise RetouchConflict(
                    f"retouch job cannot retry from status {status.value}"
                )
            job = self._retouch_job(row)
            on_requeued(connection, job)
            connection.execute(
                "UPDATE artifact_retouch_jobs SET status = ?, failure_reason = NULL, "
                "updated_at = ? WHERE job_id = ?",
                (RetouchJobStatus.QUEUED.value, isoformat_utc(now), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert updated is not None
            if before_commit is not None:
                before_commit()
            return self._retouch_job(updated)

    def list_active_retouch_jobs(self) -> tuple[RetouchJob, ...]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE status IN (?, ?) "
                "ORDER BY created_at, job_id",
                (RetouchJobStatus.QUEUED.value, RetouchJobStatus.RUNNING.value),
            ).fetchall()
            return tuple(self._retouch_job(row) for row in rows)

    def complete_retouch_job(
        self,
        *,
        job_id: str,
        blob: StoredBlob,
        requested_name: str,
        mime_type: str,
        decision: ClassificationDecision,
        quality_evidence: QualityEvidence,
        change_summary: str,
        inspection_regions: Sequence[InspectionRegion],
        now: datetime,
        on_completed: Callable[
            [sqlite3.Connection, RetouchJob, ArtifactProjection], None
        ]
        | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> tuple[RetouchJob, ArtifactProjection]:
        if (
            decision.family is not ArtifactFamily.IMAGE
            or decision.role is not ArtifactRole.DELIVERABLE
            or decision.visibility not in {ArtifactVisibility.PRIMARY, ArtifactVisibility.SECONDARY}
        ):
            raise ArtifactActionUnavailable(
                "retouch output classification must remain a user-visible image"
            )
        requested_name = sanitize_display_filename(requested_name)
        completion_payload = {
            "sha256": blob.sha256,
            "requested_name": requested_name,
            "mime_type": mime_type,
            "quality_evidence": quality_evidence.to_dict(),
            "change_summary": change_summary,
            "inspection_regions": [region.to_dict() for region in inspection_regions],
        }
        completion_digest = hashlib.sha256(
            _canonical_json(completion_payload).encode("utf-8")
        ).hexdigest()
        completed_at = isoformat_utc(now)
        with self._write() as connection:
            job_row = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise ArtifactNotFound(f"retouch job {job_id!r} was not found")
            existing_status = RetouchJobStatus(job_row["status"])
            if existing_status == RetouchJobStatus.COMPLETED:
                if job_row["completion_digest"] != completion_digest:
                    raise IdempotencyConflict(
                        "completed retouch job received a different completion payload"
                    )
                job = self._retouch_job(job_row)
                assert job.result_revision_id is not None
                artifact = self._projection_for_revision(
                    connection,
                    job.artifact_id,
                    job.result_revision_id,
                    include_internal_lineage=False,
                )
                if before_commit is not None:
                    before_commit()
                return job, artifact
            if existing_status not in {RetouchJobStatus.QUEUED, RetouchJobStatus.RUNNING}:
                raise RetouchConflict(
                    f"retouch job cannot complete from status {existing_status.value}"
                )

            entity = connection.execute(
                "SELECT * FROM artifact_entities WHERE artifact_id = ?",
                (job_row["artifact_id"],),
            ).fetchone()
            if entity is None:
                raise ArtifactNotFound(f"artifact {job_row['artifact_id']!r} was not found")
            if entity["current_revision_id"] != job_row["base_revision_id"]:
                raise RetouchConflict("base artifact changed before retouch completion")
            if (
                entity["family"] != decision.family.value
                or entity["role"] != decision.role.value
                or entity["visibility"] != decision.visibility.value
            ):
                raise RetouchConflict("retouch output would downgrade artifact classification")

            request = RetouchRequest.from_dict(_decode_json(job_row["request_json"], {}))
            revision_id = new_revision_id()
            display_name = self._claim_display_name(connection, requested_name, now, completed_at)
            revision_number_row = connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 AS next_number FROM artifact_revisions WHERE artifact_id = ?",
                (job_row["artifact_id"],),
            ).fetchone()
            assert revision_number_row is not None
            source_ids = tuple(
                dict.fromkeys(
                    (
                        *request.selected_artifact_ids,
                        *request.reference_artifact_ids,
                        job_row["annotation_layer_artifact_id"],
                    )
                )
            )
            self._require_source_artifacts(
                connection,
                source_ids,
                owner_account_id=entity["owner_account_id"],
            )
            connection.execute(
                """
                INSERT INTO artifact_revisions(
                    revision_id, artifact_id, revision_number, requested_name, display_name,
                    mime_type, size_bytes, sha256, source_artifact_ids_json,
                    supersedes_revision_id, quality_evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    job_row["artifact_id"],
                    revision_number_row["next_number"],
                    requested_name,
                    display_name,
                    mime_type,
                    blob.size_bytes,
                    blob.sha256,
                    _canonical_json(list(source_ids)),
                    job_row["base_revision_id"],
                    _canonical_json(quality_evidence.to_dict()),
                    completed_at,
                ),
            )
            connection.execute(
                "UPDATE artifact_entities SET current_revision_id = ?, status = ? WHERE artifact_id = ?",
                (revision_id, ArtifactStatus.READY.value, job_row["artifact_id"]),
            )
            self._insert_lineage_sources(connection, revision_id, source_ids)
            connection.execute(
                """
                UPDATE artifact_retouch_jobs SET
                    status = ?, result_revision_id = ?, result_sha256 = ?, completion_digest = ?,
                    change_summary = ?, inspection_regions_json = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    RetouchJobStatus.COMPLETED.value,
                    revision_id,
                    blob.sha256,
                    completion_digest,
                    change_summary,
                    _canonical_json([region.to_dict() for region in inspection_regions]),
                    completed_at,
                    job_id,
                ),
            )
            updated_job = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert updated_job is not None
            job = self._retouch_job(updated_job)
            artifact = self._projection_for_revision(
                connection,
                job_row["artifact_id"],
                revision_id,
                include_internal_lineage=False,
            )
            if on_completed is not None:
                on_completed(connection, job, artifact)
            if before_commit is not None:
                before_commit()
            return job, artifact

    def fail_retouch_job(
        self,
        job_id: str,
        reason: str,
        *,
        target: RetouchJobStatus = RetouchJobStatus.FAILED,
        now: datetime | None = None,
        on_terminal: Callable[[sqlite3.Connection, RetouchJob], None] | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> RetouchJob:
        failure_reason = str(reason or "").strip()
        if not failure_reason:
            raise ValueError("failure reason must not be empty")
        target = RetouchJobStatus(target)
        if target not in {RetouchJobStatus.FAILED, RetouchJobStatus.CANCELLED}:
            raise ValueError("retouch terminal target must be failed or cancelled")
        now = now or utc_now()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"retouch job {job_id!r} was not found")
            status = RetouchJobStatus(row["status"])
            if status == RetouchJobStatus.COMPLETED:
                raise RetouchConflict("completed retouch jobs cannot be failed")
            if status == target:
                if before_commit is not None:
                    before_commit()
                return self._retouch_job(row)
            if status in {RetouchJobStatus.FAILED, RetouchJobStatus.CANCELLED}:
                raise RetouchConflict(
                    f"retouch job is already terminal in status {status.value}"
                )
            connection.execute(
                "UPDATE artifact_retouch_jobs SET status = ?, failure_reason = ?, "
                "updated_at = ? WHERE job_id = ?",
                (target.value, failure_reason, isoformat_utc(now), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM artifact_retouch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert updated is not None
            job = self._retouch_job(updated)
            if on_terminal is not None:
                on_terminal(connection, job)
            if before_commit is not None:
                before_commit()
            return job

    @staticmethod
    def _require_revision_owner(
        connection: sqlite3.Connection,
        artifact_id: str,
        revision_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM artifact_revisions WHERE artifact_id = ? AND revision_id = ?",
            (artifact_id, revision_id),
        ).fetchone()
        if row is None:
            raise RevisionNotFound(
                f"revision {revision_id!r} does not belong to artifact {artifact_id!r}"
            )
        return row

    @staticmethod
    def _require_source_artifacts(
        connection: sqlite3.Connection,
        source_artifact_ids: Sequence[str],
        *,
        owner_account_id: str | None = None,
    ) -> None:
        for source_artifact_id in source_artifact_ids:
            row = connection.execute(
                "SELECT owner_account_id FROM artifact_entities WHERE artifact_id = ?",
                (source_artifact_id,),
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(
                    f"source artifact {source_artifact_id!r} was not found"
                )
            if (
                owner_account_id is not None
                and row["owner_account_id"] != owner_account_id
            ):
                raise ArtifactNotFound(
                    f"source artifact {source_artifact_id!r} was not found"
                )

    @staticmethod
    def _insert_lineage_sources(
        connection: sqlite3.Connection,
        revision_id: str,
        source_artifact_ids: Sequence[str],
    ) -> None:
        for source_order, source_artifact_id in enumerate(source_artifact_ids):
            connection.execute(
                """
                INSERT INTO artifact_lineage_sources(
                    revision_id, source_artifact_id, source_order
                ) VALUES (?, ?, ?)
                """,
                (revision_id, source_artifact_id, source_order),
            )

    def _projection_for_revision(
        self,
        connection: sqlite3.Connection,
        artifact_id: str,
        revision_id: str,
        *,
        include_internal_lineage: bool = True,
    ) -> ArtifactProjection:
        row = connection.execute(
            """
            SELECT
                e.artifact_id, e.family, e.role, e.visibility, e.status, e.actions_json,
                r.revision_id, r.display_name, r.mime_type, r.size_bytes, r.sha256,
                r.source_artifact_ids_json, r.supersedes_revision_id,
                r.quality_evidence_json, r.created_at
            FROM artifact_entities e
            JOIN artifact_revisions r ON r.artifact_id = e.artifact_id
            WHERE e.artifact_id = ? AND r.revision_id = ?
            """,
            (artifact_id, revision_id),
        ).fetchone()
        if row is None:
            raise RevisionNotFound(
                f"revision {revision_id!r} does not belong to artifact {artifact_id!r}"
            )

        rendition_rows = connection.execute(
            """
            SELECT ar.kind, r.artifact_id, r.revision_id, r.mime_type, r.size_bytes, r.sha256
            FROM artifact_renditions ar
            JOIN artifact_revisions r ON r.revision_id = ar.rendition_revision_id
            WHERE ar.parent_revision_id = ?
            ORDER BY ar.kind ASC
            """,
            (revision_id,),
        ).fetchall()
        feedback_row = connection.execute(
            """
            SELECT feedback_id, revision_id, signal, recorded_at
            FROM artifact_feedback
            WHERE artifact_id = ? AND revision_id = ?
            ORDER BY feedback_order DESC LIMIT 1
            """,
            (artifact_id, revision_id),
        ).fetchone()
        feedback = (
            FeedbackProjection(
                feedback_id=feedback_row["feedback_id"],
                revision_id=feedback_row["revision_id"],
                signal=FeedbackSignal(feedback_row["signal"]),
                recorded_at=feedback_row["recorded_at"],
            )
            if feedback_row is not None
            else None
        )
        source_artifact_ids = tuple(_decode_json(row["source_artifact_ids_json"], []))
        if not include_internal_lineage and source_artifact_ids:
            source_artifact_ids = tuple(
                source_artifact_id
                for source_artifact_id in source_artifact_ids
                if connection.execute(
                    """
                    SELECT 1 FROM artifact_entities
                    WHERE artifact_id = ? AND visibility IN ('primary', 'secondary')
                    """,
                    (source_artifact_id,),
                ).fetchone()
                is not None
            )

        return ArtifactProjection(
            artifact_id=row["artifact_id"],
            revision_id=row["revision_id"],
            family=ArtifactFamily(row["family"]),
            role=ArtifactRole(row["role"]),
            visibility=ArtifactVisibility(row["visibility"]),
            status=ArtifactStatus(row["status"]),
            display_name=row["display_name"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            created_at=row["created_at"],
            lineage=ArtifactLineage(
                source_artifact_ids=source_artifact_ids,
                supersedes_revision_id=row["supersedes_revision_id"],
            ),
            renditions=tuple(
                RenditionProjection(
                    kind=RenditionKind(rendition["kind"]),
                    mime_type=rendition["mime_type"],
                    size_bytes=rendition["size_bytes"],
                    sha256=rendition["sha256"],
                )
                for rendition in rendition_rows
            ),
            actions=tuple(ArtifactAction(value) for value in _decode_json(row["actions_json"], [])),
            feedback=feedback,
            quality_evidence=QualityEvidence.from_dict(
                _decode_json(row["quality_evidence_json"], {})
            ),
        )

    @staticmethod
    def _feedback_record(row: sqlite3.Row) -> FeedbackRecord:
        return FeedbackRecord(
            feedback_id=row["feedback_id"],
            artifact_id=row["artifact_id"],
            revision_id=row["revision_id"],
            signal=FeedbackSignal(row["signal"]),
            client_request_id=row["client_request_id"],
            recorded_at=row["recorded_at"],
        )

    @staticmethod
    def _external_action_receipt(row: sqlite3.Row) -> ArtifactExternalActionReceipt:
        return ArtifactExternalActionReceipt(
            artifact_id=row["artifact_id"],
            revision_id=row["revision_id"],
            action=ArtifactAction(row["action"]),
            client_request_id=row["client_request_id"],
            status=ArtifactExternalActionStatus(row["status"]),
            requested_at=row["requested_at"],
            updated_at=row["updated_at"],
            failure_code=row["failure_code"],
        )

    @staticmethod
    def _retouch_job(row: sqlite3.Row) -> RetouchJob:
        request = RetouchRequest.from_dict(_decode_json(row["request_json"], {}))
        return RetouchJob(
            job_id=row["job_id"],
            artifact_id=row["artifact_id"],
            base_revision_id=row["base_revision_id"],
            request=request,
            annotation_layer_artifact_id=row["annotation_layer_artifact_id"],
            annotation_layer_revision_id=row["annotation_layer_revision_id"],
            status=RetouchJobStatus(row["status"]),
            created_at=row["created_at"],
            result_revision_id=row["result_revision_id"],
            change_summary=row["change_summary"],
            inspection_regions=tuple(
                InspectionRegion.from_dict(item)
                for item in _decode_json(row["inspection_regions_json"], [])
            ),
            failure_reason=row["failure_reason"],
            durable_job_id=row["durable_job_id"],
            execution_thread_id=row["execution_thread_id"],
            execution_turn_id=row["execution_turn_id"],
            external_idempotency_key=row["external_idempotency_key"],
            staged_result=(
                RetouchStagedResult.from_dict(
                    _decode_json(row["staged_result_json"], {})
                )
                if row["staged_result_json"]
                else None
            ),
            input_revision_ids=_decode_json(row["input_revisions_json"], {}),
        )
