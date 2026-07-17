#!/usr/bin/env python3
"""Assemble an unsigned admission from exact prepared production bytes."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
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
    write_new_json_file,
)
from ecorex.release.protected_deployment import (  # noqa: E402
    validate_admission_payload,
)


def _sha256(path: Path) -> str:
    payload = read_stable_regular_file(
        path, maximum_bytes=2 * 1024 * 1024 * 1024, code="deployment_input_invalid"
    )
    return hashlib.sha256(payload).hexdigest()


def _json(path: Path) -> dict[str, object]:
    value = strict_json_loads(
        read_stable_regular_file(
            path, maximum_bytes=16 * 1024 * 1024, code="deployment_input_invalid"
        ),
        code="deployment_input_invalid",
    )
    if not isinstance(value, dict):
        raise ValueError("deployment_input_invalid")
    return value


def _tree(root: Path) -> str:
    tree = root.resolve(strict=True)
    if not tree.is_dir():
        raise ValueError("deployment_input_invalid")
    records: list[dict[str, object]] = []
    for path in sorted(tree.rglob("*")):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("deployment_input_invalid")
        if path.is_file():
            records.append(
                {
                    "path": path.relative_to(tree).as_posix(),
                    "sha256": _sha256(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--site-root", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--channel", required=True, choices=("canary", "stable"))
    parser.add_argument("--candidate-run-id", required=True, type=int)
    parser.add_argument("--candidate-run-attempt", required=True, type=int)
    parser.add_argument("--candidate-artifact-id", required=True, type=int)
    parser.add_argument("--candidate-artifact-sha256", required=True)
    parser.add_argument("--mode", required=True, choices=("create", "create-and-activate"))
    parser.add_argument("--rollout-percentage", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        root = args.candidate_root.resolve(strict=True)
        manifest_path = root / "output/release/release-manifest.json"
        candidate_receipt = root / "output/candidate-build-receipt.json"
        cloud_manifest = root / "output/cloud/cloud-release-manifest.json"
        public_index = root / "output/public-bootstrap-index.json"
        manifest = _json(manifest_path)
        receipt = _json(candidate_receipt)
        cloud = _json(cloud_manifest)
        if (
            manifest.get("release_id") != receipt.get("release_id")
            or manifest.get("version") != receipt.get("version")
            or cloud.get("release_id") != manifest.get("release_id")
        ):
            raise ValueError("deployment_input_identity_mismatch")
        gate_names = (
            "cdp-acceptance",
            "image-soak",
            "live-image",
            "live-model",
            "signature",
        )
        gate_paths = {name: root / f"gates/{name}.json" for name in gate_names}
        if not all(path.is_file() for path in gate_paths.values()):
            raise ValueError("deployment_gate_set_invalid")
        issued = datetime.now(timezone.utc).replace(microsecond=0)
        release_id = str(manifest["release_id"])
        admission = {
            "admission_id": (
                "deploy-"
                + hashlib.sha256(
                    (
                        f"{args.commit_sha}:{args.candidate_run_id}:"
                        f"{args.candidate_artifact_id}:{args.mode}:"
                        f"{args.rollout_percentage}"
                    ).encode()
                ).hexdigest()[:32]
            ),
            "repository": args.repository,
            "commit_sha": args.commit_sha,
            "channel": args.channel,
            "candidate": {
                "workflow_run_id": args.candidate_run_id,
                "run_attempt": args.candidate_run_attempt,
                "artifact_id": args.candidate_artifact_id,
                "artifact_sha256": args.candidate_artifact_sha256,
                "release_id": release_id,
                "version": str(manifest["version"]),
                "build_digest": str(manifest["build_digest"]),
            },
            "gates": {
                name.replace("-", "_"): _sha256(path)
                for name, path in gate_paths.items()
            },
            "targets": {
                "cloud": {
                    "artifact_sha256": _tree(root / "output/cloud"),
                    "manifest_sha256": _sha256(cloud_manifest),
                },
                "control_plane": {
                    "release_manifest_sha256": _sha256(manifest_path)
                },
                "public_site": {
                    "tree_sha256": _tree(args.site_root),
                    "public_index_sha256": _sha256(public_index),
                },
            },
            "decision": {
                "mode": args.mode,
                "rollout_percentage": args.rollout_percentage,
            },
            "issued_at": issued.isoformat().replace("+00:00", "Z"),
            "expires_at": (issued + timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
        }
        # The candidate gate is the signed candidate receipt itself rather than
        # a separately emitted gate file.
        admission["gates"]["candidate"] = _sha256(candidate_receipt)
        validate_admission_payload(admission)
        write_new_json_file(
            admission,
            args.output.resolve(),
            code="protected_deployment_admission_exists",
        )
        print(json.dumps({"ok": True, "admission_id": admission["admission_id"]}))
        return 0
    except Exception as error:
        code = str(error)
        if re.fullmatch(r"[a-z][a-z0-9_]{2,127}", code) is None:
            code = "protected_deployment_admission_assembly_failed"
        print(json.dumps({"ok": False, "code": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
