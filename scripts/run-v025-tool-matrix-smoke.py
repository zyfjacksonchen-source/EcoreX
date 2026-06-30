#!/usr/bin/env python3
"""Run the v0.2.5 EcoreX-native tool matrix smoke."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


MATRIX_SCHEMA = "v0.2.5-tool-matrix-smoke-v1"
ARTIFACT_PATH = ROOT / "docs" / "v0.2.5" / "artifacts" / "v0.2.5-tool-matrix-smoke.json"


TOOL_SMOKES: tuple[dict[str, Any], ...] = (
    {"id": "office-documents", "tool": "office_documents", "args": {"action": "probe"}, "required": True},
    {"id": "office-pdf", "tool": "office_pdf", "args": {"action": "probe"}, "required": True},
    {"id": "office-presentations", "tool": "office_presentations", "args": {"action": "probe"}, "required": True},
    {"id": "office-spreadsheets", "tool": "office_spreadsheets", "args": {"action": "probe"}, "required": True},
    {"id": "imagegen", "tool": "imagegen", "args": {"action": "probe"}, "required": True},
    {"id": "feishu-canary", "tool": "feishu_cli", "args": {"action": "status", "timeout": 5}, "required": True, "canary": True},
    {"id": "tongxin-canary", "tool": "tongxin_cli", "args": {"action": "status", "timeout": 5, "include_paths": False}, "required": True, "canary": True},
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_token(value: Any, limit: int = 96) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if any(secret in text.lower() for secret in ("token", "secret", "password", "authorization", "cookie")):
        return "[redacted]"
    text = text.replace("\\", "/")
    if ":/" in text or text.startswith("/") or "/users/" in text.lower() or "/.codex/" in text.lower():
        return "[path-redacted]"
    return text[:limit]


def _payload_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"payloadType": type(payload).__name__, "redacted": True}
    summary: dict[str, Any] = {
        "payloadKeys": sorted(str(key) for key in payload.keys())[:40],
        "redacted": True,
    }
    for key in (
        "status",
        "available",
        "configured",
        "authenticated",
        "authState",
        "configurationState",
        "scriptPresent",
        "qualityRuntimePresent",
        "providerConfigured",
        "artifactKind",
        "compatibilityId",
        "officialSkill",
        "schemaVersion",
    ):
        if key in payload:
            value = payload.get(key)
            summary[key] = _safe_token(value) if isinstance(value, str) else value
    runtime = payload.get("runtime")
    if isinstance(runtime, Mapping):
        for key in ("parseStatus", "writeStatus", "renderStatus"):
            if key in runtime:
                summary[key] = _safe_token(runtime.get(key))
    missing = payload.get("missing")
    if isinstance(missing, list):
        summary["missing"] = [_safe_token(item) for item in missing[:12]]
    return summary


def _tool_names(manager: Any) -> set[str]:
    names = {str(name) for name in getattr(manager, "tool_classes", {}).keys()}
    names.update(str(name) for name in getattr(manager, "_mcp_tool_instances", {}).keys())
    return names


def _smoke_tool(manager: Any, spec: Mapping[str, Any], tool_names: set[str]) -> dict[str, Any]:
    tool_name = str(spec.get("tool") or "")
    started = time.monotonic()
    if tool_name not in tool_names:
        return {
            "id": spec.get("id"),
            "tool": tool_name,
            "status": "fail" if spec.get("required") else "skip",
            "reason": "tool_not_loaded",
            "durationMs": int((time.monotonic() - started) * 1000),
            "redacted": True,
        }
    tool = manager.create_tool(tool_name)
    if not tool:
        return {
            "id": spec.get("id"),
            "tool": tool_name,
            "status": "fail" if spec.get("required") else "skip",
            "reason": "tool_create_failed",
            "durationMs": int((time.monotonic() - started) * 1000),
            "redacted": True,
        }
    try:
        result = tool.execute(dict(spec.get("args") or {}))
        payload = getattr(result, "result", result)
        tool_status = getattr(result, "status", "unknown")
        status = "pass" if tool_status == "success" else "fail"
        return {
            "id": spec.get("id"),
            "tool": tool_name,
            "status": status,
            "toolResultStatus": tool_status,
            "summary": _payload_summary(payload),
            "durationMs": int((time.monotonic() - started) * 1000),
            "canary": bool(spec.get("canary")),
            "redacted": True,
        }
    except Exception as exc:
        return {
            "id": spec.get("id"),
            "tool": tool_name,
            "status": "fail",
            "reason": "exception",
            "errorType": exc.__class__.__name__,
            "durationMs": int((time.monotonic() - started) * 1000),
            "redacted": True,
        }


def run_worker(environment_name: str) -> dict[str, Any]:
    from agent.tools.tool_manager import ToolManager

    started = time.monotonic()
    manager = ToolManager()
    manager.tool_classes = {}
    manager._mcp_tool_instances = {}
    manager._mcp_status = {}
    manager._mcp_active_configs = {}
    manager._mcp_loaded = False
    manager.load_tools(start_mcp=False)
    names = _tool_names(manager)

    smokes = [_smoke_tool(manager, spec, names) for spec in TOOL_SMOKES]
    browser_schema = {
        "id": "browser-schema",
        "tool": "browser",
        "status": "pass" if "browser" in names else "fail",
        "schemaVisible": "browser" in names,
        "redacted": True,
    }
    mcp_status = manager.list_mcp_status()
    mcp_schema = {
        "id": "mcp-discovery",
        "tool": "mcp",
        "status": "pass",
        "configuredCount": len(getattr(manager, "_mcp_active_configs", {}) or {}),
        "statusCount": len(mcp_status or {}),
        "autoStarted": False,
        "redacted": True,
    }
    smokes.extend([browser_schema, mcp_schema])

    failed = [item for item in smokes if item.get("status") == "fail"]
    worker_status = "pass" if not failed else "fail"
    production_verification = None
    if environment_name == "production-service-user":
        production_verification = _production_identity_verification()
        if not production_verification.get("ok"):
            worker_status = "fail"
            failed.append({
                "id": "production-identity",
                "status": "fail",
                "reason": "production_identity_not_verified",
                "redacted": True,
            })
    payload = {
        "schemaVersion": MATRIX_SCHEMA,
        "environment": environment_name,
        "status": worker_status,
        "failedCount": len(failed),
        "toolCount": len(names),
        "smokes": smokes,
        "durationMs": int((time.monotonic() - started) * 1000),
        "redacted": True,
    }
    if production_verification is not None:
        payload["productionVerification"] = production_verification
    return payload


def _production_identity_verification() -> dict[str, Any]:
    install_root = Path(os.environ.get("ECOREX_INSTALL_ROOT") or "/opt/ecorex-web").expanduser()
    try:
        executable = Path(sys.executable).resolve()
    except Exception:
        executable = Path(str(sys.executable or ""))
    user, user_source = _effective_user_name()
    executable_text = str(executable).replace("\\", "/").lower()
    root_text = str(install_root.resolve() if install_root.exists() else install_root).replace("\\", "/").lower().rstrip("/")
    venv = os.environ.get("VIRTUAL_ENV") or ""
    venv_text = str(Path(venv).expanduser()).replace("\\", "/").lower() if venv else ""
    python_under_install_root = bool(root_text and executable_text.startswith(root_text + "/"))
    venv_under_install_root = bool(root_text and venv_text.startswith(root_text + "/"))
    effective_user_ok = user == "ecorex"
    return {
        "ok": bool(effective_user_ok and python_under_install_root),
        "effectiveUserOk": effective_user_ok,
        "effectiveUser": "ecorex" if effective_user_ok else "not-ecorex",
        "effectiveUserSource": user_source,
        "pythonUnderInstallRoot": python_under_install_root,
        "venvUnderInstallRoot": venv_under_install_root,
        "installRootExpected": "/opt/ecorex-web" if str(install_root).replace("\\", "/") == "/opt/ecorex-web" else "[configured-install-root]",
        "redacted": True,
    }


def _effective_user_name() -> tuple[str, str]:
    if hasattr(os, "geteuid"):
        try:
            import pwd

            return pwd.getpwuid(os.geteuid()).pw_name, "posix-euid"
        except Exception:
            return "", "posix-euid-unavailable"
    try:
        return getpass.getuser(), "platform-user"
    except Exception:
        return "", "platform-user-unavailable"


def _run_child(environment_name: str, env_patch: Mapping[str, str]) -> dict[str, Any]:
    env = os.environ.copy()
    env.update({str(key): str(value) for key, value in env_patch.items()})
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", environment_name]
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    stdout = completed.stdout or ""
    json_text = stdout
    if not json_text.lstrip().startswith("{"):
        start = json_text.find("{")
        end = json_text.rfind("}")
        json_text = json_text[start : end + 1] if start >= 0 and end >= start else json_text
    try:
        payload = json.loads(json_text)
    except Exception:
        payload = {
            "schemaVersion": MATRIX_SCHEMA,
            "environment": environment_name,
            "status": "fail",
            "reason": "worker_json_parse_failed",
            "stdoutPresent": bool(completed.stdout.strip()),
            "stderrPresent": bool(completed.stderr.strip()),
            "redacted": True,
        }
    payload["workerExitCode"] = completed.returncode
    payload["workerDurationMs"] = int((time.monotonic() - started) * 1000)
    if completed.returncode != 0 and payload.get("status") == "pass":
        payload["status"] = "fail"
        payload["reason"] = "worker_exit_nonzero"
    return payload


def _production_service_probe() -> dict[str, Any]:
    if os.name == "nt":
        return {
            "schemaVersion": MATRIX_SCHEMA,
            "environment": "production-service-user",
            "status": "skipped",
            "reason": "linux_service_user_probe_not_applicable_on_windows",
            "redacted": True,
        }
    if not Path("/opt/ecorex-web").exists():
        return {
            "schemaVersion": MATRIX_SCHEMA,
            "environment": "production-service-user",
            "status": "skipped",
            "reason": "production_install_root_missing",
            "redacted": True,
        }
    return _run_child("production-service-user", {"ECOREX_INSTALL_ROOT": "/opt/ecorex-web"})


def run_matrix(include_production: bool = True) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ecorex-v025-tool-matrix-") as tmp:
        temp = Path(tmp)
        variants = [
            ("current", {}),
            ("clean-path", {"PATH": ""}),
            (
                "clean-user-state",
                {
                    "ECOREX_CAPABILITY_STATE_DIR": str(temp / "capability-state"),
                    "HOME": str(temp / "home"),
                    "USERPROFILE": str(temp / "home"),
                    "LOCALAPPDATA": str(temp / "localappdata"),
                    "APPDATA": str(temp / "appdata"),
                },
            ),
        ]
        environments = [_run_child(name, patch) for name, patch in variants]
        if include_production:
            environments.append(_production_service_probe())
    failures = [item for item in environments if item.get("status") == "fail"]
    return {
        "schemaVersion": MATRIX_SCHEMA,
        "status": "pass" if not failures else "fail",
        "generatedAt": _now(),
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "redacted": True,
        },
        "environments": environments,
        "failedEnvironmentCount": len(failures),
        "redacted": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--output", default=str(ARTIFACT_PATH), help="Evidence JSON output path.")
    parser.add_argument("--no-production", action="store_true", help="Skip production service-user probe entry.")
    parser.add_argument("--json", action="store_true", help="Print JSON evidence.")
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(run_worker(args.worker), ensure_ascii=False, indent=2))
        return 0

    payload = run_matrix(include_production=not args.no_production)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload["status"] == "pass":
        print(f"PASS: v0.2.5 tool matrix smoke ({output})")
    else:
        print(f"FAIL: v0.2.5 tool matrix smoke ({output})")
        for item in payload.get("environments") or []:
            if item.get("status") == "fail":
                print(f"- {item.get('environment')}: failedCount={item.get('failedCount')}")
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
