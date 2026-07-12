from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


@dataclass(frozen=True)
class ArtifactStore:
    """Workspace-owned artifact paths and display projection helpers."""

    workspace: Path

    @property
    def images_dir(self) -> Path:
        return Path(self.workspace).expanduser() / "images"

    @property
    def artifacts_dir(self) -> Path:
        return Path(self.workspace).expanduser() / "artifacts"

    def image_output_dir(self) -> str:
        self.images_dir.mkdir(parents=True, exist_ok=True)
        return str(self.images_dir)

    def path_is_runtime_dir(self, value: Any) -> bool:
        raw = str(value or "").strip()
        if not raw:
            return False
        normalized = raw.replace("\\", "/").lower()
        return "/runtime-" in normalized or normalized.endswith("/runtime") or "/runtime/" in normalized

    def display_artifact(self, artifact: Dict[str, Any]) -> Dict[str, Any]:
        path = os.fspath((artifact or {}).get("path") or "")
        hidden_reason = "runtime_path_not_previewable" if self.path_is_runtime_dir(path) else ""
        return {
            **dict(artifact or {}),
            "visible": not hidden_reason,
            "hiddenReason": hidden_reason,
            "availability": "blocked" if hidden_reason else (artifact or {}).get("availability", "available"),
        }
