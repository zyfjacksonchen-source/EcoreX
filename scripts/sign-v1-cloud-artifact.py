#!/usr/bin/env python3
"""Externally sign a detached Linux cloud-manifest payload.

The signing runner receives immutable handoff bytes only. It never builds,
finalizes or executes the cloud artifact.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.deployment.cloud_artifact_builder import (  # noqa: E402
    CloudArtifactPipelineError,
    create_detached_signature_response_from_payload,
    read_detached_signing_payload,
)
from ecorex.release import DigestPinnedExternalSigner  # noqa: E402


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("cloud_manifest_signer_configuration_missing")
    return value


def _signer() -> DigestPinnedExternalSigner:
    try:
        public = base64.b64decode(
            _required("ECOREX_RELEASE_SIGNER_PUBLIC_KEY"), validate=True
        )
        executable_sha = _required("ECOREX_RELEASE_SIGNER_EXECUTABLE_SHA256")
        if len(public) != 32 or _SHA256.fullmatch(executable_sha) is None:
            raise ValueError
        return DigestPinnedExternalSigner(
            key_id=_required("ECOREX_RELEASE_SIGNER_KEY_ID"),
            public_key=public,
            executable_path=_required("ECOREX_RELEASE_SIGNER_EXECUTABLE"),
            executable_sha256=executable_sha,
            adapter_path=os.environ.get("ECOREX_RELEASE_SIGNER_ADAPTER") or None,
            adapter_sha256=os.environ.get("ECOREX_RELEASE_SIGNER_ADAPTER_SHA256")
            or None,
            environment=os.environ,
        )
    except (TypeError, ValueError):
        raise ValueError("cloud_manifest_signer_configuration_invalid") from None


def _write_new(path: Path, value: dict[str, object]) -> None:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--descriptor", required=True, type=Path)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        descriptor, payload = read_detached_signing_payload(
            args.descriptor, args.payload
        )
        signer = _signer()
        response = create_detached_signature_response_from_payload(
            descriptor,
            payload,
            key_id=signer.key_id,
            signature=signer.sign(payload),
        )
        _write_new(args.output, response)
        print(
            json.dumps(
                {
                    "ok": True,
                    "key_id": signer.key_id,
                    "payload_sha256": hashlib.sha256(payload).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (CloudArtifactPipelineError, OSError, ValueError):
        print('{"ok":false,"code":"cloud_manifest_signing_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
