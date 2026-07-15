#!/usr/bin/env python3
"""R23-17 final release gate audit for EcoreX v0.2.3.

This audit is deliberately non-promoting. It summarizes current release
evidence and names the exact blockers that prevent the long goal from being
marked complete.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "v0.2.3"
ACCEPTANCE = DOCS / "acceptance-checklist.md"
DEFAULT_OUTPUT = DOCS / "artifacts" / "final-release-gate-audit.json"

FINAL_PASS_REQUIRED = {
    "R23-16P": "performance-final-gate-not-promoted",
    "R23-20": "session-final-gate-not-promoted",
    "R23-21": "chat-bubble-integrated-browser-smoke-missing",
    "R23-17": "r23-17-final-review-not-pass",
}

REQUIRED_CLEAN_ARTIFACTS: Tuple[Tuple[str, str, str], ...] = (
    ("security", "docs/v0.2.3/artifacts/security-permission-audit.json", "pass"),
    ("security-scan", "docs/v0.2.3/artifacts/security-permission-audit-privacy-scan.json", "success"),
    ("performance", "docs/v0.2.3/artifacts/perf-evidence-audit.json", "pass"),
    ("performance-scan", "docs/v0.2.3/artifacts/perf-evidence-audit-privacy-scan.json", "success"),
    ("external-scan", "docs/v0.2.3/artifacts/external-connections-privacy-scan.json", "success"),
    ("session-browser-smoke", "docs/v0.2.3/artifacts/session-cross-talk-browser-smoke.json", "PASS"),
    ("session-refresh-smoke", "docs/v0.2.3/artifacts/session-cross-talk-refresh-replay.json", "PASS"),
    ("cross-talk-scan", "docs/v0.2.3/artifacts/session-cross-talk-privacy-scan.json", "success"),
    ("cross-talk-screenshot-scan", "docs/v0.2.3/artifacts/session-cross-talk-screenshot-privacy-scan.json", "success"),
    ("chrome-scan", "docs/v0.2.3/artifacts/chrome-devtools-mcp-live-privacy-scan.json", "success"),
    ("chat-scan", "docs/v0.2.3/artifacts/chat-attachment-bubble-privacy-scan.json", "success"),
    ("chat-browser-smoke", "docs/v0.2.3/artifacts/chat-attachment-bubble-browser-smoke.json", "PASS"),
    ("chat-browser-scan", "docs/v0.2.3/artifacts/chat-attachment-bubble-browser-privacy-scan.json", "success"),
    ("capability-recovery-smoke", "docs/v0.2.3/artifacts/capability-recovery-smoke.json", "PASS"),
    ("capability-recovery-scan", "docs/v0.2.3/artifacts/capability-recovery-privacy-scan.json", "success"),
    ("capability-recovery-package", "docs/v0.2.3/artifacts/capability-recovery-package-audit.json", "PASS"),
    ("production-deploy", "docs/v0.2.3/artifacts/production-deploy-online.json", "PASS"),
    ("production-public-smoke", "docs/v0.2.3/artifacts/production-public-http-smoke.json", "PASS"),
    ("production-capability-smoke", "docs/v0.2.3/artifacts/production-capability-recovery-smoke.json", "PASS"),
    ("production-real-tool-invocation-smoke", "docs/v0.2.3/artifacts/production-real-tool-invocation-smoke.json", "PASS"),
    ("production-deploy-scan", "docs/v0.2.3/artifacts/production-deploy-privacy-scan.json", "success"),
    ("real-tool-invocation-smoke", "docs/v0.2.3/artifacts/real-tool-invocation-smoke.json", "PASS"),
    ("image-generation-tool-invocation-smoke", "docs/v0.2.3/artifacts/image-generation-tool-invocation-smoke.json", "PASS"),
    ("ability-extension-fallback-browser-smoke", "docs/v0.2.3/artifacts/ability-extension-fallback-browser-smoke.json", "PASS"),
    ("tool-invocation-observability-scan", "docs/v0.2.3/artifacts/tool-invocation-and-ability-observability-privacy-scan.json", "success"),
)


def _salt(value: str = "") -> bytes:
    raw = value or os.environ.get("ECOREX_ARTIFACT_SCAN_HMAC_SALT") or secrets.token_hex(32)
    return str(raw).encode("utf-8", errors="replace")


def _hmac_ref(value: str, salt: bytes) -> str:
    digest = hmac.new(salt, value.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()
    return f"hmac:{digest[:16]}"


def _parse_acceptance(text: str) -> Dict[str, Dict[str, str]]:
    rows: Dict[str, Dict[str, str]] = {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw.startswith("| R23-"):
            continue
        cells = [cell.strip() for cell in raw.strip("|").split("|")]
        if len(cells) < 4:
            continue
        rows[cells[0]] = {
            "requirement": cells[1],
            "status": cells[2],
            "refs": cells[3],
        }
    return rows


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _artifact_status(path: Path) -> str:
    try:
        payload = _load_json(path)
    except Exception:
        return "missing"
    return str(payload.get("status") or "").strip()


def _artifact_clean(path: Path, expected_status: str) -> bool:
    try:
        payload = _load_json(path)
    except Exception:
        return False
    if str(payload.get("status") or "").strip() != expected_status:
        return False
    if "findingCount" in payload and int(payload.get("findingCount") or 0) != 0:
        return False
    if "findingBucketCount" in (payload.get("metrics") or {}) and int((payload.get("metrics") or {}).get("findingBucketCount") or 0) != 0:
        return False
    return _artifact_contract_clean(path, payload)


def _metric(payload: Dict[str, Any], key: str, default: Any = 0) -> Any:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return default
    return metrics.get(key, default)


def _nested_metric(payload: Dict[str, Any], section: str, key: str, default: Any = 0) -> Any:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return default
    nested = metrics.get(section)
    if not isinstance(nested, dict):
        return default
    return nested.get(key, default)


def _artifact_contract_clean(path: Path, payload: Dict[str, Any]) -> bool:
    name = path.name
    if name == "perf-evidence-audit.json":
        metrics = payload.get("metrics") or {}
        return (
            int(metrics.get("matrixScenarioCount") or 0) >= 8
            and int(metrics.get("scenarioPairCount") or 0) >= 7
            and int(metrics.get("requiredScenarioMissingCount") or 0) == 0
            and int(metrics.get("matrixConfigIssueCount") or 0) == 0
            and int(metrics.get("missingMainArtifactCount") or 0) == 0
            and int(metrics.get("missingScanArtifactCount") or 0) == 0
            and int(metrics.get("scanNotCleanCount") or 0) == 0
            and int(metrics.get("findingBucketCount") or 0) == 0
            and int(metrics.get("scannedArtifactCount") or 0) >= 14
        )
    if name == "session-cross-talk-browser-smoke.json":
        return (
            bool(_metric(payload, "sessionQueryIncludePinned"))
            and int(_metric(payload, "includedPinnedCount")) >= 3
            and int(_metric(payload, "generalRows")) >= 4
            and int(_metric(payload, "projectRows")) >= 2
            and bool(_metric(payload, "pinnedGroupBeforeUnpinned"))
            and bool(_metric(payload, "projectPinnedGroupBeforeUnpinned"))
            and bool(_metric(payload, "backendOwnerWonOverLocalStaleBinding"))
            and bool(_metric(payload, "projectOwnerStayedInProjectBucket"))
            and bool(_metric(payload, "renameDidNotPin"))
            and int(payload.get("consoleErrorCount") or 0) == 0
        )
    if name == "session-cross-talk-refresh-replay.json":
        return (
            bool(_nested_metric(payload, "race", "staleHistoryIgnored"))
            and bool(_nested_metric(payload, "race", "activeSessionContentStable"))
            and bool(_nested_metric(payload, "race", "mismatchDiagnosticObserved"))
            and bool(_nested_metric(payload, "race", "streamExpectedSessionObserved"))
            and bool(_nested_metric(payload, "refresh", "refreshKeptCleanSession"))
            and bool(_nested_metric(payload, "refresh", "backendHistoryFetched"))
            and bool(_nested_metric(payload, "refresh", "refreshRejectedLateSession"))
            and int(payload.get("consoleErrorCount") or 0) == 0
        )
    if name == "session-cross-talk-screenshot-privacy-scan.json":
        return (
            int(payload.get("findingCount") or 0) == 0
            and int(payload.get("imageOcrScannedCount") or 0) >= 2
            and int(payload.get("imageOcrUnavailableCount") or 0) == 0
            and int(payload.get("imageOcrErrorCount") or 0) == 0
        )
    if name == "chat-attachment-bubble-browser-smoke.json":
        metrics = payload.get("metrics") or {}
        return (
            bool(payload.get("redacted"))
            and int(metrics.get("userMessageCount") or 0) == 1
            and int(metrics.get("attachmentButtonCount") or 0) >= 2
            and int(metrics.get("imageAttachmentCount") or 0) >= 1
            and bool(metrics.get("textIncludesCodex"))
            and bool(metrics.get("runCenterHidden"))
            and int(payload.get("consoleErrorCount") or 0) == 0
        )
    if name == "capability-recovery-smoke.json":
        checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
        return (
            bool(payload.get("redacted"))
            and int(payload.get("toolCount") or 0) >= 20
            and int(payload.get("extensionCount") or 0) >= 20
            and bool(checks)
            and all(isinstance(item, dict) and item.get("status") == "PASS" for item in checks)
            and not payload.get("failed")
        )
    if name == "capability-recovery-package-audit.json":
        checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
        return (
            bool(payload.get("redacted"))
            and bool(checks)
            and all(isinstance(item, dict) and item.get("status") == "PASS" for item in checks)
            and not payload.get("failed")
        )
    if name == "production-deploy-online.json":
        checks = payload.get("onlineChecks") if isinstance(payload.get("onlineChecks"), dict) else {}
        redaction = payload.get("redaction") if isinstance(payload.get("redaction"), dict) else {}
        return (
            checks.get("webServiceVersion") == "0.2.3"
            and checks.get("installationManifestVersion") == "0.2.3"
            and checks.get("publicManifestVersion") == "0.2.3"
            and checks.get("serviceActive") is True
            and checks.get("serviceEnabled") is True
            and int(checks.get("webVersionStatus") or 0) == 200
            and checks.get("webVersionBodyHas023") is True
            and redaction.get("rawTargetPersisted") is False
            and redaction.get("rawPasswordPersisted") is False
            and redaction.get("rawSecretPersisted") is False
            and redaction.get("rawUrlPersisted") is False
        )
    if name == "production-public-http-smoke.json":
        checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
        artifact_checks = payload.get("artifactChecks") if isinstance(payload.get("artifactChecks"), list) else []
        target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
        return (
            checks.get("rootStatus200") is True
            and checks.get("rootMentionsEcoreX") is True
            and checks.get("manifestStatus200") is True
            and checks.get("manifestVersion023") is True
            and int(checks.get("manifestArtifactCount") or 0) >= 3
            and checks.get("adminProtected") is True
            and checks.get("artifactHeadsOk") is True
            and checks.get("artifactSizesMatch") is True
            and len(artifact_checks) >= 3
            and all(isinstance(item, dict) and item.get("status") == 200 and item.get("sizeMatchesManifest") is True for item in artifact_checks)
            and target.get("rawUrlPersisted") is False
        )
    if name == "production-capability-recovery-smoke.json":
        checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
        redaction = payload.get("redaction") if isinstance(payload.get("redaction"), dict) else {}
        feishu = payload.get("feishuSurface") if isinstance(payload.get("feishuSurface"), dict) else {}
        return (
            checks
            and all(value is True for value in checks.values())
            and int(payload.get("toolCount") or 0) >= 20
            and int(payload.get("extensionCount") or 0) >= 20
            and int(payload.get("externalConnectionCount") or 0) >= 1
            and not payload.get("missingTools")
            and not payload.get("missingExtensions")
            and feishu.get("schemaVisible") is True
            and feishu.get("toolSchemaCallable") is True
            and redaction.get("rawTargetPersisted") is False
            and redaction.get("rawPasswordPersisted") is False
            and redaction.get("rawSecretPersisted") is False
            and redaction.get("rawUrlPersisted") is False
        )
    if name == "real-tool-invocation-smoke.json":
        checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
        return (
            bool(payload.get("redacted"))
            and bool(checks)
            and all(isinstance(item, dict) and item.get("status") == "PASS" for item in checks)
            and any(isinstance(item, dict) and item.get("label") == "bash executes real command" for item in checks)
            and any(isinstance(item, dict) and item.get("label") == "ocr text url returns browser handoff" for item in checks)
            and any(isinstance(item, dict) and item.get("label") == "feishu_cli status callable" for item in checks)
        )
    if name == "production-real-tool-invocation-smoke.json":
        checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
        redaction = payload.get("redaction") if isinstance(payload.get("redaction"), dict) else {}
        labels = {str(item.get("label")) for item in checks if isinstance(item, dict)}
        return (
            bool(checks)
            and all(isinstance(item, dict) and item.get("status") == "PASS" for item in checks)
            and {
                "production bash executes real command",
                "production ocr text url returns browser handoff",
                "production optional abilities list callable",
                "production feishu_cli status callable",
            }.issubset(labels)
            and redaction.get("rawTargetPersisted") is False
            and redaction.get("rawPasswordPersisted") is False
            and redaction.get("rawSecretPersisted") is False
            and redaction.get("rawUrlPersisted") is False
        )
    if name == "image-generation-tool-invocation-smoke.json":
        generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
        edit = payload.get("edit") if isinstance(payload.get("edit"), dict) else {}
        calls = payload.get("calls") if isinstance(payload.get("calls"), list) else []
        serialized_calls = json.dumps(calls, ensure_ascii=False)
        return (
            generation.get("image_count") == 1
            and edit.get("image_count") == 1
            and generation.get("attempted_models") == ["gpt-image-2-pro", "gpt-image-2"]
            and edit.get("attempted_models") == ["gpt-image-2-pro", "gpt-image-2"]
            and len(calls) == 4
            and all(isinstance(item, dict) and item.get("authorization_seen") is True for item in calls)
            and "\"prompt\"" not in serialized_calls
        )
    if name == "ability-extension-fallback-browser-smoke.json":
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        return (
            bool(payload.get("redacted"))
            and bool(metrics.get("toolsApiReturnsEmpty"))
            and int(metrics.get("toolsApiCallCount") or 0) >= 1
            and int(metrics.get("extensionsApiCallCount") or 0) >= 1
            and int(metrics.get("rowCount") or 0) >= 8
            and int(metrics.get("unloadedCount") or 0) == 0
            and not payload.get("failed")
            and int(payload.get("consoleErrorCount") or 0) == 0
        )
    return True


def _status_is_final_pass(value: str) -> bool:
    normalized = str(value or "").strip().upper()
    return normalized == "PASS"


def build_audit(*, salt: bytes | None = None) -> Dict[str, Any]:
    salt = salt or _salt("")
    acceptance_rows = _parse_acceptance(ACCEPTANCE.read_text(encoding="utf-8-sig"))
    blockers: List[Dict[str, Any]] = []
    slice_states: List[Dict[str, Any]] = []

    for slice_id, row in sorted(acceptance_rows.items()):
        status = row.get("status") or ""
        final_required = slice_id in FINAL_PASS_REQUIRED
        ok = _status_is_final_pass(status) if final_required else "PASS" in status.upper()
        slice_states.append(
            {
                "id": slice_id,
                "status": status,
                "finalRequired": final_required,
                "ok": bool(ok),
            }
        )
        if final_required and not ok:
            blockers.append(
                {
                    "id": FINAL_PASS_REQUIRED[slice_id],
                    "slice": slice_id,
                    "status": status,
                }
            )

    if "R23-17" not in acceptance_rows:
        blockers.append({"id": "r23-17-final-review-row-missing", "slice": "R23-17", "status": "missing"})

    artifact_states: List[Dict[str, Any]] = []
    for item_id, rel, expected in REQUIRED_CLEAN_ARTIFACTS:
        path = ROOT / rel
        clean = path.exists() and _artifact_clean(path, expected)
        artifact_states.append(
            {
                "id": item_id,
                "status": _artifact_status(path) if path.exists() else "missing",
                "expected": expected,
                "clean": clean,
                "refHash": _hmac_ref(rel, salt),
            }
        )
        if not clean:
            blockers.append({"id": f"artifact-not-clean-{item_id}", "slice": "R23-17", "status": "blocked"})

    status = "pass" if not blockers else "blocked"
    return {
        "version": "0.2.3",
        "slice": "R23-17",
        "scenario": "final-release-gate",
        "status": status,
        "complete": status == "pass",
        "redacted": True,
        "metrics": {
            "sliceCount": len(slice_states),
            "artifactCount": len(artifact_states),
            "blockerCount": len(blockers),
        },
        "sliceStates": slice_states,
        "artifactStates": artifact_states,
        "blockers": blockers,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit v0.2.3 final release gate status")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--salt", default="")
    parser.add_argument("--require-complete", action="store_true")
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    audit = build_audit(salt=_salt(args.salt))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.dump(audit, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    if args.require_complete and not audit.get("complete"):
        return 1
    return 0 if audit.get("status") in {"pass", "blocked"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
