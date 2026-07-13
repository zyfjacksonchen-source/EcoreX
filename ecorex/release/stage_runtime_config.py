"""Digest-fenced materialization for ephemeral platform-stage Runtime config."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import stat as stat_module
from typing import Any, Mapping


# GitHub limits one Actions configuration variable (and one secret) to 48 KiB.
# The protected stage transports this public config as one canonical Base64
# variable, so enforce the provider boundary locally instead of discovering it
# after a hosted job has started.
MAX_RUNTIME_CONFIG_BASE64_BYTES = 48 * 1024
MAX_RUNTIME_CONFIG_BYTES = (MAX_RUNTIME_CONFIG_BASE64_BYTES // 4) * 3
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StageRuntimeConfigError(RuntimeError):
    """A non-sensitive configuration transport failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def decode_stage_runtime_config(
    encoded: str,
    *,
    expected_sha256: str,
) -> bytes:
    """Decode, authenticate and minimally shape-check one public config."""

    if (
        not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
    ):
        raise StageRuntimeConfigError("stage_runtime_config_digest_invalid")
    if not isinstance(encoded, str) or not encoded or not encoded.isascii():
        raise StageRuntimeConfigError("stage_runtime_config_base64_invalid")
    if len(encoded) > MAX_RUNTIME_CONFIG_BASE64_BYTES:
        raise StageRuntimeConfigError("stage_runtime_config_transport_too_large")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise StageRuntimeConfigError("stage_runtime_config_base64_invalid") from None
    if not payload or len(payload) > MAX_RUNTIME_CONFIG_BYTES:
        raise StageRuntimeConfigError("stage_runtime_config_size_invalid")
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise StageRuntimeConfigError("stage_runtime_config_digest_mismatch")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise StageRuntimeConfigError("stage_runtime_config_json_invalid") from None
    if not isinstance(value, Mapping) or not isinstance(value.get("identity"), Mapping):
        raise StageRuntimeConfigError("stage_runtime_config_shape_invalid")
    return payload


def materialize_stage_runtime_config(
    output: str | os.PathLike[str],
    *,
    encoded: str,
    expected_sha256: str,
) -> dict[str, object]:
    """Create one exact private file, or accept an identical existing file."""

    path = _absolute_output(output)
    payload = decode_stage_runtime_config(encoded, expected_sha256=expected_sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        metadata = _regular_file(path)
        if metadata.st_size != len(payload) or _hash_file(path, metadata) != expected_sha256:
            raise StageRuntimeConfigError("stage_runtime_config_output_conflict")
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise StageRuntimeConfigError("stage_runtime_config_output_conflict") from None
        except OSError:
            raise StageRuntimeConfigError("stage_runtime_config_write_failed") from None
        metadata = _regular_file(path)
        if metadata.st_size != len(payload) or _hash_file(path, metadata) != expected_sha256:
            raise StageRuntimeConfigError("stage_runtime_config_write_failed")
    return {
        "schema_version": 1,
        "sha256": expected_sha256,
        "size_bytes": len(payload),
        "status": "materialized",
    }


def remove_stage_runtime_config(
    output: str | os.PathLike[str],
    *,
    expected_sha256: str | None,
) -> dict[str, object]:
    """Remove only the expected regular file; never follow an alias."""

    path = _absolute_output(output)
    if not path.exists() and not path.is_symlink():
        return {"schema_version": 1, "status": "absent"}
    if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
        raise StageRuntimeConfigError("stage_runtime_config_digest_invalid")
    metadata = _regular_file(path)
    if _hash_file(path, metadata) != expected_sha256:
        raise StageRuntimeConfigError("stage_runtime_config_cleanup_refused")
    try:
        path.unlink()
    except OSError:
        raise StageRuntimeConfigError("stage_runtime_config_cleanup_failed") from None
    return {
        "schema_version": 1,
        "sha256": expected_sha256,
        "status": "removed",
    }


def _absolute_output(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.name != "ecorex-runtime-config.json":
        raise StageRuntimeConfigError("stage_runtime_config_output_invalid")
    return path


def _regular_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        raise StageRuntimeConfigError("stage_runtime_config_output_invalid") from None
    reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        stat_module.S_ISLNK(metadata.st_mode)
        or not stat_module.S_ISREG(metadata.st_mode)
        or bool(attributes & reparse)
    ):
        raise StageRuntimeConfigError("stage_runtime_config_output_invalid")
    return metadata


def _hash_file(path: Path, before: os.stat_result) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before):
                raise StageRuntimeConfigError("stage_runtime_config_output_changed")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except StageRuntimeConfigError:
        raise
    except OSError:
        raise StageRuntimeConfigError("stage_runtime_config_output_invalid") from None
    if _identity(after) != _identity(before):
        raise StageRuntimeConfigError("stage_runtime_config_output_changed")
    return digest.hexdigest()


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


__all__ = [
    "MAX_RUNTIME_CONFIG_BASE64_BYTES",
    "MAX_RUNTIME_CONFIG_BYTES",
    "StageRuntimeConfigError",
    "decode_stage_runtime_config",
    "materialize_stage_runtime_config",
    "remove_stage_runtime_config",
]
