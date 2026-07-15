#!/usr/bin/env python3
"""Create the strict hand-off receipt for a real platform staging tree."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.candidate import (  # noqa: E402
    CandidateBuildError,
    write_stage_receipt,
)
from ecorex.pack_catalog import REQUIRED_CAPABILITY_PACK_IDS  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record a content-bound receipt for one non-placeholder release stage."
    )
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stage-id", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--workflow-run-attempt", type=int, default=1)
    parser.add_argument("--producer-executable-sha256", required=True)
    parser.add_argument("--producer-adapter-sha256")
    parser.add_argument(
        "--kind", required=True, choices=("core", "bootstrap", "capability-pack")
    )
    parser.add_argument("--platform", required=True, choices=("windows", "macos"))
    parser.add_argument("--architecture", required=True, choices=("x64", "arm64"))
    parser.add_argument("--pack-id", choices=REQUIRED_CAPABILITY_PACK_IDS)
    parser.add_argument(
        "--gate-evidence",
        action="append",
        required=True,
        metavar="GATE=SHA256",
    )
    return parser


def _gate_evidence(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, digest = value.partition("=")
        if not separator or not name or name in result:
            raise CandidateBuildError("stage_receipt_gates_invalid")
        result[name] = digest
    return result


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output = write_stage_receipt(
            source_dir=args.source_dir,
            destination=args.output,
            stage_id=args.stage_id,
            commit_sha=args.commit_sha,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            producer_executable_sha256=args.producer_executable_sha256,
            producer_adapter_sha256=args.producer_adapter_sha256,
            kind=args.kind,
            platform=args.platform,
            architecture=args.architecture,
            pack_id=args.pack_id,
            gate_evidence=_gate_evidence(args.gate_evidence),
        )
    except Exception as exc:
        code = exc.code if isinstance(exc, CandidateBuildError) else "stage_receipt_failed"
        print(json.dumps({"ok": False, "code": code}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "receipt": str(output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
