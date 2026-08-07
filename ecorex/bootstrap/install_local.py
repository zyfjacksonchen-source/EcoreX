"""Offline first-install handoff used only by the signed native Bootstrap.

The native Bootstrap downloads and verifies bytes from the public origins. This
module independently verifies the same signed manifest and artifacts, then
delegates the actual Core+Pack slot transaction to ``InstallCoordinator``.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
from pathlib import Path
import platform as platform_module
import stat
import sys

from ecorex.integration.pack_verification import verify_product_capability_pack
from ecorex.integration.windows_sandbox_security import WindowsSandboxSlotSecurity
from ecorex.update import (
    ActivationResult,
    Ed25519SignatureVerifier,
    InstallCoordinator,
    InstallState,
    LocalSourceFetcher,
    MAX_MANIFEST_BYTES,
    PreparedUpdate,
    ReleaseManifest,
    verify_manifest_signature,
)

from .cli import _read_public_keys
from .companion import BootstrapCompanionInstaller


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ecorex-bootstrap-install-local")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--sandbox-helper")
    parser.add_argument("--sandbox-helper-sha256")
    parser.add_argument("--desktop-directory")
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument(
        "--trusted-public-key",
        action="append",
        default=[],
        metavar="KEY_ID=FILE",
    )
    return parser


def _host_target() -> tuple[str, str]:
    machine = platform_module.machine().casefold()
    if os.name == "nt" and machine in {"amd64", "x86_64"}:
        return "windows", "x64"
    if sys.platform == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "macos", "arm64"
        if machine in {"amd64", "x86_64"}:
            return "macos", "x64"
    raise ValueError("unsupported Bootstrap host target")


def _regular_bytes(path_value: str, *, limit: int) -> bytes:
    path = Path(os.path.abspath(path_value))
    try:
        before = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(before.st_mode)
            or bool(getattr(before, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= limit
        ):
            raise ValueError("unsafe file")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except OSError:
        raise ValueError("Bootstrap manifest is unavailable") from None
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        len(payload) != before.st_size
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != identity
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != identity
        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        != identity
    ):
        raise ValueError("Bootstrap manifest changed while reading")
    return payload


def _real_directory(path_value: str) -> Path:
    path = Path(os.path.abspath(path_value))
    try:
        metadata = path.lstat()
    except OSError:
        raise ValueError("Bootstrap artifact directory is unavailable") from None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ValueError("Bootstrap artifact directory is unsafe")
    return path.resolve(strict=True)


def install(
    *,
    manifest_path: str,
    artifacts_path: str,
    install_root: str,
    trusted_public_keys: Sequence[str],
    sandbox_helper: str | None = None,
    sandbox_helper_sha256: str | None = None,
    desktop_directory: str | None = None,
    stage_only: bool = False,
) -> dict[str, object]:
    if not isinstance(stage_only, bool):
        raise ValueError("stage_only must be boolean")
    manifest = ReleaseManifest.from_json(
        _regular_bytes(manifest_path, limit=MAX_MANIFEST_BYTES)
    )
    verifier = Ed25519SignatureVerifier(_read_public_keys(trusted_public_keys))
    verify_manifest_signature(manifest, verifier)
    platform, architecture = _host_target()
    artifact_id = f"core-{platform}-{architecture}"
    artifact = manifest.artifact(artifact_id)
    if artifact.platform != platform or artifact.architecture != architecture:
        raise ValueError("Bootstrap Core target does not match this host")
    artifacts = _real_directory(artifacts_path)
    source_directories = {
        source.source_id: artifacts for source in manifest.sources
    }
    security = None
    if platform == "windows":
        if not sandbox_helper or not sandbox_helper_sha256:
            raise ValueError("Windows Bootstrap sandbox helper is required")
        security = WindowsSandboxSlotSecurity(
            Path(os.path.abspath(install_root)),
            sandbox_helper,
            expected_helper_sha256=sandbox_helper_sha256,
        )
    elif sandbox_helper is not None or sandbox_helper_sha256 is not None:
        raise ValueError("sandbox helper is only valid on Windows")
    local_fetcher = LocalSourceFetcher(source_directories)
    bootstrap_companion = BootstrapCompanionInstaller(
        Path(os.path.abspath(install_root)),
        platform=platform,
        architecture=architecture,
        verifier=verifier,
        fetcher=local_fetcher,
        windows_security_factory=(
            type(security) if security is not None else None
        ),
        desktop_directory=(
            Path(os.path.abspath(desktop_directory))
            if desktop_directory is not None
            else None
        ),
    )
    coordinator = InstallCoordinator(
        Path(os.path.abspath(install_root)),
        fetcher=local_fetcher,
        health_checker=lambda _slot: False,
        verifier=verifier,
        host_platform=platform,
        host_architecture=architecture,
        release_channel=manifest.channel,
        bootstrap_health_confirmation=True,
        bootstrap_companion=bootstrap_companion,
        pack_content_verifier=verify_product_capability_pack,
        payload_security_preparer=security.prepare if security is not None else None,
        payload_security_attester=security.attest if security is not None else None,
        payload_security_cleanup=(
            security.cleanup_failed if security is not None else None
        ),
        payload_security_orphan_cleanup=(
            security.cleanup_abandoned if security is not None else None
        ),
        slot_security_validator=security.validate if security is not None else None,
        slot_security_cleanup=security.cleanup_slot if security is not None else None,
    )

    recovered = coordinator.recover()
    prepared: PreparedUpdate | None = None
    activated: ActivationResult | None = None
    if isinstance(recovered, PreparedUpdate):
        if (
            recovered.release_id != manifest.release_id
            or recovered.version != manifest.version
            or recovered.build_digest != manifest.build_digest
            or recovered.artifact_id != artifact_id
        ):
            raise ValueError("a different verified Runtime update is already prepared")
        prepared = recovered
    elif (
        isinstance(recovered, ActivationResult)
        and recovered.state in {InstallState.HEALTHCHECKING, InstallState.COMPLETED}
        and recovered.current_slot is not None
        and coordinator.slots.release_manifest(recovered.current_slot) == manifest
    ):
        activated = recovered
    else:
        pointers = coordinator.slots.pointers()
        if pointers.current is not None:
            current_manifest = coordinator.slots.release_manifest(pointers.current)
            if current_manifest == manifest:
                bootstrap_companion.install_existing(manifest, artifacts)
                activated = ActivationResult(
                    transaction_id="existing-" + manifest.build_digest[:24],
                    state=InstallState.COMPLETED,
                    slot_id=pointers.current,
                    current_slot=pointers.current,
                    previous_slot=pointers.previous,
                )
            else:
                prepared = coordinator.prepare_update(
                    manifest,
                    artifact_id,
                    first_install=False,
                )
                if prepared.state is not InstallState.AWAITING_USER:
                    raise ValueError("Bootstrap update did not wait for confirmation")
        else:
            prepared = coordinator.prepare_update(
                manifest,
                artifact_id,
                first_install=True,
            )
            if prepared.state is not InstallState.AWAITING_USER:
                raise ValueError("first install did not wait for confirmation")
    if stage_only and prepared is not None:
        return {
            "schema_version": 1,
            "state": prepared.state.value,
            "transaction_id": prepared.transaction_id,
            "slot_id": prepared.slot_id,
        }
    if prepared is not None:
        activated = coordinator.activate(prepared.transaction_id)
    if activated is None:
        raise ValueError("Bootstrap update did not produce an activation result")
    if activated.state not in {InstallState.HEALTHCHECKING, InstallState.COMPLETED}:
        raise ValueError("first install did not reach Bootstrap health")
    if not activated.current_slot:
        raise ValueError("first install did not select a Runtime slot")
    return {
        "schema_version": 1,
        "state": activated.state.value,
        "transaction_id": activated.transaction_id,
        "slot_id": activated.current_slot,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = install(
            manifest_path=args.manifest,
            artifacts_path=args.artifacts,
            install_root=args.install_root,
            trusted_public_keys=args.trusted_public_key,
            sandbox_helper=args.sandbox_helper,
            sandbox_helper_sha256=args.sandbox_helper_sha256,
            desktop_directory=args.desktop_directory,
            stage_only=args.stage_only,
        )
    except Exception:
        print("EcoreX Bootstrap could not stage the verified Runtime.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
