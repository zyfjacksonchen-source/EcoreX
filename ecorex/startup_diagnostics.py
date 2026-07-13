"""Safe, one-shot Runtime startup diagnostics.

The signed Bootstrap deliberately does not retain a child Runtime's raw stdout
or stderr: provider and platform failures may include credentials, local paths,
or other sensitive details.  A fixed, nonce-bound stage code is enough to make
an activation or startup failure diagnosable without weakening that boundary.

The helpers in this module are advisory only.  Bootstrap must never make a
trust, rollback, or activation decision from their output.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Mapping


STARTUP_DIAGNOSTIC_TOKEN_ENV = "ECOREX_RUNTIME_STARTUP_DIAGNOSTIC_TOKEN"
STARTUP_DIAGNOSTIC_DIRECTORY = ".runtime-startup"
STARTUP_DIAGNOSTIC_SCHEMA_VERSION = 1
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_STAGE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_BYTES = 512


def issue_startup_diagnostic_token() -> str:
    """Return an opaque, fixed-width token suitable for one Runtime launch."""

    return secrets.token_urlsafe(32)


def prepare_startup_diagnostic_directory(
    install_root: str | os.PathLike[str],
) -> bool:
    """Create the fixed advisory directory only when it is a real directory."""

    try:
        root = Path(install_root).resolve(strict=True)
        directory = root / STARTUP_DIAGNOSTIC_DIRECTORY
        directory.mkdir(mode=0o700, exist_ok=True)
        metadata = directory.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def write_runtime_startup_diagnostic(
    stage: str,
    *,
    environment: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> bool:
    """Best-effort write of a fixed safe stage emitted by the child Runtime.

    The caller supplies neither a filename nor an install root.  Both are
    derived from the canonical ``slots/<slot>/payload`` current directory and
    the Bootstrap-issued token, so a user-controlled environment cannot select
    an arbitrary write target.
    """

    if _STAGE.fullmatch(stage) is None:
        return False
    source = os.environ if environment is None else environment
    token = source.get(STARTUP_DIAGNOSTIC_TOKEN_ENV)
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        return False
    try:
        payload = Path(cwd if cwd is not None else os.getcwd()).resolve(strict=True)
        slot = payload.parent
        slots = slot.parent
        if payload.name != "payload" or slots.name != "slots":
            return False
        root = slots.parent
        directory = root / STARTUP_DIAGNOSTIC_DIRECTORY
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            return False
        target = directory / f"{token}.json"
        target.relative_to(directory)
        payload_bytes = json.dumps(
            {
                "schema_version": STARTUP_DIAGNOSTIC_SCHEMA_VERSION,
                "stage": stage,
                "token": token,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload_bytes) > _MAX_BYTES:
            return False
        with target.open("xb") as stream:
            stream.write(payload_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        return True
    except (OSError, ValueError):
        return False


def read_startup_diagnostic(
    install_root: str | os.PathLike[str], token: str | None
) -> str | None:
    """Consume one safe stage code, treating all malformed data as absent."""

    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        return None
    target: Path | None = None
    try:
        root = Path(install_root).resolve(strict=True)
        directory = root / STARTUP_DIAGNOSTIC_DIRECTORY
        target = directory / f"{token}.json"
        target.relative_to(directory)
        metadata = target.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_size < 1
            or metadata.st_size > _MAX_BYTES
        ):
            return None
        raw = target.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "stage", "token"}
            or value.get("schema_version") != STARTUP_DIAGNOSTIC_SCHEMA_VERSION
            or value.get("token") != token
            or not isinstance(value.get("stage"), str)
            or _STAGE.fullmatch(value["stage"]) is None
        ):
            return None
        return value["stage"]
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    finally:
        if target is not None:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "STARTUP_DIAGNOSTIC_DIRECTORY",
    "STARTUP_DIAGNOSTIC_TOKEN_ENV",
    "issue_startup_diagnostic_token",
    "prepare_startup_diagnostic_directory",
    "read_startup_diagnostic",
    "write_runtime_startup_diagnostic",
]
