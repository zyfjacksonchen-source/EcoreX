#!/usr/bin/env python3
"""Fail CI when EcoreX v1 UI bypasses the locked design system.

The v1 CSS is strict and the legacy monolith is forbidden from the production
tree. This gate therefore checks both token discipline and completed removal.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
TOKENS = ROOT / "desktop" / "src" / "styles" / "tokens.css"
V1_ROOT = ROOT / "desktop" / "src" / "v1"
LEGACY = ROOT / "desktop" / "src" / "styles" / "app.css"

DECLARATION_RE = re.compile(
    r"(?P<property>-{0,2}[a-zA-Z][a-zA-Z-]*)\s*:\s*(?P<value>[^;{}]+);"
)
VAR_USE_RE = re.compile(r"var\(\s*(--[a-zA-Z0-9_-]+)")
VAR_DEF_RE = re.compile(r"(--[a-zA-Z0-9_-]+)\s*:")
RAW_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|\boklch\(")
LAYOUT_TRANSITION_RE = re.compile(r"\b(width|height|padding|margin|top|right|bottom|left)\b")


def declarations(text: str):
    for match in DECLARATION_RE.finditer(text):
        yield match.group("property").strip(), match.group("value").strip()


def debt_counts(text: str) -> dict[str, int]:
    counts = {
        "raw_colors": len(RAW_COLOR_RE.findall(text)),
        "hardcoded_radii": 0,
        "hardcoded_shadows": 0,
        "numeric_z_index": 0,
        "transition_all": 0,
        "layout_transitions": 0,
    }
    for prop, value in declarations(text):
        lowered = value.casefold()
        if prop == "border-radius" and not lowered.startswith("var("):
            counts["hardcoded_radii"] += 1
        elif prop == "box-shadow" and not (
            lowered.startswith("var(") or lowered in {"none", "inherit", "initial"}
        ):
            counts["hardcoded_shadows"] += 1
        elif prop == "z-index" and re.fullmatch(r"-?\d+", lowered):
            counts["numeric_z_index"] += 1
        elif prop == "transition" or prop.startswith("transition-"):
            if re.search(r"(^|[\s,])all([\s,]|$)", lowered):
                counts["transition_all"] += 1
            if LAYOUT_TRANSITION_RE.search(lowered):
                counts["layout_transitions"] += 1
    return counts


def strict_css_findings(path: Path, text: str) -> list[str]:
    findings: list[str] = []
    allow_colors = path == TOKENS
    if not allow_colors:
        for match in RAW_COLOR_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{path.relative_to(ROOT)}:{line}: raw colour {match.group(0)!r}")
    elif re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", text):
        findings.append(f"{path.relative_to(ROOT)}: tokens must use OKLCH, not hex/rgb/hsl")

    for match in DECLARATION_RE.finditer(text):
        prop = match.group("property").strip()
        value = match.group("value").strip()
        lowered = value.casefold()
        line = text.count("\n", 0, match.start()) + 1
        location = f"{path.relative_to(ROOT)}:{line}"
        if prop == "border-radius" and not lowered.startswith("var("):
            findings.append(f"{location}: border-radius must use a shape token")
        if prop == "box-shadow" and not (
            lowered.startswith("var(") or lowered in {"none", "inherit", "initial"}
        ):
            findings.append(f"{location}: box-shadow must use an elevation token")
        if prop == "z-index" and lowered != "auto" and not lowered.startswith("var("):
            findings.append(f"{location}: z-index must use the six-level token scale")
        if prop == "transition" or prop.startswith("transition-"):
            if re.search(r"(^|[\s,])all([\s,]|$)", lowered):
                findings.append(f"{location}: transition: all is forbidden")
            if LAYOUT_TRANSITION_RE.search(lowered):
                findings.append(f"{location}: layout properties cannot be transitioned")
    return findings


def main() -> int:
    strict_files = [TOKENS, *sorted(V1_ROOT.rglob("*.css"))]
    findings: list[str] = []
    all_css = [TOKENS, *sorted(V1_ROOT.rglob("*.css"))]
    definitions: set[str] = set()
    uses: set[str] = set()
    for path in all_css:
        text = path.read_text(encoding="utf-8")
        definitions.update(VAR_DEF_RE.findall(text))
        uses.update(VAR_USE_RE.findall(text))
    runtime_properties = {
        "--context-meter-percent",
        "--preview-width",
        "--retouch-accent",
        "--retouch-selection-color",
        "--retouch-stage-aspect",
        "--retouch-stage-zoom",
        "--retouch-text-size",
    }
    undefined = sorted(uses - definitions - runtime_properties)
    if undefined:
        findings.append("undefined CSS variables: " + ", ".join(undefined))

    for path in strict_files:
        findings.extend(strict_css_findings(path, path.read_text(encoding="utf-8")))

    if LEGACY.exists():
        findings.append("desktop/src/styles/app.css must not exist in the v1 product tree")
        actual = debt_counts(LEGACY.read_text(encoding="utf-8"))
    else:
        actual = debt_counts("")

    if findings:
        print("EcoreX v1 design-system check failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "pass",
                "strict_files": len(strict_files),
                "legacy_counts": actual,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
