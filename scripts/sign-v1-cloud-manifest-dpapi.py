#!/usr/bin/env python3
"""Windows-only detached signer for a Linux-built canonical cloud manifest."""

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

from ecorex.deployment.cloud_artifact_builder import (  # noqa: E402
    CloudArtifactPipelineError,
    create_detached_signature_response_from_payload,
    read_detached_signing_payload,
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


def _signer() -> DigestPinnedExternalSigner:
    if os.name != "nt":
        raise CloudArtifactPipelineError("cloud_dpapi_signer_windows_required")
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
            hide_window=True,
        )
    except (OSError, BoundedProcessError):
        raise CloudArtifactPipelineError("cloud_dpapi_signer_unavailable") from None
    value = strict_json_loads(result.stdout, code="cloud_dpapi_signer_invalid")
    entry = value.get("release") if isinstance(value, dict) else None
    if result.returncode != 0 or not isinstance(entry, dict):
        raise CloudArtifactPipelineError("cloud_dpapi_signer_invalid")
    try:
        public = base64.b64decode(entry.get("public_key_base64"), validate=True)
    except (TypeError, ValueError):
        raise CloudArtifactPipelineError("cloud_dpapi_signer_invalid") from None
    key_id = entry.get("key_id")
    if (
        not isinstance(key_id, str)
        or len(public) != 32
        or hashlib.sha256(public).hexdigest() != entry.get("public_key_sha256")
    ):
        raise CloudArtifactPipelineError("cloud_dpapi_signer_invalid")
    return DigestPinnedExternalSigner(
        key_id=key_id,
        public_key=public,
        executable_path=executable,
        executable_sha256=_sha256(executable),
        adapter_path=adapter,
        adapter_sha256=_sha256(adapter),
        environment=os.environ,
    )


def _write_new(path: Path, value: dict[str, object]) -> None:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        descriptor, payload = read_detached_signing_payload(
            args.descriptor, args.payload
        )
        signer = _signer()
        signature = signer.sign(payload)
        response = create_detached_signature_response_from_payload(
            descriptor,
            payload,
            key_id=signer.key_id,
            signature=signature,
        )
        _write_new(args.output, response)
        print(
            json.dumps(
                {
                    "ok": True,
                    "key_id": signer.key_id,
                    "payload_sha256": response["payload_sha256"],
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
    raise SystemExit(main())
