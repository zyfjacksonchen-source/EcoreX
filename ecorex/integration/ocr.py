"""Trusted local OCR adapter backed by the signed OCR dependency pack.

The adapter consumes bytes only.  It never accepts a host path from a model or
WebUI, and imports the optional OCR dependencies lazily so Core can start when
the pack is absent.
"""

from __future__ import annotations

from io import BytesIO
import re
import threading
import time
from typing import Any


class OCRProviderUnavailable(RuntimeError):
    code = "ocr_provider_unavailable"


_ENGINE: Any | None = None
_ENGINE_LOCK = threading.RLock()
_URL_RE = re.compile(r"https?://[^\s<>'\"`，。；、（）()\[\]{}]+", re.IGNORECASE)


def extract_urls_from_text(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            match.group(0).rstrip(".,;:!?，。；：！？")
            for match in _URL_RE.finditer(str(text or ""))
        )
    )


def _engine() -> Any:
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception as error:
        raise OCRProviderUnavailable("signed OCR runtime is unavailable") from error
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = RapidOCR()
    return _ENGINE


def _result_lines(value: Any) -> list[dict[str, Any]]:
    raw = value[0] if isinstance(value, tuple) and value else value
    if not isinstance(raw, list):
        return []
    lines: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        text = str(item[1] or "").strip()
        if not text:
            continue
        confidence = item[2] if len(item) > 2 else None
        lines.append(
            {
                "text": text,
                "confidence": (
                    max(0.0, min(1.0, float(confidence)))
                    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                    else None
                ),
            }
        )
    return lines


def extract_image_text(content: bytes, *, timeout_seconds: float = 2.0) -> dict[str, Any]:
    if not isinstance(content, bytes) or not content:
        raise ValueError("OCR image content is empty")
    if not 0.5 <= float(timeout_seconds) <= 8:
        raise ValueError("OCR timeout is outside the product bound")
    try:
        import numpy as np
        from PIL import Image, ImageOps
    except Exception as error:
        raise OCRProviderUnavailable("signed OCR image dependencies are unavailable") from error
    started = time.monotonic()
    try:
        with Image.open(BytesIO(content)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            pixels = np.asarray(image)
    except Exception as error:
        raise ValueError("OCR image content is invalid") from error
    # RapidOCR's engine is not documented as re-entrant.  The lock avoids an
    # inference-session race while Runtime concurrency remains outside it.
    with _ENGINE_LOCK:
        result = _engine()(pixels)
    lines = _result_lines(result)
    text = "\n".join(line["text"] for line in lines)
    return {
        "status": "success" if text else "empty",
        "provider": "rapidocr_onnxruntime",
        "text": text,
        "blocks": lines,
        "latencyMs": int((time.monotonic() - started) * 1000),
        "cacheHit": False,
    }


class OCRServiceAdapter:
    """Runtime consumer for the verified ``ocr.extract`` pack service."""

    service_id = "ocr.extract"
    contract_version = "1.0.0"

    def extract(self, content: bytes, *, timeout_seconds: float) -> dict[str, Any]:
        return extract_image_text(content, timeout_seconds=timeout_seconds)


__all__ = [
    "OCRProviderUnavailable",
    "OCRServiceAdapter",
    "extract_image_text",
    "extract_urls_from_text",
]
