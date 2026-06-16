#!/usr/bin/env python3
"""Smoke-test an OpenAI Images API model without printing secrets."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _error_from_http(exc: urllib.error.HTTPError) -> dict:
    try:
        body = exc.read().decode("utf-8", errors="replace")
        data = json.loads(body)
    except Exception:
        return {"status": exc.code, "error": {"message": exc.reason}}
    error = data.get("error") if isinstance(data, dict) else None
    return {
        "status": exc.code,
        "error": error if isinstance(error, dict) else {"message": body[:500]},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-image-2-pro")
    parser.add_argument("--fallback-model", default="")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--quality", default="low")
    parser.add_argument("--output-format", default="png", choices=["png", "jpeg", "webp"])
    parser.add_argument("--prompt", default="A minimal orange letter X icon on a clean dark background.")
    parser.add_argument("--output-dir", default="release-artifacts/image-smoke")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print(json.dumps({"ok": False, "status": "missing_key", "message": "OPENAI_API_KEY is not set"}))
        return 2

    api_base = (os.environ.get("OPENAI_API_BASE") or "https://api.openai.com/v1").rstrip("/")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    models = [args.model] + ([args.fallback_model] if args.fallback_model else [])
    attempts = []

    for model in models:
        payload = {
            "model": model,
            "prompt": args.prompt,
            "n": 1,
            "size": args.size,
            "quality": args.quality,
            "output_format": args.output_format,
            "moderation": "auto",
        }
        request = urllib.request.Request(
            f"{api_base}/images/generations",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            item = (data.get("data") or [{}])[0]
            b64 = item.get("b64_json")
            if not b64:
                raise RuntimeError("response did not contain data[0].b64_json")
            out_path = output_dir / f"{model.replace('/', '_')}.{args.output_format}"
            out_path.write_bytes(base64.b64decode(b64))
            print(json.dumps({
                "ok": True,
                "model": model,
                "elapsed_seconds": round(time.time() - started, 2),
                "output": str(out_path.resolve()),
                "usage": data.get("usage"),
                "attempts": attempts,
            }, ensure_ascii=False))
            return 0
        except urllib.error.HTTPError as exc:
            detail = _error_from_http(exc)
            attempts.append({
                "model": model,
                "elapsed_seconds": round(time.time() - started, 2),
                "status": detail.get("status"),
                "code": (detail.get("error") or {}).get("code"),
                "message": (detail.get("error") or {}).get("message"),
            })
        except Exception as exc:
            attempts.append({
                "model": model,
                "elapsed_seconds": round(time.time() - started, 2),
                "status": "error",
                "message": str(exc),
            })

    print(json.dumps({"ok": False, "attempts": attempts}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
