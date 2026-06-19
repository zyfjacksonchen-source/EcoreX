"""Unified discovery surface for EcoreX runtime extensions.

The registry intentionally starts as a lightweight read-only aggregator. It
does not install packages, copy built-in skills, or reload MCP servers during
startup; callers can use the returned policy/action metadata to route explicit
user actions through the existing managers.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.log import logger


ExtensionEntry = Dict[str, Any]


class ExtensionRegistry:
    """Collect skills, optional abilities, connectors, and MCP status."""

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = workspace_root or self._default_workspace_root()

    def list_extensions(self) -> Dict[str, Any]:
        entries: List[ExtensionEntry] = []
        entries.extend(self._skills())
        entries.extend(self._optional_abilities())
        entries.extend(self._mcp_servers())
        entries = self._dedupe(entries)
        return {
            "status": "success",
            "extensions": entries,
            "count": len(entries),
            "summary": self._summary(entries),
            "policy": {
                "builtinSkills": "load-from-catalog",
                "workspaceSkills": "explicit-user-overlays",
                "feishuLark": "find-skill-first-on-demand-cli",
                "mcp": "discoverable-disabled-by-default",
            },
        }

    @staticmethod
    def _default_workspace_root() -> str:
        try:
            from common.utils import expand_path
            from config import conf

            return expand_path(conf().get("agent_workspace", "~/EcoreX"))
        except Exception:
            return os.path.expanduser("~/EcoreX")

    @staticmethod
    def _normalize_path(value: Any) -> str:
        try:
            return str(Path(str(value)).expanduser())
        except Exception:
            return str(value or "")

    def _skills(self) -> List[ExtensionEntry]:
        try:
            from agent.skills.loader import SkillLoader
            from agent.skills.manager import SKILLS_CONFIG_FILE

            project_root = Path(__file__).resolve().parents[2]
            builtin_dir = project_root / "skills"
            custom_dir = Path(self.workspace_root) / "skills"
            loader = SkillLoader()
            skills = loader.load_all_skills(
                builtin_dir=str(builtin_dir),
                custom_dir=str(custom_dir),
            )
            config_path = custom_dir / SKILLS_CONFIG_FILE
            saved = self._read_json_dict(config_path)
            entries: List[ExtensionEntry] = []
            for name, skill_entry in sorted(skills.items()):
                skill = skill_entry.skill if skill_entry else None
                previous = saved.get(name, {})
                metadata = getattr(skill_entry, "metadata", None)
                default_enabled = getattr(metadata, "default_enabled", True)
                source = str(previous.get("source") or getattr(skill, "source", "") or "custom")
                kind = "builtin_skill" if source == "builtin" else "user_skill"
                entries.append({
                    "id": f"skill:{name}",
                    "type": kind,
                    "displayName": previous.get("display_name") or name,
                    "description": previous.get("description") or getattr(skill, "description", ""),
                    "origin": "builtin" if source == "builtin" else "workspace",
                    "sourcePath": self._normalize_path(getattr(skill, "base_dir", "")) if skill else "",
                    "enabled": bool(previous.get("enabled", default_enabled)),
                    "installed": True,
                    "policy": "catalog" if source == "builtin" else "user-overlay",
                    "requires": getattr(metadata, "requires", {}) if metadata else {},
                    "provides": ["skill"],
                    "configRefs": [{"path": "skills_config.json", "key": name}],
                    "status": "ready" if previous.get("enabled", default_enabled) else "disabled",
                })
            return entries
        except Exception as exc:
            logger.warning(f"[ExtensionRegistry] skill aggregation failed: {exc}")
            return [{
                "id": "skills",
                "type": "builtin_skill",
                "displayName": "Skills",
                "description": "Skill registry unavailable",
                "origin": "runtime",
                "enabled": False,
                "installed": False,
                "status": "error",
                "lastError": str(exc),
            }]

    @staticmethod
    def _read_json_dict(path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning(f"[ExtensionRegistry] failed reading {path}: {exc}")
            return {}

    def _optional_abilities(self) -> List[ExtensionEntry]:
        try:
            from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

            result = OptionalAbilities().execute({"action": "list"})
            payload = getattr(result, "result", result)
            abilities = payload.get("abilities", []) if isinstance(payload, dict) else []
            entries: List[ExtensionEntry] = []
            for item in abilities:
                if not isinstance(item, dict):
                    continue
                ability_id = str(item.get("id") or "").strip()
                if not ability_id:
                    continue
                kind = str(item.get("kind") or "")
                entry_type = "capability_pack" if kind == "capability-pack" else "plugin"
                if ability_id in {"feishu-cli", "feishu-lark"} or item.get("packId") == "feishu-lark":
                    entry_type = "connector"
                elif ability_id.endswith("-mcp") or "mcp" in ability_id:
                    entry_type = "mcp_server"
                state = item.get("capabilityState") if isinstance(item.get("capabilityState"), dict) else {}
                installed = bool(state.get("installed")) if state else bool(item.get("enabled"))
                entries.append({
                    "id": f"ability:{ability_id}",
                    "type": entry_type,
                    "displayName": item.get("label") or ability_id,
                    "description": item.get("notes") or item.get("defaultPolicy") or "",
                    "origin": "runtime-pack" if item.get("packId") else "runtime",
                    "sourceUrl": item.get("sourceUrl"),
                    "enabled": bool(item.get("enabled")),
                    "installed": installed,
                    "policy": item.get("defaultPolicy") or "discoverable",
                    "permissions": ["user-confirm-before-install"],
                    "requires": item.get("missingModules") or [],
                    "provides": [kind or entry_type],
                    "configRefs": item.get("configPath") or item.get("configKey") or [],
                    "status": state.get("state") or ("enabled" if item.get("enabled") else "available"),
                    "lastError": state.get("message") if state.get("state") == "failed" else None,
                    "installHint": item.get("installHint"),
                    "mirrorUrls": item.get("mirrorUrls"),
                })
            return entries
        except Exception as exc:
            logger.warning(f"[ExtensionRegistry] ability aggregation failed: {exc}")
            return [{
                "id": "abilities",
                "type": "capability_pack",
                "displayName": "Optional abilities",
                "description": "Optional ability registry unavailable",
                "origin": "runtime",
                "enabled": False,
                "installed": False,
                "status": "error",
                "lastError": str(exc),
            }]

    def _mcp_servers(self) -> List[ExtensionEntry]:
        try:
            from agent.tools.tool_manager import ToolManager
            from config import conf

            manager = ToolManager()
            status = manager.list_mcp_status()
            configured: Dict[str, Dict[str, Any]] = {}
            for server in conf().get("mcp_servers", []) or []:
                if not isinstance(server, dict):
                    continue
                name = str(server.get("name") or "").strip()
                if name:
                    configured[name] = server
            entries: List[ExtensionEntry] = []
            for name in sorted(set(configured) | set(status)):
                state = status.get(name) or ("configured-disabled" if not conf().get("mcp_auto_start", False) else "configured")
                server = configured.get(name, {})
                entries.append({
                    "id": f"mcp:{name}",
                    "type": "mcp_server",
                    "displayName": name,
                    "description": str(server.get("description") or server.get("command") or server.get("url") or "MCP server"),
                    "origin": "mcp",
                    "enabled": state in {"pending", "ready"},
                    "installed": state == "ready",
                    "policy": "runtime-config",
                    "configRefs": [{"path": "config.mcp_servers", "key": name}],
                    "provides": ["mcp-tools"],
                    "status": state,
                })
            return entries
        except Exception as exc:
            logger.warning(f"[ExtensionRegistry] MCP aggregation failed: {exc}")
            return []

    @staticmethod
    def _dedupe(entries: List[ExtensionEntry]) -> List[ExtensionEntry]:
        deduped: Dict[str, ExtensionEntry] = {}
        for entry in entries:
            key = str(entry.get("id") or "")
            if not key:
                continue
            clean = {k: v for k, v in entry.items() if v is not None}
            deduped[key] = {**deduped.get(key, {}), **clean}
        return [deduped[key] for key in sorted(deduped)]

    @staticmethod
    def _summary(entries: List[ExtensionEntry]) -> Dict[str, int]:
        summary: Dict[str, int] = {}
        for entry in entries:
            etype = str(entry.get("type") or "unknown")
            summary[etype] = summary.get(etype, 0) + 1
        return summary
