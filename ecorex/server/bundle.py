"""Cryptographically verified, memory-backed React bundle."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import stat as stat_module
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseManifest,
    SignatureVerifier,
    verify_artifact_signature,
    verify_manifest_signature,
)
from ecorex.update.manifest import MAX_MANIFEST_BYTES

from .errors import BundleIntegrityError
from .manifest import (
    MAX_WEB_FILES,
    MAX_WEB_MANIFEST_BYTES,
    WebBundleManifest,
    WebFileRecord,
)


RUNTIME_CONFIG_MARKER = "<!--__ECOREX_RUNTIME_CONFIG__-->"
_RUNTIME_CONFIG_COMMENT = "__ECOREX_RUNTIME_CONFIG__"
_MEDIA_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json; charset=utf-8",
}


@dataclass(frozen=True, slots=True)
class VerifiedWebFile:
    record: WebFileRecord
    content: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class VerifiedWebBundle:
    release_manifest: ReleaseManifest
    web_manifest: WebBundleManifest
    files: Mapping[str, VerifiedWebFile]
    index_template: str

    def file(self, path: str) -> VerifiedWebFile | None:
        return self.files.get(path)


class _IndexContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.head_depth = 0
        self.marker_count = 0
        self.marker_outside_head = False
        self.has_inline_script = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        folded = tag.casefold()
        if folded == "head":
            self.head_depth += 1
        if folded == "script":
            sources = [value for name, value in attrs if name.casefold() == "src"]
            if len(sources) != 1 or not sources[0] or not sources[0].strip():
                self.has_inline_script = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "head" and self.head_depth:
            self.head_depth -= 1

    def handle_comment(self, data: str) -> None:
        if data == _RUNTIME_CONFIG_COMMENT:
            self.marker_count += 1
            if not self.head_depth:
                self.marker_outside_head = True


def _regular_file(path: Path, *, label: str) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as error:
        raise BundleIntegrityError(f"{label} cannot be read: {path}") from error
    attributes = getattr(result, "st_file_attributes", 0)
    reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat_module.S_ISREG(result.st_mode)
        or stat_module.S_ISLNK(result.st_mode)
        or bool(attributes & reparse_flag)
    ):
        raise BundleIntegrityError(f"{label} must be a regular non-link file: {path}")
    return result


def _absolute_without_resolving(path: str | Path, *, label: str) -> Path:
    try:
        expanded = Path(path).expanduser()
        return Path(os.path.abspath(os.fspath(expanded)))
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise BundleIntegrityError(f"{label} path is invalid") from error


def _read_stable_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
) -> tuple[os.stat_result, bytes]:
    before = _regular_file(path, label=label)
    if before.st_size > max_bytes:
        raise BundleIntegrityError(f"{label} exceeds its size limit")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise BundleIntegrityError(f"{label} changed while it was being opened")
            content = stream.read(max_bytes + 1)
            after_read = os.fstat(stream.fileno())
    except BundleIntegrityError:
        raise
    except OSError as error:
        raise BundleIntegrityError(f"{label} cannot be read: {path}") from error
    after_path = _regular_file(path, label=label)
    identity_before = (before.st_dev, before.st_ino)
    if (
        len(content) > max_bytes
        or len(content) != before.st_size
        or (opened.st_dev, opened.st_ino) != identity_before
        or (after_read.st_dev, after_read.st_ino) != identity_before
        or (after_path.st_dev, after_path.st_ino) != identity_before
        or after_read.st_size != before.st_size
        or after_path.st_size != before.st_size
        or after_read.st_mtime_ns != before.st_mtime_ns
        or after_path.st_mtime_ns != before.st_mtime_ns
    ):
        raise BundleIntegrityError(f"{label} changed while it was being verified")
    return before, content


def _verified_root(path: Path) -> Path:
    try:
        stat = path.lstat()
    except OSError as error:
        raise BundleIntegrityError(f"web root cannot be read: {path}") from error
    attributes = getattr(stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat_module.S_ISDIR(stat.st_mode)
        or stat_module.S_ISLNK(stat.st_mode)
        or bool(attributes & reparse_flag)
    ):
        raise BundleIntegrityError("web root must be a real directory, not a link")
    return path.resolve(strict=True)


def _media_type(path: str) -> str:
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix in _MEDIA_TYPES:
        return _MEDIA_TYPES[suffix]
    guessed, _encoding = mimetypes.guess_type(path, strict=True)
    return guessed or "application/octet-stream"


def _validate_index(template: bytes) -> str:
    try:
        decoded = template.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BundleIntegrityError("index.html must be valid UTF-8") from error
    if decoded.count(RUNTIME_CONFIG_MARKER) != 1:
        raise BundleIntegrityError("index.html must contain one runtime config marker")
    parser = _IndexContractParser()
    parser.feed(decoded)
    parser.close()
    if parser.marker_count != 1 or parser.marker_outside_head:
        raise BundleIntegrityError(
            "runtime config marker must be an HTML comment inside <head>"
        )
    if parser.has_inline_script:
        raise BundleIntegrityError("signed index.html cannot contain inline scripts")
    return decoded


def load_verified_web_bundle(
    *,
    web_root: str | Path,
    release_manifest_path: str | Path,
    web_manifest_path: str | Path,
    trusted_public_keys: Mapping[str, bytes],
    web_manifest_artifact_id: str = "web-manifest",
    verifier: SignatureVerifier | None = None,
) -> VerifiedWebBundle:
    """Verify all signed metadata and preload the exact allowlisted web files."""

    verifier = verifier or Ed25519SignatureVerifier(trusted_public_keys)
    release_path = _absolute_without_resolving(
        release_manifest_path, label="release manifest"
    )
    manifest_path = _absolute_without_resolving(web_manifest_path, label="web manifest")
    try:
        _release_stat, release_payload = _read_stable_regular_file(
            release_path,
            label="release manifest",
            max_bytes=MAX_MANIFEST_BYTES,
        )
        release_manifest = ReleaseManifest.from_json(release_payload)
        verify_manifest_signature(release_manifest, verifier)
    except Exception as error:
        if isinstance(error, BundleIntegrityError):
            raise
        raise BundleIntegrityError(
            f"signed release manifest verification failed: {error}"
        ) from error

    try:
        artifact = release_manifest.artifact(web_manifest_artifact_id)
        web_stat, web_manifest_payload = _read_stable_regular_file(
            manifest_path,
            label="web manifest",
            max_bytes=MAX_WEB_MANIFEST_BYTES,
        )
        if web_stat.st_size != artifact.size_bytes:
            raise BundleIntegrityError(
                "web manifest artifact size mismatch: "
                f"expected {artifact.size_bytes}, got {web_stat.st_size}"
            )
        actual_manifest_sha = hashlib.sha256(web_manifest_payload).hexdigest()
        if actual_manifest_sha != artifact.sha256:
            raise BundleIntegrityError(
                "web manifest artifact SHA-256 mismatch: "
                f"expected {artifact.sha256}, got {actual_manifest_sha}"
            )
        verify_artifact_signature(release_manifest, artifact, verifier)
    except Exception as error:
        if isinstance(error, BundleIntegrityError):
            raise
        raise BundleIntegrityError(
            f"signed web manifest artifact verification failed: {error}"
        ) from error

    try:
        web_manifest = WebBundleManifest.from_json(web_manifest_payload)
        verdict = verifier.verify(web_manifest.canonical_payload(), web_manifest.signature)
        if verdict is not True:
            raise BundleIntegrityError("web manifest signature was rejected")
    except Exception as error:
        if isinstance(error, BundleIntegrityError):
            raise
        raise BundleIntegrityError(
            f"web manifest signature verification failed: {error}"
        ) from error
    if (
        web_manifest.release_id != release_manifest.release_id
        or web_manifest.version != release_manifest.version
        or web_manifest.build_digest != release_manifest.build_digest
    ):
        raise BundleIntegrityError("release and web manifest identities do not match")

    root = _verified_root(_absolute_without_resolving(web_root, label="web root"))
    manifest_paths = {record.path for record in web_manifest.files}
    actual_paths: set[str] = set()
    entry_count = 0
    try:
        for candidate in root.rglob("*"):
            entry_count += 1
            if entry_count > MAX_WEB_FILES * 8:
                raise BundleIntegrityError("web root contains too many entries")
            relative = candidate.relative_to(root).as_posix()
            candidate_stat = candidate.lstat()
            attributes = getattr(candidate_stat, "st_file_attributes", 0)
            reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat_module.S_ISLNK(candidate_stat.st_mode) or bool(
                attributes & reparse_flag
            ):
                raise BundleIntegrityError(
                    f"web root contains a link or reparse point: {relative}"
                )
            if stat_module.S_ISREG(candidate_stat.st_mode):
                actual_paths.add(relative)
                if len(actual_paths) > MAX_WEB_FILES:
                    raise BundleIntegrityError("web root contains too many files")
            elif not stat_module.S_ISDIR(candidate_stat.st_mode):
                raise BundleIntegrityError(
                    f"web root contains a non-file entry: {relative}"
                )
    except BundleIntegrityError:
        raise
    except OSError as error:
        raise BundleIntegrityError("web root changed while it was enumerated") from error
    unlisted = actual_paths - manifest_paths
    missing = manifest_paths - actual_paths
    if unlisted:
        raise BundleIntegrityError(
            "web root contains unlisted files: " + ", ".join(sorted(unlisted))
        )
    if missing:
        raise BundleIntegrityError(
            "web root is missing manifest files: " + ", ".join(sorted(missing))
        )

    loaded: dict[str, VerifiedWebFile] = {}
    for record in web_manifest.files:
        path = root.joinpath(*PurePosixPath(record.path).parts)
        _regular_file(path, label=f"web file {record.path!r}")
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise BundleIntegrityError(
                f"web file escapes the verified root: {record.path}"
            ) from error
        file_stat, content = _read_stable_regular_file(
            path,
            label=f"web file {record.path!r}",
            max_bytes=record.size_bytes,
        )
        if file_stat.st_size != record.size_bytes:
            raise BundleIntegrityError(
                f"web file size mismatch for {record.path}: "
                f"expected {record.size_bytes}, got {file_stat.st_size}"
            )
        actual = hashlib.sha256(content).hexdigest()
        if actual != record.sha256:
            raise BundleIntegrityError(
                f"web file SHA-256 mismatch for {record.path}: "
                f"expected {record.sha256}, got {actual}"
            )
        loaded[record.path] = VerifiedWebFile(
            record=record,
            content=content,
            media_type=_media_type(record.path),
        )
    entrypoint = loaded[web_manifest.entrypoint]
    index_template = _validate_index(entrypoint.content)
    return VerifiedWebBundle(
        release_manifest=release_manifest,
        web_manifest=web_manifest,
        files=MappingProxyType(loaded),
        index_template=index_template,
    )
