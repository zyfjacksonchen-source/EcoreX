#!/usr/bin/env python3
"""R23-16 aggregate safety gate for EcoreX v0.2.3.

The report is intentionally redacted: it records booleans, enum statuses,
relative artifact names, and HMAC references. It never echoes matched text from
scanned artifacts or local absolute paths.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "v0.2.3" / "artifacts" / "security-permission-audit.json"
SCANNER_PATH = ROOT / "scripts" / "scan-session-artifacts-privacy.py"
PERFORMANCE_AUDIT = ROOT / "docs" / "v0.2.3" / "artifacts" / "perf-evidence-audit.json"

REQUIRED_ARTIFACTS: Tuple[Tuple[str, str, str], ...] = (
    (
        "external-connections",
        "docs/v0.2.3/artifacts/external-connections-browser-smoke.json",
        "docs/v0.2.3/artifacts/external-connections-privacy-scan.json",
    ),
    (
        "performance-evidence",
        "docs/v0.2.3/artifacts/perf-evidence-audit.json",
        "docs/v0.2.3/artifacts/perf-evidence-audit-privacy-scan.json",
    ),
    (
        "browser-ocr",
        "docs/v0.2.3/artifacts/perf-browser-ocr.json",
        "docs/v0.2.3/artifacts/perf-browser-ocr-privacy-scan.json",
    ),
    (
        "image-artifact-ocr",
        "docs/v0.2.3/artifacts/perf-image-artifact-ocr.json",
        "docs/v0.2.3/artifacts/perf-image-artifact-ocr-privacy-scan.json",
    ),
    (
        "scheduler-subagent",
        "docs/v0.2.3/artifacts/perf-scheduler-subagent.json",
        "docs/v0.2.3/artifacts/perf-scheduler-subagent-privacy-scan.json",
    ),
    (
        "cross-talk",
        "docs/v0.2.3/artifacts/session-cross-talk-browser-smoke.json",
        "docs/v0.2.3/artifacts/session-cross-talk-privacy-scan.json",
    ),
)

DIRECT_SCAN_ARTIFACTS: Tuple[str, ...] = (
    "docs/v0.2.3/artifacts/chrome-devtools-mcp-live-smoke.json",
    "docs/v0.2.3/artifacts/chat-attachment-bubble-smoke.json",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("module_load_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _salt(value: str = "") -> bytes:
    raw = value or os.environ.get("ECOREX_ARTIFACT_SCAN_HMAC_SALT") or secrets.token_hex(32)
    return str(raw).encode("utf-8", errors="replace")


def _hmac_ref(value: str, salt: bytes) -> str:
    digest = hmac.new(salt, value.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()
    return f"hmac:{digest[:16]}"


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except Exception:
        return path.name


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig", errors="replace")


def _check(checks: List[Dict[str, Any]], check_id: str, ok: bool, refs: Iterable[str] = ()) -> None:
    checks.append(
        {
            "id": check_id,
            "status": "pass" if ok else "fail",
            "refs": sorted(str(item).replace("\\", "/") for item in refs),
        }
    )


def _static_checks() -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    sys.path.insert(0, str(ROOT))
    import config  # type: ignore
    from agent.tools.mcp.mcp_client import _is_default_chrome_devtools_config  # type: ignore
    from common.ecorex_tool_permissions import _DANGEROUS_TOOLS, _is_trusted_default_chrome_devtools_start  # type: ignore

    expected_args = config.chrome_devtools_mcp_args(config.DEFAULT_CDP_ENDPOINT)
    required_flags = {
        "--no-usage-statistics",
        "--no-performance-crux",
        "--redactNetworkHeaders",
    }
    browser_defaults = config.available_setting.get("tools", {}).get("browser", {})
    _check(
        checks,
        "cdp-first-defaults",
        config.DEFAULT_CDP_ENDPOINT == "http://127.0.0.1:9222"
        and browser_defaults.get("cdp_auto_launch") is True
        and browser_defaults.get("cdp_fallback") is True
        and browser_defaults.get("persistent") is True,
        ["config.py"],
    )
    _check(
        checks,
        "mcp-on-demand-local",
        config.available_setting.get("mcp_auto_start") is False
        and "--browserUrl" in expected_args
        and "http://127.0.0.1:9222" in expected_args
        and required_flags.issubset(set(expected_args)),
        ["config.py"],
    )
    _check(
        checks,
        "mcp-config-sync",
        _load_json(ROOT / "config-template.json").get("mcp_servers", [{}])[0].get("args") == expected_args
        and _load_json(ROOT / "config.json").get("mcp_servers", [{}])[0].get("args") == expected_args,
        ["config-template.json", "config.json"],
    )

    canonical = {
        "server": "chrome-devtools",
        "command": "npx",
        "args": expected_args,
        "trusted_default_chrome_devtools": True,
    }
    remote = {
        **canonical,
        "args": config.chrome_devtools_mcp_args("http://192.168.0.2:9222"),
    }
    no_privacy = {
        **canonical,
        "args": [item for item in expected_args if item != "--redactNetworkHeaders"],
    }
    extra_flag = {**canonical, "args": [*expected_args, "--remote-debugging-address=0.0.0.0"]}
    untrusted = {**canonical, "trusted_default_chrome_devtools": False}
    _check(
        checks,
        "mcp-broker-local-only",
        _is_trusted_default_chrome_devtools_start(canonical)
        and not _is_trusted_default_chrome_devtools_start(remote)
        and not _is_trusted_default_chrome_devtools_start(no_privacy)
        and not _is_trusted_default_chrome_devtools_start(extra_flag)
        and not _is_trusted_default_chrome_devtools_start(untrusted),
        ["common/ecorex_tool_permissions.py"],
    )
    _check(
        checks,
        "mcp-client-local-only",
        _is_default_chrome_devtools_config("chrome-devtools", "npx", expected_args)
        and not _is_default_chrome_devtools_config("chrome-devtools", "npx", remote["args"])
        and not _is_default_chrome_devtools_config("chrome-devtools", "npx", no_privacy["args"])
        and not _is_default_chrome_devtools_config("chrome-devtools", "npx", extra_flag["args"]),
        ["agent/tools/mcp/mcp_client.py"],
    )

    browser_service = _read("agent/tools/browser/browser_automation_service.py")
    _check(
        checks,
        "cdp-dedicated-profile",
        'DEFAULT_CDP_USER_DATA_DIR = "~/.cow/chrome_cdp_profile"' in browser_service
        and '"--no-first-run"' in browser_service
        and '"--no-default-browser-check"' in browser_service
        and '"--user-data-dir=' in browser_service
        and '"--remote-debugging-port=' in browser_service,
        ["agent/tools/browser/browser_automation_service.py"],
    )

    ocr_source = _read("agent/tools/ocr/ocr.py")
    _check(
        checks,
        "ocr-public-error-summary",
        "_public_error_summary" in ocr_source
        and '"errorSummary"' in ocr_source
        and '"error"' not in ocr_source.split('"errorSummary"', 1)[-1][:240],
        ["agent/tools/ocr/ocr.py"],
    )

    imagegen_source = _read("agent/tools/imagegen/imagegen.py")
    agent_stream_source = _read("agent/protocol/agent_stream.py")
    _check(
        checks,
        "imagegen-permission-boundary",
        "imagegen" in _DANGEROUS_TOOLS
        and '"imagegen"' in agent_stream_source
        and 'authorize_file_access("read"' in imagegen_source
        and 'authorize_file_access("write"' in imagegen_source
        and "image input read blocked by permissions" in imagegen_source
        and "image output directory blocked by permissions" in imagegen_source,
        [
            "common/ecorex_tool_permissions.py",
            "agent/protocol/agent_stream.py",
            "agent/tools/imagegen/imagegen.py",
        ],
    )

    public_payload = _read("common/ecorex_public_payload.py")
    _check(
        checks,
        "public-payload-redaction",
        "[redacted-content]" in public_payload
        and "[redacted]" in public_payload
        and "authorization" in public_payload.lower()
        and "cookie" in public_payload.lower(),
        ["common/ecorex_public_payload.py"],
    )

    adapter = _read("channel/messaging_adapter_contract.py")
    scheduler_projection = _read("agent/tools/scheduler/projection.py")
    scheduler_integration = _read("agent/tools/scheduler/integration.py")
    _check(
        checks,
        "external-delivery-redacted",
        "contentPreview" in adapter
        and "contentHash" in adapter
        and "sessionHash" in adapter
        and "receiverHash" in adapter
        and "reasonSummary" in scheduler_integration,
        [
            "channel/messaging_adapter_contract.py",
            "agent/tools/scheduler/integration.py",
        ],
    )
    _check(
        checks,
        "scheduler-projection-redacted",
        "receiverHash" in scheduler_projection
        and "redact_public_tool_value" in scheduler_projection
        and "[redacted-content]" in scheduler_projection,
        ["agent/tools/scheduler/projection.py"],
    )

    learning = _read("agent/skills/learning_service.py")
    capability = _read("agent/tools/agent_capability/agent_capability.py")
    _check(
        checks,
        "learned-skill-draft-gate",
        "Create a draft first; do not write directly to skills/." in learning
        and "security_reviewed" in learning
        and "role_reviewed" in learning
        and "approve_and_register" in learning
        and "SkillLearningService(skill_service=_skill_service(workspace)).approve_and_register" in capability,
        [
            "agent/skills/learning_service.py",
            "agent/tools/agent_capability/agent_capability.py",
        ],
    )

    app_source = _read("desktop/src/App.tsx")
    _check(
        checks,
        "run-center-hidden",
        "Run Center" not in app_source
        or "runCenterHidden" in _read("scripts/smoke-web-external-connections-browser.py"),
        ["desktop/src/App.tsx", "scripts/smoke-web-external-connections-browser.py"],
    )
    return checks


def _artifact_checks(scanner: Any, salt: bytes) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], int]:
    checks: List[Dict[str, Any]] = []
    scan_issues: List[Dict[str, Any]] = []
    finding_buckets: List[Dict[str, Any]] = []
    scanned_count = 0

    for item_id, main_rel, scan_rel in REQUIRED_ARTIFACTS:
        main_path = ROOT / main_rel
        scan_path = ROOT / scan_rel
        main_exists = main_path.exists()
        scan_exists = scan_path.exists()
        scan_clean = False
        if scan_exists:
            try:
                scan_payload = _load_json(scan_path)
            except Exception:
                scan_payload = {}
            scan_clean = scan_payload.get("status") == "success" and int(scan_payload.get("findingCount") or 0) == 0
        _check(checks, f"artifact-{item_id}", main_exists and scan_exists and scan_clean, [main_rel, scan_rel])
        if not main_exists or not scan_exists or not scan_clean:
            scan_issues.append(
                {
                    "idHash": _hmac_ref(item_id, salt),
                    "mainHash": _hmac_ref(main_rel, salt),
                    "scanHash": _hmac_ref(scan_rel, salt),
                    "status": "fail",
                }
            )
        for path in (main_path, scan_path):
            if path.exists():
                scanned_count += 1
                text = path.read_text(encoding="utf-8-sig", errors="replace")
                for pattern, count in sorted(scanner._scan_text(text).items()):
                    finding_buckets.append(
                        {
                            "artifactHash": _hmac_ref(_rel(path), salt),
                            "findingTypeHash": _hmac_ref(str(pattern), salt),
                            "count": int(count),
                        }
                    )

    for rel in DIRECT_SCAN_ARTIFACTS:
        path = ROOT / rel
        exists = path.exists()
        direct_findings = []
        if exists:
            scanned_count += 1
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            direct_findings = [
                {"findingTypeHash": _hmac_ref(str(pattern), salt), "count": int(count)}
                for pattern, count in sorted(scanner._scan_text(text).items())
            ]
            for item in direct_findings:
                finding_buckets.append({"artifactHash": _hmac_ref(rel, salt), **item})
        _check(checks, f"direct-scan-{Path(rel).stem}", exists and not direct_findings, [rel])

    chrome_rel = "docs/v0.2.3/artifacts/chrome-devtools-mcp-live-smoke.json"
    chrome_path = ROOT / chrome_rel
    chrome_ok = False
    if chrome_path.exists():
        try:
            chrome = _load_json(chrome_path)
        except Exception:
            chrome = {}
        args = chrome.get("args") if isinstance(chrome.get("args"), list) else []
        endpoint = str(chrome.get("endpoint") or "")
        cdp = chrome.get("cdp") if isinstance(chrome.get("cdp"), dict) else {}
        required = set(chrome.get("requiredToolsPresent") or [])
        chrome_ok = (
            chrome.get("status") == "pass"
            and endpoint == "http://127.0.0.1:9222"
            and cdp.get("endpoint") == "http://127.0.0.1:9222"
            and chrome.get("autoLaunchedChrome") is True
            and chrome.get("mcpInitialized") is True
            and "--redactNetworkHeaders" in args
            and "--no-usage-statistics" in args
            and "--no-performance-crux" in args
            and {"take_snapshot", "take_screenshot", "list_network_requests", "performance_start_trace"}.issubset(required)
        )
    _check(checks, "chrome-live-smoke-local", chrome_ok, [chrome_rel])

    return checks, scan_issues, finding_buckets, scanned_count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit R23-16 safety and permission evidence")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--salt", default="")
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    salt = _salt(args.salt)
    scanner = _load_module("ecorex_artifact_privacy_scanner", SCANNER_PATH)

    checks = _static_checks()
    artifact_checks, scan_issues, finding_buckets, scanned_count = _artifact_checks(scanner, salt)
    checks.extend(artifact_checks)

    failed_checks = [item for item in checks if item.get("status") != "pass"]
    failed = bool(failed_checks or scan_issues or finding_buckets)
    payload = {
        "version": "0.2.3",
        "slice": "R23-16",
        "scenario": "safety-permission-review",
        "status": "fail" if failed else "pass",
        "redacted": True,
        "metrics": {
            "checkCount": len(checks),
            "failedCheckCount": len(failed_checks),
            "artifactPairCount": len(REQUIRED_ARTIFACTS),
            "directScanCount": len(DIRECT_SCAN_ARTIFACTS),
            "scannedArtifactCount": scanned_count,
            "scanIssueCount": len(scan_issues),
            "findingBucketCount": len(finding_buckets),
            "findingTotalCount": sum(int(item.get("count") or 0) for item in finding_buckets),
        },
        "checks": checks,
        "scanIssues": scan_issues,
        "findingBuckets": finding_buckets,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
