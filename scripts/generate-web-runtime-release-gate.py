#!/usr/bin/env python3
"""Generate EcoreX Web runtime release-gate snapshots.

This script is intentionally read-only with respect to runtime capabilities:
it captures current state, audits declarative manifests, and evaluates the
permission broker in a temporary user-data directory.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.ecorex_public_payload import mask_sensitive_text  # noqa: E402


SCHEMA_VERSION = "web-release-gate-v1"
CAPABILITY_STATE_SCHEMA = "web-capability-state-snapshot-v1"
PERMISSION_MATRIX_SCHEMA = "web-permission-matrix-v1"

FORBIDDEN_MANIFEST_FIELDS = {
    "absolutePath",
    "commandPath",
    "configPath",
    "envPath",
    "installRoot",
    "logPath",
    "manifestPath",
    "pathOverride",
    "pathPrepend",
    "privateStatePath",
    "runtimePath",
    "scriptPath",
    "stateDir",
    "targetDir",
    "targetRoot",
    "workspacePath",
    "absolute_path",
    "command_path",
    "config_path",
    "env_path",
    "install_root",
    "log_path",
    "manifest_path",
    "path_override",
    "path_prepend",
    "private_state_path",
    "runtime_path",
    "script_path",
    "state_dir",
    "target_dir",
    "target_root",
    "workspace_path",
}
_FORBIDDEN_MANIFEST_FIELD_KEYS = {
    re.sub(r"[^a-z0-9]", "", field.lower())
    for field in FORBIDDEN_MANIFEST_FIELDS
}

REPAIR_ACTION_TEMPLATE = "install-capability --action repair --pack-id {pack_id}"
DISCOVER_ACTION_TEMPLATE = "find-skill --capability {pack_id}"
CONFIGURE_ACTION_TEMPLATE = "configure-capability --pack-id {pack_id}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _redact_path(
    value: str,
    runtime_root: Path,
    state_root: Path,
    output_dir: Path,
    workspace_root: Optional[Path] = None,
) -> str:
    text = str(value or "").replace("\\", "/")
    roots = [
        ("%OUTPUT_DIR%", output_dir),
        ("%STATE_ROOT%", state_root),
        ("%RUNTIME_ROOT%", runtime_root),
    ]
    if workspace_root is not None:
        roots.append(("%WORKSPACE_ROOT%", workspace_root))
    for marker, root in roots:
        raw = str(root.resolve()).replace("\\", "/").rstrip("/")
        if raw:
            text = re.sub(re.escape(raw), marker, text, flags=re.IGNORECASE)
    home = str(Path.home()).replace("\\", "/").rstrip("/")
    if home:
        text = re.sub(re.escape(home), "%USERPROFILE%", text, flags=re.IGNORECASE)
    return text


def _redact_report_paths(
    value: Any,
    runtime_root: Path,
    state_root: Path,
    output_dir: Path,
    workspace_root: Optional[Path] = None,
) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_report_paths(item, runtime_root, state_root, output_dir, workspace_root)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_report_paths(item, runtime_root, state_root, output_dir, workspace_root) for item in value]
    if isinstance(value, str):
        return _redact_path(value, runtime_root, state_root, output_dir, workspace_root)
    return value


@contextlib.contextmanager
def _patched_env(values: Dict[str, Optional[str]]) -> Iterator[None]:
    old_values: Dict[str, Optional[str]] = {}
    for key, value in values.items():
        old_values[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _manifest_candidates(runtime_root: Path) -> List[Path]:
    return [
        runtime_root / "capabilities.json",
        runtime_root / "runtime-packs" / "capabilities.json",
        ROOT / "runtime-packs" / "capabilities.json",
    ]


def _resolve_manifest(runtime_root: Path) -> Path:
    for candidate in _manifest_candidates(runtime_root):
        if candidate.is_file():
            return candidate.resolve()
    return (runtime_root / "capabilities.json").resolve()


def _pack_id(pack: Dict[str, Any]) -> str:
    return str(pack.get("id") or "").strip()


def _is_repairable_pack(pack: Dict[str, Any]) -> bool:
    if pack.get("configureOnly") is True or pack.get("discoveryOnly") is True:
        return False
    return bool(pack.get("requirements") or pack.get("moduleChecks") or pack.get("postInstallCommands"))


def _expected_repair_action(pack_id: str) -> str:
    return REPAIR_ACTION_TEMPLATE.format(pack_id=pack_id)


def _expected_discover_action(pack_id: str) -> str:
    return DISCOVER_ACTION_TEMPLATE.format(pack_id=pack_id)


def _expected_configure_action(pack_id: str) -> str:
    return CONFIGURE_ACTION_TEMPLATE.format(pack_id=pack_id)


def _is_forbidden_manifest_field(field: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(field or "").lower())
    return normalized in _FORBIDDEN_MANIFEST_FIELD_KEYS


def _walk_manifest_fields(value: Any, path: str = "$") -> Iterable[tuple[str, Any, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            current = f"{path}.{key}"
            yield str(key), item, current
            yield from _walk_manifest_fields(item, current)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_manifest_fields(item, f"{path}[{index}]")


def _audit_capability_manifest(runtime_root: Path) -> Dict[str, Any]:
    manifest_path = _resolve_manifest(runtime_root)
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    try:
        manifest = _load_json(manifest_path, {})
    except Exception as exc:
        manifest = {}
        blockers.append({
            "id": "manifest.json",
            "message": f"capability manifest is not readable JSON: {exc}",
        })
    packs = manifest.get("packs") if isinstance(manifest, dict) else None
    if not isinstance(packs, list):
        blockers.append({
            "id": "manifest.packs",
            "message": "capability manifest must contain a packs list",
        })
        packs = []

    seen: set[str] = set()
    audited_packs: List[Dict[str, Any]] = []
    for index, pack in enumerate(packs):
        if not isinstance(pack, dict):
            blockers.append({"id": f"packs[{index}]", "message": "pack entry must be an object"})
            continue
        pack_id = _pack_id(pack)
        if not pack_id:
            blockers.append({"id": f"packs[{index}].id", "message": "pack id is required"})
            continue
        if pack_id in seen:
            blockers.append({"id": f"packs[{index}].id", "message": f"duplicate pack id: {pack_id}"})
        seen.add(pack_id)

        for field, _value, field_path in _walk_manifest_fields(pack, f"packs[{index}]"):
            if _is_forbidden_manifest_field(field):
                blockers.append({
                    "id": f"{pack_id}.forbiddenField",
                    "message": f"forbidden private runtime/path field {field_path}",
                })

        expected_repair = _expected_repair_action(pack_id)
        repairable = _is_repairable_pack(pack)
        repair_action = str(pack.get("repairAction") or "").strip()
        discover_action = str(pack.get("discoverAction") or "").strip()
        configure_action = str(pack.get("configureAction") or "").strip()
        expected_discover = _expected_discover_action(pack_id)
        expected_configure = _expected_configure_action(pack_id)
        if repairable and repair_action != expected_repair:
            blockers.append({
                "id": f"{pack_id}.repairAction",
                "message": f"repairable packs must declare repairAction={expected_repair}",
            })
        if pack.get("discoveryOnly") is True and discover_action != expected_discover:
            blockers.append({
                "id": f"{pack_id}.discoverAction",
                "message": f"discoveryOnly packs must declare discoverAction={expected_discover}",
            })
        if pack.get("configureOnly") is True and configure_action != expected_configure:
            blockers.append({
                "id": f"{pack_id}.configureAction",
                "message": f"configureOnly packs must declare configureAction={expected_configure}",
            })
        if not repairable and repair_action and repair_action != expected_repair:
            warnings.append({
                "id": f"{pack_id}.repairAction",
                "message": "repairAction is present but does not match the public installer contract",
            })

        audited_packs.append({
            "id": pack_id,
            "installMode": pack.get("installMode") or "",
            "configureOnly": bool(pack.get("configureOnly")),
            "discoveryOnly": bool(pack.get("discoveryOnly")),
            "repairable": repairable,
            "repairAction": repair_action,
            "discoverAction": discover_action,
            "configureAction": configure_action,
            "requirements": list(pack.get("requirements") or []),
            "moduleChecks": list(pack.get("moduleChecks") or []),
        })

    return {
        "schemaVersion": "web-capability-manifest-audit-v1",
        "manifestPath": str(manifest_path),
        "packCount": len(audited_packs),
        "packs": audited_packs,
        "blockers": blockers,
        "warnings": warnings,
        "status": "pass" if not blockers else "fail",
    }


def _capture_baseline(args: argparse.Namespace, runtime_root: Path, state_root: Path, output_dir: Path) -> Dict[str, Any]:
    output = output_dir / "runtime-baseline.json"
    if args.baseline_input:
        source = Path(args.baseline_input).resolve()
        payload = _load_json(source, {})
        if not isinstance(payload, dict):
            payload = {}
        output.parent.mkdir(parents=True, exist_ok=True)
        if source != output.resolve():
            shutil.copyfile(source, output)
        return payload

    if args.skip_baseline_capture:
        payload = _load_json(output, {})
        return payload if isinstance(payload, dict) else {}

    checker = runtime_root / "scripts" / "check-web-core-runtime-baseline.py"
    if not checker.is_file():
        checker = ROOT / "scripts" / "check-web-core-runtime-baseline.py"
    if not checker.is_file():
        return {
            "schemaVersion": "web-core-runtime-baseline-v1",
            "summary": {
                "releaseReady": False,
                "blocking": 1,
                "blockingNames": ["check-web-core-runtime-baseline.py"],
            },
            "error": "baseline checker missing",
        }

    env = os.environ.copy()
    env.update({
        "ECOREX_STATE_DIR": str(state_root),
        "ECOREX_CAPABILITY_STATE_DIR": str(state_root / "capability-state"),
        "ECOREX_CAPABILITY_TARGET_DIR": str(state_root / "capability-packages"),
    })
    command = [
        sys.executable,
        str(checker),
        "--runtime-root",
        str(runtime_root),
        "--state-root",
        str(state_root),
        "--output",
        str(output),
    ]
    if args.strict:
        command.append("--strict")
    result = subprocess.run(command, text=True, encoding="utf-8", errors="replace", env=env)
    payload = _load_json(output, {})
    if result.returncode != 0 and isinstance(payload, dict):
        payload.setdefault("summary", {})
        payload["summary"]["releaseReady"] = False
        payload["summary"]["blocking"] = max(1, int(payload["summary"].get("blocking") or 0))
        payload.setdefault("errors", []).append({
            "source": "check-web-core-runtime-baseline.py",
            "exitCode": result.returncode,
        })
    return payload if isinstance(payload, dict) else {}


def _capture_capability_state(runtime_root: Path, state_root: Path, workspace_root: Path) -> Dict[str, Any]:
    sys.path.insert(0, str(runtime_root))
    with _patched_env({
        "ECOREX_STATE_DIR": str(state_root),
        "ECOREX_CAPABILITY_STATE_DIR": str(state_root / "capability-state"),
        "ECOREX_CAPABILITY_TARGET_DIR": str(state_root / "capability-packages"),
    }):
        try:
            from agent.runtime_capabilities import CapabilityService, RuntimeCapabilityRegistry

            registry = RuntimeCapabilityRegistry(str(workspace_root), probe_installer_status=False)
            payload = CapabilityService(registry).capabilities_payload(include_related=False)
            status = payload.get("status") or "success"
            return {
                "schemaVersion": CAPABILITY_STATE_SCHEMA,
                "generatedAt": _utc_now(),
                "status": status,
                "source": "runtime-capability-service",
                "capabilities": payload,
                "summary": payload.get("summary") or {},
                "visualWorkflow": payload.get("visualWorkflow") or {},
            }
        except Exception as exc:
            return {
                "schemaVersion": CAPABILITY_STATE_SCHEMA,
                "generatedAt": _utc_now(),
                "status": "error",
                "source": "runtime-capability-service",
                "error": str(exc),
                "summary": {},
                "visualWorkflow": {},
            }


def _permission_cases(workspace_root: Path) -> List[Dict[str, Any]]:
    workspace_file = workspace_root / "release-gate-note.md"
    return [
        {"id": "optional_abilities.status", "capability": "optional_abilities", "action": "status"},
        {"id": "agent_capability.diagnose", "capability": "agent_capability", "action": "diagnose"},
        {"id": "scheduler.list", "capability": "scheduler", "action": "list"},
        {"id": "image_jobs.status", "capability": "image_jobs", "action": "status"},
        {"id": "browser.snapshot", "capability": "browser", "action": "snapshot"},
        {
            "id": "workspace.read",
            "capability": "workspace",
            "action": "read",
            "resource": str(workspace_file),
            "cwd": str(workspace_root),
        },
        {
            "id": "artifact.read",
            "capability": "artifact",
            "action": "read",
            "resource": str(workspace_file),
            "cwd": str(workspace_root),
        },
        {
            "id": "bash.workspace_write",
            "capability": "bash",
            "action": "workspace_write",
            "resource": str(workspace_file),
            "cwd": str(workspace_root),
        },
        {
            "id": "bash.system_shell",
            "capability": "bash",
            "action": "system_shell",
            "arguments": {"command": "whoami"},
        },
        {
            "id": "feishu_cli.run.read",
            "capability": "feishu_cli",
            "action": "run",
            "arguments": {"action": "run", "args": ["base", "+record-list", "--as", "user"]},
        },
        {
            "id": "feishu_cli.run.write",
            "capability": "feishu_cli",
            "action": "run",
            "arguments": {"action": "run", "args": ["im", "+message-send", "--chat-id", "oc_x"]},
        },
        {
            "id": "feishu_cli.run.admin",
            "capability": "feishu_cli",
            "action": "run",
            "arguments": {"action": "run", "args": ["drive", "+permission-member-create"]},
        },
        {
            "id": "image_jobs.start.background",
            "capability": "image_jobs",
            "action": "start",
            "arguments": {"action": "start"},
        },
        {
            "id": "image_jobs.start.user_initiated",
            "capability": "image_jobs",
            "action": "start",
            "arguments": {"action": "start"},
            "metadata": {"user_initiated": True},
        },
    ]


def _expected_permission(case_id: str, mode: str) -> Optional[bool]:
    low_risk = {
        "optional_abilities.status",
        "agent_capability.diagnose",
        "scheduler.list",
        "image_jobs.status",
        "browser.snapshot",
        "workspace.read",
        "artifact.read",
        "feishu_cli.run.read",
    }
    if case_id in low_risk:
        return True
    if case_id == "image_jobs.start.user_initiated":
        return mode != "read-only"
    if case_id in {"bash.system_shell", "feishu_cli.run.write", "feishu_cli.run.admin", "image_jobs.start.background"}:
        return mode == "full-access"
    if case_id == "bash.workspace_write":
        return mode in {"smart-ask", "custom", "full-access"}
    return None


def _generate_permission_matrix(state_root: Path, workspace_root: Path) -> Dict[str, Any]:
    modes = ["read-only", "smart-ask", "custom", "full-access"]
    rows: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="ecorex-permission-gate-") as tmp:
        probe_workspace = Path(tmp) / "workspace"
        probe_workspace.mkdir(parents=True, exist_ok=True)
        (probe_workspace / "release-gate-note.md").write_text("release gate permission probe\n", encoding="utf-8")
        with _patched_env({
            "ECOREX_USER_DATA": str(Path(tmp) / "user-data"),
            "ECOREX_DESKTOP_USER_DATA": None,
        }):
            from common.ecorex_tool_permissions import ToolPermissionBroker

            for mode in modes:
                broker = ToolPermissionBroker()
                broker.set_mode(mode)
                if mode == "custom":
                    broker.remember_workspace_root(str(probe_workspace), access="write", cwd=str(probe_workspace))
                for case in _permission_cases(probe_workspace):
                    decision = broker.authorize_capability(
                        case["capability"],
                        case.get("action", ""),
                        resource=case.get("resource", ""),
                        arguments=case.get("arguments"),
                        metadata=case.get("metadata"),
                        cwd=case.get("cwd"),
                    )
                    expected = _expected_permission(str(case["id"]), mode)
                    row = {
                        "mode": mode,
                        "caseId": case["id"],
                        "capability": case["capability"],
                        "action": case.get("action", ""),
                        "allowed": bool(decision.get("allowed")),
                        "expectedAllowed": expected,
                        "matchesExpected": expected is None or bool(decision.get("allowed")) == expected,
                        "reason": decision.get("reason") or "",
                    }
                    if decision.get("classification"):
                        row["classification"] = decision.get("classification")
                    rows.append(row)
                    if row["matchesExpected"] is False:
                        blockers.append({
                            "id": f"{mode}.{case['id']}",
                            "message": f"expected allowed={expected}, got {row['allowed']}",
                            "reason": row["reason"],
                        })
    return {
        "schemaVersion": PERMISSION_MATRIX_SCHEMA,
        "generatedAt": _utc_now(),
        "modes": modes,
        "rows": rows,
        "summary": {
            "total": len(rows),
            "allowed": sum(1 for row in rows if row.get("allowed")),
            "denied": sum(1 for row in rows if not row.get("allowed")),
            "blockers": len(blockers),
        },
        "blockers": blockers,
        "probeWorkspace": "temporary-cleaned",
        "providedWorkspaceMutated": False,
        "status": "pass" if not blockers else "fail",
    }


def _review_consensus_markdown(report: Dict[str, Any]) -> str:
    status = "PASS" if report.get("summary", {}).get("releaseReady") else "FAIL"
    checks = report.get("checks") or {}
    lines = [
        "# EcoreX Web Runtime Release Gate Consensus",
        "",
        f"- Generated at: {report.get('generatedAt')}",
        f"- Automated status: {status}",
        "- Scope: Web service package / Web runtime / public agent runtime only.",
        "- Desktop/Electron scope: excluded.",
        "",
        "## Automated Checks",
        "",
    ]
    for check_id, check in checks.items():
        artifact = check.get("artifact") or ""
        suffix = f" ({artifact})" if artifact else ""
        lines.append(f"- {check_id}: {check.get('status')}{suffix}")
        if check.get("error"):
            lines.append(f"  - error: {check.get('error')}")
        blockers = check.get("blockers") if isinstance(check.get("blockers"), list) else []
        warnings = check.get("warnings") if isinstance(check.get("warnings"), list) else []
        for blocker in blockers[:10]:
            if isinstance(blocker, dict):
                lines.append(f"  - blocker: {blocker.get('message') or blocker.get('id')}")
        for warning in warnings[:10]:
            if isinstance(warning, dict):
                lines.append(f"  - warning: {warning.get('message') or warning.get('id')}")
    lines.extend([
        "",
        "## Human Review Gate",
        "",
        "This file is generated for each Web release snapshot. Final publishing still requires the S9 multi-agent consensus record in docs/web-runtime-goal/reviews/S09-consensus.md.",
        "",
    ])
    return "\n".join(lines)


def _check_status_from_bool(ok: bool) -> str:
    return "pass" if ok else "fail"


def _failure_text(error: BaseException) -> str:
    message = str(error) or error.__class__.__name__
    return message[:1000]


def _redacted_failure_text(
    error: BaseException,
    *,
    runtime_root: Path,
    state_root: Path,
    output_dir: Path,
    workspace_root: Optional[Path] = None,
) -> str:
    path_redacted = _redact_report_paths(
        _failure_text(error),
        runtime_root,
        state_root,
        output_dir,
        workspace_root,
    )
    return mask_sensitive_text(path_redacted, max_chars=1000)


def _ensure_failure_artifacts(
    *,
    runtime_root: Path,
    state_root: Path,
    output_dir: Path,
    workspace_root: Optional[Path] = None,
    stage: str,
    error: BaseException,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    error_message = _redacted_failure_text(
        error,
        runtime_root=runtime_root,
        state_root=state_root,
        output_dir=output_dir,
        workspace_root=workspace_root,
    )
    baseline_path = output_dir / "runtime-baseline.json"
    redaction_args = (runtime_root, state_root, output_dir, workspace_root)
    _write_json(baseline_path, _redact_report_paths({
        "schemaVersion": "web-core-runtime-baseline-v1",
        "generatedAt": _utc_now(),
        "runtimeRoot": str(runtime_root),
        "stateRoot": str(state_root),
        "summary": {
            "releaseReady": False,
            "blocking": 1,
            "blockingNames": [stage],
        },
        "errors": [{"stage": stage, "message": error_message}],
    }, *redaction_args))
    capability_path = output_dir / "capability-state.json"
    _write_json(capability_path, _redact_report_paths({
        "schemaVersion": CAPABILITY_STATE_SCHEMA,
        "generatedAt": _utc_now(),
        "status": "error",
        "source": "release-gate",
        "error": error_message,
        "summary": {},
        "visualWorkflow": {},
    }, *redaction_args))
    permission_path = output_dir / "permission-matrix.json"
    _write_json(permission_path, _redact_report_paths({
        "schemaVersion": PERMISSION_MATRIX_SCHEMA,
        "generatedAt": _utc_now(),
        "modes": [],
        "rows": [],
        "summary": {"total": 0, "allowed": 0, "denied": 0, "blockers": 1},
        "blockers": [{"id": stage, "message": error_message}],
        "probeWorkspace": "not-created",
        "providedWorkspaceMutated": False,
        "status": "fail",
    }, *redaction_args))
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": _utc_now(),
        "runtimeRoot": str(runtime_root),
        "stateRoot": str(state_root),
        "outputDir": str(output_dir),
        "summary": {
            "releaseReady": False,
            "blocking": 1,
            "blockingChecks": [stage],
        },
        "checks": {
            "releaseGate": {
                "status": "fail",
                "artifact": "web-release-gate.json",
                "blocking": True,
                "stage": stage,
                "error": error_message,
            },
        },
        "manifestAudit": {
            "schemaVersion": "web-capability-manifest-audit-v1",
            "manifestPath": str(_resolve_manifest(runtime_root)),
            "packCount": 0,
            "packs": [],
            "blockers": [{"id": stage, "message": error_message}],
            "warnings": [],
            "status": "fail",
        },
    }
    redacted = _redact_report_paths(report, runtime_root, state_root, output_dir, workspace_root)
    _write_json(output_dir / "web-release-gate.json", redacted)
    (output_dir / "review-consensus.md").write_text(_review_consensus_markdown(redacted), encoding="utf-8")
    return redacted


def _capture_release_gate_inner(
    args: argparse.Namespace,
    *,
    runtime_root: Path,
    state_root: Path,
    output_dir: Path,
    workspace_root: Path,
) -> Dict[str, Any]:
    baseline = _capture_baseline(args, runtime_root, state_root, output_dir)
    baseline_ready = bool((baseline.get("summary") or {}).get("releaseReady"))
    manifest_audit = _audit_capability_manifest(runtime_root)
    capability_state = _redact_report_paths(
        _capture_capability_state(runtime_root, state_root, workspace_root),
        runtime_root,
        state_root,
        output_dir,
        workspace_root,
    )
    permission_matrix = _redact_report_paths(
        _generate_permission_matrix(state_root, workspace_root),
        runtime_root,
        state_root,
        output_dir,
        workspace_root,
    )

    _write_json(output_dir / "capability-state.json", capability_state)
    _write_json(output_dir / "permission-matrix.json", permission_matrix)

    capability_state_ok = capability_state.get("status") in {"success", "pass"}
    manifest_ok = manifest_audit.get("status") == "pass"
    permission_ok = permission_matrix.get("status") == "pass"
    release_ready = bool(baseline_ready and manifest_ok and permission_ok and capability_state_ok)
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": _utc_now(),
        "runtimeRoot": str(runtime_root),
        "stateRoot": str(state_root),
        "outputDir": str(output_dir),
        "summary": {
            "releaseReady": release_ready,
            "blocking": sum(1 for ok in (baseline_ready, manifest_ok, permission_ok, capability_state_ok) if not ok),
            "blockingChecks": [
                name
                for name, ok in (
                    ("runtimeBaseline", baseline_ready),
                    ("capabilityManifest", manifest_ok),
                    ("permissionMatrix", permission_ok),
                    ("capabilityState", capability_state_ok),
                )
                if not ok
            ],
        },
        "checks": {
            "runtimeBaseline": {
                "status": _check_status_from_bool(baseline_ready),
                "artifact": "runtime-baseline.json",
                "blocking": not baseline_ready,
            },
            "capabilityManifest": {
                "status": manifest_audit.get("status"),
                "artifact": "web-release-gate.json#manifestAudit",
                "blocking": not manifest_ok,
                "blockers": manifest_audit.get("blockers") or [],
                "warnings": manifest_audit.get("warnings") or [],
                "packCount": manifest_audit.get("packCount") or 0,
            },
            "capabilityState": {
                "status": _check_status_from_bool(capability_state_ok),
                "artifact": "capability-state.json",
                "blocking": not capability_state_ok,
            },
            "permissionMatrix": {
                "status": permission_matrix.get("status"),
                "artifact": "permission-matrix.json",
                "blocking": not permission_ok,
                "blockers": permission_matrix.get("blockers") or [],
            },
        },
        "manifestAudit": manifest_audit,
    }
    redacted_report = _redact_report_paths(report, runtime_root, state_root, output_dir, workspace_root)
    _write_json(output_dir / "web-release-gate.json", redacted_report)
    (output_dir / "review-consensus.md").write_text(_review_consensus_markdown(redacted_report), encoding="utf-8")
    return redacted_report


def capture_release_gate(args: argparse.Namespace) -> Dict[str, Any]:
    runtime_root = Path(args.runtime_root).resolve()
    state_root = Path(args.state_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    workspace_root = Path(args.workspace_root).resolve() if args.workspace_root else state_root / "workspace"
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _capture_release_gate_inner(
            args,
            runtime_root=runtime_root,
            state_root=state_root,
            output_dir=output_dir,
            workspace_root=workspace_root,
        )
    except Exception as exc:
        return _ensure_failure_artifacts(
            runtime_root=runtime_root,
            state_root=state_root,
            output_dir=output_dir,
            workspace_root=workspace_root,
            stage="releaseGate",
            error=exc,
        )


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=ROOT, help="Runtime root to inspect.")
    parser.add_argument("--state-root", type=Path, default=ROOT / "state", help="EcoreX Web state root.")
    parser.add_argument("--workspace-root", type=Path, default=None, help="Workspace root for permission probes.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "state", help="Directory for release-gate artifacts.")
    parser.add_argument("--baseline-input", type=Path, default=None, help="Existing runtime-baseline.json to copy into output-dir.")
    parser.add_argument("--skip-baseline-capture", action="store_true", help="Reuse output-dir/runtime-baseline.json.")
    parser.add_argument("--strict", action="store_true", help="Exit nonzero when releaseReady is false.")
    parser.add_argument("--no-write", action="store_true", help="Print the release report only; artifacts are still generated.")
    return parser.parse_args(argv[1:])


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    report = capture_release_gate(args)
    if args.no_write:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(str(Path(args.output_dir).resolve() / "web-release-gate.json"))
    if args.strict and not bool(report.get("summary", {}).get("releaseReady")):
        blocking = ", ".join(report.get("summary", {}).get("blockingChecks") or [])
        print(f"ERROR Web release gate is not ready: {blocking}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
