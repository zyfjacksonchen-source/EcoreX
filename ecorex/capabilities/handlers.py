"""Trusted local capability handlers and availability composition."""

from __future__ import annotations

import base64
import hashlib
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from .packs import CapabilityPackRuntime
from .registry import CapabilityRegistry
from .service import ToolHandler


class WorkspaceReadError(RuntimeError):
    code = "workspace_read_failed"


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class WorkspaceReadHandler:
    """Bounded, read-only file access beneath explicit workspace roots.

    Absolute host paths are never returned to the model.  Files are opened and
    compared to their pre-open identity to reduce link-swap/TOCTOU attacks.
    """

    def __init__(
        self,
        roots: tuple[Path | str, ...],
        *,
        default_max_bytes: int = 256 * 1024,
        hard_max_bytes: int = 1024 * 1024,
        max_file_size_bytes: int = 64 * 1024 * 1024,
        max_directory_entries: int = 500,
    ) -> None:
        if not roots:
            raise ValueError("at least one workspace root is required")
        if not 1 <= default_max_bytes <= hard_max_bytes <= 1024 * 1024:
            raise ValueError("workspace read byte limits are invalid")
        if not 1 <= max_directory_entries <= 5_000:
            raise ValueError("workspace directory entry limit is invalid")
        if not hard_max_bytes <= max_file_size_bytes <= 1024 * 1024 * 1024:
            raise ValueError("workspace maximum file size is invalid")
        normalized: list[Path] = []
        for root in roots:
            raw_root = Path(root)
            root_stat = raw_root.lstat()
            attributes = getattr(root_stat, "st_file_attributes", 0)
            reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat_module.S_ISLNK(root_stat.st_mode) or bool(attributes & reparse_flag):
                raise ValueError("workspace root cannot be a link or reparse point")
            path = raw_root.resolve(strict=True)
            if not path.is_dir():
                raise ValueError("workspace root must be an existing directory")
            if path in normalized:
                raise ValueError("workspace roots must be unique")
            normalized.append(path)
        self._roots = tuple(normalized)
        self._default_max_bytes = default_max_bytes
        self._hard_max_bytes = hard_max_bytes
        self._max_file_size_bytes = max_file_size_bytes
        self._max_directory_entries = max_directory_entries

    def __call__(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        raw_path = str(arguments["path"])
        offset = int(arguments.get("offset_bytes", 0))
        requested = int(arguments.get("max_bytes", self._default_max_bytes))
        if not 1 <= requested <= self._hard_max_bytes or offset < 0:
            raise WorkspaceReadError("workspace read range is invalid")
        path, root_index, relative = self._resolve(raw_path)
        try:
            before = path.lstat()
        except OSError as exc:
            raise WorkspaceReadError("workspace path cannot be inspected") from exc
        attributes = getattr(before, "st_file_attributes", 0)
        reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat_module.S_ISLNK(before.st_mode) or bool(attributes & reparse_flag):
            raise WorkspaceReadError("workspace links and reparse points are not readable")
        locator = self._locator(root_index, relative)
        if stat_module.S_ISDIR(before.st_mode):
            if offset:
                raise WorkspaceReadError("directory reads do not accept byte offsets")
            return self._directory(path, locator)
        if not stat_module.S_ISREG(before.st_mode):
            raise WorkspaceReadError("workspace path is not a regular file or directory")
        if before.st_size > self._max_file_size_bytes:
            raise WorkspaceReadError(
                "workspace file is too large for direct read; use the artifact service"
            )
        return self._file(path, locator, before, offset=offset, limit=requested)

    def _resolve(self, raw_path: str) -> tuple[Path, int, Path]:
        if "\x00" in raw_path or not raw_path.strip():
            raise WorkspaceReadError("workspace path is invalid")
        requested = Path(raw_path)
        candidates: list[tuple[Path, int]] = []
        if requested.is_absolute():
            candidates = [(requested, index) for index in range(len(self._roots))]
        else:
            pure = PurePosixPath(raw_path.replace("\\", "/"))
            if any(part in {"", ".", ".."} for part in pure.parts):
                raise WorkspaceReadError("workspace path traversal is forbidden")
            candidates = [
                (root.joinpath(*pure.parts), index)
                for index, root in enumerate(self._roots)
            ]
        for candidate, index in candidates:
            root = self._roots[index]
            try:
                lexical = candidate.absolute()
                relative_lexical = lexical.relative_to(root)
            except (OSError, ValueError):
                continue
            if any(part in {"", ".", ".."} for part in relative_lexical.parts):
                continue
            current = root
            linked = False
            for part in relative_lexical.parts:
                current = current / part
                try:
                    current_stat = current.lstat()
                except OSError:
                    linked = True
                    break
                attributes = getattr(current_stat, "st_file_attributes", 0)
                reparse_flag = getattr(
                    stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
                )
                if stat_module.S_ISLNK(current_stat.st_mode) or bool(
                    attributes & reparse_flag
                ):
                    linked = True
                    break
            if linked:
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if _within(resolved, root):
                return resolved, index, resolved.relative_to(root)
        raise WorkspaceReadError("workspace path is outside the authorized roots or missing")

    @staticmethod
    def _locator(root_index: int, relative: Path) -> str:
        suffix = relative.as_posix()
        return f"workspace://{root_index}/{suffix}" if suffix else f"workspace://{root_index}/"

    def _directory(self, path: Path, locator: str) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        try:
            with os.scandir(path) as scanner:
                ordered = sorted(scanner, key=lambda entry: entry.name.casefold())
            truncated = len(ordered) > self._max_directory_entries
            for entry in ordered[: self._max_directory_entries]:
                try:
                    if entry.is_symlink():
                        kind = "link"
                        size = None
                    elif entry.is_dir(follow_symlinks=False):
                        kind = "directory"
                        size = None
                    elif entry.is_file(follow_symlinks=False):
                        kind = "file"
                        size = entry.stat(follow_symlinks=False).st_size
                    else:
                        kind = "other"
                        size = None
                except OSError:
                    kind = "unavailable"
                    size = None
                item: dict[str, Any] = {"name": entry.name, "kind": kind}
                if size is not None:
                    item["size_bytes"] = size
                entries.append(item)
        except OSError as exc:
            raise WorkspaceReadError("workspace directory cannot be read") from exc
        return {
            "kind": "directory",
            "path": locator,
            "entries": entries,
            "truncated": truncated,
        }

    def _file(
        self,
        path: Path,
        locator: str,
        before: os.stat_result,
        *,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        if offset > before.st_size:
            raise WorkspaceReadError("workspace read offset exceeds file size")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise WorkspaceReadError("workspace file changed while opening")
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                stream.seek(offset)
                content = stream.read(limit)
                after = os.fstat(stream.fileno())
        except WorkspaceReadError:
            raise
        except OSError as exc:
            raise WorkspaceReadError("workspace file cannot be read") from exc
        if (
            (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise WorkspaceReadError("workspace file changed during read")
        next_offset = offset + len(content)
        result: dict[str, Any] = {
            "kind": "file",
            "path": locator,
            "size_bytes": before.st_size,
            "sha256": digest.hexdigest(),
            "offset_bytes": offset,
            "truncated": next_offset < before.st_size,
        }
        if next_offset < before.st_size:
            result["next_offset_bytes"] = next_offset
        try:
            result["content"] = content.decode("utf-8")
            result["encoding"] = "utf-8"
        except UnicodeDecodeError:
            result["content_base64"] = base64.b64encode(content).decode("ascii")
            result["encoding"] = "base64"
        return result


@dataclass(frozen=True, slots=True)
class CapabilityHandlerSet:
    handlers: Mapping[str, ToolHandler]
    installed_pack_ids: frozenset[str]
    disabled_tools: Mapping[str, str]
    sandbox_profile_availability: Mapping[str, Mapping[str, str | None]]


def build_capability_handler_set(
    registry: CapabilityRegistry,
    *,
    workspace_roots: tuple[Path | str, ...],
    trusted_core_handlers: Mapping[str, ToolHandler] | None = None,
    pack_runtime: CapabilityPackRuntime | None = None,
) -> CapabilityHandlerSet:
    """Build one honest availability snapshot from actual executable handlers."""

    handlers: dict[str, ToolHandler] = {"read": WorkspaceReadHandler(workspace_roots)}
    for tool_id, handler in dict(trusted_core_handlers or {}).items():
        try:
            registry.get(tool_id)
        except Exception:
            raise ValueError(f"core handler references unknown tool: {tool_id}") from None
        if tool_id in handlers:
            raise ValueError(f"core handler is duplicated: {tool_id}")
        if tool_id == "shell":
            raise ValueError(
                "shell can only be bound by a verified sandbox capability pack"
            )
        if not callable(handler):
            raise TypeError(f"core handler is not callable: {tool_id}")
        handlers[tool_id] = handler
    installed_packs = frozenset()
    if pack_runtime is not None:
        for tool_id, handler in pack_runtime.handlers.items():
            if tool_id in handlers:
                raise ValueError(f"pack handler shadows a core handler: {tool_id}")
            handlers[tool_id] = handler
        installed_packs = pack_runtime.installed_pack_ids
    disabled = {
        spec.tool_id: "verified_handler_not_installed"
        for spec in registry.all()
        if spec.tool_id not in handlers
    }
    profile_availability: dict[str, Mapping[str, str | None]] = {}
    for tool_id, handler in handlers.items():
        raw = getattr(handler, "sandbox_profile_availability", None)
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            raise TypeError(
                f"sandbox profile availability is invalid for handler: {tool_id}"
            )
        profiles: dict[str, str | None] = {}
        for profile, reason in raw.items():
            if profile not in {"read-only", "workspace-write", "danger-full-access"}:
                raise ValueError(
                    f"handler reported an unknown sandbox profile: {tool_id}"
                )
            if reason is not None and (
                not isinstance(reason, str) or not reason.strip() or len(reason) > 128
            ):
                raise ValueError(
                    f"handler reported an invalid sandbox disabled reason: {tool_id}"
                )
            profiles[str(profile)] = reason
        profile_availability[tool_id] = MappingProxyType(profiles)
    if "shell" in handlers and "shell" not in profile_availability:
        profile_availability["shell"] = MappingProxyType(
            {
                "read-only": "verified_sandbox_boundary_not_bound",
                "workspace-write": "verified_sandbox_boundary_not_bound",
                "danger-full-access": "verified_sandbox_boundary_not_bound",
            }
        )
    return CapabilityHandlerSet(
        handlers=MappingProxyType(handlers),
        installed_pack_ids=installed_packs,
        disabled_tools=MappingProxyType(disabled),
        sandbox_profile_availability=MappingProxyType(profile_availability),
    )
