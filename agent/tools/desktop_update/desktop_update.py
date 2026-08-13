"""Local Agent request bridge to Electron's single desktop updater."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict

from agent.tools.base_tool import BaseTool, ToolResult


_NONCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TOOL_CALL_ID = re.compile(r"^[A-Za-z0-9._:-]{1,252}$")


class DesktopUpdateTool(BaseTool):
    name = "desktop_update"
    description = (
        "Update the installed e-Mate desktop application from its official CDN and relaunch it. "
        "Call this only after the user explicitly asks e-Mate to update itself. The Electron desktop "
        "updater owns download, verification, installation, and relaunch."
    )
    params = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["install_latest"],
                "description": "Install the latest e-Mate release and relaunch the application.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        if str((params or {}).get("action") or "").strip() != "install_latest":
            return ToolResult.fail({"status": "error", "message": "action must be install_latest"})
        if os.environ.get("EMATE_DESKTOP") != "1" or os.environ.get("EMATE_PACKAGED_RUNTIME") != "1":
            return ToolResult.fail({"status": "error", "message": "desktop updater is unavailable"})

        data_dir = Path(os.environ.get("EMATE_DATA_DIR") or "").expanduser()
        expected_nonce = os.environ.get("ECOREX_RUNTIME_OWNER_NONCE") or ""
        tool_call_id = str(getattr(self, "tool_call_id", "") or "")
        if not data_dir.is_absolute() or not _NONCE.fullmatch(expected_nonce) or not _TOOL_CALL_ID.fullmatch(tool_call_id):
            return ToolResult.fail({"status": "error", "message": "desktop updater ownership is unavailable"})
        temporary: Path | None = None
        try:
            owner = json.loads((data_dir / "bootstrap" / "runtime-owner.json").read_text(encoding="utf-8"))
            if owner.get("schema_version") != 2 or owner.get("nonce") != expected_nonce:
                raise ValueError("runtime owner changed")
            directory = data_dir / "desktop-update"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink():
                raise ValueError("request directory is unsafe")
            directory.chmod(0o700)
            target = directory / "request.json"
            temporary = directory / f"request.{os.getpid()}.tmp"
            payload = {
                "schema_version": 1,
                "action": "install_latest",
                "owner_nonce": expected_nonce,
                "tool_call_id": tool_call_id,
            }
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                )
            os.replace(temporary, target)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return ToolResult.fail({"status": "error", "message": "desktop update request was not accepted"})
        return ToolResult.success({
            "status": "accepted",
            "action": "install_latest",
            "completed": False,
            "message": "The desktop updater accepted the request; do not claim completion until e-Mate relaunches on the new version.",
            "willRelaunch": True,
        })
