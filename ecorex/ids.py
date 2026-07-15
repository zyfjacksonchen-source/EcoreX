"""Dependency-free ULID identities shared by EcoreX product domains."""

from __future__ import annotations

import os
import secrets
import threading
import time


_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD32_SET = frozenset(_CROCKFORD32)
_ULID_RANDOM_MASK = (1 << 80) - 1
_monotonic_lock = threading.Lock()
_monotonic_pid = os.getpid()
_last_timestamp_ms = -1
_last_randomness = 0


def _reset_monotonic_state_after_fork() -> None:
    global _last_randomness, _last_timestamp_ms, _monotonic_lock, _monotonic_pid
    # A forked child must not inherit a lock that another vanished parent
    # thread held at the fork boundary.  It also needs an independent random
    # suffix stream so parent and child cannot replay the same next identity.
    _monotonic_lock = threading.Lock()
    _monotonic_pid = os.getpid()
    _last_timestamp_ms = -1
    _last_randomness = 0


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_monotonic_state_after_fork)


def _valid_prefix(prefix: object) -> bool:
    return (
        isinstance(prefix, str)
        and bool(prefix)
        and len(prefix) <= 32
        and prefix[0].isalpha()
        and all(
            character.isascii() and (character.isalnum() or character == "-")
            for character in prefix
        )
    )


def _encode_base32(value: int, length: int) -> str:
    output = ["0"] * length
    for index in range(length - 1, -1, -1):
        output[index] = _CROCKFORD32[value & 31]
        value >>= 5
    return "".join(output)


def new_ulid() -> str:
    """Return a monotonic, time-sortable 26-character ULID identity.

    The random suffix alone does not define insertion order when the operating
    system clock returns the same millisecond for several durable records.  A
    process-local monotonic suffix preserves that order while retaining a fresh
    80-bit seed for every new millisecond and process.
    """

    global _last_randomness, _last_timestamp_ms, _monotonic_pid
    process_id = os.getpid()
    with _monotonic_lock:
        timestamp_ms = int(time.time_ns() // 1_000_000) & ((1 << 48) - 1)
        if process_id != _monotonic_pid:
            _monotonic_pid = process_id
            _last_timestamp_ms = -1
            _last_randomness = 0
        if timestamp_ms > _last_timestamp_ms:
            randomness = secrets.randbits(80)
        else:
            timestamp_ms = _last_timestamp_ms
            randomness = _last_randomness + 1
            if randomness > _ULID_RANDOM_MASK:
                timestamp_ms = (_last_timestamp_ms + 1) & ((1 << 48) - 1)
                randomness = secrets.randbits(80)
        _last_timestamp_ms = timestamp_ms
        _last_randomness = randomness
    return _encode_base32(timestamp_ms, 10) + _encode_base32(randomness, 16)


def new_id(prefix: str) -> str:
    if not _valid_prefix(prefix):
        raise ValueError("identity prefix is invalid")
    return f"{prefix}_{new_ulid()}"


def is_id(value: object, prefix: str) -> bool:
    """Return whether ``value`` is an identity emitted for ``prefix``.

    Security boundaries use this shared predicate instead of duplicating an
    identifier regex. That keeps narrow route exceptions synchronized with
    the identity authority when an ID representation evolves.
    """

    if not _valid_prefix(prefix) or not isinstance(value, str):
        return False
    expected_prefix = f"{prefix}_"
    if not value.startswith(expected_prefix):
        return False
    suffix = value[len(expected_prefix) :]
    return len(suffix) == 26 and all(
        character in _CROCKFORD32_SET for character in suffix
    )


__all__ = ["is_id", "new_id", "new_ulid"]
