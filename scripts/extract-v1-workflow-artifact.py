#!/usr/bin/env python3
"""Fail-closed SHA-256 verification and extraction of one Actions artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import zipfile


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ROOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MAX_ARCHIVE_BYTES = 10 * 1024 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 12 * 1024 * 1024 * 1024
_MAX_MEMBERS = 50_000
_DISK_HEADROOM_BYTES = 512 * 1024 * 1024


class ArtifactExtractionError(ValueError):
    pass


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        stat.S_IFMT(value.st_mode),
    )


def _directory_identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)


def _regular(value: os.stat_result) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and not stat.S_ISLNK(value.st_mode)
        and not bool(getattr(value, "st_file_attributes", 0) & _REPARSE_POINT)
    )


def _archive(
    path: Path, expected_sha256: str
) -> tuple[Path, tuple[int, int, int, int, int, int]]:
    absolute = Path(os.path.abspath(path.expanduser()))
    try:
        before = absolute.lstat()
        if (
            not _regular(before)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= _MAX_ARCHIVE_BYTES
        ):
            raise ArtifactExtractionError("workflow_artifact_archive_invalid")
        digest = hashlib.sha256()
        with absolute.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before) or not _regular(opened):
                raise ArtifactExtractionError("workflow_artifact_archive_changed")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = absolute.lstat()
    except ArtifactExtractionError:
        raise
    except OSError:
        raise ArtifactExtractionError("workflow_artifact_archive_invalid") from None
    if (
        _identity(after) != _identity(before)
        or _identity(current) != _identity(before)
        or not _regular(current)
        or digest.hexdigest() != expected_sha256
    ):
        raise ArtifactExtractionError("workflow_artifact_digest_mismatch")
    return absolute, _identity(before)


def _member_path(name: str) -> tuple[PurePosixPath, bool]:
    if (
        not isinstance(name, str)
        or not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name) is not None
    ):
        raise ArtifactExtractionError("workflow_artifact_member_invalid")
    directory = name.endswith("/")
    normalized = name[:-1] if directory else name
    path = PurePosixPath(normalized)
    if (
        not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.is_absolute()
    ):
        raise ArtifactExtractionError("workflow_artifact_member_invalid")
    return path, directory


def _safe_kind(info: zipfile.ZipInfo, *, directory: bool) -> None:
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ArtifactExtractionError("workflow_artifact_member_type_forbidden")
    if directory and kind == stat.S_IFREG:
        raise ArtifactExtractionError("workflow_artifact_member_type_forbidden")
    if not directory and kind == stat.S_IFDIR:
        raise ArtifactExtractionError("workflow_artifact_member_type_forbidden")


def extract_workflow_artifact(
    archive: Path,
    *,
    expected_sha256: str,
    output: Path,
    required_roots: tuple[str, ...],
) -> dict[str, object]:
    if (
        _SHA256.fullmatch(expected_sha256) is None
        or not required_roots
        or len(set(required_roots)) != len(required_roots)
        or any(_SAFE_ROOT.fullmatch(item) is None for item in required_roots)
    ):
        raise ArtifactExtractionError("workflow_artifact_invocation_invalid")
    source, source_identity = _archive(archive, expected_sha256)
    destination = Path(os.path.abspath(output.expanduser()))
    if os.path.lexists(destination):
        raise ArtifactExtractionError("workflow_artifact_output_exists")
    try:
        parent = destination.parent
        parent_metadata = parent.lstat()
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or stat.S_ISLNK(parent_metadata.st_mode)
            or bool(
                getattr(parent_metadata, "st_file_attributes", 0) & _REPARSE_POINT
            )
        ):
            raise ArtifactExtractionError("workflow_artifact_output_invalid")
        destination.mkdir(mode=0o700)
        destination_identity = _directory_identity(destination.lstat())
    except ArtifactExtractionError:
        raise
    except OSError:
        raise ArtifactExtractionError("workflow_artifact_output_invalid") from None

    files = 0
    total = 0
    observed: set[str] = set()
    roots: set[str] = set()
    try:
        with source.open("rb") as archive_stream:
            opened = os.fstat(archive_stream.fileno())
            if _identity(opened) != source_identity or not _regular(opened):
                raise ArtifactExtractionError("workflow_artifact_archive_changed")
            with zipfile.ZipFile(archive_stream, "r") as bundle:
                members = bundle.infolist()
                if not 1 <= len(members) <= _MAX_MEMBERS:
                    raise ArtifactExtractionError("workflow_artifact_member_count_invalid")
                prepared: list[tuple[zipfile.ZipInfo, PurePosixPath, bool]] = []
                for info in members:
                    member, directory = _member_path(info.filename)
                    canonical = member.as_posix().casefold()
                    if canonical in observed:
                        raise ArtifactExtractionError("workflow_artifact_member_duplicate")
                    observed.add(canonical)
                    roots.add(member.parts[0])
                    _safe_kind(info, directory=directory)
                    if info.file_size < 0 or info.compress_size < 0:
                        raise ArtifactExtractionError("workflow_artifact_member_invalid")
                    total += info.file_size
                    if total > _MAX_UNCOMPRESSED_BYTES:
                        raise ArtifactExtractionError("workflow_artifact_uncompressed_limit")
                    prepared.append((info, member, directory))
                try:
                    free = shutil.disk_usage(destination.parent).free
                except OSError:
                    raise ArtifactExtractionError(
                        "workflow_artifact_disk_check_failed"
                    ) from None
                if free < total + _DISK_HEADROOM_BYTES:
                    raise ArtifactExtractionError(
                        "workflow_artifact_disk_space_insufficient"
                    )
                for info, member, directory in prepared:
                    target = destination.joinpath(*member.parts)
                    if directory:
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with bundle.open(info, "r") as source_stream, target.open(
                        "xb"
                    ) as output_stream:
                        while chunk := source_stream.read(1024 * 1024):
                            written += len(chunk)
                            if written > info.file_size:
                                raise ArtifactExtractionError(
                                    "workflow_artifact_member_changed"
                                )
                            output_stream.write(chunk)
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
                    if written != info.file_size:
                        raise ArtifactExtractionError(
                            "workflow_artifact_member_changed"
                        )
                    files += 1
            if _identity(os.fstat(archive_stream.fileno())) != source_identity:
                raise ArtifactExtractionError("workflow_artifact_archive_changed")
    except ArtifactExtractionError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise ArtifactExtractionError("workflow_artifact_zip_invalid") from None
    try:
        final_destination_identity = _directory_identity(destination.lstat())
        final_source_identity = _identity(source.lstat())
    except OSError:
        raise ArtifactExtractionError("workflow_artifact_archive_changed") from None
    if (
        final_destination_identity != destination_identity
        or final_source_identity != source_identity
        or roots != set(required_roots)
        or files < 1
    ):
        raise ArtifactExtractionError("workflow_artifact_root_set_invalid")
    return {
        "archive_sha256": expected_sha256,
        "archive_size_bytes": source.stat().st_size,
        "member_count": len(observed),
        "file_count": files,
        "uncompressed_size_bytes": total,
        "roots": sorted(roots),
    }


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="extract-v1-workflow-artifact")
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--required-root", action="append", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = extract_workflow_artifact(
            args.archive,
            expected_sha256=args.expected_sha256,
            output=args.output,
            required_roots=tuple(args.required_root),
        )
        receipt = Path(os.path.abspath(args.receipt.expanduser()))
        receipt.parent.mkdir(parents=True, exist_ok=True)
        with receipt.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except (ArtifactExtractionError, OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, ArtifactExtractionError) else "workflow_artifact_extraction_failed"
        print(json.dumps({"ok": False, "error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
