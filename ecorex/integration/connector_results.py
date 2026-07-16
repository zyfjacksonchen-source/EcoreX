"""Atomic model-facing Connector result delivery.

Provider execution and local publication deliberately have separate durable
boundaries.  Inline results are committed as one exact replay envelope.  Large
results cross a digest-only staging boundary after CAS publication, then one
Runtime SQLite transaction commits Artifact metadata, the invocation result,
the connector outbox, and the user-thread Item/Event.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from ecorex.artifacts import (
    ArtifactFamily,
    ArtifactRole,
    ArtifactScope,
    ArtifactService,
    ArtifactStatus,
    ArtifactVisibility,
    ContentIntegrityError,
)
from ecorex.capabilities import ToolInvocationContext
from ecorex.capabilities.errors import (
    CapabilityDeniedError,
    CapabilityUnavailableError,
    ToolArgumentsValidationError,
)
from ecorex.connectors.models import ConnectorInvocationRecord
from ecorex.connectors.repository import (
    ConnectorOperationLease,
    ConnectorResultStage,
    SQLiteConnectorRepository,
)
from ecorex.protocol import ItemKind, ItemStatus, TERMINAL_TURN_STATUSES, TurnStatus
from ecorex.runtime.kernel import RuntimeKernel
from ecorex.runtime.public_tools import PublicToolActivityProjector


INLINE_DATA_LIMIT_BYTES = 512 * 1024
INLINE_ENVELOPE_LIMIT_BYTES = 768 * 1024
ARTIFACT_ENVELOPE_LIMIT_BYTES = 16 * 1024
ARTIFACT_READ_MAX_CHARS = 32_768
_UNAVAILABLE_ERROR_CODES = frozenset(
    {
        "connector_result_schema_invalid",
        "connector_result_secret_rejected",
        "connector_result_too_large",
        "connector_result_persistence_failed",
    }
)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class RuntimeConnectorResultCoordinator:
    """Linearization authority for model-originated Connector results."""

    def __init__(
        self,
        kernel: RuntimeKernel,
        artifacts: ArtifactService,
        repository: SQLiteConnectorRepository,
        *,
        account_id: str,
        fault_hook: Callable[[str, str], None] | None = None,
    ) -> None:
        if not str(account_id or "").strip():
            raise ValueError("connector result account identity is required")
        runtime_path = Path(kernel.database.path).resolve()
        artifact_path = Path(artifacts.repository.database_path).resolve()
        connector_path = Path(repository.database).resolve()
        if runtime_path != artifact_path or runtime_path != connector_path:
            raise ValueError(
                "Connector result coordination requires one authoritative Runtime database"
            )
        self.kernel = kernel
        self.artifacts = artifacts
        self.repository = repository
        self.account_id = account_id.strip()
        self.fault_hook = fault_hook

    def complete_result(
        self,
        record: ConnectorInvocationRecord,
        operation_lease: ConnectorOperationLease,
        *,
        result: Any,
        encoded_result: bytes,
        requested_name: str,
        created_by_tool_id: Literal["connector_read", "connector_write"],
        completion_path: Literal["provider_result", "late_provider_result"],
    ) -> Mapping[str, Any]:
        context = self._context(record)
        raw = bytes(encoded_result)
        if _canonical_json_bytes(result) != raw:
            raise ValueError("Connector result bytes do not match the canonical data")
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Connector result exceeds the product persistence limit")
        result_sha256 = hashlib.sha256(raw).hexdigest()
        if len(raw) <= INLINE_DATA_LIMIT_BYTES:
            envelope = self._base_envelope(
                record,
                delivery="inline",
                result_sha256=result_sha256,
                size_bytes=len(raw),
            )
            envelope["data"] = result
            if len(_canonical_json_bytes(envelope)) <= INLINE_ENVELOPE_LIMIT_BYTES:
                self.repository.stage_connector_result(
                    record,
                    operation_lease,
                    result_sha256=result_sha256,
                    size_bytes=len(raw),
                    delivery_hint="inline",
                    inline_data=result,
                    requested_name=requested_name,
                    owner_account_id=self.account_id,
                    created_by_tool_id=created_by_tool_id,
                    completion_path=completion_path,
                )
                self._fault("after_stage", record.invocation_id)
                return self.finalize_staged(
                    record.invocation_id,
                    recovery_delivery=completion_path == "late_provider_result",
                )

        declaration = self.artifacts.issue_trusted_deliverable_declaration(
            "connector.result",
            family=ArtifactFamily.DATA_EXPORT,
        )
        prepared = self.artifacts.prepare_artifact(
            raw,
            requested_name=requested_name,
            mime_type="application/json",
            role=ArtifactRole.DELIVERABLE,
            requested_visibility=ArtifactVisibility.SECONDARY,
            declaration=declaration,
            status=ArtifactStatus.READY,
            scope=ArtifactScope(
                account_id=self.account_id,
                thread_id=context.thread_id,
                turn_id=context.turn_id,
                created_by_tool_id=created_by_tool_id,
            ),
        )
        if prepared.sha256 != result_sha256 or prepared.size_bytes != len(raw):
            raise RuntimeError("Connector result CAS preparation changed its identity")
        stage = self.repository.stage_connector_result(
            record,
            operation_lease,
            result_sha256=result_sha256,
            size_bytes=len(raw),
            delivery_hint="artifact",
            requested_name=requested_name,
            owner_account_id=self.account_id,
            created_by_tool_id=created_by_tool_id,
            completion_path=completion_path,
        )
        self._fault("after_stage", record.invocation_id)
        return self.finalize_staged(
            record.invocation_id,
            prepared=prepared,
            recovery_delivery=completion_path == "late_provider_result",
        )

    def complete_unavailable(
        self,
        record: ConnectorInvocationRecord,
        operation_lease: ConnectorOperationLease,
        *,
        error_code: str,
        requested_name: str,
        created_by_tool_id: Literal["connector_read", "connector_write"],
        completion_path: Literal["provider_result", "late_provider_result"],
    ) -> Mapping[str, Any]:
        self._context(record)
        if error_code not in _UNAVAILABLE_ERROR_CODES:
            raise ValueError("Connector unavailable receipt identity is invalid")
        context = self._context(record)
        receipt_identity = _canonical_json_bytes(
            {
                "schema_version": 1,
                "identity_kind": "receipt",
                "connector_invocation_id": record.invocation_id,
                "discovery_id": context.discovery_id,
                "error_code": error_code,
            }
        )
        digest = hashlib.sha256(receipt_identity).hexdigest()
        envelope = self._base_envelope(
            record,
            delivery="result_unavailable",
            result_sha256=digest,
            size_bytes=0,
        )
        envelope["identity_kind"] = "receipt"
        envelope["error_code"] = str(error_code)
        if len(_canonical_json_bytes(envelope)) > ARTIFACT_ENVELOPE_LIMIT_BYTES:
            raise RuntimeError("Connector unavailable receipt exceeds its bounded contract")
        self.repository.stage_connector_result(
            record,
            operation_lease,
            result_sha256=digest,
            size_bytes=0,
            delivery_hint="unavailable",
            inline_data=envelope,
            requested_name=requested_name,
            owner_account_id=self.account_id,
            created_by_tool_id=created_by_tool_id,
            completion_path=completion_path,
        )
        self._fault("after_stage", record.invocation_id)
        return self.finalize_staged(
            record.invocation_id,
            recovery_delivery=completion_path == "late_provider_result",
        )

    def finalize_staged(
        self,
        invocation_id: str,
        *,
        prepared: Any | None = None,
        recovery_delivery: bool = False,
    ) -> Mapping[str, Any]:
        stage = self.repository.get_result_stage(invocation_id)
        if stage is None:
            raise KeyError(invocation_id)
        recovery_delivery = (
            recovery_delivery or stage.completion_path == "late_provider_result"
        )
        if stage.status == "finalized":
            if not isinstance(stage.result, dict):
                raise RuntimeError("Finalized Connector result envelope is unavailable")
            return stage.result

        if stage.delivery_hint == "unavailable":
            if not isinstance(stage.inline_data, dict):
                raise RuntimeError("Staged Connector unavailable receipt is invalid")
            envelope = dict(stage.inline_data)
            expected = self._base_envelope(
                stage.invocation,
                delivery="result_unavailable",
                result_sha256=stage.result_sha256,
                size_bytes=stage.size_bytes,
            )
            error_code = envelope.get("error_code")
            if (
                error_code not in _UNAVAILABLE_ERROR_CODES
                or {
                    **expected,
                    "identity_kind": "receipt",
                    "error_code": error_code,
                }
                != envelope
                or len(_canonical_json_bytes(envelope)) > ARTIFACT_ENVELOPE_LIMIT_BYTES
            ):
                raise RuntimeError("Staged Connector unavailable receipt changed")
            with self.kernel.database.transaction() as connection:
                current = self.repository.get_result_stage_in_transaction(
                    connection, invocation_id
                )
                if current is None:
                    raise RuntimeError("Connector result stage disappeared")
                if current.status == "finalized":
                    if not isinstance(current.result, dict):
                        raise RuntimeError(
                            "Finalized Connector result envelope is unavailable"
                        )
                    return current.result
                item_id, recovered_after_terminal, delivered_by_recovery = (
                    self._create_recovery_result_item_in_transaction(
                        connection,
                        current,
                        envelope,
                        recovery_delivery=recovery_delivery,
                    )
                )
                self._append_result_event_in_transaction(
                    connection,
                    current.invocation,
                    event_type="connector.result_unavailable",
                    delivery="result_unavailable",
                    error_code=str(error_code),
                    item_id=item_id,
                    artifact_id=None,
                    revision_id=None,
                    result_sha256=current.result_sha256,
                    size_bytes=current.size_bytes,
                    recovered_after_terminal=recovered_after_terminal,
                    recovery_delivery=delivered_by_recovery,
                )
                self._fault("before_finalize_commit", invocation_id)
                self.repository.complete_runtime_invocation_in_transaction(
                    connection,
                    current.invocation,
                    result=envelope,
                    completion_path=current.completion_path,
                    stage=current,
                )
            return envelope

        if stage.delivery_hint == "inline":
            raw = _canonical_json_bytes(stage.inline_data)
            if (
                len(raw) != stage.size_bytes
                or hashlib.sha256(raw).hexdigest() != stage.result_sha256
            ):
                raise RuntimeError("Staged inline Connector result identity changed")
            envelope = self._base_envelope(
                stage.invocation,
                delivery="inline",
                result_sha256=stage.result_sha256,
                size_bytes=stage.size_bytes,
            )
            envelope["data"] = stage.inline_data
            if len(_canonical_json_bytes(envelope)) > INLINE_ENVELOPE_LIMIT_BYTES:
                raise RuntimeError("Staged inline Connector envelope exceeds its bound")
            with self.kernel.database.transaction() as connection:
                current = self.repository.get_result_stage_in_transaction(
                    connection, invocation_id
                )
                if current is None:
                    raise RuntimeError("Connector result stage disappeared")
                if current.status == "finalized":
                    if not isinstance(current.result, dict):
                        raise RuntimeError(
                            "Finalized Connector result envelope is unavailable"
                        )
                    return current.result
                item_id, recovered_after_terminal, delivered_by_recovery = (
                    self._create_recovery_result_item_in_transaction(
                        connection,
                        current,
                        envelope,
                        recovery_delivery=recovery_delivery,
                    )
                )
                self._append_result_event_in_transaction(
                    connection,
                    current.invocation,
                    event_type="connector.result.completed",
                    delivery="inline",
                    error_code=None,
                    item_id=item_id,
                    artifact_id=None,
                    revision_id=None,
                    result_sha256=current.result_sha256,
                    size_bytes=current.size_bytes,
                    recovered_after_terminal=recovered_after_terminal,
                    recovery_delivery=delivered_by_recovery,
                )
                self._fault("before_finalize_commit", invocation_id)
                self.repository.complete_runtime_invocation_in_transaction(
                    connection,
                    current.invocation,
                    result=envelope,
                    completion_path=current.completion_path,
                    stage=current,
                )
            return envelope

        content = self.artifacts.blobs.read_bytes(stage.result_sha256)
        if len(content) != stage.size_bytes:
            raise RuntimeError("Staged Connector result CAS size changed")
        if prepared is None:
            declaration = self.artifacts.issue_trusted_deliverable_declaration(
                "connector.result",
                family=ArtifactFamily.DATA_EXPORT,
            )
            prepared = self.artifacts.prepare_artifact(
                content,
                requested_name=stage.requested_name,
                mime_type="application/json",
                role=ArtifactRole.DELIVERABLE,
                requested_visibility=ArtifactVisibility.SECONDARY,
                declaration=declaration,
                status=ArtifactStatus.READY,
                scope=ArtifactScope(
                    account_id=stage.owner_account_id,
                    thread_id=stage.thread_id,
                    turn_id=stage.turn_id,
                    created_by_tool_id=stage.created_by_tool_id,
                ),
            )
        if prepared.sha256 != stage.result_sha256 or prepared.size_bytes != stage.size_bytes:
            raise RuntimeError("Prepared Connector result no longer matches its stage")

        with self.kernel.database.transaction() as connection:
            current = self.repository.get_result_stage_in_transaction(
                connection, invocation_id
            )
            if current is None:
                raise RuntimeError("Connector result stage disappeared")
            if current.status == "finalized":
                if not isinstance(current.result, dict):
                    raise RuntimeError("Finalized Connector result envelope is unavailable")
                return current.result
            artifact = self.artifacts.create_artifact_in_transaction(
                connection, prepared
            )
            envelope = self._base_envelope(
                current.invocation,
                delivery="artifact",
                result_sha256=current.result_sha256,
                size_bytes=current.size_bytes,
            )
            envelope["artifact"] = {
                "artifact_id": artifact.artifact_id,
                "revision_id": artifact.revision_id,
                "family": artifact.family.value,
                "role": artifact.role.value,
                "visibility": artifact.visibility.value,
                "status": artifact.status.value,
                "display_name": artifact.display_name,
                "mime_type": artifact.mime_type,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
                "content_url": f"/api/v1/artifacts/{artifact.artifact_id}/content",
                "preview_url": f"/api/v1/artifacts/{artifact.artifact_id}/preview",
                "reader": {
                    "tool_id": "artifact_read",
                    "offset_chars": 0,
                    "max_chars": ARTIFACT_READ_MAX_CHARS,
                },
            }
            if len(_canonical_json_bytes(envelope)) > ARTIFACT_ENVELOPE_LIMIT_BYTES:
                raise RuntimeError("Connector Artifact envelope exceeds its bounded contract")

            turn = connection.execute(
                "SELECT thread_id, status FROM turns WHERE turn_id=?",
                (current.turn_id,),
            ).fetchone()
            if turn is None or str(turn["thread_id"]) != current.thread_id:
                raise RuntimeError("Connector result Runtime turn authority is unavailable")
            recovered_after_terminal = (
                TurnStatus(str(turn["status"])) in TERMINAL_TURN_STATUSES
            )
            item_id = "itm_" + hashlib.sha256(
                f"{invocation_id}\0connector-result-artifact".encode("utf-8")
            ).hexdigest()[:32]
            self.kernel._create_item_in_transaction(
                connection,
                item_id=item_id,
                thread_id=current.thread_id,
                turn_id=current.turn_id,
                kind=ItemKind.ARTIFACT,
                content={
                    "artifact": artifact.to_dict(),
                    "source": {
                        "kind": "connector_result",
                        "connector_invocation_id": invocation_id,
                        "discovery_id": current.discovery_id,
                        "recovered_after_terminal": recovered_after_terminal,
                    },
                    "change_summary": "连接器返回了较大的结构化结果，已保存为可读取的数据工件。",
                },
                status=ItemStatus.COMPLETED,
                idempotency_key=f"connector-result:{invocation_id}:artifact-item",
            )
            self._append_result_event_in_transaction(
                connection,
                current.invocation,
                event_type="artifact.connector_result.created",
                delivery="artifact",
                error_code=None,
                item_id=item_id,
                artifact_id=artifact.artifact_id,
                revision_id=artifact.revision_id,
                result_sha256=current.result_sha256,
                size_bytes=current.size_bytes,
                recovered_after_terminal=recovered_after_terminal,
            )
            self._fault("before_finalize_commit", invocation_id)
            self.repository.complete_runtime_invocation_in_transaction(
                connection,
                current.invocation,
                result=envelope,
                completion_path=current.completion_path,
                stage=current,
                artifact_id=artifact.artifact_id,
                revision_id=artifact.revision_id,
            )
        return envelope

    def recover_pending(self, *, limit: int = 1000) -> Mapping[str, int]:
        completed = 0
        deferred = 0
        for stage in self.repository.pending_result_stages(limit=limit):
            try:
                self.finalize_staged(
                    stage.invocation.invocation_id,
                    recovery_delivery=True,
                )
            except Exception as error:
                # The stage remains the durable provider-replay fence.  A
                # corrupt or temporarily unavailable CAS blob must not block
                # unrelated Runtime startup recovery.
                error_code = (
                    "artifact_cas_unavailable"
                    if isinstance(error, (ContentIntegrityError, FileNotFoundError, OSError))
                    else "local_finalize_deferred"
                )
                try:
                    self.repository.record_result_recovery_deferred(
                        invocation_id=stage.invocation.invocation_id,
                        stage_status=stage.status,
                        error_code=error_code,
                    )
                except Exception:
                    pass
                deferred += 1
            else:
                completed += 1
        return {"completed": completed, "deferred": deferred}

    def _create_recovery_result_item_in_transaction(
        self,
        connection: Any,
        stage: ConnectorResultStage,
        envelope: Mapping[str, Any],
        *,
        recovery_delivery: bool,
    ) -> tuple[str | None, bool, bool]:
        """Publish an otherwise unobserved result without rewriting Tool Items."""

        turn = connection.execute(
            "SELECT thread_id, status FROM turns WHERE turn_id=?",
            (stage.turn_id,),
        ).fetchone()
        if turn is None or str(turn["thread_id"]) != stage.thread_id:
            raise RuntimeError("Connector result Runtime turn authority is unavailable")
        recovered_after_terminal = (
            TurnStatus(str(turn["status"])) in TERMINAL_TURN_STATUSES
        )
        should_deliver = recovery_delivery or recovered_after_terminal
        if not should_deliver:
            return None, recovered_after_terminal, False
        context = self._context(stage.invocation)
        item_id = "itm_" + hashlib.sha256(
            (
                f"{stage.invocation.invocation_id}"
                "\0connector-result-recovery-item"
            ).encode("utf-8")
        ).hexdigest()[:32]
        if stage.created_by_tool_id not in {"connector_read", "connector_write"}:
            raise RuntimeError("Connector recovery Tool identity is invalid")
        public_activity = PublicToolActivityProjector.recovered_connector(
            tool_call_id=context.tool_call_id,
            tool_id=stage.created_by_tool_id,
            argument_sha256=stage.invocation.input_sha256,
            result_envelope=envelope,
        )
        self.kernel._create_item_in_transaction(
            connection,
            item_id=item_id,
            thread_id=stage.thread_id,
            turn_id=stage.turn_id,
            kind=ItemKind.TOOL_CALL,
            content=public_activity.model_dump(mode="json"),
            status=ItemStatus.COMPLETED,
            idempotency_key=(
                f"connector-result:{stage.invocation.invocation_id}:recovery-item"
            ),
        )
        return item_id, recovered_after_terminal, True

    def _append_result_event_in_transaction(
        self,
        connection: Any,
        record: ConnectorInvocationRecord,
        *,
        event_type: str,
        delivery: str,
        error_code: str | None,
        item_id: str | None,
        artifact_id: str | None,
        revision_id: str | None,
        result_sha256: str,
        size_bytes: int,
        recovered_after_terminal: bool = False,
        recovery_delivery: bool = False,
    ) -> None:
        context = self._context(record)
        payload: dict[str, Any] = {
            "connector_invocation_id": record.invocation_id,
            "discovery_id": context.discovery_id,
            "delivery": delivery,
            "result_sha256": result_sha256,
            "size_bytes": size_bytes,
            "recovered_after_terminal": recovered_after_terminal,
            "recovery_delivery": recovery_delivery,
        }
        if error_code is not None:
            payload["error_code"] = error_code
        if artifact_id is not None:
            payload["artifact_id"] = artifact_id
            payload["revision_id"] = revision_id
        self.kernel.events.append_in_transaction(
            connection,
            thread_id=context.thread_id,
            turn_id=context.turn_id,
            item_id=item_id,
            job_id=context.job_id,
            tool_call_id=context.tool_call_id,
            correlation_id=record.invocation_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=f"connector-result:{record.invocation_id}:{event_type}",
        )

    @staticmethod
    def _context(record: ConnectorInvocationRecord):
        if record.runtime_context is None:
            raise ValueError("Model-facing Connector result requires Runtime context")
        return record.runtime_context

    @staticmethod
    def _base_envelope(
        record: ConnectorInvocationRecord,
        *,
        delivery: str,
        result_sha256: str,
        size_bytes: int,
    ) -> dict[str, Any]:
        context = RuntimeConnectorResultCoordinator._context(record)
        return {
            "schema_version": 1,
            "status": "completed",
            "delivery": delivery,
            "connector_invocation_id": record.invocation_id,
            "discovery_id": context.discovery_id,
            "result_sha256": result_sha256,
            "size_bytes": size_bytes,
        }

    def _fault(self, point: str, invocation_id: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point, invocation_id)


class ArtifactReadRuntime:
    """Protected Core handler for bounded, authority-checked JSON text reads."""

    def __init__(self, artifacts: ArtifactService, *, account_id: str) -> None:
        self.artifacts = artifacts
        self.account_id = str(account_id or "").strip()
        if not self.account_id:
            raise ValueError("Artifact reader account identity is required")

    def read(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> Mapping[str, Any]:
        if context.tool_id != "artifact_read":
            raise CapabilityDeniedError("Artifact read endpoint is inconsistent")
        scope = context.execution_scope
        if scope is None:
            raise CapabilityDeniedError("Artifact read requires Runtime execution scope")
        artifact_id = arguments.get("artifact_id")
        revision_id = arguments.get("revision_id")
        offset = arguments.get("offset_chars", 0)
        maximum = arguments.get("max_chars", ARTIFACT_READ_MAX_CHARS)
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or not isinstance(revision_id, str)
            or not revision_id
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 1 <= maximum <= ARTIFACT_READ_MAX_CHARS
        ):
            raise ToolArgumentsValidationError("Artifact read request is invalid")
        try:
            projection = self.artifacts.repository.get_revision_projection(
                artifact_id,
                revision_id,
                account_id=self.account_id,
            )
            artifact_scope = self.artifacts.get_artifact_scope(artifact_id)
        except Exception:
            raise CapabilityDeniedError("Artifact is unavailable in this execution scope") from None
        if (
            artifact_scope.account_id != self.account_id
            or artifact_scope.thread_id != scope.thread_id
            or artifact_scope.created_by_tool_id
            not in {"connector_read", "connector_write"}
            or projection.family is not ArtifactFamily.DATA_EXPORT
            or projection.role is not ArtifactRole.DELIVERABLE
            or projection.visibility
            not in {ArtifactVisibility.PRIMARY, ArtifactVisibility.SECONDARY}
            or projection.mime_type != "application/json"
            or projection.status is not ArtifactStatus.READY
            or projection.revision_id != revision_id
        ):
            raise CapabilityDeniedError("Artifact is unavailable in this execution scope")
        content = self.artifacts.read_user_content(
            artifact_id,
            revision_id,
            account_id=self.account_id,
        )
        if (
            len(content) != projection.size_bytes
            or hashlib.sha256(content).hexdigest() != projection.sha256
        ):
            raise CapabilityUnavailableError("Artifact content integrity check failed")
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CapabilityUnavailableError("Artifact JSON text is not valid UTF-8") from None
        if offset > len(text):
            raise ToolArgumentsValidationError("Artifact read offset exceeds content")
        next_offset = min(len(text), offset + maximum)
        return {
            "schema_version": 1,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
            "sha256": projection.sha256,
            "size_bytes": projection.size_bytes,
            "offset_chars": offset,
            "next_offset_chars": next_offset,
            "eof": next_offset == len(text),
            "content": text[offset:next_offset],
        }


__all__ = [
    "ARTIFACT_ENVELOPE_LIMIT_BYTES",
    "ARTIFACT_READ_MAX_CHARS",
    "ArtifactReadRuntime",
    "INLINE_DATA_LIMIT_BYTES",
    "INLINE_ENVELOPE_LIMIT_BYTES",
    "RuntimeConnectorResultCoordinator",
]
