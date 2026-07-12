"""Opaque identifiers and cross-platform artifact display names."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import unicodedata

from ecorex.ids import new_id


_INVALID_FILENAME = re.compile(r"[\x00-\x1f\x7f<>:\"/\\|?*]+")
_WHITESPACE = re.compile(r"\s+")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def new_artifact_id() -> str:
    return new_id("art")


def new_revision_id() -> str:
    return new_id("rev")


def new_feedback_id() -> str:
    return new_id("fdb")


def new_retouch_job_id() -> str:
    return new_id("rtj")


def new_retouch_workspace_id() -> str:
    return new_id("rtw")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonicalize_filename_for_policy(requested_name: str) -> str:
    """Canonical basename shared by classification, persistence, and display.

    Windows removes trailing spaces/dots and Unicode compatibility characters
    can become ASCII dots or separators. Canonicalizing once before extension
    policy prevents the stored/displayed name from becoming more dangerous than
    the name that was classified.
    """

    normalized = unicodedata.normalize("NFKC", str(requested_name or ""))
    basename = normalized.replace("\\", "/").rsplit("/", 1)[-1]
    value = _INVALID_FILENAME.sub("_", basename)
    value = _WHITESPACE.sub(" ", value).strip(" .")
    return value if value and value not in {".", ".."} else "未命名"


def filename_claim_key(value: str) -> str:
    """Return a conservative case/normalization-insensitive filename key."""

    return unicodedata.normalize("NFKC", str(value)).casefold().rstrip(" .")


def _fits_filename_limits(
    value: str,
    *,
    max_length: int,
    max_utf8_bytes: int,
    max_utf16_units: int,
) -> bool:
    return (
        len(value) <= max_length
        and len(value.encode("utf-8")) <= max_utf8_bytes
        and len(value.encode("utf-16-le")) // 2 <= max_utf16_units
    )


def _truncate_filename(
    stem: str,
    suffix: str,
    *,
    max_length: int,
    max_utf8_bytes: int,
    max_utf16_units: int,
) -> str:
    low, high = 0, len(stem)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate = stem[:midpoint].rstrip(" .") + suffix
        if _fits_filename_limits(
            candidate,
            max_length=max_length,
            max_utf8_bytes=max_utf8_bytes,
            max_utf16_units=max_utf16_units,
        ):
            low = midpoint
        else:
            high = midpoint - 1
    truncated = stem[:low].rstrip(" .") or "未命名"
    candidate = truncated + suffix
    if not _fits_filename_limits(
        candidate,
        max_length=max_length,
        max_utf8_bytes=max_utf8_bytes,
        max_utf16_units=max_utf16_units,
    ):
        # A pathological suffix must not defeat the component limit.
        candidate = suffix.lstrip(".") or "未命名"
        return _truncate_filename(
            candidate,
            "",
            max_length=max_length,
            max_utf8_bytes=max_utf8_bytes,
            max_utf16_units=max_utf16_units,
        )
    return candidate


def sanitize_display_filename(
    requested_name: str,
    *,
    max_length: int = 160,
    max_utf8_bytes: int = 255,
    max_utf16_units: int = 255,
) -> str:
    """Return a basename valid on Windows and macOS without leaking a path."""

    if max_length < 24:
        raise ValueError("max_length must be at least 24")
    value = canonicalize_filename_for_policy(requested_name)

    # Windows treats a reserved device basename as reserved even when an
    # extension follows it (for example CON.txt).
    device_stem = value.split(".", 1)[0].rstrip(" .").upper()
    if device_stem in _WINDOWS_RESERVED:
        value = f"_{value}"

    if not _fits_filename_limits(
        value,
        max_length=max_length,
        max_utf8_bytes=max_utf8_bytes,
        max_utf16_units=max_utf16_units,
    ):
        dot = value.rfind(".")
        suffix = value[dot:] if 0 < dot and len(value) - dot <= 20 else ""
        stem = value[:dot] if suffix else value
        value = _truncate_filename(
            stem,
            suffix,
            max_length=max_length,
            max_utf8_bytes=max_utf8_bytes,
            max_utf16_units=max_utf16_units,
        )
    value = value.rstrip(" .")
    return value or "未命名"


def split_display_filename(requested_name: str) -> tuple[str, str]:
    value = sanitize_display_filename(requested_name)
    dot = value.rfind(".")
    if dot <= 0 or len(value) - dot > 20:
        return value, ""
    return value[:dot], value[dot:]


def minute_display_name(requested_name: str, when: datetime, sequence: int) -> str:
    if sequence < 1 or sequence > 999_999:
        raise ValueError("sequence must be between 1 and 999999")
    stem, suffix = split_display_filename(requested_name)
    marker = f"_{when.strftime('%Y%m%d-%H%M')}_{sequence:02d}"
    max_length = 180
    stem = stem[: max(1, max_length - len(marker) - len(suffix))].rstrip(" .") or "未命名"
    return sanitize_display_filename(f"{stem}{marker}{suffix}", max_length=max_length)
