#!/usr/bin/env python3
"""Validate v0.2.5 skill/capability to tool binding contracts."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _json_ready(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _binding_errors(owner: str, binding: Mapping[str, Any] | None) -> list[str]:
    if not binding:
        return [f"{owner}: missing toolBinding"]
    errors: list[str] = []
    for key in ("toolName", "dependencies", "probe", "smoke", "failurePrompt", "classification", "probeState"):
        if key not in binding or binding.get(key) in (None, "", {}):
            errors.append(f"{owner}: missing toolBinding.{key}")
    if str(binding.get("status") or "") in {"missing_binding_contract", "tool_not_loaded"}:
        errors.append(f"{owner}: binding status is {binding.get('status')}")
    if binding.get("status") == "ready" and not binding.get("probeState"):
        errors.append(f"{owner}: ready without probeState")
    probe = binding.get("probe") if isinstance(binding.get("probe"), Mapping) else {}
    smoke = binding.get("smoke") if isinstance(binding.get("smoke"), Mapping) else {}
    if not probe.get("tool") or not probe.get("action"):
        errors.append(f"{owner}: incomplete probe contract")
    if not smoke.get("tool") or not smoke.get("action"):
        errors.append(f"{owner}: incomplete smoke contract")
    return errors


def _load_builtin_rows() -> list[dict[str, Any]]:
    from agent.skills.manager import SkillManager
    from agent.skills.service import SkillService

    with tempfile.TemporaryDirectory(prefix="ecorex-v025-skill-bindings-") as tmp:
        manager = SkillManager(
            builtin_dir=str(ROOT / "skills"),
            custom_dir=str(Path(tmp) / "skills"),
            config={},
        )
        manager.extra_dirs = []
        manager.refresh_skills()
        return SkillService(manager).query()


def _extension_rows() -> list[dict[str, Any]]:
    from agent.extensions import ExtensionRegistry

    with tempfile.TemporaryDirectory(prefix="ecorex-v025-extension-bindings-") as tmp:
        return ExtensionRegistry(str(Path(tmp) / "workspace")).list_extensions().get("extensions", [])


def run_check() -> dict[str, Any]:
    from agent.skills.tool_bridge import resolve_callable_tool_name
    from agent.skills.tool_binding_contract import TOOL_BINDING_CONTRACTS

    errors: list[str] = []
    skill_evidence: list[dict[str, Any]] = []
    for row in _load_builtin_rows():
        name = str(row.get("name") or "")
        tool_name = resolve_callable_tool_name(row)
        evidence = {
            "name": name,
            "toolName": row.get("toolName") or tool_name or "",
            "status": (row.get("toolBinding") or {}).get("status") if isinstance(row.get("toolBinding"), dict) else row.get("agentSurface", {}).get("status"),
            "schemaVisible": bool(row.get("schemaVisible")),
            "builtin": bool(row.get("builtin_catalog")),
        }
        skill_evidence.append(evidence)
        if not tool_name:
            continue
        if tool_name not in TOOL_BINDING_CONTRACTS:
            errors.append(f"skill:{name}: {tool_name} has no v0.2.5 binding contract")
            continue
        errors.extend(_binding_errors(f"skill:{name}", row.get("toolBinding")))

    extension_evidence: list[dict[str, Any]] = []
    for row in _extension_rows():
        entry_id = str(row.get("id") or "")
        binding = row.get("toolBinding") if isinstance(row.get("toolBinding"), dict) else None
        tool_name = str(row.get("toolName") or "")
        if binding:
            if row.get("type") == "builtin_tool" and binding.get("status") == "missing_binding_contract":
                continue
            extension_evidence.append({
                "id": entry_id,
                "type": row.get("type"),
                "toolName": tool_name,
                "bindingStatus": binding.get("status"),
                "status": row.get("status"),
            })
            errors.extend(_binding_errors(entry_id, binding))
        elif tool_name:
            errors.append(f"{entry_id}: has toolName={tool_name} but no binding contract")

    return {
        "schemaVersion": "v0.2.5-skill-tool-binding-check-v1",
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "skills": skill_evidence,
        "extensions": extension_evidence,
        "redacted": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(ROOT / "docs" / "v0.2.5" / "artifacts" / "v0.2.5-skill-tool-binding-probe.json"),
        help="Evidence output JSON path.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full JSON evidence.")
    args = parser.parse_args()

    payload = run_check()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(_json_ready(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(_json_ready(payload), ensure_ascii=False, indent=2))
    elif payload["status"] == "pass":
        print(f"PASS: v0.2.5 skill/tool bindings validated ({output})")
    else:
        print(f"FAIL: v0.2.5 skill/tool bindings failed ({output})")
        for error in payload["errors"]:
            print(f"- {error}")
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
