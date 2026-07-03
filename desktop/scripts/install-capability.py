#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        repo_root / "scripts" / "install-capability.py",
        repo_root / "desktop" / "runtime" / "ecorex-runtime" / "scripts" / "install-capability.py",
    ]
    for script in candidates:
        if script.exists() and script.resolve() != Path(__file__).resolve():
            sys.argv[0] = str(script)
            runpy.run_path(str(script), run_name="__main__")
            return 0
    raise SystemExit("Shared capability installer not found")


if __name__ == "__main__":
    raise SystemExit(main())
