#!/usr/bin/env python3
"""Replace the one Windows installer in a verified manual feed after signing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from typing import Any
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SEMVER = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
THUMBPRINT = re.compile(r"^[0-9A-F]{40}$")
RECORD_FIELDS = {"path", "role", "source_artifact", "size_bytes", "sha256"}
EVIDENCE_FIELDS = {
    "schema_version", "document_type", "status", "version", "source_commit",
    "base_feed_build_id", "file_name", "unsigned_sha256", "signed_sha256",
    "signed_size_bytes", "signature_status", "signer_certificate_thumbprint",
}
class FinalizeError(RuntimeError):
    pass
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-feed", required=True, type=Path)
    parser.add_argument("--signed-windows-installer", required=True, type=Path)
    parser.add_argument("--authenticode-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--expected-base-feed-build-id", required=True)
    parser.add_argument("--expected-signer-thumbprint", required=True)
    return parser
def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value
def _json(path: Path) -> dict[str, Any]:
    try:
        meta = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(meta.st_mode) or not 1 <= meta.st_size <= 16 * 1024 * 1024:
            raise FinalizeError(f"invalid JSON: {path.name}")
        value = json.loads(path.read_bytes().decode(), object_pairs_hook=_unique)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise FinalizeError(f"invalid JSON: {path.name}") from None
    if not isinstance(value, dict):
        raise FinalizeError(f"invalid JSON: {path.name}")
    return value
def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= 16 * 1024 * 1024 * 1024:
        raise FinalizeError(f"invalid file: {path.name}")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise FinalizeError(f"file changed while opening: {path.name}")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise FinalizeError(f"file changed while hashing: {path.name}")
    return digest.hexdigest()
def _snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = source.lstat()
    if source.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise FinalizeError(f"invalid file: {source.name}")
    with source.open("rb") as reader, destination.open("xb") as writer:
        opened = os.fstat(reader.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise FinalizeError(f"file changed while opening: {source.name}")
        shutil.copyfileobj(reader, writer, 1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
        after = os.fstat(reader.fileno())
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise FinalizeError(f"file changed while copying: {source.name}")
def _base(args: argparse.Namespace) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    root = args.base_feed.resolve(strict=True)
    receipt = _json(root / "feed-stage-receipt.json")
    if (
        receipt.get("schema_version") != 2
        or receipt.get("document_type") != "emate.desktop-feed-stage"
        or receipt.get("distribution_mode") != "unsigned-manual"
        or receipt.get("status") != "activation-ready-unsigned-manual"
        or receipt.get("version") != args.expected_version
        or receipt.get("source_commit") != args.expected_source_sha
        or receipt.get("feed_build_id") != args.expected_base_feed_build_id
    ):
        raise FinalizeError("base feed identity is invalid")
    records = receipt.get("files")
    if not isinstance(records, list) or records != sorted(records, key=lambda item: item.get("path", "")):
        raise FinalizeError("base feed inventory is invalid")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    expected: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != RECORD_FIELDS or not SHA256.fullmatch(str(record["sha256"])):
            raise FinalizeError("base feed inventory is invalid")
        relative = PurePosixPath(str(record["path"]))
        if relative.is_absolute() or ".." in relative.parts or str(relative) in expected:
            raise FinalizeError("base feed inventory is invalid")
        path = root / relative
        if path.stat().st_size != record["size_bytes"] or _sha(path) != record["sha256"]:
            raise FinalizeError("base feed inventory drift")
        expected.add(str(relative))
    computed = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if actual != expected | {"feed-stage-receipt.json"} or computed != receipt["feed_build_id"]:
        raise FinalizeError("base feed inventory is incomplete")
    return root, receipt, records
def finalize(args: argparse.Namespace) -> dict[str, Any]:
    if not SEMVER.fullmatch(args.expected_version) or not COMMIT.fullmatch(args.expected_source_sha) or not SHA256.fullmatch(args.expected_base_feed_build_id):
        raise FinalizeError("expected release identity is invalid")
    signer = args.expected_signer_thumbprint.upper()
    if not THUMBPRINT.fullmatch(signer):
        raise FinalizeError("expected signer is invalid")
    root, receipt, records = _base(args)
    output = args.output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    output = output.parent.resolve(strict=True) / output.name
    if output.is_relative_to(root) or os.path.lexists(output):
        raise FinalizeError("feed output overlaps the base feed")
    index = _json(root / "download-index.json")
    downloads = index.get("downloads")
    windows = [item for item in downloads if isinstance(item, dict) and item.get("target") == "windows-x64"] if isinstance(downloads, list) else []
    if index.get("schema_version") != 2 or index.get("distribution_mode") != "unsigned-manual" or index.get("version") != args.expected_version or len(windows) != 1:
        raise FinalizeError("base download index is invalid")
    name = windows[0].get("file_name")
    old = next((item for item in records if item["path"] == name), None)
    if not isinstance(name, str) or old is None or args.signed_windows_installer.name != name or windows[0].get("sha256") != old["sha256"] or windows[0].get("size_bytes") != old["size_bytes"]:
        raise FinalizeError("Windows installer identity is invalid")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for record in records:
            if record["path"] != name:
                _snapshot(root / PurePosixPath(record["path"]), staging / PurePosixPath(record["path"]))
        _snapshot(args.signed_windows_installer.resolve(strict=True), staging / name)
        evidence_path = staging / "windows-authenticode-receipt.json"
        _snapshot(args.authenticode_receipt.resolve(strict=True), evidence_path)
        evidence = _json(evidence_path)
        signed_sha = _sha(staging / name)
        if set(evidence) != EVIDENCE_FIELDS or (
            evidence.get("schema_version"), evidence.get("document_type"), evidence.get("status"), evidence.get("version"), evidence.get("source_commit"), evidence.get("base_feed_build_id"), evidence.get("file_name"), evidence.get("unsigned_sha256"), evidence.get("signed_sha256"), evidence.get("signed_size_bytes"), evidence.get("signature_status"), str(evidence.get("signer_certificate_thumbprint", "")).upper()
        ) != (1, "emate.windows-authenticode-receipt", "verified", args.expected_version, args.expected_source_sha, args.expected_base_feed_build_id, name, old["sha256"], signed_sha, (staging / name).stat().st_size, "Valid", signer) or signed_sha == old["sha256"]:
            raise FinalizeError("Authenticode evidence is invalid")
        for record in records:
            if record["path"] not in {name, "download-index.json"} and _sha(staging / PurePosixPath(record["path"])) != record["sha256"]:
                raise FinalizeError("untouched feed inventory drift")
        windows[0].update({"size_bytes": (staging / name).stat().st_size, "sha256": signed_sha, "authenticode": {"status": "verified", "signer_certificate_thumbprint": signer}})
        (staging / "download-index.json").write_text(json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        by_path = {item["path"]: dict(item) for item in records}
        by_path[name].update(size_bytes=(staging / name).stat().st_size, sha256=signed_sha)
        by_path["download-index.json"].update(size_bytes=(staging / "download-index.json").stat().st_size, sha256=_sha(staging / "download-index.json"))
        by_path[evidence_path.name] = {"path": evidence_path.name, "role": "immutable-desktop", "source_artifact": "windows-x64", "size_bytes": evidence_path.stat().st_size, "sha256": _sha(evidence_path)}
        final_records = sorted(by_path.values(), key=lambda item: item["path"])
        build_id = hashlib.sha256(json.dumps(final_records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt.update(files=final_records, feed_build_id=build_id, candidate_target=f"releases/v{args.expected_version}-{build_id[:16]}")
        (staging / "feed-stage-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return receipt


def main(argv: list[str] | None = None) -> int:
    try:
        result = finalize(_parser().parse_args(argv))
    except (FinalizeError, OSError, ValueError) as error:
        print(f"emate_windows_postsign_failed:{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
