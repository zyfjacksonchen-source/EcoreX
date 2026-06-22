#!/usr/bin/env python3
"""Generate a final Xiaohongshu cover or carousel image with OpenAI, using status files and prompt-hash caching."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

OPENAI_IMAGE_MODEL_ALIASES = {
    "image-2-pro": "gpt-image-2-pro",
}
MAX_IMAGE_GENERATION_RETRIES = 4


def normalize_openai_image_model(model: str) -> str:
    value = (model or "").strip()
    return OPENAI_IMAGE_MODEL_ALIASES.get(value, value)


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
    payload["heartbeat_at"] = time.time()
    payload["pid"] = os.getpid()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{random.randint(1000, 9999)}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def image_metadata(image_bytes: bytes) -> dict[str, Any]:
    if len(image_bytes) < 32:
        raise RuntimeError("image output is too small")
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        width = int.from_bytes(image_bytes[16:20], "big")
        height = int.from_bytes(image_bytes[20:24], "big")
        return {"mime": "image/png", "width": width, "height": height}
    if image_bytes.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(image_bytes):
            if image_bytes[index] != 0xff:
                index += 1
                continue
            marker = image_bytes[index + 1]
            length = int.from_bytes(image_bytes[index + 2:index + 4], "big")
            if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}:
                height = int.from_bytes(image_bytes[index + 5:index + 7], "big")
                width = int.from_bytes(image_bytes[index + 7:index + 9], "big")
                return {"mime": "image/jpeg", "width": width, "height": height}
            index += max(length + 2, 2)
        raise RuntimeError("could not read jpeg dimensions")
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return {"mime": "image/webp"}
    raise RuntimeError("image output is not a supported image format")


def write_validated_image(output: Path, image_bytes: bytes) -> dict[str, Any]:
    metadata = image_metadata(image_bytes)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    tmp.write_bytes(image_bytes)
    os.replace(tmp, output)
    stat = output.stat()
    digest = hashlib.sha256(image_bytes).hexdigest()
    return {
        **metadata,
        "sha256": digest,
        "size_bytes": stat.st_size,
    }


def validate_existing_image(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    metadata = image_metadata(data)
    return {
        **metadata,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def cached_final_status_valid(status_path: Path, output: Path, model: str, h: str, metadata: dict[str, Any]) -> bool:
    """Only trust cache entries that were previously produced as final OpenAI images."""
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("ok") is not True:
        return False
    if str(payload.get("status") or "") not in {"completed", "cached"}:
        return False
    if payload.get("provider") != "openai":
        return False
    if payload.get("model") != model:
        return False
    if payload.get("image_kind") != "final":
        return False
    if payload.get("draft") is not False or payload.get("fallback_used") is not False:
        return False
    if payload.get("prompt_hash") != h:
        return False
    if str(payload.get("sha256") or "").lower() != str(metadata.get("sha256") or "").lower():
        return False
    cached_output = str(payload.get("output") or "")
    if not cached_output:
        return False
    try:
        if Path(cached_output).resolve() != output.resolve():
            return False
    except Exception:
        return False
    return True


def is_retryable_error(message: str) -> bool:
    text = message.lower()
    return any(marker in text for marker in (
        "http 408",
        "http 409",
        "http 425",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "remote end closed",
    ))


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
        model=normalize_openai_image_model(model),
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


def final_status_fields(model: str) -> dict[str, Any]:
    return {
        "provider": "openai",
        "model": model,
        "image_kind": "final",
        "draft": False,
        "fallback_used": False,
    }


def generate_final_image(args: argparse.Namespace, prompt: str, output: Path, status_path: Path, h: str) -> dict[str, Any]:
    attempts: list[dict[str, str]] = []
    model = normalize_openai_image_model(args.model)
    if model != "gpt-image-2-pro":
        raise ValueError("create-xiaohongshu-note final images must use --model gpt-image-2-pro")
    for retry_index in range(max(1, args.retries + 1)):
        write_status(
            status_path,
            {
                "ok": True,
                "status": "running" if retry_index == 0 else "retrying",
                **final_status_fields(model),
                "prompt_hash": h,
                "output": str(output),
                "attempts": attempts,
                "job_id": h,
                "retry_index": retry_index,
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
            metadata = write_validated_image(output, image_bytes)
            result = {
                "ok": True,
                "status": "completed",
                **final_status_fields(model),
                "prompt_hash": h,
                "job_id": h,
                "output": str(output.resolve()),
                "attempts": attempts,
                **metadata,
            }
            write_status(status_path, result)
            return result
        except Exception as exc:
            error = str(exc)
            attempts.append({"model": model, "retry": str(retry_index), "error": error})
            if retry_index < args.retries and is_retryable_error(error):
                time.sleep(min(2 ** retry_index + random.random(), 8))
                continue
            break

    result = {
        "ok": False,
        "status": "failed",
        **final_status_fields(model),
        "prompt_hash": h,
        "job_id": h,
        "output": str(output),
        "attempts": attempts,
        "message": "gpt-image-2-pro final image generation failed; do not create a local draft or placeholder preview.",
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
        "--retries",
        str(args.retries),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=creationflags)
    result = {
        "ok": True,
        "status": "pending",
        **final_status_fields(args.model),
        "prompt_hash": h,
        "job_id": h,
        "pid": process.pid,
        "output": str(output),
        "status_path": str(status_path),
        "message": "gpt-image-2-pro final image generation queued; do not mark final complete until the real image preview exists",
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
    parser.add_argument("--size", default="1080x1440")
    parser.add_argument("--quality", default="auto", choices=["low", "medium", "high", "auto"])
    parser.add_argument("--output-format", default="png", choices=["png", "jpeg", "webp"])
    parser.add_argument("--background", default="auto", choices=["auto", "opaque", "transparent"])
    parser.add_argument("--moderation", default="auto", choices=["auto", "low"])
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--async", dest="async_mode", action="store_true", help="Queue generation in a background process")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cache-by", default="prompt_hash", choices=["prompt_hash"])
    args = parser.parse_args()
    args.model = normalize_openai_image_model(args.model)
    args.retries = max(0, min(args.retries, MAX_IMAGE_GENERATION_RETRIES))

    try:
        if args.model != "gpt-image-2-pro":
            raise ValueError("create-xiaohongshu-note final images must use --model gpt-image-2-pro")
        prompt = load_prompt(args)
        cache_dir = Path(args.cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        h = prompt_hash(prompt, args.size)
        output = Path(args.output) if args.output else cache_dir / f"{h}.png"
        status_path = Path(args.status_path) if args.status_path else cache_dir / f"{h}.status.json"

        if output.exists():
            try:
                metadata = validate_existing_image(output)
            except Exception:
                output.unlink(missing_ok=True)
            else:
                if cached_final_status_valid(status_path, output, args.model, h, metadata):
                    result = {
                        "ok": True,
                        "status": "cached",
                        **final_status_fields(args.model),
                        "prompt_hash": h,
                        "job_id": h,
                        "output": str(output.resolve()),
                        "status_path": str(status_path),
                        **metadata,
                    }
                    write_status(status_path, result)
                    print(json.dumps(result, ensure_ascii=False))
                    return 0
                output.unlink(missing_ok=True)

        if args.dry_run:
            result = {
                "ok": True,
                "status": "dry_run",
                **final_status_fields(args.model),
                "size": args.size,
                "quality": args.quality,
                "output_format": args.output_format,
                "background": args.background,
                "moderation": args.moderation,
                "prompt_hash": h,
                "output": str(output),
                "status_path": str(status_path),
                "async": args.async_mode,
                "message": "dry run only; no draft image or preview was produced",
            }
            write_status(status_path, result)
            print(json.dumps(result, ensure_ascii=False))
            return 0

        if args.async_mode and not args.worker:
            result = spawn_worker(args, prompt, output, status_path, h, cache_dir)
            print(json.dumps(result, ensure_ascii=False))
            return 0

        result = generate_final_image(args, prompt, output, status_path, h)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 2
    except Exception as exc:
        model = normalize_openai_image_model(getattr(args, "model", "gpt-image-2-pro"))
        failure = {
            "ok": False,
            "status": "failed",
            **final_status_fields(model),
            "error": str(exc),
        }
        if "h" in locals():
            failure["prompt_hash"] = h
            failure["job_id"] = h
        if "output" in locals():
            failure["output"] = str(output)
        if "status_path" in locals():
            failure["status_path"] = str(status_path)
        try:
            target_status = locals().get("status_path")
            if not target_status and getattr(args, "status_path", ""):
                target_status = Path(args.status_path)
            if isinstance(target_status, Path):
                write_status(target_status, failure)
        except Exception:
            pass
        print(json.dumps(failure, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
