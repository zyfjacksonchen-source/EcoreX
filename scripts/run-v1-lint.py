#!/usr/bin/env python3
"""Run the supported EcoreX v1 Python lint surface on every host."""

from __future__ import annotations

from pathlib import Path
import compileall
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def v1_python_targets(root: Path = ROOT) -> tuple[Path, ...]:
    """Return the complete current-v1 Python surface, excluding legacy scripts."""

    scripts = tuple(
        sorted(
            path
            for path in (root / "scripts").glob("*.py")
            if path.is_file() and "v1" in path.name.casefold()
        )
    )
    roots = tuple(
        path
        for path in (
            root / "ecorex",
            root / "tests" / "v1",
            root / "platform-staging",
            root / "release" / "capability-packs",
        )
        if path.exists()
    )
    return (*roots, *scripts)


def _compile(targets: tuple[Path, ...]) -> bool:
    for target in targets:
        if target.is_dir():
            if not compileall.compile_dir(target, quiet=1, force=True):
                return False
        elif not compileall.compile_file(target, quiet=1, force=True):
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    compile_requested = "--compile" in arguments
    arguments = [value for value in arguments if value != "--compile"]
    targets = v1_python_targets()
    command = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        *(str(path) for path in targets),
        *arguments,
    ]
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode != 0:
        return result.returncode
    return 0 if not compile_requested or _compile(targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())
