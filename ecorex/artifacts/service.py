"""Application service for office artifacts, feedback, and precise retouch."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from .classification import ArtifactClassifier, ClassificationDecision
from .errors import ArtifactActionUnavailable, RetouchConflict
from .identity import sanitize_display_filename, split_display_filename, utc_now
from .models import (
    ArtifactFamily,
    ArtifactAction,
    ArtifactLineage,
    ArtifactProjection,
    ArtifactRole,
    ArtifactScope,
    ArtifactStatus,
    ArtifactVisibility,
    FeedbackRecord,
    FeedbackRequest,
    InspectionRegion,
    QualityEvidence,
    RenditionKind,
    RetouchExecutionBinding,
    RetouchAnnotation,
    RetouchJob,
    RetouchJobProjection,
    RetouchJobStatus,
    RetouchRequest,
    RetouchResult,
    RetouchStagedResult,
    coerce_inspection_regions,
    coerce_quality_evidence,
)
from .repository import ArtifactRepository
from .storage import ContentAddressedStore, StoredBlob
from .retouch_surface import compile_annotation_mask, inspect_raster
from .retouch_workspace import (
    MAX_RETOUCH_REFERENCES,
    RetouchEditSurface,
    RetouchReference,
    RetouchWorkspaceProjection,
)


@dataclass(frozen=True, slots=True)
class TrustedDeliverableDeclaration:
    """Opaque server-issued proof that a trusted tool declared a deliverable."""

    tool_id: str
    family: ArtifactFamily | None
    _authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        tool_id = str(self.tool_id or "").strip()
        if not tool_id:
            raise ValueError("trusted deliverable declaration requires a tool_id")
        if len(tool_id) > 256:
            raise ValueError("trusted deliverable declaration tool_id is too long")
        object.__setattr__(self, "tool_id", tool_id)
        if self.family is not None:
            object.__setattr__(self, "family", ArtifactFamily(self.family))


@dataclass(frozen=True, slots=True)
class PreparedArtifact:
    """Classified, CAS-backed Artifact awaiting one metadata transaction.

    The preparation deliberately carries only an immutable digest and byte
    count, never the CAS path or original content.  Its private authority binds
    it to the :class:`ArtifactService` instance that performed classification
    and storage, so a transport payload cannot forge a user-visible Artifact.
    """

    requested_name: str
    mime_type: str
    sha256: str
    size_bytes: int
    decision: ClassificationDecision
    prepared_at: datetime
    quality_evidence: QualityEvidence
    lineage: ArtifactLineage
    status: ArtifactStatus
    scope: ArtifactScope
    _authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.requested_name:
            raise ValueError("prepared Artifact name is required")
        if not self.mime_type:
            raise ValueError("prepared Artifact MIME type is required")
        digest = str(self.sha256 or "").casefold()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("prepared Artifact SHA-256 is invalid")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("prepared Artifact size is invalid")
        if not isinstance(self.prepared_at, datetime):
            raise ValueError("prepared Artifact timestamp is invalid")
        if not isinstance(self.decision, ClassificationDecision):
            raise ValueError("prepared Artifact classification is invalid")
        if not isinstance(self.quality_evidence, QualityEvidence):
            raise ValueError("prepared Artifact quality evidence is invalid")
        if not isinstance(self.lineage, ArtifactLineage):
            raise ValueError("prepared Artifact lineage is invalid")
        if not isinstance(self.scope, ArtifactScope):
            raise ValueError("prepared Artifact scope is invalid")
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "status", ArtifactStatus(self.status))


def _validate_raster_signature(content: bytes, mime_type: str) -> None:
    signatures = {
        "image/png": lambda value: value.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": lambda value: value.startswith(b"\xff\xd8\xff"),
        "image/gif": lambda value: value.startswith((b"GIF87a", b"GIF89a")),
        "image/tiff": lambda value: value.startswith((b"II*\x00", b"MM\x00*")),
        "image/webp": lambda value: len(value) >= 12 and value[:4] == b"RIFF" and value[8:12] == b"WEBP",
        "image/avif": lambda value: len(value) >= 16 and value[4:8] == b"ftyp" and b"avif" in value[8:32],
    }
    validator = signatures.get(mime_type)
    if validator is None or not validator(content):
        raise ValueError("retouch output does not match the declared raster image content type")


class ArtifactService:
    """High-level backend boundary used by the runtime/API adapter.

    The service deliberately has no ``show implementation files`` option.
    Product callers can only use :meth:`list_user_artifacts` and
    :meth:`get_user_artifact`; worker/audit access is named explicitly.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        database_path: str | Path | None = None,
        classifier: ArtifactClassifier | None = None,
        clock: Callable[[], datetime] = utc_now,
        create_storage: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        if create_storage:
            self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = ContentAddressedStore(
            self.root / "blobs",
            create=create_storage,
        )
        self.repository = ArtifactRepository(database_path or self.root / "artifacts.sqlite3")
        self.classifier = classifier or ArtifactClassifier()
        self.clock = clock
        self._declaration_authority = object()
        self._preparation_authority = object()

    def issue_trusted_deliverable_declaration(
        self,
        tool_id: str,
        *,
        family: ArtifactFamily | None = None,
    ) -> TrustedDeliverableDeclaration:
        """Issue an in-process capability; transport payloads cannot forge it."""

        return TrustedDeliverableDeclaration(
            tool_id=tool_id,
            family=family,
            _authority=self._declaration_authority,
        )

    def _resolve_declaration(
        self,
        declaration: TrustedDeliverableDeclaration | None,
        family_hint: ArtifactFamily | None,
        role: ArtifactRole,
    ) -> tuple[bool, ArtifactFamily | None]:
        if declaration is not None:
            if (
                not isinstance(declaration, TrustedDeliverableDeclaration)
                or declaration._authority is not self._declaration_authority
            ):
                raise ValueError("trusted deliverable declaration was not issued by this service")
            if ArtifactRole(role) is not ArtifactRole.DELIVERABLE:
                raise ValueError("trusted deliverable declarations apply only to deliverables")
            return True, declaration.family
        hint = ArtifactFamily(family_hint) if family_hint is not None else None
        # An untrusted family hint may only make an artifact more restrictive.
        if ArtifactRole(role) is not ArtifactRole.DELIVERABLE:
            return False, hint
        if hint not in {
            ArtifactFamily.SOURCE_CODE,
            ArtifactFamily.SCRIPT,
            ArtifactFamily.DIFF,
            ArtifactFamily.LOG,
            ArtifactFamily.TEMPORARY,
            ArtifactFamily.DIRECTORY,
        }:
            hint = None
        return False, hint

    def classify(
        self,
        requested_name: str,
        mime_type: str,
        *,
        role: ArtifactRole = ArtifactRole.DELIVERABLE,
        requested_visibility: ArtifactVisibility = ArtifactVisibility.PRIMARY,
        declaration: TrustedDeliverableDeclaration | None = None,
        family_hint: ArtifactFamily | None = None,
    ) -> ClassificationDecision:
        explicit_deliverable, trusted_family_hint = self._resolve_declaration(
            declaration, family_hint, role
        )
        decision = self.classifier.classify(
            requested_name,
            mime_type,
            role=role,
            requested_visibility=requested_visibility,
            explicit_deliverable=explicit_deliverable,
            family_hint=trusted_family_hint,
        )
        if (
            declaration is not None
            and declaration.family is not None
            and decision.is_user_visible
            and decision.family is not declaration.family
        ):
            raise ValueError("trusted deliverable declaration family does not match classified format")
        if declaration is not None:
            decision = ClassificationDecision(
                family=decision.family,
                role=decision.role,
                visibility=decision.visibility,
                actions=decision.actions,
                reasons=tuple(
                    dict.fromkeys((*decision.reasons, f"trusted_tool:{declaration.tool_id}"))
                ),
            )
        return decision

    def create_artifact(
        self,
        content: bytes | bytearray | memoryview,
        *,
        requested_name: str,
        mime_type: str = "application/octet-stream",
        role: ArtifactRole = ArtifactRole.DELIVERABLE,
        requested_visibility: ArtifactVisibility = ArtifactVisibility.PRIMARY,
        declaration: TrustedDeliverableDeclaration | None = None,
        family_hint: ArtifactFamily | None = None,
        quality_evidence: QualityEvidence | Mapping[str, object] | None = None,
        lineage: ArtifactLineage | None = None,
        status: ArtifactStatus = ArtifactStatus.READY,
        scope: ArtifactScope | None = None,
    ) -> ArtifactProjection:
        """Store content and return the worker projection.

        Returning an internal projection here lets the creating worker retain
        its opaque identity.  User delivery must go through the user methods,
        which enforce persisted visibility.
        """

        prepared = self.prepare_artifact(
            content,
            requested_name=requested_name,
            mime_type=mime_type,
            role=role,
            requested_visibility=requested_visibility,
            declaration=declaration,
            family_hint=family_hint,
            quality_evidence=quality_evidence,
            lineage=lineage,
            status=status,
            scope=scope,
        )
        self._require_prepared_artifact(prepared, verify_content=False)
        return self.repository.create_artifact(prepared)

    def prepare_artifact(
        self,
        content: bytes | bytearray | memoryview,
        *,
        requested_name: str,
        mime_type: str = "application/octet-stream",
        role: ArtifactRole = ArtifactRole.DELIVERABLE,
        requested_visibility: ArtifactVisibility = ArtifactVisibility.PRIMARY,
        declaration: TrustedDeliverableDeclaration | None = None,
        family_hint: ArtifactFamily | None = None,
        quality_evidence: QualityEvidence | Mapping[str, object] | None = None,
        lineage: ArtifactLineage | None = None,
        status: ArtifactStatus = ArtifactStatus.READY,
        scope: ArtifactScope | None = None,
    ) -> PreparedArtifact:
        """Classify and publish bytes to CAS without creating user metadata.

        A prepared value is safe to retain across a short cross-domain
        transaction boundary.  Rolling that later transaction back leaves only
        an unreachable content-addressed blob, never a visible Artifact row or
        filename claim.
        """

        decision = self.classify(
            requested_name,
            mime_type,
            role=role,
            requested_visibility=requested_visibility,
            declaration=declaration,
            family_hint=family_hint,
        )
        blob = self.blobs.put_bytes(content)
        normalized_mime = (
            str(mime_type or "application/octet-stream")
            .split(";", 1)[0]
            .strip()
            .casefold()
            or "application/octet-stream"
        )
        return PreparedArtifact(
            requested_name=sanitize_display_filename(requested_name),
            mime_type=normalized_mime,
            sha256=blob.sha256,
            size_bytes=blob.size_bytes,
            decision=decision,
            prepared_at=self.clock(),
            quality_evidence=coerce_quality_evidence(quality_evidence),
            lineage=lineage or ArtifactLineage(),
            status=ArtifactStatus(status),
            scope=scope or ArtifactScope(),
            _authority=self._preparation_authority,
        )

    def create_artifact_in_transaction(
        self,
        connection: sqlite3.Connection,
        prepared: PreparedArtifact,
    ) -> ArtifactProjection:
        """Commit prepared metadata in the caller's exact Runtime transaction.

        This method does not commit or roll back.  Before metadata enters the
        transaction it verifies both the in-process preparation authority and
        the CAS bytes, closing the delayed-commit integrity gap.
        """

        self._require_prepared_artifact(prepared, verify_content=True)
        return self.repository.create_artifact_in_transaction(connection, prepared)

    def _require_prepared_artifact(
        self,
        prepared: PreparedArtifact,
        *,
        verify_content: bool,
    ) -> None:
        if (
            not isinstance(prepared, PreparedArtifact)
            or prepared._authority is not self._preparation_authority
        ):
            raise ValueError(
                "prepared Artifact was not issued by this Artifact service"
            )
        if not verify_content:
            return
        content = self.blobs.read_bytes(prepared.sha256)
        if len(content) != prepared.size_bytes:
            raise ValueError("prepared Artifact CAS size changed before commit")

    def create_cloud_link(
        self,
        url: str,
        *,
        requested_name: str,
        quality_evidence: QualityEvidence | Mapping[str, object] | None = None,
        scope: ArtifactScope | None = None,
    ) -> ArtifactProjection:
        normalized_url = str(url or "").strip()
        if not normalized_url.startswith(("https://", "http://")):
            raise ValueError("cloud link must use http:// or https://")
        payload = json.dumps(
            {"schema_version": 1, "url": normalized_url},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        stem, _ = split_display_filename(requested_name)
        declaration = self.issue_trusted_deliverable_declaration(
            "ecorex.cloud-link", family=ArtifactFamily.CLOUD_LINK
        )
        return self.create_artifact(
            payload,
            requested_name=f"{stem}.link",
            mime_type="application/vnd.ecorex.cloud-link+json",
            declaration=declaration,
            quality_evidence=quality_evidence,
            scope=scope,
        )

    def list_user_artifacts(
        self,
        *,
        account_id: str = "local-user",
        thread_id: str | None = None,
    ) -> tuple[ArtifactProjection, ...]:
        return self.repository.list_user_projections(
            account_id=account_id,
            thread_id=thread_id,
        )

    def get_user_artifact(
        self,
        artifact_id: str,
        *,
        account_id: str = "local-user",
    ) -> ArtifactProjection:
        return self.repository.get_user_projection(
            artifact_id,
            account_id=account_id,
        )

    def get_artifact_scope(self, artifact_id: str) -> ArtifactScope:
        return self.repository.get_scope(artifact_id)

    def get_internal_artifact(self, artifact_id: str) -> ArtifactProjection:
        """Worker/admin-only method; never bind it to the product list API."""

        return self.repository.get_internal_projection(artifact_id)

    def list_internal_artifacts(self) -> tuple[ArtifactProjection, ...]:
        """Worker/admin-only audit method."""

        return self.repository.list_internal_projections()

    def read_user_content(
        self,
        artifact_id: str,
        revision_id: str | None = None,
        *,
        account_id: str = "local-user",
    ) -> bytes:
        projection = (
            self.repository.get_revision_projection(
                artifact_id,
                revision_id,
                account_id=account_id,
            )
            if revision_id
            else self.repository.get_user_projection(
                artifact_id,
                account_id=account_id,
            )
        )
        return self.blobs.read_bytes(projection.sha256)

    def read_internal_revision_content(self, revision_id: str) -> bytes:
        """Worker/admin-only CAS read by opaque revision identity."""

        return self.blobs.read_bytes(self.repository.revision_digest(revision_id))

    def open_retouch_workspace(
        self,
        artifact_id: str,
        base_revision_id: str,
        *,
        account_id: str = "local-user",
    ) -> RetouchWorkspaceProjection:
        projection = self.repository.get_revision_projection(
            artifact_id,
            base_revision_id,
            account_id=account_id,
        )
        if projection.family is not ArtifactFamily.IMAGE:
            raise ArtifactActionUnavailable("precise retouch requires an image artifact")
        if projection.status is not ArtifactStatus.READY:
            raise ArtifactActionUnavailable("precise retouch requires a ready image revision")
        if ArtifactAction.PRECISE_RETOUCH not in projection.actions:
            raise ArtifactActionUnavailable("precise retouch is unavailable for this image")
        surface = self.describe_retouch_surface(
            artifact_id,
            projection.revision_id,
            account_id=account_id,
        )
        return self.repository.create_or_get_retouch_workspace(
            artifact_id=artifact_id,
            base_revision_id=base_revision_id,
            account_id=account_id,
            edit_surface=surface,
            now=self.clock(),
        )

    def describe_retouch_surface(
        self,
        artifact_id: str,
        revision_id: str,
        *,
        account_id: str = "local-user",
    ) -> RetouchEditSurface:
        projection = self.repository.get_revision_projection(
            artifact_id,
            revision_id,
            account_id=account_id,
        )
        content = self.blobs.read_bytes(projection.sha256)
        descriptor = inspect_raster(content, projection.mime_type)
        return RetouchEditSurface(
            base_revision_id=projection.revision_id,
            raster_digest=projection.sha256,
            width_px=descriptor.width_px,
            height_px=descriptor.height_px,
            orientation=descriptor.orientation,
            color_space=descriptor.color_space,
            mime_type=descriptor.mime_type,
        )

    def get_retouch_workspace(
        self, workspace_id: str, *, account_id: str = "local-user"
    ) -> RetouchWorkspaceProjection:
        return self.repository.get_retouch_workspace(workspace_id, account_id=account_id)

    def recover_interrupted_retouch_workspace_submissions(
        self,
        *,
        account_id: str | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> int:
        """Run explicit startup recovery outside all product read paths."""

        return self.repository.recover_interrupted_retouch_workspace_submissions(
            account_id=account_id,
            before_commit=before_commit,
        )

    def update_retouch_workspace(
        self,
        workspace_id: str,
        *,
        expected_version: int,
        annotations: Sequence[RetouchAnnotation],
        reference_artifact_ids: Sequence[str],
        global_instruction: str,
        view_state: Mapping[str, Any],
        client_request_id: str,
        account_id: str = "local-user",
    ) -> RetouchWorkspaceProjection:
        workspace = self.get_retouch_workspace(workspace_id, account_id=account_id)
        annotation_values = tuple(annotations)
        annotation_ids = [item.annotation_id for item in annotation_values]
        if any(not item for item in annotation_ids):
            raise ValueError("retouch workspace annotations require stable annotation_id values")
        if len(set(annotation_ids)) != len(annotation_ids):
            raise ValueError("retouch workspace annotation_id values must be unique")
        reference_ids = tuple(dict.fromkeys(str(item).strip() for item in reference_artifact_ids))
        if any(not item for item in reference_ids):
            raise ValueError("reference_artifact_ids must not contain empty values")
        if len(reference_ids) > MAX_RETOUCH_REFERENCES:
            raise ValueError(
                f"reference_artifact_ids must contain at most {MAX_RETOUCH_REFERENCES} items"
            )
        references: list[RetouchReference] = []
        for reference_id in reference_ids:
            if reference_id == workspace.artifact_id:
                raise ValueError("the retouch target cannot also be a reference image")
            reference = self.get_user_artifact(reference_id, account_id=account_id)
            if reference.family is not ArtifactFamily.IMAGE or reference.status is not ArtifactStatus.READY:
                raise ArtifactActionUnavailable("retouch references must be ready image artifacts")
            references.append(
                RetouchReference(
                    artifact_id=reference.artifact_id,
                    revision_id=reference.revision_id,
                    display_name=reference.display_name,
                    mime_type=reference.mime_type,
                    sha256=reference.sha256,
                )
            )
        instruction = str(global_instruction or "").strip()
        if len(instruction) > 8000:
            raise ValueError("global_instruction is too long")
        normalized_view = dict(view_state)
        allowed_view_keys = {
            "zoom",
            "pan_x",
            "pan_y",
            "selected_annotation_id",
            "tool",
        }
        if not set(normalized_view).issubset(allowed_view_keys):
            raise ValueError("retouch view_state contains unsupported fields")
        for key in ("zoom", "pan_x", "pan_y"):
            if key not in normalized_view:
                continue
            value = normalized_view[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"retouch view_state {key} must be a number")
            minimum, maximum = (1.0, 8.0) if key == "zoom" else (0.0, 1.0)
            if not minimum <= float(value) <= maximum:
                raise ValueError(f"retouch view_state {key} is out of bounds")
            normalized_view[key] = float(value)
        selected_annotation_id = normalized_view.get("selected_annotation_id")
        if selected_annotation_id is not None:
            selected_annotation_id = str(selected_annotation_id).strip()
            if not selected_annotation_id or len(selected_annotation_id) > 128:
                raise ValueError("retouch view_state selected_annotation_id is invalid")
            if selected_annotation_id not in set(annotation_ids):
                raise ValueError(
                    "retouch view_state selected_annotation_id does not exist"
                )
            normalized_view["selected_annotation_id"] = selected_annotation_id
        tool = normalized_view.get("tool")
        if tool is not None and tool not in {
            "select",
            "rectangle",
            "ellipse",
            "point",
            "polygon",
            "polyline",
            "brush",
            "pan",
        }:
            raise ValueError("retouch view_state tool is invalid")
        encoded_view = json.dumps(
            normalized_view,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded_view.encode("utf-8")) > 32_768:
            raise ValueError("retouch view_state is too large")

        mask_metadata: Mapping[str, Any] | None = None
        if annotation_values:
            compiled = compile_annotation_mask(
                workspace.edit_surface.width_px,
                workspace.edit_surface.height_px,
                [item.to_dict() for item in annotation_values],
            )
            mask_blob = self.blobs.put_bytes(compiled.png_bytes)
            if mask_blob.sha256 != compiled.sha256:
                raise RetouchConflict("compiled retouch mask digest changed while storing")
            mask_metadata = compiled.to_metadata()
        payload = {
            "annotations": [item.to_dict() for item in annotation_values],
            "references": [item.to_dict() for item in references],
            "global_instruction": instruction,
            "view_state": normalized_view,
            "mask": dict(mask_metadata) if mask_metadata is not None else None,
        }
        request_digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return self.repository.update_retouch_workspace(
            workspace_id,
            account_id=account_id,
            expected_version=expected_version,
            annotations=annotation_values,
            references=references,
            global_instruction=instruction,
            view_state=normalized_view,
            mask_metadata=mask_metadata,
            client_request_id=client_request_id,
            request_digest=request_digest,
            now=self.clock(),
        )

    def claim_retouch_workspace_submission(
        self,
        workspace_id: str,
        *,
        expected_version: int,
        client_request_id: str,
        account_id: str = "local-user",
    ) -> RetouchWorkspaceProjection:
        workspace = self.repository.claim_retouch_workspace_submission(
            workspace_id,
            account_id=account_id,
            expected_version=expected_version,
            client_request_id=client_request_id,
            now=self.clock(),
        )
        if not workspace.annotations and not workspace.global_instruction:
            self.repository.release_retouch_workspace_submission(
                workspace_id,
                account_id=account_id,
                client_request_id=client_request_id,
                now=self.clock(),
            )
            raise ValueError("retouch requires at least one annotation or a global instruction")
        return workspace

    def complete_retouch_workspace_submission(
        self,
        workspace_id: str,
        *,
        client_request_id: str,
        job_id: str,
        account_id: str = "local-user",
    ) -> RetouchWorkspaceProjection:
        return self.repository.complete_retouch_workspace_submission(
            workspace_id,
            account_id=account_id,
            client_request_id=client_request_id,
            job_id=job_id,
            now=self.clock(),
        )

    def complete_retouch_workspace_submission_in_transaction(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        *,
        client_request_id: str,
        job_id: str,
        account_id: str = "local-user",
    ) -> RetouchWorkspaceProjection:
        return self.repository.complete_retouch_workspace_submission_in_transaction(
            connection,
            workspace_id,
            account_id=account_id,
            client_request_id=client_request_id,
            job_id=job_id,
            now=self.clock(),
        )

    def release_retouch_workspace_submission(
        self,
        workspace_id: str,
        *,
        client_request_id: str,
        account_id: str = "local-user",
    ) -> RetouchWorkspaceProjection:
        return self.repository.release_retouch_workspace_submission(
            workspace_id,
            account_id=account_id,
            client_request_id=client_request_id,
            now=self.clock(),
        )

    def reopen_failed_retouch_workspace(
        self,
        workspace_id: str,
        *,
        expected_version: int,
        account_id: str = "local-user",
    ) -> RetouchWorkspaceProjection:
        return self.repository.reopen_failed_retouch_workspace(
            workspace_id,
            account_id=account_id,
            expected_version=expected_version,
            now=self.clock(),
        )

    def attach_rendition(
        self,
        parent_artifact_id: str,
        *,
        content: bytes | bytearray | memoryview,
        requested_name: str,
        mime_type: str,
        kind: RenditionKind,
        parent_revision_id: str | None = None,
        family_hint: ArtifactFamily | None = None,
    ) -> ArtifactProjection:
        parent_scope = self.get_artifact_scope(parent_artifact_id)
        parent = self.get_user_artifact(
            parent_artifact_id,
            account_id=parent_scope.account_id,
        )
        if parent_revision_id is not None and parent.revision_id != parent_revision_id:
            # A rendition is a view of one immutable revision; silently
            # attaching it to a newer revision would corrupt lineage.
            from .errors import RetouchConflict

            raise RetouchConflict("parent_revision_id is stale")
        decision = self.classify(
            requested_name,
            mime_type,
            role=ArtifactRole.RENDITION,
            requested_visibility=ArtifactVisibility.INTERNAL,
            family_hint=family_hint,
        )
        blob = self.blobs.put_bytes(content)
        return self.repository.create_and_attach_rendition(
            parent_artifact_id=parent_artifact_id,
            expected_parent_revision_id=parent.revision_id,
            blob=blob,
            requested_name=sanitize_display_filename(requested_name),
            mime_type=str(mime_type or "application/octet-stream").split(";", 1)[0].strip().casefold(),
            decision=decision,
            kind=kind,
            now=self.clock(),
        )

    def record_feedback(
        self,
        artifact_id: str,
        request: FeedbackRequest,
        *,
        account_id: str = "local-user",
        on_recorded: Callable[
            [sqlite3.Connection, FeedbackRecord, ArtifactScope], None
        ]
        | None = None,
    ) -> FeedbackRecord:
        self.get_user_artifact(artifact_id, account_id=account_id)
        return self.repository.record_feedback(
            artifact_id,
            request,
            now=self.clock(),
            on_recorded=on_recorded,
        )

    def request_retouch(
        self,
        artifact_id: str,
        request: RetouchRequest,
        *,
        account_id: str = "local-user",
        execution_scope: ArtifactScope | None = None,
        on_created: Callable[
            [sqlite3.Connection, RetouchJob, ArtifactScope], RetouchExecutionBinding
        ]
        | None = None,
        on_persisted: Callable[
            [sqlite3.Connection, RetouchJob, ArtifactScope], None
        ]
        | None = None,
    ) -> RetouchJobProjection:
        """Persist a structured retouch job and its internal annotation layer."""

        self.get_user_artifact(artifact_id, account_id=account_id)

        if request.edit_surface is not None:
            base = self.repository.get_revision_projection(
                artifact_id,
                request.base_revision_id,
                account_id=account_id,
            )
            if base.sha256 != request.edit_surface["raster_digest"]:
                raise RetouchConflict(
                    "retouch edit_surface digest does not match the base revision"
                )
            descriptor = inspect_raster(self.blobs.read_bytes(base.sha256), base.mime_type)
            expected_surface = {
                "width_px": descriptor.width_px,
                "height_px": descriptor.height_px,
                "orientation": descriptor.orientation,
                "color_space": descriptor.color_space,
                "mime_type": descriptor.mime_type,
            }
            if any(
                request.edit_surface[key] != value
                for key, value in expected_surface.items()
            ):
                raise RetouchConflict(
                    "retouch edit_surface raster metadata does not match the base revision"
                )
        if request.mask is not None:
            digest = str(request.mask["sha256"])
            if not self.blobs.exists(digest):
                raise RetouchConflict("retouch mask is missing from content-addressed storage")
            mask_bytes = self.blobs.read_bytes(digest)
            if len(mask_bytes) != request.mask["size_bytes"]:
                raise RetouchConflict("retouch mask size does not match its CAS metadata")
            descriptor = inspect_raster(mask_bytes, "image/png")
            if (
                descriptor.width_px != request.mask["width_px"]
                or descriptor.height_px != request.mask["height_px"]
            ):
                raise RetouchConflict("retouch mask dimensions do not match its CAS metadata")

        annotation_payload = json.dumps(
            {
                "schema_version": 1,
                "artifact_id": artifact_id,
                "base_revision_id": request.base_revision_id,
                "annotations": [annotation.to_dict() for annotation in request.annotations],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        annotation_blob = self.blobs.put_bytes(annotation_payload)
        job = self.repository.create_retouch_job(
            artifact_id=artifact_id,
            request=request,
            annotation_blob=annotation_blob,
            annotation_requested_name="retouch-annotation-layer.json",
            annotation_mime_type="application/vnd.ecorex.retouch-annotations+json",
            now=self.clock(),
            execution_scope=execution_scope,
            on_created=on_created,
            on_persisted=on_persisted,
        )
        return job.public_projection()

    def get_retouch_job(
        self,
        job_id: str,
        *,
        account_id: str = "local-user",
    ) -> RetouchJobProjection:
        job = self.repository.get_retouch_job(job_id)
        self.get_user_artifact(job.artifact_id, account_id=account_id)
        return job.public_projection()

    def get_internal_retouch_job(self, job_id: str) -> RetouchJob:
        """Worker/admin-only record containing the internal annotation identity."""

        return self.repository.get_retouch_job(job_id)

    def mark_retouch_running(
        self,
        job_id: str,
        *,
        on_running: Callable[[sqlite3.Connection, RetouchJob], None] | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> RetouchJobProjection:
        return self.repository.mark_retouch_running(
            job_id,
            now=self.clock(),
            on_running=on_running,
            before_commit=before_commit,
        ).public_projection()

    def complete_retouch(
        self,
        job_id: str,
        content: bytes | bytearray | memoryview,
        *,
        mime_type: str,
        requested_name: str | None = None,
        change_summary: str,
        inspection_regions: Sequence[InspectionRegion | Mapping[str, object]] | None = None,
        quality_evidence: QualityEvidence | Mapping[str, object] | None = None,
        on_completed: Callable[
            [sqlite3.Connection, RetouchJob, ArtifactProjection], None
        ]
        | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> RetouchResult:
        self.stage_retouch_result(
            job_id,
            content,
            mime_type=mime_type,
            requested_name=requested_name,
            change_summary=change_summary,
            inspection_regions=inspection_regions,
            quality_evidence=quality_evidence,
            before_commit=before_commit,
        )
        return self.complete_staged_retouch(
            job_id,
            on_completed=on_completed,
            before_commit=before_commit,
        )

    def stage_retouch_result(
        self,
        job_id: str,
        content: bytes | bytearray | memoryview,
        *,
        mime_type: str,
        requested_name: str | None = None,
        change_summary: str,
        inspection_regions: Sequence[InspectionRegion | Mapping[str, object]] | None = None,
        quality_evidence: QualityEvidence | Mapping[str, object] | None = None,
        adapter_result_id: str | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> RetouchStagedResult:
        normalized_mime = str(mime_type or "").split(";", 1)[0].strip().casefold()
        if not normalized_mime.startswith("image/") or normalized_mime == "image/svg+xml":
            raise ValueError("retouch output must be a supported raster image")
        summary = str(change_summary or "").strip()
        if not summary:
            raise ValueError("change_summary must not be empty")
        if len(summary) > 8000:
            raise ValueError("change_summary is too long")
        extension = {
            "image/avif": ".avif",
            "image/gif": ".gif",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/tiff": ".tiff",
            "image/webp": ".webp",
        }.get(normalized_mime, ".img")
        output_name = requested_name or f"精准修图{extension}"
        job_state = self.repository.get_retouch_job(job_id)
        target_scope = self.get_artifact_scope(job_state.artifact_id)
        target = self.get_user_artifact(
            job_state.artifact_id,
            account_id=target_scope.account_id,
        )
        decision = self.classify(
            output_name,
            normalized_mime,
            requested_visibility=target.visibility,
        )
        if decision.family is not ArtifactFamily.IMAGE or not decision.is_user_visible:
            raise ArtifactActionUnavailable(
                "retouch output filename or media type is not a user-visible image"
            )
        data = bytes(content)
        _validate_raster_signature(data, normalized_mime)
        blob = self.blobs.put_bytes(data)
        staged = RetouchStagedResult(
            sha256=blob.sha256,
            size_bytes=blob.size_bytes,
            mime_type=normalized_mime,
            requested_name=output_name,
            change_summary=summary,
            inspection_regions=coerce_inspection_regions(inspection_regions),
            quality_evidence=coerce_quality_evidence(quality_evidence),
            adapter_result_id=adapter_result_id,
        )
        job = self.repository.stage_retouch_result(
            job_id,
            staged,
            now=self.clock(),
            before_commit=before_commit,
        )
        assert job.staged_result is not None
        return job.staged_result

    def complete_staged_retouch(
        self,
        job_id: str,
        *,
        on_completed: Callable[
            [sqlite3.Connection, RetouchJob, ArtifactProjection], None
        ]
        | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> RetouchResult:
        job_state = self.repository.get_retouch_job(job_id)
        staged = job_state.staged_result
        if staged is None:
            raise RetouchConflict("retouch job has no staged adapter result")
        data = self.blobs.read_bytes(staged.sha256)
        if len(data) != staged.size_bytes:
            raise ValueError("staged retouch CAS size does not match metadata")
        _validate_raster_signature(data, staged.mime_type)
        target_scope = self.get_artifact_scope(job_state.artifact_id)
        target = self.get_user_artifact(
            job_state.artifact_id,
            account_id=target_scope.account_id,
        )
        decision = self.classify(
            staged.requested_name,
            staged.mime_type,
            requested_visibility=target.visibility,
        )
        if decision.family is not ArtifactFamily.IMAGE or not decision.is_user_visible:
            raise ArtifactActionUnavailable(
                "retouch output filename or media type is not a user-visible image"
            )
        blob = StoredBlob(
            sha256=staged.sha256,
            size_bytes=staged.size_bytes,
            path=self.blobs.path_for(staged.sha256),
        )
        job, artifact = self.repository.complete_retouch_job(
            job_id=job_id,
            blob=blob,
            requested_name=staged.requested_name,
            mime_type=staged.mime_type,
            decision=decision,
            quality_evidence=staged.quality_evidence,
            change_summary=staged.change_summary,
            inspection_regions=staged.inspection_regions,
            now=self.clock(),
            on_completed=on_completed,
            before_commit=before_commit,
        )
        return RetouchResult(job=job.public_projection(), artifact=artifact)

    def fail_retouch(
        self,
        job_id: str,
        reason: str,
        *,
        cancelled: bool = False,
        on_terminal: Callable[[sqlite3.Connection, RetouchJob], None] | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> RetouchJobProjection:
        return self.repository.fail_retouch_job(
            job_id,
            reason,
            target=(
                RetouchJobStatus.CANCELLED
                if cancelled
                else RetouchJobStatus.FAILED
            ),
            now=self.clock(),
            on_terminal=on_terminal,
            before_commit=before_commit,
        ).public_projection()
