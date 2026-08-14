"""Callable EcoreX-native Office/PDF skill facades."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from agent.tools.base_tool import BaseTool, ToolResult
from common.office_authoring_contract import OFFICE_SECTION_SCHEMA, OFFICE_TABLE_SCHEMA
from common.office_pdf_runtime import (
    OfficePdfRuntimeError,
    probe_office_pdf_runtime,
)
from common.utils import expand_path


_PACK_SERVICE: Any = None
_PACK_SERVICE_LOCK = threading.RLock()


def bind_office_pack_service(service: Any) -> None:
    """Bind the verified Office Pack used by the public Cow tools."""

    if service is not None and not all(
        callable(getattr(service, name, None))
        for name in ("probe", "create", "edit", "read")
    ):
        raise ValueError("Office Pack service contract is incomplete")
    global _PACK_SERVICE
    with _PACK_SERVICE_LOCK:
        _PACK_SERVICE = service


def _office_pack_service() -> Any:
    with _PACK_SERVICE_LOCK:
        service = _PACK_SERVICE
    if service is None:
        raise OfficePdfRuntimeError("verified Office Pack service is unavailable")
    return service


_PUBLIC_ACTIONS = ("probe", "status", "create", "edit", "inspect")


_COMMON_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_PUBLIC_ACTIONS),
            "description": (
                "Verified Office Pack operation. Use probe/status for readiness, "
                "create/edit for replacement authoring, and inspect for bounded content."
            ),
        },
        "path": {
            "type": "string",
            "description": "Output path for create, or source path for edit/inspect/render.",
        },
        "output_path": {
            "type": "string",
            "description": "Optional edit destination. Defaults to a new -edited file.",
        },
        "title": {
            "type": "string",
            "description": "Document, workbook, deck, or PDF title for create/edit.",
        },
        "sections": {
            "type": "array",
            "items": OFFICE_SECTION_SCHEMA,
            "description": "Complete structured sections for DOCX/PDF create or replacement edit.",
        },
        "slides": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Complete title/bullet slides for PPTX create or replacement edit.",
        },
        "sheets": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Complete named sheets and rows for XLSX create or replacement edit.",
        },
    },
    "required": ["action"],
}
_DOCUMENT_PARAMS = {
    **_COMMON_PARAMS,
    "properties": {
        **_COMMON_PARAMS["properties"],
        "tables": {
            "type": "array",
            "items": OFFICE_TABLE_SCHEMA,
            "maxItems": 32,
            "description": "Complete rectangular tables for DOCX create or replacement edit.",
        },
    },
}


def _redacted_error(message: str, exc: BaseException) -> dict[str, Any]:
    return {
        "error": message,
        "errorType": exc.__class__.__name__,
        "redacted": True,
    }


class _OfficeArtifactTool(BaseTool):
    artifact_kind = ""
    official_skill = ""
    compatibility_id = ""
    params = _COMMON_PARAMS

    def __init__(self, config: Optional[dict[str, Any]] = None):
        self.config = config or {}
        self.cwd = self.config.get("cwd") or str(Path.cwd())

    def apply_config(self, config: dict) -> None:
        self.config = config or {}
        self.cwd = self.config.get("cwd") or self.cwd

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        args = params or {}
        action = str(args.get("action") or "").strip().lower().replace("-", "_")
        if action == "status":
            action = "probe"
        if not action:
            return ToolResult.fail("action is required")

        try:
            if action == "probe":
                return ToolResult.success(self._probe())
            if action == "create":
                return ToolResult.success(self._create_or_edit(args, edit=False))
            if action == "edit":
                return ToolResult.success(self._create_or_edit(args, edit=True))
            if action == "inspect":
                return ToolResult.success(self._inspect(self._source_path(args)))
            return ToolResult.fail({
                "error": "unsupported office artifact action",
                "action": action,
                "allowedActions": list(_PUBLIC_ACTIONS),
                "redacted": True,
            })
        except OfficePdfRuntimeError as exc:
            return ToolResult.fail(_redacted_error("Office/PDF runtime capability is unavailable.", exc))
        except FileNotFoundError as exc:
            return ToolResult.fail(_redacted_error("Office/PDF artifact was not found.", exc))
        except PermissionError as exc:
            return ToolResult.fail(_redacted_error("Office/PDF artifact access was blocked by permissions.", exc))
        except Exception as exc:
            return ToolResult.fail(_redacted_error("Office/PDF artifact action failed.", exc))

    def _probe(self) -> dict[str, Any]:
        try:
            pack = _office_pack_service().probe(timeout_seconds=30.0)
        except OfficePdfRuntimeError:
            pack = None
        if isinstance(pack, Mapping):
            return {
                "schemaVersion": 1,
                "packId": "office",
                "status": "ready",
                "artifactKind": self.artifact_kind,
                "compatibilityId": self.compatibility_id,
                "officialSkill": self.official_skill,
                "runtime": dict(pack),
                "redacted": True,
            }
        payload = probe_office_pdf_runtime()
        kinds = payload.get("artifactKinds") if isinstance(payload.get("artifactKinds"), dict) else {}
        return {
            "schemaVersion": payload.get("schemaVersion"),
            "packId": payload.get("packId"),
            "status": payload.get("status"),
            "artifactKind": self.artifact_kind,
            "compatibilityId": self.compatibility_id,
            "officialSkill": self.official_skill,
            "runtime": kinds.get(self.artifact_kind, {}),
            "redacted": True,
        }

    def _create_or_edit(self, args: Dict[str, Any], *, edit: bool) -> dict[str, Any]:
        source = self._source_path(args) if edit else None
        raw_target = str(args.get("output_path") or args.get("path") or "").strip()
        if not raw_target:
            raise FileNotFoundError("path is required")
        extension = {
            "document": ".docx",
            "spreadsheet": ".xlsx",
            "presentation": ".pptx",
            "pdf": ".pdf",
        }[self.artifact_kind]
        if source is not None and not str(args.get("output_path") or "").strip():
            target = source.with_name(f"{source.stem}-edited{extension}")
        else:
            target = self._resolve_path(raw_target)
        self._authorize_file_access("write", target)
        from common.office_authoring_contract import (
            validated_authoring_request,
            validated_authoring_result,
        )

        structured = {
            "operation": "create",
            "file_name": target.name,
            "title": args.get("title") or target.stem,
        }
        field = {
            "document": "sections",
            "pdf": "sections",
            "presentation": "slides",
            "spreadsheet": "sheets",
        }[self.artifact_kind]
        structured[field] = args.get(field)
        family = self.artifact_kind
        if family == "document":
            structured["tables"] = args.get("tables")
        payload, _ = validated_authoring_request(family, extension, structured)
        service = _office_pack_service()
        result = (
            service.edit(
                family,
                source.read_bytes(),
                payload,
                timeout_seconds=30.0,
            )
            if source is not None
            else service.create(family, payload, timeout_seconds=30.0)
        )
        content, mime_type, validation = validated_authoring_result(
            family, extension, result
        )
        self._write_atomic(target, content)
        return {
            "status": "completed",
            "operation": "edit" if edit else "create",
            "path": str(target),
            "source_path": str(source) if source is not None else None,
            "replacement_mode": (
                "atomic-in-place"
                if source is not None and source == target
                else "new-file"
            ),
            "file_name": target.name,
            "mime_type": mime_type,
            "size_bytes": len(content),
            "validation": dict(validation),
            "redacted": True,
        }

    def _inspect(self, source: Path) -> dict[str, Any]:
        result = _office_pack_service().read(
            self.artifact_kind,
            source.read_bytes(),
            timeout_seconds=30.0,
        )
        if not isinstance(result, Mapping) or result.get("family") != self.artifact_kind:
            raise OfficePdfRuntimeError("Office Pack inspection result is invalid")
        return {**dict(result), "path": str(source), "redacted": True}

    @staticmethod
    def _write_atomic(target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _source_path(self, args: Dict[str, Any]) -> Path:
        raw = str(args.get("path") or "").strip()
        if not raw:
            raise FileNotFoundError("path is required")
        path = self._resolve_path(raw)
        self._authorize_file_access("read", path)
        if not path.exists():
            raise FileNotFoundError("source artifact does not exist")
        return path

    def _resolve_path(self, raw: str) -> Path:
        path = Path(expand_path(raw))
        if not path.is_absolute():
            path = Path(self.cwd) / path
        return path.resolve()

    def _authorize_file_access(self, operation: str, path: Path) -> None:
        try:
            from common.ecorex_tool_permissions import get_tool_permission_broker

            decision = get_tool_permission_broker().authorize_file_access(
                operation,
                str(path),
                cwd=str(self.cwd),
            )
        except Exception as exc:
            raise PermissionError(f"permission broker unavailable: {exc.__class__.__name__}") from exc
        if not decision.get("allowed"):
            raise PermissionError("permission denied")

class OfficeDocumentsTool(_OfficeArtifactTool):
    name = "office_documents"
    artifact_kind = "document"
    compatibility_id = "office-documents"
    official_skill = "documents"
    params = _DOCUMENT_PARAMS
    description = (
        "Create, replacement-edit, and inspect Word/DOCX files "
        "through the verified Office Pack. Create/edit return a workspace file path."
    )


class OfficePdfTool(_OfficeArtifactTool):
    name = "office_pdf"
    artifact_kind = "pdf"
    compatibility_id = "office-pdf"
    official_skill = "pdf"
    description = (
        "Create, replacement-edit, and inspect PDF files "
        "through the verified Office Pack. Create/edit return a workspace file path."
    )


class OfficePresentationsTool(_OfficeArtifactTool):
    name = "office_presentations"
    artifact_kind = "presentation"
    compatibility_id = "office-presentations"
    official_skill = "Presentations"
    description = (
        "Create, replacement-edit, and inspect PPTX decks through "
        "the verified Office Pack. Create/edit return a workspace file path."
    )


class OfficeSpreadsheetsTool(_OfficeArtifactTool):
    name = "office_spreadsheets"
    artifact_kind = "spreadsheet"
    compatibility_id = "office-spreadsheets"
    official_skill = "Spreadsheets"
    description = (
        "Create, replacement-edit, and inspect XLSX workbooks through "
        "the verified Office Pack. Create/edit return a workspace file path."
    )
