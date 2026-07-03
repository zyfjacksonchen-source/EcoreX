#!/usr/bin/env python3
"""Aggregate v0.2.6 production evidence into a fail-closed final gate."""

from __future__ import annotations

import hashlib
import json
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.6"
ARTIFACT = ROOT / "docs" / "web-runtime-goal" / "artifacts" / "S12-production-final-gate.json"


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def archive_file_bytes(path: Path, suffix: str) -> bytes | None:
    normalized_suffix = suffix.replace("\\", "/").lstrip("/")
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            matches = [name for name in archive.namelist() if name.replace("\\", "/").endswith(normalized_suffix)]
            if len(matches) != 1:
                return None
            return archive.read(matches[0])
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            matches = [member for member in archive.getmembers() if member.isfile() and member.name.replace("\\", "/").endswith(normalized_suffix)]
            if len(matches) != 1:
                return None
            extracted = archive.extractfile(matches[0])
            if extracted is None:
                return None
            return extracted.read()
    return None


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def add(checks: list[dict[str, Any]], name: str, ok: bool, detail: dict[str, Any] | None = None) -> None:
    checks.append({"name": name, "status": "PASS" if ok else "FAIL", "detail": detail or {}})


def status_pass(value: Any) -> bool:
    return str(value or "").strip().upper() == "PASS"


