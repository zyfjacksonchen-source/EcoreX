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
from channel.channel_catalog import (
    CHANNEL_CATALOG,
    active_channel_set,
    channel_config_refs,
    channel_observability,
)


ExtensionEntry = Dict[str, Any]


class ExtensionRegistry:
    """Collect skills, optional abilities, connectors, and MCP status."""

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = workspace_root or self._default_workspace_root()

    def list_extensions(self) -> Dict[str, Any]:
        entries: List[ExtensionEntry] = []
        entries.extend(self._first_party_tools())
        entries.extend(self._skills())
        entries.extend(self._channels())
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
                "channels": "runtime-config-read-only-discovery",
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

    @staticmethod
    def _agent_tool_names() -> set:
        try:
            from agent.tools.tool_manager import ToolManager

            manager = ToolManager()
            if not getattr(manager, "tool_classes", None):
                manager.load_tools()
            names = {str(name) for name in getattr(manager, "tool_classes", {}).keys()}
            names.update(str(name) for name in getattr(manager, "_mcp_tool_instances", {}).keys())
            return names
        except Exception as exc:
            logger.debug(f"[ExtensionRegistry] agent tool snapshot unavailable: {exc}")
            return set()

    def _first_party_tools(self) -> List[ExtensionEntry]:
        try:
            from agent.tools.tool_manager import ToolManager

            manager = ToolManager()
            if not getattr(manager, "tool_classes", None):
                manager.load_tools()
            first_party = {str(name) for name in getattr(manager, "tool_classes", {}).keys()}
            tool_infos = manager.list_tools()
            entries: List[ExtensionEntry] = []
            for name in sorted(first_party):
                info = tool_infos.get(name) if isinstance(tool_infos, dict) else {}
                if not isinstance(info, dict):
                    info = {}
                entries.append({
                    "id": f"tool:{name}",
                    "type": "builtin_tool",
                    "displayName": name,
                    "description": info.get("description") or "",
                    "origin": "first-party",
                    "enabled": True,
                    "installed": True,
                    "policy": "built-in",
                    "permissions": ["agent-tool-schema"],
                    "requires": [],
                    "provides": ["tool", name],
                    "configRefs": [{"path": "config.tools", "key": name}],
                    "status": "ready",
                    "toolName": name,
                    "schemaVisible": True,
                    "toolSchemaCallable": True,
                })
            return entries
        except Exception as exc:
            logger.warning(f"[ExtensionRegistry] first-party tool aggregation failed: {exc}")
            return [{
                "id": "tools",
                "type": "builtin_tool",
                "displayName": "Built-in tools",
                "description": "Built-in tool registry unavailable",
                "origin": "first-party",
                "enabled": False,
                "installed": False,
                "status": "error",
                "lastError": str(exc),
            }]

    def _skills(self) -> List[ExtensionEntry]:
        try:
            from agent.skills.manager import SkillManager
            from agent.skills.service import _decorate_mention_metadata, _decorate_skill_governance
            from agent.skills.tool_bridge import resolve_callable_tool_name, skill_agent_surface

            custom_dir = Path(self.workspace_root) / "skills"
            manager = SkillManager(custom_dir=str(custom_dir))
            skills = manager.skills
            saved = manager.get_skills_config()
            agent_tool_names = self._agent_tool_names()
            entries: List[ExtensionEntry] = []
            for name, skill_entry in sorted(skills.items()):
                skill = skill_entry.skill if skill_entry else None
                previous = saved.get(name, {})
                metadata = getattr(skill_entry, "metadata", None)
                default_enabled = getattr(metadata, "default_enabled", True)
                source = str(getattr(skill, "source", "") or previous.get("source") or "custom")
                is_builtin_catalog = bool(getattr(manager, "is_builtin_catalog_skill", lambda _: False)(name))
                origin = "builtin" if source == "builtin" else "workspace" if source == "custom" else "global"
                row = {
                    **previous,
                    "name": name,
                    "display_name": previous.get("display_name") or name,
                    "description": previous.get("description") or getattr(skill, "description", ""),
                    "source": source,
                    "origin": origin,
                    "path": getattr(skill, "file_path", "") if skill else "",
                    "default_enabled": default_enabled,
                    "builtin_catalog": is_builtin_catalog,
                    "primary_env": getattr(metadata, "primary_env", None) if metadata else None,
                    "user_invocable": bool(getattr(skill_entry, "user_invocable", True)),
                    "disable_model_invocation": bool(getattr(skill, "disable_model_invocation", False)),
                }
                _decorate_skill_governance(row)
                _decorate_mention_metadata(row)
                kind = "builtin_skill" if row.get("source_group") == "builtin" else "user_skill"
                enabled = bool(row.get("enabled", previous.get("enabled", default_enabled)))
                policy = "built-in-locked" if row.get("source_group") == "builtin" else "user-overlay" if source == "custom" else "global-skill"
                callable_tool = resolve_callable_tool_name(skill) if skill else resolve_callable_tool_name(name)
                agent_surface = skill_agent_surface(skill or name, agent_tool_names, enabled=enabled)
                entries.append({
                    "id": f"skill:{name}",
                    "type": kind,
                    "displayName": previous.get("display_name") or name,
                    "description": previous.get("description") or getattr(skill, "description", ""),
                    "origin": origin,
                    "source": source,
                    "sourceGroup": row.get("sourceGroup"),
                    "source_group": row.get("source_group"),
                    "sourceLabel": row.get("sourceLabel"),
                    "source_label": row.get("source_label"),
                    "builtinCatalog": bool(row.get("builtinCatalog")),
                    "builtin_catalog": bool(row.get("builtin_catalog")),
                    "purposeGroup": row.get("purposeGroup"),
                    "purpose_group": row.get("purpose_group"),
                    "purposeLabel": row.get("purposeLabel"),
                    "purpose_label": row.get("purpose_label"),
                    "sourcePath": self._normalize_path(getattr(skill, "base_dir", "")) if skill else "",
                    "enabled": enabled,
                    "defaultEnabled": bool(row.get("default_enabled", default_enabled)),
                    "default_enabled": bool(row.get("default_enabled", default_enabled)),
                    "installed": True,
                    "policy": policy,
                    "toggleable": bool(row.get("toggleable", True)),
                    "locked": bool(row.get("locked", False)),
                    "lockReason": row.get("lockReason"),
                    "lock_reason": row.get("lock_reason"),
                    "requires": getattr(metadata, "requires", {}) if metadata else {},
                    "provides": ["skill"] + ([f"tool:{callable_tool}"] if callable_tool else []),
                    "configRefs": [{"path": "skills_config.json", "key": name}],
                    "status": "ready" if enabled else "disabled",
                    "toolName": callable_tool or "",
                    "schemaVisible": bool(agent_surface.get("schemaVisible")),
                    "toolSchemaCallable": bool(agent_surface.get("toolSchemaCallable")),
                    "agentSurface": agent_surface,
                    "category": row.get("category"),
                    "primary_env": row.get("primary_env"),
                    "user_invocable": row.get("user_invocable"),
                    "disable_model_invocation": row.get("disable_model_invocation"),
                    "mentionable": row.get("mentionable"),
                    "mention_category": row.get("mention_category"),
                    "mention_hidden_reason": row.get("mention_hidden_reason"),
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

    def _channels(self) -> List[ExtensionEntry]:
        try:
            from config import conf

            local_config = conf()
            active = active_channel_set(local_config)
            agent_tool_names = self._agent_tool_names()
            entries: List[ExtensionEntry] = []
            for name, definition in CHANNEL_CATALOG.items():
                enabled = name in active
                observed = channel_observability(
                    local_config,
                    name,
                    tool_names=agent_tool_names,
                )
                label = definition.get("label") if isinstance(definition.get("label"), dict) else {}
                display_name = str(label.get("zh") or label.get("en") or name)
                entries.append({
                    "id": f"channel:{name}",
                    "type": "connector",
                    "displayName": display_name,
                    "description": definition.get("description") or "",
                    "origin": "channel",
                    "enabled": enabled,
                    "installed": True,
                    "policy": "runtime-config",
                    "permissions": ["user-configured-credentials"],
                    "requires": [
                        str(field.get("key") or "")
                        for field in definition.get("fields", [])
                        if field.get("key")
                    ],
                    "provides": definition.get("provides") or ["channel"],
                    "configRefs": channel_config_refs(name),
                    "status": observed["status"],
                    "aliases": definition.get("aliases") or [],
                    "channelName": name,
                    "userConfigurable": True,
                    "active": observed["active"],
                    "configured": observed["configured"],
                    "running": observed["running"],
                    "configState": observed["configState"],
                    "auth": observed["auth"],
                    "agentSurface": observed["agentSurface"],
                })
            return entries
        except Exception as exc:
            logger.warning(f"[ExtensionRegistry] channel aggregation failed: {exc}")
            return [{
                "id": "channels",
                "type": "connector",
                "displayName": "Channels",
                "description": "Channel registry unavailable",
                "origin": "channel",
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
                policy_mode = str(item.get("policyMode") or item.get("defaultPolicy") or "discoverable")
                install_allowed = item.get("installAllowed")
                if install_allowed is None:
                    install_allowed = policy_mode != "disabled"
                status = state.get("state") or ("enabled" if item.get("enabled") else "available")
                if policy_mode == "disabled" and not installed:
                    status = "disabled"
                entries.append({
                    "id": f"ability:{ability_id}",
                    "type": entry_type,
                    "displayName": item.get("label") or ability_id,
                    "description": item.get("notes") or item.get("defaultPolicy") or "",
                    "origin": "runtime-pack" if item.get("packId") else "runtime",
                    "sourceUrl": item.get("sourceUrl"),
                    "enabled": bool(item.get("enabled")),
                    "installed": installed,
                    "policy": policy_mode,
                    "policyMode": policy_mode,
                    "installAllowed": bool(install_allowed),
                    "disabledReason": item.get("disabledReason") or "",
                    "policyUpdatedAt": item.get("policyUpdatedAt") or "",
                    "policySource": item.get("policySource") or "",
                    "permissions": ["user-confirm-before-install"] if install_allowed else [],
                    "requires": item.get("missingModules") or [],
                    "provides": [kind or entry_type],
                    "configRefs": item.get("configPath") or item.get("configKey") or [],
                    "status": status,
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
