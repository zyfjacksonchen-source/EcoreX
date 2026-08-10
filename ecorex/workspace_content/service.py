"""Product-owned knowledge workspace projection.

Knowledge documents have one authority: ``<workspace>/knowledge``.  This
service deliberately does not index or copy them into Runtime storage.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from typing import Any, Callable, Iterable, Iterator
import unicodedata
from urllib.parse import unquote, urlsplit
import uuid

from ecorex.runtime.database import SQLiteDatabase, json_dumps

from .paths import MAX_DEPTH, MAX_DOCUMENT_BYTES, normalize_knowledge_path


MAX_IMPORT_FILES = 100
MAX_IMPORT_BYTES = 200 * 1024 * 1024
_MAX_TREE_ENTRIES = 10_000
_MAX_GRAPH_NODES = 5_000
_MAX_GRAPH_EDGES = 20_000
_DOCUMENT_SUFFIXES = frozenset({".md", ".txt"})
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_WIKI_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{7,255}$")
_REQUEST_CONFLICT = "knowledge request ID was reused with different intent"


class WorkspaceContentError(RuntimeError):
    """Base class for safe product-facing workspace failures."""


class WorkspaceContentRejected(WorkspaceContentError):
    """The requested path or content is outside the public contract."""


class WorkspaceContentNotFound(WorkspaceContentError):
    """The requested category or document does not exist."""


class WorkspaceContentConflict(WorkspaceContentError):
    """A no-overwrite mutation conflicts with existing content."""


class WorkspaceContentUnavailable(WorkspaceContentError):
    """The trusted knowledge root or content is unavailable."""


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class WorkspaceContentService:
    """Bounded filesystem API for user-managed Markdown/text knowledge."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        database: SQLiteDatabase | str | Path | None = None,
        create_root: bool = True,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().absolute()
        self.root = self.workspace_root / "knowledge"
        self.database = (
            database
            if isinstance(database, SQLiteDatabase) or database is None
            else SQLiteDatabase(database)
        )
        self.create_root = create_root
        if create_root:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            self.root.mkdir(exist_ok=True)
        self._verify_directory(self.workspace_root, "workspace")
        if self.root.exists():
            self._verify_directory(self.root, "knowledge")
        if create_root and self.database is not None:
            self._initialize_requests()

    @staticmethod
    def _verify_directory(path: Path, label: str) -> None:
        try:
            info = path.lstat()
        except OSError as error:
            raise WorkspaceContentUnavailable(f"{label} directory is unavailable") from error
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise WorkspaceContentUnavailable(f"{label} directory is not trusted")

    @staticmethod
    def _relative(value: str, *, allow_root: bool = False) -> PurePosixPath:
        try:
            return normalize_knowledge_path(value, allow_root=allow_root)
        except ValueError as error:
            raise WorkspaceContentRejected(str(error)) from error

    def _require_root(self, *, write: bool = False) -> None:
        if not self.root.exists():
            if write and self.create_root:
                self.root.mkdir(exist_ok=False)
            else:
                raise WorkspaceContentUnavailable("knowledge directory is unavailable")
        self._verify_directory(self.root, "knowledge")

    def _initialize_requests(self) -> None:
        if self.database is None:
            return
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS knowledge_mutation_requests ("
                    "client_request_id TEXT PRIMARY KEY,operation TEXT NOT NULL,"
                    "request_sha256 TEXT NOT NULL,status TEXT NOT NULL "
                    "CHECK(status IN ('pending','completed')),plan_json TEXT NOT NULL,"
                    "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TRIGGER IF NOT EXISTS knowledge_requests_identity_immutable "
                    "BEFORE UPDATE OF client_request_id,operation,request_sha256,plan_json,created_at "
                    "ON knowledge_mutation_requests BEGIN "
                    "SELECT RAISE(ABORT,'knowledge request identity is immutable'); END"
                )
        except sqlite3.Error as error:
            raise WorkspaceContentUnavailable("knowledge request authority is unavailable") from error

    @staticmethod
    def _request_fingerprint(operation: str, payload: Any) -> str:
        encoded = json.dumps(
            {"operation": operation, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _request_identity(self, request_id: str) -> str:
        value = str(request_id or "").strip()
        if not _REQUEST_ID.fullmatch(value):
            raise WorkspaceContentRejected("knowledge client_request_id is invalid")
        if self.database is None:
            raise WorkspaceContentUnavailable("knowledge request authority is unavailable")
        return value

    def _idempotent_plan(
        self,
        *,
        operation: str,
        request_id: str | None,
        payload: Any,
        make_plan: Callable[[], Any],
    ) -> tuple[Any, str | None, str]:
        if request_id is None:
            return make_plan(), None, "pending"
        identity = self._request_identity(request_id)
        fingerprint = self._request_fingerprint(operation, payload)
        assert self.database is not None
        try:
            with self.database.transaction() as connection:
                existing = connection.execute(
                    "SELECT operation,request_sha256,status,plan_json "
                    "FROM knowledge_mutation_requests WHERE client_request_id=?",
                    (identity,),
                ).fetchone()
                if existing is None:
                    plan = make_plan()
                    timestamp = datetime.now(UTC).isoformat(timespec="microseconds")
                    connection.execute(
                        "INSERT INTO knowledge_mutation_requests("
                        "client_request_id,operation,request_sha256,status,plan_json,created_at,updated_at) "
                        "VALUES(?,?,?,'pending',?,?,?)",
                        (identity, operation, fingerprint, json_dumps(plan), timestamp, timestamp),
                    )
                    return plan, identity, "pending"
                if existing["operation"] != operation or existing["request_sha256"] != fingerprint:
                    raise WorkspaceContentConflict(_REQUEST_CONFLICT)
                try:
                    plan = json.loads(str(existing["plan_json"]))
                except json.JSONDecodeError as error:
                    raise WorkspaceContentUnavailable(
                        "knowledge request authority is invalid"
                    ) from error
                if existing["status"] not in {"pending", "completed"}:
                    raise WorkspaceContentUnavailable("knowledge request authority is invalid")
                return plan, identity, str(existing["status"])
        except WorkspaceContentError:
            raise
        except sqlite3.Error as error:
            raise WorkspaceContentUnavailable("knowledge request authority is unavailable") from error

    def _complete_request(self, identity: str | None, operation: str, payload: Any, plan: Any) -> None:
        if identity is None:
            return
        assert self.database is not None
        fingerprint = self._request_fingerprint(operation, payload)
        try:
            with self.database.transaction() as connection:
                row = connection.execute(
                    "SELECT operation,request_sha256,status,plan_json "
                    "FROM knowledge_mutation_requests WHERE client_request_id=?",
                    (identity,),
                ).fetchone()
                if row is None:
                    raise WorkspaceContentUnavailable("knowledge request authority is missing")
                if (
                    row["operation"] != operation
                    or row["request_sha256"] != fingerprint
                    or str(row["plan_json"]) != json_dumps(plan)
                ):
                    raise WorkspaceContentConflict(_REQUEST_CONFLICT)
                if row["status"] == "pending":
                    connection.execute(
                        "UPDATE knowledge_mutation_requests SET status='completed',updated_at=? "
                        "WHERE client_request_id=? AND status='pending'",
                        (datetime.now(UTC).isoformat(timespec="microseconds"), identity),
                    )
        except WorkspaceContentError:
            raise
        except sqlite3.Error as error:
            raise WorkspaceContentUnavailable("knowledge request authority is unavailable") from error

    @contextmanager
    def _directory_lease(
        self,
        relative: PurePosixPath,
    ) -> Iterator[tuple[int | None, Path]]:
        self._require_root()
        path = self.root.joinpath(*relative.parts)
        if os.name == "nt":
            try:
                before = path.lstat()
                self._verify_directory(path, "knowledge category")
            except WorkspaceContentError:
                raise
            except OSError as error:
                raise WorkspaceContentUnavailable(
                    "knowledge category is unavailable"
                ) from error
            identity = (int(before.st_dev), int(before.st_ino), int(before.st_ctime_ns))
            try:
                yield None, path
                after = path.lstat()
            except WorkspaceContentError:
                raise
            except OSError as error:
                raise WorkspaceContentUnavailable(
                    "knowledge category changed during access"
                ) from error
            if (
                identity
                != (int(after.st_dev), int(after.st_ino), int(after.st_ctime_ns))
                or stat.S_ISLNK(after.st_mode)
                or _is_reparse(after)
            ):
                raise WorkspaceContentUnavailable("knowledge category changed during access")
            return

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptors: list[int] = []
        try:
            root_before = self.root.lstat()
            descriptor = os.open(self.root, flags)
            descriptors.append(descriptor)
            root_opened = os.fstat(descriptor)
            root_after = self.root.lstat()
            root_identity = (int(root_opened.st_dev), int(root_opened.st_ino))
            if {
                (int(root_before.st_dev), int(root_before.st_ino)),
                root_identity,
                (int(root_after.st_dev), int(root_after.st_ino)),
            } != {root_identity}:
                raise WorkspaceContentUnavailable("knowledge root changed during access")
            for part in relative.parts:
                descriptor = os.open(part, flags, dir_fd=descriptor)
                descriptors.append(descriptor)
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                raise WorkspaceContentUnavailable("knowledge category is not a directory")
        except WorkspaceContentError:
            for opened_descriptor in reversed(descriptors):
                try:
                    os.close(opened_descriptor)
                except OSError:
                    pass
            raise
        except OSError as error:
            for opened_descriptor in reversed(descriptors):
                try:
                    os.close(opened_descriptor)
                except OSError:
                    pass
            raise WorkspaceContentUnavailable("knowledge category is unavailable") from error
        try:
            yield descriptor, path
        except BaseException:
            raise
        else:
            try:
                observed = path.lstat()
                root_observed = self.root.lstat()
            except OSError as error:
                raise WorkspaceContentUnavailable(
                    "knowledge category changed during access"
                ) from error
            if (
                (int(observed.st_dev), int(observed.st_ino))
                != (int(opened.st_dev), int(opened.st_ino))
                or stat.S_ISLNK(observed.st_mode)
                or _is_reparse(observed)
                or (int(root_observed.st_dev), int(root_observed.st_ino)) != root_identity
            ):
                raise WorkspaceContentUnavailable("knowledge category changed during access")
        finally:
            for opened_descriptor in reversed(descriptors):
                try:
                    os.close(opened_descriptor)
                except OSError:
                    pass

    def _entry_stat(self, relative: PurePosixPath) -> os.stat_result:
        if not relative.parts:
            with self._directory_lease(relative) as (directory_fd, _directory):
                return os.fstat(directory_fd) if directory_fd is not None else self.root.lstat()
        parent = PurePosixPath(*relative.parts[:-1])
        try:
            with self._directory_lease(parent) as (directory_fd, directory):
                return (
                    os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
                    if directory_fd is not None
                    else os.lstat(directory / relative.name)
                )
        except FileNotFoundError as error:
            raise WorkspaceContentNotFound("knowledge path does not exist") from error
        except WorkspaceContentError:
            raise
        except OSError as error:
            raise WorkspaceContentUnavailable("knowledge path is unavailable") from error

    def _entry_exists(self, relative: PurePosixPath) -> bool:
        try:
            self._entry_stat(relative)
            return True
        except WorkspaceContentNotFound:
            return False

    def _create_directory(self, relative: PurePosixPath) -> os.stat_result:
        parent = PurePosixPath(*relative.parts[:-1])
        try:
            with self._directory_lease(parent) as (directory_fd, directory):
                if directory_fd is not None:
                    os.mkdir(relative.name, mode=0o700, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                    info = os.stat(
                        relative.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                else:
                    (directory / relative.name).mkdir(mode=0o700)
                    _fsync_directory(directory)
                    info = os.lstat(directory / relative.name)
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                    raise WorkspaceContentUnavailable(
                        "knowledge category was not created safely"
                    )
                return info
        except FileExistsError as error:
            raise WorkspaceContentConflict("knowledge category already exists") from error
        except WorkspaceContentError:
            raise
        except OSError as error:
            raise WorkspaceContentUnavailable(
                "knowledge category could not be created"
            ) from error

    def _path(self, relative: PurePosixPath, *, leaf_may_be_missing: bool = False) -> Path:
        self._require_root(write=leaf_may_be_missing)
        candidate = self.root.joinpath(*relative.parts)
        current = self.root
        for index, part in enumerate(relative.parts):
            current /= part
            if leaf_may_be_missing and index == len(relative.parts) - 1 and not current.exists():
                break
            try:
                info = current.lstat()
            except FileNotFoundError as error:
                raise WorkspaceContentNotFound("knowledge path does not exist") from error
            except OSError as error:
                raise WorkspaceContentUnavailable("knowledge path is unavailable") from error
            if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise WorkspaceContentRejected("knowledge path crosses an unsafe link")
        try:
            root = self.root.resolve(strict=True)
            resolved = candidate.resolve(strict=not leaf_may_be_missing)
        except FileNotFoundError as error:
            raise WorkspaceContentNotFound("knowledge path does not exist") from error
        except OSError as error:
            raise WorkspaceContentUnavailable("knowledge path is unavailable") from error
        if not resolved.is_relative_to(root):
            raise WorkspaceContentRejected("knowledge path escapes its root")
        return candidate

    @staticmethod
    def _document_name(path: PurePosixPath) -> None:
        if path.suffix.casefold() not in _DOCUMENT_SUFFIXES:
            raise WorkspaceContentRejected("knowledge documents must be Markdown or text")

    @staticmethod
    def _decode(content: bytes) -> str:
        if len(content) > MAX_DOCUMENT_BYTES:
            raise WorkspaceContentRejected("knowledge document exceeds 10 MiB")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkspaceContentRejected("knowledge document must be UTF-8") from error
        if "\x00" in text:
            raise WorkspaceContentRejected("knowledge document contains NUL bytes")
        return text

    def _read_file(self, path: Path) -> tuple[str, int, str]:
        try:
            relative = PurePosixPath(path.relative_to(self.root).as_posix())
        except ValueError as error:
            raise WorkspaceContentRejected("knowledge document escaped its root") from error
        parent = PurePosixPath(*relative.parts[:-1])
        try:
            with self._directory_lease(parent) as (directory_fd, directory):
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = (
                    os.open(relative.name, flags, dir_fd=directory_fd)
                    if directory_fd is not None
                    else os.open(directory / relative.name, flags)
                )
                with os.fdopen(descriptor, "rb", closefd=True) as handle:
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
                        raise WorkspaceContentRejected(
                            "knowledge document is not a safe regular file"
                        )
                    if info.st_size > MAX_DOCUMENT_BYTES:
                        raise WorkspaceContentRejected("knowledge document exceeds 10 MiB")
                    content = handle.read(MAX_DOCUMENT_BYTES + 1)
                after = (
                    os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
                    if directory_fd is not None
                    else os.lstat(directory / relative.name)
                )
                if (
                    (int(after.st_dev), int(after.st_ino))
                    != (int(info.st_dev), int(info.st_ino))
                    or stat.S_ISLNK(after.st_mode)
                    or _is_reparse(after)
                ):
                    raise WorkspaceContentUnavailable(
                        "knowledge document changed during access"
                    )
        except WorkspaceContentError:
            raise
        except FileNotFoundError as error:
            raise WorkspaceContentNotFound("knowledge document does not exist") from error
        except OSError as error:
            raise WorkspaceContentUnavailable("knowledge document is unavailable") from error
        text = self._decode(content)
        return text, len(content), datetime.fromtimestamp(info.st_mtime, UTC).isoformat()

    def _scan(
        self,
        directory: Path,
        relative: PurePosixPath,
        query: str | None,
        counter: list[int],
        read_budget: list[int],
    ) -> list[dict[str, Any]]:
        try:
            with self._directory_lease(relative) as (directory_fd, leased_directory):
                entries = sorted(
                    (
                        (entry.name, entry.stat(follow_symlinks=False), entry.is_symlink())
                        for entry in os.scandir(
                            directory_fd if directory_fd is not None else leased_directory
                        )
                    ),
                    key=lambda item: item[0].casefold(),
                )
        except OSError as error:
            raise WorkspaceContentUnavailable("knowledge directory is unavailable") from error
        nodes: list[dict[str, Any]] = []
        for entry_name, info, is_symlink in entries:
            counter[0] += 1
            if counter[0] > _MAX_TREE_ENTRIES:
                raise WorkspaceContentRejected("knowledge tree contains too many entries")
            if is_symlink or _is_reparse(info):
                raise WorkspaceContentRejected("knowledge tree contains an unsafe link")
            child_relative = relative / entry_name
            try:
                portable_child = self._relative(child_relative.as_posix())
            except WorkspaceContentRejected as error:
                raise WorkspaceContentRejected(
                    "knowledge tree contains a non-portable path"
                ) from error
            if portable_child != child_relative:
                raise WorkspaceContentRejected(
                    "knowledge tree contains a non-portable path"
                )
            child_path = self.root.joinpath(*child_relative.parts)
            if stat.S_ISDIR(info.st_mode):
                if len(child_relative.parts) > MAX_DEPTH:
                    raise WorkspaceContentRejected("knowledge tree is too deep")
                children = self._scan(
                    child_path,
                    child_relative,
                    query,
                    counter,
                    read_budget,
                )
                if query is None or children or query in child_relative.as_posix().casefold():
                    nodes.append(
                        {
                            "path": child_relative.as_posix(),
                            "name": entry_name,
                            "kind": "category",
                            "size_bytes": 0,
                            "updated_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
                            "children": children,
                        }
                    )
                continue
            if not stat.S_ISREG(info.st_mode) or child_relative.suffix.casefold() not in _DOCUMENT_SUFFIXES:
                continue
            matches = query is None or query in child_relative.as_posix().casefold()
            if query is not None and not matches:
                read_budget[0] += int(info.st_size)
                if read_budget[0] > MAX_IMPORT_BYTES:
                    raise WorkspaceContentRejected(
                        "knowledge search exceeds its 200 MiB scan limit"
                    )
                text, _, _ = self._read_file(child_path)
                matches = query in text.casefold()
            if matches:
                nodes.append(
                    {
                        "path": child_relative.as_posix(),
                        "name": entry_name,
                        "kind": "document",
                        "size_bytes": info.st_size,
                        "updated_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
                        "children": [],
                    }
                )
        return nodes

    def tree(self, query: str | None = None) -> dict[str, Any]:
        self._require_root()
        normalized_query = str(query or "").strip().casefold() or None
        if normalized_query is not None and len(normalized_query) > 256:
            raise WorkspaceContentRejected("knowledge search is too long")
        return {
            "root": "knowledge",
            "query": normalized_query,
            "items": self._scan(
                self.root,
                PurePosixPath(),
                normalized_query,
                [0],
                [0],
            ),
        }

    def _document_paths(self) -> dict[str, Path]:
        result: dict[str, Path] = {}
        stack = [PurePosixPath()]
        count = 0
        while stack:
            relative = stack.pop()
            try:
                with self._directory_lease(relative) as (directory_fd, directory):
                    entries = [
                        (entry.name, entry.stat(follow_symlinks=False), entry.is_symlink())
                        for entry in os.scandir(
                            directory_fd if directory_fd is not None else directory
                        )
                    ]
            except OSError as error:
                raise WorkspaceContentUnavailable("knowledge directory is unavailable") from error
            for entry_name, info, is_symlink in entries:
                count += 1
                if count > _MAX_TREE_ENTRIES:
                    raise WorkspaceContentRejected("knowledge tree contains too many entries")
                if is_symlink or _is_reparse(info):
                    raise WorkspaceContentRejected("knowledge tree contains an unsafe link")
                child = relative / entry_name
                try:
                    portable_child = self._relative(child.as_posix())
                except WorkspaceContentRejected as error:
                    raise WorkspaceContentRejected(
                        "knowledge tree contains a non-portable path"
                    ) from error
                if portable_child != child:
                    raise WorkspaceContentRejected(
                        "knowledge tree contains a non-portable path"
                    )
                if stat.S_ISDIR(info.st_mode):
                    if len(child.parts) > MAX_DEPTH:
                        raise WorkspaceContentRejected("knowledge tree is too deep")
                    stack.append(child)
                elif stat.S_ISREG(info.st_mode) and child.suffix.casefold() in _DOCUMENT_SUFFIXES:
                    result[child.as_posix()] = self.root.joinpath(*child.parts)
        return result

    @staticmethod
    def _link_target(source: str, raw: str, documents: set[str]) -> str | None:
        value = raw.strip().strip("<>")
        if re.search(r"%(?![0-9A-Fa-f]{2})", value):
            return None
        parsed = urlsplit(unquote(value))
        if parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
            return None
        candidate = parsed.path.lstrip("/")
        if not candidate:
            return None
        base = PurePosixPath(source).parent
        parts: list[str] = []
        for part in (base / PurePosixPath(candidate)).parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    return None
                parts.pop()
            else:
                parts.append(part)
        target = PurePosixPath(*parts).as_posix()
        for choice in (target, f"{target}.md", f"{target}.txt"):
            if choice in documents:
                return choice
        return None

    def _links(self, source: str, text: str, documents: set[str]) -> list[str]:
        candidates = [*(_MARKDOWN_LINK.findall(text)), *(_WIKI_LINK.findall(text))]
        return sorted(
            {
                target
                for raw in candidates
                if (target := self._link_target(source, raw, documents)) is not None
            }
        )

    def document(self, path: str) -> dict[str, Any]:
        relative = self._relative(path)
        self._document_name(relative)
        candidate = self._path(relative)
        text, size, updated_at = self._read_file(candidate)
        documents = set(self._document_paths())
        return {
            "path": relative.as_posix(),
            "name": relative.name,
            "content": text,
            "size_bytes": size,
            "updated_at": updated_at,
            "links": self._links(relative.as_posix(), text, documents),
        }

    def graph(self) -> dict[str, Any]:
        self._require_root()
        documents = self._document_paths()
        if len(documents) > _MAX_GRAPH_NODES:
            raise WorkspaceContentRejected("knowledge graph contains too many documents")
        nodes: list[dict[str, str]] = []
        edges: list[dict[str, str]] = []
        identities = set(documents)
        read_bytes = 0
        for relative, path in sorted(documents.items()):
            read_bytes += int(self._entry_stat(PurePosixPath(relative)).st_size)
            if read_bytes > MAX_IMPORT_BYTES:
                raise WorkspaceContentRejected(
                    "knowledge graph exceeds its 200 MiB scan limit"
                )
            text, _, _ = self._read_file(path)
            nodes.append({"path": relative, "label": PurePosixPath(relative).stem})
            edges.extend(
                {"source": relative, "target": target}
                for target in self._links(relative, text, identities)
            )
            if len(edges) > _MAX_GRAPH_EDGES:
                raise WorkspaceContentRejected("knowledge graph contains too many links")
        return {"nodes": nodes, "edges": edges}

    def create_category(
        self,
        path: str,
        *,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        relative = self._relative(path)
        payload = {"path": relative.as_posix()}

        def plan() -> str:
            if self._entry_exists(relative):
                raise WorkspaceContentConflict("knowledge category already exists")
            return relative.as_posix()

        stored_plan, request_path, request_status = self._idempotent_plan(
            operation="create_category",
            request_id=client_request_id,
            payload=payload,
            make_plan=plan,
        )
        if stored_plan != relative.as_posix():
            raise WorkspaceContentUnavailable("knowledge request authority is invalid")
        try:
            info = self._entry_stat(relative)
        except WorkspaceContentNotFound:
            info = None
        if info is not None:
            if request_path is None:
                raise WorkspaceContentConflict("knowledge category already exists")
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise WorkspaceContentUnavailable("knowledge category is not trusted")
        else:
            if request_status == "completed":
                raise WorkspaceContentConflict("completed knowledge category is missing")
            try:
                info = self._create_directory(relative)
            except WorkspaceContentConflict:
                info = self._entry_stat(relative)
                if not stat.S_ISDIR(info.st_mode):
                    raise
        result = {
            "path": relative.as_posix(),
            "name": relative.name,
            "kind": "category",
            "size_bytes": 0,
            "updated_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
            "children": [],
        }
        self._complete_request(request_path, "create_category", payload, stored_plan)
        return result

    def _write_new(self, destination: Path, content: bytes) -> None:
        relative = PurePosixPath(destination.relative_to(self.root).as_posix())
        parent = PurePosixPath(*relative.parts[:-1])
        temporary_name = f"emate-knowledge-{uuid.uuid4().hex}.tmp"
        try:
            with self._directory_lease(parent) as (directory_fd, directory):
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = (
                    os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
                    if directory_fd is not None
                    else os.open(directory / temporary_name, flags, 0o600)
                )
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    try:
                        if directory_fd is not None:
                            os.link(
                                temporary_name,
                                relative.name,
                                src_dir_fd=directory_fd,
                                dst_dir_fd=directory_fd,
                                follow_symlinks=False,
                            )
                        else:
                            os.link(directory / temporary_name, directory / relative.name)
                    except FileExistsError as error:
                        raise WorkspaceContentConflict(
                            "knowledge document already exists"
                        ) from error
                    if directory_fd is not None:
                        os.fsync(directory_fd)
                    else:
                        _fsync_directory(directory)
                finally:
                    try:
                        if directory_fd is not None:
                            os.unlink(temporary_name, dir_fd=directory_fd)
                            os.fsync(directory_fd)
                        else:
                            (directory / temporary_name).unlink(missing_ok=True)
                            _fsync_directory(directory)
                    except OSError:
                        pass
        except WorkspaceContentError:
            raise
        except OSError as error:
            raise WorkspaceContentUnavailable(
                "knowledge document could not be created"
            ) from error

    def _remove_created(self, destination: Path, expected_sha256: str) -> None:
        relative = PurePosixPath(destination.relative_to(self.root).as_posix())
        parent = PurePosixPath(*relative.parts[:-1])
        try:
            with self._directory_lease(parent) as (directory_fd, directory):
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = (
                    os.open(relative.name, flags, dir_fd=directory_fd)
                    if directory_fd is not None
                    else os.open(directory / relative.name, flags)
                )
                with os.fdopen(descriptor, "rb", closefd=True) as handle:
                    info = os.fstat(handle.fileno())
                    digest = hashlib.sha256(handle.read(MAX_DOCUMENT_BYTES + 1)).hexdigest()
                if not stat.S_ISREG(info.st_mode) or digest != expected_sha256:
                    return
                if directory_fd is not None:
                    current = os.stat(
                        relative.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if (int(current.st_dev), int(current.st_ino)) != (
                        int(info.st_dev),
                        int(info.st_ino),
                    ):
                        return
                    os.unlink(relative.name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                else:
                    current = os.lstat(directory / relative.name)
                    if (int(current.st_dev), int(current.st_ino)) != (
                        int(info.st_dev),
                        int(info.st_ino),
                    ):
                        return
                    (directory / relative.name).unlink()
                    _fsync_directory(directory)
        except (FileNotFoundError, WorkspaceContentError, OSError):
            return

    def create_document(
        self,
        path: str,
        content: str,
        *,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        relative = self._relative(path)
        self._document_name(relative)
        if not isinstance(content, str) or "\x00" in content:
            raise WorkspaceContentRejected("knowledge document must be UTF-8 text")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_DOCUMENT_BYTES:
            raise WorkspaceContentRejected("knowledge document exceeds 10 MiB")
        payload = {
            "path": relative.as_posix(),
            "content_sha256": hashlib.sha256(encoded).hexdigest(),
            "size_bytes": len(encoded),
        }

        def plan() -> str:
            if self._entry_exists(relative):
                raise WorkspaceContentConflict("knowledge document already exists")
            return relative.as_posix()

        stored_plan, request_path, request_status = self._idempotent_plan(
            operation="create_document",
            request_id=client_request_id,
            payload=payload,
            make_plan=plan,
        )
        if stored_plan != relative.as_posix():
            raise WorkspaceContentUnavailable("knowledge request authority is invalid")
        destination = self.root.joinpath(*relative.parts)
        parent_relative = PurePosixPath(*relative.parts[:-1])
        parent_info = self._entry_stat(parent_relative)
        if not stat.S_ISDIR(parent_info.st_mode):
            raise WorkspaceContentNotFound("knowledge category does not exist")
        if self._entry_exists(relative):
            if request_path is None:
                raise WorkspaceContentConflict("knowledge document already exists")
            existing, _, _ = self._read_file(destination)
            if existing.encode("utf-8") != encoded:
                raise WorkspaceContentConflict("knowledge document changed after its request")
        else:
            if request_status == "completed":
                raise WorkspaceContentConflict("completed knowledge document is missing")
            try:
                self._write_new(destination, encoded)
            except WorkspaceContentConflict:
                existing, _, _ = self._read_file(destination)
                if existing.encode("utf-8") != encoded:
                    raise
            except WorkspaceContentError:
                raise
            except OSError as error:
                raise WorkspaceContentUnavailable("knowledge document could not be created") from error
        result = self.document(relative.as_posix())
        self._complete_request(request_path, "create_document", payload, stored_plan)
        return result

    @staticmethod
    def _import_name(name: str) -> str:
        value = unicodedata.normalize("NFKC", str(name or ""))
        if "/" in value or "\\" in value:
            raise WorkspaceContentRejected("import filename is invalid")
        path = WorkspaceContentService._relative(value)
        if len(path.parts) != 1:
            raise WorkspaceContentRejected("import filename is invalid")
        if path.suffix.casefold() not in _DOCUMENT_SUFFIXES:
            raise WorkspaceContentRejected("imports must be Markdown or text")
        return path.name

    def _available_name(
        self,
        category: PurePosixPath,
        name: str,
        reserved: set[str],
    ) -> str:
        path = PurePosixPath(name)
        for index in range(1, 10_001):
            candidate = name if index == 1 else f"{path.stem} ({index}){path.suffix}"
            if candidate not in reserved and not self._entry_exists(category / candidate):
                reserved.add(candidate)
                return candidate
        raise WorkspaceContentConflict("knowledge import contains too many name collisions")

    def import_documents(
        self,
        category_path: str,
        documents: Iterable[tuple[str, bytes]],
        *,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        relative_category = self._relative(category_path, allow_root=True)
        category_info = self._entry_stat(relative_category)
        if not stat.S_ISDIR(category_info.st_mode):
            raise WorkspaceContentNotFound("knowledge category does not exist")
        category = self.root.joinpath(*relative_category.parts)
        inputs = [(str(name or ""), bytes(raw)) for name, raw in documents]
        if not inputs:
            raise WorkspaceContentRejected("knowledge import requires at least one file")
        if len(inputs) > MAX_IMPORT_FILES:
            raise WorkspaceContentRejected("knowledge import exceeds 100 files")
        total = sum(len(content) for _, content in inputs)
        if total > MAX_IMPORT_BYTES:
            raise WorkspaceContentRejected("knowledge import exceeds 200 MiB")
        request_payload = {
            "category_path": relative_category.as_posix() if relative_category.parts else "",
            "files": [
                {
                    "index": index,
                    "name": name,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for index, (name, content) in enumerate(inputs)
            ],
        }

        def plan() -> dict[str, Any]:
            reserved: set[str] = set()
            items: list[dict[str, Any]] = []
            for index, (original_name, content) in enumerate(inputs):
                try:
                    filename = self._import_name(original_name)
                    self._decode(content)
                except WorkspaceContentRejected as error:
                    items.append(
                        {
                            "index": index,
                            "original_name": original_name[:512],
                            "status": "rejected",
                            "reason": str(error),
                        }
                    )
                    continue
                selected = self._available_name(relative_category, filename, reserved)
                items.append(
                    {
                        "index": index,
                        "original_name": original_name,
                        "name": selected,
                        "path": (
                            (relative_category / selected).as_posix()
                            if relative_category.parts
                            else selected
                        ),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "status": "imported" if selected == filename else "renamed",
                        "reason": None,
                    }
                )
            return {"category_path": request_payload["category_path"], "items": items}

        stored_plan, request_path, request_status = self._idempotent_plan(
            operation="import_documents",
            request_id=client_request_id,
            payload=request_payload,
            make_plan=plan,
        )
        if not isinstance(stored_plan, dict) or stored_plan.get("category_path") != request_payload["category_path"]:
            raise WorkspaceContentUnavailable("knowledge request authority is invalid")
        planned_items = stored_plan.get("items")
        if not isinstance(planned_items, list) or len(planned_items) != len(inputs):
            raise WorkspaceContentUnavailable("knowledge request authority is invalid")
        created: list[tuple[Path, str]] = []
        results: list[dict[str, Any]] = []
        try:
            for index, ((original_name, content), item) in enumerate(zip(inputs, planned_items, strict=True)):
                if not isinstance(item, dict) or item.get("index") != index:
                    raise WorkspaceContentUnavailable("knowledge request authority is invalid")
                if item.get("status") == "rejected":
                    results.append(
                        {
                            "original_name": str(item.get("original_name", original_name))[:512],
                            "name": None,
                            "path": None,
                            "status": "rejected",
                            "reason": str(item.get("reason") or "文件不符合知识导入要求。"),
                        }
                    )
                    continue
                name = item.get("name")
                path = item.get("path")
                if (
                    item.get("status") not in {"imported", "renamed"}
                    or not isinstance(name, str)
                    or not isinstance(path, str)
                    or item.get("sha256") != hashlib.sha256(content).hexdigest()
                    or self._import_name(name) != name
                    or self._relative(path) != (relative_category / name)
                ):
                    raise WorkspaceContentUnavailable("knowledge request authority is invalid")
                destination = category / name
                target_relative = relative_category / name
                if self._entry_exists(target_relative):
                    existing, _, _ = self._read_file(destination)
                    if existing.encode("utf-8") != content:
                        raise WorkspaceContentConflict("knowledge import target changed")
                else:
                    if request_status == "completed":
                        raise WorkspaceContentConflict("completed knowledge import is missing")
                    try:
                        self._write_new(destination, content)
                        created.append((destination, str(item["sha256"])))
                    except WorkspaceContentConflict:
                        existing, _, _ = self._read_file(destination)
                        if existing.encode("utf-8") != content:
                            raise
                results.append(
                    {
                        "original_name": original_name,
                        "name": name,
                        "path": path,
                        "status": item["status"],
                        "reason": None,
                    }
                )
        except Exception:
            for created_path, digest in created:
                self._remove_created(created_path, digest)
            raise
        result = {
            "imported_count": sum(item["status"] in {"imported", "renamed"} for item in results),
            "rejected_count": sum(item["status"] == "rejected" for item in results),
            "total_bytes": total,
            "items": results,
        }
        self._complete_request(request_path, "import_documents", request_payload, stored_plan)
        return result


__all__ = [
    "MAX_DOCUMENT_BYTES",
    "MAX_IMPORT_BYTES",
    "MAX_IMPORT_FILES",
    "WorkspaceContentConflict",
    "WorkspaceContentError",
    "WorkspaceContentNotFound",
    "WorkspaceContentRejected",
    "WorkspaceContentService",
    "WorkspaceContentUnavailable",
]
