#!/usr/bin/env python3
"""Validate and externally sign one protected production deployment admission."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release import DigestPinnedExternalSigner  # noqa: E402
from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    strict_json_loads,
    write_new_json_file,
)
from ecorex.release.protected_deployment import (  # noqa: E402
    ProtectedDeploymentAdmissionError,
    sign_admission,
    verify_admission,
)


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("protected_deployment_signer_configuration_missing")
    return value


def _signer() -> DigestPinnedExternalSigner:
    try:
        public = base64.b64decode(
            _required("ECOREX_DEPLOYMENT_SIGNER_PUBLIC_KEY"), validate=True
        )
        executable_sha256 = _required("ECOREX_DEPLOYMENT_SIGNER_EXECUTABLE_SHA256")
        if len(public) != 32 or _SHA256.fullmatch(executable_sha256) is None:
            raise ValueError
        return DigestPinnedExternalSigner(
            key_id=_required("ECOREX_DEPLOYMENT_SIGNER_KEY_ID"),
            public_key=public,
            executable_path=_required("ECOREX_DEPLOYMENT_SIGNER_EXECUTABLE"),
            executable_sha256=executable_sha256,
            adapter_path=os.environ.get("ECOREX_DEPLOYMENT_SIGNER_ADAPTER") or None,
            adapter_sha256=(
                os.environ.get("ECOREX_DEPLOYMENT_SIGNER_ADAPTER_SHA256") or None
            ),
            environment=os.environ,
        )
    except (TypeError, ValueError):
        raise ValueError("protected_deployment_signer_configuration_invalid") from None


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unsigned", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        payload = read_stable_regular_file(
            args.unsigned,
            maximum_bytes=2 * 1024 * 1024,
            code="protected_deployment_admission_invalid",
        )
        unsigned = strict_json_loads(
            payload, code="protected_deployment_admission_invalid"
        )
        if not isinstance(unsigned, dict):
            raise ValueError("protected_deployment_admission_invalid")
        signer = _signer()
        document = sign_admission(unsigned, signer=signer)
        verify_admission(
            document,
            public_keys={signer.key_id: signer.public_key_bytes},
        )
        write_new_json_file(
            document,
            args.output.resolve(),
            code="protected_deployment_admission_exists",
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "admission_id": document["admission"]["admission_id"],
                    "key_id": signer.key_id,
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        code = (
            str(error)
            if isinstance(error, (ValueError, ProtectedDeploymentAdmissionError))
            else "protected_deployment_admission_signing_failed"
        )
        if re.fullmatch(r"[a-z][a-z0-9_]{2,127}", code) is None:
            code = "protected_deployment_admission_signing_failed"
        print(json.dumps({"ok": False, "code": code}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
