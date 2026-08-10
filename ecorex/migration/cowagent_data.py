"""Safe one-time import of data from the predecessor desktop agent.

This module is the only product source allowed to know the predecessor's
filesystem identity.  It copies user-owned data without following links,
never overwrites e-Mate data, and records a secret-free receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from typing import Any, Iterable, Mapping

from ecorex.workspace_content.paths import MAX_DOCUMENT_BYTES, normalize_knowledge_path

from .errors import MigrationError, SourceLayoutError
from .path_security import (
    is_within,
    lexical_absolute,
    secure_directory,
    stable_copy_file,
    stable_read_bytes,
    stable_sha256_file,
)


RECEIPT_RELATIVE_PATH = Path("migration/legacy-desktop-import-v1.json")
KNOWLEDGE_LAYOUT_RECEIPT_RELATIVE_PATH = Path(
    "migration/legacy-knowledge-layout-v1.json"
)
_RECEIPT_DOMAIN = b"e-Mate legacy desktop data import receipt v1\0"
_INVENTORY_DOMAIN = b"e-Mate legacy desktop data inventory v1\0"
_MAX_RECEIPT_BYTES = 8 * 1024 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_FILES = 100_000

_COPY_TREES = frozenset(
    {
        "artifacts",
        "attachments",
        "conversations",
        "files",
        "history",
        "images",
        "knowledge",
        "memory",
        "scheduler",
        "sessions",
        "skills",
        "tasks",
        "uploads",
    }
)
_COPY_FILES = frozenset({"AGENT.md", "MEMORY.md", "RULE.md", "USER.md"})
_CHANNEL_CONFIG_FILES = frozenset({"channel.json", "channels.json", "config.json"})
_CHANNEL_NAMES = frozenset(
    {
        "dingtalk",
        "discord",
        "feishu",
        "lark",
        "qq",
        "slack",
        "telegram",
        "weixin",
        "wechat_kf",
        "wechatcom",
        "wechatcom_app",
        "wechatmp",
        "wechatmp_service",
        "wecom",
        "wecom_app",
        "wecom_bot",
    }
)
_CHANNEL_SETTING_PREFIXES = (
    "channel_",
    "group_",
    "single_",
    "subscribe_",
    "trigger_",
    *(f"{name}_" for name in sorted(_CHANNEL_NAMES)),
)
_SECRET_KEY = re.compile(
    r"(?:^|[._-])(?:api[._-]?keys?|auth|bearer|cookies?|credentials?|keys?|passwd|passwords?|private|secrets?|tokens?)(?:$|[._-])"
    r"|(?:api|access|app|aes|bot|private)(?:keys?|secrets?|tokens?)$",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"^(?:bearer\s+|sk-|xox[aboprs]-|gh[opusr]_|AIza)[A-Za-z0-9._~+/=-]+$",
    re.IGNORECASE,
)
_SECRET_PATH_PARTS = frozenset(
    {
        ".env",
        "browser",
        "browser_profile",
        "cache",
        "chrome_cdp_profile",
        "cookies",
        "credentials",
        "local state",
        "network",
        "profile",
        "profiles",
        "web token",
    }
)
_REPARSE_POINT = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


class CowAgentDataMigrationError(MigrationError):
    """The one-time predecessor data import could not be trusted."""


@dataclass(frozen=True, slots=True)
class LegacyDataRoot:
    label: str
    path: Path


@dataclass(frozen=True, slots=True)
class LegacyDataMigrationResult:
    status: str
    idempotent_replay: bool
    receipt_path: Path
    source_inventory_sha256: str | None
    copied_files: int
    reused_files: int
    skipped_entries: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "idempotent_replay": self.idempotent_replay,
            "receipt_path": str(self.receipt_path),
            "source_inventory_sha256": self.source_inventory_sha256,
            "copied_files": self.copied_files,
            "reused_files": self.reused_files,
            "skipped_entries": self.skipped_entries,
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    source_label: str
    source: Path
    source_relative_path: str
    target_relative_path: str
    size_bytes: int
    source_sha256: str
    transform: str


def default_cowagent_data_roots(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> tuple[LegacyDataRoot, ...]:
    """Return predecessor roots in deterministic precedence order."""

    values = os.environ if environ is None else environ
    user_home = Path.home() if home is None else Path(home)
    host = sys.platform if platform is None else platform
    candidates: list[LegacyDataRoot] = [
        LegacyDataRoot("legacy-config", user_home / ".cow"),
    ]
    if host.casefold().startswith("win"):
        roaming = Path(values.get("APPDATA") or user_home / "AppData/Roaming")
        local = Path(values.get("LOCALAPPDATA") or user_home / "AppData/Local")
        candidates.extend(
            (
                LegacyDataRoot("legacy-roaming", roaming / "CowAgent"),
                LegacyDataRoot("legacy-local", local / "CowAgent"),
            )
        )
    candidates.append(LegacyDataRoot("legacy-workspace", user_home / "cow"))
    return _deduplicate_roots(candidates)


def default_emate_data_root(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    del environ, platform
    user_home = Path.home() if home is None else Path(home)
    return user_home / ".emate"


def migrate_cowagent_data(
    target_root: str | os.PathLike[str],
    *,
    source_roots: Iterable[LegacyDataRoot | tuple[str, str | os.PathLike[str]]]
    | None = None,
) -> LegacyDataMigrationResult:
    """Copy the allowlisted predecessor data once and return its audit result."""

    target = _prepare_target_root(Path(target_root))
    migrate_legacy_knowledge_layout(target)
    receipt_path = target / RECEIPT_RELATIVE_PATH
    if os.path.lexists(receipt_path):
        receipt = _load_and_validate_receipt(receipt_path, target, verify_files=False)
        return _result(receipt, receipt_path, replay=True)

    roots = _normalize_roots(
        default_cowagent_data_roots() if source_roots is None else source_roots
    )
    existing: list[LegacyDataRoot] = []
    for item in roots:
        if not os.path.lexists(item.path):
            continue
        try:
            source = secure_directory(item.path, label=f"{item.label} source root")
        except SourceLayoutError as error:
            raise CowAgentDataMigrationError(
                "legacy data source root is unsafe"
            ) from error
        if is_within(target, source) or is_within(source, target):
            raise CowAgentDataMigrationError(
                "legacy and e-Mate data roots must be disjoint"
            )
        existing.append(LegacyDataRoot(item.label, source))
    if not existing:
        return LegacyDataMigrationResult(
            status="source_missing",
            idempotent_replay=False,
            receipt_path=receipt_path,
            source_inventory_sha256=None,
            copied_files=0,
            reused_files=0,
            skipped_entries=0,
        )

    candidates: list[_Candidate] = []
    skipped: list[dict[str, str]] = []
    for root in existing:
        _inventory_root(root, candidates=candidates, skipped=skipped)
        if len(candidates) + len(skipped) > _MAX_FILES:
            raise CowAgentDataMigrationError(
                "legacy data inventory exceeds its file limit"
            )
    candidates.sort(
        key=lambda item: (
            item.target_relative_path.casefold(),
            item.target_relative_path,
            item.source_label,
        )
    )
    skipped.sort(
        key=lambda item: (item["source"], item["path"].casefold(), item["path"])
    )
    inventory_digest = _inventory_digest(candidates, skipped)

    files: list[dict[str, Any]] = []
    claimed_targets: set[str] = set()
    for candidate in candidates:
        relative_key = candidate.target_relative_path.casefold()
        if relative_key in claimed_targets:
            skipped.append(
                {
                    "source": candidate.source_label,
                    "path": candidate.source_relative_path,
                    "reason": "lower_precedence_duplicate",
                }
            )
            continue
        claimed_targets.add(relative_key)
        files.append(_copy_candidate(candidate, target))

    receipt: dict[str, Any] = {
        "schema_version": 1,
        "status": "completed",
        "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_inventory_sha256": inventory_digest,
        "sources": [item.label for item in existing],
        "files": files,
        "skipped": sorted(
            skipped,
            key=lambda item: (item["source"], item["path"].casefold(), item["path"]),
        ),
    }
    receipt["authority_sha256"] = _receipt_digest(receipt)
    _publish_receipt(receipt_path, receipt)
    persisted = _load_and_validate_receipt(receipt_path, target, verify_files=True)
    return _result(persisted, receipt_path, replay=False)


def _deduplicate_roots(values: Iterable[LegacyDataRoot]) -> tuple[LegacyDataRoot, ...]:
    result: list[LegacyDataRoot] = []
    seen: set[str] = set()
    for item in values:
        key = os.path.normcase(os.path.abspath(os.fspath(item.path)))
        if key not in seen:
            seen.add(key)
            result.append(LegacyDataRoot(item.label, lexical_absolute(item.path)))
    return tuple(result)


def _normalize_roots(
    values: Iterable[LegacyDataRoot | tuple[str, str | os.PathLike[str]]],
) -> tuple[LegacyDataRoot, ...]:
    normalized: list[LegacyDataRoot] = []
    labels: set[str] = set()
    for raw in values:
        item = (
            raw
            if isinstance(raw, LegacyDataRoot)
            else LegacyDataRoot(raw[0], Path(raw[1]))
        )
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", item.label)
            or item.label in labels
        ):
            raise CowAgentDataMigrationError(
                "legacy data source labels must be unique safe tokens"
            )
        labels.add(item.label)
        normalized.append(LegacyDataRoot(item.label, lexical_absolute(item.path)))
    return _deduplicate_roots(normalized)


def _prepare_target_root(path: Path) -> Path:
    target = lexical_absolute(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
        return secure_directory(target, label="e-Mate data root")
    except (OSError, SourceLayoutError) as error:
        raise CowAgentDataMigrationError("e-Mate data root is unsafe") from error


def _portable_knowledge_path(relative: str) -> PurePosixPath | None:
    try:
        normalized = normalize_knowledge_path(relative)
    except ValueError:
        return None
    return normalized if normalized.as_posix() == relative else None


def _knowledge_candidate(
    path: Path,
    *,
    relative: str,
    metadata: os.stat_result,
    root: Path,
) -> tuple[PurePosixPath, str, int] | str:
    normalized = _portable_knowledge_path(relative)
    if normalized is None:
        return "non_portable_knowledge_path"
    if normalized.suffix.casefold() not in {".md", ".txt"}:
        return "unsupported_knowledge_file"
    if metadata.st_size > MAX_DOCUMENT_BYTES:
        return "knowledge_file_too_large"
    try:
        payload = stable_read_bytes(
            path,
            label=f"legacy knowledge file {relative}",
            maximum=MAX_DOCUMENT_BYTES,
            root=root,
        )
    except SourceLayoutError as error:
        raise CowAgentDataMigrationError("legacy knowledge file is unsafe") from error
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return "knowledge_file_not_utf8"
    if "\x00" in text:
        return "knowledge_file_contains_nul"
    return normalized, hashlib.sha256(payload).hexdigest(), len(payload)


def _inventory_root(
    root: LegacyDataRoot,
    *,
    candidates: list[_Candidate],
    skipped: list[dict[str, str]],
) -> None:
    def skip(relative: str, reason: str) -> None:
        skipped.append({"source": root.label, "path": relative, "reason": reason})

    def visit(directory: Path, relative_parts: tuple[str, ...]) -> None:
        try:
            children = sorted(
                directory.iterdir(), key=lambda item: (item.name.casefold(), item.name)
            )
        except OSError as error:
            raise CowAgentDataMigrationError(
                "legacy data directory is unreadable"
            ) from error
        for child in children:
            relative = "/".join((*relative_parts, child.name))
            try:
                metadata = child.lstat()
            except OSError as error:
                raise CowAgentDataMigrationError(
                    "legacy data entry is unreadable"
                ) from error
            if stat.S_ISLNK(metadata.st_mode) or bool(
                int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT
            ):
                skip(relative, "unsafe_link_or_reparse")
                continue
            parts = (*relative_parts, child.name)
            secret_reason = _secret_path_reason(parts)
            if stat.S_ISDIR(metadata.st_mode):
                if secret_reason:
                    skip(relative + "/", secret_reason)
                elif not relative_parts and child.name.casefold() not in _COPY_TREES | {
                    "channels"
                }:
                    skip(relative + "/", "outside_allowlist")
                elif (
                    parts[0].casefold() == "knowledge"
                    and len(parts) > 1
                    and _portable_knowledge_path("/".join(parts[1:])) is None
                ):
                    skip(relative + "/", "non_portable_knowledge_path")
                else:
                    visit(child, parts)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                skip(relative, "special_file")
                continue
            if secret_reason:
                skip(relative, secret_reason)
                continue
            top = parts[0].casefold()
            transform = "copy"
            target_relative = relative
            if len(parts) == 1 and child.name in _COPY_FILES:
                pass
            elif len(parts) == 1 and child.name.casefold() in _CHANNEL_CONFIG_FILES:
                transform = "sanitized_channel_json"
                target_relative = f"channels/imported-{root.label}-settings.json"
            elif top == "channels":
                if child.suffix.casefold() != ".json":
                    skip(relative, "channel_config_requires_json")
                    continue
                transform = "sanitized_channel_json"
            elif top not in _COPY_TREES:
                skip(relative, "outside_allowlist")
                continue
            elif top == "knowledge":
                knowledge_relative = "/".join(parts[1:])
                knowledge = _knowledge_candidate(
                    child,
                    relative=knowledge_relative,
                    metadata=metadata,
                    root=root.path,
                )
                if isinstance(knowledge, str):
                    skip(relative, knowledge)
                    continue
                normalized, digest, size = knowledge
                candidates.append(
                    _Candidate(
                        source_label=root.label,
                        source=child,
                        source_relative_path=relative,
                        target_relative_path=f"workspace/knowledge/{normalized.as_posix()}",
                        size_bytes=size,
                        source_sha256=digest,
                        transform="copy",
                    )
                )
                continue
            try:
                digest, identity = stable_sha256_file(
                    child,
                    label=f"legacy data file {relative}",
                    root=root.path,
                )
            except SourceLayoutError as error:
                raise CowAgentDataMigrationError(
                    "legacy data file is unsafe"
                ) from error
            candidates.append(
                _Candidate(
                    source_label=root.label,
                    source=child,
                    source_relative_path=relative,
                    target_relative_path=target_relative,
                    size_bytes=identity.size,
                    source_sha256=digest,
                    transform=transform,
                )
            )

    visit(root.path, ())


def migrate_legacy_knowledge_layout(target_root: str | os.PathLike[str]) -> Path | None:
    """Copy the retired ``knowledge`` tree into ``workspace/knowledge`` once."""

    target = _prepare_target_root(Path(target_root))
    receipt_path = target / KNOWLEDGE_LAYOUT_RECEIPT_RELATIVE_PATH
    if os.path.lexists(receipt_path):
        _load_and_validate_receipt(receipt_path, target, verify_files=False)
        return receipt_path
    source_path = target / "knowledge"
    if not os.path.lexists(source_path):
        return None
    try:
        source = secure_directory(
            source_path,
            label="legacy knowledge layout",
            root=target,
        )
    except SourceLayoutError as error:
        raise CowAgentDataMigrationError("legacy knowledge layout is unsafe") from error

    candidates: list[_Candidate] = []
    skipped: list[dict[str, str]] = []

    def visit(directory: Path, parts: tuple[str, ...]) -> None:
        try:
            children = sorted(
                directory.iterdir(), key=lambda item: (item.name.casefold(), item.name)
            )
        except OSError as error:
            raise CowAgentDataMigrationError(
                "legacy knowledge directory is unreadable"
            ) from error
        for child in children:
            relative_parts = (*parts, child.name)
            relative = "/".join(relative_parts)
            if len(candidates) + len(skipped) >= _MAX_FILES:
                raise CowAgentDataMigrationError(
                    "legacy knowledge inventory exceeds its file limit"
                )
            try:
                metadata = child.lstat()
            except OSError as error:
                raise CowAgentDataMigrationError(
                    "legacy knowledge entry is unreadable"
                ) from error
            if stat.S_ISLNK(metadata.st_mode) or bool(
                int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT
            ):
                skipped.append(
                    {"source": "legacy-layout", "path": relative, "reason": "unsafe_link_or_reparse"}
                )
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if _portable_knowledge_path(relative) is None:
                    skipped.append(
                        {
                            "source": "legacy-layout",
                            "path": relative + "/",
                            "reason": "non_portable_knowledge_path",
                        }
                    )
                    continue
                visit(child, relative_parts)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                skipped.append(
                    {"source": "legacy-layout", "path": relative, "reason": "special_file"}
                )
                continue
            knowledge = _knowledge_candidate(
                child,
                relative=relative,
                metadata=metadata,
                root=source,
            )
            if isinstance(knowledge, str):
                skipped.append(
                    {"source": "legacy-layout", "path": relative, "reason": knowledge}
                )
                continue
            normalized, digest, size = knowledge
            candidates.append(
                _Candidate(
                    source_label="legacy-layout",
                    source=child,
                    source_relative_path=relative,
                    target_relative_path=f"workspace/knowledge/{normalized.as_posix()}",
                    size_bytes=size,
                    source_sha256=digest,
                    transform="copy",
                )
            )

    visit(source, ())
    candidates.sort(key=lambda item: (item.target_relative_path.casefold(), item.target_relative_path))
    files: list[dict[str, Any]] = []
    claimed_targets: set[str] = set()
    for candidate in candidates:
        target_key = candidate.target_relative_path.casefold()
        if target_key in claimed_targets:
            skipped.append(
                {
                    "source": candidate.source_label,
                    "path": candidate.source_relative_path,
                    "reason": "portable_name_collision",
                }
            )
            continue
        claimed_targets.add(target_key)
        files.append(_copy_candidate(candidate, target))
    skipped.sort(key=lambda item: (item["path"].casefold(), item["path"]))
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "status": "completed",
        "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_inventory_sha256": _inventory_digest(candidates, skipped),
        "sources": ["legacy-layout"],
        "files": files,
        "skipped": skipped,
    }
    receipt["authority_sha256"] = _receipt_digest(receipt)
    _publish_receipt(receipt_path, receipt)
    _load_and_validate_receipt(receipt_path, target, verify_files=True)
    return receipt_path


def _secret_path_reason(parts: tuple[str, ...]) -> str | None:
    folded = tuple(part.casefold() for part in parts)
    for part in folded:
        if part in _SECRET_PATH_PARTS or part.startswith(".env."):
            return "secret_or_browser_state"
    if _SECRET_KEY.search(parts[-1]):
        return "secret_named_file"
    return None


def _channel_settings(payload: bytes) -> bytes:
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CowAgentDataMigrationError(
            "legacy channel configuration is not valid JSON"
        ) from None
    if not isinstance(raw, dict):
        raise CowAgentDataMigrationError(
            "legacy channel configuration must be an object"
        )
    safe = {
        str(key): sanitized
        for key, value in raw.items()
        if _is_channel_setting(str(key))
        if (sanitized := _sanitize_json(value)) is not None
    }
    return (
        json.dumps(safe, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _is_channel_setting(key: str) -> bool:
    folded = key.casefold()
    return (
        (
            folded in _CHANNEL_NAMES
            or folded == "channel_type"
            or any(folded.startswith(prefix) for prefix in _CHANNEL_SETTING_PREFIXES)
        )
        and not _SECRET_KEY.search(folded)
        and not folded.endswith(("_path", "_dir", "_proxy"))
    )


def _sanitize_json(value: Any, *, depth: int = 0) -> Any | None:
    if depth > 16:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return None if _SECRET_VALUE.fullmatch(value.strip()) else value
    if isinstance(value, list):
        return [
            item
            for child in value[:4096]
            if (item := _sanitize_json(child, depth=depth + 1)) is not None
        ]
    if isinstance(value, dict):
        return {
            str(key): item
            for key, child in list(value.items())[:4096]
            if not _SECRET_KEY.search(str(key))
            if (item := _sanitize_json(child, depth=depth + 1)) is not None
        }
    return None


def _inventory_digest(
    candidates: list[_Candidate], skipped: list[dict[str, str]]
) -> str:
    value = {
        "files": [
            {
                "source": item.source_label,
                "path": item.source_relative_path,
                "target": item.target_relative_path,
                "size_bytes": item.size_bytes,
                "source_sha256": item.source_sha256,
                "transform": item.transform,
            }
            for item in candidates
        ],
        "skipped": skipped,
    }
    return hashlib.sha256(_INVENTORY_DOMAIN + _canonical_json(value)).hexdigest()


def _copy_candidate(candidate: _Candidate, target_root: Path) -> dict[str, Any]:
    destination = target_root / Path(candidate.target_relative_path)
    if not is_within(destination, target_root):
        raise CowAgentDataMigrationError("legacy data target escaped the e-Mate root")
    _secure_mkdirs(destination.parent, target_root)
    payload: bytes | None = None
    if candidate.transform == "sanitized_channel_json":
        try:
            source_payload = stable_read_bytes(
                candidate.source,
                label="legacy channel configuration",
                maximum=_MAX_CONFIG_BYTES,
            )
            if hashlib.sha256(source_payload).hexdigest() != candidate.source_sha256:
                raise CowAgentDataMigrationError(
                    "legacy channel configuration changed during migration"
                )
            payload = _channel_settings(source_payload)
        except SourceLayoutError as error:
            raise CowAgentDataMigrationError(
                "legacy channel configuration is unsafe"
            ) from error

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        if payload is None:
            stable_copy_file(
                candidate.source,
                temporary,
                label=f"legacy data file {candidate.source_relative_path}",
            )
        else:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        target_sha256, target_identity = stable_sha256_file(
            temporary, label="staged legacy data file"
        )
        if candidate.transform == "copy" and target_sha256 != candidate.source_sha256:
            raise CowAgentDataMigrationError("legacy data changed while it was copied")
        status = _publish_file(temporary, destination, target_sha256, target_root)
        return {
            "source": candidate.source_label,
            "source_path": candidate.source_relative_path,
            "target_path": candidate.target_relative_path,
            "size_bytes": target_identity.size,
            "source_sha256": candidate.source_sha256,
            "target_sha256": target_sha256,
            "transform": candidate.transform,
            "status": status,
        }
    finally:
        temporary.unlink(missing_ok=True)


def _secure_mkdirs(directory: Path, target_root: Path) -> None:
    relative = directory.relative_to(target_root)
    current = target_root
    for part in relative.parts:
        current = current / part
        try:
            current.mkdir(exist_ok=True)
            secure_directory(
                current, label="e-Mate migration target directory", root=target_root
            )
        except (OSError, SourceLayoutError) as error:
            raise CowAgentDataMigrationError(
                "e-Mate migration target directory is unsafe"
            ) from error


def _publish_file(
    temporary: Path, destination: Path, digest: str, target_root: Path
) -> str:
    if os.path.lexists(destination):
        return _existing_file_status(destination, digest, target_root)
    try:
        os.link(temporary, destination)
    except FileExistsError:
        return _existing_file_status(destination, digest, target_root)
    except OSError as error:
        raise CowAgentDataMigrationError(
            "legacy data file could not be published atomically"
        ) from error
    return "copied"


def _existing_file_status(destination: Path, digest: str, target_root: Path) -> str:
    try:
        existing, _identity = stable_sha256_file(
            destination,
            label="existing e-Mate data file",
            root=target_root,
        )
    except SourceLayoutError as error:
        raise CowAgentDataMigrationError(
            "existing e-Mate data file is unsafe"
        ) from error
    return "reused" if existing == digest else "target_conflict"


def _publish_receipt(path: Path, value: Mapping[str, Any]) -> None:
    _secure_mkdirs(path.parent, path.parents[1])
    payload = _canonical_json(value) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError as error:
            raise CowAgentDataMigrationError(
                "legacy data receipt could not be published"
            ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _load_and_validate_receipt(
    path: Path, target_root: Path, *, verify_files: bool
) -> dict[str, Any]:
    try:
        value = json.loads(
            stable_read_bytes(
                path, label="legacy data receipt", maximum=_MAX_RECEIPT_BYTES
            )
        )
    except (SourceLayoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CowAgentDataMigrationError("legacy data receipt is unreadable") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("status") != "completed"
        or value.get("authority_sha256") != _receipt_digest(value)
        or re.fullmatch(r"[0-9a-f]{64}", str(value.get("source_inventory_sha256")))
        is None
        or not isinstance(value.get("files"), list)
        or not isinstance(value.get("skipped"), list)
    ):
        raise CowAgentDataMigrationError("legacy data receipt is invalid")
    for record in value["files"]:
        if not isinstance(record, dict) or record.get("status") not in {
            "copied",
            "reused",
            "target_conflict",
        }:
            raise CowAgentDataMigrationError(
                "legacy data receipt file record is invalid"
            )
        relative = record.get("target_path")
        digest = record.get("target_sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or re.fullmatch(r"[0-9a-f]{64}", str(digest)) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(record.get("source_sha256"))) is None
            or not isinstance(record.get("source"), str)
            or not isinstance(record.get("source_path"), str)
            or not isinstance(record.get("size_bytes"), int)
            or record.get("size_bytes", -1) < 0
            or record.get("transform") not in {"copy", "sanitized_channel_json"}
        ):
            raise CowAgentDataMigrationError(
                "legacy data receipt file authority is invalid"
            )
        destination = target_root / Path(relative)
        if not is_within(destination, target_root):
            raise CowAgentDataMigrationError(
                "legacy data receipt target escaped the e-Mate root"
            )
        if record["status"] == "target_conflict" or not verify_files:
            continue
        try:
            observed, _identity = stable_sha256_file(
                destination,
                label="migrated e-Mate data file",
                root=target_root,
            )
        except SourceLayoutError as error:
            raise CowAgentDataMigrationError(
                "migrated e-Mate data file is unavailable"
            ) from error
        if observed != digest:
            raise CowAgentDataMigrationError(
                "migrated e-Mate data file failed receipt verification"
            )
    return value


def _receipt_digest(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "authority_sha256"}
    return hashlib.sha256(_RECEIPT_DOMAIN + _canonical_json(unsigned)).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _result(
    value: Mapping[str, Any], path: Path, *, replay: bool
) -> LegacyDataMigrationResult:
    files = value.get("files", [])
    return LegacyDataMigrationResult(
        status="already_completed" if replay else str(value["status"]),
        idempotent_replay=replay,
        receipt_path=path,
        source_inventory_sha256=str(value["source_inventory_sha256"]),
        copied_files=sum(item.get("status") == "copied" for item in files),
        reused_files=sum(item.get("status") == "reused" for item in files),
        skipped_entries=len(value.get("skipped", []))
        + sum(item.get("status") == "target_conflict" for item in files),
    )


__all__ = [
    "CowAgentDataMigrationError",
    "KNOWLEDGE_LAYOUT_RECEIPT_RELATIVE_PATH",
    "LegacyDataMigrationResult",
    "LegacyDataRoot",
    "RECEIPT_RELATIVE_PATH",
    "default_cowagent_data_roots",
    "default_emate_data_root",
    "migrate_cowagent_data",
    "migrate_legacy_knowledge_layout",
]
