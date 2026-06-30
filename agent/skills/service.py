"""
Skill service for handling skill CRUD operations.

This service provides a unified interface for managing skills, which can be
called from the cloud control client (LinkAI), the local web console, or any
other management entry point.
"""

import os
import re
import shutil
import zipfile
import tempfile
from typing import Dict, List, Optional
from common.log import logger
from agent.skills.types import Skill, SkillEntry
from agent.skills.manager import CUSTOM_OVERRIDE_MARKER, SkillManager

try:
    import requests
except ImportError:
    requests = None


_SAFE_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

SKILL_SOURCE_GROUP_LABELS = {
    "external": "外部",
    "custom": "自建",
    "builtin": "内置",
}

SKILL_PURPOSE_GROUP_LABELS = {
    "system": "系统能力",
    "office": "办公能力",
    "image_media": "图像 / 媒体",
    "collaboration": "协作连接",
    "data": "数据能力",
    "development": "开发能力",
    "automation": "自动化",
    "general": "通用能力",
}


def _normalize_skill_text(value) -> str:
    return str(value or "").strip().lower()


def _optional_bool(value) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = _normalize_skill_text(value)
    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return None


def _first_present(row: dict, *keys: str):
    for key in keys:
        if key in row and row.get(key) is not None:
            return row.get(key)
    return None


def _frontmatter_list_value(value) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _source_group_for(row: dict) -> str:
    builtin_catalog = _optional_bool(_first_present(
        row,
        "builtin_catalog",
        "builtinCatalog",
        "factory_builtin",
        "factoryBuiltin",
    ))
    if builtin_catalog is True:
        return "builtin"
    source = _normalize_skill_text(_first_present(row, "source", "origin", "source_group", "sourceGroup"))
    origin = _normalize_skill_text(row.get("origin"))
    if source == "builtin" or origin in {"builtin", "first-party", "factory"}:
        return "builtin"
    if source in {"custom", "workspace", "user", "user-skill"} or origin in {"workspace", "user"}:
        return "custom"
    return "external"


def _purpose_group_for(row: dict) -> str:
    explicit = _normalize_skill_text(_first_present(
        row,
        "purpose_group",
        "purpose-group",
        "purposeGroup",
        "category",
        "mention_category",
        "mention-category",
    )).replace("-", "_").replace(" ", "_")
    explicit_aliases = {
        "system": "system",
        "internal": "system",
        "tooling": "system",
        "background": "system",
        "office": "office",
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
        "image_media": "image_media",
        "collaboration": "collaboration",
        "connector": "collaboration",
        "lark": "collaboration",
        "feishu": "collaboration",
        "data": "data",
        "database": "data",
        "analytics": "data",
        "developer": "development",
        "development": "development",
        "dev": "development",
        "coding": "development",
        "github": "development",
        "automation": "automation",
        "browser": "automation",
        "workflow": "automation",
        "computer_use": "automation",
        "general": "general",
    }
    if explicit in explicit_aliases:
        return explicit_aliases[explicit]

    text = " ".join(
        _normalize_skill_text(row.get(key))
        for key in ("name", "display_name", "displayName", "description", "source", "origin", "path", "primary_env")
    )
    if re.search(r"lark|feishu|飞书|calendar|mail|approval|attendance|contact|wiki|base|minutes|okr|task|协作|日历|邮箱|审批", text):
        return "collaboration"
    if re.search(r"office|document|documents|pdf|spreadsheet|slides|presentation|docx|pptx|xlsx|xlsm|文档|表格|幻灯片|办公", text):
        return "office"
    if re.search(r"image|vision|media|video|audio|figma|hallmark|remotion|design|creative|生成|图像|图片|视觉|媒体|设计", text):
        return "image_media"
    if re.search(r"data|database|sql|csv|analytics|chart|dashboard|base|数据|分析|仪表盘", text):
        return "data"
    if re.search(r"github|openai|plugin|skill|codex|cli|developer|swift|xcode|debug|test|开发|调试|测试", text):
        return "development"
    if re.search(r"browser|chrome|automation|workflow|computer-use|自动化|浏览器", text):
        return "automation"
    if re.search(r"find|knowledge|memory|troubleshooting|a11y|system|系统|记忆|知识|检索|排障", text):
        return "system"
    return "general"


