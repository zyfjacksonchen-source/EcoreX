#!/usr/bin/env python3
"""Validate the v0.2.5 runtime baseline evidence contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "docs" / "v0.2.5" / "artifacts" / "v0.2.5-runtime-baseline.json"
REQUIRED_DEPENDENCIES = {"node", "npm/npx", "lark-cli", "xin_agent_cli.py", "python"}
REQUIRED_GAP_SIGNALS = {
    "localFeishuCommandUsesCodexPrivatePath",
    "localFeishuUsesSystemPackageManager",
    "productionServiceUserNodeMissing",
    "productionServiceUserNpmMissing",
    "productionFeishuCliMissing",
    "productionTongxinCliMissingConfig",
    "windowsArtifactOnlyHasPlaywrightNode",
    "macosArtifactHasNoRuntimeNode",
    "linuxArtifactHasNoRuntimeNode",
    "publicReleaseContainsNestedArtifacts",
}
EXPECTED_DEPENDENCY_CONTRACTS = {
    "node": {
        "targetResolver": "RuntimeDependencyProvider.resolve_executable('node')",
        "expectedOwner": "ecorex-bundled or ecorex-state",
        "usedByAny": {"MCP", "Office artifact JS", "PDF JS renderers", "Feishu/Lark CLI runner"},
    },
    "npm/npx": {
        "targetResolver": "RuntimeDependencyProvider.resolve_executable('npm'/'npx')",
        "expectedOwner": "ecorex-bundled or ecorex-state",
        "usedByAny": {"capability installs", "MCP bootstrap", "official CLI installs"},
    },
    "lark-cli": {
        "targetResolver": "ToolExecutionEnvironment for feishu_cli",
        "expectedOwner": "ecorex-state or ecorex-bundled",
        "usedByAny": {"Feishu/Lark structured CLI canary"},
    },
    "xin_agent_cli.py": {
        "targetResolver": "ToolExecutionEnvironment for tongxin_cli plus verified bootstrap",
        "expectedOwner": "ecorex-state or explicit operator-approved path",
        "usedByAny": {"Tongxin structured read-only CLI canary"},
    },
    "python": {
        "targetResolver": "RuntimeDependencyProvider.python()",
        "expectedOwner": "ecorex-bundled",
        "usedByAny": {"all Python tools", "Office/PDF", "Imagegen", "diagnostics"},
    },
}
FORBIDDEN_RAW_KEYS = {
    "stdout",
    "stderr",
    "rawoutput",
    "raw_output",
    "rawstdout",
    "rawstderr",
    "stdoutredacted",
    "stderrredacted",
    "target",
    "sshhosthash",
    "sshuserhash",
    "domainhash",
    "message",
}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
]


def fail(message: str) -> None:
    raise SystemExit(f"FAIL {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def nested_get(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def walk_keys(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        keys: list[str] = []
        for key, child in value.items():
            child_path = f"{prefix}.{key}" if prefix else str(key)
            keys.append(child_path)
            keys.extend(walk_keys(child, child_path))
        return keys
    if isinstance(value, list):
        keys: list[str] = []
        for index, child in enumerate(value):
            keys.extend(walk_keys(child, f"{prefix}[{index}]"))
        return keys
    return []


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--allow-skipped-production", action="store_true")
    return parser.parse_args(argv[1:])


def artifact_runtime_hits(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return []
    hits = item.get("runtimeHits")
    return [str(hit).replace("\\", "/") for hit in hits if isinstance(hit, str)] if isinstance(hits, list) else []


def compute_gap_signals(data: dict[str, Any]) -> dict[str, bool]:
    local_feishu = nested_get(data, "ecorexLocalWebui", "toolStatuses", "feishuCli")
    production = data.get("production") if isinstance(data.get("production"), dict) else {}
    prod_parsed = production.get("parsed") if isinstance(production.get("parsed"), dict) else {}
    prod_service = prod_parsed.get("serviceUser") if isinstance(prod_parsed.get("serviceUser"), dict) else {}
    prod_tools = prod_parsed.get("toolStatuses") if isinstance(prod_parsed.get("toolStatuses"), dict) else {}
    prod_feishu = prod_tools.get("feishuCli") if isinstance(prod_tools.get("feishuCli"), dict) else {}
    prod_tongxin = prod_tools.get("tongxinCli") if isinstance(prod_tools.get("tongxinCli"), dict) else {}
    artifacts = data.get("ecorexArtifacts") if isinstance(data.get("ecorexArtifacts"), list) else []
    by_label = {item.get("label"): item for item in artifacts if isinstance(item, dict)}
    windows_hits = artifact_runtime_hits(by_label.get("webui-windows-x64"))
    macos_hits = artifact_runtime_hits(by_label.get("webui-macos-universal"))
    linux_hits = artifact_runtime_hits(by_label.get("web-linux-service"))
    public_release = by_label.get("public-release") if isinstance(by_label.get("public-release"), dict) else {}
    return {
        "localFeishuCommandUsesCodexPrivatePath": isinstance(local_feishu, dict) and local_feishu.get("commandSource") == "codex-private",
        "localFeishuUsesSystemPackageManager": isinstance(local_feishu, dict) and (local_feishu.get("npmSource") == "system-path" or local_feishu.get("npxSource") == "system-path"),
        "productionServiceUserNodeMissing": (prod_service.get("node") or {}).get("source") == "missing",
        "productionServiceUserNpmMissing": (prod_service.get("npm") or {}).get("source") == "missing",
        "productionFeishuCliMissing": prod_feishu.get("authState") == "cli_missing",
        "productionTongxinCliMissingConfig": prod_tongxin.get("configurationState") == "missing",
        "windowsArtifactOnlyHasPlaywrightNode": len(windows_hits) == 1 and windows_hits[0].endswith("runtime/python/Lib/site-packages/playwright/driver/node.exe"),
        "macosArtifactHasNoRuntimeNode": not macos_hits,
        "linuxArtifactHasNoRuntimeNode": not linux_hits,
        "publicReleaseContainsNestedArtifacts": isinstance(public_release.get("nestedArtifacts"), list) and bool(public_release.get("nestedArtifacts")),
    }


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    path = args.report
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = json.dumps(data, ensure_ascii=False)

    for pattern in SECRET_PATTERNS:
        require(not pattern.search(raw), f"sensitive pattern leaked: {pattern.pattern}")
    forbidden_paths = [
        key_path
        for key_path in walk_keys(data)
        if key_path.rsplit(".", 1)[-1].lower() in FORBIDDEN_RAW_KEYS
    ]
    require(not forbidden_paths, f"raw/sensitive keys must not be stored: {forbidden_paths[:5]}")

    require(data.get("schemaVersion") == "v025-runtime-baseline-v1", "unexpected schemaVersion")
    require(data.get("version") == "v0.2.5", "unexpected version label")
    require(data.get("baselineComparisonVersion") == "0.2.4", "baseline comparison version must be explicit")
    require("prior-version" in str(data.get("baselineComparisonPurpose") or ""), "baseline purpose must say prior-version")
    generated_at = str(data.get("generatedAt") or "")
    try:
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        fail("generatedAt must be ISO-8601")
    script = data.get("script")
    require(isinstance(script, dict), "script section missing")
    require(script.get("path") == "scripts/capture-v025-runtime-baseline.py", "capture script path mismatch")
    require(script.get("sha256") == file_sha256(ROOT / "scripts" / "capture-v025-runtime-baseline.py"), "capture script sha256 is stale")
    argv_record = script.get("argv")
    require(isinstance(argv_record, list), "script argv missing")
    if not args.allow_skipped_production:
        require("--include-production" in argv_record, "baseline must be captured with --include-production")
    require(bool(nested_get(data, "git", "head")), "git head missing")

    dependency_map = data.get("dependencyExecutionMap")
    require(isinstance(dependency_map, list), "dependencyExecutionMap missing")
    by_dependency = {str(item.get("dependency")): item for item in dependency_map if isinstance(item, dict)}
    require(REQUIRED_DEPENDENCIES.issubset(by_dependency), "dependencyExecutionMap incomplete")
    for name, contract in EXPECTED_DEPENDENCY_CONTRACTS.items():
        item = by_dependency[name]
        require(item.get("targetResolver") == contract["targetResolver"], f"{name} targetResolver drifted")
        require(item.get("expectedOwner") == contract["expectedOwner"], f"{name} expectedOwner drifted")
        used_by = item.get("usedBy")
        require(isinstance(used_by, list) and set(used_by) == contract["usedByAny"], f"{name} usedBy drifted")

    local_tools = nested_get(data, "ecorexLocalWebui", "toolStatuses")
    require(isinstance(local_tools, dict), "local toolStatuses missing")
    local_feishu = local_tools.get("feishuCli")
    require(isinstance(local_feishu, dict), "local feishuCli status missing")
    require(isinstance(local_tools.get("tongxinCli"), dict), "local tongxinCli status missing")
    require(local_feishu.get("commandSource") == "codex-private", "S0 must capture local Feishu codex-private dependency leak")
    require(local_feishu.get("npmSource") == "system-path" or local_feishu.get("npxSource") == "system-path", "S0 must capture local Feishu system package-manager dependency")

    artifacts = data.get("ecorexArtifacts")
    require(isinstance(artifacts, list) and artifacts, "artifact scans missing")
    by_label = {item.get("label"): item for item in artifacts if isinstance(item, dict)}
    require({"webui-windows-x64", "webui-macos-universal", "web-linux-service", "public-release"}.issubset(by_label), "artifact labels incomplete")
    for label, item in by_label.items():
        require(item.get("baselineUse") == "prior-version-runtime-gap-comparison", f"{label} baselineUse missing")
        require(bool(item.get("sha256")), f"{label} sha256 missing")
        require(isinstance(item.get("toolEntrypoints"), dict), f"{label} toolEntrypoints missing")
        require(isinstance(item.get("configSignals"), dict), f"{label} configSignals missing")
    require(isinstance(by_label["public-release"].get("nestedArtifacts"), list), "public nestedArtifacts missing")
    require(by_label["public-release"].get("nestedArtifacts"), "public nestedArtifacts must not be empty")

    production = data.get("production")
    require(isinstance(production, dict), "production section missing")
    if production.get("status") == "success":
        parsed = production.get("parsed")
        require(isinstance(parsed, dict), "production parsed section missing")
        require(isinstance(parsed.get("hostShell"), dict), "production hostShell missing")
        service_user = parsed.get("serviceUser")
        require(isinstance(service_user, dict), "production serviceUser missing")
        require((service_user.get("node") or {}).get("source") == "missing", "S0 must capture production service-user missing node")
        require((service_user.get("npm") or {}).get("source") == "missing", "S0 must capture production service-user missing npm")
        require((service_user.get("npx") or {}).get("source") == "missing", "S0 must capture production service-user missing npx")
        prod_feishu = nested_get(parsed, "toolStatuses", "feishuCli")
        prod_tongxin = nested_get(parsed, "toolStatuses", "tongxinCli")
        require(isinstance(prod_feishu, dict), "production feishu status missing")
        require(isinstance(prod_tongxin, dict), "production tongxin status missing")
        require(prod_feishu.get("authState") == "cli_missing", "S0 must capture production Feishu CLI missing")
        require((prod_feishu.get("command") or {}).get("source") == "missing", "S0 must capture production Feishu command missing")
        require(prod_tongxin.get("configurationState") == "missing", "S0 must capture production Tongxin config missing")
    else:
        require(args.allow_skipped_production and production.get("status") == "skipped", "production must be success unless --allow-skipped-production is explicit")

    gap_signals = data.get("baselineGapSignals")
    require(isinstance(gap_signals, dict), "baselineGapSignals missing")
    require(gap_signals.get("schemaVersion") == "v025-runtime-gap-signals-v1", "baselineGapSignals schema mismatch")
    computed_gap_signals = compute_gap_signals(data)
    for name in REQUIRED_GAP_SIGNALS:
        require(computed_gap_signals.get(name) is True, f"computed baseline gap signal is not true: {name}")
        require(gap_signals.get(name) == computed_gap_signals.get(name), f"recorded baseline gap signal drifted: {name}")

    print(f"PASS {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
