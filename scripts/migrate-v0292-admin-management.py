#!/usr/bin/env python3
"""Safely import released v0.2.9.2 Admin state into an empty v1 store."""

from __future__ import annotations

import argparse
import base64
import binascii
from datetime import datetime
import json
import os
from pathlib import Path
import sys

from ecorex.migration.legacy_admin_management import (
    LegacyAdminManagementImportError,
    import_v0292_admin_management,
)


def _key(args: argparse.Namespace) -> bytes | None:
    if args.dry_run:
        return None
    encoded = (
        sys.stdin.readline().strip()
        if args.encryption_key_stdin
        else os.environ.get(args.encryption_key_env, "").strip()
    )
    try:
        value = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise LegacyAdminManagementImportError(
            "v1 encryption authority is unavailable"
        ) from None
    if len(value) != 32:
        raise LegacyAdminManagementImportError("v1 encryption authority is unavailable")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy-on-write v0.2.9.2 Admin management migration"
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--as-of", type=str)
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument(
        "--encryption-key-env",
        default="ECOREX_CP_MODEL_CONFIG_ENCRYPTION_KEY_B64",
        metavar="ENV_NAME",
    )
    credentials.add_argument("--encryption-key-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        cutoff = datetime.fromisoformat(args.as_of) if args.as_of else None
        report = import_v0292_admin_management(
            args.source,
            args.target,
            encryption_key=_key(args),
            dry_run=args.dry_run,
            as_of=cutoff,
        )
    except (LegacyAdminManagementImportError, ValueError):
        print(
            json.dumps(
                {"schema_version": 1, "status": "failed", "error_code": "legacy_admin_import_failed"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {"status": "ok", **report.to_dict()},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
