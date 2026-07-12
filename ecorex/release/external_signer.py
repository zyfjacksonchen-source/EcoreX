"""Digest-pinned, stdin-only release signing boundary for CI KMS/HSM adapters.

The adapter is a release-engineering executable, not private key material.  It
receives only the exact ReleaseBuilder payload on stdin and must return one
Base64 encoded raw Ed25519 signature on stdout.  No payload, credential or key
material is ever placed in argv, a temporary file, an exception or a receipt.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
from types import MappingProxyType
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .process_boundary import (
    BoundedProcessIOError,
    BoundedProcessOutputOverflow,
    BoundedProcessTimedOut,
    run_bounded_process,
)
from .signing import SigningError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_SIGNER_OUTPUT_BYTES = 256
_MAX_SIGNER_STDERR_BYTES = 4 * 1024
_ALLOWED_ENVIRONMENT = frozenset(
    {
        # OS process and certificate discovery.
        "COMSPEC",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "USERPROFILE",
        "WINDIR",
        # GitHub Actions OIDC.  The token is inherited in memory and is never
        # included in argv, output, a receipt or an exception.
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        # Common workload-identity selectors (not long-lived private keys).
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_PROJECT",
    }
)


@dataclass(frozen=True, slots=True)
class ExternalSigningReceipt:
    """Non-sensitive proof for one successful external signing operation."""

    key_id: str
    payload_sha256: str
    signature_sha256: str
    executable_sha256: str
    adapter_sha256: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "key_id": self.key_id,
            "payload_sha256": self.payload_sha256,
            "signature_sha256": self.signature_sha256,
            "executable_sha256": self.executable_sha256,
            "adapter_sha256": self.adapter_sha256,
        }


class DigestPinnedExternalSigner:
    """ReleaseSigner backed by one immutable external KMS/HSM adapter.

    ``adapter_path`` is optional.  It supports a digest-pinned Python/native
    adapter launched by a separately pinned interpreter or host executable.
    It is the only fixed argv value; secret material and signing payloads are
    prohibited from argv by construction.
    """

    __slots__ = (
        "_adapter_path",
        "_adapter_sha256",
        "_environment",
        "_executable_path",
        "_executable_sha256",
        "_key_id",
        "_public_key",
        "_receipts",
        "_timeout_seconds",
    )

    def __init__(
        self,
        *,
        key_id: str,
        public_key: bytes,
        executable_path: str | os.PathLike[str],
        executable_sha256: str,
        adapter_path: str | os.PathLike[str] | None = None,
        adapter_sha256: str | None = None,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        if not isinstance(key_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", key_id
        ):
            raise ValueError("external signer key identity is invalid")
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ValueError("external signer public key must be raw Ed25519 bytes")
        if _SHA256.fullmatch(str(executable_sha256)) is None:
            raise ValueError("external signer executable digest is invalid")
        if (adapter_path is None) != (adapter_sha256 is None):
            raise ValueError("external signer adapter path and digest must be paired")
        if adapter_sha256 is not None and _SHA256.fullmatch(adapter_sha256) is None:
            raise ValueError("external signer adapter digest is invalid")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("external signer timeout must be between 1 and 120 seconds")

        self._executable_path = _regular_pinned_path(
            executable_path,
            expected_sha256=executable_sha256,
            label="signer executable",
        )
        self._executable_sha256 = executable_sha256
        self._adapter_path = (
            _regular_pinned_path(
                adapter_path,
                expected_sha256=str(adapter_sha256),
                label="signer adapter",
            )
            if adapter_path is not None
            else None
        )
        self._adapter_sha256 = adapter_sha256
        try:
            self._public_key = Ed25519PublicKey.from_public_bytes(public_key)
        except ValueError:
            raise ValueError("external signer public key is invalid") from None
        self._key_id = key_id
        source = os.environ if environment is None else environment
        filtered: dict[str, str] = {}
        for name in sorted(_ALLOWED_ENVIRONMENT):
            value = source.get(name)
            if isinstance(value, str) and value and "\x00" not in value:
                filtered[name] = value
        self._environment = MappingProxyType(filtered)
        self._timeout_seconds = float(timeout_seconds)
        self._receipts: list[ExternalSigningReceipt] = []

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def public_key_bytes(self) -> bytes:
        """Return only the non-secret raw verification key for trust binding."""

        return self._public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    @property
    def receipts(self) -> tuple[ExternalSigningReceipt, ...]:
        return tuple(self._receipts)

    def sign(self, payload: bytes) -> bytes:
        if not isinstance(payload, bytes) or not payload:
            raise SigningError("external signer payload must be non-empty bytes")
        executable_identity = _assert_pinned_file(
            self._executable_path,
            expected_sha256=self._executable_sha256,
            label="signer executable",
        )
        adapter_identity = None
        if self._adapter_path is not None and self._adapter_sha256 is not None:
            adapter_identity = _assert_pinned_file(
                self._adapter_path,
                expected_sha256=self._adapter_sha256,
                label="signer adapter",
            )
        command = [str(self._executable_path)]
        if self._adapter_path is not None:
            command.append(str(self._adapter_path))
        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        try:
            result = run_bounded_process(
                command,
                payload=payload,
                cwd=self._executable_path.parent,
                environment=self._environment,
                timeout_seconds=self._timeout_seconds,
                max_stdout_bytes=_MAX_SIGNER_OUTPUT_BYTES,
                max_stderr_bytes=_MAX_SIGNER_STDERR_BYTES,
                hide_window=bool(creation_flags),
            )
            stdout = result.stdout
        except BoundedProcessTimedOut:
            raise SigningError("external signer timed out safely") from None
        except BoundedProcessOutputOverflow:
            raise SigningError("external signer exceeded the safe output limit") from None
        except BoundedProcessIOError:
            raise SigningError("external signer failed safely: process I/O") from None
        except (OSError, subprocess.SubprocessError) as exc:
            raise SigningError(
                f"external signer failed safely: {type(exc).__name__}"
            ) from None
        if result.returncode != 0:
            raise SigningError("external signer rejected the payload")
        if not 1 <= len(stdout) <= _MAX_SIGNER_OUTPUT_BYTES:
            raise SigningError("external signer returned an invalid response")
        try:
            encoded = stdout.decode("ascii").strip()
            if not encoded or any(character.isspace() for character in encoded):
                raise ValueError
            signature = base64.b64decode(encoded, validate=True)
        except (UnicodeDecodeError, ValueError):
            raise SigningError("external signer returned an invalid response") from None
        if len(signature) != 64:
            raise SigningError("external signer returned an invalid signature")

        # Re-check both immutable command components before accepting any
        # signature.  A concurrent replacement therefore fails the entire
        # Candidate before ReleaseBuilder can publish its atomic directory.
        if _file_identity(self._executable_path) != executable_identity:
            raise SigningError("external signer executable changed during signing")
        _assert_pinned_file(
            self._executable_path,
            expected_sha256=self._executable_sha256,
            label="signer executable",
        )
        if self._adapter_path is not None and self._adapter_sha256 is not None:
            if _file_identity(self._adapter_path) != adapter_identity:
                raise SigningError("external signer adapter changed during signing")
            _assert_pinned_file(
                self._adapter_path,
                expected_sha256=self._adapter_sha256,
                label="signer adapter",
            )
        try:
            self._public_key.verify(signature, payload)
        except InvalidSignature:
            raise SigningError("external signer signature verification failed") from None
        self._receipts.append(
            ExternalSigningReceipt(
                key_id=self._key_id,
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                signature_sha256=hashlib.sha256(signature).hexdigest(),
                executable_sha256=self._executable_sha256,
                adapter_sha256=self._adapter_sha256,
            )
        )
        return signature

    def __repr__(self) -> str:
        return (
            "<DigestPinnedExternalSigner "
            f"key_id={self._key_id!r} executable_sha256={self._executable_sha256!r} "
            "credentials=<workload-identity:redacted>>"
        )


def _regular_pinned_path(
    value: str | os.PathLike[str],
    *,
    expected_sha256: str,
    label: str,
) -> Path:
    try:
        raw = Path(value).expanduser()
    except TypeError:
        raise ValueError(f"{label} path is invalid") from None
    if not raw.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    try:
        metadata = raw.lstat()
    except OSError:
        raise ValueError(f"{label} is unavailable") from None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size < 1
    ):
        raise ValueError(f"{label} must be a non-link regular file")
    resolved = raw.resolve(strict=True)
    try:
        _assert_pinned_file(
            resolved,
            expected_sha256=expected_sha256,
            label=label,
        )
    except SigningError as exc:
        raise ValueError(str(exc)) from None
    return resolved


def _assert_pinned_file(
    path: Path,
    *,
    expected_sha256: str,
    label: str,
) -> tuple[int, int, int, int]:
    try:
        before = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(before.st_mode)
            or bool(getattr(before, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size < 1
        ):
            raise SigningError(f"{label} is not a regular file")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _stat_identity(opened) != _stat_identity(before):
                raise SigningError(f"{label} changed while opening")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except SigningError:
        raise
    except OSError:
        raise SigningError(f"{label} is unavailable") from None
    identity = _stat_identity(before)
    if (
        _stat_identity(opened) != identity
        or _stat_identity(after) != identity
        or _stat_identity(current) != identity
    ):
        raise SigningError(f"{label} changed while hashing")
    if digest.hexdigest() != expected_sha256:
        raise SigningError(f"{label} digest does not match the protected configuration")
    return identity


def _file_identity(path: Path) -> tuple[int, int, int, int] | None:
    try:
        return _stat_identity(path.lstat())
    except OSError:
        return None


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


__all__ = ["DigestPinnedExternalSigner", "ExternalSigningReceipt"]