def _decorate_skill_governance(row: dict) -> None:
    source_group = _source_group_for(row)
    purpose_group = _purpose_group_for(row)
    is_builtin = source_group == "builtin"

    if is_builtin:
        row["enabled"] = True
        row["default_enabled"] = True

    row["builtin_catalog"] = is_builtin
    row["builtinCatalog"] = is_builtin
    row["source_group"] = source_group
    row["sourceGroup"] = source_group
    row["source_label"] = SKILL_SOURCE_GROUP_LABELS[source_group]
    row["sourceLabel"] = SKILL_SOURCE_GROUP_LABELS[source_group]
    row["purpose_group"] = purpose_group
    row["purposeGroup"] = purpose_group
    row["purpose_label"] = SKILL_PURPOSE_GROUP_LABELS[purpose_group]
    row["purposeLabel"] = SKILL_PURPOSE_GROUP_LABELS[purpose_group]
    row["toggleable"] = not is_builtin
    row["locked"] = is_builtin
    if is_builtin:
        row["lock_reason"] = "builtin-default-enabled"
        row["lockReason"] = "builtin-default-enabled"


def _is_lark_cli_skill(row: dict) -> bool:
    name = _normalize_skill_text(row.get("name") or row.get("display_name"))
    path = _normalize_skill_text(row.get("path") or row.get("file_path"))
    primary_env = _normalize_skill_text(row.get("primary_env"))
    description = _normalize_skill_text(row.get("description"))
    if re.match(r"^(lark|feishu)([-_:]|$)", name):
        return True
    if re.search(r"(^|[\\/])(lark|feishu)-[^\\/]+[\\/]skill\.md$", path):
        return True
    if primary_env.startswith(("lark_", "feishu_")):
        return True
    return "lark-cli" in description or "飞书" in description and "cli" in description


def _is_test_fixture_skill(row: dict) -> bool:
    name = _normalize_skill_text(row.get("name") or row.get("display_name"))
    path = _normalize_skill_text(row.get("path") or row.get("file_path"))
    return bool(re.match(r"^good-skill($|-)", name)) or "skill-format-check" in path


def _mention_category_for(row: dict) -> str:
    explicit = _normalize_skill_text(_first_present(row, "mention_category", "mention-category", "category")).replace("_", "-").replace(" ", "-")
    if explicit in {"creative", "creation", "content", "media", "design"}:
        return "creative"
    if explicit in {"document", "documents", "doc", "pdf", "office", "spreadsheet", "slides"}:
        return "document"
    if explicit in {"automation", "browser", "computer-use", "workflow"}:
        return "automation"
    if explicit in {"developer", "dev", "coding", "github", "figma", "macos"}:
        return "developer"
    if explicit in {"background", "cli", "system", "internal", "tooling", "connector"}:
        return "background"

    text = " ".join(
        _normalize_skill_text(row.get(key))
        for key in ("name", "display_name", "description", "source", "path", "primary_env")
    )
    if re.search(r"xiaohongshu|image|design|figma|hallmark|remotion|presentation|video|creative|生成|设计", text):
        return "creative"
    if re.search(r"document|documents|pdf|spreadsheet|slides|docx|pptx|xlsx|office|文档|表格|幻灯片", text):
        return "document"
    if re.search(r"browser|chrome|computer-use|automation|workflow|calendar|attendance|自动化|浏览器", text):
        return "automation"
    if re.search(r"github|build-macos|openai|plugin|skill|codex|cli|developer|swift|xcode|开发", text):
        return "developer"
    return "general"


def _decorate_mention_metadata(row: dict) -> None:
    category = _mention_category_for(row)
    explicit_mentionable = _optional_bool(_first_present(row, "mentionable", "mention-able"))
    explicit_hidden_reason = str(_first_present(row, "mention_hidden_reason", "mention-hidden-reason") or "").strip()
    mentionable = explicit_mentionable if explicit_mentionable is not None else category != "background"
    hidden_reason = explicit_hidden_reason

    if row.get("user_invocable") is False or row.get("disable_model_invocation") is True:
        mentionable = False
        hidden_reason = hidden_reason or "background-triggered"
    if explicit_mentionable is False:
        mentionable = False
        hidden_reason = hidden_reason or "background-triggered"
    if category == "background":
        mentionable = False
        hidden_reason = hidden_reason or "background-triggered"
    if _is_lark_cli_skill(row):
        if explicit_mentionable is False or row.get("user_invocable") is False or row.get("disable_model_invocation") is True:
            category = "background"
            mentionable = False
            hidden_reason = hidden_reason or "background-triggered"
        else:
            category = "automation"
            mentionable = True
            hidden_reason = ""
    if _is_test_fixture_skill(row):
        category = "background"
        mentionable = False
        hidden_reason = hidden_reason or "test-fixture"

    row["mentionable"] = bool(mentionable)
    row["mention_category"] = category if mentionable else "background"
    if hidden_reason:
        row["mention_hidden_reason"] = hidden_reason
    else:
        row.pop("mention_hidden_reason", None)


