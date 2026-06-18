from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from agent.tools.base_tool import BaseTool, ToolResult
from agent.tools.optional_abilities.optional_abilities import OptionalAbilities
from common.log import logger
from config import conf


def _workspace_for(tool: BaseTool) -> str:
    agent = getattr(tool, "context", None)
    workspace = getattr(agent, "workspace_dir", None) or conf().get("agent_workspace", "~/cow")
    return os.path.expanduser(str(workspace))


def _skill_service(workspace: str):
    from agent.skills.manager import SkillManager
    from agent.skills.service import SkillService

    manager = SkillManager(custom_dir=os.path.join(workspace, "skills"))
    return SkillService(manager)


def _config_path() -> Path:
    root = Path(__file__).resolve().parents[3]
    return root / "config.json"


def _mcp_json_path(workspace: str) -> Path:
    return Path(workspace).expanduser().resolve() / "mcp.json"


def _read_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_file(path: Path, data: Dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def _write_config(data: Dict[str, Any]) -> str:
    path = _config_path()
    written = _write_json_file(path, data)
    live = conf()
    for key, value in data.items():
        live[key] = value
    return written


_PACK_ALIASES = {
    "feishu": "feishu-lark",
    "lark": "feishu-lark",
    "feishu-lark": "feishu-lark",
    "lark-feishu": "feishu-lark",
    "feishu_cli": "feishu-cli",
    "feishu-cli": "feishu-cli",
    "lark-cli": "feishu-cli",
}


def _normalize_pack_id(value: str) -> str:
    key = str(value or "").strip().lower().replace("_", "-")
    return _PACK_ALIASES.get(key, key)


def _payload_for_result(result: ToolResult) -> Dict[str, Any]:
    payload = result.result if isinstance(result.result, dict) else {"result": result.result}
    return {"status": result.status or payload.get("status") or "success", **payload}


def _tail(value: Any, limit: int = 1200) -> str:
    text = str(value or "")
    return text[-limit:] if len(text) > limit else text


def _compact_install_step(ability: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    state = payload.get("capabilityState") if isinstance(payload.get("capabilityState"), dict) else {}
    return {
        "ability": ability,
        "status": payload.get("status") or "success",
        "message": payload.get("message") or state.get("message") or "",
        "exitCode": payload.get("exitCode"),
        "configPath": payload.get("configPath"),
        "logPath": payload.get("logPath") or state.get("logPath"),
        "stdoutTail": _tail(payload.get("stdout") or payload.get("output")),
        "stderrTail": _tail(payload.get("stderr")),
        "enabled": payload.get("enabled"),
        "installed": state.get("installed"),
        "state": state.get("state"),
    }


class AgentCapabilityTool(BaseTool):
    name = "agent_capability"
    description = (
        "Let the agent diagnose, install, enable, disable, and reload EcoreX skills, MCP servers, "
        "and optional capability packs. Use this instead of UI-side silent installation. "
        "Do not ask the user to type consent such as '同意安装'; call this tool and let the "
        "EcoreX permission broker surface the confirmation."
    )
    params = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: list_packs, diagnose, install_pack, install_skill, enable_skill, disable_skill, configure_mcp, reload_mcp.",
            },
            "pack_id": {"type": "string", "description": "Capability pack or optional ability id."},
            "skill": {"type": "string", "description": "Skill name."},
            "url": {"type": "string", "description": "Skill package/repo URL for install_skill."},
            "files": {"type": "array", "description": "Optional skill file entries for install_skill."},
            "server": {"type": "object", "description": "MCP server config for configure_mcp."},
            "timeout": {"type": "integer", "description": "Install timeout seconds."},
        },
        "required": ["action"],
    }

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        action = str(params.get("action") or "").strip().lower()
        workspace = _workspace_for(self)
        try:
            if action == "list_packs":
                return OptionalAbilities().execute({"action": "list"})
            if action == "diagnose":
                return self._diagnose(workspace)
            if action == "install_pack":
                pack_id = _normalize_pack_id(str(params.get("pack_id") or params.get("ability") or ""))
                if not pack_id:
                    return ToolResult.fail({"status": "error", "message": "pack_id is required"})
                return self._install_pack(pack_id, params.get("timeout"))
            if action == "install_skill":
                return self._install_skill(workspace, params)
            if action == "enable_skill":
                return self._skill_action(workspace, "open", params)
            if action == "disable_skill":
                return self._skill_action(workspace, "close", params)
            if action == "configure_mcp":
                return self._configure_mcp(workspace, params)
            if action == "reload_mcp":
                return self._reload_mcp()
        except Exception as exc:
            logger.exception(f"[AgentCapability] action failed: {action}")
            return ToolResult.fail({"status": "error", "action": action, "message": str(exc)})
        return ToolResult.fail({"status": "error", "message": "unknown action"})

    def _install_pack(self, pack_id: str, timeout: Any) -> ToolResult:
        install_plan = ["feishu-lark", "feishu-cli"] if pack_id == "feishu-lark" else [pack_id]
        steps = []
        for ability in install_plan:
            payload = _payload_for_result(OptionalAbilities().execute({
                "action": "install",
                "ability": ability,
                "timeout": timeout,
            }))
            steps.append(_compact_install_step(ability, payload))
            if payload.get("status") != "success":
                return ToolResult.fail({
                    "status": "error",
                    "packId": pack_id,
                    "message": f"{ability} 安装失败，已保留诊断信息，请先根据 stderr/logPath 修复后重试。",
                    "installPlan": install_plan,
                    "steps": steps,
                    "nextAction": {"action": "diagnose"},
                })
        return ToolResult.success({
            "status": "success",
            "packId": pack_id,
            "message": f"{pack_id} 已安装完成。",
            "installPlan": install_plan,
            "steps": steps,
            "nextAction": "refresh capabilities and continue the user task",
        })

    def _diagnose(self, workspace: str) -> ToolResult:
        abilities = OptionalAbilities().execute({"action": "list"}).result
        skills = _skill_service(workspace).dispatch("query")
        try:
            from agent.tools import ToolManager

            manager = ToolManager()
            mcp_status = manager.list_mcp_status()
        except Exception as exc:
            mcp_status = {"error": str(exc)}
        return ToolResult.success({
            "status": "success",
            "workspace": workspace,
            "abilities": abilities,
            "skills": skills,
            "mcpStatus": mcp_status,
        })

    def _install_skill(self, workspace: str, params: Dict[str, Any]) -> ToolResult:
        name = str(params.get("skill") or params.get("name") or "").strip()
        payload = {
            "name": name,
            "url": params.get("url"),
            "files": params.get("files"),
            "type": params.get("type") or "url",
            "category": params.get("category") or "agent-installed",
            "_permission_checked": True,
        }
        result = _skill_service(workspace).dispatch("add", payload)
        if result.get("code") == 200:
            return ToolResult.success({"status": "success", **result})
        return ToolResult.fail({"status": "error", **result})

    def _skill_action(self, workspace: str, action: str, params: Dict[str, Any]) -> ToolResult:
        name = str(params.get("skill") or params.get("name") or "").strip()
        if not name:
            return ToolResult.fail({"status": "error", "message": "skill is required"})
        result = _skill_service(workspace).dispatch(action, {"name": name, "_permission_checked": True})
        if result.get("code") == 200:
            return ToolResult.success({"status": "success", **result})
        return ToolResult.fail({"status": "error", **result})

    def _configure_mcp(self, workspace: str, params: Dict[str, Any]) -> ToolResult:
        server = params.get("server")
        if not isinstance(server, dict) or not server.get("name"):
            return ToolResult.fail({"status": "error", "message": "server config with name is required"})
        path = _mcp_json_path(workspace)
        config = _read_json_file(path)
        if not config:
            config = {"mcpServers": []}
        key = "mcpServers" if "mcpServers" in config else "mcp_servers"
        servers = config.get(key)
        if not isinstance(servers, list):
            servers = []
        servers = [item for item in servers if not (isinstance(item, dict) and item.get("name") == server.get("name"))]
        servers.append(server)
        config[key] = servers
        written = _write_json_file(path, config)
        reload_result = self._reload_mcp().result
        return ToolResult.success({"status": "success", "configPath": written, "server": server, "reload": reload_result})

    def _reload_mcp(self) -> ToolResult:
        from agent.tools import ToolManager

        manager = ToolManager()
        manager.refresh_mcp_if_changed()
        return ToolResult.success({"status": "success", "mcpStatus": manager.list_mcp_status()})
