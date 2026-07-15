#!/usr/bin/env python3
"""Smoke R24-02A Feishu/Lark lark_oapi runtime readiness."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _default_python() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        candidate = Path(local_appdata) / "EcoreX WebUI" / "runtime" / "python" / "python.exe"
        if candidate.exists():
            return candidate
    return ROOT / "desktop" / "runtime" / "ecorex-runtime" / "python" / "python.exe"


def _python_kind(path: Path) -> str:
    text = str(path).replace("\\", "/").lower()
    if "ecorex webui/runtime/python" in text:
        return "installed_webui_runtime"
    if "ecorex-runtime/python" in text:
        return "packaged_webui_runtime"
    return "provided_python"


def _probe_python(path: Path, timeout: int) -> dict[str, Any]:
    code = r"""
import importlib.metadata
import importlib.util
import json
import sys
spec = importlib.util.find_spec("lark_oapi")
version = ""
if spec:
    try:
        version = importlib.metadata.version("lark-oapi")
    except Exception:
        version = "unknown"
register_app = False
if spec:
    try:
        import lark_oapi
        register_app = hasattr(lark_oapi, "register_app")
    except Exception:
        register_app = False
print(json.dumps({
    "sdkPresent": bool(spec),
    "sdkVersion": version,
    "registerAppAvailable": register_app,
    "pythonVersion": sys.version.split()[0],
}, ensure_ascii=True))
"""
    if not path.exists():
        return {"status": "missing_python", "sdkPresent": False}
    proc = subprocess.run(
        [str(path), "-c", code],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout.strip() or "{}")
    except Exception:
        payload = {}
    payload.update({
        "status": "success" if proc.returncode == 0 else "error",
        "returnCode": proc.returncode,
        "pythonExecutableKind": _python_kind(path),
        "stderrPresent": bool(proc.stderr.strip()),
    })
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Check lark_oapi in the active EcoreX WebUI Python runtime.")
    parser.add_argument("--python", default="", help="Python executable to probe. Defaults to installed WebUI runtime.")
    parser.add_argument("--json-output", default="", help="Optional JSON output path.")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    python_path = Path(args.python) if args.python else _default_python()
    probe = _probe_python(python_path, max(5, args.timeout))
    payload = {
        "status": "PASS" if probe.get("status") == "success" and probe.get("sdkPresent") else "FAIL",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scope": "feishu-lark-oapi-runtime",
        "dependency": "lark_oapi",
        "package": "lark-oapi>=1.5.5",
        "probe": probe,
        "redacted": True,
    }
    if args.json_output:
        target = Path(args.json_output)
        if not target.is_absolute():
            target = ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
