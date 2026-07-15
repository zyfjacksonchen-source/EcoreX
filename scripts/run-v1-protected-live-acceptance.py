#!/usr/bin/env python3
"""Invoke the digest-pinned protected Windows live-acceptance driver.

The environment-owned driver activates the exact signed Candidate in the
runner's persistent acceptance installation, uses its managed session from
Windows Credential Manager, and performs production model/image plus Chrome
CDP scenarios.  It receives no credential in argv, stdin, environment output,
or this receipt boundary.  Only the strict redaction-safe JSON contract may be
returned on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.evidence_io import (  # noqa: E402
    strict_json_loads,
    write_new_json_file,
)
from ecorex.release.live_acceptance import (  # noqa: E402
    validate_live_acceptance_evidence,
)
from ecorex.release.process_boundary import run_bounded_process  # noqa: E402


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_ALLOWED_ENVIRONMENT = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--executable-sha256", required=True)
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--trusted-public-key", required=True)
    parser.add_argument("--staging-provenance", required=True, type=Path)
    parser.add_argument("--expected-staging-run-id", required=True, type=int)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=5_400)
    return parser


def _identity(path: Path) -> tuple[int, int, int, int]:
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _pinned_file(path: Path, expected_sha256: str) -> tuple[Path, tuple[int, int, int, int]]:
    if _SHA256.fullmatch(expected_sha256) is None:
        raise ValueError("live_acceptance_driver_digest_invalid")
    absolute = Path(os.path.abspath(path))
    before = absolute.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(before.st_mode)
        or bool(getattr(before, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(before.st_mode)
        or not 1 <= before.st_size <= 256 * 1024 * 1024
    ):
        raise ValueError("live_acceptance_driver_invalid")
    digest = hashlib.sha256()
    with absolute.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    current = absolute.lstat()
    expected_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    if (
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != expected_identity
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != expected_identity
        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        != expected_identity
        or digest.hexdigest() != expected_sha256
    ):
        raise ValueError("live_acceptance_driver_untrusted")
    return absolute, expected_identity


def _candidate_root(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    metadata = absolute.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ValueError("live_acceptance_candidate_root_invalid")
    return absolute.resolve(strict=True)


def _binding_module() -> Any:
    path = ROOT / "scripts" / "bind-v1-release-gate-evidence.py"
    name = "ecorex_v1_live_acceptance_candidate_authority"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("live_acceptance_candidate_authority_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _candidate_identity(
    root: Path,
    *,
    trusted_public_key: str,
    staging_provenance: Path,
    commit_sha: str,
    expected_staging_run_id: int,
) -> dict[str, object]:
    authenticated = _binding_module().authenticate_candidate(
        candidate_receipt=root / "candidate-build-receipt.json",
        release_manifest=root / "release" / "release-manifest.json",
        trusted_public_key=trusted_public_key,
        staging_provenance=staging_provenance,
        commit_sha=commit_sha,
        expected_staging_run_id=expected_staging_run_id,
    )
    manifest = authenticated["manifest"]
    return {
        "release_id": manifest.release_id,
        "version": manifest.version,
        "channel": manifest.channel.value,
        "build_digest": manifest.build_digest,
        "manifest_sha256": authenticated["manifest_sha256"],
        "web_tree_sha256": authenticated["web_tree_sha256"],
        "candidate_receipt_sha256": authenticated["candidate_sha256"],
    }


def _environment() -> dict[str, str]:
    result: dict[str, str] = {}
    for name in sorted(_ALLOWED_ENVIRONMENT):
        value = os.environ.get(name)
        if isinstance(value, str) and value and "\x00" not in value:
            result[name] = value
    return result


def _safe_error_code(exc: Exception) -> str:
    """Return a stable diagnostic without exposing paths or child output."""

    message = str(exc)
    if isinstance(exc, ValueError) and _ERROR_CODE.fullmatch(message) is not None:
        return message
    return type(exc).__name__


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if (
            os.name != "nt"
            or _COMMIT.fullmatch(args.commit_sha) is None
            or isinstance(args.workflow_run_id, bool)
            or args.workflow_run_id < 1
            or isinstance(args.expected_staging_run_id, bool)
            or args.expected_staging_run_id < 1
            or not 60 <= args.timeout_seconds <= 7_200
        ):
            raise ValueError("live_acceptance_invocation_invalid")
        executable, executable_identity = _pinned_file(
            args.executable, args.executable_sha256
        )
        candidate_root = _candidate_root(args.candidate_root)
        expected_candidate = _candidate_identity(
            candidate_root,
            trusted_public_key=args.trusted_public_key,
            staging_provenance=args.staging_provenance,
            commit_sha=args.commit_sha,
            expected_staging_run_id=args.expected_staging_run_id,
        )
        request = {
            "schema_version": 1,
            "commit_sha": args.commit_sha,
            "workflow_run_id": args.workflow_run_id,
            "candidate_root": str(candidate_root),
            "expected_candidate": expected_candidate,
        }
        result = run_bounded_process(
            [str(executable)],
            payload=(
                json.dumps(
                    request,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            ),
            cwd=candidate_root,
            environment=_environment(),
            timeout_seconds=float(args.timeout_seconds),
            max_stdout_bytes=8 * 1024 * 1024,
            max_stderr_bytes=64 * 1024,
        )
        if result.returncode != 0:
            raise ValueError("live_acceptance_driver_rejected")
        value = strict_json_loads(
            result.stdout, code="live_acceptance_driver_output_invalid"
        )
        if not isinstance(value, dict):
            raise ValueError("live_acceptance_driver_output_invalid")
        validate_live_acceptance_evidence(
            value,
            expected_commit=args.commit_sha,
            expected_workflow_run_id=args.workflow_run_id,
            expected_candidate=expected_candidate,
        )
        if value["runner"].get("acceptance_driver_sha256") != args.executable_sha256:
            raise ValueError("live_acceptance_driver_identity_mismatch")
        if _identity(executable) != executable_identity:
            raise ValueError("live_acceptance_driver_changed")
        _pinned_file(executable, args.executable_sha256)
        write_new_json_file(value, args.output.resolve(), code="live_acceptance_evidence_exists")
        print(
            json.dumps(
                {
                    "ok": True,
                    "release_id": expected_candidate["release_id"],
                    "gates": ["cdp-acceptance", "live-image", "live-model"],
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": _safe_error_code(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
