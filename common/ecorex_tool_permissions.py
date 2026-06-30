"""Tool-execution permission broker for EcoreX local runtimes.

The broker is shared by desktop and WebUI sidecars. High-risk local tools such
as shell and browser automation either run directly in full-access mode or pause
until the UI posts a permission decision.
"""

from __future__ import annotations

import hashlib
import fnmatch
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from common.log import logger


Decision = Dict[str, Any]
Emitter = Callable[[str, Dict[str, Any]], None]

_DANGEROUS_TOOLS = {
    "bash",
    "shell",
    "terminal",
    "browser",
    "feishu_cli",
    "tongxin_cli",
    "optional_abilities",
    "agent_capability",
    "mcp",
    "mcp_server",
    "write",
    "edit",
    "fs_write",
    "skill_write",
    "env_config",
    "send",
    "scheduler",
    "evolution_undo",
    "web_fetch",
    "web_search",
    "vision",
    "ocr",
    "imagegen",
}
_ALLOWED_MODES = {"full-access", "smart-ask", "always-ask", "read-only", "custom"}
_DEFAULT_TIMEOUT_SECONDS = 300
_DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"
_ACCESS_RANK = {"deny": 0, "read": 1, "write": 2}
_ACCESS_TIEBREAK = {"deny": 3, "write": 2, "read": 1}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path: Path, fallback: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else fallback
    except FileNotFoundError:
        return fallback
    except Exception as exc:
        logger.warning(f"[EcoreXToolPermission] failed reading {path}: {exc}")
        return fallback


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _mask_sensitive(value: str) -> str:
    text = value or ""
    text = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***", text)
    text = re.sub(r"gh[pousr]_[A-Za-z0-9_]{12,}", "ghp_***", text)
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)(=|:)\s*[^\s,&]+", r"\1=***", text)
    return text


def _summarize_args(tool_name: str, arguments: Dict[str, Any]) -> str:
    normalized = (tool_name or "").strip().lower()
    if normalized in {"bash", "shell", "terminal"}:
        command = str(arguments.get("command") or arguments.get("cmd") or "")
        return _mask_sensitive(command).strip()[:500] or "shell command"
    if normalized == "browser":
        action = str(arguments.get("action") or "browser action")
        target = str(arguments.get("url") or arguments.get("selector") or arguments.get("text") or "")
        summary = f"{action} {target}".strip()
        return _mask_sensitive(summary)[:500] or "browser action"
    if normalized == "feishu_cli":
        action = str(arguments.get("action") or "")
        cli_args = arguments.get("args")
        if isinstance(cli_args, list):
            detail = " ".join(str(item) for item in cli_args[:12])
        else:
            detail = str(cli_args or arguments.get("scope") or arguments.get("domain") or "")
        return _mask_sensitive(f"{action} {detail}".strip())[:500] or "Feishu CLI action"
    if normalized == "tongxin_cli":
        action = str(arguments.get("action") or "")
        cli_args = arguments.get("args")
        if isinstance(cli_args, list):
            detail = " ".join(str(item) for item in cli_args[:12])
        else:
            detail = str(cli_args or "")
        return _mask_sensitive(f"{action} {detail}".strip())[:500] or "Tongxin CLI read-only query"
    if normalized == "optional_abilities":
        action = str(arguments.get("action") or "")
        ability = str(arguments.get("ability") or arguments.get("pack_id") or "")
        detail = " ".join(part for part in [action, ability] if part)
        return _mask_sensitive(detail).strip()[:500] or "optional ability change"
    if normalized == "agent_capability":
        action = str(arguments.get("action") or "")
        ability = str(arguments.get("pack_id") or arguments.get("ability") or arguments.get("skill") or "")
        server = arguments.get("server")
        if isinstance(server, dict) and server.get("name"):
            ability = str(server.get("name"))
        detail = " ".join(part for part in [action, ability] if part)
        return _mask_sensitive(detail).strip()[:500] or "agent capability change"
    if normalized in {"mcp", "mcp_server"}:
        server = str(arguments.get("server") or arguments.get("server_name") or "")
        tool = str(arguments.get("tool") or arguments.get("tool_name") or "")
        command = str(arguments.get("command") or "")
        detail = " ".join(part for part in [server, tool, command] if part)
        return _mask_sensitive(detail).strip()[:500] or "MCP external capability"
    if normalized in {"write", "edit", "fs_write"}:
        path = str(arguments.get("path") or arguments.get("file") or "")
        return _mask_sensitive(path).strip()[:500] or "file write"
    if normalized == "skill_write":
        action = str(arguments.get("action") or "")
        name = str(arguments.get("name") or "")
        detail = " ".join(part for part in [action, name] if part)
        return _mask_sensitive(detail).strip()[:500] or "skill mutation"
    if normalized == "env_config":
        action = str(arguments.get("action") or "")
        key = str(arguments.get("key") or "")
        detail = " ".join(part for part in [action, key] if part)
        return _mask_sensitive(detail).strip()[:500] or "environment configuration"
    if normalized == "send":
        path = str(arguments.get("path") or arguments.get("file") or "")
        return _mask_sensitive(path).strip()[:500] or "file send"
    if normalized == "scheduler":
        action = str(arguments.get("action") or "")
        name = str(arguments.get("name") or arguments.get("task_id") or "")
        detail = " ".join(part for part in [action, name] if part)
        return _mask_sensitive(detail).strip()[:500] or "scheduled task change"
    if normalized == "evolution_undo":
        backup_id = str(arguments.get("backup_id") or "")
        return _mask_sensitive(backup_id).strip()[:500] or "self-evolution rollback"
    if normalized == "web_fetch":
        return _mask_sensitive(str(arguments.get("url") or "")).strip()[:500] or "web fetch"
    if normalized == "web_search":
        return _mask_sensitive(str(arguments.get("query") or "")).strip()[:500] or "web search"
    if normalized == "vision":
        image = str(arguments.get("image") or "")
        question = str(arguments.get("question") or "")
        detail = " ".join(part for part in [image, question[:120]] if part)
        return _mask_sensitive(detail).strip()[:500] or "vision image analysis"
    if normalized == "ocr":
        action = str(arguments.get("action") or "extract_text")
        image = str(arguments.get("image") or "")
        detail = " ".join(part for part in [action, image] if part)
        return _mask_sensitive(detail).strip()[:500] or "local OCR"
    if normalized == "imagegen":
        provider = str(arguments.get("provider") or "")
        model = str(arguments.get("model") or "")
        output_dir = str(arguments.get("output_dir") or "")
        image_refs = arguments.get("image_urls")
        image_count = len(image_refs) if isinstance(image_refs, list) else (1 if arguments.get("image_url") else 0)
        detail = " ".join(
            part
            for part in [
                "image generation",
                f"provider={provider}" if provider else "",
                f"model={model}" if model else "",
                f"refs={image_count}",
                f"output={output_dir}" if output_dir else "",
            ]
            if part
        )
        return _mask_sensitive(detail).strip()[:500] or "image generation"
    return _mask_sensitive(json.dumps(arguments, ensure_ascii=False, default=str))[:500]


