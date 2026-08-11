"""Durable project catalog projected from the shared Runtime database."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import stat
from typing import Any

from ecorex.protocol import ProjectListResponse, ProjectProjection
from ecorex.runtime.database import SQLiteDatabase, json_dumps, json_loads
from ecorex.runtime.ids import new_id


class ProjectNotFound(LookupError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or len(str(raw)) > 4096:
        raise ValueError("project_folder_invalid")
    try:
        root = raw.resolve(strict=True)
        metadata = root.lstat()
    except OSError as error:
        raise ValueError("project_folder_unavailable") from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
    ):
        raise ValueError("project_folder_must_be_a_real_directory")
    return root


def _path_identity(path: Path) -> str:
    normalized = os.path.normcase(str(path)) if os.name == "nt" else str(path)
    return normalized.casefold()


class ProjectService:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def list(self) -> ProjectListResponse:
        with self.database.reader() as connection:
            project_rows = connection.execute(
                "SELECT * FROM projects ORDER BY pinned DESC, active DESC, name COLLATE NOCASE, project_id"
            ).fetchall()
            legacy_bindings = connection.execute(
                "SELECT project_id, COUNT(*) AS count FROM project_thread_bindings GROUP BY project_id"
            ).fetchall()
            thread_rows = connection.execute(
                "SELECT metadata_json FROM threads WHERE status = 'active'"
            ).fetchall()
        counts = {str(row["project_id"]): int(row["count"]) for row in legacy_bindings}
        for row in thread_rows:
            metadata = json_loads(row["metadata_json"], {})
            project_id = metadata.get("project_id") if isinstance(metadata, dict) else None
            if isinstance(project_id, str) and project_id:
                counts[project_id] = counts.get(project_id, 0) + 1
        projects: list[ProjectProjection] = []
        for row in project_rows:
            metadata = json_loads(row["metadata_json"], {})
            if not isinstance(metadata, dict) or metadata.get("status") == "archived":
                continue
            projects.append(
                ProjectProjection(
                    project_id=str(row["project_id"]),
                    name=str(row["name"]),
                    project_path=str(row["project_path"]),
                    pinned=bool(row["pinned"]),
                    thread_count=counts.get(str(row["project_id"]), 0),
                    created_at=str(metadata.get("created_at") or ""),
                    updated_at=str(metadata.get("updated_at") or metadata.get("created_at") or ""),
                )
            )
        return ProjectListResponse(projects=projects)

    def require(self, project_id: str) -> ProjectProjection:
        for project in self.list().projects:
            if project.project_id == project_id:
                return project
        raise ProjectNotFound("project_not_found")

    def create_from_path(self, value: str | Path, *, client_request_id: str) -> ProjectProjection:
        root = _safe_root(value)
        identity = _path_identity(root)
        now = _now()
        with self.database.transaction() as connection:
            rows = connection.execute("SELECT * FROM projects").fetchall()
            for row in rows:
                metadata = json_loads(row["metadata_json"], {})
                candidate = str(row["project_path"])
                if candidate and _path_identity(Path(candidate)) == identity:
                    if isinstance(metadata, dict) and metadata.get("status") == "archived":
                        metadata = {**metadata, "status": "active", "updated_at": now}
                    connection.execute(
                        "UPDATE projects SET memory_path='MEMORY.md', "
                        "dreams_path='memory/dreams', metadata_json=?, active=1 "
                        "WHERE project_id=?",
                        (json_dumps(metadata), row["project_id"]),
                    )
                    project_id = str(row["project_id"])
                    break
            else:
                project_id = new_id("prj")
                metadata: dict[str, Any] = {
                    "schema_version": 1,
                    "status": "active",
                    "path_identity": identity,
                    "created_at": now,
                    "updated_at": now,
                    "client_request_id": client_request_id,
                }
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, legacy_project_id, name, project_path,
                        memory_path, dreams_path, pinned, active, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?)
                    """,
                    (
                        project_id,
                        f"v1:{project_id}",
                        root.name or str(root),
                        str(root),
                        "MEMORY.md",
                        "memory/dreams",
                        json_dumps(metadata),
                    ),
                )
        return self.require(project_id)

    def thread_metadata(self, project_id: str | None) -> dict[str, Any]:
        if project_id is None:
            return {"conversation_kind": "general"}
        project = self.require(project_id)
        return {
            "conversation_kind": "project",
            "project_id": project.project_id,
            "project_name": project.name,
        }
