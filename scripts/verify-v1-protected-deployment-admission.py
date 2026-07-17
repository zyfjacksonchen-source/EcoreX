#!/usr/bin/env python3
"""Authenticate a production admission at every mutation boundary."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    strict_json_loads,
)
from ecorex.release.protected_deployment import (  # noqa: E402
    ProtectedDeploymentAdmissionError,
    verify_admission,
)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admission", required=True, type=Path)
    parser.add_argument("--trusted-key", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--candidate-run-id", required=True, type=int)
    parser.add_argument("--candidate-artifact-id", required=True, type=int)
    parser.add_argument("--channel", required=True, choices=("canary", "stable"))
    parser.add_argument(
        "--mode", required=True, choices=("create", "create-and-activate")
    )
    parser.add_argument("--rollout-percentage", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        key_id, encoded = args.trusted_key.split("=", 1)
        public = base64.b64decode(encoded, validate=True)
        if not key_id or len(public) != 32:
            raise ValueError("protected_deployment_trusted_key_invalid")
        payload = read_stable_regular_file(
            args.admission,
            maximum_bytes=2 * 1024 * 1024,
            code="protected_deployment_admission_invalid",
        )
        document = strict_json_loads(
            payload, code="protected_deployment_admission_invalid"
        )
        if not isinstance(document, dict):
            raise ValueError("protected_deployment_admission_invalid")
        body = verify_admission(
            document,
            public_keys={key_id: public},
            now=datetime.now(timezone.utc),
        )
        candidate = body["candidate"]
        if (
            body["repository"] != args.repository
            or body["commit_sha"] != args.commit_sha
            or body["channel"] != args.channel
            or candidate["workflow_run_id"] != args.candidate_run_id
            or candidate["artifact_id"] != args.candidate_artifact_id
            or body["decision"]["mode"] != args.mode
            or body["decision"]["rollout_percentage"] != args.rollout_percentage
        ):
            raise ValueError("protected_deployment_dispatch_identity_mismatch")
        print(
            json.dumps(
                {
                    "ok": True,
                    "admission_id": body["admission_id"],
                    "release_id": candidate["release_id"],
                    "mode": body["decision"]["mode"],
                    "rollout_percentage": body["decision"]["rollout_percentage"],
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        code = (
            str(error)
            if isinstance(error, (ValueError, ProtectedDeploymentAdmissionError))
            else "protected_deployment_admission_verification_failed"
        )
        if re.fullmatch(r"[a-z][a-z0-9_]{2,127}", code) is None:
            code = "protected_deployment_admission_verification_failed"
        print(json.dumps({"ok": False, "code": code}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
