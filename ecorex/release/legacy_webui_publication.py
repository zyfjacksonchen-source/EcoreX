"""Publish verified v0.3.0 WebUI packages, then switch the legacy pointer."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Iterator, Protocol
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from ecorex.release.legacy_webui_manifest import build_legacy_webui_manifest

try:
    import fcntl
except ImportError:  # pragma: no cover - production publisher is Linux-only
    fcntl = None  # type: ignore[assignment]


PUBLIC_ORIGINS = (
    "https://mvdcm.ecoremedia.net/ecorex-agent",
    "https://dl.ecoremedia.net/ecorex-agent",
)
PACKAGE_ORIGINS = (
    "https://gh-proxy.com/https://github.com/zyfjacksonchen-source/EcoreX-installers/releases/download/v0.3.0",
    *(f"{origin}/downloads" for origin in PUBLIC_ORIGINS),
)
PRODUCTION_DOWNLOADS = Path("/srv/ecorex-agent-download/current/downloads")
PRODUCTION_POINTER = Path("/srv/ecorex-agent-download/legacy-pointer/manifest.json")
PRODUCTION_LOCK = Path("/run/lock/ecorex-cloud-deploy.lock")
BASELINE_VERSION = "0.2.9.2"


class LegacyPublicationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PublicationPaths:
    downloads: Path = PRODUCTION_DOWNLOADS
    pointer: Path = PRODUCTION_POINTER
    lock: Path = PRODUCTION_LOCK


class Readback(Protocol):
    def identity(self, url: str, *, maximum_bytes: int) -> tuple[int, str]: ...


class HTTPSReadback:
    def identity(self, url: str, *, maximum_bytes: int) -> tuple[int, str]:
        request = Request(url, headers={"Accept-Encoding": "identity"})
        digest = hashlib.sha256()
        size = 0
        try:
            with urlopen(request, timeout=120) as response:  # noqa: S310 - fixed HTTPS origins
                if response.status != 200:
                    raise LegacyPublicationError("public_readback_failed")
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > maximum_bytes:
                        raise LegacyPublicationError("public_readback_too_large")
                    digest.update(chunk)
        except LegacyPublicationError:
            raise
        except Exception:
            raise LegacyPublicationError("public_readback_failed") from None
        return size, digest.hexdigest()


def publish_legacy_webui(
    receipt_path: Path,
    *,
    paths: PublicationPaths = PublicationPaths(),
    readback: Readback | None = None,
    package_origins: tuple[str, ...] = PACKAGE_ORIGINS,
    manifest_origins: tuple[str, ...] = PUBLIC_ORIGINS,
    enforce_server_fence: bool = True,
    receipt_output: Path | None = None,
) -> dict[str, object]:
    if enforce_server_fence and (
        not sys.platform.startswith("linux")
        or not hasattr(os, "geteuid")
        or os.geteuid() != 0
        or paths != PublicationPaths()
    ):
        raise LegacyPublicationError("production_server_fence_failed")
    if (
        len(package_origins) < 2
        or len(manifest_origins) < 2
        or any(_valid_origin(value) is False for value in package_origins)
        or any(_valid_origin(value) is False for value in manifest_origins)
    ):
        raise LegacyPublicationError("public_origins_invalid")

    manifest = build_legacy_webui_manifest(receipt_path)
    manifest_bytes = _canonical_json(manifest)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    reader = readback or HTTPSReadback()

    with _publication_lock(paths.lock, enforce=enforce_server_fence):
        _validate_pointer(paths.pointer, enforce_owner=enforce_server_fence)
        paths.downloads.mkdir(parents=True, exist_ok=True)
        _validate_directory(paths.downloads, enforce_owner=enforce_server_fence)
        for artifact in artifacts:
            assert isinstance(artifact, dict)
            name = str(artifact["fileName"])
            source = receipt_path.resolve(strict=True).parent / name
            _publish_immutable(source, paths.downloads / name)

        readbacks: list[dict[str, object]] = []
        for origin in package_origins:
            for artifact in artifacts:
                assert isinstance(artifact, dict)
                name = str(artifact["fileName"])
                size = int(artifact["size"])
                digest = str(artifact["sha256"]).casefold()
                url = f"{origin}/{quote(name)}"
                observed = reader.identity(url, maximum_bytes=size)
                if observed != (size, digest):
                    raise LegacyPublicationError("package_readback_mismatch")
                readbacks.append(
                    {
                        "origin": origin,
                        "file_name": name,
                        "size": size,
                        "sha256": digest,
                    }
                )

        previous = paths.pointer.read_bytes()
        _atomic_replace(paths.pointer, manifest_bytes)
        try:
            expected = (len(manifest_bytes), hashlib.sha256(manifest_bytes).hexdigest())
            for origin in manifest_origins:
                if (
                    reader.identity(
                        f"{origin}/manifest.json", maximum_bytes=len(manifest_bytes)
                    )
                    != expected
                ):
                    raise LegacyPublicationError("manifest_readback_mismatch")
            result: dict[str, object] = {
                "schema": "emate.legacy-webui-publication.v1",
                "status": "published",
                "version": manifest["version"],
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "package_readbacks": readbacks,
            }
            if receipt_output is not None:
                _atomic_replace(receipt_output, _canonical_json(result))
        except Exception:
            _atomic_replace(paths.pointer, previous)
            raise
        return result


def _valid_origin(value: str) -> bool:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    return (parsed.hostname, parsed.path) in {
        ("mvdcm.ecoremedia.net", "/ecorex-agent"),
        ("dl.ecoremedia.net", "/ecorex-agent"),
        ("mvdcm.ecoremedia.net", "/ecorex-agent/downloads"),
        ("dl.ecoremedia.net", "/ecorex-agent/downloads"),
        (
            "gh-proxy.com",
            "/https://github.com/zyfjacksonchen-source/EcoreX-installers/releases/download/v0.3.0",
        ),
    }


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )


def _validate_pointer(path: Path, *, enforce_owner: bool) -> None:
    try:
        metadata = path.lstat()
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise LegacyPublicationError("legacy_pointer_invalid") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise LegacyPublicationError("legacy_pointer_invalid")
    if enforce_owner and (metadata.st_uid not in {0, 994} or metadata.st_mode & 0o022):
        raise LegacyPublicationError("legacy_pointer_permissions_invalid")
    if not isinstance(value, dict) or value.get("version") not in {
        BASELINE_VERSION,
        "0.3.0",
    }:
        raise LegacyPublicationError("legacy_pointer_version_invalid")


def _validate_directory(path: Path, *, enforce_owner: bool) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise LegacyPublicationError("downloads_directory_invalid") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LegacyPublicationError("downloads_directory_invalid")
    if enforce_owner and (metadata.st_uid not in {0, 994} or metadata.st_mode & 0o022):
        raise LegacyPublicationError("downloads_directory_permissions_invalid")


def _publish_immutable(source: Path, target: Path) -> None:
    expected = _file_identity(source)
    if target.exists():
        if not _regular_file(target) or _file_identity(target) != expected:
            raise LegacyPublicationError("immutable_package_conflict")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if not _regular_file(target) or _file_identity(target) != expected:
                raise LegacyPublicationError("immutable_package_conflict") from None
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        raise LegacyPublicationError("package_read_failed") from None
    return size, digest.hexdigest()


def _regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_nlink == 1
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _publication_lock(path: Path, *, enforce: bool) -> Iterator[None]:
    if not enforce:
        yield
        return
    assert fcntl is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise LegacyPublicationError("product_deploy_lock_unavailable") from None
    try:
        yield
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--receipt-output", required=True, type=Path)
    args = parser.parse_args()
    result = publish_legacy_webui(args.receipt, receipt_output=args.receipt_output)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
