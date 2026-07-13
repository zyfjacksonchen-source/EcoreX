from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.extensions.registry import ExtensionRegistry  # noqa: E402
from agent.skills.manager import SkillManager  # noqa: E402
from agent.skills.service import SkillService, _decorate_mention_metadata  # noqa: E402


def normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def skill_name_from_extension(extension: Dict[str, Any]) -> str:
    extension_id = str(extension.get("id") or "")
    return extension_id[len("skill:") :] if extension_id.startswith("skill:") else ""


def is_lark_or_feishu_skill(row: Dict[str, Any]) -> bool:
    name = normalize(row.get("name") or row.get("display_name") or row.get("displayName"))
    path = normalize(row.get("path") or row.get("sourcePath"))
    primary_env = normalize(row.get("primary_env"))
    description = normalize(row.get("description"))
    source = normalize(f"{row.get('source') or ''} {row.get('origin') or ''}")
    return bool(
        re.match(r"^(lark|feishu)([-_:]|$)", name)
        or re.search(r"(^|[\\/])(lark|feishu)-[^\\/]+[\\/]skill\.md$", path)
        or primary_env.startswith(("lark_", "feishu_"))
        or "lark-cli" in description
        or "lark-cli" in source
        or ("飞书" in description and "cli" in description)
        or ("飞书" in source and "cli" in source)
    )


def mention_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "mentionable": bool(row.get("mentionable")),
        "mention_category": row.get("mention_category") or "",
        "mention_hidden_reason": row.get("mention_hidden_reason") or "",
    }


def synthetic_cases() -> List[Dict[str, Any]]:
    cases = [
        {
            "name": "explicit-background",
            "mentionable": False,
            "mention_category": "automation",
            "mention_hidden_reason": "admin-hidden",
            "description": "A background-only workflow",
        },
        {
            "name": "frontmatter-document",
            "category": "documents",
            "description": "Create docx and pdf files",
        },
        {
            "name": "lark-approval",
            "path": r"C:\Users\user\.agents\skills\lark-approval\SKILL.md",
            "description": "飞书审批 API",
        },
        {
            "name": "primary-env-only",
            "primary_env": "LARK_APP_ID",
            "description": "Internal helper",
        },
        {
            "name": "good-skill-minimal",
            "path": r"C:\repo\skill-format-check\tests\good-skill-minimal\SKILL.md",
            "description": "Test fixture",
        },
    ]
    for row in cases:
        _decorate_mention_metadata(row)
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate skill @ mention classification metadata.")
    parser.add_argument("--workspace", default="", help="Workspace root. Defaults to SkillManager's configured custom dir parent.")
    parser.add_argument("--output", default="", help="Optional JSON evidence output path.")
    args = parser.parse_args()

    if args.workspace:
        workspace = Path(args.workspace).expanduser().resolve()
        manager = SkillManager(custom_dir=str(workspace / "skills"))
    else:
        manager = SkillManager()
        workspace = Path(manager.custom_dir).resolve().parent

    skills = SkillService(manager).query()
    extensions_payload = ExtensionRegistry(str(workspace)).list_extensions()
    extensions = extensions_payload.get("extensions", []) if isinstance(extensions_payload, dict) else []

    skills_by_name = {str(row.get("name") or row.get("display_name") or ""): row for row in skills}
    extension_skill_rows = [
        row for row in extensions
        if row.get("type") in {"builtin_skill", "user_skill"} and skill_name_from_extension(row)
    ]

    failures: List[Dict[str, Any]] = []
    lark_skill_rows = [row for row in skills if is_lark_or_feishu_skill(row)]
    lark_extension_rows = [row for row in extension_skill_rows if is_lark_or_feishu_skill(row)]

    for row in lark_skill_rows:
        if row.get("mentionable") is not False or row.get("mention_category") != "background":
            failures.append({"type": "lark-skill-visible", "name": row.get("name"), "fields": mention_fields(row)})

    for row in lark_extension_rows:
        if row.get("mentionable") is not False or row.get("mention_category") != "background":
            failures.append({"type": "lark-extension-visible", "name": skill_name_from_extension(row), "fields": mention_fields(row)})

    parity_checked = 0
    for extension in extension_skill_rows:
        name = skill_name_from_extension(extension)
        skill = skills_by_name.get(name)
        if not skill:
            continue
        parity_checked += 1
        if mention_fields(skill) != mention_fields(extension):
            failures.append({
                "type": "skills-extensions-parity",
                "name": name,
                "skills": mention_fields(skill),
                "extensions": mention_fields(extension),
            })

    synthetic = synthetic_cases()
    synthetic_expectations = {
        "explicit-background": {"mentionable": False, "mention_category": "background", "mention_hidden_reason": "admin-hidden"},
        "frontmatter-document": {"mentionable": True, "mention_category": "document"},
        "lark-approval": {"mentionable": False, "mention_category": "background", "mention_hidden_reason": "lark-cli-triggered"},
        "primary-env-only": {"mentionable": False, "mention_category": "background", "mention_hidden_reason": "lark-cli-triggered"},
        "good-skill-minimal": {"mentionable": False, "mention_category": "background", "mention_hidden_reason": "test-fixture"},
    }
    for row in synthetic:
        expected = synthetic_expectations[row["name"]]
        actual = mention_fields(row)
        for key, value in expected.items():
            if actual.get(key) != value:
                failures.append({"type": "synthetic-case", "name": row["name"], "expected": expected, "actual": actual})
                break

    evidence = {
        "status": "fail" if failures else "pass",
        "workspace": str(workspace),
        "skillsCount": len(skills),
        "extensionsCount": len(extensions),
        "extensionSkillCount": len(extension_skill_rows),
        "parityChecked": parity_checked,
        "larkSkillCount": len(lark_skill_rows),
        "larkExtensionCount": len(lark_extension_rows),
        "larkSkillExamples": [
            {
                "name": row.get("name") or row.get("display_name"),
                "path": row.get("path"),
                **mention_fields(row),
            }
            for row in lark_skill_rows[:10]
        ],
        "syntheticCases": [
            {
                "name": row["name"],
                **mention_fields(row),
            }
            for row in synthetic
        ],
        "failures": failures,
    }

    output = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
