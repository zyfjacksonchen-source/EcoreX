"""Read-only source inventory and SHA-256 helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping

from .errors import SourceLayoutError
from .models import InventoryEntry, SourceInventory
from .path_security import (
    assert_directory_unchanged,
    directory_identity,
    is_within,
    lexical_absolute,
    secure_directory,
    secure_regular_file,
    stable_sha256_file,
)


SOURCE_VERSION = "0.3.0"
_PIN_LABEL = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def sha256_file(path: Path) -> str:
    digest, _identity = stable_sha256_file(path, label="legacy source file")
    return digest


def _entry_digest(entries: list[InventoryEntry]) -> str:
    payload = [item.to_dict() for item in entries]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def inventory_source(
    source_root: str | Path,
    *,
    pinned_files: Mapping[str, str | Path] | None = None,
) -> SourceInventory:
    """Hash every source entry without following links or writing sidecars."""

    root = secure_directory(source_root, label="legacy source root")

    entries: list[InventoryEntry] = []

    def visit(directory: Path) -> None:
        before = directory_identity(directory, label="legacy source directory")
        try:
            children = sorted(
                directory.iterdir(),
                key=lambda item: (item.name.casefold(), item.name),
            )
        except OSError as error:
            raise SourceLayoutError("legacy source directory is unreadable") from error
        for candidate in children:
            try:
                metadata = candidate.lstat()
            except OSError as error:
                raise SourceLayoutError("legacy source entry is unreadable") from error
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse):
                raise SourceLayoutError(
                    "legacy source contains a symlink or reparse point"
                )
            relative = candidate.relative_to(root).as_posix()
            if stat.S_ISDIR(metadata.st_mode):
                visit(candidate)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise SourceLayoutError("legacy source contains a special filesystem entry")
            digest, stable = stable_sha256_file(
                candidate,
                label=f"legacy source file {relative}",
                root=root,
            )
            entries.append(
                InventoryEntry(
                    relative_path=relative,
                    kind="file",
                    size_bytes=stable.size,
                    mtime_ns=stable.mtime_ns,
                    sha256=digest,
                )
            )
        assert_directory_unchanged(
            directory,
            before,
            label="legacy source directory",
        )

    visit(root)

    for label, raw_path in sorted((pinned_files or {}).items()):
        if not _PIN_LABEL.fullmatch(str(label)):
            raise SourceLayoutError("pinned source labels must be safe lowercase tokens")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        lexical = lexical_absolute(candidate)
        resolved = secure_regular_file(
            lexical,
            label=f"pinned source file {label}",
            root=root if is_within(lexical, root) else None,
        )
        if is_within(resolved, root):
            continue
        digest, stable = stable_sha256_file(
            resolved,
            label=f"pinned source file {label}",
        )
        entries.append(
            InventoryEntry(
                relative_path=f"@pinned/{label}",
                kind="file",
                size_bytes=stable.size,
                mtime_ns=stable.mtime_ns,
                sha256=digest,
            )
        )

    entries.sort(key=lambda item: (item.relative_path.casefold(), item.relative_path))
    return SourceInventory(
        source_version=SOURCE_VERSION,
        digest=_entry_digest(entries),
        entries=tuple(entries),
        total_bytes=sum(item.size_bytes for item in entries if item.kind == "file"),
    )


def assert_disjoint_roots(source_root: Path, target_root: Path) -> tuple[Path, Path]:
    source = secure_directory(source_root, label="legacy source root")
    target_lexical = lexical_absolute(target_root)
    try:
        target = target_lexical.resolve(strict=False)
    except OSError as error:
        raise SourceLayoutError("v1 target path is invalid") from error
    if is_within(target, source) or is_within(source, target):
        raise SourceLayoutError("legacy source and v1 target must be disjoint directories")
    return source, target


def inventory_index(inventory: SourceInventory) -> dict[str, InventoryEntry]:
    return {entry.relative_path: entry for entry in inventory.entries}
