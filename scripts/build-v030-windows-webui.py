#!/usr/bin/env python3
"""Build the Windows x64 e-Mate WebUI compatibility package locally.

This command does not sign, upload, deploy, or publish anything. Production
mode only accepts a Candidate already signed by an explicitly admitted key.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.windows_webui import (  # noqa: E402
    WindowsWebUIBuildError,
    build_windows_webui_package,
)


def _key(value: str) -> tuple[str, bytes]:
    try:
        key_id, raw_path = value.split("=", 1)
    except ValueError:
        raise argparse.ArgumentTypeError("key must use KEY_ID=FILE") from None
    path = Path(raw_path)
    try:
        payload = path.read_bytes()
    except OSError:
        raise argparse.ArgumentTypeError("public key file is unavailable") from None
    if not key_id or len(payload) != 32:
        raise argparse.ArgumentTypeError("Ed25519 public key contract is invalid")
    return key_id, payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--candidate-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--trusted-public-key", required=True, action="append", type=_key)
    parser.add_argument("--production-key-id", action="append", default=[])
    parser.add_argument("--non-production-fixture", action="store_true")
    parser.add_argument(
        "--generated-at",
        default=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    keys = dict(args.trusted_public_key)
    if len(keys) != len(args.trusted_public_key):
        print("windows_webui_public_key_duplicate", file=sys.stderr)
        return 2
    try:
        package, receipt = build_windows_webui_package(
            release_dir=args.release_dir,
            candidate_receipt_path=args.candidate_receipt,
            output_dir=args.output,
            trusted_public_keys=keys,
            generated_at=args.generated_at,
            production=not args.non_production_fixture,
            production_key_ids=frozenset(args.production_key_id),
        )
    except WindowsWebUIBuildError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps({"package": str(package), "receipt": str(receipt)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
