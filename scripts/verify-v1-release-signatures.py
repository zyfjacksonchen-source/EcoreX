#!/usr/bin/env python3
"""Independently verify all Candidate signatures using the protected public key."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.update import (  # noqa: E402
    Ed25519SignatureVerifier,
    ReleaseManifest,
    verify_artifact_file,
    verify_manifest_signature,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        key_id = os.environ.get("ECOREX_RELEASE_SIGNER_KEY_ID")
        encoded = os.environ.get("ECOREX_RELEASE_SIGNER_PUBLIC_KEY")
        if not isinstance(key_id, str) or not key_id or not isinstance(encoded, str):
            raise ValueError("release_verification_key_missing")
        try:
            public = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError):
            raise ValueError("release_verification_key_invalid") from None
        if len(public) != 32:
            raise ValueError("release_verification_key_invalid")
        root = args.release_dir.resolve(strict=True)
        manifest_path = root / "release-manifest.json"
        manifest = ReleaseManifest.from_json(manifest_path.read_bytes())
        verifier = Ed25519SignatureVerifier({key_id: public})
        verify_manifest_signature(manifest, verifier)
        artifacts: list[dict[str, str]] = []
        for artifact in manifest.artifacts:
            path = root / artifact.file_name
            verify_artifact_file(path, manifest, artifact, verifier)
            artifacts.append({"artifact_id": artifact.artifact_id, "sha256": artifact.sha256})
        value = {
            "schema_version": 1,
            "status": "passed",
            "release_id": manifest.release_id,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "key_id": key_id,
            "public_key_sha256": hashlib.sha256(public).hexdigest(),
            "artifacts": artifacts,
        }
        report = args.report.resolve()
        if report.exists():
            raise ValueError("signature_report_exists")
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(json.dumps({"ok": True, "release_id": manifest.release_id}, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
