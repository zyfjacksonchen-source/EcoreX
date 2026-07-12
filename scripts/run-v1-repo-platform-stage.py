#!/usr/bin/env python3
"""Pin and invoke the repository-owned v1 platform stager."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.process_boundary import (  # noqa: E402
    BoundedProcessError,
    run_bounded_process,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=("windows", "macos"))
    parser.add_argument("--architecture", required=True, choices=("x64", "arm64"))
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--workflow-run-attempt", type=int, default=1)
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    executable = Path(sys.executable).resolve(strict=True)
    adapter = ROOT / "platform-staging" / "stager.py"
    if not adapter.is_file():
        return 78
    environment = dict(os.environ)
    environment.update(
        {
            "ECOREX_PLATFORM_STAGER_EXECUTABLE": str(executable),
            "ECOREX_PLATFORM_STAGER_EXECUTABLE_SHA256": _sha256(executable),
            "ECOREX_PLATFORM_STAGER_ADAPTER": str(adapter.resolve(strict=True)),
            "ECOREX_PLATFORM_STAGER_ADAPTER_SHA256": _sha256(adapter),
            "ECOREX_STAGE_WEB_DIST": str((ROOT / "desktop" / "dist").resolve(strict=True)),
        }
    )
    command = (
        str(executable),
        str(ROOT / "scripts" / "invoke-v1-platform-stager.py"),
        "--repo-root",
        str(ROOT),
        "--output-root",
        str(args.output_root),
        "--platform",
        args.platform,
        "--architecture",
        args.architecture,
        "--commit-sha",
        args.commit_sha,
        "--workflow-run-id",
        str(args.workflow_run_id),
        "--workflow-run-attempt",
        str(args.workflow_run_attempt),
    )
    try:
        result = run_bounded_process(
            command,
            payload=None,
            cwd=ROOT,
            environment=environment,
            timeout_seconds=2100,
            max_stdout_bytes=16 * 1024,
            max_stderr_bytes=16 * 1024,
        )
    except (OSError, BoundedProcessError):
        return 70
    if result.stdout:
        sys.stdout.buffer.write(result.stdout)
        sys.stdout.buffer.flush()
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
        sys.stderr.buffer.flush()
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
