from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from agent.tools.base_tool import BaseTool, ToolResult
from agent.tools.optional_abilities.optional_abilities import OptionalAbilities, TONGXIN_CLI_INSTALL_HINT
from common.ecorex_capability_policy import blocked_install_payload
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


def _safe_capability_event_identifier(value: Any) -> str:
    raw = str(value or "").strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    if 1 <= len(raw) <= 128 and all(char in allowed for char in raw):
        return raw
    return ""


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
    "tongxin": "tongxin-cli",
    "tongxin-cli": "tongxin-cli",
    "xin-agent": "tongxin-cli",
    "xin-agent-cli": "tongxin-cli",
    "tx-assistant": "tongxin-cli",
}

FEISHU_LARK_SOURCE_URL = "https://github.com/larksuite/cli"
FEISHU_LARK_MIRROR_URLS = ["https://registry.npmmirror.com/@larksuite/cli"]
FEISHU_LARK_NPM_MIRROR = "https://registry.npmmirror.com"


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


_FEISHU_LARK_SKILL_HINTS = (
    "feishu",
    "lark",
    "飞书",
    "@larksuite",
    "lark-cli",
)

_FIND_SKILL_POSITIVE_STATUSES = {"success", "ok", "found", "pass", "passed", "ready", "available"}


def _contains_feishu_lark_hint(value: Any) -> bool:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value or "")
    except Exception:
        text = str(value or "")
    lowered = text.lower()
    return any(hint.lower() in lowered for hint in _FEISHU_LARK_SKILL_HINTS)


