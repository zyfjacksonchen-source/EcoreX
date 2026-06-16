#!/usr/bin/env python3
"""Compact and score Feishu Base records for Xiaohongshu reference research.

The goal is to make Base pagination converge. The script selects a small,
reviewable list of useful records and tells the agent whether another page is
worth reading.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


LEARNING_KEYS = (
    "learning",
    "学习",
    "学习逻辑",
    "拆解",
    "公式",
    "结构",
    "逻辑",
    "亮点",
    "封面",
    "内页",
    "标题",
    "正文",
    "链接",
    "url",
    "URL",
)

TIME_KEYS = (
    "日期",
    "发布时间",
    "发布日",
    "笔记时间",
    "时间",
    "created_time",
    "update_time",
    "产出时间",
    "ID",
    "序号",
)

STOP_KEYWORDS = {
    "小红书",
    "小红书笔记",
    "笔记",
    "用户",
    "客户",
    "生成",
    "参考",
    "内容",
    "标题",
    "封面",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_brief(value: str) -> str:
    if not value:
        return ""
    if "\n" not in value and len(value) < 240:
        path = Path(value)
        if path.exists():
            return path.read_text(encoding="utf-8", errors="ignore")
    return value


def find_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    row_data = payload.get("data")
    fields = payload.get("fields")
    if isinstance(row_data, list) and isinstance(fields, list):
        record_ids = payload.get("record_id_list")
        records: list[dict[str, Any]] = []
        for index, row in enumerate(row_data):
            if not isinstance(row, list):
                continue
            mapped = {str(fields[i]): row[i] for i in range(min(len(fields), len(row)))}
            record_id_value = ""
            if isinstance(record_ids, list) and index < len(record_ids):
                record_id_value = str(record_ids[index])
            records.append({"record_id": record_id_value, "fields": mapped})
        return records
    for key in ("items", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    data = payload.get("data")
    if isinstance(data, dict):
        nested = find_records(data)
        if nested:
            return nested
    return []


def flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " ".join(flatten_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{key} {flatten_text(item)}" for key, item in value.items())
    return str(value)


def fields_of(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields")
    if isinstance(fields, dict):
        return fields
    return record


def record_id(record: dict[str, Any]) -> str:
    for key in ("record_id", "id", "recordId"):
        value = record.get(key)
        if value:
            return str(value)
    fields = fields_of(record)
    for key in ("ID", "序号"):
        value = fields.get(key)
        if value:
            return flatten_text(value)[:80]
    return ""


def extract_keywords(brief: str) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", brief):
        token = token.strip().lower()
        if token in STOP_KEYWORDS or "小红书" in token:
            continue
        if token and token not in tokens:
            tokens.append(token)
    return tokens[:80]


def best_field(fields: dict[str, Any], names: tuple[str, ...]) -> str:
    for key, value in fields.items():
        if any(name.lower() in str(key).lower() for name in names):
            text = flatten_text(value).strip()
            if text:
                return text[:500]
    return ""


def compact_record(record: dict[str, Any], score: int, matched: list[str]) -> dict[str, Any]:
    fields = fields_of(record)
    title = best_field(fields, ("标题", "title", "名称", "name")) or flatten_text(fields)[:80]
    url = best_field(fields, ("链接", "url", "URL", "link"))
    learning = best_field(fields, LEARNING_KEYS)
    time_value = best_field(fields, TIME_KEYS)
    cover = best_field(fields, ("封面", "cover"))
    category = best_field(fields, ("行业", "类目", "品类", "场景", "人群", "category", "audience"))
    return {
        "record_id": record_id(record),
        "score": score,
        "matched_keywords": matched[:12],
        "title": title,
        "url": url,
        "time_or_order": time_value,
        "category_or_scene": category,
        "learning_or_mechanic": learning[:1200],
        "cover_or_asset": cover[:400],
    }


def score_record(record: dict[str, Any], keywords: list[str]) -> tuple[int, list[str]]:
    fields = fields_of(record)
    text = flatten_text(fields).lower()
    matched = [keyword for keyword in keywords if keyword in text]
    score = len(matched) * 5
    for key in fields:
        lowered = str(key).lower()
        if any(signal.lower() in lowered for signal in LEARNING_KEYS):
            if flatten_text(fields.get(key)).strip():
                score += 4
        if any(signal.lower() in lowered for signal in TIME_KEYS):
            if flatten_text(fields.get(key)).strip():
                score += 2
    if re.search(r"https?://|xhslink|xiaohongshu", text):
        score += 3
    if re.search(r"封面|内页|cover|image|图片", text):
        score += 2
    if not keywords and score == 0 and text.strip():
        score = 1
    return score, matched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Feishu record-list JSON file")
    parser.add_argument("--brief", default="", help="Brief text or path to a brief/keywords file")
    parser.add_argument("--output", required=True, help="Selected compact JSON output")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--min", type=int, default=5)
    parser.add_argument("--page-count", type=int, default=1, help="How many pages have been read into this input")
    parser.add_argument("--max-pages", type=int, default=3, help="Hard pagination stop")
    args = parser.parse_args()

    payload = load_json(Path(args.input))
    brief = load_brief(args.brief)
    keywords = extract_keywords(brief)
    scored = []
    for record in find_records(payload):
        score, matched = score_record(record, keywords)
        if score > 0:
            scored.append((score, matched, record))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = [compact_record(record, score, matched) for score, matched, record in scored[: max(1, args.limit)]]
    covered_keywords = sorted({keyword for item in selected for keyword in item.get("matched_keywords", [])})
    needed_coverage = min(2, len(keywords))
    coverage_ok = not keywords or len(covered_keywords) >= needed_coverage
    enough_records = len(selected) >= args.min
    hit_page_cap = args.page_count >= args.max_pages
    should_continue = (not enough_records or not coverage_ok) and not hit_page_cap
    if not enough_records:
        reason = "fewer than minimum useful records"
    elif not coverage_ok:
        reason = "selected records exist but keyword coverage is weak; read at most one more page unless max pages is reached"
    elif hit_page_cap:
        reason = "max page cap reached; stop paging and report coverage gaps"
    else:
        reason = "enough useful records selected; stop paging unless the brief requires missing coverage"
    result = {
        "selected_count": len(selected),
        "should_continue_pagination": should_continue,
        "reason": reason,
        "keywords_used": keywords,
        "covered_keywords": covered_keywords,
        "page_count": args.page_count,
        "max_pages": args.max_pages,
        "selected": selected,
    }
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"selected_count": len(selected), "should_continue_pagination": should_continue}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
