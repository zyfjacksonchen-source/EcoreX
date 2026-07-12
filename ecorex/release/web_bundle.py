"""Strict production React dist scanner and signed Web manifest generator."""

from __future__ import annotations

import base64
import hashlib
import os
import posixpath
import re
import stat
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from ecorex.server.bundle import RUNTIME_CONFIG_MARKER
from ecorex.server.errors import BundleIntegrityError
from ecorex.server.manifest import (
    MAX_WEB_BUNDLE_BYTES,
    MAX_WEB_FILES,
    MAX_WEB_FILE_BYTES,
    MAX_WEB_MANIFEST_BYTES,
    WebBundleManifest,
    WebFileRecord,
)
from ecorex.update import SignatureEnvelope
from ecorex.update.manifest import validate_portable_path_segment

from .errors import ReleaseBuildError
from .models import WebBundleBuildInput
from .signing import ReleaseSigner, sign_envelope


WEB_MANIFEST_ARTIFACT_ID = "web-manifest"
WEB_MANIFEST_FILE_NAME = "web-manifest.json"
WEB_MANIFEST_PLATFORM = "all"
WEB_MANIFEST_ARCHITECTURE = "all"
_MAX_WEB_ENTRIES = MAX_WEB_FILES * 8
_PLACEHOLDER_SIGNATURE = base64.b64encode(b"\0" * 64).decode("ascii")
_ALLOWED_ASSET_SUFFIXES = frozenset(
    {
        ".avif",
        ".css",
        ".eot",
        ".gif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".json",
        ".mp3",
        ".mp4",
        ".ogg",
        ".otf",
        ".png",
        ".svg",
        ".ttf",
        ".wasm",
        ".wav",
        ".webm",
        ".webmanifest",
        ".webp",
        ".woff",
        ".woff2",
    }
)
_LEGACY_REFERENCE_MARKERS = (
    "chat.html",
    "channel/web/",
    "dist-electron",
    "ecorex-v029-overlay",
    "ecorex-v030-overlay",
    "webui-overlay",
    "/static/app/",
)
_LEGACY_CONTENT_MARKERS = (
    "channel/web/chat.html",
    "ecorex-v029-",
    "ecorex-v030-",
    "webui-overlay",
)
_RESOURCE_ATTRIBUTES = {
    "audio": ("src",),
    "image": ("href", "xlink:href"),
    "img": ("src",),
    "input": ("src",),
    "link": ("href",),
    "source": ("src",),
    "track": ("src",),
    "use": ("href", "xlink:href"),
    "video": ("poster", "src"),
}
_TEXT_ASSET_SUFFIXES = frozenset({".css", ".js", ".json", ".svg", ".webmanifest"})
_REFERENCE_TOKEN = re.compile(
    r'''(?P<quote>["'`])(?P<quoted>[^"'`\r\n]{1,1024})(?P=quote)'''
    r'''|url\(\s*(?P<url>[^)\r\n]{1,1024})\s*\)''',
    re.IGNORECASE,
)
_LOCAL_ASSET_LITERAL = re.compile(
    r'''(?P<local_quote>["'`])'''
    r'''(?P<local>(?:\.{1,2}/|/?assets/)[^"'`\r\n]{1,2048})'''
    r'''(?P=local_quote)''',
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ScannedWebBundle:
    dist_dir: Path
    entrypoint: str
    files: tuple[WebFileRecord, ...]
    bundle_sha256: str

    def digest_material(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "artifact_id": WEB_MANIFEST_ARTIFACT_ID,
            "file_name": WEB_MANIFEST_FILE_NAME,
            "platform": WEB_MANIFEST_PLATFORM,
            "architecture": WEB_MANIFEST_ARCHITECTURE,
            "entrypoint": self.entrypoint,
            "bundle_sha256": self.bundle_sha256,
            "files": [record.to_dict() for record in self.files],
        }


class _ProductionIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.head_depth = 0
        self.marker_count = 0
        self.marker_outside_head = False
        self.script_depth = 0
        self.script_references = 0
        self.references: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        folded_tag = tag.casefold()
        folded_names = [name.casefold() for name, _value in attrs]
        if len(folded_names) != len(set(folded_names)):
            raise ReleaseBuildError("index.html contains duplicate HTML attributes")
        attributes = {
            name.casefold(): value for name, value in attrs
        }
        if folded_tag == "head":
            self.head_depth += 1
        if folded_tag == "base":
            raise ReleaseBuildError("index.html cannot contain a CSP-incompatible base tag")
        if folded_tag in {"embed", "frame", "iframe", "object"}:
            raise ReleaseBuildError(
                f"index.html contains CSP-incompatible <{folded_tag}> content"
            )
        if folded_tag == "style" or "style" in attributes:
            raise ReleaseBuildError("index.html cannot contain inline style")
        if any(name.startswith("on") for name in attributes):
            raise ReleaseBuildError("index.html cannot contain inline event scripts")
        if "srcset" in attributes or "imagesrcset" in attributes:
            raise ReleaseBuildError(
                "index.html cannot contain non-allowlisted srcset content"
            )
        if folded_tag == "meta" and (
            attributes.get("http-equiv") or ""
        ).casefold() in {"content-security-policy", "refresh"}:
            raise ReleaseBuildError(
                "index.html cannot override CSP or perform a meta refresh"
            )
        if folded_tag == "script":
            source = attributes.get("src")
            if not source or not source.strip():
                raise ReleaseBuildError("index.html cannot contain inline script")
            self.references.append(source.strip())
            self.script_references += 1
            self.script_depth += 1
        for attribute in _RESOURCE_ATTRIBUTES.get(folded_tag, ()):
            reference = attributes.get(attribute)
            if reference:
                self.references.append(reference.strip())

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if folded == "head" and self.head_depth:
            self.head_depth -= 1
        if folded == "script" and self.script_depth:
            self.script_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.script_depth and data.strip():
            raise ReleaseBuildError("index.html cannot contain inline script content")

    def handle_comment(self, data: str) -> None:
        if data == "__ECOREX_RUNTIME_CONFIG__":
            self.marker_count += 1
            if not self.head_depth:
                self.marker_outside_head = True


def scan_web_bundle(definition: WebBundleBuildInput) -> ScannedWebBundle:
    root = _require_real_directory(definition.dist_dir, label="React dist")
    root_children = _scan_directory(root, label="React dist")
    names = {child.name for child in root_children}
    hidden = sorted(name for name in names if name.startswith("."))
    if hidden:
        raise ReleaseBuildError(f"React dist contains hidden entries: {hidden!r}")
    expected = {"assets", "index.html"}
    if names != expected:
        missing = sorted(expected - names)
        extra = sorted(names - expected)
        raise ReleaseBuildError(
            "React dist violates the exact index/assets allowlist: "
            f"missing={missing!r}, extra={extra!r}"
        )

    index_path = root / "index.html"
    index_size, index_sha256, index_content = _hash_stable_file(
        index_path,
        label="React index.html",
        max_bytes=MAX_WEB_FILE_BYTES,
        capture=True,
    )
    assets_root = _require_real_directory(root / "assets", label="React assets")
    asset_records = _scan_assets(root, assets_root)
    if not asset_records:
        raise ReleaseBuildError("React dist must contain at least one hashed asset")
    total_size = index_size + sum(record.size_bytes for record in asset_records)
    if total_size > MAX_WEB_BUNDLE_BYTES:
        raise ReleaseBuildError("React Web bundle exceeds the 150 MiB hard limit")
    index_record = WebFileRecord(
        path="index.html",
        size_bytes=index_size,
        sha256=index_sha256,
        immutable=False,
    )
    files = tuple(sorted((index_record, *asset_records), key=lambda record: record.path))
    asset_paths = {record.path for record in asset_records}
    direct_references = _validate_index(index_content, asset_paths)
    _validate_asset_reachability(root, asset_records, direct_references)
    return ScannedWebBundle(
        dist_dir=root,
        entrypoint="index.html",
        files=files,
        bundle_sha256=WebBundleManifest.compute_bundle_sha256(files),
    )


def create_signed_web_manifest(
    scanned: ScannedWebBundle,
    *,
    release_id: str,
    version: str,
    build_digest: str,
    signer: ReleaseSigner,
) -> tuple[WebBundleManifest, bytes]:
    placeholder = SignatureEnvelope(
        "ed25519", "release-placeholder", _PLACEHOLDER_SIGNATURE
    )
    try:
        unsigned = WebBundleManifest(
            schema_version=1,
            release_id=release_id,
            version=version,
            build_digest=build_digest,
            bundle_sha256=scanned.bundle_sha256,
            entrypoint=scanned.entrypoint,
            files=scanned.files,
            signature=placeholder,
        )
    except BundleIntegrityError as exc:
        raise ReleaseBuildError(f"cannot construct Web bundle manifest: {exc}") from exc
    signed = replace(
        unsigned,
        signature=sign_envelope(signer, unsigned.canonical_payload()),
    )
    payload = signed.to_json().encode("utf-8")
    if len(payload) > MAX_WEB_MANIFEST_BYTES:
        raise ReleaseBuildError("signed Web bundle manifest exceeds 1 MiB")
    return signed, payload


def _scan_assets(root: Path, assets_root: Path) -> tuple[WebFileRecord, ...]:
    pending = [assets_root]
    records: list[WebFileRecord] = []
    entry_count = 0
    while pending:
        directory = pending.pop()
        children = _scan_directory(directory, label="React assets directory")
        if not children:
            relative = directory.relative_to(root).as_posix()
            raise ReleaseBuildError(
                f"React dist contains an extra empty directory: {relative!r}"
            )
        for child in children:
            entry_count += 1
            if entry_count > _MAX_WEB_ENTRIES:
                raise ReleaseBuildError("React dist contains too many entries")
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            if any(part.startswith(".") for part in PurePosixPath(relative).parts):
                raise ReleaseBuildError(
                    f"React dist contains a hidden asset entry: {relative!r}"
                )
            _validate_web_path(relative)
            metadata = _lstat(path, label=f"React asset {relative!r}")
            if _metadata_is_link_or_reparse(metadata):
                raise ReleaseBuildError(
                    f"React dist contains a link or reparse point: {relative!r}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ReleaseBuildError(
                    f"React dist contains a non-file asset: {relative!r}"
                )
            suffix = PurePosixPath(relative).suffix.casefold()
            if suffix not in _ALLOWED_ASSET_SUFFIXES:
                raise ReleaseBuildError(
                    f"React asset is outside the production allowlist: {relative!r}"
                )
            size, digest, _content = _hash_stable_file(
                path,
                label=f"React asset {relative!r}",
                max_bytes=MAX_WEB_FILE_BYTES,
                capture=False,
            )
            try:
                record = WebFileRecord(
                    path=relative,
                    size_bytes=size,
                    sha256=digest,
                    immutable=True,
                )
            except BundleIntegrityError as exc:
                raise ReleaseBuildError(
                    f"React asset is not SHA-256 hashed and immutable: {relative!r}: {exc}"
                ) from exc
            records.append(record)
            if len(records) + 1 > MAX_WEB_FILES:
                raise ReleaseBuildError("React dist contains too many Web files")
    return tuple(records)


def _validate_index(content: bytes, asset_paths: set[str]) -> set[str]:
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseBuildError("index.html must be valid UTF-8") from exc
    if decoded.count(RUNTIME_CONFIG_MARKER) != 1:
        raise ReleaseBuildError(
            "index.html must contain exactly one EcoreX runtime config marker"
        )
    folded_index = decoded.casefold()
    if any(marker in folded_index for marker in _LEGACY_REFERENCE_MARKERS):
        raise ReleaseBuildError("index.html contains a legacy bundle or overlay reference")
    parser = _ProductionIndexParser()
    try:
        parser.feed(decoded)
        parser.close()
    except ReleaseBuildError:
        raise
    except Exception as exc:
        raise ReleaseBuildError("index.html is not valid production HTML") from exc
    if parser.marker_count != 1 or parser.marker_outside_head:
        raise ReleaseBuildError(
            "EcoreX runtime config marker must be an HTML comment inside <head>"
        )
    if parser.head_depth or parser.script_depth:
        raise ReleaseBuildError("index.html contains unclosed head or script elements")
    if parser.script_references < 1:
        raise ReleaseBuildError("index.html must reference one hashed React script")
    normalized_references: set[str] = set()
    for reference in parser.references:
        folded = reference.casefold()
        if any(marker in folded for marker in _LEGACY_REFERENCE_MARKERS):
            raise ReleaseBuildError(
                f"index.html references a legacy bundle or overlay: {reference!r}"
            )
        normalized = _normalize_asset_reference(reference)
        if normalized not in asset_paths:
            raise ReleaseBuildError(
                f"index.html reference is outside the exact Web allowlist: {reference!r}"
            )
        normalized_references.add(normalized)
    return normalized_references


def _validate_asset_reachability(
    root: Path,
    records: tuple[WebFileRecord, ...],
    direct_references: set[str],
) -> None:
    by_path = {record.path: record for record in records}
    reachable = set(direct_references)
    pending = sorted(direct_references, reverse=True)
    while pending:
        source_path = pending.pop()
        source_record = by_path[source_path]
        if PurePosixPath(source_path).suffix.casefold() not in _TEXT_ASSET_SUFFIXES:
            continue
        size, digest, content = _hash_stable_file(
            root.joinpath(*PurePosixPath(source_path).parts),
            label=f"React text asset {source_path!r}",
            max_bytes=source_record.size_bytes,
            capture=True,
        )
        if size != source_record.size_bytes or digest != source_record.sha256:
            raise ReleaseBuildError(
                f"React asset changed while resolving its allowlist: {source_path!r}"
            )
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseBuildError(
                f"React text asset must be valid UTF-8: {source_path!r}"
            ) from exc
        folded_content = decoded.casefold()
        if any(marker in folded_content for marker in _LEGACY_CONTENT_MARKERS):
            raise ReleaseBuildError(
                f"React asset contains legacy bundle or overlay code: {source_path!r}"
            )
        dependencies, missing = _extract_asset_dependencies(
            source_path,
            decoded,
            set(by_path),
        )
        if missing:
            raise ReleaseBuildError(
                f"React asset {source_path!r} references missing production "
                f"dependencies: {sorted(missing)!r}"
            )
        for dependency in sorted(dependencies - reachable, reverse=True):
            reachable.add(dependency)
            pending.append(dependency)
    orphaned = sorted(set(by_path) - reachable)
    if orphaned:
        raise ReleaseBuildError(
            "React dist contains extra or orphaned hashed assets outside the exact "
            f"dependency allowlist: {orphaned!r}"
        )


def _extract_asset_dependencies(
    source_path: str,
    content: str,
    known_paths: set[str],
) -> tuple[set[str], set[str]]:
    dependencies: set[str] = set()
    missing: set[str] = set()
    source_parent = PurePosixPath(source_path).parent.as_posix()
    # Minified JavaScript often closes an ordinary string on the same quote
    # that opens a later dynamic import.  A generic string regex can consume
    # that boundary and skip the actual asset literal.  Prefer exact local
    # asset matches and discard every generic token that overlaps one, just as
    # the production rehash step does.
    specific = list(_LOCAL_ASSET_LITERAL.finditer(content))
    generic = [
        match
        for match in _REFERENCE_TOKEN.finditer(content)
        if not any(
            match.start() < item.end() and item.start() < match.end()
            for item in specific
        )
    ]
    for match in sorted((*specific, *generic), key=lambda item: item.start()):
        local = match.groupdict().get("local")
        raw = (
            local
            or match.groupdict().get("quoted")
            or match.groupdict().get("url")
            or ""
        ).strip()
        is_url_function = match.groupdict().get("url") is not None
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1].strip()
        if not raw or raw.casefold().startswith("data:") or "\\" in raw:
            continue
        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        if parsed.path.startswith("/assets/"):
            candidate = parsed.path[1:]
        elif parsed.path.startswith("assets/"):
            candidate = parsed.path
        else:
            candidate = posixpath.normpath(
                posixpath.join(source_parent, parsed.path)
            )
        if candidate in known_paths:
            dependencies.add(candidate)
        elif (
            PurePosixPath(candidate).suffix.casefold() in _ALLOWED_ASSET_SUFFIXES
            and (
                is_url_function
                or raw.startswith((".", "/", "assets/"))
                or "/" in raw
            )
        ):
            missing.add(candidate)
    return dependencies, missing


