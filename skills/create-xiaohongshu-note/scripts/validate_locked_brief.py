#!/usr/bin/env python3
"""Validate a LockedBrief JSON file for create-xiaohongshu-note."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = [
    "brand",
    "product_or_service",
    "audience",
    "objective",
    "selected_topic",
    "reference_sources",
    "reference_links",
    "formula_decomposition",
    "must_include",
    "must_avoid",
    "tone",
    "cover_spec",
    "copy_spec",
    "delivery",
]

LIST_FIELDS = ["reference_sources", "reference_links", "must_include", "must_avoid"]
DICT_FIELDS = ["formula_decomposition", "cover_spec", "copy_spec", "delivery"]


def load_json(path: str | None) -> Any:
    if path in (None, "-"):
        return json.loads(sys.stdin.read().lstrip("\ufeff"))
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def validate(data: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in data:
            errors.append(f"missing required field: {field}")
        elif data[field] in ("", None):
            errors.append(f"empty required field: {field}")

    for field in LIST_FIELDS:
        if field in data and not isinstance(data[field], list):
            errors.append(f"{field} must be a list")
        elif field in data and not data[field]:
            warnings.append(f"{field} is empty")

    for field in DICT_FIELDS:
        if field in data and not isinstance(data[field], dict):
            errors.append(f"{field} must be an object")

    cover_spec = data.get("cover_spec", {})
    if isinstance(cover_spec, dict):
        ratio = str(cover_spec.get("ratio", "3:4"))
        if ratio not in ("3:4", "4:5", "1:1"):
            warnings.append(f"unusual cover ratio: {ratio}; Xiaohongshu default is 3:4")
        if not cover_spec.get("size"):
            warnings.append("cover_spec.size is missing; default to 1080x1440")

    copy_spec = data.get("copy_spec", {})
    if isinstance(copy_spec, dict):
        title_count = copy_spec.get("title_count", 5)
        try:
            title_count_int = int(title_count)
            if title_count_int < 3:
                warnings.append("copy_spec.title_count should usually be 3-5")
        except (TypeError, ValueError):
            errors.append("copy_spec.title_count must be numeric when provided")
        title_max = copy_spec.get("title_max_chars", 20)
        try:
            if int(title_max) > 20:
                warnings.append("copy_spec.title_max_chars should be <=20 for Xiaohongshu titles")
        except (TypeError, ValueError):
            errors.append("copy_spec.title_max_chars must be numeric when provided")

    formula = data.get("formula_decomposition", {})
    if isinstance(formula, dict):
        for key in ["source", "learning_schema_used", "selected_formula"]:
            if not formula.get(key):
                warnings.append(f"formula_decomposition.{key} is missing")

    delivery = data.get("delivery", {})
    if isinstance(delivery, dict) and not delivery.get("output_dir"):
        warnings.append("delivery.output_dir is empty; ask the user at runtime if not in a project")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("brief", nargs="?", default="-", help="LockedBrief JSON path, or - for stdin")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args()

    try:
        data = load_json(args.brief)
        if not isinstance(data, dict):
            raise ValueError("LockedBrief root must be a JSON object")
        result = validate(data)
    except Exception as exc:
        result = {"ok": False, "errors": [str(exc)], "warnings": []}

    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
