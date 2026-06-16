#!/usr/bin/env python3
"""Validate a Xiaohongshu note pack for create-xiaohongshu-note."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REQUIRED = [
    "formula_decomposition",
    "reference_links",
    "cover_design",
    "titles",
    "selected_title",
    "body",
    "tags",
    "first_comment",
    "audit_check",
]


def load_json(path: str | None) -> Any:
    if path in (None, "-"):
        return json.loads(sys.stdin.read().lstrip("\ufeff"))
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def char_len(text: Any) -> int:
    return len(str(text).strip())


def has_emoji_or_marker(text: str) -> bool:
    markers = {"✅", "❌", "⚠", "👉", "📩", "💬", "🏠", "✨", "🔥", "📍", "🆓", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"}
    if any(marker in text for marker in markers):
        return True
    return any(ord(ch) > 0xFFFF for ch in text)


def path_exists(path: Any) -> bool:
    value = str(path or "").strip()
    return bool(value) and Path(value).exists()


def validate(data: dict[str, Any], final: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    for field in REQUIRED:
        if field not in data:
            errors.append(f"missing required field: {field}")
        elif data[field] in ("", None):
            errors.append(f"empty required field: {field}")

    titles = data.get("titles", [])
    if not isinstance(titles, list) or not titles:
        errors.append("titles must be a non-empty list")
        titles = []
    for title in titles:
        if char_len(title) > 20:
            errors.append(f"title exceeds 20 chars: {title}")

    selected = data.get("selected_title", "")
    if char_len(selected) > 20:
        errors.append(f"selected_title exceeds 20 chars: {selected}")

    references = data.get("reference_links", [])
    if not isinstance(references, list) or not references:
        errors.append("reference_links must be a non-empty list")

    body = str(data.get("body", ""))
    if not has_emoji_or_marker(body):
        warnings.append("body has no obvious emoji/sign markers; confirm Xiaohongshu-native rhythm")

    cover_design = data.get("cover_design", {})
    if not isinstance(cover_design, dict):
        errors.append("cover_design must be an object")
        cover_design = {}
    status = str(cover_design.get("final_cover_status", ""))
    if status not in {"produced", "blocked", "not_yet_generated"}:
        errors.append("cover_design.final_cover_status must be produced, blocked, or not_yet_generated")
    final_cover_path = cover_design.get("final_cover_path") or data.get("cover", {}).get("final_image_path", "")
    if status in {"blocked", "not_yet_generated"} and final_cover_path:
        warnings.append("final_cover_status is not produced but a final_cover_path/final_image_path is set")
    if final:
        if status != "produced":
            errors.append("final package requires cover_design.final_cover_status=produced")
        if not path_exists(final_cover_path):
            errors.append("final package requires an existing final cover image path")

    audit = data.get("audit_check", {})
    if not isinstance(audit, dict):
        errors.append("audit_check must be an object")
        audit = {}
    for key in [
        "final_cover_produced",
        "inner_pages_produced",
        "carousel_requested",
        "reference_similarity_under_50_percent",
        "title_length_check",
        "native_copy_check",
    ]:
        if key not in audit:
            errors.append(f"audit_check.{key} is required")

    carousel = data.get("carousel", {})
    carousel_requested = bool(carousel.get("requested")) if isinstance(carousel, dict) else bool(audit.get("carousel_requested"))
    inner_pages = data.get("inner_pages", [])
    if final and carousel_requested:
        if not isinstance(inner_pages, list) or not inner_pages:
            errors.append("carousel was requested but inner_pages is empty")
        else:
            for page in inner_pages:
                if not isinstance(page, dict):
                    errors.append("inner_pages entries must be objects")
                    continue
                if str(page.get("status", "")) != "produced":
                    errors.append(f"inner page {page.get('page', '?')} is not produced")
                if not path_exists(page.get("image_path", "")):
                    errors.append(f"inner page {page.get('page', '?')} image_path does not exist")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("note_pack", nargs="?", default="-")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--final", action="store_true", help="enforce final delivery requirements including produced cover image")
    args = parser.parse_args()

    try:
        data = load_json(args.note_pack)
        if not isinstance(data, dict):
            raise ValueError("note pack root must be a JSON object")
        result = validate(data, final=args.final)
    except Exception as exc:
        result = {"ok": False, "errors": [str(exc)], "warnings": []}

    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
