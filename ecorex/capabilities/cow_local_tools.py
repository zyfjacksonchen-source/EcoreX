"""CowAgent-style local file and terminal tools.

These handlers intentionally use the signed-in desktop process' ordinary OS
authority.  They do not add an e-Mate permission profile, approval workflow,
or secondary sandbox.  Relative paths use the current project/workspace and
absolute paths behave exactly like CowAgent: the operating system is the
authority.  Account credential storage remains unreadable, matching Cow's
credential-file exception.
"""

from __future__ import annotations

from collections import deque
import fnmatch
import mimetypes
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import uuid
from typing import Any, Mapping

from .service import ToolExecutionScope


_MAX_OUTPUT_BYTES = 64 * 1024
_SKIP_DIRECTORIES = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
)


class CowLocalToolError(RuntimeError):
    code = "cow_local_tool_failed"


class _BackgroundJob:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.output: deque[str] = deque()
        self.output_bytes = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self.process.stdout is not None
        for chunk in iter(self.process.stdout.readline, ""):
            with self.lock:
                self.output.append(chunk)
                self.output_bytes += len(chunk.encode("utf-8", errors="replace"))
                while self.output and self.output_bytes > _MAX_OUTPUT_BYTES:
                    removed = self.output.popleft()
                    self.output_bytes -= len(removed.encode("utf-8", errors="replace"))

    def read(self) -> dict[str, Any]:
        with self.lock:
            text = "".join(self.output)
            self.output.clear()
            self.output_bytes = 0
        return {
            "output": text or "(no new output)",
            "running": self.process.poll() is None,
            "exit_code": self.process.poll(),
        }


