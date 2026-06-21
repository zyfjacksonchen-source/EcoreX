# encoding:utf-8
"""Retry policy helpers for model calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from typing import Any, Callable, Dict, Optional

from models.model_telemetry import classify_model_error


DEFAULT_MAX_MODEL_RETRIES = 1
DEFAULT_BACKOFF_SECONDS = 2.0
DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 30.0
DEFAULT_MAX_BACKOFF_SECONDS = 60.0
RETRYABLE_TAXONOMIES = {"rate_limit", "timeout", "network_error", "server_error"}


@dataclass(frozen=True)
class RetryDecision:
    taxonomy: str
    retryable: bool
    should_retry: bool
    delay_seconds: float
    attempt: int
    max_retries: int
    retry_after_seconds: Optional[float] = None


def coerce_max_retries(value: Any, default: int = DEFAULT_MAX_MODEL_RETRIES) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def parse_retry_after(value: Any, *, now: Optional[datetime] = None) -> Optional[float]:
    """Parse Retry-After seconds or HTTP-date into a non-negative delay."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return max(0.0, (target - reference).total_seconds())


def _fallback_backoff(taxonomy: str, attempt: int) -> float:
    base = (
        DEFAULT_RATE_LIMIT_BACKOFF_SECONDS
        if taxonomy == "rate_limit"
        else DEFAULT_BACKOFF_SECONDS
    )
    return min(DEFAULT_MAX_BACKOFF_SECONDS, base * (2 ** max(0, attempt)))


def build_retry_decision(
    details: Dict[str, Any],
    *,
    attempt: int,
    max_retries: int,
) -> RetryDecision:
    taxonomy = classify_model_error(
        status_code=details.get("status_code"),
        message=details.get("message", ""),
        error_code=details.get("error_code", ""),
        error_type=details.get("error_type", ""),
    )
    retryable = taxonomy in RETRYABLE_TAXONOMIES
    retry_after = parse_retry_after(
        details.get("retry_after")
        or details.get("retry_after_seconds")
    )
    if retry_after is None and details.get("retry_after_ms") not in (None, ""):
        try:
            retry_after = max(0.0, float(details.get("retry_after_ms")) / 1000.0)
        except (TypeError, ValueError):
            retry_after = None
    if retry_after is None:
        delay = _fallback_backoff(taxonomy, attempt)
    else:
        delay = retry_after
    return RetryDecision(
        taxonomy=taxonomy,
        retryable=retryable,
        should_retry=retryable and attempt < max_retries,
        delay_seconds=max(0.0, float(delay or 0.0)),
        attempt=max(0, int(attempt or 0)),
        max_retries=max(0, int(max_retries or 0)),
        retry_after_seconds=retry_after,
    )


def annotate_retry_evidence(
    response: Dict[str, Any],
    decision: RetryDecision,
) -> Dict[str, Any]:
    """Return an error response annotated with retry/taxonomy evidence."""
    annotated = dict(response or {})
    retry_after = decision.retry_after_seconds
    annotated.update({
        "error_taxonomy": decision.taxonomy,
        "error_type": decision.taxonomy,
        "retryable": decision.retryable,
        "retry_attempt": decision.attempt,
        "retry_attempts": decision.attempt,
        "max_retries": decision.max_retries,
        "retry_exhausted": decision.retryable and not decision.should_retry,
    })
    if retry_after is not None:
        annotated["retry_after_seconds"] = retry_after
    error_value = annotated.get("error")
    if isinstance(error_value, dict):
        nested = dict(error_value)
        nested.update({
            "taxonomy": decision.taxonomy,
            "retryable": decision.retryable,
            "retry_attempt": decision.attempt,
            "max_retries": decision.max_retries,
            "retry_exhausted": decision.retryable and not decision.should_retry,
        })
        annotated["error"] = nested
    return annotated


def sleep_for_retry(
    delay_seconds: float,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> None:
    sleeper = sleep_fn or time.sleep
    sleeper(max(0.0, float(delay_seconds or 0.0)))
