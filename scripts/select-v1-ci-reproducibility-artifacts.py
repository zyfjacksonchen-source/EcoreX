#!/usr/bin/env python3
"""Select immutable same-attempt CI artifact IDs before downloading them."""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import stat
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.ci_reproducibility import (  # noqa: E402
    CiReproducibilityError,
    atomic_create_json,
    build_artifact_selection,
    parse_now,
)


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _append_github_output(path: Path, artifact_ids: str) -> None:
    output = Path(os.path.abspath(path.expanduser()))
    try:
        metadata = output.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)
    ):
        raise CiReproducibilityError("ci_github_output_invalid")
    try:
        with output.open("ab") as stream:
            stream.write(f"artifact_ids={artifact_ids}\n".encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise CiReproducibilityError("ci_github_output_invalid") from None


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="select-v1-ci-reproducibility-artifacts"
    )
    parser.add_argument("--run-metadata", required=True, type=Path)
    parser.add_argument("--artifact-metadata", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--protected-ref", default="refs/heads/main")
    parser.add_argument("--max-run-age-seconds", type=int, default=86_400)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--github-output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.max_run_age_seconds < 1:
            raise CiReproducibilityError("ci_expected_identity_invalid")
        selection = build_artifact_selection(
            run_metadata_path=args.run_metadata,
            artifact_metadata_path=args.artifact_metadata,
            repository=args.repository,
            commit_sha=args.commit_sha,
            workflow_run_id=args.workflow_run_id,
            run_attempt=args.run_attempt,
            protected_ref=args.protected_ref,
            now=parse_now(None),
            maximum_age=timedelta(seconds=args.max_run_age_seconds),
        )
        atomic_create_json(args.output, selection)
        artifacts = selection["artifacts"]
        artifact_ids = ",".join(
            str(artifacts[target]["artifact_id"]) for target in sorted(artifacts)
        )
        _append_github_output(args.github_output, artifact_ids)
        print(json.dumps({"ok": True, "artifact_ids": artifact_ids}, sort_keys=True))
        return 0
    except (CiReproducibilityError, OSError) as error:
        code = (
            error.code
            if isinstance(error, CiReproducibilityError)
            else "ci_input_invalid"
        )
        print(json.dumps({"ok": False, "error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
