#!/usr/bin/env python3
"""Install one EcoreX Python profile exclusively from repository hash locks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
LOCK_ROOT = ROOT / "requirements" / "locks"
PROFILES = frozenset({"cloud", "dev", "platform-stage", "runtime"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    return parser


def _manifest() -> dict[str, dict[str, str]]:
    path = LOCK_ROOT / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SystemExit("dependency_lock_manifest_invalid") from None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("lock_type") != "ecorex-python-hash-lock-set"
        or value.get("python") != "3.11.9"
        or not isinstance(value.get("profiles"), list)
    ):
        raise SystemExit("dependency_lock_manifest_invalid")
    profiles: dict[str, dict[str, str]] = {}
    for raw in value["profiles"]:
        if not isinstance(raw, dict) or set(raw) != {
            "input",
            "input_sha256",
            "lock",
            "lock_sha256",
            "profile",
        }:
            raise SystemExit("dependency_lock_manifest_invalid")
        profile = raw.get("profile")
        lock = raw.get("lock")
        digest = raw.get("lock_sha256")
        if (
            not isinstance(profile, str)
            or not isinstance(lock, str)
            or lock != f"{profile}.lock"
            or _SHA256.fullmatch(str(digest)) is None
            or profile in profiles
        ):
            raise SystemExit("dependency_lock_manifest_invalid")
        candidate = LOCK_ROOT / lock
        try:
            if candidate.is_symlink() or not candidate.is_file():
                raise OSError("not regular")
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            raise SystemExit("dependency_lock_file_invalid") from None
        if actual != digest:
            raise SystemExit("dependency_lock_digest_mismatch")
        profiles[profile] = {"lock": lock, "lock_sha256": digest}
    if frozenset(profiles) != PROFILES | {"bootstrap"}:
        raise SystemExit("dependency_lock_profile_set_invalid")
    return profiles


def _run(*arguments: str) -> None:
    environment = dict(os.environ)
    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            *arguments,
            "--no-input",
            "--index-url",
            "https://pypi.org/simple",
        ),
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info[:3] != (3, 11, 9):
        raise SystemExit("dependency_python_toolchain_mismatch")
    profile = _parser().parse_args(argv).profile
    profiles = _manifest()
    for selected in ("bootstrap", profile):
        _run(
            "install",
            "--require-hashes",
            "--only-binary=:all:",
            "--no-deps",
            "-r",
            str(LOCK_ROOT / profiles[selected]["lock"]),
        )
    _run(
        "install",
        "--no-deps",
        "--no-build-isolation",
        "-e",
        str(ROOT),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
