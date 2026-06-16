#!/usr/bin/env python3
"""Search Xiaohongshu reference notes with opencli before topic selection."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def load_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("input must be a JSON object")
    return data


def clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def split_terms(text: str) -> list[str]:
    parts = re.split(r"[，,、/|;；\n]+", text)
    return [p.strip() for p in parts if p.strip()]


def unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        item = clean(item)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def derive_keywords(data: dict[str, Any], max_keywords: int) -> list[str]:
    product = clean(data.get("product_or_service"))
    audience = clean(data.get("audience"))
    objective = clean(data.get("objective"))
    topic = clean(data.get("selected_topic"))
    tone = clean(data.get("tone"))
    must_include = [clean(x) for x in data.get("must_include", []) if clean(x)] if isinstance(data.get("must_include"), list) else []

    candidates: list[str] = []
    if topic:
        candidates.append(topic)
    if product and audience:
        candidates.append(f"{product} {audience}")
    if product:
        candidates.append(product)
        candidates.append(f"{product} 怎么选")
        candidates.append(f"{product} 真实体验")
    for term in must_include[:4]:
        if product:
            candidates.append(f"{product} {term}")
        else:
            candidates.append(term)
    for term in split_terms(audience + " " + objective + " " + tone):
        if product and len(term) >= 2:
            candidates.append(f"{product} {term}")

    fallback = ["小红书 参考笔记", "小红书 标题 封面", "小红书 用户体验 笔记"]
    candidates.extend(fallback)
    return unique(candidates)[:max_keywords]


def parse_results(stdout: str) -> list[dict[str, Any]]:
    text = stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return [{"raw": line} for line in text.splitlines() if line.strip()]

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("data", "items", "results", "rows"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            items = [data]
    else:
        return [{"raw": text}]

    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            normalized.append({"raw": item})
            continue
        normalized.append(
            {
                "rank": item.get("rank"),
                "title": item.get("title"),
                "author": item.get("author"),
                "likes": item.get("likes"),
                "published_at": item.get("published_at"),
                "url": item.get("url"),
                "author_url": item.get("author_url"),
            }
        )
    return normalized


def run_search(opencli: str, keyword: str, limit: int, dry_run: bool) -> dict[str, Any]:
    cmd = [
        opencli,
        "xiaohongshu",
        "search",
        keyword,
        "--limit",
        str(limit),
        "-f",
        "json",
        "--window",
        "background",
        "--site-session",
        "persistent",
    ]
    if dry_run:
        return {"keyword": keyword, "ok": True, "dry_run": True, "cmd": cmd, "results": []}
    proc = subprocess.run(cmd, text=True, capture_output=True)
    return {
        "keyword": keyword,
        "ok": proc.returncode == 0,
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "results": parse_results(proc.stdout) if proc.returncode == 0 else [],
    }


def summarize(searches: list[dict[str, Any]], max_notes: int) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for search in searches:
        for item in search.get("results", []):
            url = clean(item.get("url") if isinstance(item, dict) else "")
            title = clean(item.get("title") if isinstance(item, dict) else item)
            key = url or title
            if not key or key in seen:
                continue
            seen.add(key)
            note = dict(item)
            note["source_keyword"] = search.get("keyword")
            notes.append(note)
            if len(notes) >= max_notes:
                return notes
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="Brief/context JSON used to derive search keywords")
    parser.add_argument("--keyword", action="append", default=[], help="Explicit keyword; repeatable")
    parser.add_argument("--max-keywords", type=int, default=5)
    parser.add_argument("--limit", type=int, default=8, help="Results per keyword")
    parser.add_argument("--max-notes", type=int, default=12, help="Notes kept in summary")
    parser.add_argument("--output", help="Output JSON path")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        data = load_json(args.input)
        keywords = unique(args.keyword) or derive_keywords(data, args.max_keywords)
        opencli = shutil.which("opencli")
        if not opencli:
            result = {
                "ok": False,
                "status": "opencli_missing",
                "keywords": keywords,
                "errors": ["opencli was not found on PATH"],
                "searches": [],
                "reference_notes": [],
            }
        else:
            searches = [run_search(opencli, keyword, args.limit, args.dry_run) for keyword in keywords]
            result = {
                "ok": all(s.get("ok") for s in searches),
                "status": "dry_run" if args.dry_run else "completed",
                "keywords": keywords,
                "searches": searches,
                "reference_notes": summarize(searches, args.max_notes),
            }
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
