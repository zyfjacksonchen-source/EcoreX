"""Native directory selection owned by the loopback Runtime, never the WebUI."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Protocol


class ProjectFolderSelectionCancelled(RuntimeError):
    """The user closed the native picker without selecting a directory."""


class FolderPicker(Protocol):
    def __call__(self) -> Path: ...


def _run_picker(command: tuple[str, ...]) -> Path:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(  # noqa: S603 - command is a fixed product contract
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=300,
            creationflags=creationflags,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("project_folder_picker_unavailable") from error
    raw = completed.stdout.decode("utf-8", errors="strict").strip()
    if completed.returncode != 0 or not raw:
        raise ProjectFolderSelectionCancelled("project_folder_selection_cancelled")
    return Path(raw)


def pick_project_folder() -> Path:
    """Open the host picker through a fixed, non-shell command contract."""

    if os.name == "nt":
        script = (
            "$s=New-Object -ComObject Shell.Application;"
            "$f=$s.BrowseForFolder(0,'选择 EcoreX 项目文件夹',0,0);"
            "if($null -ne $f){[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "[Console]::Out.Write($f.Self.Path)}"
        )
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        executable = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        return _run_picker(
            (
                str(executable),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            )
        )
    if sys.platform == "darwin":
        return _run_picker(
            (
                "/usr/bin/osascript",
                "-e",
                'POSIX path of (choose folder with prompt "选择 EcoreX 项目文件夹")',
            )
        )
    raise RuntimeError("project_folder_picker_unsupported")
