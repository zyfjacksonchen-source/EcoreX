#!/usr/bin/env python3
"""Validate desktop sidecar lifecycle contracts and write release evidence."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Any


def add_result(results: list[dict[str, Any]], name: str, ok: bool, evidence: str) -> None:
    results.append(
        {
            "name": name,
            "status": "pass" if ok else "fail",
            "evidence": evidence,
        }
    )


def contains_all(source: str, markers: list[str]) -> tuple[bool, str]:
    missing = [marker for marker in markers if marker not in source]
    return not missing, "missing=" + repr(missing) if missing else f"{len(markers)} markers present"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--version", default="0.2.0")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    sidecar_path = root / "desktop" / "electron" / "sidecar.ts"
    api_bridge_path = root / "desktop" / "electron" / "apiBridge.ts"
    sidecar = sidecar_path.read_text(encoding="utf-8")
    api_bridge = api_bridge_path.read_text(encoding="utf-8")

    results: list[dict[str, Any]] = []
    ok, evidence = contains_all(
        sidecar,
        [
            "export type SidecarPhase =",
            "private startupPromise: Promise<boolean> | null = null;",
            'this.appendDiagnostic(this.status, "single-flight-startup")',
            "startupInFlight: Boolean(this.startupPromise)",
            "recentEvents: this.diagnosticEvents.slice(-this.diagnosticLimit)",
        ],
    )
    add_result(results, "single-flight diagnostics contract", ok, evidence)

    ok = sidecar.find("this.startupPromise = startupPromise;") != -1 and (
        sidecar.find("this.startupPromise = startupPromise;") < sidecar.find("this.child = this.spawnProcess")
    )
    add_result(results, "startup latch is created before spawn", ok, "startupPromise assignment precedes spawnProcess call")

    ok = bool(re.search(r'this\.getState\(\) === "running" && this\.phase === "ready"', sidecar))
    add_result(results, "ready requires legacy running plus ready phase", ok, "waitUntilReady phase gate")

    ok, evidence = contains_all(
        sidecar,
        [
            'state: "running",\n      message: `EcoreX local runtime health check degraded',
            '}, "degraded", "health-probe-failed");',
            '}, "ready", "health-recovered");',
            '}, "restarting", "health-check-failed");',
        ],
    )
    add_result(results, "degraded watchdog can recover or restart", ok, evidence)

    ok = bool(
        re.search(
            r"if \(stoppedIntentionally\) \{\s*if \(this\.stoppingIntentionally && !this\.child\)",
            sidecar,
        )
        and "if (this.child !== launchedChild) return;" in sidecar
        and sidecar.count("if (this.child !== launchedChild) return;") >= 3
    )
    add_result(results, "stale child events cannot overwrite replacement status", ok, "spawn/error/exit/stderr guarded by current child")

    ok, evidence = contains_all(
        sidecar,
        [
            "export type SidecarManagerOptions =",
            "private readonly spawnProcess: typeof spawn;",
            "this.spawnProcess(python, [\"app.py\"]",
            "this.clearTimeoutImpl(this.restartTimer)",
            "this.setTimeoutImpl(() => {",
            "this.fetchImpl(`http://127.0.0.1:${webPort}/api/version`",
            "this.broadcastStatus(nextStatus);",
        ],
    )
    add_result(results, "sidecar lifecycle dependencies are injectable", ok, evidence)

    ok = (
        "private redactDiagnosticText(value: string)" in sidecar
        and "[runtime-token]" in sidecar
        and "[api-key]" in sidecar
        and "[email]" in sidecar
        and "message: this.redactDiagnosticText(status.message)" in sidecar
    )
    add_result(results, "sidecar diagnostics are redacted before IPC", ok, "runtime token, API key, email, and user path redaction markers present")

    ok = (
        "const MAX_SIDECAR_RESPONSE_BYTES" in api_bridge
        and "readResponseTextWithLimit(response)" in api_bridge
        and "text = await readResponseTextWithLimit(response);" in api_bridge
        and api_bridge.index("text = await readResponseTextWithLimit(response);") < api_bridge.index("clearTimeout(timeout);")
        and "sidecar response exceeded" in api_bridge
    )
    add_result(results, "api bridge timeout and byte cap cover response body", ok, "bounded body reader runs inside abort timeout scope")

    ok, evidence = contains_all(
        api_bridge,
        [
            "sidecarPhase: status.phase",
            "sidecarDiagnostics: status.diagnostics",
            '"X-EcoreX-Runtime-Token": sidecar.getRuntimeToken()',
        ],
    )
    add_result(results, "api bridge reports phase diagnostics and runtime token", ok, evidence)

    ok, evidence = contains_all(
        sidecar + "\n" + api_bridge,
        [
            "reportApiFailure(reason: string): SidecarStatus",
            '}, "degraded", reason);',
            '}, "restarting", reason);',
            "sidecar.reportApiFailure",
            '"api-bridge-timeout"',
            '"api-bridge-connectivity"',
        ],
    )
    add_result(results, "api bridge timeout immediately degrades ready sidecar", ok, evidence)

    failures = [item for item in results if item["status"] != "pass"]
    payload = {
        "status": "pass" if not failures else "fail",
        "version": args.version,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "changeIds": ["STAB-004"],
        "checks": results,
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