def _current_agent_tool_names() -> set[str]:
    """Return the current ToolManager schema snapshot without starting heavy probes."""

    try:
        from agent.tools.tool_manager import ToolManager

        manager = ToolManager()
        if not getattr(manager, "tool_classes", None):
            manager.load_tools(start_mcp=False)
        names = {str(name) for name in getattr(manager, "tool_classes", {}).keys()}
        names.update(str(name) for name in getattr(manager, "_mcp_tool_instances", {}).keys())
        return names
    except Exception as exc:
        logger.debug(f"[SkillService] tool snapshot unavailable: {exc}")
        return set()


def _skill_enabled(row: dict) -> bool:
    enabled = _optional_bool(row.get("enabled"))
    if enabled is not None:
        return enabled
    default_enabled = _optional_bool(row.get("default_enabled"))
    return True if default_enabled is None else bool(default_enabled)


def _decorate_tool_binding(row: dict, skill_or_name, tool_names: set[str]) -> None:
    try:
        from agent.skills.tool_binding_contract import skill_tool_binding_surface

        surface = skill_tool_binding_surface(skill_or_name, tool_names, enabled=_skill_enabled(row))
    except Exception as exc:
        logger.debug(f"[SkillService] tool binding unavailable for {row.get('name')}: {exc}")
        return
    row["toolName"] = surface.get("toolName") or surface.get("tool") or ""
    row["schemaVisible"] = bool(surface.get("schemaVisible"))
    row["toolSchemaCallable"] = bool(surface.get("toolSchemaCallable"))
    row["agentSurface"] = surface
    row["toolBinding"] = surface.get("toolBinding")
    row["tool_binding"] = surface.get("toolBinding")


