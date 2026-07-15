"""Safe export/materialization service for user-visible office artifacts.

The Artifact CAS remains authoritative.  This module only creates verified,
recoverable copies in one of three backend-configured user output locations.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from typing import Any

from ecorex.artifacts.identity import filename_claim_key, sanitize_display_filename, split_display_filename
from ecorex.artifacts.models import (
    ArtifactFamily,
    ArtifactRole,
    ArtifactStatus,
    ArtifactVisibility,
)
from ecorex.artifacts.service import ArtifactService

from .errors import (
    OutputArtifactNotEligible,
    OutputIdempotencyConflict,
    OutputIntegrityError,
    OutputLocationUnavailable,
    OutputMaterializationFailed,
    OutputPolicyBindingMissing,
    OutputPolicyNotFound,
    OutputRevisionConflict,
    OutputValidationError,
)
from .filesystem import SafeOutputFilesystem
from .models import (
    MaterializationProjection,
    MaterializationStatus,
    OutputAuditProjection,
    OutputLocationAlias,
    OutputLocationOption,
    OutputPolicyProjection,
    OutputPreferenceProjection,
)
from .repository import (
    OutputRepository,
    StoredMaterialization,
    StoredPolicy,
    canonical_json,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BLOCKED_FAMILIES = frozenset(
    {
        ArtifactFamily.SOURCE_CODE,
        ArtifactFamily.SCRIPT,
        ArtifactFamily.DIFF,
        ArtifactFamily.LOG,
        ArtifactFamily.TEMPORARY,
        ArtifactFamily.DIRECTORY,
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("output timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()


def _safe_id(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise OutputValidationError(f"{field} is invalid")
    return normalized


def _safe_account_id(value: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise OutputValidationError("account_id is invalid")
    return normalized


class OutputService:
    """Backend authority for output preferences and durable materialization."""

    def __init__(
        self,
        *,
        artifact_service: ArtifactService,
        database_path: str | Path,
        configured_roots: Mapping[OutputLocationAlias | str, str | Path],
        account_id: str = "local-user",
        default_alias: OutputLocationAlias | str = OutputLocationAlias.DOCUMENTS,
        runtime_database_path: str | Path | None = None,
        clock: Callable[[], datetime] = _now,
        fault_hook: Callable[[str, str], None] | None = None,
        prepare_output_roots: bool = True,
    ) -> None:
        self.artifacts = artifact_service
        output_database_path = Path(database_path).expanduser().resolve()
        self.runtime_database_path = Path(
            runtime_database_path or output_database_path
        ).expanduser().resolve()
        if output_database_path != self.runtime_database_path:
            raise OutputValidationError(
                "output facts must use the authoritative Runtime database"
            )
        artifact_database_path = getattr(
            getattr(artifact_service, "repository", None), "database_path", None
        )
        if (
            artifact_database_path is not None
            and Path(artifact_database_path).expanduser().resolve()
            != output_database_path
        ):
            raise OutputValidationError(
                "Artifact and output facts must share the Runtime database"
            )
        self.repository = OutputRepository(output_database_path)
        self.account_id = _safe_account_id(account_id)
        self.clock = clock
        self.fault_hook = fault_hook or (lambda _phase, _identity: None)
        self.default_alias = OutputLocationAlias(default_alias)
        effective_roots = dict(configured_roots)
        saved_workspace = self.repository.latest_policy_for_alias(
            self.account_id,
            OutputLocationAlias.WORKSPACE,
        )
        if saved_workspace is not None:
            effective_roots[OutputLocationAlias.WORKSPACE] = saved_workspace.root_path
        self.filesystem = SafeOutputFilesystem(
            effective_roots,
            prepare_roots=prepare_output_roots,
        )
        if not self.filesystem.is_configured(self.default_alias):
            raise OutputLocationUnavailable("the default output location is unavailable")

    def _account(self, requested: str | None) -> str:
        if requested is None:
            return self.account_id
        normalized = _safe_account_id(requested)
        if normalized != self.account_id:
            raise OutputValidationError(
                "the output request account does not match Runtime authority"
            )
        return normalized

    def close(self) -> None:
        self.filesystem.close()

    async def aclose(self) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Location preferences and immutable policy snapshots
    # ------------------------------------------------------------------
    def location_catalog(self) -> tuple[OutputLocationOption, ...]:
        return tuple(
            OutputLocationOption(
                alias=alias, available=self.filesystem.is_configured(alias)
            )
            for alias in OutputLocationAlias
        )

    def get_preference(self, *, account_id: str | None = None) -> OutputPreferenceProjection:
        account_id = self._account(account_id)
        existing = self.repository.get_preference(account_id)
        if existing is not None:
            return existing
        root = self.filesystem.inspect_configured_root(self.default_alias)
        when = _time(self.clock())
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM output_preferences WHERE account_id = ?", (account_id,)
            ).fetchone()
            if row is not None:
                return self.repository.preference_from_row(row)
            policy_id = self._insert_policy(
                connection,
                account_id=account_id,
                revision=1,
                alias=self.default_alias,
                root=root,
                created_at=when,
            )
            connection.execute(
                "INSERT INTO output_preferences(account_id, location_alias, revision, "
                "output_policy_snapshot_id, updated_at) VALUES (?, ?, 1, ?, ?)",
                (account_id, self.default_alias.value, policy_id, when),
            )
            connection.execute(
                "INSERT INTO output_preference_history(account_id, revision, location_alias, "
                "output_policy_snapshot_id, client_request_id, created_at) "
                "VALUES (?, 1, ?, ?, NULL, ?)",
                (account_id, self.default_alias.value, policy_id, when),
            )
            self._audit(
                connection,
                account_id=account_id,
                action="output.preference.initialized",
                subject_id=policy_id,
                details={"location_alias": self.default_alias.value, "revision": 1},
                created_at=when,
            )
            row = connection.execute(
                "SELECT * FROM output_preferences WHERE account_id = ?", (account_id,)
            ).fetchone()
        return self.repository.preference_from_row(row)

    def project_preference(
        self, *, account_id: str | None = None
    ) -> OutputPreferenceProjection:
        """Return the effective preference without lazy-initialization writes."""

        account_id = self._account(account_id)
        existing = self.repository.get_preference(account_id)
        if existing is not None:
            return existing
        fingerprint = self.filesystem.project_root_fingerprint(self.default_alias)
        policy_id = "outpol_" + _digest(
            {
                "contract_version": "1.0",
                "account_id": account_id,
                "preference_revision": 1,
                "location_alias": self.default_alias.value,
                "root_fingerprint": fingerprint,
            }
        )
        return OutputPreferenceProjection(
            account_id=account_id,
            location_alias=self.default_alias,
            revision=1,
            output_policy_snapshot_id=policy_id,
            updated_at="1970-01-01T00:00:00+00:00",
        )

    def set_preference(
        self,
        location_alias: OutputLocationAlias | str,
        *,
        expected_revision: int,
        client_request_id: str,
        account_id: str | None = None,
    ) -> OutputPreferenceProjection:
        account_id = self._account(account_id)
        request_id = _safe_id(client_request_id, "client_request_id")
        try:
            alias = OutputLocationAlias(location_alias)
        except ValueError as error:
            raise OutputLocationUnavailable("the selected output location is unavailable") from error
        if not self.filesystem.is_configured(alias):
            raise OutputLocationUnavailable("the selected output location is unavailable")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise OutputValidationError("expected_revision must be a positive integer")

        # Factory initialization is also serialized, so the mutation below can
        # always use a conventional compare-and-swap revision.
        self.get_preference(account_id=account_id)
        inspected_root = self.filesystem.inspect_configured_root(alias)
        request_digest = _digest(
            {
                "operation": "set_preference",
                "location_alias": alias.value,
                "expected_revision": expected_revision,
                "root_fingerprint": inspected_root[3],
            }
        )
        when = _time(self.clock())
        with self.repository.transaction() as connection:
            duplicate = connection.execute(
                "SELECT * FROM output_idempotency WHERE account_id = ? AND client_request_id = ?",
                (account_id, request_id),
            ).fetchone()
            if duplicate is not None:
                self._validate_idempotency(duplicate, "set_preference", request_digest)
                return self._preference_for_policy(connection, account_id, duplicate["result_id"])

            current_row = connection.execute(
                "SELECT * FROM output_preferences WHERE account_id = ?", (account_id,)
            ).fetchone()
            current = self.repository.preference_from_row(current_row)
            if current.revision != expected_revision:
                raise OutputRevisionConflict("the output preference changed; refresh and try again")
            current_policy_row = connection.execute(
                "SELECT * FROM output_policy_snapshots WHERE output_policy_snapshot_id = ?",
                (current.output_policy_snapshot_id,),
            ).fetchone()
            current_policy = self.repository.policy_from_row(current_policy_row)

            # Recheck at the commit boundary; a root replaced between the first
            # inspection and this transaction is never captured as authoritative.
            inspected_root = self.filesystem.inspect_configured_root(alias)
            same_root = (
                current.location_alias is alias
                and current_policy.root_path == str(inspected_root[0])
                and current_policy.root_device == inspected_root[1]
                and current_policy.root_inode == inspected_root[2]
                and current_policy.root_fingerprint == inspected_root[3]
            )
            if same_root:
                result = current
                policy_id = current.output_policy_snapshot_id
                action = "output.preference.unchanged"
            else:
                revision = current.revision + 1
                policy_id = self._insert_policy(
                    connection,
                    account_id=account_id,
                    revision=revision,
                    alias=alias,
                    root=inspected_root,
                    created_at=when,
                )
                connection.execute(
                    "UPDATE output_preferences SET location_alias = ?, revision = ?, "
                    "output_policy_snapshot_id = ?, updated_at = ? WHERE account_id = ?",
                    (alias.value, revision, policy_id, when, account_id),
                )
                connection.execute(
                    "INSERT INTO output_preference_history(account_id, revision, location_alias, "
                    "output_policy_snapshot_id, client_request_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (account_id, revision, alias.value, policy_id, request_id, when),
                )
                result = OutputPreferenceProjection(
                    account_id=account_id,
                    location_alias=alias,
                    revision=revision,
                    output_policy_snapshot_id=policy_id,
                    updated_at=when,
                )
                action = "output.preference.changed"
            connection.execute(
                "INSERT INTO output_idempotency(account_id, client_request_id, operation, "
                "request_digest, result_kind, result_id, created_at) "
                "VALUES (?, ?, 'set_preference', ?, 'policy', ?, ?)",
                (account_id, request_id, request_digest, policy_id, when),
            )
            self._audit(
                connection,
                account_id=account_id,
                action=action,
                subject_id=policy_id,
                details={"location_alias": alias.value, "revision": result.revision},
                created_at=when,
            )
        return result

    def current_policy(self, *, account_id: str | None = None) -> OutputPolicyProjection:
        account_id = self._account(account_id)
        preference = self.get_preference(account_id=account_id)
        return self.repository.get_policy(
            preference.output_policy_snapshot_id, account_id=account_id
        ).projection

    def get_policy(
        self, output_policy_snapshot_id: str, *, account_id: str | None = None
    ) -> OutputPolicyProjection:
        account_id = self._account(account_id)
        return self.repository.get_policy(
            _safe_id(output_policy_snapshot_id, "output_policy_snapshot_id"),
            account_id=account_id,
        ).projection

    # ------------------------------------------------------------------
    # Turn-frozen policy resolution
    # ------------------------------------------------------------------
    def resolve_policy_for_artifact(
        self,
        artifact_id: str,
        revision_id: str,
        *,
        account_id: str | None = None,
    ) -> OutputPolicyProjection:
        account_id = self._account(account_id)
        artifact_id = _safe_id(artifact_id, "artifact_id")
        revision_id = _safe_id(revision_id, "revision_id")
        # This also proves the requested revision belongs to the account.
        try:
            self.artifacts.repository.get_revision_projection(
                artifact_id,
                revision_id,
                include_internal=True,
                account_id=account_id,
            )
            scope = self.artifacts.get_artifact_scope(artifact_id)
        except Exception as error:
            from ecorex.artifacts.errors import ArtifactError

            if isinstance(error, ArtifactError):
                raise OutputArtifactNotEligible("the selected artifact revision is unavailable") from error
            raise

        # Artifacts without a creating Turn predate output-policy snapshots.
        # They alone may use the current preference as an explicit legacy rule.
        if scope.turn_id is None:
            return self.current_policy(account_id=account_id)
        if scope.thread_id is None:
            raise OutputPolicyBindingMissing("the artifact has no valid creating task")

        try:
            connection = sqlite3.connect(str(self.runtime_database_path), timeout=30.0)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT config_snapshot_id FROM events WHERE thread_id = ? AND turn_id = ? "
                "AND event_type = 'turn.accepted' ORDER BY seq LIMIT 2",
                (scope.thread_id, scope.turn_id),
            ).fetchall()
            if len(rows) != 1 or not rows[0]["config_snapshot_id"]:
                raise OutputPolicyBindingMissing(
                    "the task does not contain one frozen output policy"
                )
            snapshot = connection.execute(
                "SELECT kind, payload_json, payload_sha256 FROM runtime_snapshots "
                "WHERE snapshot_id = ?",
                (rows[0]["config_snapshot_id"],),
            ).fetchone()
        except OutputPolicyBindingMissing:
            raise
        except (OSError, sqlite3.Error) as error:
            raise OutputPolicyBindingMissing(
                "the task output policy cannot be verified"
            ) from error
        finally:
            if "connection" in locals():
                connection.close()
        if snapshot is None or snapshot["kind"] != "config":
            raise OutputPolicyBindingMissing("the task configuration snapshot is unavailable")
        payload_json = str(snapshot["payload_json"])
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != snapshot["payload_sha256"]:
            raise OutputIntegrityError("the task configuration snapshot failed verification")
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OutputIntegrityError("the task configuration snapshot is invalid") from error
        policy_id = payload.get("output_policy_snapshot_id") if isinstance(payload, dict) else None
        if not isinstance(policy_id, str) or not policy_id:
            raise OutputPolicyBindingMissing("the task predates a verifiable output policy binding")
        return self.repository.get_policy(policy_id, account_id=account_id).projection

    # ------------------------------------------------------------------
    # Durable Artifact-CAS materialization
    # ------------------------------------------------------------------
    def materialize_artifact_revision(
        self,
        artifact_id: str,
        revision_id: str,
        *,
        client_request_id: str,
        account_id: str | None = None,
    ) -> MaterializationProjection:
        account_id = self._account(account_id)
        artifact_id = _safe_id(artifact_id, "artifact_id")
        revision_id = _safe_id(revision_id, "revision_id")
        request_id = _safe_id(client_request_id, "client_request_id")
        projection = self._eligible_projection(artifact_id, revision_id, account_id=account_id)
        policy_projection = self.resolve_policy_for_artifact(
            artifact_id, revision_id, account_id=account_id
        )
        policy = self.repository.get_policy(
            policy_projection.output_policy_snapshot_id, account_id=account_id
        )

        source = self.filesystem.cas_source(
            self.artifacts.blobs, projection.sha256, projection.size_bytes
        )

        request_digest = _digest(
            {
                "operation": "materialize_artifact_revision",
                "artifact_id": artifact_id,
                "revision_id": revision_id,
                "output_policy_snapshot_id": policy.projection.output_policy_snapshot_id,
            }
        )
        stored = self._reserve_materialization(
            account_id=account_id,
            request_id=request_id,
            request_digest=request_digest,
            artifact_id=artifact_id,
            revision_id=revision_id,
            policy=policy,
            display_name=projection.display_name,
            sha256=projection.sha256,
            size_bytes=projection.size_bytes,
        )
        if stored.projection.status is MaterializationStatus.COMPLETED:
            self.filesystem.verify_completed(stored, policy, source)
            return stored.projection

        stored = self._start_attempt(stored.projection.materialization_id)
        reused = self.filesystem.publish(
            stored,
            policy,
            source,
            fault_hook=self.fault_hook,
            collision_handler=lambda item, observed: self._move_after_collision(
                item, observed_sha256=observed
            ),
        )
        self._mark_published(stored.projection.materialization_id, reused_existing=reused)
        self.fault_hook("after_published_record", stored.projection.materialization_id)
        return self._mark_completed(stored.projection.materialization_id)

    def get_materialization(
        self, materialization_id: str, *, account_id: str | None = None
    ) -> MaterializationProjection:
        account_id = self._account(account_id)
        stored = self.repository.get_materialization(
            _safe_id(materialization_id, "materialization_id")
        )
        if stored is None or stored.account_id != account_id:
            raise OutputMaterializationFailed("the output receipt is unavailable")
        return stored.projection

    def list_audit(
        self, *, account_id: str | None = None, limit: int = 200
    ) -> tuple[OutputAuditProjection, ...]:
        return self.repository.list_audit(
            account_id=self._account(account_id), limit=limit
        )

    # ------------------------------------------------------------------
    # Preference persistence helpers
    # ------------------------------------------------------------------
    def _insert_policy(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        revision: int,
        alias: OutputLocationAlias,
        root: tuple[Path, int, int, str],
        created_at: str,
    ) -> str:
        path, device, inode, fingerprint = root
        identity = {
            "contract_version": "1.0",
            "account_id": account_id,
            "preference_revision": revision,
            "location_alias": alias.value,
            "root_fingerprint": fingerprint,
        }
        policy_id = "outpol_" + _digest(identity)
        connection.execute(
            "INSERT INTO output_policy_snapshots(output_policy_snapshot_id, account_id, "
            "preference_revision, location_alias, root_path, root_device, root_inode, "
            "root_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                policy_id,
                account_id,
                revision,
                alias.value,
                str(path),
                device,
                inode,
                fingerprint,
                created_at,
            ),
        )
        return policy_id

    def _preference_for_policy(
        self, connection: sqlite3.Connection, account_id: str, policy_id: str
    ) -> OutputPreferenceProjection:
        row = connection.execute(
            "SELECT h.account_id, h.location_alias, h.revision, h.output_policy_snapshot_id, "
            "h.created_at AS updated_at FROM output_preference_history h "
            "WHERE h.account_id = ? AND h.output_policy_snapshot_id = ?",
            (account_id, policy_id),
        ).fetchone()
        if row is None:
            raise OutputPolicyNotFound("the recorded output preference is unavailable")
        return self.repository.preference_from_row(row)

    def select_workspace_location(
        self,
        root: str | Path,
        *,
        expected_revision: int,
        client_request_id: str,
        account_id: str | None = None,
    ) -> OutputPreferenceProjection:
        """Bind a native-folder-picker result without exposing its path to WebUI."""

        previous = self.filesystem.configured_root_path(OutputLocationAlias.WORKSPACE)
        self.filesystem.replace_configured_root(OutputLocationAlias.WORKSPACE, root)
        try:
            return self.set_preference(
                OutputLocationAlias.WORKSPACE,
                expected_revision=expected_revision,
                client_request_id=client_request_id,
                account_id=account_id,
            )
        except BaseException:
            self.filesystem.replace_configured_root(
                OutputLocationAlias.WORKSPACE,
                previous,
            )
            raise

    @staticmethod
    def _validate_idempotency(row: sqlite3.Row, operation: str, request_digest: str) -> None:
        if row["operation"] != operation or row["request_digest"] != request_digest:
            raise OutputIdempotencyConflict(
                "client_request_id was already used for a different output request"
            )

    # ------------------------------------------------------------------
    # Materialization persistence helpers
    # ------------------------------------------------------------------
    def _reserve_materialization(
        self,
        *,
        account_id: str,
        request_id: str,
        request_digest: str,
        artifact_id: str,
        revision_id: str,
        policy: StoredPolicy,
        display_name: str,
        sha256: str,
        size_bytes: int,
    ) -> StoredMaterialization:
        identity_digest = _digest(
            {
                "account_id": account_id,
                "artifact_id": artifact_id,
                "revision_id": revision_id,
                "output_policy_snapshot_id": policy.projection.output_policy_snapshot_id,
            }
        )
        materialization_id = "mat_" + identity_digest
        when = _time(self.clock())
        candidates = self._candidate_names(display_name, revision_id, sha256)
        with self.repository.transaction() as connection:
            duplicate = connection.execute(
                "SELECT * FROM output_idempotency WHERE account_id = ? AND client_request_id = ?",
                (account_id, request_id),
            ).fetchone()
            if duplicate is not None:
                self._validate_idempotency(
                    duplicate, "materialize_artifact_revision", request_digest
                )
                row = connection.execute(
                    "SELECT * FROM output_materializations WHERE materialization_id = ?",
                    (duplicate["result_id"],),
                ).fetchone()
                if row is None:
                    raise OutputIntegrityError("the output idempotency receipt is incomplete")
                return self.repository.materialization_from_row(row)

            row = connection.execute(
                "SELECT * FROM output_materializations WHERE materialization_id = ?",
                (materialization_id,),
            ).fetchone()
            if row is None:
                chosen = self._claim_candidate(
                    connection,
                    policy_id=policy.projection.output_policy_snapshot_id,
                    candidates=candidates,
                    sha256=sha256,
                    when=when,
                )
                key = filename_claim_key(chosen)
                connection.execute(
                    "INSERT INTO output_materializations(materialization_id, account_id, "
                    "artifact_id, revision_id, output_policy_snapshot_id, location_alias, "
                    "display_name, display_name_key, sha256, size_bytes, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?)",
                    (
                        materialization_id,
                        account_id,
                        artifact_id,
                        revision_id,
                        policy.projection.output_policy_snapshot_id,
                        policy.projection.location_alias.value,
                        chosen,
                        key,
                        sha256,
                        size_bytes,
                        when,
                    ),
                )
                self._audit(
                    connection,
                    account_id=account_id,
                    action="output.materialization.prepared",
                    subject_id=materialization_id,
                    details={
                        "artifact_id": artifact_id,
                        "revision_id": revision_id,
                        "location_alias": policy.projection.location_alias.value,
                        "display_name": chosen,
                    },
                    created_at=when,
                )
            else:
                stored = self.repository.materialization_from_row(row)
                expected = (
                    stored.account_id == account_id
                    and stored.projection.artifact_id == artifact_id
                    and stored.projection.revision_id == revision_id
                    and stored.projection.output_policy_snapshot_id
                    == policy.projection.output_policy_snapshot_id
                    and stored.projection.sha256 == sha256
                    and stored.projection.size_bytes == size_bytes
                )
                if not expected:
                    raise OutputIntegrityError("the output materialization identity is inconsistent")
            connection.execute(
                "INSERT INTO output_idempotency(account_id, client_request_id, operation, "
                "request_digest, result_kind, result_id, created_at) "
                "VALUES (?, ?, 'materialize_artifact_revision', ?, 'materialization', ?, ?)",
                (account_id, request_id, request_digest, materialization_id, when),
            )
            row = connection.execute(
                "SELECT * FROM output_materializations WHERE materialization_id = ?",
                (materialization_id,),
            ).fetchone()
        return self.repository.materialization_from_row(row)

    def _start_attempt(self, materialization_id: str) -> StoredMaterialization:
        with self.repository.transaction() as connection:
            connection.execute(
                "UPDATE output_materializations SET attempt_count = attempt_count + 1 "
                "WHERE materialization_id = ? AND status != 'completed'",
                (materialization_id,),
            )
            row = connection.execute(
                "SELECT * FROM output_materializations WHERE materialization_id = ?",
                (materialization_id,),
            ).fetchone()
        if row is None:
            raise OutputMaterializationFailed("the output receipt disappeared")
        return self.repository.materialization_from_row(row)

    def _mark_published(self, materialization_id: str, *, reused_existing: bool) -> None:
        when = _time(self.clock())
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM output_materializations WHERE materialization_id = ?",
                (materialization_id,),
            ).fetchone()
            if row is None:
                raise OutputMaterializationFailed("the output receipt disappeared")
            if row["status"] == "completed":
                return
            connection.execute(
                "UPDATE output_materializations SET status = 'published', "
                "reused_existing = CASE WHEN reused_existing = 1 OR ? = 1 THEN 1 ELSE 0 END, "
                "published_at = COALESCE(published_at, ?) WHERE materialization_id = ?",
                (1 if reused_existing else 0, when, materialization_id),
            )
            self._audit(
                connection,
                account_id=str(row["account_id"]),
                action="output.materialization.published",
                subject_id=materialization_id,
                details={
                    "display_name": str(row["display_name"]),
                    "reused_existing": reused_existing,
                    "attempt": int(row["attempt_count"]),
                },
                created_at=when,
            )

    def _mark_completed(self, materialization_id: str) -> MaterializationProjection:
        when = _time(self.clock())
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM output_materializations WHERE materialization_id = ?",
                (materialization_id,),
            ).fetchone()
            if row is None:
                raise OutputMaterializationFailed("the output receipt disappeared")
            if row["status"] != "completed":
                if row["status"] != "published":
                    raise OutputIntegrityError("the output was not durably published")
                connection.execute(
                    "UPDATE output_materializations SET status = 'completed', completed_at = ? "
                    "WHERE materialization_id = ?",
                    (when, materialization_id),
                )
                self._audit(
                    connection,
                    account_id=str(row["account_id"]),
                    action="output.materialization.completed",
                    subject_id=materialization_id,
                    details={
                        "artifact_id": str(row["artifact_id"]),
                        "revision_id": str(row["revision_id"]),
                        "display_name": str(row["display_name"]),
                    },
                    created_at=when,
                )
            row = connection.execute(
                "SELECT * FROM output_materializations WHERE materialization_id = ?",
                (materialization_id,),
            ).fetchone()
        return self.repository.materialization_from_row(row).projection

    def _move_after_collision(
        self,
        stored: StoredMaterialization,
        *,
        observed_sha256: str,
    ) -> StoredMaterialization:
        when = _time(self.clock())
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM output_materializations WHERE materialization_id = ?",
                (stored.projection.materialization_id,),
            ).fetchone()
            current = self.repository.materialization_from_row(row)
            if current.projection.status is MaterializationStatus.COMPLETED:
                raise OutputIntegrityError("a completed output file was changed externally")
            if current.projection.display_name != stored.projection.display_name:
                return current
            connection.execute(
                "INSERT OR IGNORE INTO output_name_collisions(output_policy_snapshot_id, "
                "display_name_key, observed_sha256, detected_at) VALUES (?, ?, ?, ?)",
                (
                    current.projection.output_policy_snapshot_id,
                    current.display_name_key,
                    observed_sha256,
                    when,
                ),
            )
            candidates = self._candidate_names(
                current.projection.display_name,
                current.projection.revision_id,
                current.projection.sha256,
                include_base=False,
            )
            chosen = self._claim_candidate(
                connection,
                policy_id=current.projection.output_policy_snapshot_id,
                candidates=candidates,
                sha256=current.projection.sha256,
                when=when,
            )
            connection.execute(
                "UPDATE output_materializations SET display_name = ?, display_name_key = ? "
                "WHERE materialization_id = ? AND status != 'completed'",
                (
                    chosen,
                    filename_claim_key(chosen),
                    current.projection.materialization_id,
                ),
            )
            self._audit(
                connection,
                account_id=current.account_id,
                action="output.materialization.collision_avoided",
                subject_id=current.projection.materialization_id,
                details={"display_name": chosen},
                created_at=when,
            )
            row = connection.execute(
                "SELECT * FROM output_materializations WHERE materialization_id = ?",
                (current.projection.materialization_id,),
            ).fetchone()
        return self.repository.materialization_from_row(row)

    @staticmethod
    def _claim_candidate(
        connection: sqlite3.Connection,
        *,
        policy_id: str,
        candidates: tuple[str, ...],
        sha256: str,
        when: str,
    ) -> str:
        for candidate in candidates:
            key = filename_claim_key(candidate)
            collisions = connection.execute(
                "SELECT observed_sha256 FROM output_name_collisions "
                "WHERE output_policy_snapshot_id = ? AND display_name_key = ?",
                (policy_id, key),
            ).fetchall()
            if any(row["observed_sha256"] != sha256 for row in collisions):
                continue
            claim = connection.execute(
                "SELECT sha256, display_name FROM output_name_claims "
                "WHERE output_policy_snapshot_id = ? AND display_name_key = ?",
                (policy_id, key),
            ).fetchone()
            if claim is None:
                connection.execute(
                    "INSERT INTO output_name_claims(output_policy_snapshot_id, display_name_key, "
                    "display_name, sha256, claimed_at) VALUES (?, ?, ?, ?, ?)",
                    (policy_id, key, candidate, sha256, when),
                )
                return candidate
            if claim["sha256"] == sha256:
                return str(claim["display_name"])
        raise OutputMaterializationFailed("no safe collision-free output name is available")

    @staticmethod
    def _candidate_names(
        display_name: str,
        revision_id: str,
        sha256: str,
        *,
        include_base: bool = True,
    ) -> tuple[str, ...]:
        base = sanitize_display_filename(display_name, max_length=180)
        stem, suffix = split_display_filename(base)
        revision_token = hashlib.sha256(revision_id.encode("utf-8")).hexdigest()[:8]
        values: list[str] = [base] if include_base else []
        for digest_length in (12, 24, 64):
            marker = f"__r{revision_token}-{sha256[:digest_length]}"
            allowed = max(1, 180 - len(marker) - len(suffix))
            candidate = sanitize_display_filename(
                f"{stem[:allowed].rstrip(' .') or '未命名'}{marker}{suffix}",
                max_length=180,
            )
            if candidate not in values:
                values.append(candidate)
        return tuple(values)

    # ------------------------------------------------------------------
    # Artifact and audit helpers
    # ------------------------------------------------------------------
    def _eligible_projection(self, artifact_id: str, revision_id: str, *, account_id: str):
        try:
            projection = self.artifacts.repository.get_revision_projection(
                artifact_id,
                revision_id,
                include_internal=True,
                account_id=account_id,
            )
        except Exception as error:
            from ecorex.artifacts.errors import ArtifactError

            if isinstance(error, ArtifactError):
                raise OutputArtifactNotEligible("the selected artifact revision is unavailable") from error
            raise
        if (
            projection.visibility is ArtifactVisibility.INTERNAL
            or projection.family in _BLOCKED_FAMILIES
            or projection.role in {
                ArtifactRole.SOURCE,
                ArtifactRole.INTERMEDIATE,
                ArtifactRole.DIAGNOSTIC,
            }
        ):
            raise OutputArtifactNotEligible("implementation and internal files cannot be exported")
        if projection.status is not ArtifactStatus.READY:
            raise OutputArtifactNotEligible("only ready user artifacts can be exported")
        if not _SHA256.fullmatch(projection.sha256):
            raise OutputIntegrityError("the artifact digest is invalid")
        return projection

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        action: str,
        subject_id: str,
        details: Mapping[str, Any],
        created_at: str,
    ) -> None:
        # Details are intentionally limited to transport-safe identities and
        # display names.  Root paths never enter the audit projection.
        connection.execute(
            "INSERT INTO output_audit(audit_id, account_id, action, subject_id, "
            "details_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "outaud_" + uuid.uuid4().hex,
                account_id,
                action,
                subject_id,
                canonical_json(dict(details)),
                created_at,
            ),
        )


__all__ = ["OutputService"]
