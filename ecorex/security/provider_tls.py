"""Digest-pinned private CA authority for managed provider egress.

The v1 provider bridge terminates TLS on loopback-only SNI hosts.  Its private
CA is deployment authority, not an ambient process setting: callers must pin
the exact regular PEM file and pass the resulting ``SSLContext`` directly to
``httpx`` with ``trust_env=False``.  Proxy variables and the platform trust
store can therefore never silently authorize an ``*.ecorex.internal`` peer.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import ssl
import stat
from typing import Iterable
from urllib.parse import urlsplit


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_SUFFIX = ".ecorex.internal"
_MAX_CA_BYTES = 1024 * 1024


class ProviderTLSConfigurationError(RuntimeError):
    """The managed-provider TLS trust boundary is unavailable or unsafe."""


def requires_private_provider_ca(origins: Iterable[str]) -> bool:
    """Return whether any normalized origin targets the private bridge zone."""

    for origin in origins:
        if not isinstance(origin, str):
            raise ProviderTLSConfigurationError("provider TLS origin is invalid")
        hostname = (urlsplit(origin).hostname or "").casefold().rstrip(".")
        if hostname.endswith(_PRIVATE_SUFFIX):
            return True
    return False


def validate_provider_ca_binding(
    origins: Iterable[str],
    *,
    ca_bundle_path: Path | None,
    ca_bundle_sha256: str | None,
) -> None:
    """Validate paired deployment settings and eagerly pin private bridge CA."""

    origin_values = tuple(origins)
    if any(not isinstance(origin, str) for origin in origin_values):
        raise ProviderTLSConfigurationError("provider TLS origin is invalid")
    private_flags = tuple(
        (urlsplit(origin).hostname or "")
        .casefold()
        .rstrip(".")
        .endswith(_PRIVATE_SUFFIX)
        for origin in origin_values
    )
    required = any(private_flags)
    if (ca_bundle_path is None) != (ca_bundle_sha256 is None):
        raise ProviderTLSConfigurationError(
            "provider CA path and digest must be configured together"
        )
    if required and ca_bundle_path is None:
        raise ProviderTLSConfigurationError(
            "private provider origins require a pinned CA bundle"
        )
    if ca_bundle_path is not None and (
        not private_flags or not all(private_flags)
    ):
        raise ProviderTLSConfigurationError(
            "a private provider CA requires an all-private origin set"
        )
    if ca_bundle_path is not None and ca_bundle_sha256 is not None:
        _pinned_ca_bytes(ca_bundle_path, ca_bundle_sha256)


def pinned_provider_ssl_context(
    ca_bundle_path: Path | None,
    ca_bundle_sha256: str | None,
) -> ssl.SSLContext | None:
    """Build a strict context from the exact pinned PEM bytes.

    ``None`` deliberately means the ordinary public Web PKI.  A configured
    private bundle is added to a fresh default context, so deployments can use
    a mixture of private bridge and public provider origins without disabling
    hostname verification or certificate validation.
    """

    if ca_bundle_path is None and ca_bundle_sha256 is None:
        return None
    if ca_bundle_path is None or ca_bundle_sha256 is None:
        raise ProviderTLSConfigurationError(
            "provider CA path and digest must be configured together"
        )
    payload = _pinned_ca_bytes(ca_bundle_path, ca_bundle_sha256)
    try:
        text = payload.decode("ascii")
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cadata=text)
    except (UnicodeDecodeError, ValueError, ssl.SSLError):
        raise ProviderTLSConfigurationError(
            "provider CA bundle is invalid"
        ) from None
    return context


def _pinned_ca_bytes(path: Path, expected_sha256: str) -> bytes:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or _SHA256.fullmatch(str(expected_sha256)) is None
    ):
        raise ProviderTLSConfigurationError("provider CA binding is invalid")
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= _MAX_CA_BYTES
        ):
            raise OSError
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(_MAX_CA_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except OSError:
        raise ProviderTLSConfigurationError(
            "provider CA bundle is unavailable"
        ) from None
    identity = _identity(before)
    if (
        len(payload) != before.st_size
        or _identity(opened) != identity
        or _identity(after) != identity
        or _identity(current) != identity
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise ProviderTLSConfigurationError(
            "provider CA bundle digest does not match"
        )
    return payload


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


__all__ = [
    "ProviderTLSConfigurationError",
    "pinned_provider_ssl_context",
    "requires_private_provider_ca",
    "validate_provider_ca_binding",
]