def _is_trusted_default_chrome_devtools_start(args: Dict[str, Any]) -> bool:
    """Only the built-in CDP MCP command may start without an interactive prompt."""
    if str(args.get("server") or "").strip() != "chrome-devtools":
        return False
    command = os.path.basename(str(args.get("command") or "").strip()).lower()
    if command not in {"npx", "npx.cmd"}:
        return False
    cli_args = args.get("args")
    if not isinstance(cli_args, list):
        return False
    parts = [str(item).strip() for item in cli_args]
    if parts and parts[0] == "-y":
        parts = parts[1:]
    if len(parts) < 4:
        return False
    if parts[0] != "chrome-devtools-mcp@latest":
        return False
    if parts[1] not in {"--browserUrl", "--browser-url"} or parts[2] != _DEFAULT_CDP_ENDPOINT:
        return False
    flags = set(parts[3:])
    trusted_flags = {
        "--no-usage-statistics",
        "--no-performance-crux",
        "--experimentalPageIdRouting",
        "--experimentalDevtools",
        "--experimentalVision",
        "--experimentalStructuredContent",
        "--experimentalIncludeAllPages",
        "--memoryDebugging",
        "--categoryExperimentalThirdParty",
        "--categoryExperimentalWebmcp",
        "--redactNetworkHeaders",
    }
    required_privacy_flags = {
        "--no-usage-statistics",
        "--no-performance-crux",
        "--redactNetworkHeaders",
    }
    return (
        required_privacy_flags.issubset(flags)
        and flags.issubset(trusted_flags)
        and bool(args.get("trusted_default_chrome_devtools"))
    )


def _is_read_only_tongxin_cli_request(args: Dict[str, Any]) -> bool:
    try:
        from agent.tools.tongxin_cli.tongxin_cli import is_read_only_tongxin_request

        return is_read_only_tongxin_request(args)
    except Exception:
        action = str((args or {}).get("action") or "").strip().lower()
        if action in {"status", "schema", "diagnose"}:
            return True
        return False


def _is_tongxin_auto_configure_request(args: Dict[str, Any]) -> bool:
    action = str((args or {}).get("action") or "").strip().lower()
    return action == "configure" and not any((args or {}).get(key) for key in ("script_path", "scriptPath", "path"))


def _is_tongxin_config_driven_bootstrap_request(args: Dict[str, Any]) -> bool:
    try:
        from agent.tools.tongxin_cli.tongxin_cli import is_config_driven_tongxin_bootstrap_request

        return is_config_driven_tongxin_bootstrap_request(args)
    except Exception:
        action = str((args or {}).get("action") or "").strip().lower()
        return action in {"bootstrap", "download"} and not any(
            (args or {}).get(key)
            for key in (
                "url",
                "download_url",
                "downloadUrl",
                "remote_url",
                "remoteUrl",
                "manifest_url",
                "manifestUrl",
                "token",
                "auth_token",
                "authToken",
            )
        )


