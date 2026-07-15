"""Stable, backend-owned presentation taxonomy for extension catalogs.

The v0.3 skill console derived these groups from skill metadata.  v1 keeps the
same user-facing taxonomy, but computes it once in the Runtime so the WebUI is
only a projection and cannot drift from governance state.
"""

from __future__ import annotations

from collections.abc import Iterable
import re


EXTENSION_CATEGORIES = (
    "system",
    "office",
    "image_media",
    "collaboration",
    "data",
    "development",
    "automation",
    "general",
)


def extension_category(
    *,
    extension_id: str,
    display_name: str,
    description: str,
    export_ids: Iterable[str],
    explicit_category: str | None = None,
    core_bundle: bool = False,
) -> str:
    """Return the v0.3-compatible purpose group for a projected extension."""

    explicit = _normalized_category(explicit_category)
    if explicit is not None:
        return explicit
    if extension_id == "ecorex.core.tools":
        return "system"
    if extension_id == "ecorex.core.connectors":
        return "collaboration"
    text = " ".join(
        (
            extension_id,
            display_name,
            description,
            *tuple(export_ids),
        )
    ).casefold()
    if re.search(
        r"lark|feishu|飞书|tencent|腾讯|calendar|mail|approval|attendance|contact|wiki|base|minutes|okr|task|connector|协作|日历|邮箱|审批",
        text,
    ):
        return "collaboration"
    if re.search(
        r"office|document|documents|pdf|spreadsheet|slides|presentation|docx|pptx|xlsx|xlsm|文档|表格|幻灯片|办公",
        text,
    ):
        return "office"
    if re.search(
        r"image|vision|media|video|audio|figma|hallmark|remotion|design|creative|生成|图像|图片|视觉|媒体|设计",
        text,
    ):
        return "image_media"
    if re.search(r"data|database|sql|csv|analytics|chart|dashboard|数据|分析|仪表盘", text):
        return "data"
    if re.search(
        r"github|openai|plugin|skill|codex|cli|developer|swift|xcode|debug|test|开发|调试|测试",
        text,
    ):
        return "development"
    if re.search(r"browser|chrome|playwright|automation|workflow|computer-use|自动化|浏览器", text):
        return "automation"
    if core_bundle or re.search(
        r"find|knowledge|memory|troubleshooting|a11y|system|系统|记忆|知识|检索|排障",
        text,
    ):
        return "system"
    return "general"


def extension_icon_key(*, extension_id: str, category: str) -> str:
    """Return a safe icon token; the UI never receives a filesystem or URL path."""

    value = extension_id.casefold()
    for pattern, icon_key in (
        (r"lark|feishu", "feishu"),
        (r"tencent|qq", "tencent"),
        (r"github", "github"),
        (r"chrome|browser|playwright", "browser"),
        (r"image|vision", "image"),
        (r"office|document|pdf|spreadsheet|slides", "document"),
    ):
        if re.search(pattern, value):
            return icon_key
    return category


def _normalized_category(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "internal": "system",
        "tooling": "system",
        "background": "system",
        "document": "office",
        "documents": "office",
        "doc": "office",
        "pdf": "office",
        "spreadsheet": "office",
        "slides": "office",
        "presentation": "office",
        "creative": "image_media",
        "creation": "image_media",
        "content": "image_media",
        "media": "image_media",
        "design": "image_media",
        "image": "image_media",
        "connector": "collaboration",
        "lark": "collaboration",
        "feishu": "collaboration",
        "database": "data",
        "analytics": "data",
        "developer": "development",
        "dev": "development",
        "coding": "development",
        "github": "development",
        "browser": "automation",
        "workflow": "automation",
        "computer_use": "automation",
    }
    candidate = aliases.get(normalized, normalized)
    return candidate if candidate in EXTENSION_CATEGORIES else None


__all__ = ["EXTENSION_CATEGORIES", "extension_category", "extension_icon_key"]
