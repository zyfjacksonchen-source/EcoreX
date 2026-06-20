#!/usr/bin/env python3
"""Aggregate the v0.1.17 release promotion gates into one machine-readable report."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Any


REQUIRED_PUBLIC_ARTIFACTS = {
    "windows-x64",
    "webui-windows-x64",
    "webui-macos-universal",
    "macos-arm64-dmg",
    "macos-x64-dmg",
}
MACOS_DESKTOP_ARTIFACTS = {"macos-arm64-dmg", "macos-x64-dmg"}
WEBUI_ARTIFACTS = {"webui-windows-x64", "webui-macos-universal"}
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
MACOS_ARTIFACT_ARCH = {
    "macos-arm64-dmg": "arm64",
    "macos-x64-dmg": "x64",
}
REQUIRED_AUTH_NEGATIVE_STATUSES = {
    "messageNoToken",
    "messageWrongToken",
    "messageQueryTokenRejected",
    "streamNoToken",
    "streamWrongToken",
    "streamQueryTokenRejected",
    "fileStatNoToken",
    "fileStatWrongToken",
    "fileServeNoToken",
    "fileServeWrongToken",
    "openPathNoToken",
    "openPathWrongToken",
}


def read_json(path: pathlib.Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{path} has a UTF-8 BOM")
    return json.loads(raw.decode("utf-8"))


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def add_check(checks: list[dict[str, Any]], name: str, status: str, evidence: str, severity: str = "info") -> None:
    checks.append(
        {
            "name": name,
            "status": status,
            "severity": severity,
            "evidence": evidence,
        }
    )


def require_bool(payload: dict[str, Any], key: str) -> bool:
    return bool(payload.get(key))


def first_non_empty(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def bad_auth_statuses(statuses: dict[str, Any]) -> dict[str, Any]:
    bad: dict[str, Any] = {}
    for key, value in statuses.items():
        try:
            if int(value) != 401:
                bad[key] = value
        except Exception:
            bad[key] = value
    return bad


def check_version_files(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    cli_version = (root / "cli" / "VERSION").read_text(encoding="utf-8").strip()
    if cli_version == version:
        add_check(checks, "CLI version", "pass", f"cli/VERSION={version}")
    else:
        add_check(checks, "CLI version", "fail", f"cli/VERSION={cli_version}, expected {version}", "blocker")

    package = read_json(root / "desktop" / "package.json")
    if package.get("version") == version:
        add_check(checks, "Desktop package version", "pass", f"desktop/package.json={version}")
    else:
        add_check(
            checks,
            "Desktop package version",
            "fail",
            f"desktop/package.json={package.get('version')}, expected {version}",
            "blocker",
        )

    package_lock = read_json(root / "desktop" / "package-lock.json")
    lock_version = package_lock.get("version") or (package_lock.get("packages") or {}).get("", {}).get("version")
    if lock_version == version:
        add_check(checks, "Desktop package-lock version", "pass", f"desktop/package-lock.json={version}")
    else:
        add_check(
            checks,
            "Desktop package-lock version",
            "fail",
            f"desktop/package-lock.json={lock_version}, expected {version}",
            "blocker",
        )


def check_skill_evidence(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "skill-classification-smoke.json"
    try:
        smoke = read_json(path)
    except Exception as exc:
        add_check(checks, "Skill classification smoke", "fail", f"{path}: {exc}", "blocker")
        return
    failures = smoke.get("failures") or []
    if smoke.get("status") == "pass" and not failures and smoke.get("parityChecked", 0) > 0:
        add_check(
            checks,
            "Skill classification smoke",
            "pass",
            f"{path}: parity={smoke.get('parityChecked')} lark={smoke.get('larkSkillCount')}",
        )
    else:
        add_check(checks, "Skill classification smoke", "fail", f"{path}: {smoke}", "blocker")


def check_office_preinstall_smoke(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "office-preinstall-smoke.json"
    try:
        smoke = read_json(path)
    except Exception as exc:
        add_check(checks, "OFFICE-001 office preinstall smoke", "fail", f"{path}: {exc}", "blocker")
        return

    required_names = {
        "office-documents builtin loaded",
        "office-documents mention metadata",
        "office-documents office routing terms",
        "office-spreadsheets builtin loaded",
        "office-spreadsheets mention metadata",
        "office-spreadsheets office routing terms",
        "office-presentations builtin loaded",
        "office-presentations mention metadata",
        "office-presentations office routing terms",
        "office-pdf builtin loaded",
        "office-pdf mention metadata",
        "office-pdf office routing terms",
        "office-pdf capability manifest",
        "office-pdf capability modules",
        "Windows staging defaults office-pdf",
        "macOS staging defaults office-pdf",
        "office-documents staged runtime skill",
        "office-spreadsheets staged runtime skill",
        "office-presentations staged runtime skill",
        "office-pdf staged runtime skill",
        "runtime manifest preinstalled office-pdf",
        "office-pdf capability preinstall recorded",
        "office-pdf runtime module imports",
    }
    smoke_checks = smoke.get("checks") or []
    by_name = {str(item.get("name") or ""): item for item in smoke_checks if isinstance(item, dict)}
    missing = sorted(required_names - set(by_name))
    failing = [name for name in sorted(required_names & set(by_name)) if by_name[name].get("status") != "pass"]
    change_ids = set(str(item) for item in smoke.get("changeIds") or [])
    if smoke.get("status") == "pass" and smoke.get("version") == version and "OFFICE-001" in change_ids and not missing and not failing:
        add_check(checks, "OFFICE-001 office preinstall smoke", "pass", f"{path}: {len(required_names)} office checks")
    else:
        add_check(
            checks,
            "OFFICE-001 office preinstall smoke",
            "fail",
            f"{path}: status={smoke.get('status')} version={smoke.get('version')} missing={missing} failing={failing} changeIds={sorted(change_ids)}",
            "blocker",
        )


def check_proactive_memory_smoke(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "proactive-memory-smoke.json"
    try:
        smoke = read_json(path)
    except Exception as exc:
        add_check(checks, "MEM-001 proactive memory smoke", "fail", f"{path}: {exc}", "blocker")
        return

    required_names = {
        "root config-template self-evolution enabled",
        "root config-template scheduler remains disabled",
        "root config-template MCP auto-start remains disabled",
        "root config-template Feishu CLI remains on-demand",
        "evolution module default enabled",
        "evolution trigger thresholds conservative",
        "config.py fallback enables proactive memory",
        "desktop sidecar default enables proactive memory",
        "WebUI local release enables proactive memory",
        "idle trigger records one eligible session",
        "note_user_turn increments future proactive review signal",
        "evolution tool allowlist remains narrow",
        "evolution concurrency remains bounded",
        "evolution background safety guards present",
        "evolution bash workspace guard blocks Windows escapes",
        "packaged runtime config-template self-evolution enabled",
        "packaged runtime config-template scheduler remains disabled",
        "packaged runtime config-template MCP auto-start remains disabled",
        "packaged runtime config-template Feishu CLI remains on-demand",
        "unpacked Electron sidecar enables proactive memory",
    }
    smoke_checks = smoke.get("checks") or []
    by_name = {str(item.get("name") or ""): item for item in smoke_checks if isinstance(item, dict)}
    missing = sorted(required_names - set(by_name))
    failing = [name for name in sorted(required_names & set(by_name)) if by_name[name].get("status") != "pass"]
    change_ids = set(str(item) for item in smoke.get("changeIds") or [])
    if smoke.get("status") == "pass" and smoke.get("version") == version and "MEM-001" in change_ids and not missing and not failing:
        add_check(checks, "MEM-001 proactive memory smoke", "pass", f"{path}: {len(required_names)} proactive-memory checks")
    else:
        add_check(
            checks,
            "MEM-001 proactive memory smoke",
            "fail",
            f"{path}: status={smoke.get('status')} version={smoke.get('version')} missing={missing} failing={failing} changeIds={sorted(change_ids)}",
            "blocker",
        )


def check_server_release_gate_smoke(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "server-release-gate-smoke.json"
    try:
        smoke = read_json(path)
    except Exception as exc:
        add_check(checks, "REL-002 server release gate smoke", "fail", f"{path}: {exc}", "blocker")
        return

    smoke_checks = smoke.get("checks") or []
    by_name = {str(item.get("name") or ""): item for item in smoke_checks if isinstance(item, dict)}
    required = "server release rejects macOS unsigned smoke without auth-negative matrix"
    item = by_name.get(required)
    change_ids = set(str(item) for item in smoke.get("changeIds") or [])
    evidence = item.get("evidence") if isinstance(item, dict) else {}
    failure_count = int((evidence or {}).get("authNegativeFailureCount") or 0) if isinstance(evidence, dict) else 0
    if (
        smoke.get("status") == "pass"
        and smoke.get("version") == version
        and "REL-002" in change_ids
        and item
        and item.get("status") == "pass"
        and failure_count >= 1
    ):
        add_check(checks, "REL-002 server release gate smoke", "pass", f"{path}: auth-negative rejection count={failure_count}")
    else:
        add_check(
            checks,
            "REL-002 server release gate smoke",
            "fail",
            f"{path}: status={smoke.get('status')} version={smoke.get('version')} changeIds={sorted(change_ids)} item={item}",
            "blocker",
        )


def check_win_unpacked_smoke(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "win-unpacked-smoke.json"
    try:
        smoke = read_json(path)
    except Exception as exc:
        add_check(checks, "Windows unpacked smoke", "fail", f"{path}: {exc}", "blocker")
        return

    required_true = ["appStarted", "sidecarReady", "authReady", "authRequired", "authNegativeReady", "cleaned"]
    missing = [key for key in required_true if not require_bool(smoke, key)]
    negative = smoke.get("authNegativeStatuses") or {}
    missing_negative = sorted(REQUIRED_AUTH_NEGATIVE_STATUSES - set(negative))
    bad_statuses = bad_auth_statuses(negative)
    if smoke.get("runtimeVersion") == version and not missing and not missing_negative and not bad_statuses:
        add_check(
            checks,
            "Windows unpacked smoke",
            "pass",
            f"{path}: runtime={smoke.get('runtimeVersion')} auth-negative={len(negative)} endpoints",
        )
    else:
        add_check(
            checks,
            "Windows unpacked smoke",
            "fail",
            f"{path}: runtime={smoke.get('runtimeVersion')} missing={missing} missingNegative={missing_negative} badStatuses={bad_statuses}",
            "blocker",
        )


def check_win_installed_smoke(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "win-installed-smoke.json"
    try:
        smoke = read_json(path)
    except FileNotFoundError:
        add_check(checks, "Windows signed installed smoke", "fail", f"{path}: missing win-installed-smoke.json", "blocker")
        return
    except Exception as exc:
        add_check(checks, "Windows signed installed smoke", "fail", f"{path}: {exc}", "blocker")
        return

    required_true = ["installed", "appStarted", "sidecarReady", "authReady", "authRequired", "authNegativeReady", "cleaned"]
    missing = [key for key in required_true if not require_bool(smoke, key)]
    negative = smoke.get("authNegativeStatuses") or {}
    missing_negative = sorted(REQUIRED_AUTH_NEGATIVE_STATUSES - set(negative))
    bad_statuses = bad_auth_statuses(negative)
    installer_sha = str(smoke.get("installerSha256") or "")
    installer_size_ok = int(smoke.get("installerSize") or 0) > 0
    installer_file_ok = bool(str(smoke.get("installerFileName") or "").strip())
    installer_metadata_ok = bool(SHA256_RE.fullmatch(installer_sha)) and installer_size_ok and installer_file_ok
    signature_ok = (
        smoke.get("installerSignatureStatus") == "Valid"
        and smoke.get("appSignatureStatus") == "Valid"
        and smoke.get("runtimePythonSignatureStatus") == "Valid"
    )
    if smoke.get("runtimeVersion") == version and signature_ok and installer_metadata_ok and not missing and not missing_negative and not bad_statuses:
        add_check(
            checks,
            "Windows signed installed smoke",
            "pass",
            f"{path}: runtime={smoke.get('runtimeVersion')} installer={smoke.get('installerFileName')} signed installed matrix passed",
        )
    else:
        add_check(
            checks,
            "Windows signed installed smoke",
            "fail",
            f"{path}: runtime={smoke.get('runtimeVersion')} signatureOk={signature_ok} installerMetadataOk={installer_metadata_ok} missing={missing} missingNegative={missing_negative} badStatuses={bad_statuses}",
            "blocker",
        )


def check_acceptance_smoke(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "acceptance-smoke.json"
    try:
        smoke = read_json(path)
    except Exception as exc:
        add_check(checks, "STAB-004/UX-004/PERF-001 acceptance smoke", "fail", f"{path}: {exc}", "blocker")
        return

    required_change_ids = {"STAB-004", "UX-004", "PERF-001"}
    change_ids = set(str(item) for item in smoke.get("changeIds") or [])
    scenarios = smoke.get("scenarios") or {}
    required_scenarios = {"noResponseDeadLoop", "stalledStream", "terminalNoFlicker", "longMarkdown"}
    missing_change_ids = sorted(required_change_ids - change_ids)
    missing_scenarios = sorted(required_scenarios - set(scenarios))
    failing_scenarios = {
        name: (scenarios.get(name) or {}).get("status")
        for name in sorted(required_scenarios & set(scenarios))
        if (scenarios.get(name) or {}).get("status") != "pass"
    }
    captures = smoke.get("captures") or []
    capture_actions = {str(item.get("action") or "") for item in captures if isinstance(item, dict)}
    required_actions = {"terminal-boundary", "postdone-tail", "switch-race", "stream-100k", "long-markdown"}
    missing_actions = sorted(required_actions - capture_actions)
    by_action = {str(item.get("action") or ""): item for item in captures if isinstance(item, dict)}
    stream_metrics = by_action.get("stream-100k", {}).get("metrics") or {}
    long_metrics = by_action.get("long-markdown", {}).get("metrics") or {}
    artifact_metrics = by_action.get("artifact-preview", {}).get("metrics") or {}
    metric_failures = []
    if int(stream_metrics.get("deliveredChars") or 0) < 100000:
        metric_failures.append("stream-100k.deliveredChars")
    if int(stream_metrics.get("markerLeaks") or 0) != 0:
        metric_failures.append("stream-100k.markerLeaks")
    if int(stream_metrics.get("latestDomNodes") or 999999) > 2500:
        metric_failures.append("stream-100k.latestDomNodes")
    if int(stream_metrics.get("stopButtons") or 0) != 0:
        metric_failures.append("stream-100k.stopButtons")
    if not bool(long_metrics.get("expanded")):
        metric_failures.append("long-markdown.expanded")
    if float(long_metrics.get("expandMs") or 999999) > 3000:
        metric_failures.append("long-markdown.expandMs")
    if int(long_metrics.get("textChars") or 0) < 1000:
        metric_failures.append("long-markdown.textChars")
    if int(artifact_metrics.get("thumbNaturalWidth") or 0) <= 0 or int(artifact_metrics.get("previewNaturalWidth") or 0) <= 0:
        metric_failures.append("artifact-preview.naturalWidth")
    if (
        smoke.get("status") == "pass"
        and smoke.get("version") == version
        and not missing_change_ids
        and not missing_scenarios
        and not failing_scenarios
        and not missing_actions
        and not metric_failures
    ):
        add_check(
            checks,
            "STAB-004/UX-004/PERF-001 acceptance smoke",
            "pass",
            f"{path}: scenarios={len(required_scenarios)} actions={len(required_actions)}",
        )
    else:
        add_check(
            checks,
            "STAB-004/UX-004/PERF-001 acceptance smoke",
            "fail",
            f"{path}: status={smoke.get('status')} version={smoke.get('version')} missingChangeIds={missing_change_ids} missingScenarios={missing_scenarios} failingScenarios={failing_scenarios} missingActions={missing_actions} metricFailures={metric_failures}",
            "blocker",
        )


def check_xhs_markdown_image_stability_smoke(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "xhs-markdown-image-stability-smoke.json"
    try:
        smoke = read_json(path)
    except Exception as exc:
        add_check(checks, "XHS-001/PERF-002/IMG-001/ART-002 markdown image stability smoke", "fail", f"{path}: {exc}", "blocker")
        return

    required_change_ids = {"XHS-001", "PERF-002", "IMG-001", "ART-002"}
    required_checks = {
        "history reload preserves local terminal turn",
        "history final requires payload not botSeq only",
        "completed request resume is guarded",
        "voice attach does not preempt final done",
        "stream markdown has compact-output normalizer",
        "long markdown chunk fallback is fence-aware",
        "streaming long markdown uses live window",
        "streaming long markdown normalizes bounded window",
        "long reply preview is markdown-boundary aware",
        "pending artifact stat retry closes",
        "backend keeps pending artifacts",
        "backend extracts async output artifacts",
        "frontend preserves artifact statusPath",
        "frontend consumes pending artifact statusPath",
        "pending status json does not early-ready on ok true",
        "artifact merge upserts duplicate status",
        "file_to_send skips empty artifact",
        "structured tool output recognizes output path",
        "xhs image worker validates image outputs and writes atomically",
        "xhs image retry metadata",
        "xhs image retries are clamped",
        "xhs status writes are atomic and bootstrap failures close",
        "generic image generation rejects empty or invalid success",
        "invalid cached image is rejected",
        "xhs bootstrap failure writes failed status",
        "generic invalid image bytes are rejected",
    }
    change_ids = set(str(item) for item in smoke.get("changeIds") or [])
    smoke_checks = smoke.get("checks") or []
    by_name = {str(item.get("name") or ""): item for item in smoke_checks if isinstance(item, dict)}
    missing = sorted(required_checks - set(by_name))
    failing = [name for name in sorted(required_checks & set(by_name)) if by_name[name].get("status") != "pass"]
    missing_change_ids = sorted(required_change_ids - change_ids)
    if smoke.get("status") == "pass" and smoke.get("version") == version and not missing_change_ids and not missing and not failing:
        add_check(
            checks,
            "XHS-001/PERF-002/IMG-001/ART-002 markdown image stability smoke",
            "pass",
            f"{path}: {len(required_checks)} checks",
        )
    else:
        add_check(
            checks,
            "XHS-001/PERF-002/IMG-001/ART-002 markdown image stability smoke",
            "fail",
            f"{path}: status={smoke.get('status')} version={smoke.get('version')} missingChangeIds={missing_change_ids} missing={missing} failing={failing}",
            "blocker",
        )


def check_sidecar_contract_smoke(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "sidecar-contract-smoke.json"
    try:
        smoke = read_json(path)
    except Exception as exc:
        add_check(checks, "Sidecar lifecycle contract smoke", "fail", f"{path}: {exc}", "blocker")
        return

    change_ids = set(str(item) for item in smoke.get("changeIds") or [])
    smoke_checks = smoke.get("checks") or []
    failing = [item for item in smoke_checks if item.get("status") != "pass"]
    if smoke.get("status") == "pass" and smoke.get("version") == version and "STAB-004" in change_ids and len(smoke_checks) >= 6 and not failing:
        add_check(checks, "Sidecar lifecycle contract smoke", "pass", f"{path}: {len(smoke_checks)} checks")
    else:
        add_check(
            checks,
            "Sidecar lifecycle contract smoke",
            "fail",
            f"{path}: status={smoke.get('status')} version={smoke.get('version')} changeIds={sorted(change_ids)} failing={failing}",
            "blocker",
        )


def check_sidecar_lifecycle_smoke(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "sidecar-lifecycle-smoke.json"
    try:
        smoke = read_json(path)
    except Exception as exc:
        add_check(checks, "Sidecar executable lifecycle smoke", "fail", f"{path}: {exc}", "blocker")
        return

    required_checks = {
        "single-flight startup",
        "startup waiters share startup latch",
        "stale child isolation",
        "diagnostic redaction",
        "health degraded restart",
        "api bridge body cap",
        "api bridge parse redaction",
        "api bridge timeout degrades ready status",
    }
    smoke_checks = smoke.get("checks") or []
    by_name = {str(item.get("name") or ""): item for item in smoke_checks if isinstance(item, dict)}
    missing = sorted(required_checks - set(by_name))
    failing = [name for name in sorted(required_checks & set(by_name)) if by_name[name].get("status") != "pass"]
    change_ids = set(str(item) for item in smoke.get("changeIds") or [])
    if smoke.get("status") == "pass" and smoke.get("version") == version and "STAB-004" in change_ids and not missing and not failing:
        add_check(checks, "Sidecar executable lifecycle smoke", "pass", f"{path}: {len(required_checks)} behavioral checks")
    else:
        add_check(
            checks,
            "Sidecar executable lifecycle smoke",
            "fail",
            f"{path}: status={smoke.get('status')} version={smoke.get('version')} missing={missing} failing={failing} changeIds={sorted(change_ids)}",
            "blocker",
        )


def check_local_path_safety_smoke(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "local-path-safety-smoke.json"
    try:
        smoke = read_json(path)
    except Exception as exc:
        add_check(checks, "STAB-003 local path safety smoke", "fail", f"{path}: {exc}", "blocker")
        return

    required_checks = {
        "managed Chinese and space path",
        "selected path stat",
        "outside path denied",
        "missing path",
        "relative path invalid",
        "dangerous open blocked",
        "dangerous reveal allowed",
        "symlink realpath guard",
        "full-access stat allowed",
    }
    smoke_checks = smoke.get("checks") or []
    by_name = {str(item.get("name") or ""): item for item in smoke_checks if isinstance(item, dict)}
    missing = sorted(required_checks - set(by_name))
    failing = [name for name in sorted(required_checks & set(by_name)) if by_name[name].get("status") != "pass"]
    symlink_check = by_name.get("symlink realpath guard") or {}
    symlink_available = symlink_check.get("symlinkAvailable") is True
    dist_fresh = smoke.get("distFresh") is True
    change_ids = set(str(item) for item in smoke.get("changeIds") or [])
    if (
        smoke.get("status") == "pass"
        and smoke.get("version") == version
        and "STAB-003" in change_ids
        and not missing
        and not failing
        and symlink_available
        and dist_fresh
    ):
        add_check(checks, "STAB-003 local path safety smoke", "pass", f"{path}: {len(required_checks)} path checks")
    else:
        add_check(
            checks,
            "STAB-003 local path safety smoke",
            "fail",
            f"{path}: status={smoke.get('status')} version={smoke.get('version')} missing={missing} failing={failing} symlinkAvailable={symlink_available} distFresh={dist_fresh} changeIds={sorted(change_ids)}",
            "blocker",
        )


def check_webui_local_artifacts(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "webui-local-artifacts.json"
    try:
        evidence = read_json(path)
    except Exception as exc:
        add_check(checks, "WebUI local artifacts", "fail", f"{path}: {exc}", "blocker")
        return

    artifacts = evidence.get("artifacts") or {}
    required = {"webui-windows-x64", "webui-macos-universal", "webui-win-mac"}
    missing = sorted(required - set(artifacts))
    invalid = []
    for artifact_id in sorted(required & set(artifacts)):
        item = artifacts.get(artifact_id) or {}
        artifact_path = pathlib.Path(str(item.get("path") or ""))
        size = int(item.get("size") or 0)
        sha256 = str(item.get("sha256") or "")
        if not artifact_path.is_file() or artifact_path.stat().st_size != size or not SHA256_RE.fullmatch(sha256):
            invalid.append(artifact_id)
    validation = str(evidence.get("validation") or "")
    if evidence.get("ok") is True and evidence.get("version") == version and not missing and not invalid and "PASS" in validation:
        add_check(checks, "WebUI local artifacts", "pass", f"{path}: 3 ZIP artifacts built and validate_zip_assets passed")
    else:
        add_check(
            checks,
            "WebUI local artifacts",
            "fail",
            f"{path}: ok={evidence.get('ok')} version={evidence.get('version')} missing={missing} invalid={invalid} validation={validation}",
            "blocker",
        )


def read_webui_evidence(root: pathlib.Path, version: str) -> dict[str, Any]:
    path = root / "docs" / f"v{version}" / "webui-local-artifacts.json"
    try:
        evidence = read_json(path)
    except Exception:
        return {}
    artifacts = evidence.get("artifacts")
    return artifacts if isinstance(artifacts, dict) else {}


def artifact_ready_status(artifact: dict[str, Any]) -> bool:
    status = str(artifact.get("status") or "")
    artifact_id = str(artifact.get("id") or "")
    if artifact_id in MACOS_DESKTOP_ARTIFACTS:
        return status in {"ready", "ready-unsigned"}
    return status == "ready"


def check_artifact_metadata(root: pathlib.Path, manifest: dict[str, Any], artifact: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    artifact_id = str(artifact.get("id") or "unknown")
    status = str(artifact.get("status") or "")
    if not artifact_ready_status(artifact):
        add_check(checks, f"Artifact ready: {artifact_id}", "fail", f"status={status}", "blocker")
        return

    size_ok = int(artifact.get("size") or 0) > 0
    sha_ok = bool(SHA256_RE.fullmatch(str(artifact.get("sha256") or "")))
    href_ok = bool(str(artifact.get("href") or "").strip())
    updated_ok = bool(str(artifact.get("updatedAt") or "").strip())
    metadata_ok = size_ok and sha_ok and href_ok and updated_ok
    if metadata_ok:
        add_check(checks, f"Artifact metadata: {artifact_id}", "pass", f"status={status} size={artifact.get('size')}")
    else:
        add_check(
            checks,
            f"Artifact metadata: {artifact_id}",
            "fail",
            f"status={status} sizeOk={size_ok} shaOk={sha_ok} hrefOk={href_ok} updatedAtOk={updated_ok}",
            "blocker",
        )

    if artifact_id == "windows-x64":
        if str(artifact.get("signature") or "") == "Valid":
            add_check(checks, "Windows manifest signature", "pass", "windows-x64 signature=Valid")
            smoke_path = root / "docs" / f"v{manifest.get('version')}" / "win-installed-smoke.json"
            try:
                smoke = read_json(smoke_path)
                binding_ok = (
                    str(smoke.get("installerFileName") or "") == str(artifact.get("fileName") or "")
                    and str(smoke.get("installerSha256") or "").upper() == str(artifact.get("sha256") or "").upper()
                    and int(smoke.get("installerSize") or 0) == int(artifact.get("size") or 0)
                )
            except Exception as exc:
                smoke = {}
                binding_ok = False
                binding_error = str(exc)
            else:
                binding_error = ""
            if binding_ok:
                add_check(
                    checks,
                    "Windows installed smoke artifact binding",
                    "pass",
                    f"fileName={smoke.get('installerFileName')} size={smoke.get('installerSize')}",
                )
            else:
                add_check(
                    checks,
                    "Windows installed smoke artifact binding",
                    "fail",
                    f"{smoke_path}: fileName={smoke.get('installerFileName')} sha={smoke.get('installerSha256')} size={smoke.get('installerSize')} manifestFile={artifact.get('fileName')} manifestSha={artifact.get('sha256')} manifestSize={artifact.get('size')} error={binding_error}",
                    "blocker",
                )
        else:
            add_check(
                checks,
                "Windows manifest signature",
                "fail",
                f"windows-x64 signature={artifact.get('signature')}",
                "blocker",
                )

    if artifact_id in WEBUI_ARTIFACTS:
        evidence_artifacts = read_webui_evidence(root, str(manifest.get("version") or ""))
        evidence = evidence_artifacts.get(artifact_id) or {}
        evidence_path = pathlib.Path(str(evidence.get("path") or ""))
        local_ok = evidence_path.is_file()
        local_sha = ""
        local_size = 0
        if local_ok:
            local_size = evidence_path.stat().st_size
            local_sha = sha256_file(evidence_path)
        binding_ok = (
            isinstance(evidence, dict)
            and str(evidence.get("fileName") or evidence_path.name) == str(artifact.get("fileName") or "")
            and str(artifact.get("href") or "") == f"downloads/{artifact.get('fileName')}"
            and int(evidence.get("size") or 0) == int(artifact.get("size") or 0)
            and int(artifact.get("size") or 0) == local_size
            and str(evidence.get("sha256") or "").upper() == str(artifact.get("sha256") or "").upper()
            and str(artifact.get("sha256") or "").upper() == local_sha
        )
        if binding_ok:
            add_check(checks, f"WebUI manifest binding: {artifact_id}", "pass", f"fileName={artifact.get('fileName')} size={artifact.get('size')}")
        else:
            add_check(
                checks,
                f"WebUI manifest binding: {artifact_id}",
                "fail",
                f"localOk={local_ok} evidenceFile={evidence.get('fileName') or evidence_path.name} manifestFile={artifact.get('fileName')} evidenceSize={evidence.get('size')} localSize={local_size} manifestSize={artifact.get('size')} evidenceSha={evidence.get('sha256')} localSha={local_sha} manifestSha={artifact.get('sha256')} href={artifact.get('href')}",
                "blocker",
            )

    if artifact_id in MACOS_DESKTOP_ARTIFACTS:
        smoke = artifact.get("installSmoke") or artifact.get("install_smoke") or {}
        signature = str(artifact.get("signature") or "")
        signed_or_allowed_unsigned = signature == "Valid" or (status == "ready-unsigned" and signature == "unsigned")
        expected_arch = MACOS_ARTIFACT_ARCH[artifact_id]
        expected_file = f"EcoreX_{manifest.get('version')}_{expected_arch}.dmg"
        required_true = [
            "mounted",
            "appFound",
            "copied",
            "launched",
            "versionOk",
            "sidecarReady",
            "authReady",
            "authRequired",
            "authNegativeReady",
            "gatekeeperInstructionShown",
        ]
        missing_true = [key for key in required_true if not isinstance(smoke, dict) or not require_bool(smoke, key)]
        identity_ok = (
            isinstance(smoke, dict)
            and str(artifact.get("fileName") or "") == expected_file
            and str(smoke.get("artifact") or "") == expected_file
            and str(smoke.get("arch") or "") == expected_arch
            and int(smoke.get("bytes") or 0) == int(artifact.get("size") or 0)
        )
        instructions_ok = bool(first_non_empty(smoke, "gatekeeperInstructions", "instructions", "instructionsUrl", "instructions_url")) if isinstance(smoke, dict) else False
        evidence_value = first_non_empty(smoke, "runId", "run_id", "evidenceUrl", "evidence_url", "evidence") if isinstance(smoke, dict) else ""
        placeholder_evidence = evidence_value.rstrip("/") in {"https://github.com/actions/runs", "https://github.com//actions/runs"}
        negative = smoke.get("authNegativeStatuses") if isinstance(smoke, dict) else {}
        missing_negative = sorted(REQUIRED_AUTH_NEGATIVE_STATUSES - set(negative or {}))
        bad_negative = bad_auth_statuses(negative or {})
        smoke_ok = (
            isinstance(smoke, dict)
            and str(smoke.get("status") or "").lower() == "pass"
            and str(smoke.get("version") or "") == str(manifest.get("version") or "")
            and str(smoke.get("sha256") or "").upper() == str(artifact.get("sha256") or "").upper()
            and bool(evidence_value)
            and not placeholder_evidence
            and identity_ok
            and instructions_ok
            and not missing_true
            and not missing_negative
            and not bad_negative
        )
        if signed_or_allowed_unsigned and smoke_ok:
            add_check(checks, f"macOS install smoke: {artifact_id}", "pass", f"signature={signature} artifact={expected_file}")
        else:
            add_check(
                checks,
                f"macOS install smoke: {artifact_id}",
                "fail",
                f"status={status} signature={signature} smokeOk={smoke_ok} identityOk={identity_ok} instructionsOk={instructions_ok} missingTrue={missing_true} missingNegative={missing_negative} badNegative={bad_negative} placeholderEvidence={placeholder_evidence}",
                "blocker",
            )


def check_manifest(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    manifest_path = root / "deploy" / "ecorex-site" / "manifest.json"
    try:
        manifest = read_json(manifest_path)
    except Exception as exc:
        add_check(checks, "Public manifest", "fail", f"{manifest_path}: {exc}", "blocker")
        return

    if manifest.get("product") == "EcoreX" and manifest.get("version") == version:
        add_check(checks, "Public manifest version", "pass", f"{manifest_path}: EcoreX {version}")
    else:
        add_check(
            checks,
            "Public manifest version",
            "fail",
            f"{manifest_path}: product={manifest.get('product')} version={manifest.get('version')}",
            "blocker",
        )

    artifacts = {str(item.get("id") or ""): item for item in manifest.get("artifacts") or []}
    missing = sorted(REQUIRED_PUBLIC_ARTIFACTS - set(artifacts))
    if missing:
        add_check(checks, "Required public artifacts", "fail", f"missing={missing}", "blocker")
    else:
        add_check(checks, "Required public artifacts", "pass", f"{len(REQUIRED_PUBLIC_ARTIFACTS)} required artifacts listed")

    for artifact_id in sorted(REQUIRED_PUBLIC_ARTIFACTS & set(artifacts)):
        check_artifact_metadata(root, manifest, artifacts[artifact_id], checks)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--version", default="0.1.17")
    parser.add_argument("--output", default="")
    parser.add_argument("--allow-no-go", action="store_true", help="Write the report and return 0 even when blockers remain")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    checks: list[dict[str, Any]] = []
    check_version_files(root, args.version, checks)
    check_skill_evidence(root, args.version, checks)
    check_office_preinstall_smoke(root, args.version, checks)
    check_proactive_memory_smoke(root, args.version, checks)
    check_server_release_gate_smoke(root, args.version, checks)
    check_win_unpacked_smoke(root, args.version, checks)
    check_win_installed_smoke(root, args.version, checks)
    check_acceptance_smoke(root, args.version, checks)
    check_xhs_markdown_image_stability_smoke(root, args.version, checks)
    check_local_path_safety_smoke(root, args.version, checks)
    check_sidecar_contract_smoke(root, args.version, checks)
    check_sidecar_lifecycle_smoke(root, args.version, checks)
    check_webui_local_artifacts(root, args.version, checks)
    check_manifest(root, args.version, checks)

    blockers = [item for item in checks if item["status"] == "fail" and item.get("severity") == "blocker"]
    warnings = [item for item in checks if item.get("severity") == "warn" or item["status"] in {"skipped", "missing"}]
    report = {
        "product": "EcoreX",
        "version": args.version,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": "go" if not blockers else "no-go",
        "summary": {
            "total": len(checks),
            "blockers": len(blockers),
            "warnings": len(warnings),
        },
        "checks": checks,
    }

    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = pathlib.Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    if blockers and not args.allow_no_go:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
