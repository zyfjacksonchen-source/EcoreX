"""Resolve the signed, relocatable Python used only for zipapp Packs.

The product Runtime executable may be a launcher or a frozen binary, so
``sys.executable`` is never a valid implicit Pack interpreter.  The platform
stager records one fixed interpreter and the complete closure digest inside
``pack-python.json`` in the signed Core payload.  Runtime revalidates that
contract without PATH lookup, symlink following or fallback.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping

from ecorex.update.manifest import portable_path_segment_key


PACK_PYTHON_MANIFEST = "pack-python.json"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CLOSURE_FILES = 50_000
MAX_CLOSURE_BYTES = 1024 * 1024 * 1024
MAX_CLOSURE_SCAN_WORKERS = 16
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TARGETS = frozenset(
    {("windows", "x64"), ("macos", "arm64"), ("macos", "x64")}
)


class PackPythonError(RuntimeError):
    """Stable, non-path-bearing Pack interpreter trust failure."""

    def __init__(self, code: str) -> None:
        self.code = code if re.fullmatch(r"[a-z][a-z0-9_]{2,127}", code) else "pack_python_invalid"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class PackPythonIdentity:
    platform: str
    architecture: str
    relative_path: str
    size_bytes: int
    sha256: str
    closure_file_count: int
    closure_size_bytes: int
    closure_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "architecture": self.architecture,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "closure_file_count": self.closure_file_count,
            "closure_size_bytes": self.closure_size_bytes,
            "closure_sha256": self.closure_sha256,
        }


def expected_pack_python_path(platform: str) -> str:
    if platform == "windows":
        return "bin/pack-python/python.exe"
    if platform == "macos":
        return "bin/pack-python/bin/python3"
    raise PackPythonError("pack_python_target_unsupported")


def resolve_pack_python(
    payload_root: str | os.PathLike[str],
    *,
    platform: str,
    architecture: str,
) -> tuple[Path, PackPythonIdentity]:
    if (platform, architecture) not in _TARGETS:
        raise PackPythonError("pack_python_target_unsupported")
    root = _trusted_directory(Path(payload_root), code="pack_python_payload_invalid")
    manifest_path = root / PACK_PYTHON_MANIFEST
    payload = _stable_regular_bytes(
        manifest_path,
        maximum=MAX_MANIFEST_BYTES,
        code="pack_python_manifest_invalid",
    )
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise PackPythonError("pack_python_manifest_invalid") from None
    expected_keys = {
        "schema_version",
        "platform",
        "architecture",
        "relative_path",
        "size_bytes",
        "sha256",
        "closure_file_count",
        "closure_size_bytes",
        "closure_sha256",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected_keys
        or value.get("schema_version") != 1
        or value.get("platform") != platform
        or value.get("architecture") != architecture
        or value.get("relative_path") != expected_pack_python_path(platform)
        or isinstance(value.get("size_bytes"), bool)
        or not isinstance(value.get("size_bytes"), int)
        or not 1 <= value["size_bytes"] <= MAX_CLOSURE_BYTES
        or _SHA256.fullmatch(str(value.get("sha256"))) is None
        or isinstance(value.get("closure_file_count"), bool)
        or not isinstance(value.get("closure_file_count"), int)
        or not 1 <= value["closure_file_count"] <= MAX_CLOSURE_FILES
        or isinstance(value.get("closure_size_bytes"), bool)
        or not isinstance(value.get("closure_size_bytes"), int)
        or not 1 <= value["closure_size_bytes"] <= MAX_CLOSURE_BYTES
        or _SHA256.fullmatch(str(value.get("closure_sha256"))) is None
        or payload
        != json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ):
        raise PackPythonError("pack_python_manifest_invalid")
    relative = PurePosixPath(value["relative_path"])
    executable = _contained_path(root, relative)
    executable_payload = _stable_regular_bytes(
        executable,
        maximum=MAX_CLOSURE_BYTES,
        code="pack_python_interpreter_invalid",
    )
    if (
        len(executable_payload) != value["size_bytes"]
        or hashlib.sha256(executable_payload).hexdigest() != value["sha256"]
    ):
        raise PackPythonError("pack_python_interpreter_digest_mismatch")
    if platform == "macos" and not executable.stat().st_mode & stat.S_IXUSR:
        raise PackPythonError("pack_python_interpreter_not_executable")
    closure_root = _contained_path(root, PurePosixPath("bin/pack-python"))
    tree = scan_pack_python_closure(closure_root)
    if (
        tree["file_count"] != value["closure_file_count"]
        or tree["size_bytes"] != value["closure_size_bytes"]
        or tree["sha256"] != value["closure_sha256"]
    ):
        raise PackPythonError("pack_python_closure_mismatch")
    identity = PackPythonIdentity(
        platform=platform,
        architecture=architecture,
        relative_path=value["relative_path"],
        size_bytes=value["size_bytes"],
        sha256=value["sha256"],
        closure_file_count=value["closure_file_count"],
        closure_size_bytes=value["closure_size_bytes"],
        closure_sha256=value["closure_sha256"],
    )
    return executable, identity


def build_pack_python_manifest(
    payload_root: str | os.PathLike[str],
    *,
    platform: str,
    architecture: str,
) -> bytes:
    """Build canonical manifest bytes for the platform stager."""

    if (platform, architecture) not in _TARGETS:
        raise PackPythonError("pack_python_target_unsupported")
    root = _trusted_directory(Path(payload_root), code="pack_python_payload_invalid")
    relative = expected_pack_python_path(platform)
    executable = _contained_path(root, PurePosixPath(relative))
    payload = _stable_regular_bytes(
        executable,
        maximum=MAX_CLOSURE_BYTES,
        code="pack_python_interpreter_invalid",
    )
    tree = scan_pack_python_closure(
        _contained_path(root, PurePosixPath("bin/pack-python"))
    )
    value = {
        "schema_version": 1,
        "platform": platform,
        "architecture": architecture,
        "relative_path": relative,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "closure_file_count": tree["file_count"],
        "closure_size_bytes": tree["size_bytes"],
        "closure_sha256": tree["sha256"],
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def scan_pack_python_closure(root: Path) -> Mapping[str, Any]:
    directory = _trusted_directory(root, code="pack_python_closure_invalid")
    candidates: list[tuple[Path, str]] = []
    pending = [directory]
    seen: set[str] = set()
    declared_total = 0
    while pending:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name.casefold(), reverse=True)
        except OSError:
            raise PackPythonError("pack_python_closure_invalid") from None
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(directory).as_posix()
            pure = PurePosixPath(relative)
            if any(part in {"", ".", ".."} or ":" in part for part in pure.parts):
                raise PackPythonError("pack_python_closure_invalid")
            collision = "/".join(portable_path_segment_key(part) for part in pure.parts)
            if collision in seen:
                raise PackPythonError("pack_python_closure_collision")
            seen.add(collision)
            try:
                metadata = path.lstat()
            except OSError:
                raise PackPythonError("pack_python_closure_invalid") from None
            if _is_link_or_reparse(metadata):
                raise PackPythonError("pack_python_closure_link_refused")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise PackPythonError("pack_python_closure_invalid")
            if len(candidates) >= MAX_CLOSURE_FILES:
                raise PackPythonError("pack_python_closure_file_limit")
            if metadata.st_size > MAX_CLOSURE_BYTES:
                raise PackPythonError("pack_python_closure_invalid")
            declared_total += metadata.st_size
            if declared_total > MAX_CLOSURE_BYTES:
                raise PackPythonError("pack_python_closure_size_limit")
            candidates.append((path, relative))
    if not candidates:
        raise PackPythonError("pack_python_closure_invalid")
    workers = min(MAX_CLOSURE_SCAN_WORKERS, len(candidates))
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="ecorex-pack-verify",
    ) as executor:
        records = list(executor.map(_closure_file_record, candidates))
    total = sum(int(record["size_bytes"]) for record in records)
    if total != declared_total or total > MAX_CLOSURE_BYTES:
        raise PackPythonError("pack_python_closure_size_limit")
    records.sort(key=lambda item: item["path"])
    digest = hashlib.sha256(
        b"ecorex-pack-python-v1\n"
        + json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    ).hexdigest()
    return {"file_count": len(records), "size_bytes": total, "sha256": digest}


def _closure_file_record(candidate: tuple[Path, str]) -> dict[str, Any]:
    path, relative = candidate
    size, digest = _stable_regular_digest(
        path,
        maximum=MAX_CLOSURE_BYTES,
        code="pack_python_closure_invalid",
        minimum=0,
    )
    return {"path": relative, "size_bytes": size, "sha256": digest}


def _contained_path(root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise PackPythonError("pack_python_path_invalid")
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError:
            raise PackPythonError("pack_python_path_missing") from None
        if _is_link_or_reparse(metadata):
            raise PackPythonError("pack_python_path_link_refused")
    try:
        current.absolute().relative_to(root.absolute())
    except ValueError:
        raise PackPythonError("pack_python_path_escape") from None
    return current


def _trusted_directory(path: Path, *, code: str) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        metadata = absolute.lstat()
    except (OSError, TypeError, ValueError):
        raise PackPythonError(code) from None
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise PackPythonError(code)
    return absolute


def _stable_regular_bytes(
    path: Path,
    *,
    maximum: int,
    code: str,
    minimum: int = 1,
) -> bytes:
    try:
        before = path.lstat()
        if (
            _is_link_or_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or not minimum <= before.st_size <= maximum
        ):
            raise PackPythonError(code)
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except PackPythonError:
        raise
    except OSError:
        raise PackPythonError(code) from None
    identity = _stat_identity(before)
    path_identity = _path_identity(before)
    if (
        _stat_identity(opened) != identity
        or _stat_identity(after) != identity
        or _path_identity(current) != path_identity
        or len(payload) != before.st_size
    ):
        raise PackPythonError(code)
    return payload


def _stable_regular_digest(
    path: Path,
    *,
    maximum: int,
    code: str,
    minimum: int = 1,
) -> tuple[int, str]:
    try:
        before = path.lstat()
        if (
            _is_link_or_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or not minimum <= before.st_size <= maximum
        ):
            raise PackPythonError(code)
        digest = hashlib.sha256()
        observed_size = 0
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            while chunk := stream.read(1024 * 1024):
                observed_size += len(chunk)
                if observed_size > maximum:
                    raise PackPythonError(code)
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except PackPythonError:
        raise
    except OSError:
        raise PackPythonError(code) from None
    identity = _stat_identity(before)
    path_identity = _path_identity(before)
    if (
        _stat_identity(opened) != identity
        or _stat_identity(after) != identity
        or _path_identity(current) != path_identity
        or observed_size != before.st_size
    ):
        raise PackPythonError(code)
    return observed_size, digest.hexdigest()


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _path_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        *_stat_identity(metadata),
        metadata.st_mode,
        int(getattr(metadata, "st_file_attributes", 0)),
    )


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


__all__ = [
    "PACK_PYTHON_MANIFEST",
    "PackPythonError",
    "PackPythonIdentity",
    "build_pack_python_manifest",
    "expected_pack_python_path",
    "resolve_pack_python",
    "scan_pack_python_closure",
]
