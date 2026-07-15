#!/usr/bin/env python3
"""Bind typed CI reproducibility evidence to an existing signed Candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.ci_reproducibility import (  # noqa: E402
    CiReproducibilityError,
    atomic_create_json,
    bind_to_candidate,
)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bind-v1-reproducibility-evidence")
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--candidate-receipt", required=True, type=Path)
    parser.add_argument("--release-manifest", required=True, type=Path)
    parser.add_argument("--trusted-public-key", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        bound = bind_to_candidate(
            evidence_path=args.evidence,
            candidate_receipt_path=args.candidate_receipt,
            release_manifest_path=args.release_manifest,
            trusted_public_key=args.trusted_public_key,
        )
        atomic_create_json(args.output, bound)
        print(
            json.dumps(
                {"ok": True, "release_id": bound["release_id"]}, sort_keys=True
            )
        )
        return 0
    except (CiReproducibilityError, OSError) as error:
        code = error.code if isinstance(error, CiReproducibilityError) else "ci_input_invalid"
        print(json.dumps({"ok": False, "error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
