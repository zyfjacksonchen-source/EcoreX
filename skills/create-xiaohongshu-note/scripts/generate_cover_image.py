#!/usr/bin/env python3
"""Generate a final Xiaohongshu cover or carousel image with OpenAI, using status files and prompt-hash caching."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def load_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8-sig")
    if args.prompt:
        return args.prompt
    raise ValueError("provide --prompt or --prompt-file")


def prompt_hash(prompt: str, size: str) -> str:
    return hashlib.sha256((prompt + "\n" + size).encode("utf-8")).hexdigest()[:16]


def write_status(path: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def decode_response_image(response: Any) -> bytes:
    data = getattr(response, "data", None) or response.get("data")
    if not data:
        raise RuntimeError("OpenAI response did not contain image data")
    first = data[0]
    b64_json = getattr(first, "b64_json", None)
    url = getattr(first, "url", None)
    if isinstance(first, dict):
        b64_json = first.get("b64_json", b64_json)
        url = first.get("url", url)
    if b64_json:
        return base64.b64decode(b64_json)
    if url:
        with urllib.request.urlopen(url, timeout=120) as resp:
            return resp.read()
    raise RuntimeError("OpenAI image item had neither b64_json nor url")


def _openai_image_payload(
    *,
    model: str,
    prompt: str,
    size: str,
    quality: str,
    output_format: str,
    background: str,
    moderation: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size,
    }
    if quality:
        payload["quality"] = quality
    fmt = (output_format or "").strip().lower()
    if fmt in {"png", "jpeg", "webp"}:
        payload["output_format"] = fmt
    bg = (background or "").strip().lower()
    if bg in {"auto", "opaque", "transparent"}:
        payload["background"] = bg
    mod = (moderation or "").strip().lower()
    if mod in {"auto", "low"}:
        payload["moderation"] = mod
    return payload


def _extract_openai_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
        data = json.loads(body)
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or body
            return f"HTTP {exc.code}: {code or 'error'}: {message}"
        return f"HTTP {exc.code}: {body or exc.reason}"
    except Exception:
        return f"HTTP {exc.code}: {exc.reason}"


def generate_once(
    model: str,
    prompt: str,
    size: str,
    timeout: float,
    *,
    quality: str,
    output_format: str,
    background: str,
    moderation: str,
) -> bytes:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    api_base = (os.environ.get("OPENAI_API_BASE") or "https://api.openai.com/v1").rstrip("/")
    payload = _openai_image_payload(
        model=model,
        prompt=prompt,
        size=size,
        quality=quality,
        output_format=output_format,
        background=background,
        moderation=moderation,
    )
    request = urllib.request.Request(
        f"{api_base}/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_extract_openai_error(exc)) from exc
    return decode_response_image(data)


def generate_with_fallback(args: argparse.Namespace, prompt: str, output: Path, status_path: Path, h: str) -> dict[str, Any]:
    attempts: list[dict[str, str]] = []
    for model in [args.model, args.fallback_model]:
        if not model:
            continue
        write_status(
            status_path,
            {
                "status": "running",
                "model": model,
                "fallback_model": args.fallback_model,
                "prompt_hash": h,
                "output": str(output),
                "attempts": attempts,
            },
        )
        try:
            image_bytes = generate_once(
                model,
                prompt,
                args.size,
                args.timeout,
                quality=args.quality,
                output_format=args.output_format,
                background=args.background,
                moderation=args.moderation,
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(image_bytes)
            result = {
                "ok": True,
                "status": "completed",
                "model": model,
                "fallback_used": model != args.model,
                "fallback_model": args.fallback_model,
                "prompt_hash": h,
                "output": str(output.resolve()),
                "attempts": attempts,
            }
            write_status(status_path, result)
            return result
        except Exception as exc:
            attempts.append({"model": model, "error": str(exc)})
            if model == args.fallback_model:
                break

    result = {
        "ok": False,
        "status": "failed",
        "model": args.model,
        "fallback_model": args.fallback_model,
        "prompt_hash": h,
        "output": str(output),
        "attempts": attempts,
    }
    write_status(status_path, result)
    return result


def spawn_worker(args: argparse.Namespace, prompt: str, output: Path, status_path: Path, h: str, cache_dir: Path) -> dict[str, Any]:
    prompt_file = cache_dir / f"{h}.prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--prompt-file",
        str(prompt_file),
        "--output",
        str(output),
        "--status-path",
        str(status_path),
        "--cache-dir",
        str(cache_dir),
        "--model",
        args.model,
        "--fallback-model",
        args.fallback_model,
        "--size",
        args.size,
        "--quality",
        args.quality,
        "--output-format",
        args.output_format,
        "--background",
        args.background,
        "--moderation",
        args.moderation,
        "--timeout",
        str(args.timeout),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=creationflags)
    result = {
        "ok": True,
        "status": "pending",
        "model": args.model,
        "fallback_model": args.fallback_model,
        "prompt_hash": h,
        "output": str(output),
        "status_path": str(status_path),
        "message": "image generation queued; keep updating status and do not mark final complete until the image preview exists",
    }
    write_status(status_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--output")
    parser.add_argument("--status-path")
    parser.add_argument("--cache-dir", default=".xhs_image_cache")
    parser.add_argument("--model", default="gpt-image-2-pro")
    parser.add_argument("--fallback-model", default="gpt-image-2")
    parser.add_argument("--size", default="1080x1440")
    parser.add_argument("--quality", default="auto", choices=["low", "medium", "high", "auto"])
    parser.add_argument("--output-format", default="png", choices=["png", "jpeg", "webp"])
    parser.add_argument("--background", default="auto", choices=["auto", "opaque", "transparent"])
    parser.add_argument("--moderation", default="auto", choices=["auto", "low"])
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--async", dest="async_mode", action="store_true", help="Queue generation in a background process")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cache-by", default="prompt_hash", choices=["prompt_hash"])
    args = parser.parse_args()

    try:
        prompt = load_prompt(args)
        cache_dir = Path(args.cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        h = prompt_hash(prompt, args.size)
        output = Path(args.output) if args.output else cache_dir / f"{h}.png"
        status_path = Path(args.status_path) if args.status_path else cache_dir / f"{h}.status.json"

        if output.exists():
            result = {
                "ok": True,
                "status": "cached",
                "model": args.model,
                "fallback_model": args.fallback_model,
                "prompt_hash": h,
                "output": str(output.resolve()),
                "status_path": str(status_path),
            }
            write_status(status_path, result)
            print(json.dumps(result, ensure_ascii=False))
            return 0

        if args.dry_run:
            result = {
                "ok": True,
                "status": "dry_run",
                "model": args.model,
                "fallback_model": args.fallback_model,
                "size": args.size,
                "quality": args.quality,
                "output_format": args.output_format,
                "background": args.background,
                "moderation": args.moderation,
                "prompt_hash": h,
                "output": str(output),
                "status_path": str(status_path),
                "async": args.async_mode,
            }
            print(json.dumps(result, ensure_ascii=False))
            return 0

        if args.async_mode and not args.worker:
            result = spawn_worker(args, prompt, output, status_path, h, cache_dir)
            print(json.dumps(result, ensure_ascii=False))
            return 0

        result = generate_with_fallback(args, prompt, output, status_path, h)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
