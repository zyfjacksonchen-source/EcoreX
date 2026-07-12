"""Backend-owned standard output roots; browser clients never submit paths."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Iterable


_WINDOWS_USER_SHELL_FOLDERS = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
)
_WINDOWS_DOWNLOADS_ID = "{374DE290-123F-4565-9164-39C4925E467B}"


def _windows_known_folder(value_name: str, fallback: Path) -> Path:
    if sys.platform != "win32":
        return fallback
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_USER_SHELL_FOLDERS) as key:
            raw, _kind = winreg.QueryValueEx(key, value_name)
        expanded = os.path.expandvars(str(raw)).strip()
        candidate = Path(expanded).expanduser()
        return candidate if candidate.is_absolute() else fallback
    except (ImportError, OSError, TypeError, ValueError):
        return fallback


def standard_output_roots(
    workspace_roots: Iterable[str | Path] = (),
) -> dict[str, Path]:
    """Resolve OS/user directories once inside the trusted product composer.

    The returned roots are only configuration inputs for ``OutputService``;
    callers receive aliases, never these host paths.
    """

    home = Path.home().expanduser()
    documents = _windows_known_folder("Personal", home / "Documents")
    downloads = _windows_known_folder(_WINDOWS_DOWNLOADS_ID, home / "Downloads")
    workspace = next((Path(value).expanduser() for value in workspace_roots), None)
    roots = {
        "documents": documents / "EcoreX",
        "downloads": downloads / "EcoreX",
    }
    if workspace is not None and workspace.is_absolute():
        roots["workspace"] = workspace / "EcoreX Output"
    return roots


__all__ = ["standard_output_roots"]
