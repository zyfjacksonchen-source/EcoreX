# encoding:utf-8
"""Model-call telemetry helpers.

The collector is intentionally local and bounded for now. It gives backend
diagnostics and tests a stable event shape without committing this slice to a
database or UI contract before the Run Center exists.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time
from typing import Any, Deque, Dict, Iterable, Optional


_MAX_MODEL_CALL_EVENTS = 1000
_events: Deque[Dict[str, Any]] = deque(maxlen=_MAX_MODEL_CALL_EVENTS)
_events_lock = threading.Lock()


def _coerce_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _coerce_status_code(value: Any) -> Optional[int]:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return None
    if code <= 0:
        return code
    return code


def _nested_get(data: Dict[str, Any], *path: str) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_present(data: Dict[str, Any], candidates: Iterable[Any]) -> Any:
    for candidate in candidates:
        if isinstance(candidate, tuple):
            value = _nested_get(data, *candidate)
        else:
            value = data.get(candidate)
        if value is not None:
            return value
    return None


def normalize_usage_tokens(usage: Any) -> Dict[str, int]:
    """Normalize provider usage envelopes into token buckets."""
    if not isinstance(usage, dict):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
        }

    input_tokens = _coerce_int(
        _first_present(
            usage,
            (
                "prompt_tokens",
                "input_tokens",
                "inputTokens",
                "promptTokens",
                "promptTokenCount",
            ),
        )
    )
    output_tokens = _coerce_int(
        _first_present(
            usage,
            (
                "completion_tokens",
                "output_tokens",
                "outputTokens",
                "completionTokens",
                "candidatesTokenCount",
            ),
        )
    )
    total_tokens = _coerce_int(
        _first_present(usage, ("total_tokens", "totalTokens", "totalTokenCount"))
    )
    if total_tokens <= 0 and (input_tokens or output_tokens):
        total_tokens = input_tokens + output_tokens

    reasoning_tokens = _coerce_int(
        _first_present(
            usage,
            (
                "reasoning_tokens",
                "reasoningTokens",
                "thoughtsTokenCount",
                ("completion_tokens_details", "reasoning_tokens"),
                ("completionTokensDetails", "reasoningTokens"),
                ("output_tokens_details", "reasoning_tokens"),
                ("outputTokensDetails", "reasoningTokens"),
            ),
        )
    )
    cached_tokens = _coerce_int(
        _first_present(
            usage,
            (
                "cached_tokens",
                "cachedTokens",
                "cache_read_input_tokens",
                "cacheReadInputTokens",
                "cachedContentTokenCount",
                ("prompt_tokens_details", "cached_tokens"),
                ("promptTokensDetails", "cachedTokens"),
                ("input_tokens_details", "cached_tokens"),
                ("input_token_details", "cached_tokens"),
                ("inputTokenDetails", "cachedTokens"),
            ),
        )
    )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_tokens": cached_tokens,
    }


def classify_model_error(
    *,
    status_code: Any = None,
    message: Any = "",
    error_code: Any = "",
    error_type: Any = "",
) -> str:
    """Map provider-specific failures into a small retry/diagnostic taxonomy."""
    status = _coerce_status_code(status_code)
    text = " ".join(
        str(part or "").lower()
        for part in (message, error_code, error_type)
        if part is not None
    )

    if any(
        marker in text
        for marker in (
            "context length",
            "context window",
            "context overflow",
            "prompt is too long",
            "request_too_large",
            "tokens exceed",
            "maximum context",
            "exceeds model context",
        )
    ):
        return "context_overflow"
    if status == 429 or "rate limit" in text or "rate_limit" in text:
        return "rate_limit"
    if status == 408 or "timeout" in text or "timed out" in text:
        return "timeout"
    if (
        status == 0
        or "connection" in text
        or "network" in text
        or "chunkedencodingerror" in text
        or "reset by peer" in text
        or "dns" in text
        or "ssl" in text
    ):
        return "network_error"
    if "cancel" in text:
        return "cancelled"
    if status is not None and 500 <= status <= 599:
        return "server_error"
    if status is not None and 400 <= status <= 499:
        return "client_error"
    return "unknown"


def record_model_call(event: Dict[str, Any]) -> Dict[str, Any]:
    """Append a telemetry event and return the stored dict."""
    stored = dict(event)
    with _events_lock:
        _events.append(stored)
    return stored


def get_recent_model_calls(limit: int = 100) -> list:
    count = max(0, int(limit or 0))
    with _events_lock:
        rows = list(_events)
    if count == 0:
        return []
    return rows[-count:]


def reset_model_call_telemetry_for_tests() -> None:
    with _events_lock:
        _events.clear()


def is_model_error_response(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    error_value = response.get("error")
    return bool(error_value)


def extract_error_details(response: Dict[str, Any]) -> Dict[str, Any]:
    error_value = response.get("error")
    message = response.get("message", "")
    error_code = response.get("error_code", "")
    error_type = response.get("error_type", "")
    if isinstance(error_value, dict):
        message = error_value.get("message") or message
        error_code = error_value.get("code") or error_code
        error_type = error_value.get("type") or error_type
    elif error_value not in (None, False, True):
        message = message or str(error_value)

    return {
        "message": str(message or ""),
        "status_code": response.get("status_code"),
        "error_code": str(error_code or ""),
        "error_type": str(error_type or ""),
        "retry_after": (
            response.get("retry_after")
            or response.get("retry_after_seconds")
            or response.get("retry_after_ms")
        ),
    }


def chunk_has_model_output(chunk: Any) -> bool:
    if not isinstance(chunk, dict):
        return False
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            continue
        if delta.get("content") or delta.get("reasoning_content"):
            return True
        if delta.get("tool_calls") or delta.get("function_call"):
            return True
    return False


@dataclass
class ModelCallSpan:
    provider: str
    model: str
    stream: bool
    retry_count: int = 0
    api_path: str = "/chat/completions"

    def __post_init__(self) -> None:
        self.started_at = time.time()
        self._start_perf = time.perf_counter()
        self.first_token_latency_ms: Optional[float] = None
        self._usage = normalize_usage_tokens({})
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    def mark_first_token(self) -> None:
        if self.first_token_latency_ms is None:
            self.first_token_latency_ms = self._elapsed_ms()

    def observe_chunk(self, chunk: Any) -> None:
        if chunk_has_model_output(chunk):
            self.mark_first_token()
        if isinstance(chunk, dict) and "usage" in chunk:
            self.observe_usage(chunk.get("usage"))

    def observe_response(self, response: Any) -> None:
        if isinstance(response, dict) and "usage" in response:
            self.observe_usage(response.get("usage"))

    def observe_usage(self, usage: Any) -> None:
        normalized = normalize_usage_tokens(usage)
        if any(normalized.values()):
            self._usage.update(normalized)

    def finish_completed(self) -> Dict[str, Any]:
        return self._finish("completed")

    def finish_error(
        self,
        *,
        message: Any = "",
        status_code: Any = None,
        error_code: Any = "",
        error_type: Any = "",
        **_ignored: Any,
    ) -> Dict[str, Any]:
        return self._finish(
            "failed",
            message=message,
            status_code=status_code,
            error_code=error_code,
            error_type=error_type,
        )

    def finish_cancelled(
        self,
        *,
        message: Any = "stream consumer closed before completion",
    ) -> Dict[str, Any]:
        return self._finish(
            "cancelled",
            message=message,
            error_type="consumer_cancelled",
        )

    def _elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._start_perf) * 1000.0, 3)

    def _finish(
        self,
        status: str,
        *,
        message: Any = "",
        status_code: Any = None,
        error_code: Any = "",
        error_type: Any = "",
    ) -> Dict[str, Any]:
        if self._finished:
            return {}
        self._finished = True
        status_int = _coerce_status_code(status_code)
        event = {
            "timestamp": self.started_at,
            "completed_at": time.time(),
            "provider": self.provider or "",
            "model": self.model or "",
            "api_path": self.api_path,
            "stream": bool(self.stream),
            "retry_count": _coerce_int(self.retry_count),
            "status": status,
            "first_token_latency_ms": self.first_token_latency_ms,
            "total_latency_ms": self._elapsed_ms(),
            "input_tokens": self._usage["input_tokens"],
            "output_tokens": self._usage["output_tokens"],
            "total_tokens": self._usage["total_tokens"],
            "reasoning_tokens": self._usage["reasoning_tokens"],
            "cached_tokens": self._usage["cached_tokens"],
            "error_taxonomy": "",
            "error_status_code": None,
            "error_code": "",
            "error_type": "",
            "error_message": "",
        }
        if status != "completed":
            event.update({
                "error_taxonomy": classify_model_error(
                    status_code=status_int,
                    message=message,
                    error_code=error_code,
                    error_type=error_type,
                ),
                "error_status_code": status_int,
                "error_code": str(error_code or ""),
                "error_type": str(error_type or ""),
                "error_message": str(message or "")[:500],
            })
        return record_model_call(event)


def _close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def instrument_model_stream(stream: Any, span: ModelCallSpan):
    """Yield a provider stream while recording latency, usage, and failures."""
    stream_iter = iter(stream)
    try:
        for chunk in stream_iter:
            if isinstance(chunk, dict) and is_model_error_response(chunk):
                details = extract_error_details(chunk)
                span.finish_error(**details)
                yield chunk
                return
            span.observe_chunk(chunk)
            yield chunk
        span.finish_completed()
    except GeneratorExit:
        if not span.finished:
            span.finish_cancelled()
        _close_stream(stream_iter)
        raise
    except Exception as exc:
        span.finish_error(message=str(exc))
        _close_stream(stream_iter)
        raise
