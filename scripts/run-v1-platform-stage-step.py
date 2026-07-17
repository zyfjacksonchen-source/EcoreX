#!/usr/bin/env python3
"""Run fixed platform Stage command groups with cross-shell fail-fast semantics."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
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
        ("test Web", "npm", ("run", "test:v1"), "desktop"),
        ("build Web", "npm", ("run", "build"), "desktop"),
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one repository-owned platform Stage command group."
    )
    parser.add_argument("step", choices=("install-dependencies", "build-web"))
    return parser


def _npm_executable() -> str:
    name = "npm.cmd" if os.name == "nt" else "npm"
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
    commands: list[StageCommand] = []
    for label, executable_kind, arguments, cwd_relative in templates:
        if executable_kind == "python":
            executable = python
        elif executable_kind == "npm":
            if npm is None:
                npm = _npm_executable()
            executable = npm
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        commands = _commands(args.step)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 127
    return run_commands(commands)


if __name__ == "__main__":
    raise SystemExit(main())
