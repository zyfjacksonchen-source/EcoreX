#!/usr/bin/env python3
"""Build or finalize the production Linux/aarch64 cloud artifact."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import stat
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.deployment.cloud_artifact_builder import (  # noqa: E402
    CloudArtifactPipelineError,
    attach_detached_cloud_signature,
    build_linux_cloud_artifact,
    finalize_operator_waived_unsigned_artifact,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--source-root", type=Path, default=ROOT)
    build.add_argument("--artifact-root", type=Path, required=True)
    build.add_argument("--handoff-root", type=Path, required=True)
    build.add_argument("--release-id", required=True)
    build.add_argument("--expected-commit", required=True)
    attach = commands.add_parser("attach")
    attach.add_argument("--artifact-root", type=Path, required=True)
    attach.add_argument("--handoff-root", type=Path, required=True)
    attach.add_argument("--signature-response", type=Path, required=True)
    attach.add_argument("--release-keyring", type=Path, required=True)
    waive = commands.add_parser("waive")
    waive.add_argument("--artifact-root", type=Path, required=True)
    waive.add_argument("--handoff-root", type=Path, required=True)
    waive.add_argument("--operator-instruction-sha256", required=True)
    pack = commands.add_parser("pack")
    pack.add_argument("--artifact-root", type=Path, required=True)
    pack.add_argument("--archive", type=Path, required=True)
    unpack = commands.add_parser("unpack")
    unpack.add_argument("--archive", type=Path, required=True)
    unpack.add_argument("--artifact-root", type=Path, required=True)
    return parser


def _pack(root: Path, archive_path: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    if archive_path.exists() or archive_path.is_symlink():
        raise CloudArtifactPipelineError("cloud_transport_archive_invalid")
    members = sorted(root.rglob("*"), key=lambda item: item.as_posix())
    files = 0
    with tarfile.open(archive_path, "x:", format=tarfile.PAX_FORMAT) as bundle:
        for path in members:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not (
                stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
            ):
                raise CloudArtifactPipelineError("cloud_transport_member_invalid")
            mode = stat.S_IMODE(metadata.st_mode)
            if (stat.S_ISDIR(metadata.st_mode) and mode != 0o755) or (
                stat.S_ISREG(metadata.st_mode) and mode not in {0o644, 0o755}
            ):
                raise CloudArtifactPipelineError("cloud_transport_mode_invalid")
            relative = path.relative_to(root).as_posix()
            info = bundle.gettarinfo(str(path), arcname=relative)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            if stat.S_ISREG(metadata.st_mode):
                with path.open("rb") as stream:
                    bundle.addfile(info, stream)
                files += 1
            else:
                bundle.addfile(info)
    with archive_path.open("rb") as stream:
        os.fsync(stream.fileno())
    return {"member_count": len(members), "file_count": files}


def _unpack(archive_path: Path, root: Path) -> dict[str, object]:
    archive_path = archive_path.resolve(strict=True)
    if root.exists() or root.is_symlink():
        raise CloudArtifactPipelineError("cloud_transport_output_invalid")
    root.mkdir(mode=0o700)
    observed: set[str] = set()
    files = 0
    try:
        with tarfile.open(archive_path, "r:") as bundle:
            for member in bundle:
                path = PurePosixPath(member.name)
                if (
                    not path.parts
                    or path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or member.name in observed
                    or not (member.isdir() or member.isreg())
                    or member.mode not in ({0o755} if member.isdir() else {0o644, 0o755})
                ):
                    raise CloudArtifactPipelineError("cloud_transport_member_invalid")
                observed.add(member.name)
                target = root.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=member.mode)
                    target.chmod(member.mode)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise CloudArtifactPipelineError("cloud_transport_member_invalid")
                with source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                target.chmod(member.mode)
                files += 1
    except (OSError, tarfile.TarError):
        raise CloudArtifactPipelineError("cloud_transport_archive_invalid") from None
    # Extraction is private while incomplete. Only a fully validated transport
    # tree is published as read-only and traversable by the runtime identity.
    root.chmod(0o555)
    return {"member_count": len(observed), "file_count": files}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_linux_cloud_artifact(
                args.source_root,
                args.artifact_root,
                args.handoff_root,
                release_id=args.release_id,
                expected_commit=args.expected_commit,
            )
        elif args.command == "attach":
            result = attach_detached_cloud_signature(
                args.artifact_root,
                args.handoff_root,
                args.signature_response,
                args.release_keyring,
            )
        elif args.command == "waive":
            result = finalize_operator_waived_unsigned_artifact(
                args.artifact_root,
                args.handoff_root,
                operator_instruction_sha256=args.operator_instruction_sha256,
            )
        elif args.command == "pack":
            result = _pack(args.artifact_root, args.archive)
        else:
            result = _unpack(args.archive, args.artifact_root)
        print(json.dumps({"ok": True, **result}, sort_keys=True, separators=(",", ":")))
        return 0
    except (CloudArtifactPipelineError, OSError, ValueError):
        print('{"ok":false,"code":"cloud_artifact_pipeline_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
