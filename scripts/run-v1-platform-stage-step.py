#!/usr/bin/env python3
"""Run fixed platform Stage command groups with cross-shell fail-fast semantics."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class StageCommand:
    label: str
    argv: tuple[str, ...]
    cwd: Path


Runner = Callable[..., subprocess.CompletedProcess[object]]

# This literal catalog is an audited dependency/reproducibility boundary. The
# dependency-lock checker parses it without importing this module and pins the
# complete runner AST, so command additions and execution bypasses fail closed.
COMMAND_CATALOG = {
    "install-dependencies": (
        (
            "install locked platform-stage Python profile",
            "python",
            (
                "scripts/install-v1-python-profile.py",
                "--profile",
                "platform-stage",
            ),
            ".",
        ),
        (
            "validate Python dependency locks",
            "python",
            ("scripts/check-v1-dependency-locks.py",),
            ".",
        ),
        (
            "install managed Chromium",
            "python",
            ("-m", "playwright", "install", "chromium"),
            ".",
        ),
    ),
    "build-web": (
        ("install locked Web dependencies", "npm", ("ci",), "desktop"),
        ("typecheck Web", "npm", ("run", "typecheck"), "desktop"),
        ("build Web", "npm", ("run", "build"), "desktop"),
        ("test built Web", "npm", ("run", "test:v1"), "desktop"),
        (
            "validate tested Web content addresses",
            "python",
            (
                "scripts/check-v1-reproducibility.py",
                "--web-dist",
                "desktop/dist",
            ),
            ".",
        ),
        (
            "validate tested Web bundle",
            "node",
            ("tools/check-v1-bundle.mjs", "dist"),
            "desktop",
        ),
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one repository-owned platform Stage command group."
    )
    parser.add_argument(
        "step",
        choices=("clean-check", "install-dependencies", "build-web"),
    )
    return parser


def _npm_executable() -> str:
    name = "npm.cmd" if os.name == "nt" else "npm"
    executable = shutil.which(name)
    if executable is None:
        raise FileNotFoundError(f"stage_command_not_found:{name}")
    return executable


def _node_executable() -> str:
    name = "node.exe" if os.name == "nt" else "node"
    executable = shutil.which(name)
    if executable is None:
        raise FileNotFoundError(f"stage_command_not_found:{name}")
    return executable


def _commands(step: str) -> tuple[StageCommand, ...]:
    try:
        templates = COMMAND_CATALOG[step]
    except KeyError:
        raise ValueError(f"unknown_stage_step:{step}") from None
    python = str(Path(sys.executable).resolve(strict=True))
    npm: str | None = None
    node: str | None = None
    commands: list[StageCommand] = []
    for label, executable_kind, arguments, cwd_relative in templates:
        if executable_kind == "python":
            executable = python
        elif executable_kind == "npm":
            if npm is None:
                npm = _npm_executable()
            executable = npm
        elif executable_kind == "node":
            if node is None:
                node = _node_executable()
            executable = node
        else:
            raise ValueError(f"unknown_stage_executable:{executable_kind}")
        resolved_arguments = tuple(
            str(ROOT / argument) if argument.startswith("scripts/") else argument
            for argument in arguments
        )
        commands.append(
            StageCommand(
                label,
                (executable, *resolved_arguments),
                (ROOT / cwd_relative).resolve(strict=True),
            )
        )
    return tuple(commands)


def _exit_code(returncode: int) -> int:
    return returncode if 1 <= returncode <= 255 else 1


def check_clean_checkout(
    *,
    root: Path = ROOT,
    runner: Runner = subprocess.run,
) -> int:
    dist_root = root / "desktop" / "dist"
    if os.path.lexists(dist_root):
        print("stage_checkout_dist_present", file=sys.stderr, flush=True)
        return 75
    git_name = "git.exe" if os.name == "nt" else "git"
    git = shutil.which(git_name)
    if git is None:
        print(f"stage_command_not_found:{git_name}", file=sys.stderr, flush=True)
        return 127
    try:
        result = runner(
            (
                git,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
            cwd=root,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        print(
            f"stage_checkout_status_failed:{type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 127
    if result.returncode != 0:
        print("stage_checkout_status_failed", file=sys.stderr, flush=True)
        return _exit_code(int(result.returncode))
    if result.stdout:
        print("stage_checkout_not_clean", file=sys.stderr, flush=True)
        return 75
    print("stage_checkout_clean", flush=True)
    return 0


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
    )


def _read_stable_regular_file(path: Path) -> tuple[int, str]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise ValueError(f"stage_dist_non_regular:{path.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise ValueError(f"stage_dist_path_drift:{path.name}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = path.lstat()
    if _identity(before) != _identity(after) or _identity(after) != _identity(final):
        raise ValueError(f"stage_dist_file_drift:{path.name}")
    return before.st_size, digest.hexdigest()


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink():
        raise ValueError("stage_dist_root_invalid")
    entries: list[tuple[str, str, int, str]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories.sort()
        files.sort()
        for name in directories:
            path = current_path / name
            value = path.lstat()
            if not stat.S_ISDIR(value.st_mode) or path.is_symlink():
                raise ValueError(f"stage_dist_directory_invalid:{name}")
            relative = path.relative_to(root).as_posix()
            entries.append((relative, "directory", 0, ""))
        for name in files:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            size, digest = _read_stable_regular_file(path)
            entries.append((relative, "file", size, digest))
    entries.sort()
    if not entries or not any(
        entry[0] == "index.html" and entry[1] == "file" for entry in entries
    ):
        raise ValueError("stage_dist_index_missing")
    return tuple(entries)


def _stable_tree_digest(root: Path) -> str:
    first = _tree_snapshot(root)
    second = _tree_snapshot(root)
    if first != second:
        raise ValueError("stage_dist_tree_drift")
    digest = hashlib.sha256()
    for entry in first:
        digest.update("\0".join(map(str, entry)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def run_commands(
    commands: Sequence[StageCommand],
    *,
    runner: Runner = subprocess.run,
) -> int:
    """Run commands in order and return immediately on the first failure."""

    for index, command in enumerate(commands, start=1):
        print(
            f"::group::platform-stage[{index}/{len(commands)}] {command.label}",
            flush=True,
        )
        try:
            result = runner(command.argv, cwd=command.cwd, check=False)
        except OSError as exc:
            print("::endgroup::", flush=True)
            print(
                f"::error title=Platform Stage command launch failed::"
                f"{command.label}: {type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return 127
        print("::endgroup::", flush=True)
        if result.returncode != 0:
            print(
                f"::error title=Platform Stage command failed::"
                f"{command.label} exited {result.returncode}; later commands were not run",
                file=sys.stderr,
                flush=True,
            )
            return _exit_code(int(result.returncode))
    return 0


def run_build_web_commands(
    commands: Sequence[StageCommand],
    *,
    dist_root: Path = ROOT / "desktop" / "dist",
    runner: Runner = subprocess.run,
    expected_commands: Sequence[StageCommand] | None = None,
) -> int:
    if expected_commands is None:
        try:
            expected_commands = _commands("build-web")
        except (FileNotFoundError, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr, flush=True)
            return 127
    if tuple(commands) != tuple(expected_commands):
        print("stage_web_command_sequence_invalid", file=sys.stderr, flush=True)
        return 76
    if os.path.lexists(dist_root):
        print("stage_web_dist_not_clean", file=sys.stderr, flush=True)
        return 73
    result = run_commands(commands[:1], runner=runner)
    if result != 0:
        return result
    if os.path.lexists(dist_root):
        print("stage_web_dist_created_by_npm_ci", file=sys.stderr, flush=True)
        return 73
    result = run_commands(commands[1:2], runner=runner)
    if result != 0:
        return result
    if os.path.lexists(dist_root):
        print("stage_web_dist_created_before_build", file=sys.stderr, flush=True)
        return 73
    result = run_commands(commands[2:3], runner=runner)
    if result != 0:
        return result
    try:
        built_digest = _stable_tree_digest(dist_root)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 74
    result = run_commands(commands[3:4], runner=runner)
    if result != 0:
        return result
    try:
        tested_digest = _stable_tree_digest(dist_root)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 74
    if tested_digest != built_digest:
        print("stage_web_dist_mutated_by_test", file=sys.stderr, flush=True)
        return 74
    result = run_commands(commands[4:], runner=runner)
    if result != 0:
        return result
    try:
        validated_digest = _stable_tree_digest(dist_root)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 74
    if validated_digest != built_digest:
        print("stage_web_dist_mutated_by_validation", file=sys.stderr, flush=True)
        return 74
    print(f"stage_web_dist_sha256={built_digest}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.step == "clean-check":
        return check_clean_checkout()
    try:
        commands = _commands(args.step)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 127
    if args.step == "build-web":
        return run_build_web_commands(commands)
    return run_commands(commands)


if __name__ == "__main__":
    raise SystemExit(main())
