"""Signed Pack-Python worker for dependency service invocations."""

from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import sys
import time
from typing import Any, Mapping


def _inside(module: Any, root: Path) -> None:
    origin = getattr(module, "__file__", None)
    try:
        Path(origin).resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, TypeError, ValueError):
        raise RuntimeError("dependency origin escaped verified pack") from None


def _ocr(payload: Mapping[str, Any], runtime: Path) -> Mapping[str, Any]:
    import numpy as np
    from PIL import Image, ImageOps
    import PIL
    import rapidocr_onnxruntime
    from rapidocr_onnxruntime import RapidOCR

    for module in (np, PIL, rapidocr_onnxruntime):
        _inside(module, runtime)
    content = base64.b64decode(str(payload.get("content_base64") or ""), validate=True)
    if not content or len(content) > 8 * 1024 * 1024:
        raise ValueError("OCR content is invalid")
    started = time.monotonic()
    with Image.open(BytesIO(content)) as source:
        pixels = np.asarray(ImageOps.exif_transpose(source).convert("RGB"))
    raw = RapidOCR()(pixels)
    values = raw[0] if isinstance(raw, tuple) and raw else raw
    blocks: list[dict[str, Any]] = []
    if isinstance(values, list):
        for item in values:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            text = str(item[1] or "").strip()
            if not text:
                continue
            confidence = item[2] if len(item) > 2 else None
            blocks.append(
                {
                    "text": text,
                    "confidence": (
                        max(0.0, min(1.0, float(confidence)))
                        if isinstance(confidence, (int, float))
                        and not isinstance(confidence, bool)
                        else None
                    ),
                }
            )
    text = "\n".join(item["text"] for item in blocks)
    return {
        "status": "success" if text else "empty",
        "provider": "rapidocr_onnxruntime",
        "text": text,
        "blocks": blocks,
        "latencyMs": int((time.monotonic() - started) * 1000),
        "cacheHit": False,
    }


def _office_probe(runtime: Path) -> Mapping[str, Any]:
    import docx
    import openpyxl
    import pptx
    import pypdf
    import reportlab

    for module in (docx, openpyxl, pptx, pypdf, reportlab):
        _inside(module, runtime)
    return {
        "provider": "python-office-formats-v1",
        "modules": ["docx", "openpyxl", "pptx", "pypdf", "reportlab"],
    }


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    root = Path(sys.argv[1]).resolve(strict=True)
    runtime = (root / "runtime" / "python").resolve(strict=True)
    runtime.relative_to(root)
    sys.path.insert(0, str(runtime))
    try:
        request = json.loads(
            sys.stdin.buffer.read(12 * 1024 * 1024 + 1).decode("utf-8")
        )
        pack_id = request.get("pack_id")
        operation = request.get("operation")
        payload = request.get("payload")
        if request.get("schema_version") != 1 or not isinstance(payload, Mapping):
            return 3
        if pack_id == "ocr" and operation == "extract":
            result = _ocr(payload, runtime)
        elif pack_id == "office" and operation == "probe":
            result = _office_probe(runtime)
        else:
            return 4
        sys.stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "pack_id": pack_id,
                    "status": "success",
                    "result": result,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except Exception:
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
