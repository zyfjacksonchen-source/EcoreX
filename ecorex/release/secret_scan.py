"""Deterministic secret-shape policy shared by Stage and Candidate gates."""

from __future__ import annotations

from pathlib import PurePosixPath
import re


_PRIVATE_KEY = re.compile(
    rb"-----BEGIN ((?:RSA |EC |OPENSSH )?PRIVATE KEY)-----\r?\n"
    rb"(?:[A-Za-z0-9+/=]{16,}\r?\n)+-----END \1-----"
)
_TEXT_DETECTORS = (
    ("aws_access_key", re.compile(rb"(?<![A-Za-z0-9+/=])AKIA[0-9A-Z]{16}(?![A-Za-z0-9+/=])")),
    ("github_token", re.compile(rb"(?<![A-Za-z0-9+/=])gh[pousr]_[A-Za-z0-9]{20,}(?![A-Za-z0-9+/=])")),
    ("slack_token", re.compile(rb"(?<![A-Za-z0-9+/=])xox[baprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9+/=])")),
)
TEXT_SECRET_SUFFIXES = frozenset(
    {
        ".cfg",
        ".c",
        ".cpp",
        ".conf",
        ".css",
        ".env",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".key",
        ".md",
        ".mjs",
        ".pem",
        ".py",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)
TEXT_SECRET_FILENAMES = frozenset(
    {
        "browser-runtime.json",
        "ecorex-image-pack.json",
        "ecorex-pack.json",
        "pack-python.json",
        "runtime-config.json",
        "storage-migrations.json",
    }
)


def detect_secret(payload: bytes, logical_path: str) -> str | None:
    """Return a stable detector id without returning matched credential bytes."""

    if _PRIVATE_KEY.search(payload):
        return "private_key"
    path = PurePosixPath(logical_path.replace("\\", "/"))
    if (
        path.suffix.casefold() not in TEXT_SECRET_SUFFIXES
        and path.name not in TEXT_SECRET_FILENAMES
    ):
        return None
    for detector_id, pattern in _TEXT_DETECTORS:
        if pattern.search(payload):
            return detector_id
    return None