class CowLocalTools:
    """One process-wide Cow-style local tool set."""

    def __init__(self, roots, *, workspace_root_resolver=None) -> None:
        self.roots = tuple(Path(root).expanduser().resolve() for root in roots)
        if not self.roots:
            raise ValueError("Cow local tools require a workspace root")
        self.workspace_root_resolver = workspace_root_resolver
        self._jobs: dict[str, _BackgroundJob] = {}
        self._jobs_lock = threading.Lock()

    def handlers(self) -> dict[str, Any]:
        return {
            "read": self.read,
            "write": self.write,
            "edit": self.edit,
            "ls": self.ls,
            "search_files": self.search_files,
            "bash": self.shell,
            "memory_search": self.memory_search,
            "memory_get": self.memory_get,
        }

    def _cwd(self, context: Any = None) -> Path:
        if self.workspace_root_resolver is not None:
            scope: ToolExecutionScope | None = getattr(context, "execution_scope", None)
            resolved = self.workspace_root_resolver(scope)
            if resolved:
                return Path(resolved[0]).expanduser().resolve()
        return self.roots[0]

    def _path(self, value: Any, context: Any = None, *, default: str = ".") -> Path:
        raw = str(value if value not in (None, "") else default).strip()
        if not raw or "\x00" in raw:
            raise CowLocalToolError("path is invalid")
        expanded = Path(raw).expanduser()
        return expanded if expanded.is_absolute() else self._cwd(context) / expanded

    @staticmethod
    def _credential_path(path: Path) -> bool:
        parts = tuple(part.casefold() for part in path.expanduser().absolute().parts)
        if ".emate" not in parts:
            return False
        name = path.name.casefold()
        return any(token in name for token in ("credential", "vault", "secret", "token"))

    def read(self, arguments: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        path = self._path(arguments.get("path"), context)
        if self._credential_path(path):
            raise CowLocalToolError("account credentials are not readable")
        if path.is_dir():
            return self.ls({"path": str(path)}, context)
        if not path.is_file():
            raise CowLocalToolError(f"file not found: {arguments.get('path')}")
        offset = int(arguments.get("offset", arguments.get("offset_bytes", 0)))
        limit = int(arguments.get("limit", arguments.get("max_bytes", 64 * 1024)))
        if offset < 0 or not 1 <= limit <= 1024 * 1024:
            raise CowLocalToolError("read range is invalid")
        content = path.read_bytes()
        selected = content[offset : offset + limit]
        try:
            text = selected.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = ""
            encoding = "binary"
        return {
            "path": str(path),
            "content": text,
            "encoding": encoding,
            "size_bytes": len(content),
            "offset": offset,
            "next_offset": min(len(content), offset + len(selected)),
            "eof": offset + len(selected) >= len(content),
        }

    def write(self, arguments: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        path = self._path(arguments.get("path"), context)
        if self._credential_path(path):
            raise CowLocalToolError("account credentials are not writable")
        content = str(arguments.get("content", ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"path": str(path), "bytes_written": len(content.encode("utf-8"))}

    def edit(self, arguments: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        path = self._path(arguments.get("path"), context)
        if self._credential_path(path):
            raise CowLocalToolError("account credentials are not editable")
        old = str(arguments.get("oldText", ""))
        new = str(arguments.get("newText", ""))
        if not path.is_file():
            raise CowLocalToolError(f"file not found: {arguments.get('path')}")
        content = path.read_text(encoding="utf-8")
        if not old:
            updated, replacements = content + new, 1
        else:
            matches = content.count(old)
            if matches == 0:
                raise CowLocalToolError("oldText was not found")
            if matches > 1 and not bool(arguments.get("replaceAll", False)):
                raise CowLocalToolError("oldText is not unique; use replaceAll")
            replacements = matches if bool(arguments.get("replaceAll", False)) else 1
            updated = content.replace(old, new, -1 if replacements == matches else 1)
        path.write_text(updated, encoding="utf-8")
        return {"path": str(path), "replacements": replacements}

    def ls(self, arguments: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        path = self._path(arguments.get("path"), context)
        if self._credential_path(path):
            raise CowLocalToolError("account credentials are not listable")
        if not path.is_dir():
            raise CowLocalToolError(f"directory not found: {arguments.get('path', '.')}")
        limit = int(arguments.get("limit", 500))
        entries = []
        with os.scandir(path) as scanner:
            for entry in sorted(scanner, key=lambda item: item.name.casefold())[:limit]:
                entries.append(entry.name + ("/" if entry.is_dir(follow_symlinks=False) else ""))
        return {"path": str(path), "entries": entries, "entry_count": len(entries)}

    def search_files(self, arguments: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        pattern = str(arguments.get("pattern", ""))
        if not pattern:
            raise CowLocalToolError("pattern is required")
        root = self._path(arguments.get("path"), context)
        target = str(arguments.get("target", "content"))
        mode = str(arguments.get("output_mode", "content"))
        file_glob = str(arguments.get("file_glob", "*"))
        ignore_case = bool(arguments.get("ignore_case", False))
        no_ignore = bool(arguments.get("no_ignore", False))
        maximum = int(arguments.get("max_results", 50))
        if target == "content":
            try:
                expression = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
            except re.error as error:
                raise CowLocalToolError(f"invalid regex: {error}") from error
        results: list[dict[str, Any]] = []
        paths = (root,) if root.is_file() else self._walk(root, no_ignore=no_ignore)
        for path in paths:
            if len(results) >= maximum:
                break
            if self._credential_path(path):
                continue
            if not path.is_file() or not fnmatch.fnmatch(path.name, file_glob):
                continue
            relative = str(path.relative_to(root)) if root.is_dir() else path.name
            if target == "files":
                matcher = pattern if any(char in pattern for char in "*?[") else f"*{pattern}*"
                name = path.name.casefold() if ignore_case else path.name
                wanted = matcher.casefold() if ignore_case else matcher
                if fnmatch.fnmatchcase(name, wanted):
                    results.append({"file": relative})
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            matched = [
                {"file": relative, "line": number, "text": line[:2000]}
                for number, line in enumerate(lines, 1)
                if expression.search(line)
            ]
            if mode == "files" and matched:
                results.append({"file": relative})
            elif mode == "count" and matched:
                results.append({"file": relative, "count": len(matched)})
            else:
                results.extend(matched[: maximum - len(results)])
        return {"results": results[:maximum], "match_count": min(len(results), maximum)}

    @staticmethod
    def _memory_files(root: Path) -> tuple[Path, ...]:
        candidates = [root / "MEMORY.md"]
        for directory in (root / "memory", root / "knowledge"):
            if directory.is_dir():
                candidates.extend(directory.rglob("*.md"))
        return tuple(
            path
            for path in candidates
            if path.is_file()
            and not path.is_symlink()
            and path.stat().st_size <= 10 * 1024 * 1024
        )

    def memory_search(
        self, arguments: Mapping[str, Any], context: Any = None
    ) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise CowLocalToolError("query is required")
        maximum = int(arguments.get("max_results", 10))
        if not 1 <= maximum <= 50:
            raise CowLocalToolError("max_results must be between 1 and 50")
        root = self._cwd(context)
        folded_query = query.casefold()
        terms = tuple(
            dict.fromkeys(
                (folded_query, *re.findall(r"[\w\u3400-\u9fff]+", folded_query))
            )
        )
        results: list[dict[str, Any]] = []
        for path in self._memory_files(root):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(lines, 1):
                folded = line.casefold()
                hits = sum(folded.count(term) for term in terms if term)
                if not hits:
                    continue
                start = max(1, number - 1)
                end = min(len(lines), number + 1)
                results.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "start_line": start,
                        "end_line": end,
                        "score": hits,
                        "snippet": "\n".join(lines[start - 1 : end])[:4000],
                    }
                )
        results.sort(
            key=lambda item: (
                -int(item["score"]),
                str(item["path"]),
                int(item["start_line"]),
            )
        )
        return {
            "query": query,
            "results": results[:maximum],
            "match_count": min(len(results), maximum),
            "message": None
            if results
            else "No matching memory or knowledge was found.",
        }

    def memory_get(
        self, arguments: Mapping[str, Any], context: Any = None
    ) -> dict[str, Any]:
        raw = str(arguments.get("path", "")).replace("\\", "/").strip()
        if not raw:
            raise CowLocalToolError("path is required")
        if raw != "MEMORY.md" and not raw.startswith(("memory/", "knowledge/")):
            raw = f"memory/{raw}"
        root = self._cwd(context).resolve()
        path = (root / raw).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise CowLocalToolError("memory path is outside the workspace") from None
        if not path.is_file() or path.is_symlink():
            raise CowLocalToolError(f"memory file not found: {raw}")
        lines = path.read_text(encoding="utf-8").splitlines()
        start = max(1, int(arguments.get("start_line", 1)))
        count = arguments.get("num_lines")
        if count is None:
            end = len(lines)
        else:
            count = int(count)
            if not 1 <= count <= 5000:
                raise CowLocalToolError("num_lines must be between 1 and 5000")
            end = min(len(lines), start + count - 1)
        selected = lines[start - 1 : end]
        return {
            "path": path.relative_to(root).as_posix(),
            "start_line": start,
            "end_line": start + len(selected) - 1,
            "total_lines": len(lines),
            "content": "\n".join(selected),
        }

    def _walk(self, root: Path, *, no_ignore: bool):
        if not root.is_dir():
            return ()
        found: list[Path] = []
        for directory, names, files in os.walk(root):
            if not no_ignore:
                names[:] = [name for name in names if name not in _SKIP_DIRECTORIES]
            for name in files:
                found.append(Path(directory) / name)
        return tuple(found)

    def shell(self, arguments: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        job_id = str(arguments.get("bash_id", "")).strip()
        if job_id:
            return self._background(job_id, kill=bool(arguments.get("kill", False)))
        command = str(arguments.get("command", "")).strip()
        if not command:
            raise CowLocalToolError("command is required")
        warning = self._catastrophic(command)
        if warning:
            raise CowLocalToolError(warning)
        cwd = self._path(arguments.get("cwd"), context)
        if not cwd.is_dir():
            raise CowLocalToolError("working directory does not exist")
        timeout = int(arguments.get("timeout", arguments.get("timeout_seconds", 120)))
        if not 1 <= timeout <= 600:
            raise CowLocalToolError("timeout must be between 1 and 600 seconds")
        process = self._popen(command, cwd)
        if bool(arguments.get("run_in_background", False)):
            identity = uuid.uuid4().hex[:12]
            with self._jobs_lock:
                self._jobs[identity] = _BackgroundJob(process)
            return {"bash_id": identity, "output": f"Started in background ({identity})"}
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate(process)
            raise CowLocalToolError(f"command timed out after {timeout} seconds") from None
        encoded = stdout.encode("utf-8", errors="replace")
        if len(encoded) > _MAX_OUTPUT_BYTES:
            stdout = encoded[-_MAX_OUTPUT_BYTES:].decode("utf-8", errors="replace")
        return {"output": stdout or "(no output)", "exit_code": process.returncode}

    @staticmethod
    def _popen(command: str, cwd: Path) -> subprocess.Popen[str]:
        kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": os.environ.copy(),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(command, shell=True, **kwargs)

    def _background(self, job_id: str, *, kill: bool) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise CowLocalToolError("unknown background command")
        if kill and job.process.poll() is None:
            self._terminate(job.process)
        result = job.read()
        result["bash_id"] = job_id
        if not result["running"]:
            with self._jobs_lock:
                self._jobs.pop(job_id, None)
        return result

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if sys.platform == "win32":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass

    @staticmethod
    def _catastrophic(command: str) -> str:
        lowered = command.casefold()
        tokens = lowered.split()
        for index, token in enumerate(tokens):
            if token != "rm":
                continue
            recursive_force = False
            for argument in tokens[index + 1 :]:
                if argument.startswith("-") and "r" in argument and "f" in argument:
                    recursive_force = True
                elif argument in {"/", "/*"} and recursive_force:
                    return "This command will delete the entire filesystem"
                elif not argument.startswith("-"):
                    break
        if "dd " in lowered and "if=/dev/zero" in lowered:
            return "This command can destroy disk data"
        if re.search(r"\b(shutdown|reboot|halt|poweroff)\b", lowered):
            return "This command will shut down or restart the system"
        return ""


class CowSendTool:
    """Publish CowAgent's local-file send result through e-Mate Artifacts."""

    def __init__(self, artifact_service: Any, *, account_id: str) -> None:
        self.artifact_service = artifact_service
        self.account_id = account_id
        self.declaration = artifact_service.issue_trusted_deliverable_declaration(
            "send"
        )

    def __call__(self, arguments: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        from ecorex.artifacts import ArtifactScope

        raw = str(arguments.get("path") or "").strip()
        if not raw or raw.casefold().startswith(("http://", "https://")):
            raise CowLocalToolError("send requires a local file path")
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise CowLocalToolError(f"file not found: {raw}")
        scope = getattr(context, "execution_scope", None)
        artifact = self.artifact_service.create_artifact(
            path.read_bytes(),
            requested_name=path.name,
            mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            declaration=self.declaration,
            scope=ArtifactScope(
                account_id=self.account_id,
                thread_id=getattr(scope, "thread_id", None),
                turn_id=getattr(scope, "turn_id", None),
                created_by_tool_id="send",
            ),
        )
        return {
            "type": "file_to_send",
            "artifact_id": artifact.artifact_id,
            "revision_id": artifact.revision_id,
            "file_name": artifact.display_name,
            "mime_type": artifact.mime_type,
            "size": artifact.size_bytes,
            "message": str(arguments.get("message") or f"正在发送 {artifact.display_name}"),
        }


__all__ = ["CowLocalToolError", "CowLocalTools", "CowSendTool"]
