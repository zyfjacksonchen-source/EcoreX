"""Backend-owned project workspace authorization for tool executions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat as stat_module
from typing import Any

from ecorex.capabilities import ToolExecutionScope


class ProjectWorkspaceAuthority:
    """Resolve the project root durably bound to an exact Thread/Turn/Job.

    Tool arguments never contribute roots.  The only additional root comes
    from a project selected through the host folder picker and persisted in
    the backend project catalog before the Thread was created.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def __call__(self, scope: ToolExecutionScope | None) -> tuple[Path, ...]:
        if not isinstance(scope, ToolExecutionScope):
            return ()
        try:
            connection = sqlite3.connect(
                f"file:{self.database_path.resolve().as_posix()}?mode=ro",
                uri=True,
                timeout=5,
            )
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute(
                    "SELECT thread.metadata_json AS thread_metadata, "
                    "project.project_id, project.project_path, "
                    "project.active AS project_active, project.metadata_json AS project_metadata "
                    "FROM jobs AS job "
                    "JOIN turns AS turn ON turn.turn_id=job.turn_id "
                    "JOIN threads AS thread ON thread.thread_id=turn.thread_id "
                    "LEFT JOIN projects AS project ON project.project_id="
                    "json_extract(thread.metadata_json, '$.project_id') "
                    "WHERE job.job_id=? AND job.kind='agent_turn' "
                    "AND job.turn_id=? AND turn.thread_id=?",
                    (scope.job_id, scope.turn_id, scope.thread_id),
                ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return ()
        if row is None or row["project_id"] is None or int(row["project_active"] or 0) != 1:
            return ()
        try:
            thread_metadata: Any = json.loads(str(row["thread_metadata"]))
            project_metadata: Any = json.loads(str(row["project_metadata"]))
        except (TypeError, ValueError):
            return ()
        project_path = str(row["project_path"] or "")
        if (
            not isinstance(thread_metadata, dict)
            or thread_metadata.get("project_id") != row["project_id"]
            or not isinstance(project_metadata, dict)
            or project_metadata.get("status", "active") != "active"
        ):
            return ()
        try:
            return (self._safe_root(project_path),)
        except (OSError, ValueError):
            # A removed, replaced or newly-linked project directory must not
            # turn into a broader fallback authority or an untyped crash.
            return ()

    @staticmethod
    def _safe_root(value: str) -> Path:
        if not value or "\x00" in value:
            raise ValueError("project workspace path is invalid")
        raw = Path(value)
        metadata = raw.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat_module.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag):
            raise ValueError("project workspace cannot be a link or reparse point")
        resolved = raw.resolve(strict=True)
        if not resolved.is_dir() or not os.path.isabs(resolved):
            raise ValueError("project workspace must be an existing directory")
        return resolved


__all__ = ["ProjectWorkspaceAuthority"]
