"""EcoreX CLI tool - safe wrapper for the bundled project CLI."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.tools.base_tool import BaseTool, ToolResult


DEFAULT_TIMEOUT = 60
MAX_OUTPUT_CHARS = 12000

SAFE_ACTIONS = {
    "version",
    "status",
    "skill_list",
    "skill_info",
    "knowledge_status",
    "knowledge_list",
}
NETWORK_ACTIONS = {"skill_search", "skill_list_remote"}
MUTATING_ACTIONS = {"skill_install", "skill_enable", "skill_disable", "install_browser"}
ALL_ACTIONS = SAFE_ACTIONS | NETWORK_ACTIONS | MUTATING_ACTIONS


class EcoreXCli(BaseTool):
    """Structured access to selected bundled CLI commands."""

    name: str = "ecorex_cli"
    description: str = (
        "Run selected built-in EcoreX CLI capabilities without raw shell. "
        "Actions: version, status, skill_list, skill_info, skill_search, skill_list_remote, "
        "skill_install, skill_enable, skill_disable, knowledge_status, knowledge_list, install_browser."
    )

    params: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: version, status, skill_list, skill_info, skill_search, skill_list_remote, skill_install, skill_enable, skill_disable, knowledge_status, knowledge_list, install_browser.",
            },
            "name": {
                "type": "string",
                "description": "Skill name or install spec for skill_info/skill_install/skill_enable/skill_disable.",
            },
            "query": {
                "type": "string",
                "description": "Search query for skill_search.",
            },
            "page": {
                "type": "integer",
                "description": "Remote skill-list page number.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds. Default: {DEFAULT_TIMEOUT}; maximum: 300.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.cwd = self.config.get("cwd", self._project_root())

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "").strip().lower().replace("-", "_")
        if action not in ALL_ACTIONS:
            return ToolResult.fail(f"Error: unsupported action '{action}'.")

        timeout = self._timeout(args.get("timeout"))
        command = self._command_for_action(action, args)
        if not command:
            return ToolResult.fail("Error: missing required argument for CLI action.")

        decision = self._authorize(action, args)
        if not decision.get("allowed", True):
            return ToolResult.fail(f"Error: {decision.get('reason') or 'CLI action blocked by permissions.'}")

        try:
            result = subprocess.run(
                [sys.executable, "-m", "cli.cli", *command],
                cwd=self._project_root(),
                text=True,
                capture_output=True,
                timeout=timeout,
                env=self._env(),
            )
        except subprocess.TimeoutExpired:
            return ToolResult.fail(f"Error: ecorex_cli action '{action}' timed out after {timeout}s.")
        except Exception as exc:
            return ToolResult.fail(f"Error running ecorex_cli action '{action}': {exc}")

        stdout = self._truncate(result.stdout or "")
        stderr = self._truncate(result.stderr or "")
        payload = {
            "action": action,
            "command": ["python", "-m", "cli.cli", *command],
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "output": stdout if result.returncode == 0 or not stderr else f"{stdout}\n{stderr}".strip(),
        }
        if result.returncode != 0:
            return ToolResult.fail(payload)
        return ToolResult.success(payload)

    def _command_for_action(self, action: str, args: Dict[str, Any]) -> List[str]:
        if action == "version":
            return ["version"]
        if action == "status":
            return ["status"]
        if action == "skill_list":
            return ["skill", "list"]
        if action == "skill_list_remote":
            page = self._positive_int(args.get("page"), 1, minimum=1, maximum=100)
            return ["skill", "list", "--remote", "--page", str(page)]
        if action == "skill_search":
            query = str(args.get("query") or args.get("name") or "").strip()
            return ["skill", "search", query] if query else []
        if action == "skill_info":
            name = str(args.get("name") or "").strip()
            return ["skill", "info", name] if name else []
        if action == "skill_install":
            name = str(args.get("name") or "").strip()
            return ["skill", "install", name] if name else []
        if action == "skill_enable":
            name = str(args.get("name") or "").strip()
            return ["skill", "enable", name] if name else []
        if action == "skill_disable":
            name = str(args.get("name") or "").strip()
            return ["skill", "disable", name] if name else []
        if action == "knowledge_status":
            return ["knowledge"]
        if action == "knowledge_list":
            return ["knowledge", "list"]
        if action == "install_browser":
            return ["install-browser"]
        return []

    def _authorize(self, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if action in SAFE_ACTIONS:
            return {"allowed": True, "reason": "safe-cli-action"}
        try:
            from common.ecorex_tool_permissions import get_tool_permission_broker

            broker = get_tool_permission_broker()
            if action in NETWORK_ACTIONS:
                return broker.authorize_noninteractive("web_fetch", {"url": "https://www.ecoreai.cn/ecorex-agent/skills"})
            if action in {"skill_install", "skill_enable", "skill_disable"}:
                return broker.authorize_noninteractive("skill_write", {"action": action, "name": args.get("name")})
            if action == "install_browser":
                return broker.authorize_noninteractive("browser", {"action": "install_browser"})
        except Exception as exc:
            return {"allowed": False, "reason": f"Permission broker unavailable; CLI action blocked. {exc}"}
        return {"allowed": False, "reason": "CLI action blocked by default."}

    @staticmethod
    def _project_root() -> str:
        return str(Path(__file__).resolve().parents[3])

    @staticmethod
    def _env() -> Dict[str, str]:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    @staticmethod
    def _timeout(value: Any) -> int:
        return EcoreXCli._positive_int(value, DEFAULT_TIMEOUT, minimum=1, maximum=300)

    @staticmethod
    def _positive_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _truncate(text: str) -> str:
        if len(text) <= MAX_OUTPUT_CHARS:
            return text
        return text[:MAX_OUTPUT_CHARS] + f"\n\n[truncated at {MAX_OUTPUT_CHARS} chars]"
