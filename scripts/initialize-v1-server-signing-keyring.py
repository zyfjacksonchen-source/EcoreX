#!/usr/bin/env python3
"""Initialize four independent software keys on an attested encrypted volume."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ecorex.security.encrypted_volume_signer import initialize_keyring


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-root", required=True, type=Path)
    parser.add_argument("--encryption-attestation-sha256", required=True)
    args = parser.parse_args()
    try:
        result = initialize_keyring(
            args.key_root,
            attestation_sha256=args.encryption_attestation_sha256,
        )
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print('{"ok":false,"code":"server_signer_initialization_failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
