#!/usr/bin/env python3
"""Check every authoritative v1 source file, including untracked files."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
_TEXT_SUFFIXES = frozenset(
    {
        ".c",
        ".cpp",
        ".css",
        ".go",
        ".h",
        ".html",
        ".js",
        ".json",
        ".md",
        ".mjs",
        ".mod",
        ".ps1",
        ".py",
        ".sh",
        ".sum",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)


def source_files(root: Path = ROOT) -> tuple[Path, ...]:
    roots = (
        root / "ecorex",
        root / "tests" / "v1",
        root / "platform-staging",
        root / "release" / "capability-packs",
        root / "desktop" / "src",
        root / "desktop" / "tools",
    )
    result = {
        path
        for base in roots
        if base.is_dir()
        for path in base.rglob("*")
        if path.is_file() and path.suffix.casefold() in _TEXT_SUFFIXES
    }
    result.update(
        path
        for path in (root / "scripts").glob("*.py")
        if path.is_file() and "v1" in path.name.casefold()
    )
    workflow_root = root / ".github" / "workflows"
    result.update(
        path
        for path in workflow_root.glob("*")
        if path.is_file() and path.suffix.casefold() in {".yml", ".yaml"}
    )
    action_lock = root / "requirements" / "locks" / "github-actions.json"
    if action_lock.is_file():
        result.add(action_lock)
    return tuple(sorted(result))


def check(root: Path = ROOT) -> None:
    files = source_files(root)
    if not files:
        raise ValueError("v1_source_inventory_empty")
    result = subprocess.run(
        ("git", "ls-files", "-z"), cwd=root, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise ValueError("v1_source_git_inventory_unavailable")
    tracked = {
        value.decode("utf-8")
        for value in result.stdout.split(b"\0")
        if value
    }
    violations: list[str] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if relative not in tracked:
            violations.append(f"untracked:{relative}")
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            violations.append(f"not-regular:{relative}")
            continue
        payload = path.read_bytes()
        if b"\0" in payload:
            violations.append(f"binary:{relative}")
            continue
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            violations.append(f"not-utf8:{relative}")
            continue
        if b"\r" in payload:
            violations.append(f"crlf:{relative}")
        if payload and not payload.endswith(b"\n"):
            violations.append(f"missing-final-lf:{relative}")
        if any(line.endswith((b" ", b"\t")) for line in payload.splitlines()):
            violations.append(f"trailing-whitespace:{relative}")
    if violations:
        raise ValueError("v1_source_tree_invalid:" + ",".join(violations[:32]))


def main() -> int:
    try:
        check()
        print(json.dumps({"ok": True, "source_file_count": len(source_files())}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
