#!/usr/bin/env python3
"""Invoke a digest-pinned platform stager and mint content-bound stage receipts.

The external stager is responsible for producing real redistributable Runtime
and Pack trees and for running platform probes.  This wrapper never fabricates
missing bytes or passing evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.candidate import (  # noqa: E402
    CandidateBuildError,
    PACK_TOOLS,
    STAGE_GATES,
    write_stage_receipt,
)
from ecorex.release.process_boundary import (  # noqa: E402
    BoundedProcessError,
    run_bounded_process,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{2,127}$")
_SECRET_PATTERNS = (
    re.compile(
        rb"-----BEGIN ((?:RSA |EC |OPENSSH )?PRIVATE KEY)-----\r?\n"
        rb"(?:[A-Za-z0-9+/=]{16,}\r?\n)+-----END \1-----"
    ),
    re.compile(rb"(?<![A-Za-z0-9+/=])AKIA[0-9A-Z]{16}(?![A-Za-z0-9+/=])"),
    re.compile(rb"(?<![A-Za-z0-9+/=])gh[pousr]_[A-Za-z0-9]{20,}(?![A-Za-z0-9+/=])"),
    re.compile(rb"(?<![A-Za-z0-9+/=])xox[baprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9+/=])"),
)
_STAGER_TIMEOUT_SECONDS = 45 * 60


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=("windows", "macos"))
    parser.add_argument("--architecture", required=True, choices=("x64", "arm64"))
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--workflow-run-attempt", type=int, default=1)
    return parser


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CandidateBuildError("platform_stager_configuration_missing")
    return value


def _public_keyring_environment(name: str) -> dict[str, str]:
    try:
        value = json.loads(_environment(name))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise CandidateBuildError("platform_stager_publication_trust_invalid") from None
    if not isinstance(value, dict) or not value:
        raise CandidateBuildError("platform_stager_publication_trust_invalid")
    return value


def _pinned_file(path_value: str, digest: str) -> Path:
    if _SHA256.fullmatch(digest) is None:
        raise CandidateBuildError("platform_stager_digest_invalid")
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise CandidateBuildError("platform_stager_path_invalid")
    try:
        before = path.lstat()
    except OSError:
        raise CandidateBuildError("platform_stager_unavailable") from None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(before.st_mode)
        or bool(getattr(before, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size < 1
    ):
        raise CandidateBuildError("platform_stager_invalid")
    resolved = path.resolve(strict=True)
    if hashlib.sha256(resolved.read_bytes()).hexdigest() != digest:
        raise CandidateBuildError("platform_stager_digest_mismatch")
    return resolved


def _evidence(root: Path, target: str, kind: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for gate in STAGE_GATES[kind]:
        path = root / ".evidence" / target / kind / f"{gate}.json"
        try:
            payload = path.read_bytes()
        except OSError:
            raise CandidateBuildError("platform_stage_evidence_missing") from None
        if not 1 <= len(payload) <= 4 * 1024 * 1024:
            raise CandidateBuildError("platform_stage_evidence_invalid")
        if any(pattern.search(payload) for pattern in _SECRET_PATTERNS):
            raise CandidateBuildError("platform_stage_evidence_contains_secret")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise CandidateBuildError("platform_stage_evidence_invalid") from None
        if (
            not isinstance(value, dict)
            or value.get("status") != "passed"
            or value.get("gate") != gate
        ):
            raise CandidateBuildError("platform_stage_evidence_not_passed")
        values[gate] = hashlib.sha256(payload).hexdigest()
    return values


def _failure(path: Path, code: str, args: argparse.Namespace) -> None:
    path.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": 1,
        "receipt_type": "ecorex-platform-stage",
        "status": "failed",
        "code": code,
        "commit_sha": args.commit_sha if _COMMIT.fullmatch(args.commit_sha) else None,
        "platform": args.platform,
        "architecture": args.architecture,
    }
    (path / "stage-failure.json").write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _adapter_failure_code(stderr: bytes) -> str | None:
    """Extract only a stable public StageError code from bounded stderr."""

    try:
        text = stderr.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        code = value.get("code") if isinstance(value, dict) else None
        if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
            return code
    return None


def _adapter_failure_diagnostic(stderr: bytes) -> dict[str, str] | None:
    """Retain only the fixed non-secret classifier and hashes from Stage."""

    try:
        text = stderr.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(value, dict) or value.get("code") != "stage_supply_chain_secret_match":
            continue
        diagnostic = value.get("diagnostic")
        if not isinstance(diagnostic, dict) or set(diagnostic) != {
            "content_sha256",
            "detector_id",
            "kind",
            "location_sha256",
        }:
            return None
        if (
            diagnostic.get("detector_id")
            not in {"aws_access_key", "github_token", "private_key", "slack_token"}
            or diagnostic.get("kind") not in {"archive_member", "regular"}
            or _SHA256.fullmatch(str(diagnostic.get("content_sha256"))) is None
            or _SHA256.fullmatch(str(diagnostic.get("location_sha256"))) is None
        ):
            return None
        return {key: str(diagnostic[key]) for key in sorted(diagnostic)}
    return None


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output_root.resolve()
    try:
        if (
            _COMMIT.fullmatch(args.commit_sha) is None
            or args.workflow_run_id < 1
            or args.workflow_run_attempt < 1
        ):
            raise CandidateBuildError("platform_stage_identity_invalid")
        if (args.platform, args.architecture) not in {
            ("windows", "x64"),
            ("macos", "arm64"),
            ("macos", "x64"),
        }:
            raise CandidateBuildError("platform_stage_target_invalid")
        if os.path.lexists(output):
            raise CandidateBuildError("platform_stage_output_exists")
        output.mkdir(parents=True)
        executable = _pinned_file(
            _environment("ECOREX_PLATFORM_STAGER_EXECUTABLE"),
            _environment("ECOREX_PLATFORM_STAGER_EXECUTABLE_SHA256"),
        )
        adapter_value = os.environ.get("ECOREX_PLATFORM_STAGER_ADAPTER") or None
        adapter_digest = os.environ.get("ECOREX_PLATFORM_STAGER_ADAPTER_SHA256") or None
        if (adapter_value is None) != (adapter_digest is None):
            raise CandidateBuildError("platform_stager_adapter_invalid")
        adapter = _pinned_file(adapter_value, adapter_digest) if adapter_value else None
        request = json.dumps(
            {
                "schema_version": 1,
                "operation": "stage-ecorex-v1-candidate",
                "repo_root": str(args.repo_root.resolve(strict=True)),
                "output_root": str(output),
                "platform": args.platform,
                "architecture": args.architecture,
                "commit_sha": args.commit_sha,
                "workflow_run_id": args.workflow_run_id,
                "workflow_run_attempt": args.workflow_run_attempt,
                "public_bootstrap_index_url": _environment(
                    "ECOREX_PUBLIC_BOOTSTRAP_INDEX_URL"
                ),
                "publication_public_keys": _public_keyring_environment(
                    "ECOREX_PUBLICATION_PUBLIC_KEYS_JSON"
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        command = [str(executable), *([str(adapter)] if adapter is not None else [])]
        try:
            result = run_bounded_process(
                command,
                payload=request,
                cwd=args.repo_root.resolve(strict=True),
                environment=os.environ,
                timeout_seconds=_STAGER_TIMEOUT_SECONDS,
                max_stdout_bytes=4096,
                max_stderr_bytes=4096,
            )
        except (OSError, BoundedProcessError) as exc:
            raise CandidateBuildError(
                f"platform_stager_{type(exc).__name__.casefold()}"
            ) from None
        if result.returncode != 0:
            diagnostic = _adapter_failure_diagnostic(result.stderr)
            if diagnostic is not None:
                print(
                    json.dumps(
                        {"event": "platform_stage_safe_diagnostic", **diagnostic},
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
            raise CandidateBuildError(
                _adapter_failure_code(result.stderr) or "platform_stager_rejected"
            )
        if not 1 <= len(result.stdout) <= 4096:
            raise CandidateBuildError("platform_stager_rejected")
        try:
            response = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise CandidateBuildError("platform_stager_response_invalid") from None
        if response != {"schema_version": 1, "status": "passed"}:
            raise CandidateBuildError("platform_stager_response_invalid")
        # A replacement after execution invalidates all output before receipt
        # generation; it never reaches Candidate signing.
        if hashlib.sha256(executable.read_bytes()).hexdigest() != _environment(
            "ECOREX_PLATFORM_STAGER_EXECUTABLE_SHA256"
        ):
            raise CandidateBuildError("platform_stager_changed")
        if adapter is not None and hashlib.sha256(adapter.read_bytes()).hexdigest() != adapter_digest:
            raise CandidateBuildError("platform_stager_changed")

        target = f"{args.platform}-{args.architecture}"
        stages = output / "stages" / target
        receipts = output / "receipts" / target
        receipts.mkdir(parents=True, exist_ok=False)
        inputs = (
            ("core", "core", None, "core"),
            ("bootstrap", "bootstrap", None, "bootstrap"),
        ) + tuple(
            (pack_id, f"packs/{pack_id}", pack_id, "capability-pack")
            for pack_id in PACK_TOOLS
        )
        for kind_key, relative, pack_id, receipt_kind in inputs:
            write_stage_receipt(
                source_dir=stages / relative,
                destination=receipts / f"{kind_key}.json",
                stage_id=f"{kind_key}-{target}",
                commit_sha=args.commit_sha,
                workflow_run_id=args.workflow_run_id,
                workflow_run_attempt=args.workflow_run_attempt,
                producer_executable_sha256=_environment(
                    "ECOREX_PLATFORM_STAGER_EXECUTABLE_SHA256"
                ),
                producer_adapter_sha256=adapter_digest,
                kind=receipt_kind,
                platform=args.platform,
                architecture=args.architecture,
                pack_id=pack_id,
                gate_evidence=_evidence(output, target, kind_key),
            )
        print(json.dumps({"ok": True, "target": target}, sort_keys=True))
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, CandidateBuildError) else "platform_stage_failed"
        try:
            _failure(output, code, args)
        except Exception:
            pass
        print(json.dumps({"ok": False, "code": code}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
