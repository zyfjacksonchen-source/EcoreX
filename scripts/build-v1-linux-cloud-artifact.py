#!/usr/bin/env python3
"""Build or finalize the production Linux/aarch64 cloud artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.deployment.cloud_artifact_builder import (  # noqa: E402
    CloudArtifactPipelineError,
    attach_detached_cloud_signature,
    build_linux_cloud_artifact,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--source-root", type=Path, default=ROOT)
    build.add_argument("--artifact-root", type=Path, required=True)
    build.add_argument("--handoff-root", type=Path, required=True)
    build.add_argument("--release-id", required=True)
    build.add_argument("--expected-commit", required=True)
    attach = commands.add_parser("attach")
    attach.add_argument("--artifact-root", type=Path, required=True)
    attach.add_argument("--handoff-root", type=Path, required=True)
    attach.add_argument("--signature-response", type=Path, required=True)
    attach.add_argument("--release-keyring", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_linux_cloud_artifact(
                args.source_root,
                args.artifact_root,
                args.handoff_root,
                release_id=args.release_id,
                expected_commit=args.expected_commit,
            )
        else:
            result = attach_detached_cloud_signature(
                args.artifact_root,
                args.handoff_root,
                args.signature_response,
                args.release_keyring,
            )
        print(json.dumps({"ok": True, **result}, sort_keys=True, separators=(",", ":")))
        return 0
    except (CloudArtifactPipelineError, OSError, ValueError):
        print('{"ok":false,"code":"cloud_artifact_pipeline_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
