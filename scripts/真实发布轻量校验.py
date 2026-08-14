#!/usr/bin/env python3
"""Run the current cheap, local-only e-Mate release preflight."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CHECKS = (
    "scripts/check-v1-source-tree.py",
    "scripts/check-v1-server-schema-authority.py",
    "scripts/check-v1-reproducibility.py",
    "scripts/check-v1-public-download-site.py",
)


def build_report() -> dict[str, object]:
    checks: list[dict[str, object]] = []
    for relative_path in CHECKS:
        result = subprocess.run(
            [sys.executable, relative_path],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        checks.append(
            {
                "path": relative_path,
                "command": f"{sys.executable} {relative_path}",
                "status": "passed" if result.returncode == 0 else "failed",
                "error": result.stderr.strip() if result.returncode else "",
            }
        )
    return {
        "schema_version": 1,
        "status": "passed" if all(row["status"] == "passed" for row in checks) else "failed",
        "check_count": len(checks),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="真实发布轻量校验")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
