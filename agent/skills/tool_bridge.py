"""Bridge installed skills and capability aliases to callable agent tools."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional


SKILL_CALLABLE_TOOL_ALIASES: dict[str, str] = {
    "office-documents": "office_documents",
    "office_documents": "office_documents",
    "documents": "office_documents",
    "document": "office_documents",
    "word": "office_documents",
    "doc": "office_documents",
    "docx": "office_documents",
    "office-pdf": "office_pdf",
    "office_pdf": "office_pdf",
    "pdf": "office_pdf",
    "office-presentations": "office_presentations",
    "office_presentations": "office_presentations",
    "presentations": "office_presentations",
    "presentation": "office_presentations",
    "powerpoint": "office_presentations",
    "slides": "office_presentations",
    "ppt": "office_presentations",
    "pptx": "office_presentations",
    "office-spreadsheets": "office_spreadsheets",
    "office_spreadsheets": "office_spreadsheets",
    "spreadsheets": "office_spreadsheets",
    "spreadsheet": "office_spreadsheets",
    "excel": "office_spreadsheets",
    "workbook": "office_spreadsheets",
    "xlsx": "office_spreadsheets",
    "xlsm": "office_spreadsheets",
    "csv": "office_spreadsheets",
    "tsv": "office_spreadsheets",
    "image-generation": "imagegen",
    "image_generation": "imagegen",
    "imagegen": "imagegen",
    "image": "imagegen",
    "tongxin": "tongxin_cli",
    "tongxin-cli": "tongxin_cli",
    "tongxin_cli": "tongxin_cli",
    "xin-agent": "tongxin_cli",
    "xin-agent-cli": "tongxin_cli",
    "xin_agent": "tongxin_cli",
    "xin_agent_cli": "tongxin_cli",
    "tx-assistant": "tongxin_cli",
    "芯助手": "tongxin_cli",
    "芯助手cli": "tongxin_cli",
    "通芯": "tongxin_cli",
    "通芯助手": "tongxin_cli",
}

_FRONTMATTER_TOOL_KEYS = (
    "callable-tool",
    "callable_tool",
    "tool",
    "tool-name",
    "tool_name",
)
_FRONTMATTER_ALIAS_KEYS = (
    "compatibility-id",
    "compatibility_id",
    "adopts-official-skill",
    "adopts_official_skill",
    "name",
)


def normalize_skill_tool_alias(value: Any) -> str:
    """Normalize a skill, capability, or tool alias into a stable lookup key."""

    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\\", "/").split("/")[-1]
    text = re.sub(r"\s+", "-", text)
    return text.lower()


def resolve_callable_tool_name(skill_or_name: Any) -> Optional[str]:
    """Return the canonical tool name for a skill/capability alias when known."""

    candidates: list[Any] = []
    frontmatter: Mapping[str, Any] = {}
    if isinstance(skill_or_name, str):
        candidates.append(skill_or_name)
    elif isinstance(skill_or_name, Mapping):
        candidates.extend([skill_or_name.get("name"), skill_or_name.get("displayName")])
        frontmatter = skill_or_name
    else:
        candidates.extend([
            getattr(skill_or_name, "name", None),
            getattr(skill_or_name, "display_name", None),
        ])
        raw_frontmatter = getattr(skill_or_name, "frontmatter", None)
        if isinstance(raw_frontmatter, Mapping):
            frontmatter = raw_frontmatter

    for key in _FRONTMATTER_TOOL_KEYS:
        value = frontmatter.get(key)
        if value:
            return str(value).strip()

    for key in _FRONTMATTER_ALIAS_KEYS:
        value = frontmatter.get(key)
        if value:
            candidates.append(value)

    for candidate in candidates:
        key = normalize_skill_tool_alias(candidate)
        if key in SKILL_CALLABLE_TOOL_ALIASES:
            return SKILL_CALLABLE_TOOL_ALIASES[key]
    return None


def skill_agent_surface(
    skill_or_name: Any,
    tool_names: Iterable[str],
    *,
    enabled: bool = True,
) -> dict[str, Any]:
    """Project whether a skill has a model-visible callable tool surface."""

    tool_name = resolve_callable_tool_name(skill_or_name)
    loaded_tools = {str(item) for item in (tool_names or [])}
    schema_visible = bool(tool_name and tool_name in loaded_tools)
    if not tool_name:
        return {
            "tool": "",
            "schemaVisible": False,
            "toolSchemaCallable": False,
            "callable": False,
            "status": "no_tool_mapping",
            "callableReason": "skill has no EcoreX callable tool mapping",
        }
    if not schema_visible:
        return {
            "tool": tool_name,
            "schemaVisible": False,
            "toolSchemaCallable": False,
            "callable": False,
            "status": "tool_not_loaded",
            "callableReason": "mapped tool is not loaded in the current agent snapshot",
        }
    return {
        "tool": tool_name,
        "schemaVisible": True,
        "toolSchemaCallable": True,
        "callable": bool(enabled),
        "status": "ready" if enabled else "disabled",
        "callableReason": (
            "mapped tool schema is visible to the model"
            if enabled
            else "skill is disabled"
        ),
    }
