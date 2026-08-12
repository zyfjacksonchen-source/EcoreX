"""Trusted local capability handlers and availability composition."""

from __future__ import annotations

import base64
import hashlib
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .packs import CapabilityPackRuntime
from .registry import CapabilityRegistry
from .service import ToolHandler
from .service import ToolExecutionScope
from .cow_local_tools import CowLocalTools


WorkspaceRootResolver = Callable[[ToolExecutionScope | None], tuple[Path, ...]]


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
        workspace_root_resolver: WorkspaceRootResolver | None = None,
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
        self._workspace_root_resolver = workspace_root_resolver

    def __call__(self, arguments: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        raw_path = str(arguments["path"])
        offset = int(arguments.get("offset_bytes", 0))
        requested = int(arguments.get("max_bytes", self._default_max_bytes))
        if not 1 <= requested <= self._hard_max_bytes or offset < 0:
            raise WorkspaceReadError("workspace read range is invalid")
        roots = self._roots_for_context(context)
        path, root_index, relative = self._resolve(raw_path, roots)
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

    def _roots_for_context(self, context: Any) -> tuple[Path, ...]:
        if self._workspace_root_resolver is None:
            return self._roots
        additional = self._workspace_root_resolver(
            getattr(context, "execution_scope", None)
        )
        if not isinstance(additional, tuple):
            raise WorkspaceReadError("workspace authority returned invalid roots")
        # A project-scoped Turn treats its backend-authorized project as the
        # working directory.  Static product roots remain available as
        # secondary roots, while a general conversation still receives only
        # the configured static roots.
        return tuple(dict.fromkeys((*additional, *self._roots)))

    def _resolve(self, raw_path: str, roots: tuple[Path, ...]) -> tuple[Path, int, Path]:
        if "\x00" in raw_path or not raw_path.strip():
            raise WorkspaceReadError("workspace path is invalid")
        if raw_path == ".":
            return roots[0], 0, Path()
        locator_root_index: int | None = None
        if raw_path.startswith("workspace://"):
            locator = raw_path.removeprefix("workspace://")
            root_text, separator, suffix = locator.partition("/")
            if (
                not separator
                or not root_text.isascii()
                or not root_text.isdecimal()
                or (locator_root_index := int(root_text)) >= len(roots)
            ):
                raise WorkspaceReadError("workspace locator is invalid")
            if not suffix:
                return roots[locator_root_index], locator_root_index, Path()
            relative = PurePosixPath(suffix)
            if relative.is_absolute() or any(
                part in {"", ".", ".."} for part in relative.parts
            ):
                raise WorkspaceReadError("workspace locator is invalid")
            raw_path = relative.as_posix()
            roots = (roots[locator_root_index],)
        requested = Path(raw_path)
        candidates: list[tuple[Path, int]] = []
        if requested.is_absolute():
            candidates = [(requested, index) for index in range(len(roots))]
        else:
            pure = PurePosixPath(raw_path.replace("\\", "/"))
            if any(part in {"", ".", ".."} for part in pure.parts):
                raise WorkspaceReadError("workspace path traversal is forbidden")
            candidates = [
                (root.joinpath(*pure.parts), index)
                for index, root in enumerate(roots)
            ]
        for candidate, index in candidates:
            root = roots[index]
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
                return (
                    resolved,
                    locator_root_index if locator_root_index is not None else index,
                    resolved.relative_to(root),
                )
        raise WorkspaceReadError("workspace path is outside the authorized roots or missing")

    @staticmethod
    def _locator(root_index: int, relative: Path) -> str:
        suffix = relative.as_posix()
        if suffix == ".":
            suffix = ""
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


def build_capability_handler_set(
    registry: CapabilityRegistry,
    *,
    workspace_roots: tuple[Path | str, ...],
    trusted_core_handlers: Mapping[str, ToolHandler] | None = None,
    pack_runtime: CapabilityPackRuntime | None = None,
    workspace_root_resolver: WorkspaceRootResolver | None = None,
) -> CapabilityHandlerSet:
    """Build one honest availability snapshot from actual executable handlers."""

    local_tools = CowLocalTools(
        workspace_roots,
        workspace_root_resolver=workspace_root_resolver,
    )
    handlers: dict[str, ToolHandler] = local_tools.handlers()
    for tool_id, handler in dict(trusted_core_handlers or {}).items():
        try:
            registry.get(tool_id)
        except Exception:
            raise ValueError(f"core handler references unknown tool: {tool_id}") from None
        if tool_id in handlers:
            raise ValueError(f"core handler is duplicated: {tool_id}")
        if not callable(handler):
            raise TypeError(f"core handler is not callable: {tool_id}")
        handlers[tool_id] = handler
    installed_packs = frozenset()
    if pack_runtime is not None:
        for tool_id, handler in pack_runtime.handlers.items():
            if tool_id == "bash":
                continue
            if tool_id in handlers:
                raise ValueError(f"pack handler shadows a core handler: {tool_id}")
            handlers[tool_id] = handler
            binder = getattr(handler, "bind_workspace_root_resolver", None)
            if callable(binder):
                binder(workspace_root_resolver)
        installed_packs = pack_runtime.installed_pack_ids
    disabled = {
        spec.tool_id: "verified_handler_not_installed"
        for spec in registry.all()
        if spec.tool_id not in handlers
    }
    return CapabilityHandlerSet(
        handlers=MappingProxyType(handlers),
        installed_pack_ids=installed_packs,
        disabled_tools=MappingProxyType(disabled),
    )
