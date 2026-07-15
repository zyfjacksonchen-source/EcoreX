"""Callable EcoreX-native Office/PDF skill facades."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from agent.tools.base_tool import BaseTool, ToolResult
from common.office_pdf_runtime import (
    OfficePdfRuntimeError,
    analyze_document_quality,
    analyze_pdf_quality,
    analyze_presentation_quality,
    analyze_spreadsheet_quality,
    build_document_quality_evidence,
    build_pdf_quality_evidence,
    build_presentation_quality_evidence,
    build_spreadsheet_quality_evidence,
    compare_pdf_page_quality,
    inspect_office_pdf_artifact,
    probe_office_pdf_runtime,
    render_document_preview,
    render_pdf_pages,
    render_presentation_preview,
    render_spreadsheet_preview,
)
from common.utils import expand_path
from config import conf


_COMMON_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "probe",
                "status",
                "inspect",
                "analyze",
                "render_preview",
                "render_pages",
                "quality_check",
                "compare",
            ],
            "description": (
                "Operation to run. Use probe/status for runtime readiness, inspect for "
                "redacted structure, analyze for quality metrics, render_preview/render_pages "
                "for trusted PNG previews, quality_check for gate evidence, and compare for PDF references."
            ),
        },
        "path": {
            "type": "string",
            "description": "Local Office/PDF artifact path.",
        },
        "reference_path": {
            "type": "string",
            "description": "Optional reference PDF path for visual/layout comparison.",
        },
        "output_dir": {
            "type": "string",
            "description": "Optional output directory for rendered previews.",
        },
        "max_pages": {
            "type": "integer",
            "description": "Maximum rendered PDF/DOCX pages. Defaults to 4.",
        },
        "max_slides": {
            "type": "integer",
            "description": "Maximum rendered presentation slides. Defaults to 4.",
        },
        "max_sheets": {
            "type": "integer",
            "description": "Maximum rendered spreadsheet sheets. Defaults to 4.",
        },
        "dpi": {
            "type": "integer",
            "description": "Render DPI. Defaults to 144.",
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "LibreOffice render timeout in seconds. Defaults to 90.",
        },
        "render": {
            "type": "boolean",
            "description": "For quality_check, render trusted previews before building evidence. Defaults to true.",
        },
        "visual_inspection_passed": {
            "type": "boolean",
            "description": "Set true only after a trusted visual review has passed.",
        },
        "authoring_route": {
            "type": "string",
            "description": "Presentation authoring route, e.g. artifact-tool, template-following, or verified-existing-deck.",
        },
    },
    "required": ["action"],
}


def _safe_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _safe_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


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
        if action == "render_pages":
            action = "render_preview"
        if not action:
            return ToolResult.fail("action is required")

        try:
            if action == "probe":
                return ToolResult.success(self._probe())
            if action == "inspect":
                return ToolResult.success(inspect_office_pdf_artifact(self._source_path(args), kind=self.artifact_kind))
            if action == "analyze":
                return ToolResult.success(self._analyze(self._source_path(args)))
            if action == "render_preview":
                return ToolResult.success(self._render(self._source_path(args), args))
            if action == "quality_check":
                return ToolResult.success(self._quality_check(self._source_path(args), args))
            if action == "compare":
                return ToolResult.success(self._compare(self._source_path(args), args))
            return ToolResult.fail({
                "error": "unsupported office artifact action",
                "action": action,
                "allowedActions": [
                    "probe",
                    "inspect",
                    "analyze",
                    "render_preview",
                    "quality_check",
                    "compare",
                ],
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

    def _default_output_dir(self, source: Path) -> Path:
        workspace = Path(expand_path(conf().get("agent_workspace", "~/EcoreX")))
        digest = hashlib.sha256(str(source).encode("utf-8", errors="replace")).hexdigest()[:12]
        return workspace / "office-renders" / self.artifact_kind / digest

    def _output_dir(self, source: Path, args: Dict[str, Any]) -> Path:
        raw = str(args.get("output_dir") or "").strip()
        path = self._resolve_path(raw) if raw else self._default_output_dir(source)
        self._authorize_file_access("write", path)
        path.mkdir(parents=True, exist_ok=True)
        return path

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

    def _render_args(self, args: Dict[str, Any]) -> dict[str, int]:
        return {
            "dpi": _safe_int(args.get("dpi"), 144, minimum=72, maximum=300),
            "timeout_seconds": _safe_int(args.get("timeout_seconds"), 90, minimum=5, maximum=600),
            "max_pages": _safe_int(args.get("max_pages"), 4, minimum=1, maximum=24),
            "max_slides": _safe_int(args.get("max_slides"), 4, minimum=1, maximum=24),
            "max_sheets": _safe_int(args.get("max_sheets"), 4, minimum=1, maximum=24),
        }

    def _analyze(self, source: Path) -> dict[str, Any]:
        analyzers: dict[str, Callable[[Path], dict[str, Any]]] = {
            "document": analyze_document_quality,
            "pdf": analyze_pdf_quality,
            "presentation": analyze_presentation_quality,
            "spreadsheet": analyze_spreadsheet_quality,
        }
        return analyzers[self.artifact_kind](source)

    def _render(self, source: Path, args: Dict[str, Any]) -> dict[str, Any]:
        target = self._output_dir(source, args)
        render_args = self._render_args(args)
        if self.artifact_kind == "pdf":
            return render_pdf_pages(
                source,
                target,
                max_pages=render_args["max_pages"],
                dpi=render_args["dpi"],
            )
        if self.artifact_kind == "document":
            return render_document_preview(
                source,
                target,
                max_pages=render_args["max_pages"],
                dpi=render_args["dpi"],
                timeout_seconds=render_args["timeout_seconds"],
            )
        if self.artifact_kind == "presentation":
            return render_presentation_preview(
                source,
                target,
                max_slides=render_args["max_slides"],
                dpi=render_args["dpi"],
                timeout_seconds=render_args["timeout_seconds"],
            )
        if self.artifact_kind == "spreadsheet":
            return render_spreadsheet_preview(
                source,
                target,
                max_sheets=render_args["max_sheets"],
                dpi=render_args["dpi"],
                timeout_seconds=render_args["timeout_seconds"],
            )
        raise OfficePdfRuntimeError("unsupported artifact kind")

    def _quality_check(self, source: Path, args: Dict[str, Any]) -> dict[str, Any]:
        renders = []
        render_error: Optional[dict[str, Any]] = None
        if _safe_bool(args.get("render"), default=True):
            try:
                rendered = self._render(source, args)
                renders = list(rendered.get("artifacts") or []) if isinstance(rendered, dict) else []
            except Exception as exc:
                render_error = _redacted_error("trusted preview render failed", exc)

        visual_passed = _safe_bool(args.get("visual_inspection_passed"), default=False)
        if self.artifact_kind == "pdf":
            reference_path = None
            if args.get("reference_path"):
                reference_path = self._resolve_path(str(args.get("reference_path")))
                self._authorize_file_access("read", reference_path)
            evidence = build_pdf_quality_evidence(
                source,
                renders=renders,
                reference_path=reference_path,
                visual_inspection_passed=visual_passed,
            )
        elif self.artifact_kind == "document":
            evidence = build_document_quality_evidence(
                source,
                renders=renders,
                visual_inspection_passed=visual_passed,
            )
        elif self.artifact_kind == "presentation":
            evidence = build_presentation_quality_evidence(
                source,
                authoring_route=str(args.get("authoring_route") or ""),
                renders=renders,
                visual_inspection_passed=visual_passed,
            )
        elif self.artifact_kind == "spreadsheet":
            evidence = build_spreadsheet_quality_evidence(
                source,
                renders=renders,
                visual_inspection_passed=visual_passed,
            )
        else:
            raise OfficePdfRuntimeError("unsupported artifact kind")
        if render_error:
            evidence["renderError"] = render_error
        evidence["callableTool"] = self.name
        evidence["compatibilityId"] = self.compatibility_id
        evidence["officialSkill"] = self.official_skill
        evidence["redacted"] = True
        return evidence

    def _compare(self, source: Path, args: Dict[str, Any]) -> dict[str, Any]:
        if self.artifact_kind != "pdf":
            return {
                "status": "not-applicable",
                "message": "compare is currently supported for office_pdf/PDF only",
                "redacted": True,
            }
        raw_reference = str(args.get("reference_path") or "").strip()
        if not raw_reference:
            raise FileNotFoundError("reference_path is required")
        reference = self._resolve_path(raw_reference)
        self._authorize_file_access("read", reference)
        if not reference.exists():
            raise FileNotFoundError("reference artifact does not exist")
        return compare_pdf_page_quality(reference, source)


class OfficeDocumentsTool(_OfficeArtifactTool):
    name = "office_documents"
    artifact_kind = "document"
    compatibility_id = "office-documents"
    official_skill = "documents"
    description = (
        "Callable EcoreX-native facade for the documents skill. Use to probe, inspect, "
        "render, and quality-check Word/DOCX artifacts without exposing document text."
    )


class OfficePdfTool(_OfficeArtifactTool):
    name = "office_pdf"
    artifact_kind = "pdf"
    compatibility_id = "office-pdf"
    official_skill = "pdf"
    description = (
        "Callable EcoreX-native facade for the pdf skill. Use to probe, inspect, render, "
        "quality-check, and compare PDF page layout evidence."
    )


class OfficePresentationsTool(_OfficeArtifactTool):
    name = "office_presentations"
    artifact_kind = "presentation"
    compatibility_id = "office-presentations"
    official_skill = "Presentations"
    description = (
        "Callable EcoreX-native facade for the Presentations skill. Use to probe, inspect, "
        "render, and quality-check PPT/PPTX decks for story flow, bounds, fonts, charts, and overlap."
    )


class OfficeSpreadsheetsTool(_OfficeArtifactTool):
    name = "office_spreadsheets"
    artifact_kind = "spreadsheet"
    compatibility_id = "office-spreadsheets"
    official_skill = "Spreadsheets"
    description = (
        "Callable EcoreX-native facade for the Spreadsheets skill. Use to probe, inspect, "
        "render, and quality-check Excel/CSV artifacts for typed values, formulas, charts, and exports."
    )
