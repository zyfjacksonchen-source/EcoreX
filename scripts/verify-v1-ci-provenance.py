#!/usr/bin/env python3
"""Create typed cross-runner evidence from one trusted v1 CI run."""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.ci_reproducibility import (  # noqa: E402
    CiReproducibilityError,
    atomic_create_json,
    build_source_evidence,
    parse_now,
)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify-v1-ci-provenance")
    parser.add_argument("--run-metadata", required=True, type=Path)
    parser.add_argument("--artifact-metadata", required=True, type=Path)
    parser.add_argument("--contracts-root", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--protected-ref", default="refs/heads/main")
    parser.add_argument("--max-run-age-seconds", type=int, default=86_400)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.max_run_age_seconds < 1:
            raise CiReproducibilityError("ci_expected_identity_invalid")
        evidence = build_source_evidence(
            repository_root=ROOT,
            run_metadata_path=args.run_metadata,
            artifact_metadata_path=args.artifact_metadata,
            contracts_root=args.contracts_root,
            repository=args.repository,
            commit_sha=args.commit_sha,
            workflow_run_id=args.workflow_run_id,
            run_attempt=args.run_attempt,
            protected_ref=args.protected_ref,
            now=parse_now(None),
            maximum_age=timedelta(seconds=args.max_run_age_seconds),
        )
        atomic_create_json(args.output, evidence)
        print(
            json.dumps(
                {
                    "ok": True,
                    "workflow_run_id": args.workflow_run_id,
                    "run_attempt": args.run_attempt,
                    "canonical_web_bundle_sha256": evidence[
                        "canonical_web_bundle_sha256"
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    except (CiReproducibilityError, OSError) as error:
        code = error.code if isinstance(error, CiReproducibilityError) else "ci_input_invalid"
        print(json.dumps({"ok": False, "error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
