"""Content-addressed, non-executable local Skill bundles.

Local bundles are an integrity provenance class, not a publisher trust class.
Both ZIP uploads and administrator-selected directories are normalized to the
same ordered file inventory before any Extension revision can be staged.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Iterable, Mapping
import unicodedata
import uuid
import zipfile

from .errors import ExtensionIntegrityError, ExtensionManifestError
from .models import parse_semver, validate_version_range


LOCAL_BUNDLE_SCHEMA_VERSION = 1
MAX_LOCAL_BUNDLE_BYTES = 10 * 1024 * 1024
MAX_LOCAL_BUNDLE_FILES = 256
MAX_LOCAL_FILE_BYTES = 2 * 1024 * 1024
MAX_LOCAL_PATH_BYTES = 512
MAX_LOCAL_PATH_DEPTH = 8
MAX_LOCAL_PATH_TOTAL_BYTES = 64 * 1024
MAX_ZIP_EXPANSION_RATIO = 100
MAX_FRONTMATTER_BYTES = 16 * 1024
MAX_SKILL_DOCUMENT_BYTES = 128 * 1024
MAX_SKILL_ESTIMATED_TOKENS = 32_000

_FRONTMATTER_KEYS = frozenset(
    {"name", "description", "version", "license", "compatibility", "tags"}
)
_REQUIRED_FRONTMATTER_KEYS = frozenset({"name", "description"})
_FRONTMATTER_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BANNED_SEGMENTS = frozenset(
    {
        "bin",
        "command",
        "commands",
        "env",
        "hooks",
        "native",
        "network",
        "scripts",
        "secrets",
    }
)
_BANNED_SUFFIXES = frozenset(
    {
        ".bat", ".c", ".class", ".cmd", ".com", ".cpp", ".cs", ".dll",
        ".dylib", ".exe", ".go", ".hta", ".jar", ".jsx", ".lua",
        ".msi", ".php", ".pl", ".ps1", ".pyc", ".rb",
        ".rs", ".so", ".ts", ".tsx", ".vbs", ".wasm",
    }
)
_STATIC_SUFFIXES = frozenset(
    {
        ".avif", ".bmp", ".csv", ".gif", ".jpeg", ".jpg", ".json", ".md",
        ".js", ".mjs", ".png", ".py", ".sh", ".txt", ".webp", ".xml",
        ".yaml", ".yml", ".tsv",
    }
)
_SCRIPT_SUFFIXES = frozenset({".js", ".mjs", ".py", ".sh"})
_IGNORED_CACHE_DIRECTORIES = frozenset({"__pycache__", ".pytest_cache"})
_IGNORED_CACHE_SUFFIXES = frozenset({".pyc", ".pyo"})
SKILL_RUNTIME_FILE = "skill-runtime.json"
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_DOMAIN_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_WINDOWS_RESERVED = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class LocalSkillMetadata:
    name: str
    description: str
    version: str = "0.0.0"
    license: str | None = None
    compatibility: str = "*"
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "license": self.license,
            "compatibility": self.compatibility,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class LocalBundleFile:
    path: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class LocalSkillBundle:
    artifact_sha256: str
    metadata: LocalSkillMetadata
    files: tuple[LocalBundleFile, ...]
    total_size_bytes: int

    @property
    def revision_evidence(self) -> str:
        return self.artifact_sha256

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LOCAL_BUNDLE_SCHEMA_VERSION,
            "kind": "declarative_skill",
            "metadata": self.metadata.to_dict(),
            "files": [item.to_dict() for item in self.files],
            "total_size_bytes": self.total_size_bytes,
        }


@dataclass(frozen=True, slots=True)
class SkillRuntimeManifest:
    runtime: str
    entrypoint: str
    environment: tuple[str, ...]
    network_domains: tuple[str, ...]
    external_commands: tuple[str, ...]
    effects: tuple[str, ...]


class LocalSkillBundleStore:
    """Product-owned CAS for normalized static Skill content."""

    def __init__(self, root: str | Path, *, create: bool = True) -> None:
        self.root = Path(root).expanduser().resolve()
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        elif not self.root.exists():
            return
        metadata = self.root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse(metadata)
        ):
            raise ExtensionIntegrityError("local Skill CAS root must be a real directory")

    def converge_startup(self) -> None:
        """Create and validate the CAS only after Runtime Phase-A admission."""

        self.root.mkdir(parents=True, exist_ok=True)
        metadata = self.root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse(metadata)
        ):
            raise ExtensionIntegrityError(
                "local Skill CAS root must be a real directory"
            )

    def ingest_zip(self, payload: bytes) -> LocalSkillBundle:
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_LOCAL_BUNDLE_BYTES:
            raise ExtensionManifestError("local Skill ZIP exceeds the 10 MiB input boundary")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_LOCAL_BUNDLE_FILES * 2:
                    raise ExtensionManifestError("local Skill ZIP has too many entries")
                files: dict[str, bytes] = {}
                canonical_names: set[str] = set()
                total = 0
                compressed_total = 0
                for entry in entries:
                    path = _validate_path(
                        entry.filename,
                        allow_directory=True,
                        allow_script_paths=True,
                    )
                    folded = path.casefold()
                    if folded in canonical_names:
                        raise ExtensionManifestError(
                            "local Skill ZIP contains duplicate or case-colliding paths"
                        )
                    canonical_names.add(folded)
                    mode = (entry.external_attr >> 16) & 0xFFFF
                    file_type = stat.S_IFMT(mode) if mode else 0
                    if entry.is_dir():
                        if file_type not in {0, stat.S_IFDIR}:
                            raise ExtensionManifestError("local Skill ZIP directory type is invalid")
                        continue
                    if file_type not in {0, stat.S_IFREG}:
                        raise ExtensionManifestError(
                            "local Skill ZIP may not contain links, devices, or special files"
                        )
                    _validate_executable_resource_mode(path, mode)
                    if entry.flag_bits & 0x1:
                        raise ExtensionManifestError("encrypted local Skill ZIP entries are forbidden")
                    if entry.file_size < 0 or entry.file_size > MAX_LOCAL_FILE_BYTES:
                        raise ExtensionManifestError("a local Skill file exceeds the 2 MiB limit")
                    total += entry.file_size
                    compressed_total += max(0, entry.compress_size)
                    if total > MAX_LOCAL_BUNDLE_BYTES:
                        raise ExtensionManifestError("local Skill content exceeds 10 MiB")
                    if (
                        entry.file_size > 0
                        and entry.compress_size == 0
                        and entry.compress_type != zipfile.ZIP_STORED
                    ):
                        raise ExtensionManifestError("local Skill ZIP has an invalid compression ratio")
                    if (
                        entry.compress_size > 0
                        and entry.file_size > entry.compress_size * MAX_ZIP_EXPANSION_RATIO
                    ):
                        raise ExtensionManifestError("local Skill ZIP expansion ratio is unsafe")
                    _validate_static_resource(path, allow_scripts=True)
                    with archive.open(entry, "r") as stream:
                        content = stream.read(MAX_LOCAL_FILE_BYTES + 1)
                        trailing = stream.read(1)
                    if trailing or len(content) > MAX_LOCAL_FILE_BYTES:
                        raise ExtensionManifestError("a local Skill file exceeds the 2 MiB limit")
                    if len(content) != entry.file_size:
                        raise ExtensionIntegrityError("local Skill ZIP entry size changed while reading")
                    files[path] = content
                if compressed_total > 0 and total > compressed_total * MAX_ZIP_EXPANSION_RATIO:
                    raise ExtensionManifestError("local Skill ZIP aggregate expansion ratio is unsafe")
        except (zipfile.BadZipFile, RuntimeError, OSError, EOFError) as error:
            if isinstance(error, (ExtensionManifestError, ExtensionIntegrityError)):
                raise
            raise ExtensionManifestError("local Skill bundle must be a valid bounded ZIP") from error
        return self._ingest_files(files)

    def ingest_directory(
        self, directory: str | Path, *, migrated_frontmatter: bool = False
    ) -> LocalSkillBundle:
        source = Path(directory).expanduser()
        try:
            root_metadata = source.lstat()
            root = source.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise ExtensionManifestError("local Skill directory is unavailable") from error
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or _is_reparse(root_metadata)
        ):
            raise ExtensionManifestError("local Skill source must be a real directory")
        files: dict[str, bytes] = {}
        canonical_names: set[str] = set()
        identities: set[tuple[int, int]] = set()
        path_total = 0
        total = 0
        for current, directories, names in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in tuple(directories):
                child = current_path / name
                metadata = child.lstat()
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or _is_reparse(metadata)
                ):
                    raise ExtensionManifestError(
                        "local Skill directory may not contain links or special directories"
                    )
                if name.casefold() in _IGNORED_CACHE_DIRECTORIES:
                    directories.remove(name)
            for name in names:
                if name == ".ecorex-custom-override":
                    continue
                child = current_path / name
                metadata = child.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or _is_reparse(metadata)
                    or metadata.st_nlink != 1
                ):
                    raise ExtensionManifestError(
                        "local Skill directory may contain only non-linked static files"
                    )
                if child.suffix.casefold() in _IGNORED_CACHE_SUFFIXES:
                    continue
                identity = (int(metadata.st_dev), int(metadata.st_ino))
                if identity in identities:
                    raise ExtensionManifestError("local Skill hard-linked files are forbidden")
                identities.add(identity)
                relative = child.relative_to(root).as_posix()
                path = _validate_path(relative, allow_script_paths=True)
                _validate_executable_resource_mode(path, metadata.st_mode)
                folded = path.casefold()
                if folded in canonical_names:
                    raise ExtensionManifestError(
                        "local Skill directory contains duplicate or case-colliding paths"
                    )
                canonical_names.add(folded)
                path_total += len(path.encode("utf-8"))
                if path_total > MAX_LOCAL_PATH_TOTAL_BYTES:
                    raise ExtensionManifestError("local Skill path inventory is too large")
                _validate_static_resource(path, allow_scripts=True)
                if metadata.st_size > MAX_LOCAL_FILE_BYTES:
                    raise ExtensionManifestError("a local Skill file exceeds the 2 MiB limit")
                content = child.read_bytes()
                after = child.lstat()
                if (
                    after.st_dev != metadata.st_dev
                    or after.st_ino != metadata.st_ino
                    or after.st_size != metadata.st_size
                    or after.st_mtime_ns != metadata.st_mtime_ns
                    or len(content) != metadata.st_size
                ):
                    raise ExtensionIntegrityError(
                        "local Skill directory changed during normalization"
                    )
                files[path] = content
                total += len(content)
                if len(files) > MAX_LOCAL_BUNDLE_FILES or total > MAX_LOCAL_BUNDLE_BYTES:
                    raise ExtensionManifestError("local Skill directory exceeds product limits")
        return self._ingest_files(files, migrated_frontmatter=migrated_frontmatter)

    def verify(self, artifact_sha256: str) -> LocalSkillBundle:
        if not isinstance(artifact_sha256, str) or not _SHA256.fullmatch(artifact_sha256):
            raise ExtensionIntegrityError("local Skill CAS digest is invalid")
        directory = self._directory(artifact_sha256)
        manifest_path = directory / "bundle.json"
        _require_real_directory(directory, label="local Skill CAS revision")
        try:
            manifest_metadata = manifest_path.lstat()
            if (
                not stat.S_ISREG(manifest_metadata.st_mode)
                or stat.S_ISLNK(manifest_metadata.st_mode)
                or _is_reparse(manifest_metadata)
                or manifest_metadata.st_nlink != 1
            ):
                raise ExtensionIntegrityError("local Skill CAS manifest is not a real file")
            raw = manifest_path.read_bytes()
        except OSError as error:
            raise ExtensionIntegrityError("local Skill CAS manifest is missing") from error
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ExtensionManifestError) as error:
            raise ExtensionIntegrityError("local Skill CAS manifest is invalid") from error
        bundle = _bundle_from_manifest(value)
        canonical = _canonical_manifest(bundle.manifest_dict())
        digest = _bundle_digest(canonical)
        if raw != canonical or digest != artifact_sha256:
            raise ExtensionIntegrityError("local Skill CAS manifest digest is invalid")
        file_root = directory / "files"
        _require_real_directory(file_root, label="local Skill CAS file root")
        observed: list[LocalBundleFile] = []
        for record in bundle.files:
            target = file_root.joinpath(*PurePosixPath(record.path).parts)
            for parent in target.parents:
                if parent == file_root.parent:
                    break
                _require_real_directory(parent, label="local Skill CAS parent")
            try:
                metadata = target.lstat()
                content = target.read_bytes()
                after = target.lstat()
            except OSError as error:
                raise ExtensionIntegrityError("local Skill CAS file is missing") from error
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse(metadata)
                or metadata.st_dev != after.st_dev
                or metadata.st_ino != after.st_ino
                or metadata.st_size != after.st_size
                or len(content) != record.size_bytes
            ):
                raise ExtensionIntegrityError("local Skill CAS file type or size is invalid")
            current = hashlib.sha256(content).hexdigest()
            if current != record.sha256:
                raise ExtensionIntegrityError("local Skill CAS file digest is invalid")
            observed.append(record)
        actual: list[str] = []
        for current, directories, names in os.walk(file_root, followlinks=False):
            current_path = Path(current)
            _require_real_directory(current_path, label="local Skill CAS directory")
            for name in directories:
                _require_real_directory(
                    current_path / name, label="local Skill CAS directory"
                )
            for name in names:
                item = current_path / name
                metadata = item.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or _is_reparse(metadata)
                    or metadata.st_nlink != 1
                ):
                    raise ExtensionIntegrityError(
                        "local Skill CAS inventory contains a linked or special file"
                    )
                actual.append(item.relative_to(file_root).as_posix())
        actual_paths = tuple(sorted(actual))
        if actual_paths != tuple(record.path for record in observed):
            raise ExtensionIntegrityError("local Skill CAS contains an unmanifested file")
        return LocalSkillBundle(
            artifact_sha256,
            bundle.metadata,
            bundle.files,
            bundle.total_size_bytes,
        )

    def read_verified_file(self, artifact_sha256: str, resource_id: str) -> bytes:
        """Read one exact static resource after re-verifying the whole revision.

        ``resource_id`` is a bundle-relative identity, never a host path.  The
        inventory digest and the file identity are checked again at read time so
        a Turn cannot consume bytes which differ from its frozen Extension
        revision after an administrator replaces or tampers with the CAS.
        """

        bundle = self.verify(artifact_sha256)
        normalized = _validate_path(resource_id, allow_script_paths=True)
        records = {record.path: record for record in bundle.files}
        record = records.get(normalized)
        if record is None:
            raise ExtensionIntegrityError("local Skill resource is absent from its CAS revision")
        root = self._directory(artifact_sha256) / "files"
        target = root.joinpath(*PurePosixPath(normalized).parts)
        for parent in target.parents:
            if parent == root.parent:
                break
            _require_real_directory(parent, label="local Skill CAS parent")
        try:
            before = target.lstat()
            content = target.read_bytes()
            after = target.lstat()
        except OSError as error:
            raise ExtensionIntegrityError("local Skill CAS resource is unavailable") from error
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or _is_reparse(before)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or len(content) != record.size_bytes
            or hashlib.sha256(content).hexdigest() != record.sha256
        ):
            raise ExtensionIntegrityError("local Skill CAS resource identity is invalid")
        return content

    def resolve_verified_file(
        self, artifact_sha256: str, resource_id: str
    ) -> tuple[Path, LocalBundleFile]:
        """Resolve one CAS file only after complete revision verification.

        This is the sole host-path bridge used by the controlled Skill process
        boundary.  Callers receive both the canonical path and its manifest
        digest, so an OS launcher can bind the exact CAS tree and entry bytes
        instead of accepting an arbitrary path supplied by Skill content.
        """

        bundle = self.verify(artifact_sha256)
        normalized = _validate_path(resource_id, allow_script_paths=True)
        records = {record.path: record for record in bundle.files}
        record = records.get(normalized)
        if record is None:
            raise ExtensionIntegrityError(
                "local Skill resource is absent from its CAS revision"
            )
        root = (self._directory(artifact_sha256) / "files").resolve(strict=True)
        target = root.joinpath(*PurePosixPath(normalized).parts).resolve(strict=True)
        if target.parent != root and root not in target.parents:
            raise ExtensionIntegrityError("local Skill CAS resource escaped its revision")
        # Reuse the stable-file and digest checks rather than returning a path
        # merely because it was present in the verified inventory.
        self.read_verified_file(artifact_sha256, normalized)
        return target, record

    def _ingest_files(
        self,
        files: Mapping[str, bytes],
        *,
        migrated_frontmatter: bool = False,
    ) -> LocalSkillBundle:
        if not files or len(files) > MAX_LOCAL_BUNDLE_FILES:
            raise ExtensionManifestError("local Skill must contain between 1 and 256 files")
        if "SKILL.md" not in files:
            raise ExtensionManifestError("local Skill root must contain exact-case SKILL.md")
        path_total = sum(len(path.encode("utf-8")) for path in files)
        total = sum(len(content) for content in files.values())
        if path_total > MAX_LOCAL_PATH_TOTAL_BYTES or total > MAX_LOCAL_BUNDLE_BYTES:
            raise ExtensionManifestError("local Skill normalized inventory exceeds product limits")
        for path, content in files.items():
            _validate_static_content(path, content)
        metadata = (
            parse_migrated_skill_frontmatter(files["SKILL.md"])
            if migrated_frontmatter
            else parse_skill_frontmatter(files["SKILL.md"])
        )
        parse_skill_runtime_manifest(
            files,
            allow_undeclared_scripts=migrated_frontmatter,
        )
        _validate_skill_execution_budget(files["SKILL.md"])
        records = tuple(
            LocalBundleFile(
                path=path,
                size_bytes=len(files[path]),
                sha256=hashlib.sha256(files[path]).hexdigest(),
            )
            for path in sorted(files)
        )
        unsigned = {
            "schema_version": LOCAL_BUNDLE_SCHEMA_VERSION,
            "kind": "declarative_skill",
            "metadata": metadata.to_dict(),
            "files": [record.to_dict() for record in records],
            "total_size_bytes": total,
        }
        canonical = _canonical_manifest(unsigned)
        digest = _bundle_digest(canonical)
        bundle = LocalSkillBundle(digest, metadata, records, total)
        target = self._directory(digest)
        if target.exists():
            return self.verify(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{digest}.", dir=str(target.parent))
        )
        try:
            (staging / "files").mkdir()
            for record in records:
                output = staging / "files" / Path(*PurePosixPath(record.path).parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(files[record.path])
            (staging / "bundle.json").write_bytes(canonical)
            try:
                os.replace(staging, target)
            except OSError:
                if not target.exists():
                    raise
            return self.verify(digest)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _directory(self, digest: str) -> Path:
        return self.root / "sha256" / digest[:2] / digest


def parse_skill_frontmatter(payload: bytes) -> LocalSkillMetadata:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_SKILL_DOCUMENT_BYTES:
        raise ExtensionManifestError("SKILL.md size is invalid")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExtensionManifestError("SKILL.md must be strict UTF-8") from error
    if text.startswith("\ufeff") or _CONTROL.search(text):
        raise ExtensionManifestError("SKILL.md contains a BOM or control character")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ExtensionManifestError("SKILL.md must begin with YAML frontmatter")
    closing = next((index for index, line in enumerate(lines[1:], 1) if line == "---"), None)
    if closing is None:
        raise ExtensionManifestError("SKILL.md frontmatter is not closed")
    prefix = "\n".join(lines[: closing + 1]).encode("utf-8")
    if len(prefix) > MAX_FRONTMATTER_BYTES:
        raise ExtensionManifestError("SKILL.md frontmatter exceeds 16 KiB")
    values: dict[str, Any] = {}
    index = 1
    while index < closing:
        line = lines[index]
        if not line or line.startswith((" ", "\t", "-")) or ":" not in line:
            raise ExtensionManifestError("SKILL.md frontmatter must use flat product fields")
        key, raw = line.split(":", 1)
        if not _FRONTMATTER_KEY.fullmatch(key) or key not in _FRONTMATTER_KEYS:
            raise ExtensionManifestError("SKILL.md frontmatter contains an unknown field")
        if key in values:
            raise ExtensionManifestError("SKILL.md frontmatter contains a duplicate field")
        raw = raw.strip()
        if key == "tags" and not raw:
            tags: list[str] = []
            index += 1
            while index < closing and lines[index].startswith("  - "):
                tags.append(_frontmatter_scalar(lines[index][4:].strip(), label="tag"))
                index += 1
            values[key] = tags
            continue
        if key == "tags":
            try:
                tags_value = json.loads(raw)
            except json.JSONDecodeError as error:
                raise ExtensionManifestError("SKILL.md tags must be a JSON string array") from error
            if not isinstance(tags_value, list) or not all(isinstance(tag, str) for tag in tags_value):
                raise ExtensionManifestError("SKILL.md tags must be a string array")
            values[key] = tags_value
        else:
            values[key] = _frontmatter_scalar(raw, label=key)
        index += 1
    missing = _REQUIRED_FRONTMATTER_KEYS - set(values)
    if missing:
        raise ExtensionManifestError("SKILL.md frontmatter is missing name or description")
    name = _bounded_text(values["name"], label="name", maximum=128)
    description = _bounded_text(values["description"], label="description", maximum=2048)
    version = _bounded_text(values.get("version", "0.0.0"), label="version", maximum=128)
    parse_semver(version)
    license_value = values.get("license")
    license_name = (
        _bounded_text(license_value, label="license", maximum=128)
        if license_value is not None
        else None
    )
    compatibility = _bounded_text(
        values.get("compatibility", "*"), label="compatibility", maximum=256
    )
    validate_version_range(compatibility)
    raw_tags = values.get("tags", [])
    if len(raw_tags) > 32 or any(not _TAG.fullmatch(tag) for tag in raw_tags):
        raise ExtensionManifestError("SKILL.md tags must be bounded safe identifiers")
    tags = tuple(sorted(set(raw_tags)))
    if len(tags) != len(raw_tags):
        raise ExtensionManifestError("SKILL.md tags must be unique")
    return LocalSkillMetadata(
        name=name,
        description=description,
        version=version,
        license=license_name,
        compatibility=compatibility,
        tags=tags,
    )


def parse_migrated_skill_frontmatter(payload: bytes) -> LocalSkillMetadata:
    """Read identity fields from legacy YAML without trusting legacy metadata."""

    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_SKILL_DOCUMENT_BYTES:
        raise ExtensionManifestError("SKILL.md size is invalid")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExtensionManifestError("SKILL.md must be strict UTF-8") from error
    if text.startswith("\ufeff") or _CONTROL.search(text):
        raise ExtensionManifestError("SKILL.md contains a BOM or control character")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ExtensionManifestError("SKILL.md must begin with YAML frontmatter")
    closing = next((index for index, line in enumerate(lines[1:], 1) if line == "---"), None)
    if closing is None:
        raise ExtensionManifestError("SKILL.md frontmatter is not closed")
    if len("\n".join(lines[: closing + 1]).encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise ExtensionManifestError("SKILL.md frontmatter exceeds 16 KiB")
    values: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        if key in {"name", "description", "version", "license", "compatibility"}:
            values.setdefault(key, _frontmatter_scalar(raw.strip(), label=key))
    missing = _REQUIRED_FRONTMATTER_KEYS - set(values)
    if missing:
        raise ExtensionManifestError("SKILL.md frontmatter is missing name or description")
    name = _bounded_text(values["name"], label="name", maximum=128)
    description = _bounded_text(values["description"], label="description", maximum=2048)
    version = _bounded_text(values.get("version", "0.0.0"), label="version", maximum=128)
    parse_semver(version)
    compatibility = _bounded_text(
        values.get("compatibility", "*"), label="compatibility", maximum=256
    )
    validate_version_range(compatibility)
    license_value = values.get("license")
    return LocalSkillMetadata(
        name=name,
        description=description,
        version=version,
        license=(
            _bounded_text(license_value, label="license", maximum=128)
            if license_value is not None
            else None
        ),
        compatibility=compatibility,
    )


def parse_skill_runtime_manifest(
    files: Mapping[str, bytes],
    *,
    allow_undeclared_scripts: bool = False,
) -> SkillRuntimeManifest | None:
    scripts = tuple(
        path for path in files if PurePosixPath(path).suffix.casefold() in _SCRIPT_SUFFIXES
    )
    payload = files.get(SKILL_RUNTIME_FILE)
    if payload is None:
        if scripts and not allow_undeclared_scripts:
            raise ExtensionManifestError("Skill scripts require skill-runtime.json")
        return None
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ExtensionManifestError) as error:
        raise ExtensionManifestError("skill-runtime.json must be canonical JSON") from error
    required = {
        "schema_version", "runtime", "entrypoint", "environment",
        "network_domains", "external_commands", "effects",
    }
    if not isinstance(value, Mapping) or set(value) != required or value["schema_version"] != 1:
        raise ExtensionManifestError("skill-runtime.json contract is invalid")
    runtime = value["runtime"]
    if runtime not in {"python", "node", "trusted-shell"}:
        raise ExtensionManifestError("Skill runtime is unsupported")
    entrypoint = _validate_path(str(value["entrypoint"]), allow_script_paths=True)
    expected_suffixes = {
        "python": frozenset({".py"}),
        "node": frozenset({".js", ".mjs"}),
        "trusted-shell": frozenset({".sh"}),
    }[runtime]
    if (
        entrypoint not in files
        or PurePosixPath(entrypoint).suffix.casefold() not in expected_suffixes
    ):
        raise ExtensionManifestError("Skill entrypoint does not match its declared runtime")

    def string_list(key: str, *, maximum: int = 32) -> tuple[str, ...]:
        raw = value[key]
        if (
            not isinstance(raw, list)
            or len(raw) > maximum
            or not all(isinstance(item, str) and item for item in raw)
            or len(set(raw)) != len(raw)
        ):
            raise ExtensionManifestError(f"Skill {key} declaration is invalid")
        return tuple(raw)

    environment = string_list("environment")
    domains = tuple(item.casefold() for item in string_list("network_domains"))
    commands = string_list("external_commands")
    effects = string_list("effects", maximum=4)
    if any(_ENVIRONMENT_NAME.fullmatch(item) is None for item in environment):
        raise ExtensionManifestError("Skill environment declarations are invalid")
    if any(_DOMAIN_NAME.fullmatch(item) is None for item in domains):
        raise ExtensionManifestError("Skill network domain declarations are invalid")
    if any(not _TAG.fullmatch(item) for item in commands):
        raise ExtensionManifestError("Skill external command declarations are invalid")
    if not effects or any(item not in {"read", "write", "network", "execute"} for item in effects):
        raise ExtensionManifestError("Skill effect declarations are invalid")
    if bool(domains) != ("network" in effects) or bool(commands) != ("execute" in effects):
        raise ExtensionManifestError("Skill effects do not match network or command declarations")
    return SkillRuntimeManifest(runtime, entrypoint, environment, domains, commands, effects)


def _validate_skill_execution_budget(payload: bytes) -> None:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExtensionManifestError("SKILL.md must be strict UTF-8") from error
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ExtensionManifestError("SKILL.md must begin with YAML frontmatter")
    closing = next(
        (index for index, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"),
        None,
    )
    if closing is None:
        raise ExtensionManifestError("SKILL.md frontmatter is not closed")
    body = "".join(lines[closing + 1 :])
    lexical = len(re.findall(r"[\w\u3400-\u9fff]+|[^\s]", body, re.UNICODE))
    byte_bound = (len(body.encode("utf-8")) + 2) // 3
    if max(lexical, byte_bound) > MAX_SKILL_ESTIMATED_TOKENS:
        raise ExtensionManifestError("SKILL.md instructions exceed the execution token boundary")


def _frontmatter_scalar(raw: str, *, label: str) -> str:
    if not raw or raw[0] in "|>&*!{}[]@`" or "\t" in raw:
        raise ExtensionManifestError(f"SKILL.md {label} must be a bounded scalar")
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ExtensionManifestError(f"SKILL.md {label} quoted scalar is invalid") from error
        if not isinstance(value, str):
            raise ExtensionManifestError(f"SKILL.md {label} must be a string")
        return value
    if raw.startswith("'"):
        if len(raw) < 2 or not raw.endswith("'"):
            raise ExtensionManifestError(f"SKILL.md {label} quoted scalar is invalid")
        return raw[1:-1].replace("''", "'")
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    return raw


def _bounded_text(value: Any, *, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value.encode("utf-8")) > maximum
        or _CONTROL.search(value)
    ):
        raise ExtensionManifestError(f"SKILL.md {label} is invalid")
    return value


def _validate_path(
    value: str,
    *,
    allow_directory: bool = False,
    allow_script_paths: bool = False,
) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ExtensionManifestError("local Skill contains an unsafe path")
    candidate = value[:-1] if allow_directory and value.endswith("/") else value
    path = PurePosixPath(candidate)
    parts = path.parts
    if (
        not candidate
        or candidate.startswith("/")
        or path.is_absolute()
        or len(parts) > MAX_LOCAL_PATH_DEPTH
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
        or len(candidate.encode("utf-8")) > MAX_LOCAL_PATH_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
        or unicodedata.normalize("NFC", candidate) != candidate
        or any(
            part.endswith((".", " "))
            or _WINDOWS_RESERVED.fullmatch(part) is not None
            for part in parts
        )
    ):
        raise ExtensionManifestError("local Skill path escapes the static bundle boundary")
    if any(
        part.casefold() in _BANNED_SEGMENTS
        and not (allow_script_paths and part.casefold() == "scripts")
        for part in parts
    ):
        raise ExtensionManifestError("local Skill contains a forbidden executable namespace")
    return path.as_posix()


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _require_real_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ExtensionIntegrityError(f"{label} is missing") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
    ):
        raise ExtensionIntegrityError(f"{label} is not a real directory")


def _validate_static_resource(path: str, *, allow_scripts: bool = False) -> None:
    if path == "SKILL.md":
        return
    suffix = PurePosixPath(path).suffix.casefold()
    if "scripts" in (part.casefold() for part in PurePosixPath(path).parts) and suffix not in _SCRIPT_SUFFIXES:
        raise ExtensionManifestError("Skill scripts namespace may contain only declared script files")
    if suffix in _SCRIPT_SUFFIXES and not allow_scripts:
        raise ExtensionManifestError("local Skill scripts require migrated package provenance")
    if suffix in _BANNED_SUFFIXES or suffix not in _STATIC_SUFFIXES:
        raise ExtensionManifestError("local Skill resources must use a static data format")


def _validate_executable_resource_mode(path: str, mode: int) -> None:
    if mode & 0o111 and PurePosixPath(path).suffix.casefold() not in _SCRIPT_SUFFIXES:
        raise ExtensionManifestError(
            "only declared Skill script formats may carry an executable mode"
        )


def _validate_static_content(path: str, content: bytes) -> None:
    suffix = PurePosixPath(path).suffix.casefold()
    if path == "SKILL.md" or suffix in {
        ".csv", ".js", ".json", ".md", ".mjs", ".py", ".sh", ".tsv",
        ".txt", ".xml", ".yaml", ".yml"
    }:
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExtensionManifestError("local Skill text resources must be strict UTF-8") from error
        if "\x00" in decoded or _CONTROL.search(decoded):
            raise ExtensionManifestError("local Skill text resource contains control bytes")
        return
    signatures = {
        ".png": (b"\x89PNG\r\n\x1a\n",),
        ".jpg": (b"\xff\xd8\xff",),
        ".jpeg": (b"\xff\xd8\xff",),
        ".gif": (b"GIF87a", b"GIF89a"),
        ".bmp": (b"BM",),
    }
    if suffix == ".webp":
        valid = len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    elif suffix == ".avif":
        valid = len(content) >= 12 and content[4:8] == b"ftyp" and b"avif" in content[8:32]
    else:
        valid = any(content.startswith(prefix) for prefix in signatures.get(suffix, ()))
    if not valid:
        raise ExtensionManifestError("local Skill media resource signature is invalid")


def _canonical_manifest(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _bundle_digest(canonical_manifest: bytes) -> str:
    return hashlib.sha256(b"ecorex-local-skill-bundle-v1\0" + canonical_manifest).hexdigest()


def _bundle_from_manifest(value: Any) -> LocalSkillBundle:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "kind", "metadata", "files", "total_size_bytes"
    }:
        raise ExtensionIntegrityError("local Skill CAS manifest shape is invalid")
    if value["schema_version"] != LOCAL_BUNDLE_SCHEMA_VERSION or value["kind"] != "declarative_skill":
        raise ExtensionIntegrityError("local Skill CAS manifest contract is unsupported")
    metadata_raw = value["metadata"]
    files_raw = value["files"]
    if not isinstance(metadata_raw, Mapping) or set(metadata_raw) != {
        "name", "description", "version", "license", "compatibility", "tags"
    }:
        raise ExtensionIntegrityError("local Skill CAS metadata is invalid")
    tags = metadata_raw["tags"]
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ExtensionIntegrityError("local Skill CAS tags are invalid")
    metadata = LocalSkillMetadata(
        name=str(metadata_raw["name"]),
        description=str(metadata_raw["description"]),
        version=str(metadata_raw["version"]),
        license=(str(metadata_raw["license"]) if metadata_raw["license"] is not None else None),
        compatibility=str(metadata_raw["compatibility"]),
        tags=tuple(tags),
    )
    # Reuse the frontmatter validators for semantic fields without reparsing YAML.
    _bounded_text(metadata.name, label="name", maximum=128)
    _bounded_text(metadata.description, label="description", maximum=2048)
    parse_semver(metadata.version)
    validate_version_range(metadata.compatibility)
    if metadata.license is not None:
        _bounded_text(metadata.license, label="license", maximum=128)
    if tuple(sorted(set(metadata.tags))) != metadata.tags or any(
        not _TAG.fullmatch(tag) for tag in metadata.tags
    ):
        raise ExtensionIntegrityError("local Skill CAS tags are non-canonical")
    if not isinstance(files_raw, list) or not 1 <= len(files_raw) <= MAX_LOCAL_BUNDLE_FILES:
        raise ExtensionIntegrityError("local Skill CAS file inventory is invalid")
    records: list[LocalBundleFile] = []
    for raw in files_raw:
        if not isinstance(raw, Mapping) or set(raw) != {"path", "size_bytes", "sha256"}:
            raise ExtensionIntegrityError("local Skill CAS file record is invalid")
        path = _validate_path(str(raw["path"]), allow_script_paths=True)
        _validate_static_resource(path, allow_scripts=True)
        size = raw["size_bytes"]
        digest = raw["sha256"]
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= MAX_LOCAL_FILE_BYTES
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
        ):
            raise ExtensionIntegrityError("local Skill CAS file identity is invalid")
        records.append(LocalBundleFile(path, size, digest))
    if tuple(record.path for record in records) != tuple(sorted(record.path for record in records)):
        raise ExtensionIntegrityError("local Skill CAS file inventory is not canonical")
    folded = [record.path.casefold() for record in records]
    if len(folded) != len(set(folded)) or "SKILL.md" not in {record.path for record in records}:
        raise ExtensionIntegrityError("local Skill CAS file inventory collides or lacks SKILL.md")
    total = value["total_size_bytes"]
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total != sum(record.size_bytes for record in records)
        or total > MAX_LOCAL_BUNDLE_BYTES
    ):
        raise ExtensionIntegrityError("local Skill CAS total size is invalid")
    return LocalSkillBundle("", metadata, tuple(records), total)


def _reject_duplicate_pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExtensionManifestError("local Skill CAS manifest contains duplicate keys")
        result[key] = value
    return result


__all__ = [
    "LOCAL_BUNDLE_SCHEMA_VERSION",
    "MAX_LOCAL_BUNDLE_BYTES",
    "MAX_LOCAL_BUNDLE_FILES",
    "MAX_LOCAL_FILE_BYTES",
    "LocalBundleFile",
    "LocalSkillBundle",
    "LocalSkillBundleStore",
    "LocalSkillMetadata",
    "parse_migrated_skill_frontmatter",
    "parse_skill_frontmatter",
]
