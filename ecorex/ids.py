"""Dependency-free ULID identities shared by EcoreX product domains."""

from __future__ import annotations

import secrets
import time


_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD32_SET = frozenset(_CROCKFORD32)


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
    """Return a 26-character ULID-shaped, time-sortable opaque identity."""

    timestamp_ms = int(time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    randomness = secrets.randbits(80)
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
