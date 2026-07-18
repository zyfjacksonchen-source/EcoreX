#!/usr/bin/env python3
"""GET and verify all channel-required published v1 release bytes."""

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

from ecorex.release.online_verification import (  # noqa: E402
    OnlinePublicationVerificationError,
    OnlinePublicationVerifier,
    OnlineVerificationLimits,
)
from ecorex.update import Ed25519SignatureVerifier  # noqa: E402


_ENVIRONMENT = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--temporary-directory", type=Path)
    parser.add_argument(
        "--trusted-public-key",
        action="append",
        required=True,
        metavar="KEY_ID=BASE64",
    )
    parser.add_argument(
        "--github-token-env", default="ECOREX_GITHUB_RELEASE_READ_TOKEN"
    )
    parser.add_argument(
        "--checkpoint-key-env", default="ECOREX_PUBLICATION_CHECKPOINT_KEY_BASE64"
    )
    parser.add_argument(
        "--allowed-redirect-host",
        action="append",
        default=[],
        metavar="SOURCE_ID=HOST",
    )
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--connect-timeout-seconds", type=float, default=10)
    parser.add_argument("--read-timeout-seconds", type=float, default=90)
    parser.add_argument("--total-timeout-seconds", type=float, default=3600)
    parser.add_argument(
        "--maximum-total-bytes", type=int, default=16 * 1024 * 1024 * 1024
    )
    return parser


def _public_keys(values: list[str]) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for raw in values:
        key_id, separator, encoded = raw.partition("=")
        if not separator or key_id in result:
            raise OnlinePublicationVerificationError("trusted_public_keys_invalid")
        try:
            public = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError):
            raise OnlinePublicationVerificationError(
                "trusted_public_keys_invalid"
            ) from None
        if len(public) != 32:
            raise OnlinePublicationVerificationError("trusted_public_keys_invalid")
        result[key_id] = public
    return result


def _checkpoint_key(variable: str) -> bytes:
    if _ENVIRONMENT.fullmatch(variable) is None:
        raise OnlinePublicationVerificationError("checkpoint_key_environment_invalid")
    try:
        value = base64.b64decode(os.environ.get(variable, ""), validate=True)
    except (TypeError, ValueError):
        raise OnlinePublicationVerificationError("checkpoint_key_invalid") from None
    if len(value) != 32:
        raise OnlinePublicationVerificationError("checkpoint_key_invalid")
    return value


def _token(variable: str) -> str | None:
    if _ENVIRONMENT.fullmatch(variable) is None:
        raise OnlinePublicationVerificationError("github_token_environment_invalid")
    return os.environ.get(variable) or None


def _redirects(values: list[str]) -> dict[str, frozenset[str]]:
    prepared: dict[str, set[str]] = {}
    for raw in values:
        source_id, separator, host = raw.partition("=")
        if not separator or not source_id or not host:
            raise OnlinePublicationVerificationError("redirect_allowlist_invalid")
        prepared.setdefault(source_id, set()).add(host)
    return {key: frozenset(value) for key, value in prepared.items()}


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verifier = Ed25519SignatureVerifier(_public_keys(args.trusted_public_key))
        limits = OnlineVerificationLimits(
            attempts=args.attempts,
            connect_timeout_seconds=args.connect_timeout_seconds,
            read_timeout_seconds=args.read_timeout_seconds,
            total_timeout_seconds=args.total_timeout_seconds,
            maximum_total_bytes=args.maximum_total_bytes,
        )
        with OnlinePublicationVerifier(
            verifier=verifier,
            limits=limits,
            github_token=_token(args.github_token_env),
            checkpoint_key=_checkpoint_key(args.checkpoint_key_env),
            allowed_redirect_hosts=_redirects(args.allowed_redirect_host),
        ) as online:
            receipt = online.verify(
                release_dir=args.release_dir,
                output=args.output,
                checkpoint=args.checkpoint,
                temporary_directory=args.temporary_directory,
            )
        payload = args.output.read_bytes()
        print(
            json.dumps(
                {
                    "ok": True,
                    "release_id": receipt["release_id"],
                    "publication_policy": receipt["publication_policy"],
                    "publication_receipt_sha256": hashlib.sha256(payload).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except OnlinePublicationVerificationError as error:
        code = error.code
    except Exception:
        code = "online_publication_verification_failed"
    print(json.dumps({"ok": False, "code": code}, sort_keys=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(run())
