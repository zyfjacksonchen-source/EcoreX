#!/usr/bin/env python3
"""Verify and expand one signed Runtime into the packaged desktop bundle."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import platform as host_platform
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ecorex.integration.pack_verification import verify_product_capability_pack  # noqa: E402
from ecorex.update import (  # noqa: E402
    Ed25519SignatureVerifier,
    ReleaseManifest,
    SlotStore,
    verify_artifact_file,
    verify_manifest_signature,
)
from ecorex.update.coordinator import _slot_id  # noqa: E402
from ecorex.update.pack_install import (  # noqa: E402
    PreparedPackSet,
    resolve_release_pack_set,
    verify_prepared_pack_set,
)


def _host_target() -> tuple[str, str]:
    platform = "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "unsupported"
    architecture = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(host_platform.machine().casefold(), "unsupported")
    return platform, architecture


def _public_keys(config_path: Path) -> dict[str, bytes]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    encoded = raw.get("release_public_keys")
    if not isinstance(encoded, dict) or not encoded:
        raise ValueError("Bootstrap release keyring is invalid")
    keys: dict[str, bytes] = {}
    for key_id, value in encoded.items():
        if not isinstance(key_id, str) or not isinstance(value, str):
            raise ValueError("Bootstrap release keyring is invalid")
        key = base64.b64decode(value, validate=True)
        if len(key) != 32:
            raise ValueError("Bootstrap release keyring is invalid")
        keys[key_id] = key
    return keys


def stage(
    *,
    target: tuple[str, str],
    config_path: Path,
    release_dir: Path,
    destination: Path,
) -> None:
    if target != _host_target():
        raise ValueError("Runtime must be expanded on its target operating system and architecture")
    manifest_path = release_dir / "release-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = ReleaseManifest.from_json(manifest_bytes)
    verifier = Ed25519SignatureVerifier(_public_keys(config_path))
    verify_manifest_signature(manifest, verifier)
    platform, architecture = target
    core = manifest.artifact(f"core-{platform}-{architecture}")
    core_path = release_dir / core.file_name
    verify_artifact_file(core_path, manifest, core, verifier)
    pack_set = resolve_release_pack_set(
        manifest,
        platform=platform,
        architecture=architecture,
        verifier=verifier,
    )
    if pack_set is None:
        raise ValueError("Desktop Runtime requires the complete default Capability Pack set")

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".emate-runtime-stage-", dir=destination.parent) as temporary_name:
        temporary = Path(temporary_name)
        pack_inputs = temporary / "pack-inputs"
        pack_inputs.mkdir()
        package_paths: dict[str, Path] = {}
        for artifact in pack_set.artifacts:
            source = release_dir / artifact.file_name
            verify_artifact_file(source, manifest, artifact, verifier)
            copied = pack_inputs / artifact.file_name
            shutil.copyfile(source, copied)
            package_paths[artifact.artifact_id] = copied
        prepared = PreparedPackSet(pack_set=pack_set, package_paths=package_paths)
        verify_prepared_pack_set(
            manifest,
            prepared,
            verifier=verifier,
            pack_content_verifier=verify_product_capability_pack,
        )

        store = SlotStore(temporary / "install")
        slot_id = _slot_id(manifest, core)
        slot = store.stage(
            core_path,
            slot_id=slot_id,
            manifest=manifest,
            artifact=core,
            payload_enricher=prepared.payload_enricher,
        )
        bundle = temporary / "bundle"
        bundle.mkdir()
        os.replace(slot / "payload", bundle / "payload")
        marker = json.loads((slot / ".slot.json").read_text(encoding="utf-8"))
        marker["created_at"] = json.loads(manifest_bytes)["created_at"]
        (bundle / ".slot.json").write_text(
            json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        (bundle / "release-manifest.json").write_bytes(manifest_bytes)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise ValueError("Runtime bundle destination is unsafe")
            shutil.rmtree(destination)
        os.replace(bundle, destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=("macos", "windows"))
    parser.add_argument("--architecture", required=True, choices=("arm64", "x64"))
    parser.add_argument("--bootstrap-config", required=True, type=Path)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    stage(
        target=(args.platform, args.architecture),
        config_path=args.bootstrap_config.resolve(strict=True),
        release_dir=args.release_dir.resolve(strict=True),
        destination=args.destination,
    )
    print(f"Staged expanded e-Mate Runtime at {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
