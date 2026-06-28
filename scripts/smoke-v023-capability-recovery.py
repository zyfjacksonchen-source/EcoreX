#!/usr/bin/env python3
"""v0.2.3 capability recovery smoke.

This guards the regression where EcoreX appears as a plain chat AI because
first-party tools are not discoverable through the unified runtime surfaces.
"""

from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def check(label: str, ok: bool, evidence: Any = None) -> dict[str, Any]:
    row: dict[str, Any] = {"label": label, "status": "PASS" if ok else "FAIL"}
    if evidence is not None:
        row["evidence"] = evidence
    return row


def main() -> int:
    sys.path.insert(0, str(ROOT))

    sys.modules.setdefault("web", types.SimpleNamespace(
        header=lambda *args, **kwargs: None,
        data=lambda: b"{}",
        input=lambda **kwargs: types.SimpleNamespace(**kwargs),
        cookies=lambda: {},
        ctx=types.SimpleNamespace(env={}, method="GET", status="200 OK"),
        notfound=Exception,
        application=lambda *args, **kwargs: None,
        httpserver=types.SimpleNamespace(LogMiddleware=types.SimpleNamespace(log=lambda *args, **kwargs: None)),
    ))

    from common.log import logger
    logger.setLevel("ERROR")

    from agent.extensions import ExtensionRegistry
    from agent.tools.bash.bash import Bash
    from agent.tools.optional_abilities.optional_abilities import OptionalAbilities
    from agent.tools.tool_manager import ToolManager
    from channel.channel_catalog import channel_observability
    from channel.web import web_channel
    from channel.web.web_channel import ChannelsHandler, ExtensionsHandler, ToolsHandler

    web_channel._require_auth = lambda: None

    manager = ToolManager()
    manager.load_tools()
    tool_names = set(manager.tool_classes.keys())

    bash_result = Bash({"cwd": str(ROOT), "safety_mode": False, "timeout": 10}).execute({
        "command": "echo ecorex_capability_recovery_ok"
    })
    bash_output = ""
    if getattr(bash_result, "result", None) and isinstance(bash_result.result, dict):
        bash_output = str(bash_result.result.get("output") or "")

    extensions_payload = ExtensionRegistry(str(ROOT)).list_extensions()
    extensions = extensions_payload.get("extensions") if isinstance(extensions_payload, dict) else []
    by_id = {
        str(item.get("id")): item
        for item in extensions
        if isinstance(item, dict) and item.get("id")
    }

    tools_api = json.loads(ToolsHandler().GET())
    tools_api_names = {
        str(item.get("name"))
        for item in tools_api.get("tools", [])
        if isinstance(item, dict) and item.get("name")
    }
    extensions_api = json.loads(ExtensionsHandler().GET())
    extensions_api_ids = {
        str(item.get("id"))
        for item in extensions_api.get("extensions", [])
        if isinstance(item, dict) and item.get("id")
    }

    channel_names = ChannelsHandler._agent_tool_names()
    feishu_surface = channel_observability(
        {"channel_type": "web,feishu", "feishu_app_id": "", "feishu_app_secret": ""},
        "feishu",
        tool_names=channel_names,
    ).get("agentSurface", {})

    abilities = OptionalAbilities().execute({"action": "list"}).result
    ability_rows = abilities.get("abilities", []) if isinstance(abilities, dict) else []
    ability_ids = {
        str(item.get("id")): item
        for item in ability_rows
        if isinstance(item, dict) and item.get("id")
    }

    core_tools = {"bash", "read", "write", "edit", "ls", "find", "host_diagnostics"}
    v023_tools = {"browser", "feishu_cli", "optional_abilities", "agent_capability", "ocr", "imagegen"}
    checks = [
        check("tool manager loads v0.2.2 core tools", core_tools.issubset(tool_names), sorted(core_tools - tool_names)),
        check("tool manager loads v0.2.3 tool surfaces", v023_tools.issubset(tool_names), sorted(v023_tools - tool_names)),
        check("bash executes a real command", bash_result.status == "success" and "ecorex_capability_recovery_ok" in bash_output),
        check("extensions expose builtin bash tool", by_id.get("tool:bash", {}).get("status") == "ready"),
        check("extensions expose builtin file tools", all(by_id.get(f"tool:{name}", {}).get("status") == "ready" for name in ("read", "write", "edit", "ls"))),
        check("extensions expose v0.2.3 browser and feishu tools", all(by_id.get(f"tool:{name}", {}).get("status") == "ready" for name in ("browser", "feishu_cli", "optional_abilities", "agent_capability"))),
        check("api tools expose builtin shell and v0.2.3 tools", {"bash", "browser", "feishu_cli", "ocr", "imagegen"}.issubset(tools_api_names), sorted({"bash", "browser", "feishu_cli", "ocr", "imagegen"} - tools_api_names)),
        check("api tools registry health is not empty", tools_api.get("toolCount", 0) > 0 and tools_api.get("registryStatus") in {"ready", "degraded"}, tools_api.get("registry")),
        check("api extensions expose builtin tools", {"tool:bash", "tool:read", "tool:write", "tool:edit", "tool:browser", "tool:feishu_cli"}.issubset(extensions_api_ids)),
        check("channel snapshot self-loads feishu_cli", isinstance(channel_names, set) and "feishu_cli" in channel_names),
        check("feishu channel schema is visible after cold snapshot", feishu_surface.get("status") == "schema_visible_unverified", feishu_surface),
        check("optional ability browser-cdp remains discoverable", "browser-cdp" in ability_ids),
        check("optional ability chrome-devtools-mcp remains discoverable", "chrome-devtools-mcp" in ability_ids),
        check("optional ability feishu-cli remains discoverable", "feishu-cli" in ability_ids),
        check("optional ability fast-ocr remains discoverable", "fast-ocr" in ability_ids),
    ]
    failed = [row for row in checks if row["status"] != "PASS"]
    result = {
        "status": "PASS" if not failed else "FAIL",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "toolCount": len(tool_names),
        "extensionCount": len(by_id),
        "checks": checks,
        "failed": [row["label"] for row in failed],
        "redacted": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
