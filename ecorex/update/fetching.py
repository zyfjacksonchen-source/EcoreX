"""Artifact fetch boundary.

There is deliberately no HTTP implementation in the core update package.
Production networking belongs to a separately configured capability, while
tests and offline recovery use :class:`LocalSourceFetcher` with no network
side effects.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from .manifest import ReleaseArtifact, ReleaseSource, SourceKind


class FetchError(RuntimeError):
    pass


@runtime_checkable
class ArtifactFetcher(Protocol):
    def fetch(
        self,
        source: ReleaseSource,
        artifact: ReleaseArtifact,
        destination: Path,
        *,
        resume_from: int,
        max_bytes: int,
    ) -> None:
        """Append from ``resume_from`` without ever growing beyond ``max_bytes``."""

        ...


class LocalSourceFetcher:
    """Fetch release artifacts from explicit local source directories."""

    def __init__(
        self,
        source_directories: Mapping[str | SourceKind, str | os.PathLike[str]],
        *,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self._directories = {
            key.value if isinstance(key, SourceKind) else key: Path(value)
            for key, value in source_directories.items()
        }
        for directory in self._directories.values():
            if not directory.is_dir():
                raise ValueError(f"local release source is not a directory: {directory}")
            metadata = directory.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag):
                raise ValueError("local release source cannot be a link or reparse point")
        self.chunk_size = chunk_size

    def fetch(
        self,
        source: ReleaseSource,
        artifact: ReleaseArtifact,
        destination: Path,
        *,
        resume_from: int,
        max_bytes: int,
    ) -> None:
        directory = self._directories.get(source.source_id)
        if directory is None:
            directory = self._directories.get(source.kind.value)
        if directory is None:
            raise FetchError(f"no local directory configured for source {source.source_id!r}")
        origin = directory / artifact.file_name
        try:
            metadata = origin.lstat()
            origin_size = metadata.st_size
        except OSError as exc:
            raise FetchError(
                f"local source {source.source_id!r} does not contain {artifact.file_name!r}"
            ) from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(attributes & reparse_flag)
        ):
            raise FetchError(f"local source artifact is not a regular file: {origin}")
        if max_bytes != artifact.size_bytes:
            raise FetchError("fetch limit must match the signed artifact size")
        if origin_size != artifact.size_bytes or origin_size > max_bytes:
            raise FetchError(
                f"local source artifact size is {origin_size}, expected {artifact.size_bytes}"
            )
        if resume_from < 0 or resume_from > origin_size:
            raise FetchError(
                f"resume offset {resume_from} is outside local artifact size {origin_size}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(destination):
            destination_metadata = destination.lstat()
            destination_attributes = getattr(destination_metadata, "st_file_attributes", 0)
            if (
                not stat.S_ISREG(destination_metadata.st_mode)
                or stat.S_ISLNK(destination_metadata.st_mode)
                or bool(destination_attributes & reparse_flag)
            ):
                raise FetchError("download destination must be a regular file")
            if destination_metadata.st_size != resume_from:
                raise FetchError("download destination size does not match resume offset")
        elif resume_from:
            raise FetchError("cannot resume a missing download destination")
        mode = "ab" if resume_from else "wb"
        with origin.open("rb") as source_stream, destination.open(mode) as target_stream:
            source_stream.seek(resume_from)
            while chunk := source_stream.read(self.chunk_size):
                target_stream.write(chunk)
            target_stream.flush()
            os.fsync(target_stream.fileno())
