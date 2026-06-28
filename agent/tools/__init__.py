"""First-party tool exports for the agent runtime.

Tool discovery imports this package before it can build model-visible schemas.
One optional tool must never make the whole registry empty, so each concrete
tool class is imported independently and missing optional dependencies are
recorded for diagnostics.
"""

from __future__ import annotations

import importlib
from typing import Any, Optional

from agent.tools.base_tool import BaseTool
from agent.tools.tool_manager import ToolManager
from common.log import logger


_TOOL_IMPORT_ERRORS: list[dict[str, str]] = []


def _record_import_error(module_name: str, class_name: str, exc: BaseException) -> None:
    entry = {
        "module": module_name,
        "class": class_name,
        "errorType": exc.__class__.__name__,
        "message": str(exc),
    }
    if entry not in _TOOL_IMPORT_ERRORS:
        _TOOL_IMPORT_ERRORS.append(entry)
    logger.warning(
        "[Tools] %s.%s not loaded: %s: %s",
        module_name,
        class_name,
        exc.__class__.__name__,
        exc,
    )


def _safe_import(module_name: str, class_name: str) -> Optional[type]:
    try:
        module = importlib.import_module(module_name)
        cls: Any = getattr(module, class_name)
        return cls if isinstance(cls, type) else None
    except Exception as exc:
        _record_import_error(module_name, class_name, exc)
        return None


def get_tool_import_errors() -> list[dict[str, str]]:
    return list(_TOOL_IMPORT_ERRORS)


Read = _safe_import("agent.tools.read.read", "Read")
Write = _safe_import("agent.tools.write.write", "Write")
Edit = _safe_import("agent.tools.edit.edit", "Edit")
Bash = _safe_import("agent.tools.bash.bash", "Bash")
EcoreXCli = _safe_import("agent.tools.ecorex_cli.ecorex_cli", "EcoreXCli")
FeishuCli = _safe_import("agent.tools.feishu_cli.feishu_cli", "FeishuCli")
TongxinCli = _safe_import("agent.tools.tongxin_cli.tongxin_cli", "TongxinCli")
HostDiagnostics = _safe_import("agent.tools.host_diagnostics.host_diagnostics", "HostDiagnostics")
OptionalAbilities = _safe_import("agent.tools.optional_abilities.optional_abilities", "OptionalAbilities")
AgentCapabilityTool = _safe_import("agent.tools.agent_capability.agent_capability", "AgentCapabilityTool")
SubagentTool = _safe_import("agent.tools.subagent.subagent", "SubagentTool")
Find = _safe_import("agent.tools.find.find", "Find")
Ls = _safe_import("agent.tools.ls.ls", "Ls")
Send = _safe_import("agent.tools.send.send", "Send")
MemorySearchTool = _safe_import("agent.tools.memory.memory_search", "MemorySearchTool")
MemoryGetTool = _safe_import("agent.tools.memory.memory_get", "MemoryGetTool")
EvolutionUndoTool = _safe_import("agent.tools.evolution_undo.evolution_undo", "EvolutionUndoTool")
EnvConfig = _safe_import("agent.tools.env_config.env_config", "EnvConfig")
SchedulerTool = _safe_import("agent.tools.scheduler.scheduler_tool", "SchedulerTool")
WebSearch = _safe_import("agent.tools.web_search.web_search", "WebSearch")
WebFetch = _safe_import("agent.tools.web_fetch.web_fetch", "WebFetch")
Vision = _safe_import("agent.tools.vision.vision", "Vision")
OcrTool = _safe_import("agent.tools.ocr.ocr", "OcrTool")
BrowserTool = _safe_import("agent.tools.browser.browser_tool", "BrowserTool")
ImageGenTool = _safe_import("agent.tools.imagegen.imagegen", "ImageGenTool")
McpTool = _safe_import("agent.tools.mcp.mcp_tool", "McpTool")
McpClientRegistry = _safe_import("agent.tools.mcp.mcp_client", "McpClientRegistry")


__all__ = [
    "BaseTool",
    "ToolManager",
    "Read",
    "Write",
    "Edit",
    "Bash",
    "EcoreXCli",
    "FeishuCli",
    "TongxinCli",
    "HostDiagnostics",
    "OptionalAbilities",
    "AgentCapabilityTool",
    "SubagentTool",
    "Find",
    "Ls",
    "Send",
    "MemorySearchTool",
    "MemoryGetTool",
    "EvolutionUndoTool",
    "EnvConfig",
    "SchedulerTool",
    "WebSearch",
    "WebFetch",
    "Vision",
    "OcrTool",
    "BrowserTool",
    "ImageGenTool",
    "McpTool",
]
