#!/usr/bin/env python3
"""Validate the v0.2.2 release gate state.

This checker is intentionally stricter than the harness matrix checker: a
coherent release gate may be valid while still reporting BLOCKED. Use
`--require-releasable` only when preparing an actual release candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "v0.2.2"
ARTIFACTS = DOCS / "artifacts"
MATRIX_PATH = DOCS / "harness-matrix.json"
ACCEPTANCE_PATH = DOCS / "acceptance-checklist.md"
EVIDENCE_PATH = DOCS / "evidence-ledger.md"
REVIEW_PATH = DOCS / "review-log.md"
RELEASE_GATE_DOC = DOCS / "release-gate.md"
RELEASE_MANIFEST = DOCS / "release-manifest.md"
PUBLIC_SITE_MANIFEST = ROOT / "deploy" / "ecorex-site" / "manifest.json"
PUBLIC_SITE_ROOT = ROOT / "deploy" / "ecorex-site"
DEPLOY_SMOKE_ARTIFACT = ARTIFACTS / "release-deploy-rollback-smoke.json"
TARGET_DEPLOY_SMOKE_ARTIFACT = ARTIFACTS / "release-target-deploy-rollback-smoke.json"
TARGET_COMMAND_TEMPLATE_ARTIFACT = ARTIFACTS / "release-target-command-template.json"
PRODUCTION_DEPLOY_ONLINE_ARTIFACT = ARTIFACTS / "production-deploy-online.json"
ONLINE_WEB_BROWSER_SMOKE_ARTIFACT = ARTIFACTS / "online-web-browser-smoke.json"
ONLINE_WEB_BROWSER_SMOKE_WAIVER_ARTIFACT = ARTIFACTS / "online-web-browser-smoke-waiver.json"
FEISHU_IM_SMOKE_ARTIFACT = ARTIFACTS / "feishu-im-real-credential-smoke.json"
EXPECTED_RELEASE_VERSION = "0.2.2"
EXPECTED_WEB_SERVICE_TARBALL = f"release-artifacts/EcoreX_{EXPECTED_RELEASE_VERSION}-web-linux-service.tar.gz"
EXPECTED_WEB_SERVICE_SHA256 = "3BEA1EF91C61E9E42235AE7695DDAEBEF25B4A6C5B13B6726240539CC937CCF7"
EXPECTED_WEB_SERVICE_SIZE = 3679009
EXPECTED_WEB_SERVICE_FILENAME = f"EcoreX_{EXPECTED_RELEASE_VERSION}-web-linux-service.tar.gz"
EXPECTED_PUBLIC_RELEASE_ZIP = f"release-artifacts/EcoreX_{EXPECTED_RELEASE_VERSION}-public-release.zip"
EXPECTED_PUBLIC_RELEASE_ZIP_SHA256 = "BFA0DD949907ECE14787FB5C1D32F3163C42E72ABFB9A83EF9A7BE8FE6DD5F7C"
EXPECTED_PUBLIC_RELEASE_ZIP_SIZE = 264864808
EXPECTED_ONLINE_SMOKE_WAIVER_REASON = "operator-requested-skip"
EXPECTED_PUBLIC_ARTIFACTS = {
    "web-linux-service": {
        "version": EXPECTED_RELEASE_VERSION,
        "fileName": EXPECTED_WEB_SERVICE_FILENAME,
        "href": f"downloads/{EXPECTED_WEB_SERVICE_FILENAME}",
        "size": EXPECTED_WEB_SERVICE_SIZE,
        "sha256": EXPECTED_WEB_SERVICE_SHA256,
    },
    "webui-windows-x64": {
        "version": EXPECTED_RELEASE_VERSION,
        "fileName": f"EcoreX_{EXPECTED_RELEASE_VERSION}-webui-windows-x64.zip",
        "href": f"downloads/EcoreX_{EXPECTED_RELEASE_VERSION}-webui-windows-x64.zip",
        "size": 83385608,
        "sha256": "BE25FAE0B33DAFF66EA7C0749B21A6F3198C021C43B87DA3123F3666E41A96F1",
    },
    "webui-macos-universal": {
        "version": EXPECTED_RELEASE_VERSION,
        "fileName": f"EcoreX_{EXPECTED_RELEASE_VERSION}-webui-macos-universal.zip",
        "href": f"downloads/EcoreX_{EXPECTED_RELEASE_VERSION}-webui-macos-universal.zip",
        "size": 175711756,
        "sha256": "2FD49E130040CAF5E98F2038465441A21F3A995B41316F9493C68299A8FDE261",
    },
}
EXPECTED_FEISHU_IM_COMMAND = "lark-cli im +chat-list --as user --page-size 1 --format json"
TARGET_TEMPLATE_LOCAL_INPUT_PATHS = {
    "package": EXPECTED_WEB_SERVICE_TARBALL,
    "installer": "scripts/install-ecorex-web.sh",
    "checker": "scripts/check-ecorex-web-release.sh",
}
TARGET_DEPLOY_COMMAND_SEQUENCE = [
    "prepare_remote_dir",
    "upload_package",
    "upload_installer",
    "upload_checker",
    "chmod_release_scripts",
    "capture_pre_state",
    "install_v022",
    "check_deploy",
    "capture_deploy_state",
    "rollback_to_previous",
    "capture_rollback_state",
]
TARGET_DEPLOY_COMMAND_SEQUENCE_WITH_REBUILT_ROLLBACK_BASELINE = [
    "prepare_remote_dir",
    "upload_package",
    "upload_installer",
    "upload_checker",
    "chmod_release_scripts",
    "upload_rollback_baseline_package",
    "capture_pre_state",
    "install_v022",
    "check_deploy",
    "capture_deploy_state",
    "rollback_to_previous",
    "capture_rollback_state",
]
SECRET_TOKEN_PATTERN = re.compile(r"sk-[A-Za-z0-9]{20,}")
RAW_FEISHU_ID_PATTERN = re.compile(r"\b(?:ou|oc|om|omt|cli)_[A-Za-z0-9_]{8,}\b")
HASH_SHAPE_PATTERN = re.compile(r"[A-F0-9]{16}|[A-F0-9]{32}|[A-F0-9]{64}")
ALLOWED_TARGET_BLOCKED_REASONS = {
    "missing-target-confirmation",
    "missing-ssh-host",
    "missing-local-file",
    "package-sha256-mismatch",
    "pre-state-version-not-rollback-target",
    "pre-state-current-missing",
    "deploy-state-version-mismatch",
    "deploy-service-inactive",
    "rollback-state-version-mismatch",
    "rollback-service-inactive",
    "public-http-probe-failed",
    "candidate-retention-unproven",
    "command-failed",
    "command-timeout",
    "command-start-failed",
    "target-state-unparseable",
    "target-smoke-failed",
}

REQUIRED_ACCEPTANCE_IDS = {f"R22-{index:02d}" for index in range(1, 20)}
REQUIRED_RELEASE_SCRIPTS = [
    "scripts/prepare-ecorex-public-release.ps1",
    "scripts/prepare-ecorex-web-release.ps1",
    "scripts/prepare-ecorex-webui-local-release.ps1",
    "scripts/validate-ecorex-release-artifacts.py",
    "scripts/check-ecorex-server-release.sh",
    "scripts/check-ecorex-web-release.sh",
    "scripts/install-ecorex-public-release.sh",
]
RELEASE_DEFAULT_VERSION_FILES = [
    ("scripts/install-ecorex-web.sh", r'VERSION="\$\{VERSION:-([^}"]+)\}"'),
    ("scripts/check-ecorex-web-release.sh", r'VERSION="\$\{VERSION:-([^}"]+)\}"'),
    ("scripts/check-ecorex-server-release.sh", r'VERSION="\$\{VERSION:-([^}"]+)\}"'),
    ("scripts/install-ecorex-public-release.sh", r'VERSION="\$\{VERSION:-([^}"]+)\}"'),
    ("scripts/prepare-ecorex-web-release.ps1", r'\[string\]\$Version\s*=\s*"([^"]+)"'),
    ("scripts/prepare-ecorex-webui-local-release.ps1", r'\[string\]\$Version\s*=\s*"([^"]+)"'),
]
BLOCKED_MANIFEST_MARKERS_BY_BLOCKER = {
    "public-manifest-not-promoted": [
        "public-manifest-not-promoted",
        "deploy/ecorex-site/manifest.json",
    ],
    "release-defaults-not-promoted": [
        "release-defaults-not-promoted",
        "installer/check/package scripts still default to `0.2.1`",
    ],
    "public-release-package-not-built": [
        "public-release-package-not-built",
        EXPECTED_PUBLIC_RELEASE_ZIP,
    ],
    "target-environment-deploy-rollback-not-exercised": [
        "target-environment-deploy-rollback-not-exercised",
        "No production deploy/rollback target was exercised",
    ],
    "online-web-browser-smoke-not-pass": [
        "online-web-browser-smoke-not-pass",
        "Online browser smoke did not pass",
    ],
    "final-release-review-not-pass": ["final-release-review-not-pass"],
}


class ReleaseGateError(RuntimeError):
    """Raised when the release gate evidence is malformed."""


def _load_matrix_checker() -> Any:
    checker_path = ROOT / "scripts" / "check-v022-harness-matrix.py"
    spec = importlib.util.spec_from_file_location("check_v022_harness_matrix", checker_path)
    if spec is None or spec.loader is None:
        raise ReleaseGateError(f"cannot load matrix checker: {checker_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_text(path: Path) -> str:
    if not path.exists():
        raise ReleaseGateError(f"required file missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReleaseGateError(f"{path.relative_to(ROOT)} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReleaseGateError(f"{path.relative_to(ROOT)} root must be a JSON object")
    return payload


def _ensure_no_secret_markers(text: str, label: str) -> None:
    if SECRET_TOKEN_PATTERN.search(text):
        raise ReleaseGateError(f"{label} contains an API-key shaped token")


def _ensure_no_raw_feishu_ids(text: str, label: str) -> None:
    if RAW_FEISHU_ID_PATTERN.search(text):
        raise ReleaseGateError(f"{label} contains a raw Feishu identifier")


def _parse_acceptance_table(text: str) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw.startswith("| R22-"):
            continue
        cells = [cell.strip() for cell in raw.strip("|").split("|")]
        if len(cells) < 5:
            continue
        item_id, area, acceptance, status, evidence = cells[:5]
        rows[item_id] = {
            "area": area,
            "acceptance": acceptance,
            "status": status,
            "evidence": evidence,
        }
    return rows


def _pending_section(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return []
    result: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.strip().startswith("- "):
            result.append(line.strip()[2:].strip())
    return result


def _status_is_incomplete(status: str) -> bool:
    value = str(status or "").upper()
    return any(marker in value for marker in ("PENDING", "TODO", "NEEDS-SLICE", "LOCAL-PASS"))


def _add_check(checks: list[dict[str, Any]], check_id: str, status: str, detail: str) -> None:
    checks.append({"id": check_id, "status": status, "detail": detail})


def _add_blocker(blockers: list[dict[str, str]], blocker_id: str, surface: str, reason: str) -> None:
    if any(item["id"] == blocker_id for item in blockers):
        return
    blockers.append({"id": blocker_id, "surface": surface, "reason": reason})


def _validate_release_gate_doc(text: str, state: str, blocker_ids: set[str]) -> str:
    _ensure_no_secret_markers(text, "release-gate.md")
    base_markers = [
        "# v0.2.2 Release Gate",
        "## Status",
        "Current status:",
    ]
    missing = [marker for marker in base_markers if marker not in text]
    if missing:
        raise ReleaseGateError(f"release-gate.md missing markers: {', '.join(missing)}")

    if state == "pass":
        required_markers = [
            "Current status: `PASS`",
            "## Release Evidence",
            "No active release blockers",
        ]
        stale_markers = [
            "RELEASE-CANDIDATE-EVIDENCE-BLOCKED",
            "public-manifest-not-promoted",
            "release-defaults-not-promoted",
            "target-environment-deploy-rollback-not-exercised",
        ]
        missing = [marker for marker in required_markers if marker not in text]
        stale = [marker for marker in stale_markers if marker in text]
        if missing:
            raise ReleaseGateError(f"release-gate.md missing PASS markers: {', '.join(missing)}")
        if stale:
            raise ReleaseGateError(f"release-gate.md contains stale blocked markers: {', '.join(stale)}")
        return "release-gate.md documents PASS state"

    required_markers = [
        "Current status: `BLOCKED`",
        "## Blockers",
        "BLOCKED",
    ]
    required_markers.extend(sorted(blocker_ids))
    missing = [marker for marker in required_markers if marker not in text]
    if missing:
        raise ReleaseGateError(f"release-gate.md missing blocked markers: {', '.join(missing)}")
    return "release-gate.md contains blocker table"


def _validate_release_manifest(text: str, state: str, blocker_ids: set[str]) -> str:
    _ensure_no_secret_markers(text, "release-manifest.md")
    required_markers = [
        "# EcoreX v0.2.2 Release Manifest",
        "`web-linux-service`",
        EXPECTED_WEB_SERVICE_TARBALL,
        EXPECTED_WEB_SERVICE_SHA256,
        "docs/v0.2.2/artifacts/release-deploy-rollback-smoke.json",
        "docs/v0.2.2/artifacts/feishu-im-real-credential-smoke.json",
        "Real Feishu/IM read-only credential smoke now passes",
        "exact ordered target smoke command chain",
        "target.*Hash",
    ]
    if state == "pass":
        required_markers.extend(
            [
                "RELEASE-PASS",
                "Promoted Release Evidence",
                "Public manifest promoted",
                "Release defaults promoted",
                "Target-environment deploy/rollback smoke passed",
            ]
        )
        stale_markers = [
            "RELEASE-CANDIDATE-EVIDENCE-BLOCKED",
            "public-manifest-not-promoted",
            "release-defaults-not-promoted",
            "target-environment-deploy-rollback-not-exercised",
            "Known Non-Final Gates",
        ]
        stale = [marker for marker in stale_markers if marker in text]
        if stale:
            raise ReleaseGateError(f"release-manifest.md contains stale blocked markers: {', '.join(stale)}")
    else:
        required_markers.extend(
            [
                "RELEASE-CANDIDATE-EVIDENCE-BLOCKED",
                "Known Non-Final Gates",
            ]
        )
        for blocker_id in sorted(blocker_ids):
            required_markers.extend(BLOCKED_MANIFEST_MARKERS_BY_BLOCKER.get(blocker_id, [blocker_id]))
    missing = [marker for marker in required_markers if marker not in text]
    if missing:
        raise ReleaseGateError(f"release-manifest.md missing markers: {', '.join(missing)}")
    return f"manifest references {EXPECTED_WEB_SERVICE_TARBALL} and {state} release evidence"


def _validate_public_manifest_promotion() -> tuple[bool, str]:
    if not PUBLIC_SITE_MANIFEST.exists():
        return False, f"{PUBLIC_SITE_MANIFEST.relative_to(ROOT)} missing"

    payload = _load_json(PUBLIC_SITE_MANIFEST)
    serialized = json.dumps(payload, sort_keys=True)
    _ensure_no_secret_markers(serialized, "deploy/ecorex-site/manifest.json")

    failures: list[str] = []
    if payload.get("product") != "EcoreX":
        failures.append(f"product={payload.get('product')!r}")
    if payload.get("version") != EXPECTED_RELEASE_VERSION:
        failures.append(f"version={payload.get('version')!r}")

    artifacts = payload.get("artifacts")
    artifacts_by_id: dict[str, dict[str, Any]] = {}
    if isinstance(artifacts, list):
        artifacts_by_id = {
            str(item.get("id")): item
            for item in artifacts
            if isinstance(item, dict) and item.get("id")
        }

    for artifact_id, expected_artifact in EXPECTED_PUBLIC_ARTIFACTS.items():
        artifact = artifacts_by_id.get(artifact_id)
        if not isinstance(artifact, dict):
            failures.append(f"{artifact_id} artifact missing")
            continue
        for key, expected in expected_artifact.items():
            if artifact.get(key) != expected:
                failures.append(f"{artifact_id}.{key}={artifact.get(key)!r}")
        if artifact.get("status") != "ready":
            failures.append(f"{artifact_id}.status={artifact.get('status')!r}")
        if artifact.get("href") == expected_artifact["href"]:
            failures.extend(_validate_public_manifest_download(artifact_id, expected_artifact))

    if failures:
        return False, "; ".join(failures)
    return True, (
        f"public manifest promotes {len(EXPECTED_PUBLIC_ARTIFACTS)} Web artifacts to "
        f"{EXPECTED_RELEASE_VERSION} with matching download size/hash"
    )


def _validate_public_manifest_download(artifact_id: str, expected_artifact: dict[str, Any]) -> list[str]:
    href = expected_artifact["href"]
    if not isinstance(href, str) or href.startswith("/") or "\\" in href:
        return [f"{artifact_id}.href={href!r}"]
    parts = href.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return [f"{artifact_id}.href={href!r}"]

    path = PUBLIC_SITE_ROOT.joinpath(*parts)
    if not path.is_file():
        return [f"{artifact_id}.download missing at {_display_path(path)}"]

    failures: list[str] = []
    size = path.stat().st_size
    if size != expected_artifact["size"]:
        failures.append(f"{artifact_id}.download.size={size}")
    sha256 = _sha256_file(path)
    if sha256 != expected_artifact["sha256"]:
        failures.append(f"{artifact_id}.download.sha256={sha256}")
    return failures


def _validate_release_default_versions() -> tuple[bool, str]:
    failures: list[str] = []
    checked = 0
    for relative_path, pattern in RELEASE_DEFAULT_VERSION_FILES:
        path = ROOT / relative_path
        if not path.exists():
            failures.append(f"{relative_path}=missing")
            continue
        text = path.read_text(encoding="utf-8")
        _ensure_no_secret_markers(text, relative_path)
        match = re.search(pattern, text)
        if not match:
            failures.append(f"{relative_path}=default-marker-missing")
            continue
        checked += 1
        observed = match.group(1)
        if observed != EXPECTED_RELEASE_VERSION:
            failures.append(f"{relative_path} default={observed!r}")

    if failures:
        return False, "; ".join(failures)
    return True, f"{checked} release default version markers point to {EXPECTED_RELEASE_VERSION}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _is_hash_shape(value: Any) -> bool:
    return isinstance(value, str) and HASH_SHAPE_PATTERN.fullmatch(value) is not None


def _validate_public_release_package() -> tuple[bool, str]:
    package_path = ROOT / EXPECTED_PUBLIC_RELEASE_ZIP
    if not package_path.exists():
        return False, f"{EXPECTED_PUBLIC_RELEASE_ZIP} missing"
    size = package_path.stat().st_size
    sha256 = _sha256_file(package_path)
    failures: list[str] = []
    if size != EXPECTED_PUBLIC_RELEASE_ZIP_SIZE:
        failures.append(f"size={size}")
    if sha256 != EXPECTED_PUBLIC_RELEASE_ZIP_SHA256:
        failures.append(f"sha256={sha256}")
    if failures:
        return False, "; ".join(failures)
    return True, f"{EXPECTED_PUBLIC_RELEASE_ZIP} size={size} sha256={sha256}"


def _expect(payload: dict[str, Any], key: str, expected: Any) -> None:
    if payload.get(key) != expected:
        raise ReleaseGateError(f"deploy smoke {key} expected {expected!r}, got {payload.get(key)!r}")


def _validate_deploy_smoke_artifact(payload: dict[str, Any]) -> str:
    _ensure_no_secret_markers(json.dumps(payload, sort_keys=True), "release-deploy-rollback-smoke.json")
    _expect(payload, "status", "PASS")
    scope = payload.get("scope")
    if scope == "local-filesystem-web-linux-service":
        for key, expected in {
            "productionEnvironment": False,
            "requiresRoot": False,
            "requiresSystemd": False,
            "requiresNetwork": False,
        }.items():
            _expect(payload, key, expected)
    elif scope == "target-environment-web-linux-service":
        _expect(payload, "productionEnvironment", True)
    else:
        raise ReleaseGateError(
            "deploy smoke scope must be local-filesystem-web-linux-service or target-environment-web-linux-service"
        )

    artifact = payload.get("artifact")
    if not isinstance(artifact, dict):
        raise ReleaseGateError("deploy smoke artifact must contain artifact object")
    for key, expected in {
        "path": EXPECTED_WEB_SERVICE_TARBALL,
        "version": EXPECTED_RELEASE_VERSION,
        "artifactId": "web-linux-service",
        "sha256": EXPECTED_WEB_SERVICE_SHA256,
    }.items():
        if artifact.get(key) != expected:
            raise ReleaseGateError(f"deploy smoke artifact.{key} expected {expected!r}, got {artifact.get(key)!r}")
    if not isinstance(artifact.get("size"), int) or artifact["size"] <= 0:
        raise ReleaseGateError("deploy smoke artifact.size must be a positive integer")

    sha_file = artifact.get("sha256File")
    if not isinstance(sha_file, dict) or sha_file.get("matches") is not True:
        raise ReleaseGateError("deploy smoke artifact.sha256File must confirm sidecar hash")
    release_json = artifact.get("releaseJson")
    if not isinstance(release_json, dict):
        raise ReleaseGateError("deploy smoke artifact.releaseJson must be an object")
    if release_json.get("version") != EXPECTED_RELEASE_VERSION:
        raise ReleaseGateError("deploy smoke releaseJson version mismatch")
    if release_json.get("artifactId") != "web-linux-service":
        raise ReleaseGateError("deploy smoke releaseJson artifactId mismatch")

    package_checks = payload.get("packageChecks")
    if not isinstance(package_checks, dict):
        raise ReleaseGateError("deploy smoke packageChecks must be an object")
    for key in ("checksumsJsonFilesVerified", "sha256SumsFilesVerified"):
        if not isinstance(package_checks.get(key), int) or package_checks[key] <= 0:
            raise ReleaseGateError(f"deploy smoke packageChecks.{key} must be positive")
    if package_checks.get("releaseJsonCovered") is not True:
        raise ReleaseGateError("deploy smoke packageChecks.releaseJsonCovered must be true")
    required_paths = set(package_checks.get("requiredPaths") or [])
    for required in ("runtime/app.py", "release.json", "checksums.json", "SHA256SUMS.txt"):
        if required not in required_paths:
            raise ReleaseGateError(f"deploy smoke packageChecks.requiredPaths missing {required}")

    deploy = payload.get("deploy")
    rollback = payload.get("rollback")
    if not isinstance(deploy, dict) or deploy.get("verified") is not True:
        raise ReleaseGateError("deploy smoke deploy.verified must be true")
    if deploy.get("currentVersionAfterDeploy") != EXPECTED_RELEASE_VERSION:
        raise ReleaseGateError("deploy smoke deploy current version mismatch")
    if not isinstance(rollback, dict) or rollback.get("verified") is not True:
        raise ReleaseGateError("deploy smoke rollback.verified must be true")
    if rollback.get("currentVersionAfterRollback") != "0.2.1":
        raise ReleaseGateError("deploy smoke rollback current version mismatch")
    if rollback.get("candidateRetainedForAudit") is not True:
        raise ReleaseGateError("deploy smoke rollback must retain candidate for audit")
    allowed_pointer_methods = {"symlink", "manifest-pointer-fallback"}
    if scope == "target-environment-web-linux-service":
        allowed_pointer_methods.add("target-current-symlink")
    if payload.get("pointerMethod") not in allowed_pointer_methods:
        raise ReleaseGateError(f"deploy smoke pointerMethod must be one of {sorted(allowed_pointer_methods)}")
    return (
        f"{artifact['path']} scope={scope} size={artifact['size']} "
        f"checksums={package_checks['checksumsJsonFilesVerified']} pointer={payload.get('pointerMethod')}"
    )


def _validate_target_deploy_smoke_artifact(payload: dict[str, Any]) -> tuple[bool, str]:
    serialized = json.dumps(payload, sort_keys=True)
    _ensure_no_secret_markers(serialized, "release-target-deploy-rollback-smoke.json")
    _ensure_no_raw_feishu_ids(serialized, "release-target-deploy-rollback-smoke.json")

    redaction = payload.get("redaction")
    if not isinstance(redaction, dict):
        raise ReleaseGateError("target deploy smoke redaction must be an object")
    for key in (
        "rawTargetPersisted",
        "rawCommandsPersisted",
        "rawStdoutPersisted",
        "rawStderrPersisted",
        "rawSecretsPersisted",
    ):
        if redaction.get(key) is not False:
            raise ReleaseGateError(f"target deploy smoke redaction.{key} must be false")

    target = payload.get("target")
    if not isinstance(target, dict) or target.get("rawTargetPersisted") is not False:
        raise ReleaseGateError("target deploy smoke target.rawTargetPersisted must be false")

    if payload.get("status") == "BLOCKED":
        reason = str(payload.get("reason") or "target deploy/rollback smoke has not passed")
        if reason not in ALLOWED_TARGET_BLOCKED_REASONS:
            raise ReleaseGateError(f"target deploy smoke blocked reason is not allowlisted: {reason!r}")
        reason_hash = payload.get("reasonHash")
        if reason_hash is not None and (
            not isinstance(reason_hash, str) or not re.fullmatch(r"[A-F0-9]{16}|[A-F0-9]{32}|[A-F0-9]{64}", reason_hash)
        ):
            raise ReleaseGateError("target deploy smoke reasonHash must be uppercase hex hash when present")
        return False, f"target deploy/rollback smoke blocked: {reason}"

    detail = _validate_deploy_smoke_artifact(payload)
    if payload.get("scope") != "target-environment-web-linux-service":
        raise ReleaseGateError("target deploy smoke scope must be target-environment-web-linux-service")
    if payload.get("productionEnvironment") is not True:
        raise ReleaseGateError("target deploy smoke productionEnvironment must be true")
    if payload.get("requiresNetwork") is not True:
        raise ReleaseGateError("target deploy smoke requiresNetwork must be true")
    if payload.get("requiresSystemd") is not True:
        raise ReleaseGateError("target deploy smoke requiresSystemd must be true")
    if payload.get("requiresRoot") is not True:
        raise ReleaseGateError("target deploy smoke requiresRoot must be true")

    pre_state = payload.get("preState")
    if not isinstance(pre_state, dict):
        raise ReleaseGateError("target deploy smoke preState must be an object")
    if pre_state.get("currentVersion") != "0.2.1":
        raise ReleaseGateError("target deploy smoke preState.currentVersion must be 0.2.1")
    if pre_state.get("serviceActive") is not True:
        raise ReleaseGateError("target deploy smoke preState.serviceActive must be true")
    if pre_state.get("serviceEnabled") is not True:
        raise ReleaseGateError("target deploy smoke preState.serviceEnabled must be true")

    for key in ("sshHostHash", "serviceNameHash", "installRootHash", "workspaceRootHash"):
        value = target.get(key)
        if not _is_hash_shape(value):
            raise ReleaseGateError(f"target deploy smoke target.{key} must be an uppercase hex hash")
    for key, value in target.items():
        if key.endswith("Hash") and value not in ("", None) and not _is_hash_shape(value):
            raise ReleaseGateError(f"target deploy smoke target.{key} must be an uppercase hex hash")

    commands = payload.get("commands")
    if not isinstance(commands, list):
        raise ReleaseGateError("target deploy smoke commands must record upload/install/check/rollback command hashes")
    command_names = [command.get("name") if isinstance(command, dict) else None for command in commands]
    if command_names not in (
        TARGET_DEPLOY_COMMAND_SEQUENCE,
        TARGET_DEPLOY_COMMAND_SEQUENCE_WITH_REBUILT_ROLLBACK_BASELINE,
    ):
        raise ReleaseGateError("target deploy smoke commands must match expected ordered command chain")
    for index, command in enumerate(commands):
        if not isinstance(command, dict):
            raise ReleaseGateError("target deploy smoke command rows must be objects")
        for forbidden in ("argv", "stdout", "stderr", "command"):
            if forbidden in command:
                raise ReleaseGateError(f"target deploy smoke command row {index} persists raw {forbidden}")
        for required in ("name", "argvHash", "exitCode", "stdoutHash", "stderrHash"):
            if required not in command:
                raise ReleaseGateError(f"target deploy smoke command row {index} missing {required}")
        for key in ("argvHash", "stdoutHash", "stderrHash"):
            if not _is_hash_shape(command.get(key)):
                raise ReleaseGateError(f"target deploy smoke command row {index} {key} must be an uppercase hex hash")
        if command.get("exitCode") != 0:
            raise ReleaseGateError(f"target deploy smoke command {command.get('name')} exited {command.get('exitCode')}")

    deploy = payload.get("deploy")
    rollback = payload.get("rollback")
    if not isinstance(deploy, dict) or deploy.get("targetCheckCommandPassed") is not True:
        raise ReleaseGateError("target deploy smoke deploy.targetCheckCommandPassed must be true")
    if deploy.get("serviceActiveAfterDeploy") is not True:
        raise ReleaseGateError("target deploy smoke deploy.serviceActiveAfterDeploy must be true")
    if deploy.get("serviceEnabledAfterDeploy") is not True:
        raise ReleaseGateError("target deploy smoke deploy.serviceEnabledAfterDeploy must be true")
    if not isinstance(rollback, dict) or rollback.get("candidateRetainedForAudit") is not True:
        raise ReleaseGateError("target deploy smoke rollback.candidateRetainedForAudit must be true")
    if rollback.get("serviceActiveAfterRollback") is not True:
        raise ReleaseGateError("target deploy smoke rollback.serviceActiveAfterRollback must be true")
    if rollback.get("serviceEnabledAfterRollback") is not True:
        raise ReleaseGateError("target deploy smoke rollback.serviceEnabledAfterRollback must be true")
    if payload.get("pointerMethod") != "target-current-symlink":
        raise ReleaseGateError("target deploy smoke pointerMethod must be target-current-symlink")

    return True, detail


def _validate_target_command_template_artifact(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True)
    _ensure_no_secret_markers(serialized, "release-target-command-template.json")
    _ensure_no_raw_feishu_ids(serialized, "release-target-command-template.json")

    if payload.get("status") not in {"READY_FOR_TARGET_INPUT", "LOCAL_ARTIFACTS_INCOMPLETE"}:
        raise ReleaseGateError("target command template status must be READY_FOR_TARGET_INPUT or LOCAL_ARTIFACTS_INCOMPLETE")
    if payload.get("scope") != "target-environment-web-linux-service":
        raise ReleaseGateError("target command template scope must be target-environment-web-linux-service")
    if payload.get("targetExecution") is not False:
        raise ReleaseGateError("target command template targetExecution must be false")
    if payload.get("networkUsed") is not False:
        raise ReleaseGateError("target command template networkUsed must be false")

    redaction = payload.get("redaction")
    if not isinstance(redaction, dict):
        raise ReleaseGateError("target command template redaction must be an object")
    for key in (
        "rawTargetPersisted",
        "rawCommandsPersisted",
        "rawStdoutPersisted",
        "rawStderrPersisted",
        "rawSecretsPersisted",
    ):
        if redaction.get(key) is not False:
            raise ReleaseGateError(f"target command template redaction.{key} must be false")

    artifact = payload.get("artifact")
    if not isinstance(artifact, dict):
        raise ReleaseGateError("target command template artifact must be an object")
    if artifact.get("artifactId") != "web-linux-service":
        raise ReleaseGateError("target command template artifactId must be web-linux-service")
    if artifact.get("version") != EXPECTED_RELEASE_VERSION:
        raise ReleaseGateError("target command template artifact version mismatch")
    if artifact.get("path") != EXPECTED_WEB_SERVICE_TARBALL:
        raise ReleaseGateError("target command template artifact path mismatch")
    if artifact.get("expectedSha256") != EXPECTED_WEB_SERVICE_SHA256:
        raise ReleaseGateError("target command template expectedSha256 mismatch")
    if payload.get("status") == "READY_FOR_TARGET_INPUT" and artifact.get("sha256MatchesExpected") is not True:
        raise ReleaseGateError("target command template READY_FOR_TARGET_INPUT requires sha256MatchesExpected=true")

    template_artifact = payload.get("templateArtifact")
    if not isinstance(template_artifact, dict):
        raise ReleaseGateError("target command template templateArtifact must be an object")
    if template_artifact.get("writesPassEvidence") is not False:
        raise ReleaseGateError("target command template must not write PASS evidence")
    if template_artifact.get("clearsTargetBlocker") is not False:
        raise ReleaseGateError("target command template must not clear target blocker")

    required_target_inputs = payload.get("requiredTargetInputs")
    if not isinstance(required_target_inputs, list):
        raise ReleaseGateError("target command template requiredTargetInputs must be a list")
    for marker in ("--ssh-host <target-host>", "--confirm-target-environment"):
        if marker not in required_target_inputs:
            raise ReleaseGateError(f"target command template requiredTargetInputs missing {marker}")

    command_template = str(payload.get("commandTemplate") or "")
    for marker in (
        "--ssh-host <target-host>",
        "--ssh-user <target-user>",
        "--ssh-identity <path-to-private-key>",
        "--public-base-url <https://target.example>",
        "--confirm-target-environment",
        "docs/v0.2.2/artifacts/release-target-deploy-rollback-smoke.json",
    ):
        if marker not in command_template:
            raise ReleaseGateError(f"target command template commandTemplate missing {marker}")

    local_inputs = payload.get("localInputs")
    if not isinstance(local_inputs, list) or len(local_inputs) < 3:
        raise ReleaseGateError("target command template localInputs must include package, installer, and checker")
    rows_by_name: dict[str, dict[str, Any]] = {}
    for row in local_inputs:
        if not isinstance(row, dict):
            raise ReleaseGateError("target command template localInputs rows must be objects")
        name = str(row.get("name") or "")
        if name:
            rows_by_name[name] = row
    for name, expected_path in TARGET_TEMPLATE_LOCAL_INPUT_PATHS.items():
        if name not in rows_by_name:
            raise ReleaseGateError(f"target command template localInputs missing {name}")
        _validate_target_template_local_input(name, rows_by_name[name], expected_path)

    return f"{payload.get('status')} target handoff template does not execute target or clear blocker"


def _validate_target_template_local_input(name: str, row: dict[str, Any], expected_path: str) -> None:
    if row.get("exists") is not True:
        raise ReleaseGateError(f"target command template localInputs.{name}.exists must be true")
    if row.get("path") != expected_path:
        raise ReleaseGateError(f"target command template localInputs.{name}.path mismatch")

    file_path = ROOT / expected_path
    if not file_path.is_file():
        raise ReleaseGateError(f"target command template localInputs.{name}.path does not exist")
    expected_size = EXPECTED_WEB_SERVICE_SIZE if name == "package" else file_path.stat().st_size
    expected_sha256 = EXPECTED_WEB_SERVICE_SHA256 if name == "package" else _sha256_file(file_path)

    size = row.get("size")
    if not isinstance(size, int) or size <= 0:
        raise ReleaseGateError(f"target command template localInputs.{name}.size must be a positive integer")
    if size != expected_size:
        raise ReleaseGateError(f"target command template localInputs.{name}.size mismatch")

    sha256 = row.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[A-F0-9]{64}", sha256):
        raise ReleaseGateError(f"target command template localInputs.{name}.sha256 must be uppercase hex SHA256")
    if sha256 != expected_sha256:
        raise ReleaseGateError(f"target command template localInputs.{name}.sha256 mismatch")


def _validate_feishu_im_smoke_artifact(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True)
    _ensure_no_secret_markers(serialized, "feishu-im-real-credential-smoke.json")
    _ensure_no_raw_feishu_ids(serialized, "feishu-im-real-credential-smoke.json")
    for key, expected in {
        "status": "PASS",
        "scope": "real-feishu-im-readonly",
        "requiresNetwork": True,
        "writesMessages": False,
        "writesFiles": False,
        "rawIdentifiersPersisted": False,
    }.items():
        if payload.get(key) != expected:
            raise ReleaseGateError(f"Feishu smoke {key} expected {expected!r}, got {payload.get(key)!r}")

    auth = payload.get("auth")
    if not isinstance(auth, dict):
        raise ReleaseGateError("Feishu smoke auth must be an object")
    user = auth.get("user")
    bot = auth.get("bot")
    scope_checks = auth.get("scopeChecks")
    if not isinstance(user, dict) or user.get("available") is not True:
        raise ReleaseGateError("Feishu smoke user identity must be available")
    if user.get("tokenStatus") != "valid":
        raise ReleaseGateError("Feishu smoke user token must be valid after verify")
    if not isinstance(bot, dict) or bot.get("available") is not True:
        raise ReleaseGateError("Feishu smoke bot identity must be available")
    if not isinstance(scope_checks, dict):
        raise ReleaseGateError("Feishu smoke scopeChecks must be an object")
    for scope in ("im:chat:read", "im:message"):
        if scope_checks.get(scope) is not True:
            raise ReleaseGateError(f"Feishu smoke missing required scope check: {scope}")

    im = payload.get("im")
    if not isinstance(im, dict):
        raise ReleaseGateError("Feishu smoke im must be an object")
    if im.get("command") != EXPECTED_FEISHU_IM_COMMAND:
        raise ReleaseGateError("Feishu smoke must use the expected read-only chat-list command")
    if im.get("identity") != "user" or im.get("readOnly") is not True:
        raise ReleaseGateError("Feishu smoke must use user identity and read-only IM command")
    item_count = im.get("itemCount")
    if not isinstance(item_count, int) or item_count < 0:
        raise ReleaseGateError("Feishu smoke im.itemCount must be a non-negative integer")
    if item_count > 0 and not im.get("firstChatIdHash"):
        raise ReleaseGateError("Feishu smoke must hash the first chat id when a chat is returned")
    redaction = payload.get("redaction")
    if not isinstance(redaction, dict) or redaction.get("rawChatPayload") != "not_persisted":
        raise ReleaseGateError("Feishu smoke redaction contract is missing")
    return f"real read-only Feishu IM smoke passed with itemCount={item_count}"


def _validate_production_deploy_online_artifact(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True)
    _ensure_no_secret_markers(serialized, "production-deploy-online.json")
    _ensure_no_raw_feishu_ids(serialized, "production-deploy-online.json")
    for key, expected in {
        "status": "PASS",
        "scope": "production-online-web-and-admin",
        "version": EXPECTED_RELEASE_VERSION,
        "productionEnvironment": True,
    }.items():
        if payload.get(key) != expected:
            raise ReleaseGateError(f"production deploy {key} expected {expected!r}, got {payload.get(key)!r}")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ReleaseGateError("production deploy artifacts must be an object")
    web_tarball = artifacts.get("webTarball")
    public_zip = artifacts.get("publicZip")
    if not isinstance(web_tarball, dict) or not isinstance(public_zip, dict):
        raise ReleaseGateError("production deploy artifacts must include webTarball and publicZip")
    for key, expected in {
        "size": EXPECTED_WEB_SERVICE_SIZE,
        "sha256": EXPECTED_WEB_SERVICE_SHA256,
    }.items():
        if web_tarball.get(key) != expected:
            raise ReleaseGateError(f"production deploy webTarball.{key} expected {expected!r}, got {web_tarball.get(key)!r}")
    if not str(web_tarball.get("path") or "").endswith(EXPECTED_WEB_SERVICE_FILENAME):
        raise ReleaseGateError("production deploy webTarball.path must point to the expected tarball")
    for key, expected in {
        "size": EXPECTED_PUBLIC_RELEASE_ZIP_SIZE,
        "sha256": EXPECTED_PUBLIC_RELEASE_ZIP_SHA256,
    }.items():
        if public_zip.get(key) != expected:
            raise ReleaseGateError(f"production deploy publicZip.{key} expected {expected!r}, got {public_zip.get(key)!r}")
    if not str(public_zip.get("path") or "").endswith(f"EcoreX_{EXPECTED_RELEASE_VERSION}-public-release.zip"):
        raise ReleaseGateError("production deploy publicZip.path must point to the expected public zip")

    target = payload.get("target")
    if not isinstance(target, dict) or target.get("rawTargetPersisted") is not False:
        raise ReleaseGateError("production deploy target.rawTargetPersisted must be false")
    for key in ("sshHostHash", "sshUserHash", "domainHash"):
        if not _is_hash_shape(target.get(key)):
            raise ReleaseGateError(f"production deploy target.{key} must be an uppercase hex hash")

    post_state = payload.get("postState")
    if not isinstance(post_state, dict):
        raise ReleaseGateError("production deploy postState must be an object")
    for key, expected in {
        "currentVersion": EXPECTED_RELEASE_VERSION,
        "adminPublicManifestVersion": EXPECTED_RELEASE_VERSION,
        "releaseArtifactId": "web-linux-service",
        "serviceActive": True,
        "serviceEnabled": True,
        "webLocalStatus": 200,
    }.items():
        if post_state.get(key) != expected:
            raise ReleaseGateError(f"production deploy postState.{key} expected {expected!r}, got {post_state.get(key)!r}")

    target_smoke = payload.get("targetSmoke")
    if not isinstance(target_smoke, dict):
        raise ReleaseGateError("production deploy targetSmoke must be an object")
    if target_smoke.get("deployVerified") is not True or target_smoke.get("rollbackVerified") is not True:
        raise ReleaseGateError("production deploy targetSmoke must confirm deploy and rollback")

    online_checks = payload.get("onlineChecks")
    if not isinstance(online_checks, dict):
        raise ReleaseGateError("production deploy onlineChecks must be an object")
    for key, expected in {
        "webLocalStatus": 200,
        "webVersion": EXPECTED_RELEASE_VERSION,
        "serviceActive": True,
        "serviceEnabled": True,
        "publicManifestVersion": EXPECTED_RELEASE_VERSION,
        "publicCheckExecuted": True,
    }.items():
        if online_checks.get(key) != expected:
            raise ReleaseGateError(f"production deploy onlineChecks.{key} expected {expected!r}, got {online_checks.get(key)!r}")

    redaction = payload.get("redaction")
    if not isinstance(redaction, dict):
        raise ReleaseGateError("production deploy redaction must be an object")
    for key in ("rawTargetPersisted", "rawPasswordPersisted", "rawSecretPersisted", "rawUrlPersisted", "rawOutputPersisted"):
        if redaction.get(key) is not False:
            raise ReleaseGateError(f"production deploy redaction.{key} must be false")

    commands = payload.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ReleaseGateError("production deploy commands must be a non-empty list")
    command_names = {command.get("name") for command in commands if isinstance(command, dict)}
    for required_name in ("final_check_v022_web", "install_public_site_and_admin", "check_public_site_and_admin", "capture_final_online_state"):
        if required_name not in command_names:
            raise ReleaseGateError(f"production deploy commands missing {required_name}")
    for index, command in enumerate(commands):
        if not isinstance(command, dict):
            raise ReleaseGateError("production deploy command rows must be objects")
        for forbidden in ("argv", "stdout", "stderr", "command"):
            if forbidden in command:
                raise ReleaseGateError(f"production deploy command row {index} persists raw {forbidden}")
        if command.get("exitCode") != 0:
            raise ReleaseGateError(f"production deploy command {command.get('name')} exited {command.get('exitCode')}")
        for key in ("argvHash", "stdoutHash", "stderrHash"):
            if not _is_hash_shape(command.get(key)):
                raise ReleaseGateError(f"production deploy command row {index} {key} must be an uppercase hex hash")

    return f"production Web/admin deploy passed with web tar {EXPECTED_WEB_SERVICE_SHA256}"


def _validate_online_web_browser_smoke_artifact(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True)
    _ensure_no_secret_markers(serialized, "online-web-browser-smoke.json")
    _ensure_no_raw_feishu_ids(serialized, "online-web-browser-smoke.json")
    if payload.get("status") != "PASS":
        raise ReleaseGateError(f"online browser smoke status expected 'PASS', got {payload.get('status')!r}")

    target = payload.get("target")
    if not isinstance(target, dict) or target.get("rawTargetPersisted") is not False:
        raise ReleaseGateError("online browser smoke target.rawTargetPersisted must be false")
    for key in ("domainHash", "appUrlHash"):
        if not _is_hash_shape(target.get(key)):
            raise ReleaseGateError(f"online browser smoke target.{key} must be an uppercase hex hash")

    login = payload.get("login")
    if not isinstance(login, dict):
        raise ReleaseGateError("online browser smoke login must be an object")
    if login.get("status") != 200 or login.get("sessionEmailVisible") is not True:
        raise ReleaseGateError("online browser smoke must prove explicit login identity")
    if login.get("sessionProvider") != "web-password":
        raise ReleaseGateError("online browser smoke sessionProvider must be web-password")

    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        raise ReleaseGateError("online browser smoke manifest must be an object")
    if manifest.get("status") != 200 or manifest.get("version") != EXPECTED_RELEASE_VERSION:
        raise ReleaseGateError("online browser smoke manifest must expose v0.2.2")
    artifact_versions = manifest.get("artifactVersions")
    if not isinstance(artifact_versions, list) or EXPECTED_RELEASE_VERSION not in artifact_versions:
        raise ReleaseGateError("online browser smoke manifest artifact versions must include v0.2.2")

    admin_health = payload.get("adminHealth")
    if not isinstance(admin_health, dict):
        raise ReleaseGateError("online browser smoke adminHealth must be an object")
    if admin_health.get("status") != 200 or admin_health.get("version") != EXPECTED_RELEASE_VERSION:
        raise ReleaseGateError("online browser smoke admin health must expose v0.2.2")

    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        raise ReleaseGateError("online browser smoke metrics must be an object")
    for key in (
        "emailVisible",
        "versionVisible",
        "newSessionHeadline",
        "oldHeadlineHidden",
        "projectEntry",
        "generalEntry",
        "runCenterHidden",
        "localFallbackHidden",
        "bodyHasSystemStack",
        "codeHasMonoStack",
        "runTimingVisible",
    ):
        if metrics.get(key) is not True:
            raise ReleaseGateError(f"online browser smoke metrics.{key} must be true")

    project_start_menu = metrics.get("projectStartMenu")
    if not isinstance(project_start_menu, dict):
        raise ReleaseGateError("online browser smoke metrics.projectStartMenu must be an object")
    for key in ("visible", "hasImport", "hasNoProject", "hasSearch", "closedOnBlank"):
        if project_start_menu.get(key) is not True:
            raise ReleaseGateError(f"online browser smoke metrics.projectStartMenu.{key} must be true")

    run_timing = metrics.get("runTiming")
    if not isinstance(run_timing, dict):
        raise ReleaseGateError("online browser smoke metrics.runTiming must be an object")
    if run_timing.get("visible") is not True or run_timing.get("finalLabelVisible") is not True:
        raise ReleaseGateError("online browser smoke must prove final run timing is visible")
    if not (run_timing.get("inProcessSummary") is True or run_timing.get("fallbackVisible") is True):
        raise ReleaseGateError("online browser smoke must prove run timing is attached to the message UI")

    narrow = metrics.get("narrowViewport")
    if not isinstance(narrow, dict) or narrow.get("noHorizontalOverflow") is not True:
        raise ReleaseGateError("online browser smoke must prove narrow viewport has no horizontal overflow")

    if payload.get("consoleErrors") not in ([], None):
        raise ReleaseGateError("online browser smoke consoleErrors must be empty")
    if payload.get("assertionErrors") not in ([], None):
        raise ReleaseGateError("online browser smoke assertionErrors must be empty")
    redaction = payload.get("redaction")
    if not isinstance(redaction, dict):
        raise ReleaseGateError("online browser smoke redaction must be an object")
    for key in ("rawTargetPersisted", "rawPasswordPersisted", "rawSecretsPersisted"):
        if redaction.get(key) is not False:
            raise ReleaseGateError(f"online browser smoke redaction.{key} must be false")

    return "online Web browser smoke passed with identity, version, admin, and UI checks"


def _parse_artifact_time(payload: dict[str, Any], artifact_name: str) -> datetime:
    value = payload.get("generatedAt")
    if not isinstance(value, str) or not value.strip():
        raise ReleaseGateError(f"{artifact_name} generatedAt must be present")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReleaseGateError(f"{artifact_name} generatedAt must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ReleaseGateError(f"{artifact_name} generatedAt must include timezone")
    return parsed


def _validate_online_smoke_freshness(production_payload: dict[str, Any], online_payload: dict[str, Any]) -> str:
    production_at = _parse_artifact_time(production_payload, "production-deploy-online.json")
    online_at = _parse_artifact_time(online_payload, "online-web-browser-smoke.json")
    if online_at < production_at:
        raise ReleaseGateError("online browser smoke must be generated after production deploy")
    return "online browser smoke is newer than or equal to production deploy evidence"


def _validate_online_web_browser_smoke_waiver_artifact(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True)
    _ensure_no_secret_markers(serialized, "online-web-browser-smoke-waiver.json")
    _ensure_no_raw_feishu_ids(serialized, "online-web-browser-smoke-waiver.json")
    if payload.get("status") != "WAIVED":
        raise ReleaseGateError("online browser smoke waiver status must be WAIVED")
    if payload.get("scope") != "production-online-web-browser-smoke":
        raise ReleaseGateError("online browser smoke waiver scope mismatch")
    if payload.get("reason") != EXPECTED_ONLINE_SMOKE_WAIVER_REASON:
        raise ReleaseGateError("online browser smoke waiver reason mismatch")
    _parse_artifact_time(payload, "online-web-browser-smoke-waiver.json")
    for key in ("operatorInstructionHash", "productionDeployArtifactSha256"):
        if not _is_hash_shape(payload.get(key)):
            raise ReleaseGateError(f"online browser smoke waiver {key} must be uppercase hex hash")
    if payload.get("webTarballSha256") != EXPECTED_WEB_SERVICE_SHA256:
        raise ReleaseGateError("online browser smoke waiver webTarballSha256 mismatch")
    if payload.get("publicReleaseSha256") != EXPECTED_PUBLIC_RELEASE_ZIP_SHA256:
        raise ReleaseGateError("online browser smoke waiver publicReleaseSha256 mismatch")
    if payload.get("productionDeployArtifact") != "docs/v0.2.2/artifacts/production-deploy-online.json":
        raise ReleaseGateError("online browser smoke waiver productionDeployArtifact mismatch")
    if payload.get("productionDeployArtifactSha256") != _sha256_file(PRODUCTION_DEPLOY_ONLINE_ARTIFACT):
        raise ReleaseGateError("online browser smoke waiver productionDeployArtifactSha256 mismatch")
    redaction = payload.get("redaction")
    if not isinstance(redaction, dict):
        raise ReleaseGateError("online browser smoke waiver redaction must be an object")
    for key in ("rawInstructionPersisted", "rawTargetPersisted", "rawSecretsPersisted"):
        if redaction.get(key) is not False:
            raise ReleaseGateError(f"online browser smoke waiver redaction.{key} must be false")
    return "online browser smoke explicitly waived by operator with hash-only evidence"


def evaluate_release_gate() -> dict[str, Any]:
    errors: list[str] = []
    checks: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    feishu_im_smoke_valid = False

    matrix_summary: dict[str, Any] = {}
    matrix_payload: dict[str, Any] = {}
    try:
        matrix_checker = _load_matrix_checker()
        matrix_summary = matrix_checker.validate_matrix(MATRIX_PATH)
        matrix_payload = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        _add_check(
            checks,
            "harness-matrix-reviewed",
            "pass" if matrix_summary.get("status") == "REVIEWED-PASS" else "fail",
            f"matrix status={matrix_summary.get('status')} rows={matrix_summary.get('rows')} commands={matrix_summary.get('commands')}",
        )
        if matrix_summary.get("status") != "REVIEWED-PASS":
            errors.append("harness matrix must be REVIEWED-PASS before release")
    except Exception as exc:
        errors.append(f"harness matrix invalid: {exc}")
        _add_check(checks, "harness-matrix-reviewed", "fail", str(exc))

    acceptance = _parse_acceptance_table(_read_text(ACCEPTANCE_PATH))
    missing_acceptance = sorted(REQUIRED_ACCEPTANCE_IDS - set(acceptance))
    if missing_acceptance:
        errors.append(f"acceptance checklist missing ids: {', '.join(missing_acceptance)}")
        _add_check(checks, "acceptance-ids-complete", "fail", ", ".join(missing_acceptance))
    else:
        _add_check(checks, "acceptance-ids-complete", "pass", "R22-01 through R22-19 present")

    incomplete_non_release = [
        item_id
        for item_id, row in sorted(acceptance.items())
        if item_id != "R22-12" and _status_is_incomplete(row.get("status", ""))
    ]
    if incomplete_non_release:
        errors.append(f"non-release acceptance items incomplete: {', '.join(incomplete_non_release)}")
        _add_check(checks, "non-release-acceptance-complete", "fail", ", ".join(incomplete_non_release))
    else:
        _add_check(checks, "non-release-acceptance-complete", "pass", "all non-release R22 items are non-pending")

    r22_12_status = acceptance.get("R22-12", {}).get("status", "")
    _add_check(checks, "r22-12-status", "pass" if r22_12_status else "fail", r22_12_status or "missing")

    if FEISHU_IM_SMOKE_ARTIFACT.exists():
        try:
            feishu_detail = _validate_feishu_im_smoke_artifact(_load_json(FEISHU_IM_SMOKE_ARTIFACT))
            feishu_im_smoke_valid = True
            _add_check(checks, "feishu-im-real-credential-smoke-valid", "pass", feishu_detail)
        except ReleaseGateError as exc:
            errors.append(str(exc))
            _add_check(checks, "feishu-im-real-credential-smoke-valid", "fail", str(exc))
    else:
        _add_check(
            checks,
            "feishu-im-real-credential-smoke-valid",
            "blocked",
            _display_path(FEISHU_IM_SMOKE_ARTIFACT),
        )

    for blocker in matrix_payload.get("externalBlockers") or []:
        if not isinstance(blocker, dict):
            continue
        if blocker.get("status") == "BLOCKER-PENDING-CREDENTIALS":
            if blocker.get("surface") == "feishu" and feishu_im_smoke_valid:
                continue
            _add_blocker(
                blockers,
                str(blocker.get("id") or "external-credential-blocker"),
                str(blocker.get("surface") or "external"),
                str(blocker.get("reason") or "external credentials are unavailable"),
            )

    review_pending = _pending_section(_read_text(REVIEW_PATH), "## Pending Reviews")
    evidence_pending = _pending_section(_read_text(EVIDENCE_PATH), "## Pending Evidence")
    pending_text = "\n".join(review_pending + evidence_pending).lower()
    if "real-network" in pending_text or "真实网络" in pending_text:
        _add_blocker(
            blockers,
            "broader-real-network-validation",
            "reconnect",
            "Broader real-network validation remains pending beyond deterministic browser weak-network fallback.",
        )
    if "feishu" in pending_text and "blocker-pending-credentials" in pending_text and not feishu_im_smoke_valid:
        _add_blocker(
            blockers,
            "feishu-im-real-credential-smoke",
            "feishu",
            "Real Feishu/IM smoke remains blocked by unavailable credentials.",
        )

    missing_scripts = [value for value in REQUIRED_RELEASE_SCRIPTS if not (ROOT / value).exists()]
    if missing_scripts:
        errors.append(f"release scripts missing: {', '.join(missing_scripts)}")
        _add_check(checks, "release-scripts-present", "fail", ", ".join(missing_scripts))
    else:
        _add_check(checks, "release-scripts-present", "pass", f"{len(REQUIRED_RELEASE_SCRIPTS)} scripts present")

    release_gate_text = ""
    if not RELEASE_GATE_DOC.exists():
        errors.append("release gate document missing")
        _add_check(checks, "release-gate-doc", "fail", str(RELEASE_GATE_DOC.relative_to(ROOT)))
    else:
        release_gate_text = RELEASE_GATE_DOC.read_text(encoding="utf-8")

    release_manifest_text = ""
    if not RELEASE_MANIFEST.exists():
        _add_blocker(
            blockers,
            "v022-release-manifest-missing",
            "release",
            "docs/v0.2.2/release-manifest.md has not been produced for a release candidate.",
        )
    else:
        release_manifest_text = RELEASE_MANIFEST.read_text(encoding="utf-8")

    try:
        public_promoted, public_detail = _validate_public_manifest_promotion()
        _add_check(
            checks,
            "public-manifest-promoted",
            "pass" if public_promoted else "blocked",
            public_detail,
        )
        if not public_promoted:
            _add_blocker(
                blockers,
                "public-manifest-not-promoted",
                "release",
                "Public download manifest is not promoted to v0.2.2 web-linux-service evidence.",
            )
    except ReleaseGateError as exc:
        errors.append(str(exc))
        _add_check(checks, "public-manifest-promoted", "fail", str(exc))

    try:
        defaults_promoted, defaults_detail = _validate_release_default_versions()
        _add_check(
            checks,
            "release-default-versions-promoted",
            "pass" if defaults_promoted else "blocked",
            defaults_detail,
        )
        if not defaults_promoted:
            _add_blocker(
                blockers,
                "release-defaults-not-promoted",
                "release",
                "Release install/check/package defaults still require explicit overrides for v0.2.2.",
            )
    except ReleaseGateError as exc:
        errors.append(str(exc))
        _add_check(checks, "release-default-versions-promoted", "fail", str(exc))

    try:
        public_package_valid, public_package_detail = _validate_public_release_package()
        _add_check(
            checks,
            "public-release-package-valid",
            "pass" if public_package_valid else "blocked",
            public_package_detail,
        )
        if not public_package_valid:
            _add_blocker(
                blockers,
                "public-release-package-not-built",
                "release",
                "Default public release package is not built or does not match the promoted v0.2.2 evidence.",
            )
    except ReleaseGateError as exc:
        errors.append(str(exc))
        _add_check(checks, "public-release-package-valid", "fail", str(exc))

    local_deploy_smoke_payload: dict[str, Any] | None = None
    target_deploy_smoke_passed = False
    if not DEPLOY_SMOKE_ARTIFACT.exists():
        _add_blocker(
            blockers,
            "deploy-rollback-smoke-missing",
            "release",
            "No v0.2.2 deploy/rollback smoke artifact exists yet.",
        )
    else:
        try:
            local_deploy_smoke_payload = _load_json(DEPLOY_SMOKE_ARTIFACT)
            smoke_detail = _validate_deploy_smoke_artifact(local_deploy_smoke_payload)
            if local_deploy_smoke_payload.get("scope") != "local-filesystem-web-linux-service":
                raise ReleaseGateError(
                    "release-deploy-rollback-smoke.json must remain local-filesystem-web-linux-service; "
                    "target evidence belongs in release-target-deploy-rollback-smoke.json"
                )
            _add_check(checks, "deploy-rollback-smoke-valid", "pass", smoke_detail)
        except ReleaseGateError as exc:
            errors.append(str(exc))
            _add_check(checks, "deploy-rollback-smoke-valid", "fail", str(exc))

    if TARGET_DEPLOY_SMOKE_ARTIFACT.exists():
        try:
            target_deploy_smoke_payload = _load_json(TARGET_DEPLOY_SMOKE_ARTIFACT)
            target_passed, target_detail = _validate_target_deploy_smoke_artifact(target_deploy_smoke_payload)
            target_deploy_smoke_passed = target_deploy_smoke_passed or target_passed
            _add_check(
                checks,
                "target-deploy-rollback-smoke-valid",
                "pass" if target_passed else "blocked",
                target_detail,
            )
        except ReleaseGateError as exc:
            errors.append(str(exc))
            _add_check(checks, "target-deploy-rollback-smoke-valid", "fail", str(exc))
    elif local_deploy_smoke_payload is not None and not target_deploy_smoke_passed:
        _add_check(
            checks,
            "target-deploy-rollback-smoke-valid",
            "blocked",
            "docs/v0.2.2/artifacts/release-target-deploy-rollback-smoke.json missing",
        )

    if TARGET_COMMAND_TEMPLATE_ARTIFACT.exists():
        try:
            target_template_payload = _load_json(TARGET_COMMAND_TEMPLATE_ARTIFACT)
            template_detail = _validate_target_command_template_artifact(target_template_payload)
            _add_check(checks, "target-command-template-valid", "pass", template_detail)
        except ReleaseGateError as exc:
            errors.append(str(exc))
            _add_check(checks, "target-command-template-valid", "fail", str(exc))

    if local_deploy_smoke_payload is not None:
        if target_deploy_smoke_passed:
            _add_check(
                checks,
                "target-environment-deploy-rollback",
                "pass",
                "target-environment deploy/rollback smoke passed",
            )
        else:
            _add_check(
                checks,
                "target-environment-deploy-rollback",
                "blocked",
                "only local-filesystem deploy/rollback smoke has passed",
            )
            _add_blocker(
                blockers,
                "target-environment-deploy-rollback-not-exercised",
                "release",
                "No target-environment deploy/rollback smoke has been exercised for the promoted Web release.",
            )

    production_payload: dict[str, Any] | None = None
    production_online_valid = False
    if PRODUCTION_DEPLOY_ONLINE_ARTIFACT.exists():
        try:
            production_payload = _load_json(PRODUCTION_DEPLOY_ONLINE_ARTIFACT)
            production_detail = _validate_production_deploy_online_artifact(production_payload)
            _add_check(checks, "production-deploy-online-valid", "pass", production_detail)
            production_online_valid = True
        except ReleaseGateError as exc:
            errors.append(str(exc))
            _add_check(checks, "production-deploy-online-valid", "fail", str(exc))
    else:
        message = f"{PRODUCTION_DEPLOY_ONLINE_ARTIFACT.relative_to(ROOT)} missing"
        errors.append(message)
        _add_check(checks, "production-deploy-online-valid", "fail", message)

    online_payload: dict[str, Any] | None = None
    online_web_browser_valid = False
    if ONLINE_WEB_BROWSER_SMOKE_ARTIFACT.exists():
        try:
            online_payload = _load_json(ONLINE_WEB_BROWSER_SMOKE_ARTIFACT)
            online_detail = _validate_online_web_browser_smoke_artifact(online_payload)
            _add_check(checks, "online-web-browser-smoke-valid", "pass", online_detail)
            online_web_browser_valid = True
        except ReleaseGateError as exc:
            if ONLINE_WEB_BROWSER_SMOKE_WAIVER_ARTIFACT.exists():
                try:
                    waiver_payload = _load_json(ONLINE_WEB_BROWSER_SMOKE_WAIVER_ARTIFACT)
                    waiver_detail = _validate_online_web_browser_smoke_waiver_artifact(waiver_payload)
                    _add_check(checks, "online-web-browser-smoke-waiver-valid", "pass", waiver_detail)
                    _add_check(checks, "online-web-browser-smoke-valid", "waived", str(exc))
                except ReleaseGateError as waiver_exc:
                    errors.append(str(exc))
                    errors.append(str(waiver_exc))
                    _add_check(checks, "online-web-browser-smoke-waiver-valid", "fail", str(waiver_exc))
                    _add_check(checks, "online-web-browser-smoke-valid", "fail", str(exc))
                    _add_blocker(
                        blockers,
                        "online-web-browser-smoke-not-pass",
                        "release",
                        "Online browser smoke did not pass and the waiver artifact is invalid.",
                    )
            else:
                errors.append(str(exc))
                _add_check(checks, "online-web-browser-smoke-valid", "fail", str(exc))
                _add_blocker(
                    blockers,
                    "online-web-browser-smoke-not-pass",
                    "release",
                    "Online browser smoke did not pass; current deployment remains live and manual user testing is required.",
                )
    else:
        message = f"{ONLINE_WEB_BROWSER_SMOKE_ARTIFACT.relative_to(ROOT)} missing"
        if ONLINE_WEB_BROWSER_SMOKE_WAIVER_ARTIFACT.exists():
            try:
                waiver_payload = _load_json(ONLINE_WEB_BROWSER_SMOKE_WAIVER_ARTIFACT)
                waiver_detail = _validate_online_web_browser_smoke_waiver_artifact(waiver_payload)
                _add_check(checks, "online-web-browser-smoke-waiver-valid", "pass", waiver_detail)
                _add_check(checks, "online-web-browser-smoke-valid", "waived", message)
            except ReleaseGateError as waiver_exc:
                errors.append(message)
                errors.append(str(waiver_exc))
                _add_check(checks, "online-web-browser-smoke-waiver-valid", "fail", str(waiver_exc))
                _add_check(checks, "online-web-browser-smoke-valid", "fail", message)
                _add_blocker(
                    blockers,
                    "online-web-browser-smoke-not-pass",
                    "release",
                    "Online browser smoke artifact is missing and the waiver artifact is invalid.",
                )
        else:
            errors.append(message)
            _add_check(checks, "online-web-browser-smoke-valid", "fail", message)
            _add_blocker(
                blockers,
                "online-web-browser-smoke-not-pass",
                "release",
                "Online browser smoke artifact is missing; manual user testing is required before release PASS.",
            )

    if production_online_valid and online_web_browser_valid and production_payload is not None and online_payload is not None:
        try:
            freshness_detail = _validate_online_smoke_freshness(production_payload, online_payload)
            _add_check(checks, "online-web-browser-smoke-freshness", "pass", freshness_detail)
        except ReleaseGateError as exc:
            errors.append(str(exc))
            _add_check(checks, "online-web-browser-smoke-freshness", "fail", str(exc))
            _add_blocker(
                blockers,
                "online-web-browser-smoke-not-pass",
                "release",
                "Online browser smoke is older than production deploy evidence; manual user testing is required.",
            )

    if r22_12_status and "PASS" not in r22_12_status.upper() and not blockers:
        _add_blocker(
            blockers,
            "final-release-review-not-pass",
            "release",
            f"R22-12 status is {r22_12_status}; final release review is not pass.",
        )

    blocker_ids = {str(item.get("id") or "") for item in blockers if item.get("id")}
    document_state = "pass" if not errors and not blockers else "blocked"
    if release_gate_text:
        try:
            gate_detail = _validate_release_gate_doc(release_gate_text, document_state, blocker_ids)
            _add_check(checks, "release-gate-doc", "pass", gate_detail)
        except ReleaseGateError as exc:
            errors.append(str(exc))
            _add_check(checks, "release-gate-doc", "fail", str(exc))

    if release_manifest_text:
        try:
            manifest_detail = _validate_release_manifest(release_manifest_text, document_state, blocker_ids)
            _add_check(checks, "release-manifest-valid", "pass", manifest_detail)
        except ReleaseGateError as exc:
            errors.append(str(exc))
            _add_check(checks, "release-manifest-valid", "fail", str(exc))

    releasable = not errors and not blockers
    return {
        "status": "PASS" if releasable else "BLOCKED",
        "releasable": releasable,
        "errors": errors,
        "blockers": blockers,
        "checks": checks,
        "matrix": matrix_summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a short text summary.")
    parser.add_argument("--require-releasable", action="store_true", help="Return non-zero while release blockers remain.")
    parser.add_argument("--artifact", default="", help="Optional path to write the JSON gate result.")
    args = parser.parse_args(argv)

    try:
        result = evaluate_release_gate()
    except ReleaseGateError as exc:
        print(f"v0.2.2 release gate check failed: {exc}", file=sys.stderr)
        return 2

    if args.artifact:
        artifact = Path(args.artifact)
        if not artifact.is_absolute():
            artifact = ROOT / artifact
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"v0.2.2 release gate {result['status']}: "
            f"{len(result['blockers'])} blockers, {len(result['errors'])} errors"
        )

    if result["errors"]:
        return 2
    if args.require_releasable and not result["releasable"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
