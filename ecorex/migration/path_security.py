"""Filesystem identity checks shared by the one-time migration boundary.

``Path.is_symlink()`` is not sufficient on Windows: directory junctions and
other reparse points report as ordinary directories to Python 3.11.  The
migration reads user-selected legacy data, so every path is checked lexically
before canonicalisation and every opened file is matched to its pre/post
``lstat`` identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import BinaryIO

from .errors import SourceChangedError, SourceLayoutError


_CHUNK_BYTES = 1024 * 1024
_REPARSE_POINT = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


@dataclass(frozen=True, slots=True)
class PathIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    links: int
    file_attributes: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "PathIdentity":
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            mode=int(value.st_mode),
            size=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
            ctime_ns=int(value.st_ctime_ns),
            links=int(value.st_nlink),
            file_attributes=int(getattr(value, "st_file_attributes", 0)),
        )

    @property
    def is_reparse(self) -> bool:
        return bool(self.file_attributes & _REPARSE_POINT)

    @property
    def is_symlink(self) -> bool:
        return stat.S_ISLNK(self.mode)

    @property
    def is_directory(self) -> bool:
        return stat.S_ISDIR(self.mode)

    @property
    def is_regular_file(self) -> bool:
        return stat.S_ISREG(self.mode)


def lexical_absolute(path: str | os.PathLike[str]) -> Path:
    raw = Path(path).expanduser()
    return Path(os.path.abspath(os.fspath(raw)))


def _parts(path: Path) -> tuple[Path, ...]:
    absolute = lexical_absolute(path)
    anchor = Path(absolute.anchor)
    current = anchor
    values: list[Path] = [anchor]
    for part in absolute.parts[1:]:
        current = current / part
        values.append(current)
    return tuple(values)


def _same_location(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath(
            [
                os.path.normcase(os.path.abspath(os.fspath(path))),
                os.path.normcase(os.path.abspath(os.fspath(root))),
            ]
        )
    except ValueError:
        return False
    return common == os.path.normcase(os.path.abspath(os.fspath(root)))


def lstat_identity(path: Path, *, label: str) -> PathIdentity:
    try:
        return PathIdentity.from_stat(path.lstat())
    except OSError as error:
        raise SourceLayoutError(f"{label} is unavailable") from error


def reject_link_or_reparse(identity: PathIdentity, *, label: str) -> None:
    if identity.is_symlink or identity.is_reparse:
        raise SourceLayoutError(f"{label} must not be a symlink or reparse point")


def secure_directory(
    path: str | os.PathLike[str],
    *,
    label: str,
    root: Path | None = None,
) -> Path:
    """Return a canonical directory after checking every lexical component."""

    lexical = lexical_absolute(path)
    if root is not None and not is_within(lexical, lexical_absolute(root)):
        raise SourceLayoutError(f"{label} is outside the trusted root")
    for component in _parts(lexical):
        identity = lstat_identity(component, label=label)
        reject_link_or_reparse(identity, label=label)
        if not identity.is_directory:
            raise SourceLayoutError(f"{label} has a non-directory path component")
    try:
        return lexical.resolve(strict=True)
    except OSError as error:
        raise SourceLayoutError(f"{label} is unavailable") from error


def secure_regular_file(
    path: str | os.PathLike[str],
    *,
    label: str,
    root: Path | None = None,
) -> Path:
    lexical = lexical_absolute(path)
    if root is not None and not is_within(lexical, lexical_absolute(root)):
        raise SourceLayoutError(f"{label} is outside the trusted root")
    parent = secure_directory(lexical.parent, label=f"{label} parent", root=root)
    candidate = parent / lexical.name
    identity = lstat_identity(candidate, label=label)
    reject_link_or_reparse(identity, label=label)
    if not identity.is_regular_file:
        raise SourceLayoutError(f"{label} is not a regular file")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise SourceLayoutError(f"{label} is unavailable") from error
    if root is not None and not is_within(resolved, root):
        raise SourceLayoutError(f"{label} escaped the trusted root")
    return resolved


def _opened_identity(stream: BinaryIO) -> PathIdentity:
    return PathIdentity.from_stat(os.fstat(stream.fileno()))


def _same_file_object(left: PathIdentity, right: PathIdentity) -> bool:
    """Match a path and opened handle without trusting Windows permission bits."""

    return (
        left.device,
        left.inode,
        stat.S_IFMT(left.mode),
        left.size,
        left.mtime_ns,
        left.ctime_ns,
        left.links,
        left.file_attributes,
    ) == (
        right.device,
        right.inode,
        stat.S_IFMT(right.mode),
        right.size,
        right.mtime_ns,
        right.ctime_ns,
        right.links,
        right.file_attributes,
    )


def _assert_stable(
    path: Path,
    *,
    before: PathIdentity,
    opened_before: PathIdentity,
    opened_after: PathIdentity,
    label: str,
) -> None:
    after = lstat_identity(path, label=label)
    reject_link_or_reparse(after, label=label)
    if not after.is_regular_file or not (
        before == after
        and opened_before == opened_after
        and _same_file_object(before, opened_before)
    ):
        raise SourceChangedError(f"{label} changed while it was being read")


def stable_read_bytes(
    path: str | os.PathLike[str],
    *,
    label: str,
    maximum: int | None = None,
    root: Path | None = None,
) -> bytes:
    candidate = secure_regular_file(path, label=label, root=root)
    before = lstat_identity(candidate, label=label)
    if maximum is not None and not 0 <= before.size <= maximum:
        raise SourceLayoutError(f"{label} exceeds its size limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened_before = _opened_identity(stream)
            chunks: list[bytes] = []
            observed = 0
            while chunk := stream.read(_CHUNK_BYTES):
                observed += len(chunk)
                if maximum is not None and observed > maximum:
                    raise SourceLayoutError(f"{label} exceeds its size limit")
                chunks.append(chunk)
            opened_after = _opened_identity(stream)
    except (SourceLayoutError, SourceChangedError):
        raise
    except OSError as error:
        raise SourceLayoutError(f"{label} is unreadable") from error
    _assert_stable(
        candidate,
        before=before,
        opened_before=opened_before,
        opened_after=opened_after,
        label=label,
    )
    return b"".join(chunks)


def stable_sha256_file(
    path: str | os.PathLike[str],
    *,
    label: str,
    maximum: int | None = None,
    root: Path | None = None,
) -> tuple[str, PathIdentity]:
    candidate = secure_regular_file(path, label=label, root=root)
    before = lstat_identity(candidate, label=label)
    if maximum is not None and not 0 <= before.size <= maximum:
        raise SourceLayoutError(f"{label} exceeds its size limit")
    digest = hashlib.sha256()
    observed = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened_before = _opened_identity(stream)
            while chunk := stream.read(_CHUNK_BYTES):
                observed += len(chunk)
                if maximum is not None and observed > maximum:
                    raise SourceLayoutError(f"{label} exceeds its size limit")
                digest.update(chunk)
            opened_after = _opened_identity(stream)
    except (SourceLayoutError, SourceChangedError):
        raise
    except OSError as error:
        raise SourceLayoutError(f"{label} is unreadable") from error
    _assert_stable(
        candidate,
        before=before,
        opened_before=opened_before,
        opened_after=opened_after,
        label=label,
    )
    return digest.hexdigest(), before


def stable_copy_file(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    label: str,
    root: Path | None = None,
) -> None:
    candidate = secure_regular_file(source, label=label, root=root)
    before = lstat_identity(candidate, label=label)
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
        with os.fdopen(descriptor, "rb") as input_stream, output.open("xb") as stream:
            opened_before = _opened_identity(input_stream)
            while chunk := input_stream.read(_CHUNK_BYTES):
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
            opened_after = _opened_identity(input_stream)
    except (SourceLayoutError, SourceChangedError):
        raise
    except OSError as error:
        raise SourceLayoutError(f"{label} could not be copied") from error
    _assert_stable(
        candidate,
        before=before,
        opened_before=opened_before,
        opened_after=opened_after,
        label=label,
    )


def directory_identity(path: Path, *, label: str) -> PathIdentity:
    identity = lstat_identity(path, label=label)
    reject_link_or_reparse(identity, label=label)
    if not identity.is_directory:
        raise SourceLayoutError(f"{label} is not a directory")
    return identity


def assert_directory_unchanged(
    path: Path, before: PathIdentity, *, label: str
) -> None:
    after = directory_identity(path, label=label)
    if after != before:
        raise SourceChangedError(f"{label} changed while it was being inventoried")


__all__ = [
    "PathIdentity",
    "assert_directory_unchanged",
    "directory_identity",
    "is_within",
    "lexical_absolute",
    "lstat_identity",
    "reject_link_or_reparse",
    "secure_directory",
    "secure_regular_file",
    "stable_copy_file",
    "stable_read_bytes",
    "stable_sha256_file",
]