def _is_positive_find_skill_result(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    status = str(value.get("status") or value.get("state") or value.get("result") or "").strip().lower()
    positive = (
        status in _FIND_SKILL_POSITIVE_STATUSES
        or value.get("success") is True
        or value.get("ok") is True
        or value.get("found") is True
        or value.get("available") is True
    )
    return positive and _contains_feishu_lark_hint(value)


def _has_find_skill_discovery(params: Dict[str, Any]) -> bool:
    for key in ("discovery_source", "source", "via", "gate", "resolved_by"):
        if "find" in str(params.get(key) or "").strip().lower():
            return True
    return (
        _is_positive_find_skill_result(params.get("find_skill_result"))
        or _is_positive_find_skill_result(params.get("findSkillResult"))
    )


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
                "description": "One of: list_packs, diagnose, install_pack, install_skill, enable_skill, disable_skill, configure_mcp, reload_mcp, request_skill_learning, create_skill_draft, approve_skill_draft.",
            },
            "pack_id": {"type": "string", "description": "Capability pack or optional ability id."},
            "skill": {"type": "string", "description": "Skill name."},
            "name": {"type": "string", "description": "Skill or draft name for learned skills."},
            "description": {"type": "string", "description": "Skill description for learned skill drafts."},
            "goal": {"type": "string", "description": "User goal or workflow being learned."},
            "url": {"type": "string", "description": "Skill package/repo URL for install_skill."},
            "files": {"type": "array", "description": "Optional skill file entries for install_skill."},
            "sources": {"type": "array", "description": "Redacted source summaries for learned skill drafts."},
            "reviews": {"type": "array", "description": "Role review summaries for learned skill drafts."},
            "draft": {"type": "object", "description": "Learned skill draft returned by create_skill_draft."},
            "request_id": {"type": "string", "description": "Optional runtime request id for ledger-backed learning events."},
            "session_id": {"type": "string", "description": "Optional runtime session id for ledger-backed learning events."},
            "discovery_source": {"type": "string", "description": "Discovery gate used before install_skill, e.g. find-skill."},
            "find_skill_result": {"type": "object", "description": "Optional structured result returned by the find skill/find-skill gate."},
            "server": {"type": "object", "description": "MCP server config for configure_mcp."},
            "script_path": {
                "type": "string",
                "description": "Optional local xin_agent_cli.py path when configuring the Tongxin CLI capability pack. If omitted, EcoreX may auto-detect or use configured authenticated bootstrap settings.",
            },
            "python_path": {
                "type": "string",
                "description": "Optional EcoreX-owned Python executable path used to run xin_agent_cli.py.",
            },
            "timeout": {"type": "integer", "description": "Install timeout seconds."},
        },
        "required": ["action"],
    }

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        action = str(params.get("action") or "").strip().lower()
        workspace = _workspace_for(self)
        try:
            if action == "list_packs":
                from agent.runtime_capabilities import CapabilityService

                return ToolResult.success(CapabilityService(workspace_root=workspace).capabilities_payload(include_related=False))
            if action == "diagnose":
                return self._diagnose(workspace)
            if action == "install_pack":
                pack_id = _normalize_pack_id(str(params.get("pack_id") or params.get("ability") or ""))
                if not pack_id:
                    return ToolResult.fail({"status": "error", "message": "pack_id is required"})
                return self._install_pack(pack_id, params)
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
            if action == "request_skill_learning":
                return self._request_skill_learning(params)
            if action == "create_skill_draft":
                return self._create_skill_draft(params)
            if action == "approve_skill_draft":
                return self._approve_skill_draft(workspace, params)
        except Exception as exc:
            logger.exception(f"[AgentCapability] action failed: {action}")
            return ToolResult.fail({"status": "error", "action": action, "message": str(exc)})
        return ToolResult.fail({"status": "error", "message": "unknown action"})

    def _install_pack(self, pack_id: str, params: Dict[str, Any]) -> ToolResult:
        if pack_id == "tongxin-cli":
            configure_args: Dict[str, Any] = {
                "action": "configure",
                "ability": "tongxin-cli",
            }
            script_path = params.get("script_path") or params.get("scriptPath") or params.get("path")
            if script_path:
                configure_args["script_path"] = script_path
            python_path = params.get("python_path") or params.get("pythonPath")
            if python_path:
                configure_args["python_path"] = python_path
            payload = _payload_for_result(OptionalAbilities().execute(configure_args))
            configured = payload.get("status") == "success" and bool(payload.get("configured"))
            response = {
                **payload,
                "packId": "tongxin-cli",
                "configureOnly": True,
                "readOnly": True,
                "defaultEnabled": True,
                "installHint": TONGXIN_CLI_INSTALL_HINT,
                "message": payload.get("message") or (
                    "tongxin-cli 已配置完成。"
                    if configured else
                    "tongxin-cli 未能完成配置，请确认本地 xin_agent_cli.py 路径存在。"
                ),
                "nextAction": {
                    "tool": "tongxin_cli",
                    "action": "status" if configured else "auto_configure",
                },
            }
            return ToolResult.success(response) if configured else ToolResult.fail(response)
        timeout = params.get("timeout")
        install_plan = ["feishu-cli"] if pack_id in {"feishu-lark", "feishu-cli"} else [pack_id]
        policy_pack_id = "feishu-lark" if pack_id in {"feishu-lark", "feishu-cli"} else pack_id
        blocked = blocked_install_payload(policy_pack_id, action="install_pack")
        if blocked:
            event_pack_id = str(blocked.get("packId") or "redacted-capability-pack")
            if not blocked.get("packIdRedacted") and policy_pack_id != pack_id:
                blocked["requestedPackId"] = pack_id
            self._record_capability_policy_blocked(event_pack_id, blocked, action="install_pack")
            return ToolResult.fail(blocked)
        if pack_id in {"feishu-lark", "feishu-cli"} and not _has_find_skill_discovery(params):
            return ToolResult.fail({
                "status": "error",
                "packId": pack_id,
                "discoveryOnly": True,
                "sourceUrl": FEISHU_LARK_SOURCE_URL,
                "mirrorUrls": FEISHU_LARK_MIRROR_URLS,
                "message": (
                    "Feishu/Lark CLI install requires the built-in find skill/find-skill gate first. "
                    "Retry install_pack with discovery_source='find-skill' or a structured find_skill_result."
                ),
                "nextAction": {
                    "skill": "find",
                    "ability": "find-skill",
                    "query": "official Feishu Lark CLI @larksuite/cli install source",
                },
            })
        steps = []
        for ability in install_plan:
            install_args: Dict[str, Any] = {
                "action": "install",
                "ability": ability,
                "timeout": timeout,
            }
            if ability == "feishu-cli":
                for key in ("discovery_source", "source", "via", "gate", "resolved_by", "find_skill_result", "findSkillResult"):
                    if key in params:
                        install_args[key] = params[key]
            payload = _payload_for_result(OptionalAbilities().execute(install_args))
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

    def _request_skill_learning(self, params: Dict[str, Any]) -> ToolResult:
        from agent.skills.learning_service import SkillLearningService

        payload = SkillLearningService().learning_prompt(
            goal=str(params.get("goal") or params.get("description") or ""),
            request_id=str(params.get("request_id") or params.get("requestId") or ""),
            session_id=str(params.get("session_id") or params.get("sessionId") or ""),
        )
        return ToolResult.success(payload)

    def _create_skill_draft(self, params: Dict[str, Any]) -> ToolResult:
        from agent.skills.learning_service import SkillLearningService

        payload = SkillLearningService().create_draft(
            name=str(params.get("name") or params.get("skill") or ""),
            description=str(params.get("description") or ""),
            files=params.get("files") if isinstance(params.get("files"), list) else [],
            goal=str(params.get("goal") or ""),
            sources=params.get("sources") if isinstance(params.get("sources"), list) else [],
            request_id=str(params.get("request_id") or params.get("requestId") or ""),
            session_id=str(params.get("session_id") or params.get("sessionId") or ""),
            reviews=params.get("reviews") if isinstance(params.get("reviews"), list) else [],
        )
        return ToolResult.success(payload)

    def _approve_skill_draft(self, workspace: str, params: Dict[str, Any]) -> ToolResult:
        from agent.skills.learning_service import SkillLearningService

        draft = params.get("draft") if isinstance(params.get("draft"), dict) else {}
        payload = SkillLearningService(skill_service=_skill_service(workspace)).approve_and_register(
            draft=draft,
            request_id=str(params.get("request_id") or params.get("requestId") or ""),
            session_id=str(params.get("session_id") or params.get("sessionId") or ""),
        )
        return ToolResult.success(payload)

    def _record_capability_policy_blocked(self, pack_id: str, blocked: Dict[str, Any], *, action: str) -> None:
        context = getattr(self, "context", None)
        request_id = _safe_capability_event_identifier(getattr(context, "_current_request_id", ""))
        if not request_id:
            return
        session_id = _safe_capability_event_identifier(getattr(context, "_current_session_id", ""))
        policy = blocked.get("policy") if isinstance(blocked.get("policy"), dict) else {}
        safe_pack_id = _safe_capability_event_identifier(pack_id) or "redacted-capability-pack"
        try:
            from agent.protocol import get_run_event_ledger

            get_run_event_ledger().append_event(
                request_id=request_id,
                session_id=session_id,
                turn_id=request_id,
                event_type="capability.policy_blocked",
                payload={
                    "pack_id": safe_pack_id,
                    "action": action,
                    "error_type": blocked.get("errorType") or "capability_policy_blocked",
                    "policy_mode": policy.get("policyMode") or "disabled",
                    "install_allowed": bool(policy.get("installAllowed")),
                    "policy_source": policy.get("policySource") or "",
                    "policy_updated_at": policy.get("policyUpdatedAt") or "",
                    "pack_id_redacted": bool(blocked.get("packIdRedacted") or policy.get("packIdRedacted")),
                },
                idempotency_key=f"{request_id}:capability.policy_blocked:{safe_pack_id}:{action}",
                source="tool",
            )
        except Exception as exc:
            logger.debug(f"[AgentCapability] capability policy event skipped: {exc}")

    def _diagnose(self, workspace: str) -> ToolResult:
        from agent.runtime_capabilities import CapabilityService

        return ToolResult.success(CapabilityService(workspace_root=workspace).diagnose_payload())

    def _install_skill(self, workspace: str, params: Dict[str, Any]) -> ToolResult:
        name = str(params.get("skill") or params.get("name") or "").strip()
        if _contains_feishu_lark_hint({
            "name": name,
            "url": params.get("url"),
            "files": params.get("files"),
            "category": params.get("category"),
        }) and not _has_find_skill_discovery(params):
            return ToolResult.fail({
                "status": "error",
                "message": (
                    "Feishu/Lark related skill installs must be discovered through the built-in "
                    "find skill / find-skill gate first. Retry install_skill with "
                    "discovery_source='find-skill' or a find_skill_result payload."
                ),
                "nextAction": {
                    "skill": "find",
                    "ability": "find-skill",
                    "query": "Feishu Lark connector skill or SDK source",
                },
            })
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
