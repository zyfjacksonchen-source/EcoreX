#!/usr/bin/env python3
"""Verify v0.2.4 EcoreX-native skill facade mappings."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any


EXPECTED_FACADES = {
    "office-presentations": "Presentations",
    "office-spreadsheets": "Spreadsheets",
    "office-documents": "documents",
    "office-pdf": "pdf",
    "image-generation": "imagegen",
}

EXPECTED_TOOLS = {
    "office-presentations": "office_presentations",
    "office-spreadsheets": "office_spreadsheets",
    "office-documents": "office_documents",
    "office-pdf": "office_pdf",
    "image-generation": "imagegen",
}


def add_check(checks: list[dict[str, Any]], label: str, ok: bool, evidence: Any) -> None:
    checks.append({
        "label": label,
        "status": "PASS" if ok else "FAIL",
        "evidence": evidence,
    })


def load_rows(root: pathlib.Path):
    sys.path.insert(0, str(root))
    from agent.skills.formatter import format_skills_for_prompt
    from agent.skills.manager import SkillManager
    from agent.skills.service import SkillService
    from agent.tools.tool_manager import ToolManager

    tmp = tempfile.TemporaryDirectory(prefix="ecorex-v024-native-facades-")
    manager = SkillManager(
        builtin_dir=str(root / "skills"),
        custom_dir=str(pathlib.Path(tmp.name) / "skills"),
        config={},
    )
    manager.extra_dirs = []
    manager.refresh_skills()
    rows = {row.get("name"): row for row in SkillService(manager).query()}
    prompts = {
        name: format_skills_for_prompt([manager.skills[name].skill])
        for name in EXPECTED_FACADES
        if name in manager.skills
    }
    tool_manager = ToolManager()
    tool_manager.tool_classes = {}
    tool_manager._mcp_tool_instances = {}
    tool_manager.load_tools(config_dict={"tongxin_cli": {"script_path": ""}})
    tool_infos = tool_manager.list_tools()
    tmp.cleanup()
    return rows, prompts, tool_infos


def _display_path(root: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except Exception:
        return path.name


def check_skill_doc(root: pathlib.Path, path: pathlib.Path, legacy_id: str, official_skill: str) -> tuple[bool, dict[str, Any]]:
    if not path.is_file():
        return False, {"path": _display_path(root, path), "reason": "missing"}
    text = path.read_text(encoding="utf-8", errors="replace")
    gates = "quality-gates:" in text
    return (
        f"compatibility-id: {legacy_id}" in text
        and f"adopts-official-skill: {official_skill}" in text
        and "ecorex-native-facade: true" in text
        and gates,
        {
            "path": _display_path(root, path),
            "compatibility": f"compatibility-id: {legacy_id}" in text,
            "official": f"adopts-official-skill: {official_skill}" in text,
            "nativeFacade": "ecorex-native-facade: true" in text,
            "qualityGates": gates,
        },
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    if not (root / "agent").is_dir() and (root.parent / "agent").is_dir():
        root = root.parent

    rows, prompts, tool_infos = load_rows(root)
    checks: list[dict[str, Any]] = []
    for legacy_id, official_skill in EXPECTED_FACADES.items():
        row = rows.get(legacy_id) or {}
        add_check(
            checks,
            f"{legacy_id} root facade API",
            row.get("compatibility_id") == legacy_id
            and row.get("adopts_official_skill") == official_skill
            and row.get("ecorex_native_facade") is True
            and bool(row.get("quality_gates")),
            {
                "compatibility_id": row.get("compatibility_id"),
                "adopts_official_skill": row.get("adopts_official_skill"),
                "ecorex_native_facade": row.get("ecorex_native_facade"),
                "quality_gates": row.get("quality_gates"),
            },
        )

        prompt = prompts.get(legacy_id, "")
        add_check(
            checks,
            f"{legacy_id} prompt facade fields",
            f"<compatibility_id>{legacy_id}</compatibility_id>" in prompt
            and f"<adopts_official_skill>{official_skill}</adopts_official_skill>" in prompt
            and "<ecorex_native_facade>true</ecorex_native_facade>" in prompt
            and "<quality_gates>" in prompt,
            {"promptLength": len(prompt)},
        )
        tool_name = EXPECTED_TOOLS[legacy_id]
        add_check(
            checks,
            f"{legacy_id} callable tool prompt bridge",
            f"<callable_tool>{tool_name}</callable_tool>" in prompt,
            {"toolName": tool_name, "promptLength": len(prompt)},
        )

        add_check(
            checks,
            f"{legacy_id} callable tool schema registered",
            tool_name in tool_infos,
            {
                "toolName": tool_name,
                "registered": tool_name in tool_infos,
                "toolCount": len(tool_infos),
            },
        )

        root_ok, root_doc = check_skill_doc(root, root / "skills" / legacy_id / "SKILL.md", legacy_id, official_skill)
        add_check(checks, f"{legacy_id} root skill doc facade metadata", root_ok, root_doc)

        runtime_path = root / "desktop" / "runtime" / "ecorex-runtime" / "skills" / legacy_id / "SKILL.md"
        if runtime_path.exists():
            staged_ok, staged_doc = check_skill_doc(root, runtime_path, legacy_id, official_skill)
            add_check(checks, f"{legacy_id} staged runtime skill doc facade metadata", staged_ok, staged_doc)

    failures = [item for item in checks if item["status"] != "PASS"]
    payload = {
        "status": "PASS" if not failures else "FAIL",
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "scope": "R24-01 native skill facade compatibility mapping",
        "checks": checks,
        "failed": failures,
        "redacted": True,
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
