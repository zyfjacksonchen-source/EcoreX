"""Runtime-owned Office Skill execution and Artifact publication."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from pathlib import PurePath
from typing import Any, Callable, Mapping

from ecorex.artifacts import (
    ArtifactError,
    ArtifactFamily,
    ArtifactNotFound,
    ArtifactScope,
    ArtifactService,
    ArtifactStatus,
)
from ecorex.capabilities import ToolInvocationContext
from ecorex.json_boundary import JSONComplexityError, validate_json_complexity
from ecorex.protocol import ItemKind, ItemStatus


_SKILL_FAMILIES = {
    "office-documents": (ArtifactFamily.DOCUMENT, ".docx"),
    "office-spreadsheets": (ArtifactFamily.SPREADSHEET, ".xlsx"),
    "office-presentations": (ArtifactFamily.PRESENTATION, ".pptx"),
    "office-pdf": (ArtifactFamily.PDF, ".pdf"),
}
_MAX_PARAMETERS_BYTES = 64 * 1024
_MAX_INSPECTION_TEXT_BYTES = 192 * 1024
_MAX_INSPECTION_RESULT_BYTES = 240 * 1024


class OfficeSkillError(RuntimeError):
    code = "office_skill_failed"

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RuntimeOfficeSkillBackend:
    """Create or inspect one bounded Office artifact through the signed Pack."""

    def __init__(
        self,
        *,
        service: Any,
        artifacts: ArtifactService,
        kernel: Any,
        account_id: str,
    ) -> None:
        if not all(callable(getattr(service, method, None)) for method in ("create", "read")):
            raise ValueError("Office Pack service does not implement create and read")
        self.service = service
        self.artifacts = artifacts
        self.kernel = kernel
        self.account_id = account_id

    def supports(self, skill: Any) -> bool:
        name = str(getattr(skill, "name", ""))
        return (
            name in _SKILL_FAMILIES
            and str(getattr(skill, "extension_id", "")) == f"skill.{name}"
        )

    async def run(
        self,
        skill: Any,
        parameters: Mapping[str, Any],
        context: ToolInvocationContext,
        *,
        state_fence: Callable[[], None],
    ) -> Mapping[str, Any]:
        if not self.supports(skill):
            raise OfficeSkillError("office_skill_unsupported")
        scope = context.execution_scope
        if scope is None or not context.idempotency_key:
            raise OfficeSkillError("office_skill_execution_scope_missing")
        family, extension = _SKILL_FAMILIES[str(skill.name)]
        _validate_json_request(parameters)
        if parameters.get("operation") in {"inspect", "read"}:
            return await self._read(
                family,
                parameters,
                context,
                state_fence=state_fence,
            )
        payload, requested_name = _validated_request(
            family, extension, parameters
        )
        request_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "family": family.value,
                    "file_name": requested_name,
                    "payload": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        publication_key = (
            f"office:{self.account_id}:{context.idempotency_key}:"
            f"{family.value}"
        )
        item_id = "itm_" + hashlib.sha256(
            f"{publication_key}\0artifact-item".encode("utf-8")
        ).hexdigest()[:32]
        existing = self._existing(item_id, request_sha256)
        if existing is not None:
            return existing

        state_fence()
        pack_result = await asyncio.to_thread(
            self.service.create,
            family.value,
            payload,
            timeout_seconds=30.0,
        )
        state_fence()
        content, mime_type, validation = _validated_pack_result(
            family, extension, pack_result
        )
        declaration = self.artifacts.issue_trusted_deliverable_declaration(
            "skill_run", family=family
        )
        prepared = self.artifacts.prepare_artifact(
            content,
            requested_name=requested_name,
            mime_type=mime_type,
            declaration=declaration,
            quality_evidence={
                "status": "warning",
                "checks": [
                    {
                        "name": "structural-validation",
                        "status": "passed",
                        "detail": json.dumps(
                            validation,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )[:1024],
                    },
                    {
                        "name": "visual-inspection",
                        "status": "warning",
                        "detail": "视觉渲染验收尚未执行",
                    },
                ],
                "summary": "已完成结构校验，等待视觉渲染验收。",
            },
            scope=ArtifactScope(
                account_id=self.account_id,
                thread_id=scope.thread_id,
                turn_id=scope.turn_id,
                created_by_tool_id="skill_run",
            ),
        )
        state_fence()
        created = None
        with self.kernel.database.transaction() as connection:
            row = connection.execute(
                "SELECT content_json FROM items WHERE item_id=?", (item_id,)
            ).fetchone()
            if row is None:
                created = self.artifacts.create_artifact_in_transaction(
                    connection, prepared
                )
                self.kernel._create_item_in_transaction(
                    connection,
                    item_id=item_id,
                    thread_id=scope.thread_id,
                    turn_id=scope.turn_id,
                    kind=ItemKind.ARTIFACT,
                    content={
                        "artifact": created.to_dict(),
                        "source": {
                            "kind": "office_skill",
                            "skill_id": str(skill.name),
                            "request_sha256": request_sha256,
                        },
                        "change_summary": "办公文件已生成并完成结构校验。",
                    },
                    status=ItemStatus.COMPLETED,
                    idempotency_key=f"{publication_key}:artifact-item",
                )
                self.kernel.events.append_in_transaction(
                    connection,
                    thread_id=scope.thread_id,
                    turn_id=scope.turn_id,
                    item_id=item_id,
                    job_id=scope.job_id,
                    event_type="artifact.office.created",
                    payload={
                        "artifact_id": created.artifact_id,
                        "revision_id": created.revision_id,
                        "family": created.family.value,
                        "sha256": created.sha256,
                    },
                    correlation_id=context.idempotency_key,
                    idempotency_key=f"{publication_key}:created-event",
                )
        if created is None:
            existing = self._existing(item_id, request_sha256)
            if existing is None:
                raise OfficeSkillError("office_artifact_publication_conflict")
            return existing
        return _public_result(created)

    async def _read(
        self,
        family: ArtifactFamily,
        parameters: Mapping[str, Any],
        context: ToolInvocationContext,
        *,
        state_fence: Callable[[], None],
    ) -> Mapping[str, Any]:
        scope = context.execution_scope
        assert scope is not None
        artifact_id, revision_id = _validated_read_request(parameters)
        try:
            artifact = self.artifacts.get_user_artifact(
                artifact_id,
                account_id=self.account_id,
            )
        except ArtifactNotFound:
            raise OfficeSkillError("office_artifact_not_found") from None
        if artifact.revision_id != revision_id:
            raise OfficeSkillError("office_artifact_revision_changed")
        if artifact.family is not family or artifact.status is not ArtifactStatus.READY:
            raise OfficeSkillError("office_artifact_not_readable")
        try:
            artifact_scope = self.artifacts.get_artifact_scope(artifact_id)
        except ArtifactError:
            raise OfficeSkillError("office_artifact_not_found") from None
        if (
            artifact_scope.account_id != self.account_id
            or artifact_scope.thread_id != scope.thread_id
        ):
            raise OfficeSkillError("office_artifact_not_found")
        if not 1 <= artifact.size_bytes <= 5 * 1024 * 1024:
            raise OfficeSkillError("office_artifact_too_large")

        state_fence()
        try:
            content = self.artifacts.read_user_content(
                artifact_id,
                revision_id,
                account_id=self.account_id,
            )
        except ArtifactError:
            raise OfficeSkillError("office_artifact_read_failed") from None
        if len(content) != artifact.size_bytes:
            raise OfficeSkillError("office_artifact_read_failed")
        pack_result = await asyncio.to_thread(
            self.service.read,
            family.value,
            content,
            timeout_seconds=30.0,
        )
        state_fence()
        try:
            current = self.artifacts.get_user_artifact(
                artifact_id,
                account_id=self.account_id,
            )
        except ArtifactNotFound:
            raise OfficeSkillError("office_artifact_not_found") from None
        if current.revision_id != revision_id:
            raise OfficeSkillError("office_artifact_revision_changed")
        return _validated_read_result(artifact, family, pack_result)

    def _existing(
        self, item_id: str, request_sha256: str
    ) -> Mapping[str, Any] | None:
        with self.kernel.database.reader() as connection:
            row = connection.execute(
                "SELECT content_json FROM items WHERE item_id=?", (item_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            content = json.loads(str(row["content_json"]))
            artifact_id = content["artifact"]["artifact_id"]
            revision_id = content["artifact"]["revision_id"]
            stored_request_sha256 = content["source"]["request_sha256"]
        except (KeyError, TypeError, json.JSONDecodeError):
            raise OfficeSkillError("office_artifact_item_invalid") from None
        if stored_request_sha256 != request_sha256:
            raise OfficeSkillError("office_artifact_idempotency_conflict")
        artifact = self.artifacts.get_user_artifact(
            str(artifact_id), account_id=self.account_id
        )
        if artifact.revision_id != revision_id:
            raise OfficeSkillError("office_artifact_revision_changed")
        return _public_result(artifact)


def _validated_request(
    family: ArtifactFamily,
    extension: str,
    parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    if not isinstance(parameters, Mapping) or parameters.get("operation") != "create":
        raise OfficeSkillError("office_create_parameters_invalid")
    allowed = {
        ArtifactFamily.DOCUMENT: {"operation", "file_name", "title", "sections"},
        ArtifactFamily.PDF: {"operation", "file_name", "title", "sections"},
        ArtifactFamily.PRESENTATION: {"operation", "file_name", "title", "slides"},
        ArtifactFamily.SPREADSHEET: {"operation", "file_name", "title", "sheets"},
    }[family]
    if set(parameters) - allowed:
        raise OfficeSkillError("office_create_parameters_invalid")
    title = _bounded_text(parameters.get("title") or "e-Mate 办公产物", 512)
    file_name = str(parameters.get("file_name") or f"{title}{extension}").strip()
    if (
        not file_name
        or "/" in file_name
        or "\\" in file_name
        or PurePath(file_name).name != file_name
        or not file_name.casefold().endswith(extension)
        or len(file_name.encode("utf-8")) > 240
    ):
        raise OfficeSkillError("office_file_name_invalid")
    payload: dict[str, Any] = {"title": title}
    if family in {ArtifactFamily.DOCUMENT, ArtifactFamily.PDF}:
        sections = parameters.get("sections")
        if not isinstance(sections, list) or not 1 <= len(sections) <= 64:
            raise OfficeSkillError("office_sections_invalid")
        payload["sections"] = [
            {
                "heading": _bounded_text(section.get("heading") or "", 512, empty=True),
                "paragraphs": [
                    _bounded_text(value, 4096)
                    for value in _bounded_list(section.get("paragraphs"), 128)
                ],
            }
            for section in sections
            if isinstance(section, Mapping)
        ]
        if len(payload["sections"]) != len(sections):
            raise OfficeSkillError("office_sections_invalid")
    elif family is ArtifactFamily.PRESENTATION:
        slides = _bounded_list(parameters.get("slides"), 120)
        payload["slides"] = [
            {
                "title": _bounded_text(slide.get("title"), 512),
                "bullets": [
                    _bounded_text(value, 2048)
                    for value in _bounded_list(slide.get("bullets") or [], 32, empty=True)
                ],
            }
            for slide in slides
            if isinstance(slide, Mapping)
        ]
        if len(payload["slides"]) != len(slides):
            raise OfficeSkillError("office_slides_invalid")
    else:
        sheets = _bounded_list(parameters.get("sheets"), 12)
        normalized_sheets = []
        for sheet in sheets:
            if not isinstance(sheet, Mapping):
                raise OfficeSkillError("office_sheets_invalid")
            rows = _bounded_list(sheet.get("rows"), 500, empty=True)
            normalized_rows = []
            for row in rows:
                if not isinstance(row, list) or len(row) > 50:
                    raise OfficeSkillError("office_rows_invalid")
                normalized_row = []
                for value in row:
                    if isinstance(value, float) and not math.isfinite(value):
                        raise OfficeSkillError("office_cell_invalid")
                    if value is not None and not isinstance(value, (str, int, float, bool)):
                        raise OfficeSkillError("office_cell_invalid")
                    if isinstance(value, str):
                        value = _bounded_text(value, 4096, empty=True)
                    normalized_row.append(value)
                normalized_rows.append(normalized_row)
            normalized_sheets.append(
                {"name": _bounded_text(sheet.get("name"), 64), "rows": normalized_rows}
            )
        payload["sheets"] = normalized_sheets
    return payload, file_name


def _validate_json_request(parameters: Mapping[str, Any]) -> None:
    if not isinstance(parameters, Mapping):
        raise OfficeSkillError("office_parameters_invalid")
    try:
        value = dict(parameters)
        validate_json_complexity(value, max_depth=8, max_nodes=20_000)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (
        JSONComplexityError,
        TypeError,
        ValueError,
        UnicodeEncodeError,
        RecursionError,
    ):
        raise OfficeSkillError("office_parameters_invalid") from None
    if len(encoded) > _MAX_PARAMETERS_BYTES:
        raise OfficeSkillError("office_parameters_too_large")


def _validated_read_request(parameters: Mapping[str, Any]) -> tuple[str, str]:
    if set(parameters) != {"operation", "artifact_id", "revision_id"}:
        raise OfficeSkillError("office_read_parameters_invalid")
    if parameters.get("operation") not in {"inspect", "read"}:
        raise OfficeSkillError("office_read_parameters_invalid")
    return (
        _bounded_identifier(parameters.get("artifact_id")),
        _bounded_identifier(parameters.get("revision_id")),
    )


def _bounded_identifier(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise OfficeSkillError("office_read_parameters_invalid")
    if not 1 <= len(value.encode("utf-8")) <= 128:
        raise OfficeSkillError("office_read_parameters_invalid")
    return value


def _bounded_list(value: Any, maximum: int, *, empty: bool = False) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum or (not empty and not value):
        raise OfficeSkillError("office_collection_invalid")
    return value


def _bounded_text(value: Any, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise OfficeSkillError("office_text_invalid")
    text = value.strip()
    if (not empty and not text) or len(text.encode("utf-8")) > maximum:
        raise OfficeSkillError("office_text_invalid")
    return text


def _validated_pack_result(
    family: ArtifactFamily,
    extension: str,
    result: Any,
) -> tuple[bytes, str, Mapping[str, Any]]:
    if not isinstance(result, Mapping) or result.get("family") != family.value:
        raise OfficeSkillError("office_pack_result_invalid")
    if result.get("extension") != extension or not isinstance(result.get("validation"), Mapping):
        raise OfficeSkillError("office_pack_result_invalid")
    try:
        content = base64.b64decode(str(result.get("content_base64") or ""), validate=True)
    except (ValueError, TypeError):
        raise OfficeSkillError("office_pack_result_invalid") from None
    if not 1 <= len(content) <= 5 * 1024 * 1024 or result.get("size_bytes") != len(content):
        raise OfficeSkillError("office_pack_result_invalid")
    if extension == ".pdf":
        valid_signature = content.startswith(b"%PDF-")
    else:
        valid_signature = content.startswith(b"PK")
    expected_mime_type = {
        ArtifactFamily.DOCUMENT: (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        ArtifactFamily.SPREADSHEET: (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        ArtifactFamily.PRESENTATION: (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        ArtifactFamily.PDF: "application/pdf",
    }[family]
    mime_type = result.get("mime_type")
    if not valid_signature or mime_type != expected_mime_type:
        raise OfficeSkillError("office_pack_result_invalid")
    return content, expected_mime_type, dict(result["validation"])


def _validated_read_result(
    artifact: Any,
    family: ArtifactFamily,
    result: Any,
) -> Mapping[str, Any]:
    if not isinstance(result, Mapping):
        raise OfficeSkillError("office_pack_result_invalid")
    detached = dict(result)
    try:
        validate_json_complexity(detached, max_depth=8, max_nodes=20_000)
        encoded = json.dumps(
            detached,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (
        JSONComplexityError,
        TypeError,
        ValueError,
        UnicodeEncodeError,
        RecursionError,
    ):
        raise OfficeSkillError("office_pack_result_invalid") from None
    text = result.get("text")
    structure = result.get("structure")
    warnings = result.get("warnings")
    if (
        result.get("family") != family.value
        or not isinstance(text, str)
        or len(text.encode("utf-8")) > _MAX_INSPECTION_TEXT_BYTES
        or not isinstance(structure, Mapping)
        or not isinstance(warnings, list)
        or len(warnings) > 64
        or any(
            not isinstance(value, str) or len(value.encode("utf-8")) > 512
            for value in warnings
        )
        or not isinstance(result.get("truncated"), bool)
        or len(encoded) > _MAX_INSPECTION_RESULT_BYTES
    ):
        raise OfficeSkillError("office_pack_result_invalid")
    return {
        "status": "completed",
        "operation": "inspect",
        "artifact_id": artifact.artifact_id,
        "revision_id": artifact.revision_id,
        "family": artifact.family.value,
        "display_name": artifact.display_name,
        "mime_type": artifact.mime_type,
        "text": text,
        "structure": dict(structure),
        "warnings": list(warnings),
        "truncated": result["truncated"],
        "summary": "已提取办公文件文本与结构；未执行视觉布局验收。",
    }


def _public_result(artifact: Any) -> Mapping[str, Any]:
    return {
        "status": "completed",
        "artifact_id": artifact.artifact_id,
        "revision_id": artifact.revision_id,
        "family": artifact.family.value,
        "display_name": artifact.display_name,
        "mime_type": artifact.mime_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "content_url": f"/api/v1/artifacts/{artifact.artifact_id}/content",
        "summary": "办公文件已生成并完成结构校验；视觉渲染验收尚未执行。",
    }


__all__ = ["OfficeSkillError", "RuntimeOfficeSkillBackend"]
