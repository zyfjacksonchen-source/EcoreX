#!/usr/bin/env python3
"""Select one immutable live-accepted Candidate for a later publication run."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.candidate_handoff import (  # noqa: E402
    CandidateHandoffError,
    build_candidate_handoff,
    write_candidate_handoff,
)


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _append_github_output(path: Path, artifact_id: int, artifact_sha256: str) -> None:
    output = Path(os.path.abspath(path.expanduser()))
    try:
        metadata = output.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)
        or metadata.st_nlink != 1
    ):
        raise CandidateHandoffError("candidate_handoff_github_output_invalid")
    try:
        with output.open("ab") as stream:
            stream.write(
                (
                    f"artifact_id={artifact_id}\n"
                    f"artifact_sha256={artifact_sha256}\n"
                ).encode("ascii")
            )
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise CandidateHandoffError("candidate_handoff_github_output_invalid") from None


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="select-v1-accepted-candidate")
    parser.add_argument("--run-metadata", required=True, type=Path)
    parser.add_argument("--artifact-metadata", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--artifact-id", required=True, type=int)
    parser.add_argument("--channel", required=True, choices=("canary", "stable"))
    parser.add_argument("--protected-ref", default="refs/heads/main")
    parser.add_argument("--max-run-age-seconds", type=int, default=2_592_000)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--github-output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        handoff = build_candidate_handoff(
            run_metadata_path=args.run_metadata,
            artifact_metadata_path=args.artifact_metadata,
            repository=args.repository,
            commit_sha=args.commit_sha,
            workflow_run_id=args.workflow_run_id,
            run_attempt=args.run_attempt,
            artifact_id=args.artifact_id,
            channel=args.channel,
            protected_ref=args.protected_ref,
            now=datetime.now(timezone.utc),
            maximum_age=timedelta(seconds=args.max_run_age_seconds),
        )
        write_candidate_handoff(handoff, args.output)
        _append_github_output(
            args.github_output,
            args.artifact_id,
            str(handoff["artifact_archive_sha256"]),
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "artifact_id": args.artifact_id,
                    "artifact_archive_sha256": handoff["artifact_archive_sha256"],
                },
                sort_keys=True,
            )
        )
        return 0
    except (CandidateHandoffError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, CandidateHandoffError) else "candidate_handoff_invalid"
        print(json.dumps({"ok": False, "error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