class SkillService:
    """
    High-level service for skill lifecycle management.
    Wraps SkillManager and provides network-aware operations such as
    downloading skill files from remote URLs.
    """

    def __init__(self, skill_manager: SkillManager):
        """
        :param skill_manager: The SkillManager instance to operate on
        """
        self.manager = skill_manager

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------
    def query(self) -> List[dict]:
        """
        Query all skills and return a serialisable list.
        Reads from skills_config.json (refreshes from disk if needed).

        :return: list of skill info dicts
        """
        self.manager.refresh_skills()
        config = self.manager.get_skills_config()
        result = []
        tool_names = _current_agent_tool_names()
        for name, item in config.items():
            row = dict(item)
            is_builtin_catalog = bool(getattr(self.manager, "is_builtin_catalog_skill", lambda _: False)(name))
            if is_builtin_catalog:
                row["builtin_catalog"] = True
            entry = self.manager.skills.get(name)
            if entry:
                row["user_invocable"] = bool(entry.user_invocable)
                row["disable_model_invocation"] = bool(entry.skill.disable_model_invocation)
                row["path"] = entry.skill.file_path
                compatibility_id = _first_present(
                    entry.skill.frontmatter,
                    "compatibility-id",
                    "compatibility_id",
                )
                adopts_official_skill = _first_present(
                    entry.skill.frontmatter,
                    "adopts-official-skill",
                    "adopts_official_skill",
                )
                native_facade = _first_present(
                    entry.skill.frontmatter,
                    "ecorex-native-facade",
                    "ecorex_native_facade",
                )
                quality_gates = _frontmatter_list_value(_first_present(
                    entry.skill.frontmatter,
                    "quality-gates",
                    "quality_gates",
                ))
                if compatibility_id:
                    row["compatibility_id"] = str(compatibility_id)
                if adopts_official_skill:
                    row["adopts_official_skill"] = str(adopts_official_skill)
                if native_facade is not None:
                    row["ecorex_native_facade"] = _optional_bool(native_facade)
                if quality_gates:
                    row["quality_gates"] = quality_gates
                if entry.metadata:
                    row["always"] = bool(entry.metadata.always)
                    row["default_enabled"] = bool(entry.metadata.default_enabled)
                    row["primary_env"] = entry.metadata.primary_env
                    row["os"] = list(entry.metadata.os or [])
            _decorate_skill_governance(row)
            _decorate_mention_metadata(row)
            _decorate_tool_binding(row, entry.skill if entry else row, tool_names)
            result.append(row)
        logger.info(f"[SkillService] query: {len(result)} skills found")
        return result

    # ------------------------------------------------------------------
    # add / install
    # ------------------------------------------------------------------
    def add(self, payload: dict) -> None:
        """
        Add (install) a skill from a remote payload.

        Supported payload types:

        1. ``type: "url"`` – download individual files or write reviewed file contents::

            {
                "name": "web_search",
                "type": "url",
                "enabled": true,
                "files": [
                    {"url": "https://...", "path": "README.md"},
                    {"url": "https://...", "path": "scripts/main.py"},
                    {"path": "SKILL.md", "content": "---\\nname: custom\\n..."}
                ]
            }

        2. ``type: "package"`` – download a zip archive and extract::

            {
                "name": "plugin-custom-tool",
                "type": "package",
                "category": "skills",
                "enabled": true,
                "files": [{"url": "https://cdn.example.com/skills/custom-tool.zip"}]
            }

        :param payload: skill add payload from server
        """
        name = payload.get("name")
        if not name:
            raise ValueError("skill name is required")
        self._validate_skill_name(name)
        self._ensure_skill_mutation_allowed("add", payload)

        skill_dir = self._skill_dir(name)
        if os.path.exists(skill_dir) and not payload.get("replace"):
            raise ValueError(f"skill '{name}' already exists; set replace=true to overwrite")

        if self._is_builtin_skill(name) and not payload.get("allow_builtin_override"):
            raise ValueError(
                f"skill '{name}' is built in; explicit allow_builtin_override=true is required "
                "to install a workspace overlay"
            )

        payload_type = payload.get("type", "url")

        if payload_type == "package":
            self._add_package(name, payload)
        else:
            self._add_url(name, payload)

        if self._is_builtin_skill(name) and payload.get("allow_builtin_override"):
            self._write_custom_override_marker(name)

        self.manager.refresh_skills()

        category = payload.get("category")
        if category and name in self.manager.skills_config:
            self.manager.skills_config[name]["category"] = category
            self.manager._save_skills_config()

    def _add_url(self, name: str, payload: dict) -> None:
        """Install a skill by downloading individual files."""
        files = payload.get("files", [])
        if not files:
            raise ValueError("skill files list is empty")

        skill_dir = self._skill_dir(name)

        tmp_dir = skill_dir + ".tmp"
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        os.makedirs(tmp_dir, exist_ok=True)

        try:
            for file_info in files:
                url = file_info.get("url")
                content = file_info.get("content")
                rel_path = file_info.get("path")
                if not rel_path or (not url and content is None):
                    logger.warning(f"[SkillService] add: skip invalid file entry {file_info}")
                    continue
                dest = self._safe_join(tmp_dir, rel_path)
                if content is not None:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "w", encoding="utf-8") as handle:
                        handle.write(str(content))
                else:
                    self._download_file(url, dest)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        if os.path.exists(skill_dir):
            shutil.rmtree(skill_dir)
        os.rename(tmp_dir, skill_dir)

        logger.info(f"[SkillService] add: skill '{name}' installed via url ({len(files)} files)")

    def _add_package(self, name: str, payload: dict) -> None:
        """
        Install a skill by downloading a zip archive and extracting it.

        If the archive contains a single top-level directory, that directory
        is used as the skill folder directly; otherwise a new directory named
        after the skill is created to hold the extracted contents.
        """
        files = payload.get("files", [])
        if not files or not files[0].get("url"):
            raise ValueError("package url is required")

        url = files[0]["url"]
        skill_dir = self._skill_dir(name)

        with tempfile.TemporaryDirectory() as tmp_dir:
            zip_path = os.path.join(tmp_dir, "package.zip")
            self._download_file(url, zip_path)

            if not zipfile.is_zipfile(zip_path):
                raise ValueError(f"downloaded file is not a valid zip archive: {url}")

            extract_dir = os.path.join(tmp_dir, "extracted")
            with zipfile.ZipFile(zip_path, "r") as zf:
                self._safe_extract_zip(zf, extract_dir)

            # Determine the actual content root.
            # If the zip has a single top-level directory, use its contents
            # so the skill folder is clean (no extra nesting).
            top_items = [
                item for item in os.listdir(extract_dir)
                if not item.startswith(".")
            ]
            if len(top_items) == 1:
                single = os.path.join(extract_dir, top_items[0])
                if os.path.isdir(single):
                    extract_dir = single

            if os.path.exists(skill_dir):
                shutil.rmtree(skill_dir)
            shutil.copytree(extract_dir, skill_dir)

        logger.info(f"[SkillService] add: skill '{name}' installed via package ({url})")

    # ------------------------------------------------------------------
    # open / close (enable / disable)
    # ------------------------------------------------------------------
    def open(self, payload: dict) -> None:
        """
        Enable a skill by name.

        :param payload: {"name": "skill_name"}
        """
        name = payload.get("name")
        if not name:
            raise ValueError("skill name is required")
        self._validate_skill_name(name)
        self._ensure_skill_mutation_allowed("open", payload)
        self.manager.set_skill_enabled(name, enabled=True)
        logger.info(f"[SkillService] open: skill '{name}' enabled")

    def close(self, payload: dict) -> None:
        """
        Disable a skill by name.

        :param payload: {"name": "skill_name"}
        """
        name = payload.get("name")
        if not name:
            raise ValueError("skill name is required")
        self._validate_skill_name(name)
        if self._skill_source_group(name) == "builtin":
            raise PermissionError("Built-in factory skills are always enabled and cannot be disabled.")
        self._ensure_skill_mutation_allowed("close", payload)
        self.manager.set_skill_enabled(name, enabled=False)
        logger.info(f"[SkillService] close: skill '{name}' disabled")

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------
    def delete(self, payload: dict) -> None:
        """
        Delete a skill by removing its directory entirely.

        :param payload: {"name": "skill_name"}
        """
        name = payload.get("name")
        if not name:
            raise ValueError("skill name is required")
        self._validate_skill_name(name)
        self._ensure_skill_mutation_allowed("delete", payload)

        skill_dir = self._skill_dir(name)
        if os.path.exists(skill_dir):
            shutil.rmtree(skill_dir)
            logger.info(f"[SkillService] delete: removed directory {skill_dir}")
        else:
            logger.warning(f"[SkillService] delete: skill directory not found: {skill_dir}")

        # Refresh will remove the deleted skill from config automatically
        self.manager.refresh_skills()
        logger.info(f"[SkillService] delete: skill '{name}' deleted")

    # ------------------------------------------------------------------
    # dispatch - single entry point for protocol messages
    # ------------------------------------------------------------------
    def dispatch(self, action: str, payload: Optional[dict] = None) -> dict:
        """
        Dispatch a skill management action and return a protocol-compatible
        response dict.

        :param action: one of query / add / open / close / delete
        :param payload: action-specific payload (may be None for query)
        :return: dict with action, code, message, payload
        """
        payload = payload or {}
        try:
            if action == "query":
                result_payload = self.query()
                return {"action": action, "code": 200, "message": "success", "payload": result_payload}
            elif action == "add":
                self.add(payload)
            elif action == "open":
                self.open(payload)
            elif action == "close":
                self.close(payload)
            elif action == "delete":
                self.delete(payload)
            else:
                return {"action": action, "code": 400, "message": f"unknown action: {action}", "payload": None}
            return {"action": action, "code": 200, "message": "success", "payload": None}
        except Exception as e:
            logger.error(f"[SkillService] dispatch error: action={action}, error={e}")
            return {"action": action, "code": 500, "message": str(e), "payload": None}

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_skill_name(name: str):
        if not isinstance(name, str) or not _SAFE_SKILL_NAME.match(name):
            raise ValueError("invalid skill name")
        if name in {".", ".."} or os.sep in name or (os.altsep and os.altsep in name):
            raise ValueError("invalid skill name")
        if name.endswith((".", " ")):
            raise ValueError("invalid skill name")
        first_component = name.split(".", 1)[0].upper()
        if first_component in _WINDOWS_RESERVED_NAMES:
            raise ValueError("invalid skill name")

    def _skill_dir(self, name: str) -> str:
        self._validate_skill_name(name)
        return self._safe_join(self.manager.custom_dir, name)

    def _is_builtin_skill(self, name: str) -> bool:
        try:
            if self.manager.is_builtin_catalog_skill(name):
                return True
        except Exception:
            pass
        try:
            candidate = os.path.abspath(os.path.join(self.manager.builtin_dir, name))
            root = os.path.abspath(self.manager.builtin_dir)
            return os.path.commonpath([root, candidate]) == root and os.path.isdir(candidate)
        except Exception:
            return False

    def _skill_source_group(self, name: str) -> str:
        try:
            builtin_catalog = bool(self.manager.is_builtin_catalog_skill(name))
        except Exception:
            builtin_catalog = False
        entry = self.manager.skills.get(name)
        if entry:
            return _source_group_for({
                "source": entry.skill.source,
                "origin": entry.skill.source,
                "builtin_catalog": builtin_catalog,
            })
        config_row = self.manager.skills_config.get(name, {})
        if builtin_catalog:
            config_row = {**config_row, "builtin_catalog": True}
        return _source_group_for(config_row)

    def _write_custom_override_marker(self, name: str) -> None:
        marker_path = os.path.join(self._skill_dir(name), CUSTOM_OVERRIDE_MARKER)
        try:
            with open(marker_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "This workspace skill intentionally overrides an EcoreX built-in skill.\n"
                    "Remove this file to allow managed built-in refresh on future releases.\n"
                )
        except Exception as exc:
            logger.warning(f"[SkillService] Failed to write override marker for '{name}': {exc}")

    def _ensure_skill_mutation_allowed(self, action: str, payload: dict) -> None:
        if payload.get("_permission_checked") is True:
            return
        try:
            from common.ecorex_tool_permissions import get_tool_permission_broker

            decision = get_tool_permission_broker().authorize_noninteractive(
                "skill_write",
                {
                    "action": action,
                    "name": payload.get("name"),
                    "type": payload.get("type"),
                    "category": payload.get("category"),
                },
            )
            if not decision.get("allowed", False):
                raise PermissionError(
                    decision.get("reason")
                    or "Current permission mode blocks skill modifications."
                )
        except PermissionError:
            raise
        except Exception as exc:
            logger.warning(f"[SkillService] permission broker unavailable; skill mutation blocked: {exc}")
            raise PermissionError("Permission broker unavailable; skill modification blocked.")

    @staticmethod
    def _safe_join(root: str, relative_path: str) -> str:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValueError("invalid relative path")
        normalized = relative_path.replace("\\", os.sep).replace("/", os.sep)
        if os.path.isabs(normalized):
            raise ValueError("absolute paths are not allowed")
        parts = [part for part in normalized.split(os.sep) if part]
        if any(part in {".", ".."} for part in parts):
            raise ValueError("path traversal is not allowed")

        root_abs = os.path.abspath(root)
        candidate = os.path.abspath(os.path.join(root_abs, *parts))
        try:
            common = os.path.commonpath([root_abs, candidate])
        except ValueError:
            raise ValueError("path escapes skill root")
        if common != root_abs:
            raise ValueError("path escapes skill root")
        return candidate

    def _safe_extract_zip(self, zf: zipfile.ZipFile, extract_dir: str):
        os.makedirs(extract_dir, exist_ok=True)
        for member in zf.infolist():
            member_path = member.filename.replace("\\", "/")
            if not member_path or member_path.endswith("/"):
                continue
            if member_path.startswith("/") or member_path.startswith("\\"):
                raise ValueError("zip archive contains an absolute path")
            dest = self._safe_join(extract_dir, member_path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(member, "r") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)

    @staticmethod
    def _download_file(url: str, dest: str):
        """
        Download a file from *url* and save to *dest*.

        :param url: remote file URL
        :param dest: local destination path
        """
        if requests is None:
            raise RuntimeError("requests library is required for downloading skill files")

        dest_dir = os.path.dirname(dest)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)

        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            f.write(resp.content)
        logger.debug(f"[SkillService] downloaded {url} -> {dest}")
