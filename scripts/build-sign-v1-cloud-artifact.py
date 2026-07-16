#!/usr/bin/env python3
"""Sign one already-built Linux aarch64 cloud tree with the direct release key."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.deployment.cloud_artifact import (  # noqa: E402
    CloudArtifactBuildError,
    build_signed_cloud_artifact,
)
from ecorex.release import DigestPinnedExternalSigner  # noqa: E402
from ecorex.release.evidence_io import strict_json_loads  # noqa: E402
from ecorex.release.process_boundary import (  # noqa: E402
    BoundedProcessError,
    run_bounded_process,
)


ADAPTER = ROOT / "scripts" / "ecorex-v1-dpapi-ed25519-signer.py"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _exact_main(commit: str) -> None:
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=ROOT, capture_output=True, check=True, text=True
    ).stdout.strip()
    remote = subprocess.run(
        ("git", "rev-parse", "origin/main"),
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.splitlines()
    dirty = [line for line in status if not line[3:].replace("\\", "/").startswith(".artifacts/")]
    if head != commit or remote != commit or dirty:
        raise ValueError("cloud_artifact_exact_main_required")


def _signer() -> DigestPinnedExternalSigner:
    executable = Path(sys.executable).resolve(strict=True)
    adapter = ADAPTER.resolve(strict=True)
    try:
        result = run_bounded_process(
            (str(executable), str(adapter), "describe"),
            payload=None,
            cwd=ROOT,
            environment=os.environ,
            timeout_seconds=15,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=1024,
            hide_window=os.name == "nt",
        )
    except (OSError, BoundedProcessError):
        raise ValueError("cloud_artifact_release_signer_unavailable") from None
    value = strict_json_loads(result.stdout, code="cloud_artifact_release_signer_invalid")
    entry = value.get("release") if isinstance(value, dict) else None
    if result.returncode != 0 or not isinstance(entry, dict):
        raise ValueError("cloud_artifact_release_signer_invalid")
    try:
        public = base64.b64decode(entry.get("public_key_base64"), validate=True)
    except (TypeError, ValueError):
        raise ValueError("cloud_artifact_release_signer_invalid") from None
    key_id = entry.get("key_id")
    if (
        not isinstance(key_id, str)
        or len(public) != 32
        or hashlib.sha256(public).hexdigest() != entry.get("public_key_sha256")
    ):
        raise ValueError("cloud_artifact_release_signer_invalid")
    return DigestPinnedExternalSigner(
        key_id=key_id,
        public_key=public,
        executable_path=executable,
        executable_sha256=_sha256(executable),
        adapter_path=adapter,
        adapter_sha256=_sha256(adapter),
        environment=os.environ,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    try:
        _exact_main(args.expected_commit)
        result = build_signed_cloud_artifact(
            args.root, release_id=args.release_id, signer=_signer()
        )
        print(json.dumps({"ok": True, **result}, sort_keys=True, separators=(",", ":")))
        return 0
    except (CloudArtifactBuildError, ValueError, OSError, subprocess.SubprocessError):
        print('{"ok":false,"code":"cloud_artifact_build_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
