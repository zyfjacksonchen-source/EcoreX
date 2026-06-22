"""Discover, enable, and install EcoreX optional runtime abilities."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger
from config import conf


RUNTIME_ROOT = Path(__file__).resolve().parents[3]
FEISHU_LARK_SOURCE_URL = "https://github.com/larksuite/cli"
FEISHU_LARK_MIRROR_URLS = ["https://registry.npmmirror.com/@larksuite/cli"]
FEISHU_LARK_NPM_MIRROR = "https://registry.npmmirror.com"

_LIVE_PROVIDER_CONFIG_KEYS = {
    "model",
    "bot_type",
    "text_to_image",
    "open_ai_api_key",
    "open_ai_api_base",
    "custom_api_key",
    "custom_api_base",
    "linkai_api_key",
    "linkai_api_base",
    "claude_api_key",
    "claude_api_base",
    "gemini_api_key",
    "gemini_api_base",
    "minimax_api_key",
    "minimax_api_base",
    "deepseek_api_key",
    "deepseek_api_base",
    "mimo_api_key",
    "mimo_api_base",
    "qianfan_api_key",
    "qianfan_api_base",
    "zhipu_ai_api_key",
    "zhipu_ai_api_base",
    "moonshot_api_key",
    "moonshot_api_base",
    "ark_api_key",
    "ark_api_base",
    "dashscope_api_key",
    "dashscope_api_base",
    "use_azure_chatgpt",
    "azure_deployment_id",
    "azure_api_version",
    "azure_openai_dalle_api_base",
    "azure_openai_dalle_api_key",
    "azure_openai_dalle_deployment_id",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _config_path() -> Path:
    explicit = RUNTIME_ROOT / "config.json"
    return explicit if explicit.exists() else RUNTIME_ROOT / "config.json"


def _template_path() -> Path:
    return RUNTIME_ROOT / "config-template.json"


def _read_runtime_config() -> Dict[str, Any]:
    path = _config_path()
    if not path.exists() and _template_path().exists():
        path = _template_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning(f"[OptionalAbilities] failed reading runtime config: {exc}")
        return {}


def _write_runtime_config(data: Dict[str, Any]) -> Path:
    path = _config_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _update_live_config(data: Dict[str, Any]) -> None:
    live = conf()
    for key, value in data.items():
        if key in _LIVE_PROVIDER_CONFIG_KEYS and live.get(key) not in (None, ""):
            continue
        live[key] = value


def _which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt" and not name.lower().endswith(".cmd"):
        return shutil.which(f"{name}.cmd")
    return None


def _state_dir() -> Path:
    override = os.environ.get("ECOREX_CAPABILITY_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return RUNTIME_ROOT / "capability-state"


def _safe_pack_dir_name(pack_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(pack_id or "").strip()).strip(".-")
    return safe or "pack"


def _capability_package_root() -> Path:
    override = os.environ.get("ECOREX_CAPABILITY_TARGET_DIR")
    if override:
        return Path(override).expanduser()
    return RUNTIME_ROOT / "capability-packages"


def _playwright_browsers_dir() -> Optional[Path]:
    override = os.environ.get("ECOREX_PLAYWRIGHT_BROWSERS_DIR") or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    return Path(override).expanduser() if override else None


def _capability_target_dir(pack_id: str) -> Path:
    return _capability_package_root() / _safe_pack_dir_name(pack_id)


def _add_capability_target_to_path(path: Path) -> None:
    try:
        resolved = str(path.resolve())
    except Exception:
        resolved = str(path)
    if not path.exists():
        return
    existing = {str(Path(item).resolve()) for item in sys.path if item}
    if resolved not in existing:
        sys.path.insert(0, resolved)

    pythonpath = os.environ.get("PYTHONPATH", "")
    parts = [item for item in pythonpath.split(os.pathsep) if item]
    normalized = {str(Path(item).resolve()) for item in parts}
    if resolved not in normalized:
        os.environ["PYTHONPATH"] = resolved if not pythonpath else resolved + os.pathsep + pythonpath


def _apply_installed_capability_paths() -> None:
    state_dir = _state_dir()
    if not state_dir.exists():
        return
    for state_path in state_dir.glob("*.json"):
        try:
            data = json.loads(state_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if not (data.get("installed") or data.get("state") == "installed"):
            continue
        target = data.get("targetDir") or str(_capability_target_dir(str(data.get("packId") or state_path.stem)))
        _add_capability_target_to_path(Path(str(target)))


def _capability_manifest_path() -> Optional[Path]:
    candidates = [
        RUNTIME_ROOT / "capabilities.json",
        RUNTIME_ROOT / "desktop" / "runtime-packs" / "capabilities.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _capability_pack_ids() -> set[str]:
    pack_ids = {str(meta.get("packId")) for meta in _ability_defs().values() if meta.get("packId")}
    manifest = _capability_manifest_path()
    if not manifest:
        return pack_ids
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        logger.warning(f"[OptionalAbilities] failed reading capability manifest ids: {exc}")
        return pack_ids
    packs = data.get("packs") if isinstance(data, dict) else None
    if isinstance(packs, list):
        for pack in packs:
            if isinstance(pack, dict) and pack.get("id"):
                pack_ids.add(str(pack["id"]))
    return pack_ids


def _capability_state(pack_id: str) -> Dict[str, Any]:
    status_path = _state_dir() / f"{pack_id}.json"
    if not status_path.exists():
        return {"installed": False, "state": "not-installed"}
    try:
        data = json.loads(status_path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            return {
                "installed": bool(data.get("installed") or data.get("state") == "installed"),
                "state": data.get("state") or "unknown",
                "updatedAt": data.get("updatedAt"),
                "message": data.get("message"),
                "logPath": data.get("logPath"),
                "targetDir": data.get("targetDir"),
            }
    except Exception as exc:
        logger.warning(f"[OptionalAbilities] failed reading capability state {pack_id}: {exc}")
    return {"installed": False, "state": "unknown"}


def _has_chrome_devtools_mcp(config: Dict[str, Any]) -> bool:
    servers = config.get("mcp_servers")
    if not isinstance(servers, list):
        return False
    for server in servers:
        if not isinstance(server, dict):
            continue
        args = server.get("args") if isinstance(server.get("args"), list) else []
        joined = " ".join(str(item) for item in args)
        if server.get("name") == "chrome-devtools" or "chrome-devtools-mcp" in joined:
            return True
    return False


def _ensure_chrome_devtools_mcp(config: Dict[str, Any]) -> None:
    servers = config.get("mcp_servers")
    if not isinstance(servers, list):
        servers = []
        config["mcp_servers"] = servers
    if _has_chrome_devtools_mcp(config):
        return
    servers.append({
        "name": "chrome-devtools",
        "type": "stdio",
        "command": "npx.cmd" if os.name == "nt" else "npx",
        "args": [
            "chrome-devtools-mcp@latest",
            "--browserUrl",
            "http://127.0.0.1:9222",
            "--no-usage-statistics",
        ],
        "timeout": 30,
    })


def _set_nested(data: Dict[str, Any], *keys: str, value: Any) -> None:
    target = data
    for key in keys[:-1]:
        child = target.get(key)
        if not isinstance(child, dict):
            child = {}
            target[key] = child
        target = child
    target[keys[-1]] = value


def _get_nested(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    target: Any = data
    for key in keys:
        if not isinstance(target, dict):
            return default
        target = target.get(key)
    return default if target is None else target


def _ability_defs() -> Dict[str, Dict[str, Any]]:
    return {
        "find-skill": {
            "label": "Find skill and file finder",
            "kind": "built-in",
            "defaultPolicy": "loaded",
            "startupImpact": "low",
            "enabled": True,
            "installable": False,
            "notes": "Built in for v0.1.14; no extra install required.",
        },
        "skill-creator": {
            "label": "Skill creator",
            "kind": "built-in",
            "defaultPolicy": "loaded",
            "startupImpact": "low",
            "enabled": True,
            "installable": False,
            "notes": "Built in for v0.1.14; use when creating or packaging skills.",
        },
        "ecorex-cli": {
            "label": "Structured EcoreX CLI",
            "kind": "built-in",
            "defaultPolicy": "loaded",
            "startupImpact": "low",
            "enabled": True,
            "installable": False,
            "notes": "Use ecorex_cli instead of raw shell for bundled CLI actions.",
        },
        "host-diagnostics": {
            "label": "Host diagnostics",
            "kind": "built-in",
            "defaultPolicy": "loaded",
            "startupImpact": "low",
            "enabled": True,
            "installable": False,
            "notes": "Read-only diagnostics for capability, MCP, CDP, permissions, and logs.",
        },
        "chrome-devtools-mcp": {
            "label": "Chrome DevTools MCP",
            "kind": "optional-runtime",
            "defaultPolicy": "discoverable-disabled",
            "startupImpact": "high",
            "configKey": "mcp_auto_start",
            "installable": False,
            "notes": "Uses npx chrome-devtools-mcp@latest after explicit enablement.",
        },
        "browser-cdp": {
            "label": "Browser CDP auto-launch",
            "kind": "optional-runtime",
            "defaultPolicy": "discoverable-disabled",
            "startupImpact": "medium",
            "configPath": ["tools", "browser", "cdp_auto_launch"],
            "packId": "browser-automation",
            "notes": "Keeps browser automation available without starting Chrome/CDP at boot.",
        },
        "feishu-cli": {
            "label": "Feishu/Lark CLI connector",
            "kind": "optional-runtime",
            "defaultPolicy": "discoverable-disabled",
            "startupImpact": "medium",
            "configPath": ["tools", "feishu_cli", "auto_install"],
            "packId": "feishu-lark",
            "sourceUrl": FEISHU_LARK_SOURCE_URL,
            "mirrorUrls": FEISHU_LARK_MIRROR_URLS,
            "installHint": (
                "Agent should use the built-in find skill first (gated as find-skill), then install official "
                "@larksuite/cli on demand. If npmjs.org times out, retry with "
                f"the domestic npm mirror {FEISHU_LARK_NPM_MIRROR}."
            ),
            "notes": "Installs official @larksuite/cli on demand through the structured feishu_cli tool after the find-skill gate.",
        },
        "scheduler": {
            "label": "Scheduler background service",
            "kind": "optional-runtime",
            "defaultPolicy": "discoverable-disabled",
            "startupImpact": "medium",
            "configKey": "scheduler_enabled",
            "installable": False,
            "notes": "Enables scheduled task scanning on future agent/runtime initialization.",
        },
        "self-evolution": {
            "label": "Self-evolution idle trigger",
            "kind": "optional-runtime",
            "defaultPolicy": "discoverable-disabled",
            "startupImpact": "medium",
            "configKey": "self_evolution_enabled",
            "installable": False,
            "notes": "Manual skill/memory edits still work; proactive idle repair starts only after enable.",
        },
        "browser-automation": {
            "label": "Playwright fallback browser pack",
            "kind": "capability-pack",
            "defaultPolicy": "install-on-demand",
            "startupImpact": "none-until-used",
            "packId": "browser-automation",
            "notes": "Large pack. Install only when fallback Chromium is required.",
        },
        "office-pdf": {
            "label": "Office/PDF parsing pack",
            "kind": "capability-pack",
            "defaultPolicy": "install-on-demand",
            "startupImpact": "none-until-used",
            "packId": "office-pdf",
            "notes": "Document parsing extras for PDF, Word, Excel, and PowerPoint.",
        },
        "memory-heavy": {
            "label": "Heavy memory/data pack",
            "kind": "capability-pack",
            "defaultPolicy": "install-on-demand",
            "startupImpact": "none-until-used",
            "packId": "memory-heavy",
            "notes": "Installs heavier local data dependencies such as numpy/pandas.",
        },
        "model-connectors": {
            "label": "Vendor model SDK pack",
            "kind": "capability-pack",
            "defaultPolicy": "install-on-demand",
            "startupImpact": "none-until-used",
            "packId": "model-connectors",
            "notes": "Official SDKs for selected non-OpenAI-compatible providers.",
        },
        "voice": {
            "label": "Voice capability pack",
            "kind": "capability-pack",
            "defaultPolicy": "install-on-demand",
            "startupImpact": "none-until-used",
            "packId": "voice",
            "notes": "Speech recognition and voice-related dependencies.",
        },
        "im-channels": {
            "label": "Extra IM channel pack",
            "kind": "capability-pack",
            "defaultPolicy": "install-on-demand",
            "startupImpact": "none-until-used",
            "packId": "im-channels",
            "notes": "Slack, Discord, Telegram, WeChat, WeCom, DingTalk dependencies.",
        },
        "subagents": {
            "label": "Parallel subagent orchestration",
            "kind": "planned",
            "defaultPolicy": "plan-required",
            "startupImpact": "n/a",
            "enabled": False,
            "installable": False,
            "notes": "Not a runtime switch yet. Requires durable child sessions, worktree transactions, and UI/API orchestration.",
        },
        "goals": {
            "label": "Runtime goal tool",
            "kind": "planned",
            "defaultPolicy": "plan-required",
            "startupImpact": "n/a",
            "enabled": False,
            "installable": False,
            "notes": "Goal ledger docs exist; a first-class runtime goal API/tool is still planned.",
        },
    }


class OptionalAbilities(BaseTool):
    name: str = "optional_abilities"
    description: str = (
        "Discover, enable, disable, or install EcoreX optional abilities without raw shell. "
        "Use this before starting heavy MCP/CDP/Feishu/scheduler/self-evolution/capability packs."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: list, status, enable, disable, install.",
            },
            "ability": {
                "type": "string",
                "description": "Ability id, e.g. chrome-devtools-mcp, browser-cdp, feishu-cli, scheduler, self-evolution, browser-automation, office-pdf.",
            },
            "timeout": {
                "type": "integer",
                "description": "Install timeout seconds. Default 600, max 3600.",
            },
            "discovery_source": {
                "type": "string",
                "description": "Discovery gate for Feishu/Lark CLI install. Must be find-skill.",
            },
            "find_skill_result": {
                "type": "object",
                "description": "Structured result returned by the built-in find skill/find-skill gate.",
            },
        },
        "required": ["action"],
    }

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        _apply_installed_capability_paths()
        action = str(args.get("action") or "").strip().lower().replace("-", "_")
        ability = str(args.get("ability") or "").strip().lower().replace("_", "-")
        if action in {"list", "status"}:
            return ToolResult.success(self._list(ability or None))
        if action in {"enable", "disable", "install"}:
            if not ability:
                return ToolResult.fail({"status": "error", "message": "ability is required"})
            if action == "enable":
                return self._enable(ability)
            if action == "disable":
                return self._disable(ability)
            return self._install(
                ability,
                timeout=self._timeout(args.get("timeout")),
                discovery_source=args.get("discovery_source"),
                find_skill_result=args.get("find_skill_result") or args.get("findSkillResult"),
            )
        return ToolResult.fail({"status": "error", "message": "action must be one of: list, status, enable, disable, install"})

    def _list(self, ability: Optional[str] = None) -> Dict[str, Any]:
        config = _read_runtime_config()
        defs = _ability_defs()
        selected = {ability: defs[ability]} if ability and ability in defs else defs
        abilities = [self._status_for(key, meta, config) for key, meta in selected.items()]
        if ability and ability not in defs:
            return {"status": "error", "message": f"unknown ability: {ability}", "known": sorted(defs)}
        return {
            "status": "success",
            "generatedAt": _now(),
            "policy": "Heavy abilities are discoverable but disabled by default; enable/install only after user permission.",
            "abilities": abilities,
        }

    def _status_for(self, key: str, meta: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        item = {
            "id": key,
            "label": meta.get("label"),
            "kind": meta.get("kind"),
            "defaultPolicy": meta.get("defaultPolicy"),
            "startupImpact": meta.get("startupImpact"),
            "notes": meta.get("notes"),
            "agentCanEnable": meta.get("kind") == "optional-runtime",
            "agentCanInstall": bool(meta.get("packId")),
        }
        for field in ("discoveryOnly", "sourceUrl", "mirrorUrls", "installHint"):
            if field in meta:
                item[field] = meta[field]
        if "enabled" in meta:
            item["enabled"] = bool(meta.get("enabled"))
        elif meta.get("configKey"):
            item["enabled"] = bool(config.get(str(meta["configKey"]), False))
        elif meta.get("configPath"):
            item["enabled"] = bool(_get_nested(config, *meta["configPath"], default=False))
        else:
            item["enabled"] = False

        if key == "chrome-devtools-mcp":
            item["configured"] = _has_chrome_devtools_mcp(config)
            item["npx"] = bool(_which("npx"))
        if key == "feishu-cli":
            item["larkCli"] = bool(_which("lark-cli"))
            item["npm"] = bool(_which("npm"))
        if meta.get("packId"):
            item["packId"] = meta["packId"]
            item["capabilityState"] = _capability_state(str(meta["packId"]))
        return item

    def _enable(self, ability: str) -> ToolResult:
        defs = _ability_defs()
        meta = defs.get(ability)
        if not meta:
            return ToolResult.fail({"status": "error", "message": f"unknown ability: {ability}", "known": sorted(defs)})
        if meta.get("kind") != "optional-runtime":
            return ToolResult.fail({"status": "error", "message": f"{ability} is not an enableable runtime switch"})

        config = _read_runtime_config()
        if meta.get("configKey"):
            config[str(meta["configKey"])] = True
        elif meta.get("configPath"):
            _set_nested(config, *meta["configPath"], value=True)
        if ability == "chrome-devtools-mcp":
            _ensure_chrome_devtools_mcp(config)

        path = _write_runtime_config(config)
        _update_live_config(config)

        if ability == "chrome-devtools-mcp":
            try:
                from agent.tools import ToolManager

                ToolManager()._load_mcp_tools()
            except Exception as exc:
                logger.warning(f"[OptionalAbilities] failed to start MCP after enable: {exc}")

        return ToolResult.success({
            "status": "success",
            "action": "enable",
            "ability": ability,
            "enabled": True,
            "configPath": str(path),
            "message": "Ability enabled. Heavy processes may start now or on the next matching tool use.",
        })

    def _disable(self, ability: str) -> ToolResult:
        defs = _ability_defs()
        meta = defs.get(ability)
        if not meta:
            return ToolResult.fail({"status": "error", "message": f"unknown ability: {ability}", "known": sorted(defs)})
        if meta.get("kind") != "optional-runtime":
            return ToolResult.fail({"status": "error", "message": f"{ability} is not a runtime switch"})

        config = _read_runtime_config()
        if meta.get("configKey"):
            config[str(meta["configKey"])] = False
        elif meta.get("configPath"):
            _set_nested(config, *meta["configPath"], value=False)
        path = _write_runtime_config(config)
        _update_live_config(config)

        if ability == "chrome-devtools-mcp":
            try:
                from agent.tools import ToolManager

                ToolManager().shutdown_mcp()
            except Exception as exc:
                logger.warning(f"[OptionalAbilities] failed to shut down MCP after disable: {exc}")

        return ToolResult.success({
            "status": "success",
            "action": "disable",
            "ability": ability,
            "enabled": False,
            "configPath": str(path),
        })

    def _feishu_lark_discovery_only(self, ability: str) -> ToolResult:
        return ToolResult.fail({
            "status": "error",
            "ability": ability,
            "packId": "feishu-lark",
            "discoveryOnly": True,
            "sourceUrl": FEISHU_LARK_SOURCE_URL,
            "mirrorUrls": FEISHU_LARK_MIRROR_URLS,
            "installHint": (
                "Use the built-in find skill first (gated as find-skill) to discover and install the Feishu/Lark skill or connector. "
                "For real Feishu/Lark CLI work, install official @larksuite/cli on demand. "
                f"If npmjs.org times out, retry with the domestic npm mirror: npm install --registry={FEISHU_LARK_NPM_MIRROR} @larksuite/cli@1.0.56."
            ),
            "message": (
                "feishu-lark is discovery-only. Use the built-in find skill/find-skill gate first, then fall back to "
                "official npm or a domestic npm mirror if discovery or npmjs.org times out."
            ),
        })

    def _install(self, ability: str, timeout: int, discovery_source: Any = None, find_skill_result: Any = None) -> ToolResult:
        defs = _ability_defs()
        meta = defs.get(ability)
        if not meta:
            if ability == "feishu-lark":
                return self._install_feishu_cli(timeout, discovery_source=discovery_source, find_skill_result=find_skill_result)
            if ability in _capability_pack_ids():
                return self._install_capability_pack(ability, timeout)
            return ToolResult.fail({"status": "error", "message": f"unknown ability: {ability}", "known": sorted(defs)})

        if ability == "feishu-cli":
            return self._install_feishu_cli(timeout, discovery_source=discovery_source, find_skill_result=find_skill_result)

        pack_id = meta.get("packId")
        if not pack_id:
            return ToolResult.fail({"status": "error", "message": f"{ability} has no installer"})
        if str(pack_id) == "feishu-lark":
            return self._install_feishu_cli(timeout, discovery_source=discovery_source, find_skill_result=find_skill_result)
        return self._install_capability_pack(str(pack_id), timeout)

    def _install_feishu_cli(self, timeout: int, discovery_source: Any = None, find_skill_result: Any = None) -> ToolResult:
        from agent.tools.feishu_cli.feishu_cli import FeishuCli

        install_args: Dict[str, Any] = {
            "action": "install",
            "timeout": timeout,
        }
        if discovery_source is not None:
            install_args["discovery_source"] = discovery_source
        if find_skill_result is not None:
            install_args["find_skill_result"] = find_skill_result
        result = FeishuCli().execute({
            **install_args,
        })
        payload = result.result if isinstance(result.result, dict) else {"result": result.result}
        status = "installed" if result.status == "success" and payload.get("available") else "failed"
        state_dir = _state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "packId": "feishu-lark",
            "state": status,
            "installed": status == "installed",
            "updatedAt": _now(),
            "message": payload.get("message") or ("Feishu/Lark CLI is ready." if status == "installed" else "Feishu/Lark CLI installation failed."),
            "sourceUrl": FEISHU_LARK_SOURCE_URL,
            "mirrorUrls": FEISHU_LARK_MIRROR_URLS,
            "installHint": "Official @larksuite/cli is installed on demand after the built-in find-skill gate.",
            "command": payload.get("command"),
            "installRoot": payload.get("installRoot"),
        }
        try:
            (state_dir / "feishu-lark.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[OptionalAbilities] failed writing Feishu/Lark state: {exc}")

        merged = {
            **payload,
            "status": "success" if status == "installed" else "error",
            "packId": "feishu-lark",
            "capabilityState": state,
        }
        return ToolResult.success(merged) if status == "installed" else ToolResult.fail(merged)

    def _install_capability_pack(self, pack_id: str, timeout: int) -> ToolResult:
        manifest = _capability_manifest_path()
        if not manifest:
            return ToolResult.fail({"status": "error", "message": "capability manifest not found"})
        installer = RUNTIME_ROOT / "scripts" / "install-capability.py"
        if not installer.exists():
            installer = RUNTIME_ROOT / "desktop" / "scripts" / "install-capability.py"
        if not installer.exists():
            return ToolResult.fail({"status": "error", "message": "capability installer not found"})

        state_dir = _state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        target_dir = _capability_target_dir(pack_id)
        command = [
            sys.executable,
            str(installer),
            "--pack-id",
            pack_id,
            "--runtime-dir",
            str(RUNTIME_ROOT),
            "--manifest",
            str(manifest),
            "--index-dir",
            str(state_dir),
            "--target-dir",
            str(target_dir),
            "--timeout",
            str(timeout),
            "--fallback-index-url",
            os.environ.get("ECOREX_PIP_FALLBACK_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple"),
        ]
        browsers_dir = _playwright_browsers_dir()
        if browsers_dir:
            command.extend(["--playwright-browsers-dir", str(browsers_dir)])
        try:
            result = subprocess.run(
                command,
                cwd=str(RUNTIME_ROOT),
                text=True,
                capture_output=True,
                timeout=timeout,
                env={
                    **os.environ,
                    "PYTHONIOENCODING": "utf-8",
                    "ECOREX_CAPABILITY_STATE_DIR": str(_state_dir()),
                    "ECOREX_CAPABILITY_TARGET_DIR": str(_capability_package_root()),
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult.fail({"status": "error", "message": f"install timed out after {timeout}s", "packId": pack_id})
        except Exception as exc:
            return ToolResult.fail({"status": "error", "message": str(exc), "packId": pack_id})

        state = _capability_state(pack_id)
        if state.get("installed"):
            _add_capability_target_to_path(target_dir)
        payload = {
            "status": "success" if result.returncode == 0 else "error",
            "packId": pack_id,
            "exitCode": result.returncode,
            "targetDir": str(target_dir),
            "capabilityState": state,
            "stdout": (result.stdout or "")[-4000:],
            "stderr": (result.stderr or "")[-4000:],
        }
        return ToolResult.success(payload) if result.returncode == 0 else ToolResult.fail(payload)

    @staticmethod
    def _timeout(value: Any) -> int:
        try:
            parsed = int(value)
        except Exception:
            parsed = 600
        return max(30, min(3600, parsed))
