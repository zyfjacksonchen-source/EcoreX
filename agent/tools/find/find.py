"""Find tool - discover files and directories by name or path pattern."""

import fnmatch
import os
from pathlib import Path
from typing import Any, Dict, List

from agent.tools.base_tool import BaseTool, ToolResult
from agent.tools.utils.truncate import DEFAULT_MAX_BYTES, format_size, truncate_head
from common.utils import expand_path


DEFAULT_LIMIT = 200
DEFAULT_MAX_DEPTH = 12
VALID_TYPES = {"any", "file", "dir", "directory"}


class Find(BaseTool):
    """Tool for finding files and directories by pattern."""

    name: str = "find"
    description: str = (
        "Find files or directories by name/path pattern. Use this before read/grep "
        "when you need to locate likely files. Returns relative paths by default "
        f"and stops at {DEFAULT_LIMIT} matches or {format_size(DEFAULT_MAX_BYTES)}."
    )

    params: dict = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob-like pattern to match file or directory names/paths, e.g. '*.py', '*config*', 'docs/**/*.md'.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search. Relative paths are based on workspace directory. Default: current workspace.",
            },
            "type": {
                "type": "string",
                "description": "What to return: any, file, or dir. Default: any.",
            },
            "max_depth": {
                "type": "integer",
                "description": f"Maximum directory depth to traverse from path. Default: {DEFAULT_MAX_DEPTH}.",
            },
            "limit": {
                "type": "integer",
                "description": f"Maximum number of matches to return. Default: {DEFAULT_LIMIT}.",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "Include dotfiles and hidden directories. Default: false.",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.cwd = self.config.get("cwd", os.getcwd())

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        raw_pattern = args.get("pattern") or args.get("name") or ""
        pattern = str(raw_pattern).strip()
        if not pattern:
            return ToolResult.fail("Error: pattern parameter is required")

        search_path = str(args.get("path") or ".").strip() or "."
        absolute_root = self._resolve_path(search_path)
        if not os.path.exists(absolute_root):
            return ToolResult.fail(f"Error: Search path not found: {search_path}")
        if not os.path.isdir(absolute_root):
            return ToolResult.fail(f"Error: Search path is not a directory: {search_path}")

        type_filter = str(args.get("type") or "any").strip().lower()
        if type_filter not in VALID_TYPES:
            return ToolResult.fail("Error: type must be one of: any, file, dir")
        if type_filter == "directory":
            type_filter = "dir"

        max_depth = self._positive_int(args.get("max_depth"), DEFAULT_MAX_DEPTH, minimum=0, maximum=100)
        limit = self._positive_int(args.get("limit"), DEFAULT_LIMIT, minimum=1, maximum=2000)
        include_hidden = bool(args.get("include_hidden", False))

        root_decision = self._authorize_read(absolute_root)
        if not root_decision.get("allowed", True):
            return ToolResult.fail(f"Error: {root_decision.get('reason') or 'Search path blocked by permissions.'}")

        matches: List[str] = []
        scanned = 0
        skipped_denied = 0
        limit_reached = False

        pattern_norm = pattern.replace("\\", "/")
        basename_only = "/" not in pattern_norm

        for current_root, dirs, files in os.walk(absolute_root, topdown=True):
            depth = self._depth(absolute_root, current_root)
            if depth > max_depth:
                dirs[:] = []
                continue

            dirs[:] = sorted(dirs, key=lambda item: item.lower())
            files = sorted(files, key=lambda item: item.lower())

            pruned_dirs = []
            for dirname in dirs:
                full_path = os.path.join(current_root, dirname)
                if not include_hidden and self._is_hidden(dirname):
                    continue
                if depth >= max_depth:
                    continue
                decision = self._authorize_read(full_path)
                if not decision.get("allowed", True):
                    skipped_denied += 1
                    continue
                pruned_dirs.append(dirname)
            dirs[:] = pruned_dirs

            entries = [(name, True) for name in dirs] + [(name, False) for name in files]
            for name, is_dir in entries:
                if len(matches) >= limit:
                    limit_reached = True
                    dirs[:] = []
                    break
                if not include_hidden and self._is_hidden(name):
                    continue
                if type_filter == "file" and is_dir:
                    continue
                if type_filter == "dir" and not is_dir:
                    continue

                full_path = os.path.join(current_root, name)
                decision = self._authorize_read(full_path)
                if not decision.get("allowed", True):
                    skipped_denied += 1
                    continue

                rel_path = os.path.relpath(full_path, self.cwd)
                root_rel_path = os.path.relpath(full_path, absolute_root)
                rel_display = rel_path.replace("\\", "/")
                root_rel_display = root_rel_path.replace("\\", "/")
                candidate = name if basename_only else root_rel_display
                scanned += 1

                if self._matches(pattern_norm, candidate, rel_display, root_rel_display):
                    matches.append(rel_display + ("/" if is_dir else ""))

            if limit_reached:
                break

        if not matches:
            return ToolResult.success({
                "matches": [],
                "output": "(no matches)",
                "scanned": scanned,
                "skipped_denied": skipped_denied,
            })

        output = "\n".join(matches)
        truncation = truncate_head(output, max_lines=999999)
        notices = []
        if limit_reached:
            notices.append(f"{limit} match limit reached")
        if truncation.truncated:
            notices.append(f"{format_size(DEFAULT_MAX_BYTES)} output limit reached")
        if skipped_denied:
            notices.append(f"{skipped_denied} permission-denied entries skipped")
        if notices:
            output = f"{truncation.content}\n\n[{'. '.join(notices)}]"
        else:
            output = truncation.content

        return ToolResult.success({
            "matches": matches,
            "output": output,
            "count": len(matches),
            "scanned": scanned,
            "skipped_denied": skipped_denied,
            "details": {"truncation": truncation.to_dict()} if truncation.truncated else None,
        })

    def _resolve_path(self, path: str) -> str:
        path = expand_path(path)
        if os.path.isabs(path):
            return os.path.realpath(path)
        return os.path.realpath(os.path.join(self.cwd, path))

    def _authorize_read(self, path: str) -> Dict[str, Any]:
        try:
            from common.ecorex_tool_permissions import get_tool_permission_broker

            return get_tool_permission_broker().authorize_file_access("read", path, cwd=self.cwd)
        except Exception as exc:
            return {
                "allowed": False,
                "reason": f"Permission broker unavailable; local find blocked. {exc}",
            }

    @staticmethod
    def _positive_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _depth(root: str, path: str) -> int:
        rel = os.path.relpath(path, root)
        if rel in {".", ""}:
            return 0
        return len(Path(rel).parts)

    @staticmethod
    def _is_hidden(name: str) -> bool:
        return name.startswith(".")

    @staticmethod
    def _matches(pattern: str, name_candidate: str, rel_display: str, root_rel_display: str) -> bool:
        candidates = {
            name_candidate,
            rel_display,
            root_rel_display,
            os.path.basename(root_rel_display),
        }
        return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates)