def _is_tongxin_config_driven_auth_request(args: Dict[str, Any]) -> bool:
    try:
        from agent.tools.tongxin_cli.tongxin_cli import is_config_driven_tongxin_auth_request

        return is_config_driven_tongxin_auth_request(args)
    except Exception:
        action = str((args or {}).get("action") or "").strip().lower().replace("-", "_")
        return action in {"auth", "login", "auto_configure", "auto_config"} and not any(
            (args or {}).get(key)
            for key in (
                "auth_url",
                "authUrl",
                "login_url",
                "loginUrl",
                "remote_auth_url",
                "remoteAuthUrl",
                "url",
                "download_url",
                "downloadUrl",
                "remote_url",
                "remoteUrl",
                "manifest_url",
                "manifestUrl",
                "bootstrap_url",
                "bootstrapUrl",
                "bootstrap_manifest_url",
                "bootstrapManifestUrl",
                "auth_token",
                "authToken",
                "bootstrap_token",
                "bootstrapToken",
                "token",
                "target_dir",
                "targetDir",
                "bootstrap_dir",
                "bootstrapDir",
            )
        )


def _is_read_only_feishu_cli_request(args: Dict[str, Any]) -> bool:
    action = str((args or {}).get("action") or "").strip().lower()
    return action in {"status", "diagnose", "ensure", "config_init_status", "agent_auth_status", "auth_login_status", "auth_status"}


def _is_default_feishu_cli_request(args: Dict[str, Any]) -> bool:
    action = str((args or {}).get("action") or "").strip().lower().replace("-", "_")
    return action in {
        "status",
        "diagnose",
        "ensure",
        "install",
        "agent_auth",
        "agent_authorize",
        "authorize_agent",
        "config_init",
        "config_init_status",
        "agent_auth_status",
        "auth_login_status",
        "auth_status",
        "auth_login",
        "run",
    }


def _is_tongxin_capability_configure_request(tool_name: str, args: Dict[str, Any]) -> bool:
    normalized_tool = str(tool_name or "").strip().lower()
    action = str((args or {}).get("action") or "").strip().lower().replace("-", "_")
    ability = str((args or {}).get("ability") or (args or {}).get("pack_id") or "").strip().lower().replace("_", "-")
    aliases = {"tongxin", "tongxin-cli", "xin-agent", "xin-agent-cli", "tx-assistant"}
    has_explicit_path = any((args or {}).get(key) for key in ("script_path", "scriptPath", "path"))
    if has_explicit_path:
        return False
    if normalized_tool == "optional_abilities":
        return action in {"configure", "install"} and ability in aliases
    if normalized_tool == "agent_capability":
        return action == "install_pack" and ability in aliases
    return False


def _normalize_access(value: Any, default: str = "deny") -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _ACCESS_RANK else default


