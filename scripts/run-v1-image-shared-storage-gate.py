#!/usr/bin/env python3
"""Run the credentialed PostgreSQL/S3 image gate without skip-to-pass paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    write_new_json_file,
)

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_NODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PYTEST_NODE_IDS = (
    "tests/v1/test_image_orchestrator_real_shared_storage.py::"
    "test_real_postgres_s3_concurrency_idempotency_recovery_and_gc",
    "tests/v1/test_image_orchestrator_production_storage.py::"
    "test_real_postgres_image_schema_migrate_validate_and_drift_gate",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate-type", required=True, choices=("shared-storage", "soak"))
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--node-id", action="append", required=True)
    parser.add_argument("--jobs", required=True, type=int)
    parser.add_argument("--workers", required=True, type=int)
    parser.add_argument("--minimum-duration-seconds", required=True, type=float)
    parser.add_argument("--round-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if (
            _COMMIT.fullmatch(args.commit_sha) is None
            or args.workflow_run_id < 1
            or len(args.node_id) != 2
            or len(set(args.node_id)) != 2
            or any(_NODE.fullmatch(value) is None for value in args.node_id)
            or not 32 <= args.jobs <= 256
            or not 8 <= args.workers <= 48
            or not math.isfinite(args.minimum_duration_seconds)
            or args.minimum_duration_seconds < 0
            or not math.isfinite(args.round_timeout_seconds)
            or not 60 <= args.round_timeout_seconds <= 3600
        ):
            raise ValueError("image_gate_input_invalid")
        required_environment = (
            "ECOREX_TEST_POSTGRES_DSN",
            "ECOREX_TEST_S3_ENDPOINT",
            "ECOREX_TEST_S3_ACCESS_KEY",
            "ECOREX_TEST_S3_SECRET_KEY",
        )
        if any(not os.environ.get(name) for name in required_environment):
            raise ValueError("image_gate_credentials_missing")
        environment = dict(os.environ)
        environment.update(
            {
                "ECOREX_TEST_IMAGE_JOBS": str(args.jobs),
                "ECOREX_TEST_IMAGE_WORKERS": str(args.workers),
                "ECOREX_TEST_IMAGE_NODE_IDS": ",".join(args.node_id),
            }
        )
        started = time.monotonic()
        rounds = 0
        junit_digests: list[str] = []
        with tempfile.TemporaryDirectory(prefix="ecorex-image-gate-junit-") as temporary:
            while rounds == 0 or time.monotonic() - started < args.minimum_duration_seconds:
                junit = Path(temporary) / f"round-{rounds}.xml"
                command = (
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={junit}",
                    *_PYTEST_NODE_IDS,
                )
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=args.round_timeout_seconds,
                    check=False,
                )
                if result.returncode != 0:
                    raise ValueError("image_gate_round_failed")
                try:
                    payload = read_stable_regular_file(
                        junit,
                        maximum_bytes=4 * 1024 * 1024,
                        code="image_gate_junit_invalid",
                    )
                    root = ET.fromstring(payload)
                except (OSError, ET.ParseError):
                    raise ValueError("image_gate_junit_invalid") from None
                suites = [root] if root.tag == "testsuite" else list(root.findall("./testsuite"))
                cases = list(root.iter("testcase"))
                if (
                    root.tag not in {"testsuite", "testsuites"}
                    or not suites
                    or len(cases) != 2
                    or sum(int(item.get("tests", "-1")) for item in suites) != 2
                    or sum(int(item.get("failures", "-1")) for item in suites) != 0
                    or sum(int(item.get("errors", "-1")) for item in suites) != 0
                    or sum(int(item.get("skipped", "-1")) for item in suites) != 0
                    or any(
                        item.find("failure") is not None
                        or item.find("error") is not None
                        or item.find("skipped") is not None
                        for item in cases
                    )
                ):
                    raise ValueError("image_gate_round_failed")
                observed = {
                    f"{item.get('classname', '')}::{item.get('name', '')}" for item in cases
                }
                expected = {
                    f"{node_id.split('::', 1)[0].removesuffix('.py').replace('/', '.')}::"
                    f"{node_id.split('::', 1)[1]}"
                    for node_id in _PYTEST_NODE_IDS
                }
                if observed != expected:
                    raise ValueError("image_gate_pytest_identity_invalid")
                junit_digests.append(hashlib.sha256(payload).hexdigest())
                rounds += 1
        duration = time.monotonic() - started
        if not math.isfinite(duration):
            raise ValueError("image_gate_duration_invalid")
        value = {
            "schema_version": 1,
            "evidence_type": "ecorex-image-shared-storage-execution",
            "gate_type": args.gate_type,
            "status": "passed",
            "commit_sha": args.commit_sha,
            "workflow_run_id": args.workflow_run_id,
            "node_ids": args.node_id,
            "jobs_per_round": args.jobs,
            "workers_per_round": args.workers,
            "pytest_node_ids": list(_PYTEST_NODE_IDS),
            "rounds_completed": rounds,
            "duration_seconds": duration,
            "pytest_junit_sha256": junit_digests,
        }
        output = args.output.resolve()
        if os.path.lexists(output):
            raise ValueError("image_gate_evidence_exists")
        write_new_json_file(value, output, code="image_gate_evidence_exists")
        print(json.dumps({"ok": True, "rounds": rounds, "duration_seconds": duration}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
