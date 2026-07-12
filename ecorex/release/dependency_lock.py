"""Fail-closed identity for the repository-owned Python dependency lock set."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping


LOCK_MANIFEST_SCHEMA_VERSION = 1
LOCK_TYPE = "ecorex-python-hash-lock-set"
REQUIRED_PROFILES = frozenset(
    {"bootstrap", "cloud", "dev", "platform-stage", "runtime"}
)
_SAFE_FILE = re.compile(r"^[a-z][a-z0-9-]{0,31}\.(?:in|lock)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_LOCK_BYTES = 2 * 1024 * 1024


class DependencyLockError(ValueError):
    """A stable, non-sensitive dependency lock validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DependencyLockManifest:
    path: Path
    sha256: str
    profiles: Mapping[str, Mapping[str, str]]


def load_dependency_lock_manifest(
    path: str | os.PathLike[str],
) -> DependencyLockManifest:
    """Validate the manifest and every referenced input/lock byte sequence."""

    manifest = _regular_file(Path(path), max_bytes=_MAX_MANIFEST_BYTES)
    payload = _stable_bytes(manifest, max_bytes=_MAX_MANIFEST_BYTES)
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise DependencyLockError("dependency_lock_manifest_invalid") from None
    if not isinstance(value, dict) or set(value) != {
        "generator",
        "lock_type",
        "profiles",
        "python",
        "schema_version",
    }:
        raise DependencyLockError("dependency_lock_manifest_invalid")
    generator = value.get("generator")
    if (
        value.get("schema_version") != LOCK_MANIFEST_SCHEMA_VERSION
        or value.get("lock_type") != LOCK_TYPE
        or value.get("python") != "3.11.9"
        or not isinstance(generator, dict)
        or set(generator) != {"hashes", "index", "name", "universal", "version"}
        or generator.get("name") != "uv"
        or generator.get("version") != "0.11.7"
        or generator.get("index") != "https://pypi.org/simple"
        or generator.get("universal") is not True
        or generator.get("hashes") is not True
    ):
        raise DependencyLockError("dependency_lock_manifest_unsupported")
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, list) or len(raw_profiles) != len(REQUIRED_PROFILES):
        raise DependencyLockError("dependency_lock_profile_set_invalid")
    root = manifest.parent.resolve(strict=True)
    observed: dict[str, Mapping[str, str]] = {}
    for raw in raw_profiles:
        if not isinstance(raw, dict) or set(raw) != {
            "input",
            "input_sha256",
            "lock",
            "lock_sha256",
            "profile",
        }:
            raise DependencyLockError("dependency_lock_profile_invalid")
        profile = raw.get("profile")
        input_name = raw.get("input")
        lock_name = raw.get("lock")
        if (
            profile not in REQUIRED_PROFILES
            or profile in observed
            or input_name != f"{profile}.in"
            or lock_name != f"{profile}.lock"
            or not isinstance(input_name, str)
            or not isinstance(lock_name, str)
            or _SAFE_FILE.fullmatch(input_name) is None
            or _SAFE_FILE.fullmatch(lock_name) is None
            or _SHA256.fullmatch(str(raw.get("input_sha256"))) is None
            or _SHA256.fullmatch(str(raw.get("lock_sha256"))) is None
        ):
            raise DependencyLockError("dependency_lock_profile_invalid")
        input_path = _contained_regular_file(root, input_name)
        lock_path = _contained_regular_file(root, lock_name)
        if (
            _sha256(_stable_bytes(input_path, max_bytes=_MAX_LOCK_BYTES))
            != raw["input_sha256"]
            or _sha256(_stable_bytes(lock_path, max_bytes=_MAX_LOCK_BYTES))
            != raw["lock_sha256"]
        ):
            raise DependencyLockError("dependency_lock_digest_mismatch")
        observed[profile] = MappingProxyType(
            {
                "input": input_name,
                "input_sha256": raw["input_sha256"],
                "lock": lock_name,
                "lock_sha256": raw["lock_sha256"],
            }
        )
    if frozenset(observed) != REQUIRED_PROFILES:
        raise DependencyLockError("dependency_lock_profile_set_invalid")
    return DependencyLockManifest(
        path=manifest,
        sha256=_sha256(payload),
        profiles=MappingProxyType(dict(sorted(observed.items()))),
    )


def _contained_regular_file(root: Path, name: str) -> Path:
    path = _regular_file(root / name, max_bytes=_MAX_LOCK_BYTES)
    try:
        path.relative_to(root)
    except ValueError:
        raise DependencyLockError("dependency_lock_path_invalid") from None
    return path


def _regular_file(path: Path, *, max_bytes: int) -> Path:
    try:
        metadata = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(metadata.st_mode)
            or not 1 <= metadata.st_size <= max_bytes
        ):
            raise DependencyLockError("dependency_lock_file_invalid")
        return path.resolve(strict=True)
    except DependencyLockError:
        raise
    except OSError:
        raise DependencyLockError("dependency_lock_file_invalid") from None


def _stable_bytes(path: Path, *, max_bytes: int) -> bytes:
    try:
        before = path.stat()
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
        current = path.stat()
    except OSError:
        raise DependencyLockError("dependency_lock_file_unreadable") from None
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        len(payload) != before.st_size
        or len(payload) > max_bytes
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != identity
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != identity
        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        != identity
    ):
        raise DependencyLockError("dependency_lock_file_changed")
    return payload


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value
