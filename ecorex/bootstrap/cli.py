"""Minimal command-line entry point for the signed EcoreX Bootstrap."""

from __future__ import annotations

import argparse
import os
import signal
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

from ecorex.integration.pack_verification import verify_product_capability_pack
from ecorex.migration import (
    DEFAULT_SOURCE_VERSION,
    SUPPORTED_SOURCE_VERSIONS,
    ProductMigrationError,
    write_product_migration_plan,
)
from ecorex.update import (
    Ed25519SignatureVerifier,
    ProductFileLock,
    VerificationError,
)

from .errors import (
    BootstrapConfigurationError,
    BootstrapTrustError,
    RuntimeLaunchError,
)
from .supervisor import BootstrapExitCode, BootstrapSupervisor, RuntimeEndpoint


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ecorex-bootstrap")
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--trusted-public-key",
        action="append",
        default=[],
        metavar="KEY_ID=FILE",
        help="raw 32-byte Ed25519 public key; repeatable",
    )
    parser.add_argument("--max-requested-restarts", type=int, default=3)
    parser.add_argument(
        "--legacy-v030-source",
        help=(
            "installer-selected v0.3 data root; records a one-time copy-on-write "
            "migration plan before v1 starts"
        ),
    )
    parser.add_argument(
        "--legacy-source",
        help="installer-selected released data root for one-time copy-on-write migration",
    )
    parser.add_argument(
        "--legacy-source-version",
        choices=sorted(SUPPORTED_SOURCE_VERSIONS),
        default=DEFAULT_SOURCE_VERSION,
    )
    parser.add_argument(
        "--legacy-release-evidence",
        help="runtime-manifest.json or release.json from the installed legacy Runtime",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        keys = _read_public_keys(args.trusted_public_key)
        verifier = Ed25519SignatureVerifier(keys)
        if args.legacy_v030_source and args.legacy_source:
            raise BootstrapConfigurationError("Only one legacy source may be selected")
        legacy_source = args.legacy_source or args.legacy_v030_source
        legacy_version = (
            DEFAULT_SOURCE_VERSION
            if args.legacy_v030_source and not args.legacy_source
            else args.legacy_source_version
        )
        if args.legacy_release_evidence and not legacy_source:
            raise BootstrapConfigurationError(
                "Legacy release evidence requires a selected legacy source"
            )
        if legacy_source:
            install_root = Path(os.path.abspath(args.install_root))
            with ProductFileLock(install_root / "install-update.lock", timeout=10.0):
                write_product_migration_plan(
                    install_root,
                    Path(os.path.abspath(legacy_source)),
                    source_version=legacy_version,
                    release_evidence_file=(
                        Path(os.path.abspath(args.legacy_release_evidence))
                        if args.legacy_release_evidence
                        else None
                    ),
                )
        supervisor = BootstrapSupervisor(
            args.install_root,
            endpoint=RuntimeEndpoint(args.host, args.port),
            verifier=verifier,
            max_requested_restarts=args.max_requested_restarts,
            pack_content_verifier=verify_product_capability_pack,
        )
        prior_handlers: dict[int, object] = {}

        def stop(signum: int, _frame: object) -> None:
            supervisor.request_stop(signum)

        for signum in (int(signal.SIGINT), int(signal.SIGTERM)):
            prior_handlers[signum] = signal.signal(signum, stop)
        try:
            return supervisor.run().exit_code
        finally:
            for signum, handler in prior_handlers.items():
                signal.signal(signum, handler)
    except BootstrapTrustError:
        print("EcoreX Bootstrap refused an untrusted Runtime slot.", file=sys.stderr)
        return int(BootstrapExitCode.TRUST_FAILURE)
    except (
        BootstrapConfigurationError,
        ProductMigrationError,
        VerificationError,
        ValueError,
    ):
        print("EcoreX Bootstrap configuration is invalid.", file=sys.stderr)
        return int(BootstrapExitCode.CONFIGURATION)
    except RuntimeLaunchError:
        print("EcoreX Bootstrap could not launch the verified Runtime.", file=sys.stderr)
        return int(BootstrapExitCode.RUNTIME_FAILURE)


def _read_public_keys(definitions: Sequence[str]) -> dict[str, bytes]:
    if not definitions:
        raise BootstrapConfigurationError("At least one trusted public key is required")
    keys: dict[str, bytes] = {}
    for definition in definitions:
        if not isinstance(definition, str) or definition.count("=") != 1:
            raise BootstrapConfigurationError("Trusted public key definition is invalid")
        key_id, raw_path = definition.split("=", 1)
        if not key_id or not raw_path or key_id in keys:
            raise BootstrapConfigurationError("Trusted public key definition is invalid")
        path = Path(os.path.abspath(raw_path))
        current = path
        while True:
            try:
                ancestor = current.lstat()
            except OSError as exc:
                raise BootstrapConfigurationError(
                    "Trusted public key path is unreadable"
                ) from exc
            attributes = getattr(ancestor, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat.S_ISLNK(ancestor.st_mode) or bool(attributes & reparse_flag):
                raise BootstrapConfigurationError("Trusted public key path is unsafe")
            if current.parent == current:
                break
            current = current.parent
        try:
            metadata = path.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or bool(attributes & reparse_flag)
                or metadata.st_size != 32
            ):
                raise BootstrapConfigurationError("Trusted public key file is unsafe")
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    raise BootstrapConfigurationError(
                        "Trusted public key changed while opening"
                    )
                value = stream.read(33)
                after = os.fstat(stream.fileno())
            current_metadata = path.lstat()
        except OSError as exc:
            raise BootstrapConfigurationError("Trusted public key file is unreadable") from exc
        identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        if (
            len(value) != 32
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != identity
            or (
                current_metadata.st_dev,
                current_metadata.st_ino,
                current_metadata.st_size,
                current_metadata.st_mtime_ns,
            )
            != identity
        ):
            raise BootstrapConfigurationError("Trusted public key must contain 32 bytes")
        keys[key_id] = value
    return keys


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