def _normalize_operation(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return "write" if normalized in {"write", "edit", "delete", "create"} else "read"


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _safe_commonpath(paths) -> str:
    try:
        return os.path.commonpath(paths)
    except Exception:
        return ""


def _path_is_within(child: str, parent: str) -> bool:
    child_norm = _norm_path(child)
    parent_norm = _norm_path(parent)
    return _safe_commonpath([child_norm, parent_norm]) == parent_norm


def _resolve_profile_path(value: str, cwd: Optional[str]) -> str:
    from common.utils import expand_path

    raw = str(value or "").strip()
    if not raw:
        return ""
    expanded = expand_path(raw)
    if os.path.isabs(expanded):
        return os.path.realpath(expanded)
    base = cwd or os.getcwd()
    return os.path.realpath(os.path.join(base, expanded))


def _workspace_roots(profile: Dict[str, Any], cwd: Optional[str]) -> list:
    roots = []
    raw_roots = (
        profile.get("workspaceRoots")
        or profile.get("workspace_roots")
        or profile.get("writable_roots")
        or []
    )
    if isinstance(raw_roots, dict):
        raw_roots = [path for path, enabled in raw_roots.items() if enabled]
    if isinstance(raw_roots, (list, tuple, set)):
        for item in raw_roots:
            resolved = _resolve_profile_path(str(item), cwd)
            if resolved:
                roots.append(resolved)
    if cwd:
        roots.append(os.path.realpath(cwd))

    deduped = []
    seen = set()
    for root in roots:
        key = _norm_path(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def _default_filesystem_profile(cwd: Optional[str]) -> Dict[str, Any]:
    """Conservative fallback when no explicit filesystem profile is saved."""
    roots = []
    try:
        from config import conf

        cfg = conf()
        value = cfg.get("agent_workspace") if hasattr(cfg, "get") else None
        resolved = _resolve_profile_path(str(value), cwd) if value else ""
        if resolved:
            roots.append(resolved)
    except Exception:
        pass
    return {
        "defaultAccess": "deny",
        "workspaceRoots": roots,
        "rules": [
            {"path": ":workspace_roots", "access": "write"},
        ],
    }


def _glob_matches(pattern: str, target_path: str, cwd: Optional[str], roots: list) -> bool:
    normalized_pattern = str(pattern or "").replace("\\", "/").strip()
    if not normalized_pattern:
        return False
    target_abs = _norm_path(target_path).replace("\\", "/")
    candidates = {target_abs, os.path.basename(target_abs)}
    if cwd and _path_is_within(target_path, cwd):
        try:
            candidates.add(os.path.relpath(target_path, cwd).replace("\\", "/"))
        except Exception:
            pass
    for root in roots:
        if _path_is_within(target_path, root):
            try:
                candidates.add(os.path.relpath(target_path, root).replace("\\", "/"))
            except Exception:
                pass
    return any(fnmatch.fnmatchcase(candidate, normalized_pattern) for candidate in candidates)


def _rule_path_candidates(raw_path: str, roots: list, cwd: Optional[str]) -> list:
    token = str(raw_path or "").strip()
    if token in {":workspace", ":workspace_roots", ":cwd"}:
        return list(roots) or ([cwd] if cwd else [])
    resolved = _resolve_profile_path(token, cwd)
    return [resolved] if resolved else []


def _rule_matches(rule: Dict[str, Any], target_path: str, roots: list, cwd: Optional[str]) -> Tuple[bool, int]:
    if not isinstance(rule, dict):
        return False, 0
    glob_pattern = rule.get("glob")
    if glob_pattern:
        pattern = str(glob_pattern)
        if not _glob_matches(pattern, target_path, cwd, roots):
            return False, 0
        root_specificity = 0
        for root in roots:
            if _path_is_within(target_path, root):
                root_specificity = max(root_specificity, len(_norm_path(root)))
        return True, root_specificity + len(pattern)

    raw_path = rule.get("path")
    if raw_path is None:
        return False, 0
    best_specificity = 0
    for candidate in _rule_path_candidates(str(raw_path), roots, cwd):
        if candidate and _path_is_within(target_path, candidate):
            best_specificity = max(best_specificity, len(_norm_path(candidate)))
    return best_specificity > 0, best_specificity


def _allowed_for_operation(access: str, operation: str) -> bool:
    required = 2 if operation == "write" else 1
    return _ACCESS_RANK.get(access, 0) >= required


class ToolPermissionBroker:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._decisions: Dict[str, Decision] = {}

    def authorize(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: Optional[Dict[str, Any]],
        emit_event: Optional[Emitter] = None,
        cancel_event: Any = None,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> Decision:
        normalized_tool = (tool_name or "").strip().lower()
        args = arguments if isinstance(arguments, dict) else {}
        if normalized_tool == "optional_abilities" and str(args.get("action") or "").strip().lower() in {"list", "status"}:
            return {"allowed": True, "reason": "read-only-optional-ability-status"}
        if normalized_tool == "agent_capability" and str(args.get("action") or "").strip().lower() in {"list_packs", "diagnose"}:
            return {"allowed": True, "reason": "read-only-agent-capability-status"}
        if normalized_tool == "tongxin_cli" and _is_tongxin_auto_configure_request(args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-tongxin-cli-auto-config"})
            return {"allowed": True, "reason": "default-tongxin-cli-auto-config"}
        if normalized_tool == "tongxin_cli" and _is_tongxin_config_driven_bootstrap_request(args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-tongxin-cli-authenticated-bootstrap"})
            return {"allowed": True, "reason": "default-tongxin-cli-authenticated-bootstrap"}
        if normalized_tool == "tongxin_cli" and _is_tongxin_config_driven_auth_request(args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-tongxin-cli-configured-auth"})
            return {"allowed": True, "reason": "default-tongxin-cli-configured-auth"}
        if normalized_tool == "tongxin_cli" and _is_read_only_tongxin_cli_request(args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-read-only-tongxin-cli"})
            return {"allowed": True, "reason": "default-read-only-tongxin-cli"}
        if _is_tongxin_capability_configure_request(normalized_tool, args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-tongxin-cli-auto-config"})
            return {"allowed": True, "reason": "default-tongxin-cli-auto-config"}
        if normalized_tool == "feishu_cli" and _is_read_only_feishu_cli_request(args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-read-only-feishu-cli"})
            return {"allowed": True, "reason": "default-read-only-feishu-cli"}
        if normalized_tool == "feishu_cli" and _is_default_feishu_cli_request(args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-structured-feishu-cli"})
            return {"allowed": True, "reason": "default-structured-feishu-cli"}
        if not self._requires_permission(normalized_tool):
            return {"allowed": True, "reason": "not-required"}

        settings = self._load_settings()
        mode = str(settings.get("mode") or "smart-ask")
        grant_key = self._grant_key(normalized_tool)

        if mode == "full-access":
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "full-access"})
            return {"allowed": True, "reason": "full-access"}

        if mode == "read-only":
            self._audit("tool-execution", "deny", {"tool": normalized_tool, "reason": "read-only"})
            return {"allowed": False, "reason": "Current read-only mode blocks local tool execution."}

        if settings.get("alwaysAllow", {}).get(grant_key):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "remembered-grant"})
            return {"allowed": True, "reason": "remembered-grant"}

        if not (emit_event or self._interactive_permission_available()):
            summary = _summarize_args(normalized_tool, args)
            self._audit(
                "tool-execution",
                "deny",
                {
                    "tool": normalized_tool,
                    "reason": "interactive-confirmation-unavailable",
                    "summary": summary,
                },
            )
            return {
                "allowed": False,
                "reason": "Interactive permission confirmation is unavailable in this runtime; switch to full-access explicitly or use a Web/Desktop surface.",
            }

        request_id = uuid.uuid4().hex
        summary = _summarize_args(normalized_tool, args)
        request = {
            "id": request_id,
            "tool": normalized_tool,
            "tool_call_id": tool_call_id,
            "summary": summary,
            "title": self._title_for_tool(normalized_tool),
            "message": self._message_for_tool(normalized_tool, summary),
            "created_at": _now(),
            "mode": mode,
        }
        with self._condition:
            self._pending[request_id] = request

        if emit_event:
            emit_event("tool_permission_request", request)

        deadline = time.time() + max(1, timeout_seconds)
        decision: Optional[Decision] = None
        while time.time() < deadline:
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                decision = {
                    "allowed": False,
                    "reason": "User stopped the current task.",
                    "cancelled": True,
                }
                break
            with self._condition:
                if request_id in self._decisions:
                    decision = self._decisions.pop(request_id)
                    break
                self._condition.wait(timeout=0.25)

        if decision is None:
            decision = {"allowed": False, "reason": "Permission confirmation timed out; tool was not executed."}

        with self._condition:
            self._pending.pop(request_id, None)

        allowed = bool(decision.get("allowed"))
        if allowed and decision.get("remember"):
            settings.setdefault("alwaysAllow", {})[grant_key] = True
            settings["updatedAt"] = _now()
            self._save_settings(settings)

        self._audit(
            "tool-execution",
            "allow" if allowed else "deny",
            {
                "tool": normalized_tool,
                "requestId": request_id,
                "reason": decision.get("reason", ""),
                "remember": bool(decision.get("remember")),
            },
        )
        return decision

    def list_pending(self) -> Dict[str, Any]:
        with self._condition:
            pending = list(self._pending.values())
        return {"status": "success", "pending": pending, **self.get_state()}

    def get_state(self) -> Dict[str, Any]:
        settings = self._load_settings()
        return {
            "mode": settings.get("mode", "smart-ask"),
            "grantsCount": len(settings.get("alwaysAllow") or {}),
            "auditPath": str(self._audit_path()),
            "updatedAt": settings.get("updatedAt"),
        }

    def list_workspace_roots(self, cwd: Optional[str] = None) -> list:
        settings = self._load_settings()
        profile = settings.get("filesystem")
        if not isinstance(profile, dict):
            profile = _default_filesystem_profile(cwd)
        return _workspace_roots(profile, cwd)

    def authorize_noninteractive(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Decision:
        """Authorize background startup work that cannot surface a UI prompt."""
        normalized_tool = (tool_name or "").strip().lower()
        args = arguments if isinstance(arguments, dict) else {}
        if normalized_tool == "optional_abilities" and str(args.get("action") or "").strip().lower() in {"list", "status"}:
            return {"allowed": True, "reason": "read-only-optional-ability-status"}
        if normalized_tool == "agent_capability" and str(args.get("action") or "").strip().lower() in {"list_packs", "diagnose"}:
            return {"allowed": True, "reason": "read-only-agent-capability-status"}
        if normalized_tool == "tongxin_cli" and _is_tongxin_auto_configure_request(args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-tongxin-cli-auto-config"})
            return {"allowed": True, "reason": "default-tongxin-cli-auto-config"}
        if normalized_tool == "tongxin_cli" and _is_tongxin_config_driven_bootstrap_request(args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-tongxin-cli-authenticated-bootstrap"})
            return {"allowed": True, "reason": "default-tongxin-cli-authenticated-bootstrap"}
        if normalized_tool == "tongxin_cli" and _is_tongxin_config_driven_auth_request(args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-tongxin-cli-configured-auth"})
            return {"allowed": True, "reason": "default-tongxin-cli-configured-auth"}
        if normalized_tool == "tongxin_cli" and _is_read_only_tongxin_cli_request(args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-read-only-tongxin-cli"})
            return {"allowed": True, "reason": "default-read-only-tongxin-cli"}
        if _is_tongxin_capability_configure_request(normalized_tool, args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-tongxin-cli-auto-config"})
            return {"allowed": True, "reason": "default-tongxin-cli-auto-config"}
        if normalized_tool == "feishu_cli" and _is_read_only_feishu_cli_request(args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-read-only-feishu-cli"})
            return {"allowed": True, "reason": "default-read-only-feishu-cli"}
        if normalized_tool == "feishu_cli" and _is_default_feishu_cli_request(args):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "default-structured-feishu-cli"})
            return {"allowed": True, "reason": "default-structured-feishu-cli"}
        if not self._requires_permission(normalized_tool):
            return {"allowed": True, "reason": "not-required"}

        settings = self._load_settings()
        mode = str(settings.get("mode") or "smart-ask")
        grant_key = self._grant_key(normalized_tool)

        if mode == "read-only":
            self._audit("tool-execution", "deny", {"tool": normalized_tool, "reason": "read-only"})
            return {"allowed": False, "reason": "Current read-only mode blocks local tool execution."}

        if normalized_tool == "browser" and _is_trusted_default_chrome_devtools_start(args):
            self._audit(
                "tool-execution",
                "allow",
                {
                    "tool": normalized_tool,
                    "reason": "default-cdp-mcp-startup",
                    "server": "chrome-devtools",
                },
            )
            return {"allowed": True, "reason": "default-cdp-mcp-startup"}

        if mode == "full-access":
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "full-access"})
            return {"allowed": True, "reason": "full-access"}
        if settings.get("alwaysAllow", {}).get(grant_key):
            self._audit("tool-execution", "allow", {"tool": normalized_tool, "reason": "remembered-grant"})
            return {"allowed": True, "reason": "remembered-grant"}

        summary = _summarize_args(normalized_tool, args)
        self._audit(
            "tool-execution",
            "deny",
            {
                "tool": normalized_tool,
                "reason": "interactive-confirmation-required",
                "summary": summary,
            },
        )
        return {
            "allowed": False,
            "reason": "Interactive permission confirmation is required before this external capability can start.",
        }

    def set_mode(self, mode: str) -> Dict[str, Any]:
        normalized = self._normalize_mode(mode)
        settings = self._load_settings()
        settings["mode"] = normalized
        settings["updatedAt"] = _now()
        self._save_settings(settings)
        self._audit("permission.mode.update", "allow", {"mode": normalized})
        return {"status": "success", **self.get_state()}

    def reset_grants(self) -> Dict[str, Any]:
        settings = self._load_settings()
        settings["alwaysAllow"] = {}
        settings["updatedAt"] = _now()
        self._save_settings(settings)
        self._audit("permission.grants.reset", "allow", {"mode": settings.get("mode", "smart-ask")})
        return {"status": "success", **self.get_state()}

    def is_read_only(self) -> bool:
        return str(self._load_settings().get("mode") or "smart-ask") == "read-only"

    def authorize_file_access(
        self,
        operation: str,
        path: str,
        cwd: Optional[str] = None,
    ) -> Decision:
        """Authorize local filesystem access using the active EcoreX profile.

        This is intentionally narrower than the shell/browser approval broker:
        file tools call it directly, and Web file serving uses the same decision
        so local file reads are not governed by a separate ad-hoc path policy.
        ``full-access`` keeps its explicit whole-host behavior. Other modes use
        a conservative workspace-scoped fallback when no saved filesystem
        profile exists; ``custom`` without a profile still fails closed.
        """
        op = _normalize_operation(operation)
        if not path:
            return {"allowed": False, "reason": "Missing local file path."}

        settings = self._load_settings()
        mode = str(settings.get("mode") or "smart-ask")
        if mode == "read-only" and op == "write":
            self._audit("filesystem-access", "deny", {
                "operation": op,
                "path": _mask_sensitive(path),
                "reason": "read-only",
            })
            return {"allowed": False, "reason": "Current read-only mode blocks local file writes."}

        profile = settings.get("filesystem")
        if not isinstance(profile, dict):
            if mode == "full-access":
                self._audit("filesystem-access", "allow", {
                    "operation": op,
                    "path": _mask_sensitive(path),
                    "reason": "full-access-no-filesystem-profile",
                })
                return {"allowed": True, "reason": "full-access"}
            if mode == "custom":
                self._audit("filesystem-access", "deny", {
                    "operation": op,
                    "path": _mask_sensitive(path),
                    "reason": "missing-custom-filesystem-profile",
                })
                return {
                    "allowed": False,
                    "reason": "Custom permission mode requires a filesystem profile before local files can be accessed.",
                }
            profile = _default_filesystem_profile(cwd)

        decision = self._evaluate_filesystem_profile(profile, path, op, cwd)
        self._audit("filesystem-access", "allow" if decision.get("allowed") else "deny", {
            "operation": op,
            "path": _mask_sensitive(path),
            "reason": decision.get("reason", ""),
            "access": decision.get("access", ""),
        })
        return decision

    def remember_workspace_root(self, path: str, access: str = "write", cwd: Optional[str] = None) -> Dict[str, Any]:
        root = _resolve_profile_path(path, cwd)
        if not root:
            return {"status": "error", "message": "path is required"}
        if not os.path.isdir(root):
            return {"status": "error", "message": f"path is not a directory: {root}"}

        settings = self._load_settings()
        profile = settings.get("filesystem")
        if not isinstance(profile, dict):
            profile = _default_filesystem_profile(cwd)
        roots = profile.get("workspaceRoots") or profile.get("workspace_roots") or []
        if not isinstance(roots, list):
            roots = []
        root_real = os.path.realpath(root)
        root_key = _norm_path(root_real)
        normalized_roots = []
        seen = set()
        for item in roots:
            resolved = _resolve_profile_path(str(item), cwd)
            if not resolved:
                continue
            key = _norm_path(resolved)
            if key not in seen:
                seen.add(key)
                normalized_roots.append(resolved)
        if root_key not in seen:
            normalized_roots.append(root_real)
        profile["workspaceRoots"] = normalized_roots
        profile.setdefault("defaultAccess", "deny")
        rules = profile.get("rules")
        if not isinstance(rules, list):
            rules = []
        workspace_rule = next((rule for rule in rules if isinstance(rule, dict) and rule.get("path") == ":workspace_roots"), None)
        normalized_access = _normalize_access(access, "write")
        if workspace_rule is not None:
            current_access = _normalize_access(workspace_rule.get("access"), "deny")
            if _ACCESS_RANK.get(current_access, 0) < _ACCESS_RANK.get(normalized_access, 2):
                workspace_rule["access"] = normalized_access
        else:
            rules.append({"path": ":workspace_roots", "access": _normalize_access(access, "write")})
        profile["rules"] = rules
        settings["filesystem"] = profile
        settings["updatedAt"] = _now()
        self._save_settings(settings)
        self._audit("filesystem-profile", "allow", {
            "operation": "remember-workspace-root",
            "path": _mask_sensitive(root_real),
            "access": _normalize_access(access, "write"),
        })
        return {"status": "success", "path": root_real, **self.get_state()}

    def decide(self, request_id: str, decision: str, remember: bool = False) -> Dict[str, Any]:
        normalized = (decision or "").strip().lower()
        if normalized in {"allow", "allow_once", "always_allow"}:
            payload: Decision = {
                "allowed": True,
                "reason": "user-allowed" if normalized != "always_allow" else "user-allowed-always",
                "remember": remember or normalized == "always_allow",
            }
        elif normalized in {"deny", "reject"}:
            payload = {"allowed": False, "reason": "User denied this local tool execution."}
        else:
            return {"status": "error", "message": "invalid permission decision"}

        with self._condition:
            if request_id not in self._pending:
                return {"status": "error", "message": "permission request is no longer pending"}
            self._decisions[request_id] = payload
            self._condition.notify_all()
        return {"status": "success", "request_id": request_id, "allowed": payload["allowed"]}

    def _evaluate_filesystem_profile(
        self,
        profile: Dict[str, Any],
        path: str,
        operation: str,
        cwd: Optional[str],
    ) -> Decision:
        target_path = os.path.realpath(path)
        roots = _workspace_roots(profile, cwd)
        default_access = _normalize_access(
            profile.get("defaultAccess", profile.get("default", "deny")),
            "deny",
        )
        best_access = default_access
        best_specificity = -1
        best_tiebreak = _ACCESS_TIEBREAK.get(best_access, 0)

        rules = profile.get("rules")
        if not isinstance(rules, list):
            rules = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            matched, specificity = _rule_matches(rule, target_path, roots, cwd)
            if not matched:
                continue
            access = _normalize_access(rule.get("access"), "deny")
            tiebreak = _ACCESS_TIEBREAK.get(access, 0)
            if specificity > best_specificity or (
                specificity == best_specificity and tiebreak > best_tiebreak
            ):
                best_access = access
                best_specificity = specificity
                best_tiebreak = tiebreak

        allowed = _allowed_for_operation(best_access, operation)
        if allowed:
            return {
                "allowed": True,
                "reason": f"filesystem profile allows {operation}",
                "access": best_access,
            }
        return {
            "allowed": False,
            "reason": (
                f"Filesystem profile blocks {operation} access to this path "
                f"(effective access: {best_access})."
            ),
            "access": best_access,
        }

    def _requires_permission(self, tool_name: str) -> bool:
        return (tool_name or "").strip().lower() in _DANGEROUS_TOOLS

    @staticmethod
    def _interactive_permission_available() -> bool:
        explicit = str(os.environ.get("ECOREX_TOOL_PERMISSION_INTERACTIVE") or "").strip().lower()
        if explicit in {"1", "true", "yes", "on"}:
            return True
        if explicit in {"0", "false", "no", "off"}:
            return False
        if os.environ.get("ECOREX_DESKTOP") == "1":
            return True
        if os.environ.get("ECOREX_WEB_PORT") or os.environ.get("ECOREX_WEB_PUBLIC_BASE_URL"):
            return True
        try:
            from config import conf

            channel_type = str(conf().get("channel_type", "") or "").lower()
            return "web" in channel_type
        except Exception:
            return False

    def _user_data_dir(self) -> Path:
        configured = os.environ.get("ECOREX_DESKTOP_USER_DATA") or os.environ.get("ECOREX_USER_DATA")
        if configured:
            return Path(configured)
        try:
            from config import conf, get_appdata_dir

            if conf().get("appdata_dir"):
                return Path(get_appdata_dir()) / "permissions"
        except Exception:
            pass
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA", str(Path.home())))
            return base / "EcoreX" / "permissions"
        return Path.home() / ".config" / "ecorex" / "permissions"

    def _settings_path(self) -> Path:
        return self._user_data_dir() / "permissions.json"

    def _audit_path(self) -> Path:
        return self._user_data_dir() / "permission-audit.jsonl"

    def _load_settings(self) -> Dict[str, Any]:
        data = _read_json(self._settings_path(), {})
        data["mode"] = self._normalize_mode(data.get("mode"))
        always_allow = data.get("alwaysAllow")
        data["alwaysAllow"] = always_allow if isinstance(always_allow, dict) else {}
        return data

    def _save_settings(self, settings: Dict[str, Any]) -> None:
        settings["mode"] = self._normalize_mode(settings.get("mode"))
        settings.setdefault("alwaysAllow", {})
        _write_json(self._settings_path(), settings)

    def _audit(self, action: str, decision: str, detail: Dict[str, Any]) -> None:
        try:
            path = self._audit_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "createdAt": _now(),
                "action": action,
                "decision": decision,
                "detail": detail,
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def _normalize_mode(mode: Any) -> str:
        normalized = str(mode or "").strip().lower()
        return normalized if normalized in _ALLOWED_MODES else "smart-ask"

    @staticmethod
    def _grant_key(tool_name: str) -> str:
        return f"tool-execution:{(tool_name or '').strip().lower()}"

    @staticmethod
    def _title_for_tool(tool_name: str) -> str:
        if tool_name == "browser":
            return "Browser automation confirmation"
        if tool_name == "feishu_cli":
            return "Feishu CLI access confirmation"
        if tool_name == "tongxin_cli":
            return "Tongxin CLI access confirmation"
        if tool_name == "optional_abilities":
            return "Optional ability enablement confirmation"
        if tool_name == "agent_capability":
            return "Agent capability installation confirmation"
        if tool_name in {"mcp", "mcp_server"}:
            return "MCP external capability confirmation"
        if tool_name in {"write", "edit", "fs_write"}:
            return "File write confirmation"
        if tool_name == "skill_write":
            return "Skill mutation confirmation"
        if tool_name == "env_config":
            return "Environment configuration confirmation"
        if tool_name == "send":
            return "File send confirmation"
        if tool_name == "scheduler":
            return "Scheduled task confirmation"
        if tool_name == "evolution_undo":
            return "Self-evolution rollback confirmation"
        if tool_name in {"web_fetch", "web_search"}:
            return "Internet access confirmation"
        if tool_name == "vision":
            return "Image analysis confirmation"
        if tool_name == "imagegen":
            return "Image generation confirmation"
        return "Local command confirmation"

    @staticmethod
    def _message_for_tool(tool_name: str, summary: str) -> str:
        if tool_name == "browser":
            return f"EcoreX wants to control the browser: {summary}"
        if tool_name == "feishu_cli":
            return f"EcoreX wants to access Feishu through lark-cli: {summary}"
        if tool_name == "tongxin_cli":
            return f"EcoreX wants to query Tongxin Assistant read-only account data: {summary}"
        if tool_name == "optional_abilities":
            return f"EcoreX wants to enable or install an optional ability: {summary}"
        if tool_name == "agent_capability":
            return f"EcoreX wants the agent to install or configure a capability: {summary}"
        if tool_name in {"mcp", "mcp_server"}:
            return f"EcoreX wants to start or call an MCP external capability: {summary}"
        if tool_name in {"write", "edit", "fs_write"}:
            return f"EcoreX wants to write a local file: {summary}"
        if tool_name == "skill_write":
            return f"EcoreX wants to modify installed skills: {summary}"
        if tool_name == "env_config":
            return f"EcoreX wants to modify or read environment configuration: {summary}"
        if tool_name == "send":
            return f"EcoreX wants to send a local file: {summary}"
        if tool_name == "scheduler":
            return f"EcoreX wants to create or modify a scheduled background task: {summary}"
        if tool_name == "evolution_undo":
            return f"EcoreX wants to restore memory or skill files from an evolution backup: {summary}"
        if tool_name == "web_fetch":
            return f"EcoreX wants to fetch content from the internet: {summary}"
        if tool_name == "web_search":
            return f"EcoreX wants to search the internet: {summary}"
        if tool_name == "vision":
            return f"EcoreX wants to analyze an image using a model API: {summary}"
        if tool_name == "imagegen":
            return f"EcoreX wants to generate or edit images and write local image files: {summary}"
        return f"EcoreX wants to run a local shell command: {summary}"


_BROKER = ToolPermissionBroker()


def get_tool_permission_broker() -> ToolPermissionBroker:
    return _BROKER