def artifact_summary(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    return {
        "path": str(path),
        "status": payload.get("status"),
        "checkCount": payload.get("checkCount"),
        "passCount": payload.get("passCount"),
        "failCount": payload.get("failCount"),
        "sha256": sha_file(path),
    }


def main() -> int:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    evidence_paths = {
        "deploy": ROOT / "docs" / f"v{VERSION}" / "artifacts" / "production-deploy-online.json",
        "userBehavior200": ROOT / "docs" / f"v{VERSION}" / "artifacts" / "production-200-user-behavior.json",
        "imageOcrVision32": ROOT / "docs" / f"v{VERSION}" / "artifacts" / "production-32-image-ocr-vision-toolchain.json",
        "browserUi18": ROOT / "docs" / f"v{VERSION}" / "artifacts" / "production-browser-ui-v026-smoke.json",
        "agentProduct450": ROOT / "docs" / f"v{VERSION}" / "artifacts" / "production-agent-product-acceptance.json",
        "tongxinS13": ROOT / "docs" / "web-runtime-goal" / "artifacts" / "S13-tongxin-cli-bundled-realtime.json",
        "tongxinPostdeploy": ROOT / "docs" / f"v{VERSION}" / "artifacts" / "production-tongxin-postdeploy.json",
    }
    inputs = {key: artifact_summary(path) for key, path in evidence_paths.items()}
    manifest = read_json(ROOT / "deploy" / "ecorex-site" / "manifest.json")
    manifest_artifacts = {str(item.get("id")): item for item in manifest.get("artifacts") or []}

    release_files = {
        "webui-windows-x64": ROOT / "release-artifacts" / f"EcoreX_{VERSION}-webui-windows-x64.zip",
        "webui-macos-universal": ROOT / "release-artifacts" / f"EcoreX_{VERSION}-webui-macos-universal.zip",
        "web-linux-service": ROOT / "release-artifacts" / f"EcoreX_{VERSION}-web-linux-service.tar.gz",
        "public-release": ROOT / "release-artifacts" / f"EcoreX_{VERSION}-public-release.zip",
    }
    release_artifacts: dict[str, dict[str, Any]] = {}
    for artifact_id, path in release_files.items():
        manifest_item = manifest_artifacts.get(artifact_id) or {}
        item = {
            "file": path.name,
            "size": path.stat().st_size,
            "sha256": sha_file(path),
        }
        if artifact_id == "public-release":
            item.update({"manifestSha256": None, "manifestSize": None, "manifestMatch": "not-applicable"})
        else:
            item.update({
                "manifestSha256": manifest_item.get("sha256"),
                "manifestSize": manifest_item.get("size"),
                "manifestMatch": path.stat().st_size == manifest_item.get("size") and sha_file(path) == manifest_item.get("sha256"),
            })
        release_artifacts[artifact_id] = item

    checks: list[dict[str, Any]] = []
    for key, item in inputs.items():
        add(checks, f"{key} artifact PASS", status_pass(item.get("status")), {
            "checkCount": item.get("checkCount"),
            "passCount": item.get("passCount"),
            "failCount": item.get("failCount"),
        })
    agent_product = inputs.get("agentProduct450") or {}
    add(
        checks,
        "agent product acceptance covers full 450-check matrix",
        agent_product.get("checkCount") == 450
        and agent_product.get("passCount") == 450
        and agent_product.get("failCount") == 0,
        {
            "checkCount": agent_product.get("checkCount"),
            "passCount": agent_product.get("passCount"),
            "failCount": agent_product.get("failCount"),
        },
    )
    tongxin_s13 = read_json(evidence_paths["tongxinS13"])
    tongxin_remote = tongxin_s13.get("remoteValidation") or {}
    tongxin_privacy = tongxin_s13.get("privacy") or {}
    tongxin_local = tongxin_s13.get("localValidation") or {}
    tongxin_models = tongxin_local.get("modelsImport") or {}
    tongxin_deps = tongxin_local.get("runtimeDependencies") or {}
    tongxin_meta = tongxin_remote.get("metaEvidence") or {}
    tongxin_counts = tongxin_remote.get("realtimeStatusCounts") or {}
    tongxin_postdeploy = read_json(evidence_paths["tongxinPostdeploy"])
    tongxin_postdeploy_probe = tongxin_postdeploy.get("probe") or {}
    tongxin_postdeploy_real = tongxin_postdeploy_probe.get("realProbe") or {}
    tongxin_postdeploy_statuses = tongxin_postdeploy_real.get("realtimeStatusCounts") or {}
    tongxin_postdeploy_wrapper = tongxin_postdeploy_real.get("sampleWrapperRealtime") or {}
    add(
        checks,
        "S13 Tongxin realtime account-id direct path validated",
        tongxin_remote.get("numericAccountCount") == 42
        and tongxin_remote.get("testedRealtimeAccountCount") == 42
        and tongxin_counts.get("ok") == 42
        and tongxin_meta.get("accountResolution") == "direct_account_id"
        and tongxin_meta.get("cacheDatabaseExists") is False,
        {
            "numericAccountCount": tongxin_remote.get("numericAccountCount"),
            "testedRealtimeAccountCount": tongxin_remote.get("testedRealtimeAccountCount"),
            "realtimeStatusCounts": tongxin_counts,
            "accountResolution": tongxin_meta.get("accountResolution"),
        },
    )
    add(
        checks,
        "S13 Tongxin artifact redacts business and credential material",
        tongxin_remote.get("rawAccountListQueryPersisted") is False
        and tongxin_remote.get("rawAccountIdsPersisted") is False
        and tongxin_remote.get("rawAccountNamesPersisted") is False
        and tongxin_remote.get("rawCompanyNamesPersisted") is False
        and tongxin_remote.get("exactRealtimeCostPersisted") is False
        and tongxin_privacy.get("rawCredentialPersisted") is False
        and tongxin_privacy.get("rawTokenPersisted") is False
        and tongxin_privacy.get("rawBusinessIdentifiersPersisted") is False
        and tongxin_privacy.get("rawExactSpendPersisted") is False,
        {
            "rawAccountListQueryPersisted": tongxin_remote.get("rawAccountListQueryPersisted"),
            "rawAccountIdsPersisted": tongxin_remote.get("rawAccountIdsPersisted"),
            "rawAccountNamesPersisted": tongxin_remote.get("rawAccountNamesPersisted"),
            "rawCompanyNamesPersisted": tongxin_remote.get("rawCompanyNamesPersisted"),
            "exactRealtimeCostPersisted": tongxin_remote.get("exactRealtimeCostPersisted"),
        },
    )
    add(
        checks,
        "S13 Tongxin models compatibility exports DATABASE Database database and get_db",
        tongxin_models.get("hasDatabase") is True
        and tongxin_models.get("hasDatabaseAlias") is True
        and tongxin_models.get("hasLowercaseDatabase") is True
        and tongxin_models.get("databaseEquals") is True
        and tongxin_models.get("hasGetDb") is True,
        tongxin_models,
    )
    add(
        checks,
        "S13 Tongxin postdeploy server runtime and state package compatibility validated",
        tongxin_postdeploy_probe.get("releaseVersion") == VERSION
        and (tongxin_postdeploy_probe.get("fileSummary") or {}).get("allMatch") is True
        and (tongxin_postdeploy_probe.get("runtimeModels") or {}).get("hasDatabaseAlias") is True
        and (tongxin_postdeploy_probe.get("stateModels") or {}).get("hasDatabaseAlias") is True
        and (tongxin_postdeploy_probe.get("wrapperStatus") or {}).get("bundledAvailable") is True
        and (tongxin_postdeploy_probe.get("wrapperStatus") or {}).get("remotePreferred") is False,
        {
            "releaseVersion": tongxin_postdeploy_probe.get("releaseVersion"),
            "fileSummary": tongxin_postdeploy_probe.get("fileSummary"),
            "wrapperStatus": {
                "bundledAvailable": (tongxin_postdeploy_probe.get("wrapperStatus") or {}).get("bundledAvailable"),
                "remotePreferred": (tongxin_postdeploy_probe.get("wrapperStatus") or {}).get("remotePreferred"),
            },
        },
    )
    add(
        checks,
        "S13 Tongxin postdeploy real account discovery and sampled realtime path validated",
        tongxin_postdeploy_real.get("numericAccountCount") == 42
        and tongxin_postdeploy_real.get("sampledRealtimeAccountCount") == 3
        and tongxin_postdeploy_statuses.get("ok") == 3
        and tongxin_postdeploy_wrapper.get("toolStatus") == "success"
        and tongxin_postdeploy_wrapper.get("ok") is True
        and tongxin_postdeploy_wrapper.get("metaAccountResolution") == "direct_account_id"
        and int(tongxin_postdeploy_wrapper.get("rowCount") or 0) > 0
        and tongxin_postdeploy_real.get("rawAccountIdsPersisted") is False
        and tongxin_postdeploy_real.get("exactRealtimeCostPersisted") is False,
        {
            "numericAccountCount": tongxin_postdeploy_real.get("numericAccountCount"),
            "sampledRealtimeAccountCount": tongxin_postdeploy_real.get("sampledRealtimeAccountCount"),
            "realtimeStatusCounts": tongxin_postdeploy_statuses,
            "sampleWrapperRealtime": tongxin_postdeploy_wrapper,
        },
    )
    add(
        checks,
        "S13 Tongxin cryptography dependency declared in core runtime",
        tongxin_deps.get("cryptographyDeclaredInRootRequirements") is True
        and tongxin_deps.get("cryptographyDeclaredInWebCoreRequirements") is True
        and tongxin_deps.get("webAndDesktopCoreRequirementsMatch") is True,
        tongxin_deps,
    )
    tongxin_bundled_files = tongxin_s13.get("bundledFiles") or {}
    tongxin_release_hashes: dict[str, Any] = {}
    for artifact_id in ("webui-windows-x64", "webui-macos-universal", "web-linux-service"):
        path = release_files[artifact_id]
        file_results = {}
        for relative_path, expected_sha in tongxin_bundled_files.items():
            normalized_relative_path = str(relative_path).replace("\\", "/")
            suffix = f"runtime/{normalized_relative_path}"
            data = archive_file_bytes(path, suffix)
            file_results[relative_path] = {
                "present": data is not None,
                "match": data is not None and sha_bytes(data) == expected_sha,
            }
        tongxin_release_hashes[artifact_id] = file_results
    add(
        checks,
        "S13 Tongxin bundled package hashes are present in release artifacts",
        bool(tongxin_bundled_files)
        and all(
            result.get("present") is True and result.get("match") is True
            for artifact_results in tongxin_release_hashes.values()
            for result in artifact_results.values()
        ),
        {"artifactCount": len(tongxin_release_hashes), "fileCount": len(tongxin_bundled_files), "results": tongxin_release_hashes},
    )
    deploy_checks = read_json(evidence_paths["deploy"]).get("onlineChecks") or {}
    add(
        checks,
        "deployment service active/enabled/version",
        deploy_checks.get("serviceActive") is True
        and deploy_checks.get("serviceEnabled") is True
        and deploy_checks.get("webVersionStatus") == 200
        and deploy_checks.get("webServiceVersion") == VERSION,
    )
    for artifact_id, item in release_artifacts.items():
        add(checks, f"{artifact_id} release artifact present", release_files[artifact_id].exists(), {"size": item["size"], "sha256": item["sha256"]})
        if artifact_id != "public-release":
            add(checks, f"{artifact_id} manifest hash matches file", item["manifestMatch"] is True, {
                "manifestSha256": item.get("manifestSha256"),
                "sha256": item["sha256"],
            })
    shortcut = Path.home() / "Desktop" / "EcoreX WebUI.url"
    shortcut_text = shortcut.read_text(encoding="utf-8", errors="replace") if shortcut.exists() else ""
    add(checks, "desktop shortcut points to v0.2.6 web app", "ecorex-agent/app/" in shortcut_text and f"v{VERSION}" in shortcut_text, {
        "exists": shortcut.exists(),
        "rawUrlPersisted": False,
    })

    failures = [item for item in checks if item["status"] != "PASS"]
    payload = {
        "status": "PASS" if not failures else "FAIL",
        "version": VERSION,
        "scope": "S12-production-final-gate",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "checkCount": len(checks),
        "passCount": sum(1 for item in checks if item["status"] == "PASS"),
        "failCount": len(failures),
        "checks": checks,
        "inputs": inputs,
        "releaseArtifacts": release_artifacts,
        "redaction": {"rawPasswordPersisted": False, "rawUrlPersisted": False, "rawSecretPersisted": False},
    }
    ARTIFACT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "artifact": str(ARTIFACT), "checkCount": payload["checkCount"], "passCount": payload["passCount"], "failCount": payload["failCount"]}, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
