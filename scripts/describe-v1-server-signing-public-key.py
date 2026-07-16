#!/usr/bin/env python3
"""Emit one bounded public key description; never emits private key material."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ecorex.security.encrypted_volume_signer import ROLES, public_key_description


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-root", required=True, type=Path)
    parser.add_argument("--role", required=True, choices=ROLES)
    args = parser.parse_args()
    try:
        value = public_key_description(args.key_root, role=args.role)
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print('{"ok":false,"code":"server_signer_description_failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
