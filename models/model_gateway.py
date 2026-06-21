# encoding:utf-8
"""Provider-agnostic model-call telemetry and retry wrapper.

Native provider adapters such as DashScope, Gemini, Claude, Zhipu, and
Moonshot return OpenAI-shaped dicts/chunks but do not inherit the shared
OpenAI-compatible gateway. Keep their agent-path telemetry and retry semantics
aligned here without rewriting each vendor adapter.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Optional

from models.model_retry import (
    annotate_retry_evidence,
    build_retry_decision,
    coerce_max_retries,
    sleep_for_retry,
)
from models.model_telemetry import (
    ModelCallSpan,
    chunk_has_model_output,
    extract_error_details,
    instrument_model_stream,
    is_model_error_response,
)


NATIVE_AGENT_API_PATH = "/native/call_with_tools"


def _coerce_retry_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _new_span(provider: str, model: str, *, stream: bool, retry_count: int) -> ModelCallSpan:
    return ModelCallSpan(
        provider=provider or "",
        model=model or "",
        stream=stream,
        retry_count=retry_count,
        api_path=NATIVE_AGENT_API_PATH,
    )


def _as_stream(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        return iter([value])
    return iter(value)


def _coerce_sync_response(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        return {
            "error": True,
            "message": f"Native provider returned unsupported response: {type(value).__name__}",
            "status_code": 500,
        }
    try:
        iterator = iter(value)
    except TypeError:
        return {
            "error": True,
            "message": f"Native provider returned unsupported response: {type(value).__name__}",
            "status_code": 500,
        }

    try:
        first = next(iterator)
    except StopIteration:
        first = {
            "error": True,
            "message": "Native provider returned an empty sync response stream",
            "status_code": 500,
        }
    finally:
        _close_stream(iterator)

    if isinstance(first, dict):
        return first
    return {
        "error": True,
        "message": f"Native provider yielded unsupported sync response: {type(first).__name__}",
        "status_code": 500,
    }


def _error_response_from_exception(exc: BaseException) -> Dict[str, Any]:
    return {
        "error": True,
        "message": str(exc),
        "status_code": 500,
    }


def _mark_stream_retry_suppressed(response: Dict[str, Any]) -> Dict[str, Any]:
    annotated = dict(response or {})
    annotated["retry_suppressed"] = True
    annotated["retry_suppressed_reason"] = "stream_output_started"
    error_value = annotated.get("error")
    if isinstance(error_value, dict):
        nested = dict(error_value)
        nested["retry_suppressed"] = True
        nested["retry_suppressed_reason"] = "stream_output_started"
        annotated["error"] = nested
    return annotated


def _close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def call_native_model_with_gateway(
    invoke: Callable[[int], Any],
    *,
    provider: str,
    model: str,
    stream: bool,
    retry_count: Any = 0,
    max_model_retries: Any = None,
    retry_sleep: Optional[Callable[[float], None]] = None,
) -> Any:
    """Call a native provider adapter with shared retry and telemetry semantics.

    ``invoke`` receives the absolute retry count to pass to the provider for
    diagnostics. It may return either a sync response dict or a stream/generator
    of OpenAI-like chunks.
    """
    base_retry_count = _coerce_retry_count(retry_count)
    max_retries = coerce_max_retries(max_model_retries)
    if stream:
        return _stream_native_with_gateway(
            invoke,
            provider=provider,
            model=model,
            base_retry_count=base_retry_count,
            max_model_retries=max_retries,
            retry_sleep=retry_sleep,
        )
    return _sync_native_with_gateway(
        invoke,
        provider=provider,
        model=model,
        base_retry_count=base_retry_count,
        max_model_retries=max_retries,
        retry_sleep=retry_sleep,
    )


def _sync_native_with_gateway(
    invoke: Callable[[int], Any],
    *,
    provider: str,
    model: str,
    base_retry_count: int,
    max_model_retries: int,
    retry_sleep: Optional[Callable[[float], None]],
) -> Dict[str, Any]:
    last_response: Dict[str, Any] = {}
    for attempt in range(max_model_retries + 1):
        absolute_retry_count = base_retry_count + attempt
        span = _new_span(
            provider,
            model,
            stream=False,
            retry_count=absolute_retry_count,
        )
        try:
            response = invoke(absolute_retry_count)
        except Exception as exc:
            response = _error_response_from_exception(exc)
        try:
            response = _coerce_sync_response(response)
        except Exception as exc:
            response = _error_response_from_exception(exc)

        span.observe_response(response)
        if not is_model_error_response(response):
            span.finish_completed()
            return response

        details = extract_error_details(response)
        decision = build_retry_decision(
            details,
            attempt=attempt,
            max_retries=max_model_retries,
        )
        span.finish_error(**details)
        annotated = annotate_retry_evidence(response, decision)
        last_response = annotated
        if decision.should_retry:
            sleep_for_retry(decision.delay_seconds, retry_sleep)
            continue
        return annotated
    return last_response


def _stream_native_with_gateway(
    invoke: Callable[[int], Any],
    *,
    provider: str,
    model: str,
    base_retry_count: int,
    max_model_retries: int,
    retry_sleep: Optional[Callable[[float], None]],
):
    def retrying_stream():
        attempt = 0
        while attempt <= max_model_retries:
            absolute_retry_count = base_retry_count + attempt
            span = _new_span(
                provider,
                model,
                stream=True,
                retry_count=absolute_retry_count,
            )
            try:
                raw_stream = _as_stream(invoke(absolute_retry_count))
            except Exception as exc:
                raw_stream = iter([_error_response_from_exception(exc)])

            stream = instrument_model_stream(raw_stream, span)
            buffered_chunks = []
            output_started = False
            retry_next = False
            retry_decision = None
            try:
                for chunk in stream:
                    if isinstance(chunk, dict) and is_model_error_response(chunk):
                        details = extract_error_details(chunk)
                        retry_decision = build_retry_decision(
                            details,
                            attempt=attempt,
                            max_retries=max_model_retries,
                        )
                        annotated = annotate_retry_evidence(chunk, retry_decision)
                        if not output_started and retry_decision.should_retry:
                            retry_next = True
                            _close_stream(stream)
                            break
                        if output_started and retry_decision.should_retry:
                            annotated = _mark_stream_retry_suppressed(annotated)
                        for buffered in buffered_chunks:
                            yield buffered
                        yield annotated
                        return

                    if chunk_has_model_output(chunk):
                        output_started = True
                        for buffered in buffered_chunks:
                            yield buffered
                        buffered_chunks = []
                        yield chunk
                    elif output_started:
                        yield chunk
                    else:
                        buffered_chunks.append(chunk)
                else:
                    for buffered in buffered_chunks:
                        yield buffered
                    return
            except GeneratorExit:
                _close_stream(stream)
                raise
            except Exception as exc:
                details = {"message": str(exc), "status_code": 500}
                retry_decision = build_retry_decision(
                    details,
                    attempt=attempt,
                    max_retries=max_model_retries,
                )
                error_chunk = annotate_retry_evidence(
                    _error_response_from_exception(exc),
                    retry_decision,
                )
                if output_started:
                    if retry_decision.retryable:
                        error_chunk = _mark_stream_retry_suppressed(error_chunk)
                    for buffered in buffered_chunks:
                        yield buffered
                    yield error_chunk
                    return
                if retry_decision.should_retry:
                    retry_next = True
                else:
                    for buffered in buffered_chunks:
                        yield buffered
                    yield error_chunk
                    return

            if retry_next and retry_decision is not None:
                sleep_for_retry(retry_decision.delay_seconds, retry_sleep)
                attempt += 1
                continue
            return

    return retrying_stream()
