"""Shared Cow Office authoring boundary for Core and public tools."""

from __future__ import annotations

import base64
import math
from pathlib import PurePath
from typing import Any, Mapping


MIME_TYPES = {
    "document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
}


class OfficeAuthoringContractError(ValueError):
    pass


def validated_authoring_request(
    family: str,
    extension: str,
    parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    if family not in MIME_TYPES or parameters.get("operation") != "create":
        raise OfficeAuthoringContractError("office_create_parameters_invalid")
    allowed = {
        "document": {"operation", "file_name", "title", "sections"},
        "pdf": {"operation", "file_name", "title", "sections"},
        "presentation": {"operation", "file_name", "title", "slides"},
        "spreadsheet": {"operation", "file_name", "title", "sheets"},
    }[family]
    if set(parameters) - allowed:
        raise OfficeAuthoringContractError("office_create_parameters_invalid")
    title = _text(parameters.get("title") or "e-Mate 办公产物", 512)
    file_name = str(parameters.get("file_name") or f"{title}{extension}").strip()
    if (
        not file_name
        or "/" in file_name
        or "\\" in file_name
        or PurePath(file_name).name != file_name
        or not file_name.casefold().endswith(extension)
        or len(file_name.encode("utf-8")) > 240
    ):
        raise OfficeAuthoringContractError("office_file_name_invalid")
    payload: dict[str, Any] = {"title": title}
    if family in {"document", "pdf"}:
        sections = _list(parameters.get("sections"), 64)
        payload["sections"] = [
            {
                "heading": _text(section.get("heading") or "", 512, empty=True),
                "paragraphs": [
                    _text(value, 4096)
                    for value in _list(section.get("paragraphs"), 128)
                ],
            }
            for section in sections
            if isinstance(section, Mapping)
        ]
        if len(payload["sections"]) != len(sections):
            raise OfficeAuthoringContractError("office_sections_invalid")
    elif family == "presentation":
        slides = _list(parameters.get("slides"), 120)
        payload["slides"] = [
            {
                "title": _text(slide.get("title"), 512),
                "bullets": [
                    _text(value, 2048)
                    for value in _list(slide.get("bullets") or [], 32, empty=True)
                ],
            }
            for slide in slides
            if isinstance(slide, Mapping)
        ]
        if len(payload["slides"]) != len(slides):
            raise OfficeAuthoringContractError("office_slides_invalid")
    else:
        sheets = _list(parameters.get("sheets"), 12)
        normalized = []
        for sheet in sheets:
            if not isinstance(sheet, Mapping):
                raise OfficeAuthoringContractError("office_sheets_invalid")
            rows = _list(sheet.get("rows"), 500, empty=True)
            normalized_rows = []
            for row in rows:
                if not isinstance(row, list) or len(row) > 50:
                    raise OfficeAuthoringContractError("office_rows_invalid")
                normalized_row = []
                for value in row:
                    if isinstance(value, float) and not math.isfinite(value):
                        raise OfficeAuthoringContractError("office_cell_invalid")
                    if value is not None and not isinstance(value, (str, int, float, bool)):
                        raise OfficeAuthoringContractError("office_cell_invalid")
                    normalized_row.append(
                        _text(value, 4096, empty=True) if isinstance(value, str) else value
                    )
                normalized_rows.append(normalized_row)
            normalized.append(
                {"name": _text(sheet.get("name"), 64), "rows": normalized_rows}
            )
        payload["sheets"] = normalized
    return payload, file_name


def validated_authoring_result(
    family: str,
    extension: str,
    result: Any,
) -> tuple[bytes, str, Mapping[str, Any]]:
    if (
        not isinstance(result, Mapping)
        or result.get("family") != family
        or result.get("extension") != extension
        or not isinstance(result.get("validation"), Mapping)
    ):
        raise OfficeAuthoringContractError("office_pack_result_invalid")
    try:
        content = base64.b64decode(str(result.get("content_base64") or ""), validate=True)
    except (ValueError, TypeError):
        raise OfficeAuthoringContractError("office_pack_result_invalid") from None
    mime_type = MIME_TYPES[family]
    signature = content.startswith(b"%PDF-") if extension == ".pdf" else content.startswith(b"PK")
    if (
        not 1 <= len(content) <= 5 * 1024 * 1024
        or result.get("size_bytes") != len(content)
        or result.get("mime_type") != mime_type
        or not signature
    ):
        raise OfficeAuthoringContractError("office_pack_result_invalid")
    return content, mime_type, dict(result["validation"])


def _list(value: Any, maximum: int, *, empty: bool = False) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum or (not empty and not value):
        raise OfficeAuthoringContractError("office_collection_invalid")
    return value


def _text(value: Any, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise OfficeAuthoringContractError("office_text_invalid")
    text = value.strip()
    if (not empty and not text) or len(text.encode("utf-8")) > maximum:
        raise OfficeAuthoringContractError("office_text_invalid")
    return text
