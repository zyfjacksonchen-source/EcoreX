"""Fast local OCR helpers for text and URL extraction."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
import tempfile
import time
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger
from common.tool_execution_environment import ToolExecutionEnvironment
from common.utils import expand_path


_URL_BOUNDARY_CHARS = r"\s<>'\"`，。；、（）()\[\]{}"
_URL_RE = re.compile(rf"https?://[^{_URL_BOUNDARY_CHARS}]+", re.IGNORECASE)
_BARE_URL_RE = re.compile(
    rf"(?<![@\w:/.-])"
    rf"((?:[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.)+"
    rf"(?:com|net|org|io|ai|cn|co|me|dev|app|xyz|top|link|site|cloud|tech|edu|gov|info|biz)"
    rf"(?::\d{{2,5}})?(?:/[^{_URL_BOUNDARY_CHARS}]*)?)",
    re.IGNORECASE,
)
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_MAX = 128
_DEFAULT_TIMEOUT_SECONDS = 2.0
_PREPROCESS_TARGET_LONG_EDGE = 960
_RAPIDOCR_DET_LIMIT_SIDE_LEN = 736
_RAPIDOCR_ENGINES: Dict[str, Any] = {}


def _ocr_executor() -> ToolExecutionEnvironment:
    return ToolExecutionEnvironment(tool_name="ocr", include_system_path=True)


def _owned_python_runtime(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        with _ocr_executor().owned_python_import_context():
            return func(*args, **kwargs)

    return wrapper


def _trim_url(value: str) -> str:
    return str(value or "").strip().rstrip(".,;:!?，。；：！？")


def _normalize_ocr_url_text(text: str) -> str:
    return re.sub(
        r"\b(https?)\s*[:：/\\|]+\s*(?=[a-z0-9])",
        r"\1://",
        text or "",
        flags=re.IGNORECASE,
    )


def extract_urls_from_text(text: str) -> List[str]:
    normalized_text = _normalize_ocr_url_text(text or "")
    urls: List[str] = []
    seen = set()
    explicit_spans: List[Tuple[int, int]] = []
    for match in _URL_RE.finditer(normalized_text):
        url = _trim_url(match.group(0))
        key = url.lower()
        if url and key not in seen:
            seen.add(key)
            urls.append(url)
        explicit_spans.append(match.span())
    for match in _BARE_URL_RE.finditer(normalized_text):
        start, end = match.span(1)
        if any(start >= span_start and end <= span_end for span_start, span_end in explicit_spans):
            continue
        url = _trim_url(match.group(1))
        if not url:
            continue
        normalized = f"https://{url}"
        key = normalized.lower()
        if key not in seen:
            seen.add(key)
            urls.append(normalized)
    return urls


def _safe_timeout(value: Any) -> float:
    try:
        return max(0.5, min(float(value or _DEFAULT_TIMEOUT_SECONDS), 8.0))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SECONDS


def _image_bytes_from_source(source: str, cwd: Optional[str] = None) -> Tuple[bytes, str]:
    value = str(source or "").strip()
    if not value:
        raise ValueError("image is required")
    if value.startswith("data:image/"):
        header, _, data = value.partition(",")
        if ";base64" not in header or not data:
            raise ValueError("only base64 data image URLs are supported")
        return base64.b64decode(data), "data-url"
    if value.startswith("file://"):
        value = value[7:]
    expanded = expand_path(value)
    path = Path(expanded)
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    path = path.resolve()
    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker

        decision = get_tool_permission_broker().authorize_file_access("read", str(path), cwd=None)
        if not decision.get("allowed"):
            raise PermissionError(decision.get("reason") or "image read blocked by permissions")
    except PermissionError:
        raise
    except Exception as exc:
        raise PermissionError(f"Permission broker unavailable; image read blocked. {exc}") from exc
    return path.read_bytes(), str(path)


def _cache_key(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def _cache_put(key: str, value: Dict[str, Any]) -> None:
    _CACHE[key] = value
    while len(_CACHE) > _CACHE_MAX:
        oldest = next(iter(_CACHE))
        _CACHE.pop(oldest, None)


def _public_error_summary(exc: BaseException) -> Dict[str, Any]:
    text = str(exc or "")
    return {
        "errorType": exc.__class__.__name__,
        "errorHash": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12],
        "errorLength": len(text),
        "redacted": True,
    }


def _preprocess_image(image_bytes: bytes) -> Tuple[str, str]:
    executor = _ocr_executor()
    Image = executor.import_python_module("PIL.Image")
    ImageOps = executor.import_python_module("PIL.ImageOps")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        output = handle.name
    try:
        with tempfile.NamedTemporaryFile(suffix=".input", delete=False) as input_handle:
            input_handle.write(image_bytes)
            input_path = input_handle.name
        with Image.open(input_path) as image:
            image = ImageOps.exif_transpose(image).convert("L")
            mask = image.point(lambda pixel: 255 if int(pixel) < 245 else 0)
            bbox = mask.getbbox()
            if bbox:
                width, height = image.size
                left, top, right, bottom = bbox
                margin = max(8, min(32, int(max(width, height) * 0.03)))
                left = max(0, left - margin)
                top = max(0, top - margin)
                right = min(width, right + margin)
                bottom = min(height, bottom + margin)
                crop_width = max(1, right - left)
                crop_height = max(1, bottom - top)
                original_area = max(1, width * height)
                crop_area = crop_width * crop_height
                if crop_width >= 16 and crop_height >= 16 and crop_area < original_area * 0.9:
                    image = image.crop((left, top, right, bottom))
            width, height = image.size
            long_edge = max(1, max(width, height))
            if long_edge != _PREPROCESS_TARGET_LONG_EDGE:
                scale = _PREPROCESS_TARGET_LONG_EDGE / long_edge
                if long_edge < _PREPROCESS_TARGET_LONG_EDGE:
                    scale = min(3.0, scale)
                image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
            image = ImageOps.autocontrast(image)
            image.save(output, format="PNG")
        return output, input_path
    except Exception:
        try:
            os.unlink(output)
        except OSError:
            pass
        raise


def _pytesseract_available() -> bool:
    if "pytesseract" in sys.modules:
        return True
    return _ocr_executor().provider.resolve_python_package("pytesseract", allow_system_path=True).available


def _pillow_available() -> bool:
    if "PIL" in sys.modules or "PIL.Image" in sys.modules:
        return True
    return _ocr_executor().provider.resolve_python_package("PIL", allow_system_path=True).available


def _rapidocr_module_name() -> str:
    for name in ("rapidocr_onnxruntime", "rapidocr"):
        if name in sys.modules:
            return name
        if _ocr_executor().provider.resolve_python_package(name, allow_system_path=True).available:
            return name
    return ""


def _rapidocr_available() -> bool:
    return bool(_rapidocr_module_name())


def _collect_rapidocr_text(value: Any) -> List[str]:
    texts: List[str] = []
    if value is None:
        return texts
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        for key in ("text", "rec_text", "recText"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                texts.append(item)
        for key in ("texts", "rec_texts", "recTexts"):
            item = value.get(key)
            if isinstance(item, list):
                texts.extend(str(part) for part in item if str(part or "").strip())
        return texts
    if hasattr(value, "txts"):
        try:
            item = getattr(value, "txts")
            if isinstance(item, list):
                texts.extend(str(part) for part in item if str(part or "").strip())
        except Exception:
            pass
    if isinstance(value, tuple) and value:
        return _collect_rapidocr_text(value[0])
    if isinstance(value, list):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[1], str):
                texts.append(item[1])
            else:
                texts.extend(_collect_rapidocr_text(item))
    return texts


@_owned_python_runtime
def _run_rapidocr(image_path: str, timeout: float) -> str:
    del timeout
    module_name = _rapidocr_module_name()
    if not module_name:
        raise RuntimeError("rapidocr executable module not found")
    engine = _RAPIDOCR_ENGINES.get(module_name)
    if engine is None:
        module = _ocr_executor().import_python_module(module_name)
        rapidocr_cls = getattr(module, "RapidOCR")
        try:
            engine = rapidocr_cls(
                det_limit_type="max",
                det_limit_side_len=_RAPIDOCR_DET_LIMIT_SIDE_LEN,
            )
        except TypeError:
            engine = rapidocr_cls()
        _RAPIDOCR_ENGINES[module_name] = engine
    result = engine(image_path)
    return "\n".join(_collect_rapidocr_text(result))


@_owned_python_runtime
def _run_pytesseract(image_path: str, timeout: float) -> str:
    pytesseract = _ocr_executor().import_python_module("pytesseract")
    Image = _ocr_executor().import_python_module("PIL.Image")

    with Image.open(image_path) as image:
        return str(pytesseract.image_to_string(image, timeout=timeout) or "")


def _run_tesseract_cli(image_path: str, timeout: float) -> str:
    executor = _ocr_executor()
    dependency = executor.resolve_executable("tesseract", native=True)
    if not dependency.available:
        raise RuntimeError("tesseract executable not found")
    result = executor.run_completed(
        [dependency.path, image_path, "stdout", "--psm", "6"],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "tesseract failed").strip()[:300])
    return result.stdout or ""


@_owned_python_runtime
def _local_ocr(image_bytes: bytes, timeout: float) -> Dict[str, Any]:
    cache = _CACHE.get(_cache_key(image_bytes))
    if cache:
        return {**cache, "cacheHit": True}
    started = time.monotonic()
    processed = ""
    source = ""
    if not _pillow_available():
        return {
            "status": "unavailable",
            "provider": "unavailable",
            "text": "",
            "latencyMs": int((time.monotonic() - started) * 1000),
            "cacheHit": False,
        }
    try:
        processed, source = _preprocess_image(image_bytes)
        provider = ""
        if _rapidocr_available():
            provider = _rapidocr_module_name()
            text = _run_rapidocr(processed, timeout)
        elif _pytesseract_available():
            provider = "pytesseract"
            text = _run_pytesseract(processed, timeout)
        elif _ocr_executor().resolve_executable("tesseract", native=True).available:
            provider = "tesseract-cli"
            text = _run_tesseract_cli(processed, timeout)
        else:
            text = ""
            provider = "unavailable"
        payload = {
            "status": "success" if text else ("unavailable" if provider == "unavailable" else "empty"),
            "provider": provider,
            "text": text.strip(),
            "latencyMs": int((time.monotonic() - started) * 1000),
            "cacheHit": False,
        }
        _cache_put(_cache_key(image_bytes), payload)
        return payload
    finally:
        for path in (processed, source):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


class OcrTool(BaseTool):
    name: str = "ocr"
    description: str = (
        "Fast local OCR and URL extraction. Use this before vision when the goal is to read text or links "
        "from a screenshot/image. For URL-reading tasks, extract URLs first, then use browser.navigate/CDP."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["extract_text", "extract_urls", "diagnose"],
                "description": "OCR action to perform.",
            },
            "image": {
                "type": "string",
                "description": "Local image path, file:// URL, or base64 data image URL.",
            },
            "text": {
                "type": "string",
                "description": "Optional text to scan for URLs before OCR.",
            },
            "timeout": {
                "type": "number",
                "description": "Local OCR timeout seconds, default 2, max 8.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.cwd = self.config.get("cwd", os.getcwd())

    def apply_config(self, config: Dict[str, Any]) -> None:
        self.config = config or {}
        self.cwd = self.config.get("cwd", os.getcwd())

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "").strip().lower()
        if action == "diagnose":
            return ToolResult.success(self._diagnose())
        if action not in {"extract_text", "extract_urls"}:
            return ToolResult.fail("Error: action must be one of: extract_text, extract_urls, diagnose")
        started = time.monotonic()
        text_input = str(args.get("text") or "")
        urls = extract_urls_from_text(text_input)
        image = str(args.get("image") or "").strip()
        ocr_payload: Dict[str, Any] = {
            "status": "skipped",
            "provider": "none",
            "text": "",
            "latencyMs": 0,
            "cacheHit": False,
        }
        if image:
            try:
                image_bytes, source = _image_bytes_from_source(image, cwd=self.cwd)
                ocr_payload = _local_ocr(image_bytes, _safe_timeout(args.get("timeout")))
                text_value = str(ocr_payload.get("text") or "")
                for url in extract_urls_from_text(text_value):
                    if url.lower() not in {item.lower() for item in urls}:
                        urls.append(url)
                ocr_payload["sourceHash"] = hashlib.sha256(str(source).encode("utf-8", errors="replace")).hexdigest()[:12]
            except Exception as exc:
                error_summary = _public_error_summary(exc)
                logger.warning(f"[OCR] local OCR failed: {error_summary}")
                ocr_payload = {
                    "status": "error",
                    "provider": "local",
                    "errorSummary": error_summary,
                    "latencyMs": int((time.monotonic() - started) * 1000),
                    "cacheHit": False,
                }
        if action == "extract_urls":
            return ToolResult.success({
                "status": "success",
                "urls": urls,
                "urlCount": len(urls),
                "ocr": self._public_ocr_metadata(ocr_payload),
                "nextAction": {"tool": "browser", "action": "navigate", "url": urls[0]} if urls else None,
                "totalLatencyMs": int((time.monotonic() - started) * 1000),
            })
        return ToolResult.success({
            "status": "success",
            "text": str(ocr_payload.get("text") or text_input or "")[:12000],
            "urls": urls,
            "ocr": self._public_ocr_metadata(ocr_payload),
            "totalLatencyMs": int((time.monotonic() - started) * 1000),
        })

    @staticmethod
    def _public_ocr_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in payload.items()
            if key not in {"text"}
        }

    @staticmethod
    def _diagnose() -> Dict[str, Any]:
        return {
            "status": "success",
            "providers": {
                "rapidocr": _rapidocr_available(),
                "rapidocrModule": _rapidocr_module_name(),
                "pytesseract": _pytesseract_available(),
                "tesseractCli": _ocr_executor().resolve_executable("tesseract", native=True).available,
            },
            "cacheEntries": len(_CACHE),
            "rapidocrEngineCached": bool(_RAPIDOCR_ENGINES),
            "defaultTimeoutSeconds": _DEFAULT_TIMEOUT_SECONDS,
        }
