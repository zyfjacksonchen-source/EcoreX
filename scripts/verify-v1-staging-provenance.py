#!/usr/bin/env python3
"""Verify that Candidate staging inputs came from the trusted dispatch workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


STAGE_WORKFLOW = ".github/workflows/ecorex-v1-platform-stage.yml"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--expected-run-id", required=True, type=int)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-repository", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if (
            args.expected_run_id < 1
            or _COMMIT.fullmatch(args.expected_commit) is None
            or _REPOSITORY.fullmatch(args.expected_repository) is None
        ):
            raise ValueError("staging_expectation_invalid")
        payload = args.metadata.read_bytes()
        if not 1 <= len(payload) <= 2 * 1024 * 1024:
            raise ValueError("staging_run_metadata_invalid")
        value = json.loads(payload.decode("utf-8"))
        repository = value.get("repository") if isinstance(value, dict) else None
        head_repository = value.get("head_repository") if isinstance(value, dict) else None
        pull_requests = value.get("pull_requests") if isinstance(value, dict) else None
        path = value.get("path") if isinstance(value, dict) else None
        if isinstance(path, str) and path.startswith("./"):
            path = path[2:]
        if (
            not isinstance(value, dict)
            or value.get("id") != args.expected_run_id
            or value.get("head_sha") != args.expected_commit
            or value.get("event") != "workflow_dispatch"
            or value.get("status") != "completed"
            or value.get("conclusion") != "success"
            or path != STAGE_WORKFLOW
            or not isinstance(repository, dict)
            or repository.get("full_name") != args.expected_repository
            or not isinstance(head_repository, dict)
            or head_repository.get("full_name") != args.expected_repository
            or pull_requests != []
            or isinstance(value.get("run_attempt"), bool)
            or not isinstance(value.get("run_attempt"), int)
            or value["run_attempt"] < 1
        ):
            raise ValueError("staging_run_provenance_rejected")
        receipt = {
            "schema_version": 1,
            "status": "passed",
            "workflow_path": STAGE_WORKFLOW,
            "workflow_run_id": args.expected_run_id,
            "run_attempt": value["run_attempt"],
            "commit_sha": args.expected_commit,
            "repository": args.expected_repository,
            "metadata_sha256": hashlib.sha256(payload).hexdigest(),
        }
        output = args.receipt.resolve()
        if output.exists():
            raise ValueError("staging_provenance_receipt_exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else type(exc).__name__
                    ),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
