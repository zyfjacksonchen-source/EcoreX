#!/usr/bin/env python3
"""Atomically activate one verified e-Mate desktop feed candidate."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from ecorex.update.locking import LockUnavailable, ProductFileLock


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SEMVER = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_TARGET = re.compile(
    r"^releases/v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)-[0-9a-f]{16}$"
)
_POINTER_FILES = [
    "latest.yml",
    "latest-mac.yml",
    "download-index.json",
    "public-bootstrap-index.json",
]
_RECEIPT_FIELDS = [
    "operation",
    "feed_build_id",
    "previous_target",
    "new_target",
    "manifest_sha256",
    "public_readback_sha256",
    "completed_at",
]
_MAX_FILES = 500
_MAX_FILE_BYTES = 16 * 1024 * 1024 * 1024
_MAX_JSON_BYTES = 16 * 1024 * 1024


class FeedDeployError(RuntimeError):
    pass


class ReadbackError(FeedDeployError):
    def __init__(self, observed: bytes = b"") -> None:
        super().__init__("readback_failed")
        self.observed = observed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--activation-receipt", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--expected-release-id", required=True)
    parser.add_argument("--expected-build-digest", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    verifier = parser.add_mutually_exclusive_group(required=True)
    verifier.add_argument("--readback-command", type=Path)
    verifier.add_argument("--readback-url")
    parser.add_argument("--readback-argument", action="append", default=[])
    parser.add_argument("--readback-host")
    parser.add_argument("--readback-proxy")
    parser.add_argument("--readback-timeout-seconds", type=float, default=60.0)
    return parser


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _safe_absolute(path: Path, label: str) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise FeedDeployError(f"{label}_path_invalid")
    normalized = Path(os.path.abspath(path))
    try:
        metadata = normalized.lstat()
    except OSError:
        raise FeedDeployError(f"{label}_unavailable") from None
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise FeedDeployError(f"{label}_invalid")
    return normalized


def _safe_directory(path: Path, label: str, device: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        raise FeedDeployError(f"{label}_unavailable") from None
    if (
        _is_link_or_reparse(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != device
    ):
        raise FeedDeployError(f"{label}_invalid")
    return metadata


def _strict_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            _is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or not 1 <= metadata.st_size <= _MAX_JSON_BYTES
        ):
            raise FeedDeployError("stage_receipt_invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            payload = b""
            while len(payload) <= _MAX_JSON_BYTES:
                chunk = os.read(descriptor, min(1024 * 1024, _MAX_JSON_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload += chunk
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            len(payload) != metadata.st_size
            or before.st_ino != metadata.st_ino
            or before.st_dev != metadata.st_dev
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        ):
            raise FeedDeployError("stage_receipt_changed")

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
    except FeedDeployError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise FeedDeployError("stage_receipt_invalid") from None
    if not isinstance(value, dict):
        raise FeedDeployError("stage_receipt_invalid")
    return value


def _safe_record_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise FeedDeployError("inventory_path_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in ("", ".", "..") for part in path.parts):
        raise FeedDeployError("inventory_path_invalid")
    if any(not _SAFE_ID.fullmatch(part) for part in path.parts):
        raise FeedDeployError("inventory_path_invalid")
    return path


def _hash_file(path: Path, expected_size: int, expected_sha256: str) -> None:
    try:
        metadata = path.lstat()
        if (
            _is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != expected_size
        ):
            raise FeedDeployError("inventory_file_invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(descriptor)
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_FILE_BYTES:
                    raise FeedDeployError("inventory_file_too_large")
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except FeedDeployError:
        raise
    except OSError:
        raise FeedDeployError("inventory_file_unavailable") from None
    if (
        before.st_dev != metadata.st_dev
        or before.st_ino != metadata.st_ino
        or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        or size != expected_size
        or digest.hexdigest() != expected_sha256
    ):
        raise FeedDeployError("inventory_hash_mismatch")


def _verified_bytes(path: Path, expected_size: int, expected_sha256: str) -> bytes:
    if expected_size > _MAX_JSON_BYTES:
        raise FeedDeployError("public_index_too_large")
    try:
        metadata = path.lstat()
        if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise FeedDeployError("public_index_invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(descriptor)
            payload = b""
            while len(payload) <= expected_size:
                chunk = os.read(descriptor, expected_size + 1 - len(payload))
                if not chunk:
                    break
                payload += chunk
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except FeedDeployError:
        raise
    except OSError:
        raise FeedDeployError("public_index_unavailable") from None
    if (
        before.st_dev != metadata.st_dev
        or before.st_ino != metadata.st_ino
        or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        or len(payload) != expected_size
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise FeedDeployError("public_index_changed")
    return payload


def _validate_inventory(candidate: Path, receipt: dict[str, Any], device: int) -> bytes:
    raw_files = receipt["files"]
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= _MAX_FILES:
        raise FeedDeployError("inventory_invalid")
    records: list[dict[str, Any]] = []
    expected: set[str] = set()
    folded: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {
            "path", "role", "source_artifact", "size_bytes", "sha256"
        }:
            raise FeedDeployError("inventory_record_invalid")
        relative = _safe_record_path(raw["path"])
        value = str(relative)
        if value in expected or value.casefold() in folded:
            raise FeedDeployError("inventory_path_duplicate")
        if raw["role"] not in {"pointer", "immutable-desktop", "immutable-runtime"}:
            raise FeedDeployError("inventory_role_invalid")
        if not isinstance(raw["source_artifact"], str) or not _SAFE_ID.fullmatch(raw["source_artifact"]):
            raise FeedDeployError("inventory_source_invalid")
        size = raw["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= _MAX_FILE_BYTES:
            raise FeedDeployError("inventory_size_invalid")
        if not isinstance(raw["sha256"], str) or not _SHA256.fullmatch(raw["sha256"]):
            raise FeedDeployError("inventory_hash_invalid")
        expected.add(value)
        folded.add(value.casefold())
        records.append(raw)

    canonical = sorted(records, key=lambda item: item["path"])
    if records != canonical:
        raise FeedDeployError("inventory_order_invalid")
    computed_build_id = hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if computed_build_id != receipt["feed_build_id"]:
        raise FeedDeployError("feed_build_id_mismatch")

    manifest_path = f"runtime/{receipt['release_id']}/release-manifest.json"
    manifests = [item for item in records if item["path"] == manifest_path]
    if (
        len(manifests) != 1
        or manifests[0]["role"] != "immutable-runtime"
        or manifests[0]["sha256"] != receipt["runtime_manifest_sha256"]
    ):
        raise FeedDeployError("runtime_manifest_inventory_mismatch")
    public_records = [item for item in records if item["path"] == "public-bootstrap-index.json"]
    if len(public_records) != 1 or public_records[0]["role"] != "pointer":
        raise FeedDeployError("public_index_inventory_missing")

    actual: set[str] = set()
    directory_count = 0
    for directory, directories, filenames in os.walk(candidate, topdown=True, followlinks=False):
        directory_count += 1
        if directory_count > _MAX_FILES + 1:
            raise FeedDeployError("inventory_too_large")
        base = Path(directory)
        _safe_directory(base, "candidate_directory", device)
        for name in tuple(directories):
            _safe_directory(base / name, "candidate_directory", device)
        for name in filenames:
            path = base / name
            try:
                metadata = path.lstat()
            except OSError:
                raise FeedDeployError("inventory_file_unavailable") from None
            if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_dev != device:
                raise FeedDeployError("inventory_file_invalid")
            relative = path.relative_to(candidate).as_posix()
            actual.add(relative)
            if len(actual) > _MAX_FILES + 1:
                raise FeedDeployError("inventory_too_large")
    if actual != expected | {"feed-stage-receipt.json"}:
        raise FeedDeployError("inventory_not_complete")

    for item in records:
        _hash_file(candidate / PurePosixPath(item["path"]), item["size_bytes"], item["sha256"])
    return _verified_bytes(
        candidate / "public-bootstrap-index.json",
        public_records[0]["size_bytes"],
        public_records[0]["sha256"],
    )


def _validate_stage(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any], bytes]:
    if os.name != "posix":
        raise FeedDeployError("platform_unsupported")
    root = _safe_absolute(args.root, "root")
    root_metadata = root.lstat()
    releases = root / "releases"
    _safe_directory(releases, "releases", root_metadata.st_dev)
    candidate = _safe_absolute(args.candidate, "candidate")
    if candidate.parent != releases or candidate.lstat().st_dev != root_metadata.st_dev:
        raise FeedDeployError("candidate_boundary_invalid")

    receipt = _strict_json(candidate / "feed-stage-receipt.json")
    expected_keys = {
        "schema_version", "document_type", "status", "version", "source_commit",
        "release_id", "build_digest", "runtime_manifest_sha256", "feed_build_id",
        "candidate_target", "nginx_config_sha256", "files", "activation",
    }
    if set(receipt) != expected_keys:
        raise FeedDeployError("stage_receipt_fields_invalid")
    if receipt.get("schema_version") != 1 or receipt.get("document_type") != "emate.desktop-feed-stage":
        raise FeedDeployError("stage_receipt_contract_invalid")
    if receipt.get("status") != "activation-ready":
        raise FeedDeployError("stage_not_activation_ready")
    identities = (
        ("version", args.expected_version, _SEMVER),
        ("source_commit", args.expected_source_sha, _COMMIT),
        ("release_id", args.expected_release_id, _SAFE_ID),
        ("build_digest", args.expected_build_digest, _SHA256),
        ("runtime_manifest_sha256", args.expected_manifest_sha256, _SHA256),
    )
    for field, expected, pattern in identities:
        if not isinstance(expected, str) or not pattern.fullmatch(expected) or receipt.get(field) != expected:
            raise FeedDeployError(f"{field}_mismatch")
    for field in ("feed_build_id", "nginx_config_sha256"):
        if not isinstance(receipt.get(field), str) or not _SHA256.fullmatch(receipt[field]):
            raise FeedDeployError(f"{field}_invalid")
    target = f"releases/v{args.expected_version}-{receipt['feed_build_id'][:16]}"
    if receipt.get("candidate_target") != target or not _TARGET.fullmatch(target):
        raise FeedDeployError("candidate_target_invalid")
    if candidate != root / PurePosixPath(target):
        raise FeedDeployError("candidate_identity_mismatch")
    activation = receipt.get("activation")
    if not isinstance(activation, dict) or activation != {
        "strategy": "same-filesystem-current-symlink-rename",
        "allowed_operations": ["activate", "rollback"],
        "link": "/srv/e-mate-update/current",
        "pointer_files": _POINTER_FILES,
        "missing_files_must_return": 404,
        "receipt_required_fields": _RECEIPT_FIELDS,
    }:
        raise FeedDeployError("activation_contract_invalid")
    public_bytes = _validate_inventory(candidate, receipt, root_metadata.st_dev)
    return root, candidate, receipt, public_bytes


def _current_target(root: Path, device: int) -> str | None:
    current = root / "current"
    if not os.path.lexists(current):
        return None
    try:
        metadata = current.lstat()
        target = os.readlink(current)
    except OSError:
        raise FeedDeployError("current_invalid") from None
    if not stat.S_ISLNK(metadata.st_mode) or metadata.st_dev != device:
        raise FeedDeployError("current_invalid")
    if not _TARGET.fullmatch(target):
        raise FeedDeployError("current_target_invalid")
    destination = root / PurePosixPath(target)
    _safe_directory(destination, "current_target", device)
    return target


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_current(root: Path, target: str) -> None:
    temporary = root / f".current.activate-{os.getpid()}-{os.urandom(8).hex()}"
    try:
        os.symlink(target, temporary)
        os.replace(temporary, root / "current")
        _fsync_directory(root)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _rollback(root: Path, candidate_target: str, previous_target: str | None) -> None:
    current = root / "current"
    try:
        if not current.is_symlink() or os.readlink(current) != candidate_target:
            raise FeedDeployError("rollback_fence_lost")
        if previous_target is None:
            os.unlink(current)
            _fsync_directory(root)
        else:
            _replace_current(root, previous_target)
    except OSError:
        raise FeedDeployError("rollback_failed") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_readback_options(args: argparse.Namespace) -> None:
    if not 0 < args.readback_timeout_seconds <= 300:
        raise FeedDeployError("readback_timeout_invalid")
    if args.readback_command is not None:
        if args.readback_url or args.readback_host or args.readback_proxy:
            raise FeedDeployError("readback_options_invalid")
        command = args.readback_command
        if not command.is_absolute() or ".." in command.parts:
            raise FeedDeployError("readback_command_invalid")
        try:
            metadata = command.lstat()
        except OSError:
            raise FeedDeployError("readback_command_invalid") from None
        if (
            _is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or not os.access(command, os.X_OK)
        ):
            raise FeedDeployError("readback_command_invalid")
        return
    if args.readback_argument:
        raise FeedDeployError("readback_options_invalid")
    try:
        parsed = urllib.parse.urlsplit(args.readback_url)
        port = parsed.port
    except ValueError:
        raise FeedDeployError("readback_url_invalid") from None
    expected_host = args.readback_host
    if (
        not expected_host
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.scheme not in {"http", "https"}
        or (parsed.scheme == "http" and not _is_loopback(parsed.hostname or ""))
        or (parsed.scheme == "https" and port not in (None, 443))
    ):
        raise FeedDeployError("readback_url_invalid")
    if args.readback_proxy:
        try:
            proxy = urllib.parse.urlsplit(args.readback_proxy)
            proxy.port
        except ValueError:
            raise FeedDeployError("readback_proxy_invalid") from None
        if (
            proxy.scheme not in {"http", "https"}
            or not proxy.hostname
            or proxy.username
            or proxy.password
            or proxy.path not in ("", "/")
            or proxy.query
            or proxy.fragment
        ):
            raise FeedDeployError("readback_proxy_invalid")


def _http_readback(args: argparse.Namespace, maximum: int) -> bytes:
    handlers: list[Any] = [_NoRedirect()]
    if args.readback_proxy:
        handlers.append(urllib.request.ProxyHandler({"http": args.readback_proxy, "https": args.readback_proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(args.readback_url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    observed = b""
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=args.readback_timeout_seconds) as response:
            if response.status != 200:
                raise ReadbackError()
            media_type = response.headers.get_content_type()
            encoding = (response.headers.get("Content-Encoding") or "identity").lower()
            cache_control = (response.headers.get("Cache-Control") or "").lower()
            if media_type != "application/json" or encoding != "identity" or "no-store" not in cache_control:
                raise ReadbackError()
            observed = response.read(maximum + 1)
    except ReadbackError:
        raise
    except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError):
        raise ReadbackError(observed) from None
    if len(observed) > maximum:
        raise ReadbackError(observed)
    return observed


def _command_readback(args: argparse.Namespace, maximum: int) -> bytes:
    command = args.readback_command
    assert command is not None
    try:
        result = subprocess.run(
            [os.fspath(command), *args.readback_argument],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=args.readback_timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        observed = error.stdout if isinstance(error, subprocess.TimeoutExpired) and isinstance(error.stdout, bytes) else b""
        raise ReadbackError(observed) from None
    if result.returncode != 0 or len(result.stdout) > maximum:
        raise ReadbackError(result.stdout[: maximum + 1])
    return result.stdout


def _readback(args: argparse.Namespace, expected: bytes) -> bytes:
    if args.readback_command is not None:
        observed = _command_readback(args, len(expected))
    else:
        observed = _http_readback(args, len(expected))
    if observed != expected:
        raise ReadbackError(observed)
    return observed


def _receipt_path(root: Path, requested: Path) -> Path:
    if not requested.is_absolute() or ".." in requested.parts:
        raise FeedDeployError("activation_receipt_path_invalid")
    receipts = root / "activation-receipts"
    if os.path.lexists(receipts):
        _safe_directory(receipts, "activation_receipts", root.lstat().st_dev)
    else:
        receipts.mkdir(mode=0o700)
        _fsync_directory(root)
    path = Path(os.path.abspath(requested))
    if path.parent != receipts or os.path.lexists(path):
        raise FeedDeployError("activation_receipt_path_invalid")
    return path


def _write_receipt(path: Path, value: dict[str, Any]) -> None:
    if list(value) != _RECEIPT_FIELDS or len(value) != 7:
        raise FeedDeployError("activation_receipt_invalid")
    payload = json.dumps(value, sort_keys=False, separators=(",", ":")).encode("utf-8") + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _completed_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def activate(args: argparse.Namespace) -> dict[str, Any]:
    root = _safe_absolute(args.root, "root")
    _validate_readback_options(args)
    lock = ProductFileLock(root / ".e-mate-feed-activation.lock", timeout=0)
    try:
        with lock:
            root, _candidate, stage, public_bytes = _validate_stage(args)
            output = _receipt_path(root, args.activation_receipt)
            device = root.lstat().st_dev
            previous = _current_target(root, device)
            candidate_target = stage["candidate_target"]
            _replace_current(root, candidate_target)
            try:
                observed = _readback(args, public_bytes)
            except ReadbackError as error:
                try:
                    _rollback(root, candidate_target, previous)
                except FeedDeployError:
                    raise FeedDeployError("readback_failed_rollback_failed") from None
                receipt = {
                    "operation": "rollback",
                    "feed_build_id": stage["feed_build_id"],
                    "previous_target": candidate_target,
                    "new_target": previous,
                    "manifest_sha256": stage["runtime_manifest_sha256"],
                    "public_readback_sha256": hashlib.sha256(error.observed).hexdigest(),
                    "completed_at": _completed_at(),
                }
                _write_receipt(output, receipt)
                raise FeedDeployError("readback_failed_rolled_back") from None
            receipt = {
                "operation": "activate",
                "feed_build_id": stage["feed_build_id"],
                "previous_target": previous,
                "new_target": candidate_target,
                "manifest_sha256": stage["runtime_manifest_sha256"],
                "public_readback_sha256": hashlib.sha256(observed).hexdigest(),
                "completed_at": _completed_at(),
            }
            _write_receipt(output, receipt)
            return receipt
    except LockUnavailable:
        raise FeedDeployError("activation_lock_unavailable") from None


def main(argv: list[str] | None = None) -> int:
    try:
        result = activate(_parser().parse_args(argv))
    except (FeedDeployError, OSError, ValueError) as error:
        print(f"emate_feed_activation_failed:{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
