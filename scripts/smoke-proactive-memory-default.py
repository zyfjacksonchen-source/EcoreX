#!/usr/bin/env python3
"""Verify EcoreX proactive memory is enabled with production guards."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any


def add_check(checks: list[dict[str, Any]], name: str, ok: bool, evidence: str) -> None:
    checks.append({
        "name": name,
        "status": "pass" if ok else "fail",
        "evidence": evidence,
    })


def read_text_no_bom(path: pathlib.Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{path} has a UTF-8 BOM")
    return raw.decode("utf-8")


def read_json_no_bom(path: pathlib.Path) -> Any:
    return json.loads(read_text_no_bom(path))


def check_json_runtime_defaults(path: pathlib.Path, checks: list[dict[str, Any]], prefix: str) -> None:
    payload = read_json_no_bom(path)
    tools = payload.get("tools") if isinstance(payload.get("tools"), dict) else {}
    feishu_cli = tools.get("feishu_cli") if isinstance(tools.get("feishu_cli"), dict) else {}
    add_check(checks, f"{prefix} self-evolution enabled", payload.get("self_evolution_enabled") is True, str(path))
    add_check(checks, f"{prefix} scheduler remains disabled", payload.get("scheduler_enabled") is False, str(path))
    add_check(checks, f"{prefix} MCP auto-start remains disabled", payload.get("mcp_auto_start") is False, str(path))
    add_check(
        checks,
        f"{prefix} Feishu CLI remains on-demand",
        feishu_cli.get("auto_install") is False,
        json.dumps(feishu_cli, ensure_ascii=False, sort_keys=True),
    )


def check_source_defaults(root: pathlib.Path, checks: list[dict[str, Any]]) -> None:
    check_json_runtime_defaults(root / "config-template.json", checks, "root config-template")

    sys.path.insert(0, str(root))
    from agent.evolution.config import DEFAULT_ENABLED, EvolutionConfig

    add_check(checks, "evolution module default enabled", DEFAULT_ENABLED is True, f"DEFAULT_ENABLED={DEFAULT_ENABLED}")
    cfg = EvolutionConfig()
    add_check(
        checks,
        "evolution trigger thresholds conservative",
        cfg.enabled is True and cfg.idle_minutes >= 10 and cfg.min_turns >= 6 and cfg.max_steps <= 12,
        json.dumps({
            "enabled": cfg.enabled,
            "idleMinutes": cfg.idle_minutes,
            "minTurns": cfg.min_turns,
            "maxSteps": cfg.max_steps,
        }, sort_keys=True),
    )

    config_py = read_text_no_bom(root / "config.py")
    add_check(
        checks,
        "config.py fallback enables proactive memory",
        '"self_evolution_enabled": True' in config_py and 'cfg["self_evolution_enabled"] = True' in config_py,
        "config.py default and minimal-config fallback",
    )

    sidecar_ts = read_text_no_bom(root / "desktop" / "electron" / "sidecar.ts")
    add_check(
        checks,
        "desktop sidecar default enables proactive memory",
        "self_evolution_enabled: true" in sidecar_ts
        and "scheduler_enabled: false" in sidecar_ts
        and "mcp_auto_start: false" in sidecar_ts
        and "auto_install = false" in sidecar_ts,
        "desktop/electron/sidecar.ts desktop runtime defaults",
    )

    webui_script = read_text_no_bom(root / "scripts" / "prepare-ecorex-webui-local-release.ps1")
    add_check(
        checks,
        "WebUI local release enables proactive memory",
        "self_evolution_enabled = $true" in webui_script
        and '"self_evolution_enabled": True' in webui_script
        and "scheduler_enabled = $false" in webui_script
        and '"scheduler_enabled": False' in webui_script,
        "scripts/prepare-ecorex-webui-local-release.ps1",
    )


class FakeAgent:
    def __init__(self) -> None:
        self._evo_last_active = time.time() - 120
        self._evo_turns = 6
        self._evo_channel_type = "web"
        self._evo_receiver = "session-a"
        self.messages = [{"role": "user", "content": "remember my office workflow"}]


class FakeBridge:
    def __init__(self) -> None:
        self.agents = {"session-a": FakeAgent()}
        self.default_agent = None


def check_trigger_behavior(root: pathlib.Path, checks: list[dict[str, Any]]) -> None:
    sys.path.insert(0, str(root))
    import agent.evolution.trigger as trigger
    from agent.evolution.config import EvolutionConfig

    bridge = FakeBridge()
    calls: list[tuple[str, str, str]] = []
    original_runner = trigger.run_evolution_for_session
    try:
        def fake_runner(agent_bridge, session_id: str, channel_type: str = "", receiver: str = "", **_: Any) -> bool:
            calls.append((session_id, channel_type, receiver))
            return True

        trigger.run_evolution_for_session = fake_runner
        trigger._scan_once(bridge, EvolutionConfig(enabled=True, idle_minutes=1, min_turns=6, max_steps=12))
    finally:
        trigger.run_evolution_for_session = original_runner

    agent = bridge.agents["session-a"]
    add_check(
        checks,
        "idle trigger records one eligible session",
        calls == [("session-a", "web", "session-a")] and getattr(agent, "_evo_turns", None) == 0,
        json.dumps({"calls": calls, "remainingTurns": getattr(agent, "_evo_turns", None)}, ensure_ascii=False),
    )

    trigger.note_user_turn(agent, channel_type="web", receiver="session-a")
    add_check(
        checks,
        "note_user_turn increments future proactive review signal",
        getattr(agent, "_evo_turns", 0) == 1 and getattr(agent, "_evo_receiver", "") == "session-a",
        json.dumps({"turns": getattr(agent, "_evo_turns", None), "receiver": getattr(agent, "_evo_receiver", "")}),
    )


def check_executor_guards(root: pathlib.Path, checks: list[dict[str, Any]]) -> None:
    sys.path.insert(0, str(root))
    import agent.evolution.executor as executor

    allowed = set(getattr(executor, "_ALLOWED_TOOLS", set()))
    expected = {"read", "write", "edit", "ls", "bash", "memory_search", "memory_get"}
    add_check(checks, "evolution tool allowlist remains narrow", allowed == expected, json.dumps(sorted(allowed)))
    add_check(checks, "evolution concurrency remains bounded", getattr(executor, "_MAX_CONCURRENT", None) == 2, f"_MAX_CONCURRENT={getattr(executor, '_MAX_CONCURRENT', None)}")

    source = read_text_no_bom(root / "agent" / "evolution" / "executor.py")
    required_snippets = [
        "authorize_noninteractive",
        "fs_write",
        "skill_write",
        "_WorkspaceWriteGuard",
        "_BashWorkspaceGuard",
        "_builtin_skill_names",
        "if not cfg.enabled",
        "_workspace_changed",
    ]
    missing = [snippet for snippet in required_snippets if snippet not in source]
    add_check(checks, "evolution background safety guards present", not missing, f"missing={missing}")

    class DummyResult:
        status = "success"

    class DummyBash:
        name = "bash"
        description = "dummy bash"
        params: dict[str, Any] = {}
        cwd = ""

        def __init__(self) -> None:
            self.executed: list[str] = []

        def execute(self, args: dict[str, Any]) -> DummyResult:
            self.executed.append(str(args.get("command") or ""))
            return DummyResult()

    with tempfile.TemporaryDirectory(prefix="ecorex-evo-bash-") as tmp:
        workspace = pathlib.Path(tmp)
        inner = DummyBash()
        guard = executor._BashWorkspaceGuard(inner, str(workspace))
        inside_abs = workspace / "output" / "evo.txt"
        blocked_cases = {
            "windows drive absolute": r"echo hi > C:\Users\Public\ecorex-evo.txt",
            "windows drive relative": r"echo hi > C:ecorex-evo.txt",
            "windows unc": r"type \\server\share\secret.txt",
            "cmd home env": r"cmd /c echo hi > %USERPROFILE%\ecorex-evo.txt",
            "powershell temp env": r"powershell -NoProfile -Command \"Set-Content $env:TEMP\ecorex-evo.txt hi\"",
            "bash home env": "echo hi > $HOME/ecorex-evo.txt",
            "tilde": "echo hi > ~/ecorex-evo.txt",
            "parent traversal": r"echo hi > ..\ecorex-evo.txt",
        }
        blocked_results = {
            name: getattr(guard.execute({"command": command}), "status", "")
            for name, command in blocked_cases.items()
        }
        allowed_relative = getattr(guard.execute({"command": "echo hi > output/ecorex-evo.txt"}), "status", "")
        allowed_absolute = getattr(guard.execute({"command": f"echo hi > {inside_abs}"}), "status", "")

    blocked_ok = all(status in {"error", "fail"} for status in blocked_results.values())
    allowed_ok = allowed_relative == "success" and allowed_absolute == "success"
    add_check(
        checks,
        "evolution bash workspace guard blocks Windows escapes",
        blocked_ok and allowed_ok,
        json.dumps({
            "blocked": blocked_results,
            "allowedRelative": allowed_relative,
            "allowedAbsolute": allowed_absolute,
        }, ensure_ascii=False, sort_keys=True),
    )


def read_unpacked_sidecar(root: pathlib.Path, unpacked_dir: pathlib.Path) -> tuple[str, str]:
    direct = unpacked_dir / "resources" / "app" / "dist-electron" / "sidecar.js"
    if direct.exists():
        return read_text_no_bom(direct), str(direct)

    asar_path = unpacked_dir / "resources" / "app.asar"
    if not asar_path.exists():
        raise FileNotFoundError(str(direct))

    with tempfile.TemporaryDirectory(prefix="ecorex-asar-sidecar-") as tmp:
        out_dir = pathlib.Path(tmp)
        npx = "npx.cmd" if sys.platform.startswith("win") else "npx"
        result = subprocess.run(
            [npx, "asar", "extract-file", str(asar_path), "dist-electron/sidecar.js"],
            cwd=str(out_dir),
            text=True,
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "asar extract failed").strip())
        candidates = [out_dir / "sidecar.js", out_dir / "dist-electron" / "sidecar.js"]
        extracted = next((candidate for candidate in candidates if candidate.exists()), None)
        if not extracted:
            matches = list(out_dir.rglob("sidecar.js"))
            extracted = matches[0] if matches else None
        if not extracted:
            raise FileNotFoundError("dist-electron/sidecar.js extracted from app.asar")
        return read_text_no_bom(extracted), f"{asar_path}!/dist-electron/sidecar.js"


def check_runtime_artifacts(root: pathlib.Path, runtime_dir: pathlib.Path | None, unpacked_dir: pathlib.Path | None, checks: list[dict[str, Any]]) -> None:
    if runtime_dir:
        check_json_runtime_defaults(runtime_dir / "config-template.json", checks, "packaged runtime config-template")
        manifest_path = runtime_dir / "runtime-manifest.json"
        if manifest_path.exists():
            manifest = read_json_no_bom(manifest_path)
            add_check(
                checks,
                "runtime manifest present",
                manifest.get("product") == "EcoreX" or bool(manifest.get("runtime")),
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            )

    if unpacked_dir:
        text, evidence_path = read_unpacked_sidecar(root, unpacked_dir)
        add_check(
            checks,
            "unpacked Electron sidecar enables proactive memory",
            "self_evolution_enabled: true" in text
            and "scheduler_enabled: false" in text
            and "mcp_auto_start: false" in text,
            evidence_path,
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--version", default="0.2.0")
    parser.add_argument("--output", default="")
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--unpacked-dir", default="")
    parser.add_argument("--require-runtime", action="store_true")
    parser.add_argument("--require-unpacked", action="store_true")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    if not (root / "agent").is_dir() and (root.parent / "agent").is_dir():
        root = root.parent

    runtime_dir = pathlib.Path(args.runtime_dir).resolve() if args.runtime_dir else None
    unpacked_dir = pathlib.Path(args.unpacked_dir).resolve() if args.unpacked_dir else None
    if args.require_runtime and not runtime_dir:
        runtime_dir = root / "desktop" / "release" / "win-unpacked" / "resources" / "ecorex-runtime"
    if args.require_unpacked and not unpacked_dir:
        unpacked_dir = root / "desktop" / "release" / "win-unpacked"

    checks: list[dict[str, Any]] = []
    try:
        check_source_defaults(root, checks)
        check_trigger_behavior(root, checks)
        check_executor_guards(root, checks)
        check_runtime_artifacts(root, runtime_dir, unpacked_dir, checks)
    except Exception as exc:
        add_check(checks, "proactive memory smoke exception", False, repr(exc))

    if args.require_runtime and not runtime_dir:
        add_check(checks, "packaged runtime required", False, "missing --runtime-dir")
    elif args.require_runtime and runtime_dir and not runtime_dir.exists():
        add_check(checks, "packaged runtime required", False, str(runtime_dir))

    if args.require_unpacked and not unpacked_dir:
        add_check(checks, "unpacked app required", False, "missing --unpacked-dir")
    elif args.require_unpacked and unpacked_dir and not unpacked_dir.exists():
        add_check(checks, "unpacked app required", False, str(unpacked_dir))

    failures = [item for item in checks if item["status"] != "pass"]
    payload = {
        "status": "pass" if not failures else "fail",
        "version": args.version,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "changeIds": ["MEM-001"],
        "checks": checks,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = pathlib.Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
