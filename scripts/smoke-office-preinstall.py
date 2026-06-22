#!/usr/bin/env python3
"""Verify EcoreX office skills and bundled Office/PDF capability preinstall config."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any


OFFICE_SKILLS = {
    "office-documents": [".doc", ".docx", "Word"],
    "office-spreadsheets": [".xlsx", ".csv", "Excel"],
    "office-presentations": [".pptx", "PowerPoint"],
    "office-pdf": [".pdf", "PDF"],
}
OFFICE_MODULES = ["pypdf", "pdfminer", "docx", "pptx", "openpyxl", "xlsxwriter", "markdownify"]


def add_check(checks: list[dict[str, Any]], name: str, ok: bool, evidence: str) -> None:
    checks.append({
        "name": name,
        "status": "pass" if ok else "fail",
        "evidence": evidence,
    })


def read_json(path: pathlib.Path) -> Any:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{path} has a UTF-8 BOM")
    return json.loads(raw.decode("utf-8"))


def find_runtime_python(runtime_dir: pathlib.Path) -> pathlib.Path | None:
    for candidate in (
        runtime_dir / "python" / "python.exe",
        runtime_dir / "python" / "bin" / "python3",
        runtime_dir / "python" / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    return None


def check_builtin_skills(root: pathlib.Path, checks: list[dict[str, Any]]) -> None:
    sys.path.insert(0, str(root))
    from agent.skills.manager import SkillManager
    from agent.skills.service import SkillService

    with tempfile.TemporaryDirectory(prefix="ecorex-office-skills-") as tmp:
        manager = SkillManager(
            builtin_dir=str(root / "skills"),
            custom_dir=str(pathlib.Path(tmp) / "skills"),
            config={},
        )
        manager.extra_dirs = []
        manager.refresh_skills()
        rows = {row.get("name"): row for row in SkillService(manager).query()}

    for name, terms in OFFICE_SKILLS.items():
        row = rows.get(name)
        add_check(checks, f"{name} builtin loaded", bool(row), str(row.get("path") if row else "missing"))
        if not row:
            continue
        add_check(
            checks,
            f"{name} mention metadata",
            row.get("source") == "builtin"
            and row.get("enabled") is not False
            and row.get("user_invocable") is True
            and row.get("mentionable") is True
            and row.get("mention_category") == "document",
            json.dumps({
                "source": row.get("source"),
                "enabled": row.get("enabled"),
                "user_invocable": row.get("user_invocable"),
                "mentionable": row.get("mentionable"),
                "mention_category": row.get("mention_category"),
            }, ensure_ascii=False, sort_keys=True),
        )
        description = str(row.get("description") or "")
        missing_terms = [term for term in terms if term.lower() not in description.lower()]
        add_check(checks, f"{name} office routing terms", not missing_terms, f"missing={missing_terms}")


def check_capability_manifest(root: pathlib.Path, checks: list[dict[str, Any]]) -> None:
    manifest_path = root / "desktop" / "runtime-packs" / "capabilities.json"
    manifest = read_json(manifest_path)
    packs = {str(pack.get("id")): pack for pack in manifest.get("packs") or [] if isinstance(pack, dict)}
    pack = packs.get("office-pdf")
    add_check(checks, "office-pdf capability manifest", bool(pack), str(manifest_path))
    if not pack:
        return
    module_checks = set(str(item) for item in pack.get("moduleChecks") or [])
    missing_modules = sorted(set(OFFICE_MODULES) - module_checks)
    add_check(
        checks,
        "office-pdf capability modules",
        not missing_modules and pack.get("discoveryOnly") is not True,
        f"missing={missing_modules} discoveryOnly={pack.get('discoveryOnly')}",
    )


def check_stage_defaults(root: pathlib.Path, checks: list[dict[str, Any]]) -> None:
    win_text = (root / "desktop" / "scripts" / "stage-runtime-win.ps1").read_text(encoding="utf-8")
    mac_text = (root / "desktop" / "scripts" / "stage-runtime-mac.sh").read_text(encoding="utf-8")
    add_check(checks, "Windows staging defaults office-pdf", '@("office-pdf")' in win_text and "ECOREX_PREINSTALL_PACKS" in win_text, "stage-runtime-win.ps1")
    add_check(checks, "macOS staging defaults office-pdf", 'ECOREX_PREINSTALL_PACKS:-office-pdf' in mac_text and '"preinstalledPacks"' in mac_text, "stage-runtime-mac.sh")


def check_staged_runtime(runtime_dir: pathlib.Path, checks: list[dict[str, Any]], require_modules: bool) -> None:
    for name in OFFICE_SKILLS:
        path = runtime_dir / "skills" / name / "SKILL.md"
        add_check(checks, f"{name} staged runtime skill", path.is_file(), str(path))

    runtime_manifest_path = runtime_dir / "runtime-manifest.json"
    runtime_manifest = read_json(runtime_manifest_path)
    preinstalled = set(str(item) for item in runtime_manifest.get("preinstalledPacks") or [])
    add_check(checks, "runtime manifest preinstalled office-pdf", "office-pdf" in preinstalled, json.dumps(sorted(preinstalled)))

    state_path = runtime_dir / "capability-state" / "office-pdf.json"
    if state_path.exists():
        state = read_json(state_path)
        state_ok = state.get("state") == "installed" and state.get("installed") is True
        state_evidence = json.dumps({"state": state.get("state"), "installed": state.get("installed"), "message": state.get("message")}, ensure_ascii=False)
    else:
        state_ok = "office-pdf" in preinstalled
        state_evidence = f"sanitized release runtime has no capability-state file; runtime-manifest preinstalledPacks={sorted(preinstalled)}"
    add_check(checks, "office-pdf capability preinstall recorded", state_ok, state_evidence)

    if not require_modules:
        return
    python = find_runtime_python(runtime_dir)
    if not python:
        add_check(checks, "office-pdf runtime module imports", False, "runtime python not found")
        return
    script = (
        "import importlib.util, json; "
        f"mods={OFFICE_MODULES!r}; "
        "missing=[m for m in mods if importlib.util.find_spec(m) is None]; "
        "print(json.dumps({'missing': missing})); "
        "raise SystemExit(1 if missing else 0)"
    )
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    result = subprocess.run([str(python), "-c", script], text=True, capture_output=True, env=env, timeout=30)
    evidence = (result.stdout or result.stderr or "").strip()
    add_check(checks, "office-pdf runtime module imports", result.returncode == 0, evidence)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--version", default="0.1.18")
    parser.add_argument("--output", default="")
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--require-staged-runtime", action="store_true")
    parser.add_argument("--skip-module-imports", action="store_true")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    if not (root / "agent").is_dir() and (root.parent / "agent").is_dir():
        root = root.parent
    checks: list[dict[str, Any]] = []
    check_builtin_skills(root, checks)
    check_capability_manifest(root, checks)
    check_stage_defaults(root, checks)

    runtime_dir = pathlib.Path(args.runtime_dir).resolve() if args.runtime_dir else root / "desktop" / "runtime" / "ecorex-runtime"
    if args.require_staged_runtime or runtime_dir.exists():
        check_staged_runtime(runtime_dir, checks, require_modules=not args.skip_module_imports)
    else:
        add_check(checks, "staged runtime optional", True, f"not present: {runtime_dir}")

    failures = [item for item in checks if item["status"] != "pass"]
    payload = {
        "status": "pass" if not failures else "fail",
        "version": args.version,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "changeIds": ["OFFICE-001"],
        "checks": checks,
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
