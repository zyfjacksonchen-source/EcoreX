"""Django-compatible password hashing without importing the Django runtime."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets
import string


PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 1_000_000
_MAX_ITERATIONS = 2_000_000
_DJANGO_SALT = re.compile(r"^[A-Za-z0-9]{8,64}$")
_ALPHABET = string.ascii_letters + string.digits
_LEGACY_ECOREX_ITERATIONS = 180_000


class PasswordCredentialError(ValueError):
    """The supplied password or encoded credential is outside the contract."""


@dataclass(frozen=True, slots=True)
class PasswordCredentialMetadata:
    format: str
    iterations: int
    salt: bytes
    digest: bytes

    @property
    def needs_upgrade(self) -> bool:
        return (
            self.format != "django"
            or self.iterations != PBKDF2_ITERATIONS
        )


def encode_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    if not _valid_new_password(password) or not 100_000 <= iterations <= _MAX_ITERATIONS:
        raise PasswordCredentialError("password credential is invalid")
    return _encode_verified_password(password, iterations=iterations)


def _encode_verified_password(
    password: str,
    *,
    iterations: int = PBKDF2_ITERATIONS,
) -> str:
    if (
        not _valid_login_password(password)
        or not 100_000 <= iterations <= _MAX_ITERATIONS
    ):
        raise PasswordCredentialError("password credential is invalid")
    salt = "".join(secrets.choice(_ALPHABET) for _ in range(22))
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        iterations,
    )
    return (
        f"{PBKDF2_ALGORITHM}${iterations}${salt}$"
        + base64.b64encode(derived).decode("ascii")
    )


def validate_encoded_password(encoded: str) -> str:
    parse_encoded_password(encoded)
    return encoded


def verify_password(password: str, encoded: str) -> bool:
    verified, _replacement = verify_password_and_upgrade(password, encoded)
    return verified


def verify_password_and_upgrade(
    password: str,
    encoded: str,
) -> tuple[bool, str | None]:
    """Verify a current or released legacy hash and optionally return a rehash.

    Historical authentication accepts the v0.2.9.2 eight-character floor.
    New credentials keep the stronger ten-character product contract.
    """

    if not _valid_login_password(password):
        _perform_dummy_work("EcoreXInvalidInput")
        return False, None
    try:
        metadata = parse_encoded_password(encoded)
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            metadata.salt,
            metadata.iterations,
        )
        _perform_cost_padding(password, completed_iterations=metadata.iterations)
    except PasswordCredentialError:
        _perform_dummy_work(password)
        return False
    if not hmac.compare_digest(derived, metadata.digest):
        return False, None
    replacement = (
        _encode_verified_password(password) if metadata.needs_upgrade else None
    )
    return True, replacement


def parse_encoded_password(encoded: str) -> PasswordCredentialMetadata:
    algorithm, iterations_text, salt_text, digest_text = _parts(encoded)
    if algorithm != PBKDF2_ALGORITHM or not iterations_text.isdigit():
        raise PasswordCredentialError("password credential is invalid")
    iterations = int(iterations_text)
    if not 100_000 <= iterations <= _MAX_ITERATIONS:
        raise PasswordCredentialError("password credential is invalid")
    digest = _strict_base64(digest_text, expected_bytes=32)

    if iterations == _LEGACY_ECOREX_ITERATIONS:
        try:
            salt = _strict_base64(salt_text, expected_bytes=16)
        except PasswordCredentialError:
            pass
        else:
            return PasswordCredentialMetadata(
                format="ecorex-v0.2.9.2",
                iterations=iterations,
                salt=salt,
                digest=digest,
            )
    if _DJANGO_SALT.fullmatch(salt_text) is None:
        raise PasswordCredentialError("password credential is invalid")
    return PasswordCredentialMetadata(
        format="django",
        iterations=iterations,
        salt=salt_text.encode("ascii"),
        digest=digest,
    )


def _parts(encoded: str) -> tuple[str, str, str, str]:
    if not isinstance(encoded, str) or not 60 <= len(encoded) <= 256:
        raise PasswordCredentialError("password credential is invalid")
    parts = encoded.split("$")
    if len(parts) != 4:
        raise PasswordCredentialError("password credential is invalid")
    return parts[0], parts[1], parts[2], parts[3]


def _strict_base64(value: str, *, expected_bytes: int) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        raise PasswordCredentialError("password credential is invalid") from None
    if (
        len(decoded) != expected_bytes
        or base64.b64encode(decoded).decode("ascii") != value
    ):
        raise PasswordCredentialError("password credential is invalid")
    return decoded


def _valid_login_password(password: str) -> bool:
    return (
        isinstance(password, str)
        and 8 <= len(password) <= 256
        and "\x00" not in password
    )


def _valid_new_password(password: str) -> bool:
    return _valid_login_password(password) and len(password) >= 10


# Missing accounts perform the same bounded PBKDF2 work as existing accounts.
_DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$1000000$EcoreXDummySalt2026$"
    "8x46OigQcuO4YM7vnWx8T06ZKe7BEI+/orJ0FfcBuvg="
)


def dummy_password_hash() -> str:
    return _DUMMY_PASSWORD_HASH


def _perform_dummy_work(password: str) -> None:
    metadata = parse_encoded_password(_DUMMY_PASSWORD_HASH)
    hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        metadata.salt,
        metadata.iterations,
    )


def _perform_cost_padding(password: str, *, completed_iterations: int) -> None:
    remaining = PBKDF2_ITERATIONS - completed_iterations
    if remaining <= 0:
        return
    hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        b"EcoreXPasswordCostPadding",
        remaining,
    )


__all__ = [
    "PBKDF2_ALGORITHM",
    "PBKDF2_ITERATIONS",
    "PasswordCredentialError",
    "PasswordCredentialMetadata",
    "dummy_password_hash",
    "encode_password",
    "parse_encoded_password",
    "validate_encoded_password",
    "verify_password",
    "verify_password_and_upgrade",
]