def _normalize_asset_reference(reference: str) -> str:
    if not reference or "\\" in reference or any(ord(value) < 32 for value in reference):
        raise ReleaseBuildError("index.html contains an unsafe asset reference")
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ReleaseBuildError(
            f"index.html asset reference is not a fixed same-origin path: {reference!r}"
        )
    value = parsed.path
    if value.startswith("/"):
        value = value[1:]
    elif value.startswith("./"):
        value = value[2:]
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != "assets"
    ):
        raise ReleaseBuildError(
            f"index.html asset reference is outside assets/: {reference!r}"
        )
    _validate_web_path(value)
    return value


def _validate_web_path(value: str) -> None:
    try:
        for part in PurePosixPath(value).parts:
            validate_portable_path_segment(part, label="React asset path")
    except ValueError as exc:
        raise ReleaseBuildError(f"React asset path is not portable: {value!r}") from exc


def _hash_stable_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    capture: bool,
) -> tuple[int, str, bytes]:
    before = _require_regular_file(path, label=label)
    if before.st_size < 1 or before.st_size > max_bytes:
        raise ReleaseBuildError(f"{label} is empty or exceeds its size limit")
    digest = hashlib.sha256()
    captured = bytearray()
    read_size = 0
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            _require_same_file(before, opened, label=label)
            while chunk := stream.read(64 * 1024):
                read_size += len(chunk)
                if read_size > max_bytes:
                    raise ReleaseBuildError(f"{label} exceeds its size limit")
                digest.update(chunk)
                if capture:
                    captured.extend(chunk)
            after_read = os.fstat(stream.fileno())
    except ReleaseBuildError:
        raise
    except OSError as exc:
        raise ReleaseBuildError(f"{label} cannot be read") from exc
    after_path = _require_regular_file(path, label=label)
    _require_same_file(before, after_read, label=label)
    _require_same_file(before, after_path, label=label)
    if read_size != before.st_size:
        raise ReleaseBuildError(f"{label} changed while it was hashed")
    return read_size, digest.hexdigest(), bytes(captured)


def _scan_directory(path: Path, *, label: str) -> list[os.DirEntry[str]]:
    _require_real_directory(path, label=label)
    try:
        return sorted(os.scandir(path), key=lambda child: child.name)
    except OSError as exc:
        raise ReleaseBuildError(f"{label} cannot be enumerated") from exc


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise ReleaseBuildError(f"{label} cannot be inspected") from exc


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _require_real_directory(path: Path, *, label: str) -> Path:
    metadata = _lstat(path, label=label)
    if _metadata_is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseBuildError(f"{label} must be a real non-link directory")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise ReleaseBuildError(f"{label} cannot be resolved") from exc


def _require_regular_file(path: Path, *, label: str) -> os.stat_result:
    metadata = _lstat(path, label=label)
    if _metadata_is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ReleaseBuildError(f"{label} must be a regular non-link file")
    return metadata


def _require_same_file(
    expected: os.stat_result,
    actual: os.stat_result,
    *,
    label: str,
) -> None:
    if (
        (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
        or actual.st_size != expected.st_size
        or actual.st_mtime_ns != expected.st_mtime_ns
        or _metadata_is_link_or_reparse(actual)
    ):
        raise ReleaseBuildError(f"{label} changed while it was verified")
