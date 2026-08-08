#!/usr/bin/env python3
"""Fail when predecessor product branding enters an e-Mate product tree."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import io
import json
import mmap
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
import sys
from typing import Iterable
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS = (
    ROOT / "ecorex",
    ROOT / "desktop/src",
    ROOT / "desktop/electron",
    ROOT / "desktop/dist",
)
_SKIP_DIRECTORIES = frozenset(
    {
        ".candidate",
        ".git",
        ".pytest_cache",
        "__pycache__",
        "coverage",
        "docs",
        "node_modules",
    }
)
_ALLOWED_DIRECTORIES = frozenset({"fixtures", "migration", "tests"})
_NOTICE_NAMES = frozenset(
    {
        "notice",
        "notice.md",
        "notice.txt",
        "third-party-notices",
        "third-party-notices.md",
        "third-party-notices.txt",
        "third_party_notices.md",
        "third_party_notices.txt",
    }
)
_TEXT_PATTERNS = (
    ("predecessor-product", ("C" + "ow" + "Agent").encode("ascii")),
    ("predecessor-product-spaced", ("C" + "ow" + " Agent").encode("ascii")),
    ("predecessor-domain", ("c" + "owagent.ai").encode("ascii")),
)
_STANDALONE = re.compile(
    rb"(?<![A-Za-z0-9_])" + ("C" + "ow").encode("ascii") + rb"(?![A-Za-z0-9_])"
)
_REPARSE_POINT = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


@dataclass(frozen=True, slots=True)
class BrandViolation:
    path: str
    rule: str
    location: str


def product_files(
    paths: Iterable[Path], *, allow_contained_symlinks: bool = False
) -> tuple[tuple[Path, Path], ...]:
    result: list[tuple[Path, Path]] = []
    for raw_root in paths:
        root = raw_root.resolve(strict=False)
        if not root.exists():
            continue
        if root.is_file():
            result.append((root.parent, root))
            continue
        for directory, names, files in os.walk(root, followlinks=False):
            current = Path(directory)
            relative_directory = current.relative_to(root)
            unsafe_directories = [
                name
                for name in names
                if _unsafe_entry(
                    current / name,
                    root,
                    allow_contained_symlinks=allow_contained_symlinks,
                )
            ]
            result.extend((root, current / name) for name in unsafe_directories)
            names[:] = [
                name
                for name in sorted(names, key=str.casefold)
                if name.casefold() not in _SKIP_DIRECTORIES
                and name.casefold() not in _ALLOWED_DIRECTORIES
                and name not in unsafe_directories
            ]
            if any(
                part.casefold() in _ALLOWED_DIRECTORIES
                for part in relative_directory.parts
            ):
                names[:] = []
                continue
            for name in sorted(files, key=str.casefold):
                path = current / name
                relative = path.relative_to(root)
                if name.casefold() in _NOTICE_NAMES or any(
                    part.casefold() in _ALLOWED_DIRECTORIES for part in relative.parts
                ):
                    continue
                result.append((root, path))
    return tuple(result)


def check(
    paths: Iterable[Path] = DEFAULT_ROOTS, *, allow_contained_symlinks: bool = False
) -> list[BrandViolation]:
    violations: list[BrandViolation] = []
    for root, path in product_files(
        paths, allow_contained_symlinks=allow_contained_symlinks
    ):
        relative = path.relative_to(root).as_posix()
        if _unsafe_entry(
            path, root, allow_contained_symlinks=allow_contained_symlinks
        ):
            violations.append(BrandViolation(relative, "unsafe-entry", "path"))
            continue
        path_match = _first_match(os.fsencode(relative))
        if path_match is not None:
            violations.append(BrandViolation(relative, path_match[0], "path"))
        if path.suffix.casefold() == ".zip" and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                violations.extend(_archive_violations(archive, relative))
            continue
        try:
            with path.open("rb") as stream:
                if path.stat().st_size == 0:
                    continue
                with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as payload:
                    match = _first_match(payload)
                    if match is not None:
                        violations.append(
                            BrandViolation(
                                relative, match[0], _location(payload, match[1])
                            )
                        )
        except OSError:
            violations.append(BrandViolation(relative, "unreadable-entry", "path"))
    return sorted(
        violations, key=lambda item: (item.path.casefold(), item.path, item.location)
    )


def _unsafe_entry(
    path: Path, root: Path, *, allow_contained_symlinks: bool
) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(metadata.st_mode):
        if not allow_contained_symlinks:
            return True
        try:
            path.resolve(strict=True).relative_to(root.resolve(strict=True))
        except (OSError, ValueError):
            return True
        return False
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT)


def _archive_violations(
    archive: zipfile.ZipFile, prefix: str, *, depth: int = 0
) -> list[BrandViolation]:
    violations: list[BrandViolation] = []
    for member in archive.infolist():
        name = PurePosixPath(member.filename)
        location = f"{prefix}!/{member.filename}"
        if (
            name.is_absolute()
            or ".." in name.parts
            or "\\" in member.filename
        ):
            violations.append(BrandViolation(location, "unsafe-archive-entry", "path"))
            continue
        if member.is_dir() or _allowed_archive_member(name):
            continue
        path_match = _first_match(os.fsencode(member.filename))
        if path_match is not None:
            violations.append(BrandViolation(location, path_match[0], "path"))
        payload = archive.read(member)
        if depth < 3 and zipfile.is_zipfile(io.BytesIO(payload)):
            with zipfile.ZipFile(io.BytesIO(payload)) as nested:
                violations.extend(
                    _archive_violations(nested, location, depth=depth + 1)
                )
            continue
        match = _first_match(payload)
        if match is not None:
            violations.append(
                BrandViolation(location, match[0], _location(payload, match[1]))
            )
    return violations


def _allowed_archive_member(path: PurePosixPath) -> bool:
    return path.name.casefold() in _NOTICE_NAMES or any(
        part.casefold() in _ALLOWED_DIRECTORIES for part in path.parts
    )


def _first_match(payload: bytes | mmap.mmap) -> tuple[str, int] | None:
    lowered = payload[:].lower()
    matches: list[tuple[int, str]] = []
    for rule, pattern in _TEXT_PATTERNS:
        offset = lowered.find(pattern.lower())
        if offset >= 0:
            matches.append((offset, rule))
        wide = b"\0".join(bytes((byte,)) for byte in pattern) + b"\0"
        wide_offset = lowered.find(wide.lower())
        if wide_offset >= 0:
            matches.append((wide_offset, f"{rule}-utf16"))
    # Rust and Swift binaries legitimately contain the generic copy-on-write type
    # name `Cow`; keep the short-form check for human-readable product text only.
    if payload.find(b"\0") < 0:
        standalone = _STANDALONE.search(payload)
        if standalone is not None:
            matches.append((standalone.start(), "predecessor-short-brand"))
    if not matches:
        return None
    offset, rule = min(matches)
    return rule, offset


def _location(payload: mmap.mmap, offset: int) -> str:
    prefix = payload[:offset]
    try:
        prefix.decode("utf-8")
    except UnicodeDecodeError:
        return f"byte:{offset}"
    return f"line:{prefix.count(bytes((10,))) + 1}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check-emate-brand")
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--allow-contained-symlinks",
        action="store_true",
        help="allow only links whose resolved target stays inside the scanned tree",
    )
    args = parser.parse_args(argv)
    roots = tuple(args.paths) or DEFAULT_ROOTS
    violations = check(
        roots, allow_contained_symlinks=args.allow_contained_symlinks
    )
    result = {
        "ok": not violations,
        "scanned_files": len(
            product_files(
                roots, allow_contained_symlinks=args.allow_contained_symlinks
            )
        ),
        "violations": [asdict(item) for item in violations],
    }
    print(
        json.dumps(result, ensure_ascii=False, sort_keys=True),
        file=sys.stderr if violations else sys.stdout,
    )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
