#!/usr/bin/env python3
"""Release gate for R23-20 session artifacts.

The scanner reports pattern names and counts only. It intentionally does not
echo matched text, paths, prompts, or identifiers back into the report.
"""

from __future__ import annotations

import argparse
import glob
import hmac
import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DENYLIST: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("windows_path", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(?:\\\\+|/)")),
    ("user_home_path", re.compile(r"(/Users/|/home/|\\\\Users\\\\)", re.IGNORECASE)),
    ("temp_path", re.compile(r"(/tmp/|\\\\Temp\\\\|\\\\RWTemp\\\\)", re.IGNORECASE)),
    ("email", re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)),
    ("api_token", re.compile(r"(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})")),
    ("credential_word", re.compile(r"\b(authorization|cookie|credential|secret|token)\b", re.IGNORECASE)),
    ("feishu_app_id", re.compile(r"\bcli_[A-Za-z0-9_-]{8,}\b")),
    ("feishu_open_id", re.compile(r"\bou_[A-Za-z0-9_-]{8,}\b")),
    ("feishu_chat_id", re.compile(r"\boc_[A-Za-z0-9_-]{8,}\b")),
    ("feishu_message_id", re.compile(r"\bom_[A-Za-z0-9_-]{8,}\b")),
    ("feishu_tenant_token", re.compile(r"\btenant_access_token\b", re.IGNORECASE)),
    ("feishu_qr_url", re.compile(r"(?i)(qrcode_url|qr_url|verification_uri|verification_url|https://open\.feishu\.cn/[^\s\"')<>]+)")),
    ("feishu_qr_image", re.compile(r"(?i)data:image/(?:png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=]{32,}")),
    ("raw_session_id_key", re.compile(r'"session_id"\s*:')),
    ("raw_session_id_camel_key", re.compile(r'"sessionId"\s*:')),
    ("raw_request_id_key", re.compile(r'"request_id"\s*:')),
    ("raw_request_id_camel_key", re.compile(r'"requestId"\s*:')),
    ("raw_project_id_key", re.compile(r'"project_id"\s*:')),
    ("raw_project_id_camel_key", re.compile(r'"projectId"\s*:')),
    ("raw_project_path_key", re.compile(r'"projectPath"\s*:|"project_path"\s*:')),
    ("raw_memory_path_key", re.compile(r'"memoryPath"\s*:|"memory_path"\s*:')),
    ("raw_dreams_path_key", re.compile(r'"dreamsPath"\s*:|"dreams_path"\s*:')),
    ("raw_file_name_key", re.compile(r'"fileName"\s*:|"file_name"\s*:')),
    ("raw_file_path_key", re.compile(r'"filePath"\s*:|"file_path"\s*:')),
    ("raw_local_storage_dump", re.compile(r"\blocalStorage\b")),
    ("raw_message_body_key", re.compile(r'"messages"\s*:|"prompt"\s*:|"content"\s*:')),
    ("raw_tool_result", re.compile(r"\btool_result\b", re.IGNORECASE)),
)


def _salt(args: argparse.Namespace) -> bytes:
    value = args.salt or os.environ.get("ECOREX_ARTIFACT_SCAN_HMAC_SALT") or secrets.token_hex(32)
    return str(value).encode("utf-8", errors="replace")


def _artifact_hash(path: Path, salt: bytes) -> str:
    digest = hmac.new(salt, str(path.resolve()).encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()
    return f"hmac:{digest[:16]}"


def _expand_inputs(patterns: Iterable[str]) -> List[Path]:
    paths: List[Path] = []
    seen = set()
    for item in patterns:
        matches = glob.glob(item)
        if not matches:
            matches = [item]
        for match in matches:
            path = Path(match)
            if not path.exists() or not path.is_file():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    return paths


def _scan_text(text: str) -> Dict[str, int]:
    findings: Dict[str, int] = {}
    for name, pattern in DENYLIST:
        count = len(pattern.findall(text))
        if count:
            findings[name] = count
    return findings


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _ocr_image_text(path: Path, timeout: float) -> Tuple[str, Dict[str, Any]]:
    try:
        from agent.tools.ocr.ocr import _local_ocr  # type: ignore

        payload = _local_ocr(path.read_bytes(), timeout)
    except Exception as exc:
        return "", {
            "status": "error",
            "provider": "local",
            "errorType": exc.__class__.__name__,
            "redacted": True,
        }
    status = str(payload.get("status") or "").strip().lower()
    return str(payload.get("text") or ""), {
        "status": status,
        "provider": str(payload.get("provider") or ""),
        "latencyMs": int(payload.get("latencyMs") or 0),
        "cacheHit": bool(payload.get("cacheHit")),
        "redacted": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan R23-20 artifacts for privacy blockers")
    parser.add_argument("artifacts", nargs="+", help="Artifact paths or glob patterns")
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument("--salt", default="")
    parser.add_argument("--ocr-images", action="store_true", help="OCR image artifacts and scan recognized text.")
    parser.add_argument("--image-timeout", type=float, default=2.0, help="Per-image OCR timeout in seconds.")
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    salt = _salt(args)
    paths = _expand_inputs(args.artifacts)
    if not paths:
        payload = {
            "status": "failed",
            "filesScanned": 0,
            "findingCount": 0,
            "findings": [],
            "inputError": "no_artifacts_scanned",
            "redacted": True,
        }
        if args.json_output:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 1
    findings: List[Dict[str, Any]] = []
    image_ocr_scanned = 0
    image_ocr_unavailable = 0
    image_ocr_errors = 0
    for path in paths:
        is_image = path.suffix.lower() in IMAGE_SUFFIXES
        if is_image:
            if not args.ocr_images:
                continue
            text, ocr_meta = _ocr_image_text(path, max(0.5, min(float(args.image_timeout or 2.0), 8.0)))
            status = str(ocr_meta.get("status") or "")
            if status == "unavailable":
                image_ocr_unavailable += 1
            elif status == "error":
                image_ocr_errors += 1
            else:
                image_ocr_scanned += 1
        else:
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except Exception:
                continue
        counts = _scan_text(text)
        for pattern, count in sorted(counts.items()):
            findings.append({
                "artifactHash": _artifact_hash(path, salt),
                "pattern": pattern,
                "count": count,
            })

    failed = bool(findings or image_ocr_unavailable or image_ocr_errors)
    payload = {
        "status": "failed" if failed else "success",
        "filesScanned": len(paths),
        "findingCount": len(findings),
        "imageOcrScannedCount": image_ocr_scanned,
        "imageOcrUnavailableCount": image_ocr_unavailable,
        "imageOcrErrorCount": image_ocr_errors,
        "findings": findings,
        "redacted": True,
    }
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
