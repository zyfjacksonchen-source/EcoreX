#!/usr/bin/env python3
"""Build one exact-main Candidate under an explicit direct-release waiver.

This command does not deploy anything.  It reuses the production Candidate
builder, requires all three real platform-stage trees/receipts, signs with the
current-user DPAPI adapter, and writes a separately signed waiver adjacent to
the immutable release directory.  The waiver says protected gates were not
run; it is never accepted as a protected-pipeline PASS.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release import (  # noqa: E402
    CandidateBuildError,
    DigestPinnedExternalSigner,
    DirectReleaseWaiverError,
    build_candidate,
    build_direct_release_waiver,
    parse_external_public_key_description,
)
from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    strict_json_loads,
    write_new_json_file,
)
from ecorex.release.process_boundary import (  # noqa: E402
    BoundedProcessError,
    run_bounded_process,
)
from ecorex.release.models import WebBundleBuildInput  # noqa: E402
from ecorex.release.web_bundle import scan_web_bundle  # noqa: E402
from ecorex.update import ReleaseManifest  # noqa: E402


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ADAPTER = ROOT / "scripts" / "ecorex-v1-dpapi-ed25519-signer.py"
_TARGETS = ("windows-x64", "macos-arm64", "macos-x64")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "build one signed stable Candidate from exact main and real "
            "Windows/macOS stages, recording explicit protected-gate waiver"
        )
    )
    parser.add_argument("--recipe", required=True, type=Path)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--web-dist", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--waiver", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-staging-run-id", required=True, type=int)
    parser.add_argument("--staging-provenance", required=True, type=Path)
    parser.add_argument("--dependency-lock-manifest", required=True, type=Path)
    parser.add_argument("--operator-instruction-sha256", required=True)
    parser.add_argument("--publication-key-description", required=True, type=Path)
    parser.add_argument("--delta-base-release-dir", type=Path)
    return parser


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*arguments: str, allow_missing: bool = False) -> str | None:
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=ROOT,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            ),
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("direct_release_git_unavailable") from None
    if result.returncode != 0:
        if allow_missing:
            return None
        raise ValueError("direct_release_git_identity_invalid")
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ValueError("direct_release_git_identity_invalid") from None


def _assert_exact_main(expected_commit: str) -> None:
    if _COMMIT.fullmatch(expected_commit) is None:
        raise ValueError("direct_release_commit_invalid")
    head = _git("rev-parse", "HEAD")
    local_main = _git("rev-parse", "refs/heads/main")
    remote_main = _git("rev-parse", "refs/remotes/origin/main")
    branch = _git("branch", "--show-current")
    if (
        head != expected_commit
        or local_main != expected_commit
        or remote_main != expected_commit
        or branch != "main"
    ):
        raise ValueError("direct_release_exact_main_required")
    status = _git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    assert status is not None
    for entry in status.split("\0"):
        if not entry:
            continue
        if entry.startswith("?? .artifacts/") and (ROOT / ".artifacts").is_dir():
            continue
        raise ValueError("direct_release_worktree_not_exact")


def _describe_adapter() -> tuple[dict[str, Any], str, str]:
    executable = Path(sys.executable).resolve(strict=True)
    adapter = _ADAPTER.resolve(strict=True)
    executable_sha256 = _sha256_file(executable)
    adapter_sha256 = _sha256_file(adapter)
    try:
        result = run_bounded_process(
            (str(executable), str(adapter), "describe"),
            payload=None,
            cwd=ROOT,
            environment=os.environ,
            timeout_seconds=15,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=1024,
            hide_window=os.name == "nt",
        )
    except (OSError, BoundedProcessError):
        raise ValueError("direct_release_key_store_unavailable") from None
    if result.returncode != 0 or not result.stdout:
        raise ValueError("direct_release_key_store_unavailable")
    value = strict_json_loads(
        result.stdout, code="direct_release_key_description_invalid"
    )
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "release", "publication", "status"}
        or value.get("schema_version") != 1
        or value.get("status") != "ready"
    ):
        raise ValueError("direct_release_key_description_invalid")
    if (
        _sha256_file(executable) != executable_sha256
        or _sha256_file(adapter) != adapter_sha256
    ):
        raise ValueError("direct_release_signer_changed")
    return value, executable_sha256, adapter_sha256


def _key(value: Any, role: str) -> tuple[str, bytes]:
    if not isinstance(value, dict) or set(value) != {
        "algorithm",
        "key_id",
        "public_key_base64",
        "public_key_sha256",
        "protection",
        "created_at",
    }:
        raise ValueError("direct_release_key_description_invalid")
    if value.get("algorithm") != "ed25519" or value.get("protection") != (
        "windows-dpapi-current-user"
    ):
        raise ValueError("direct_release_key_description_invalid")
    try:
        public = base64.b64decode(value.get("public_key_base64"), validate=True)
    except (TypeError, ValueError):
        raise ValueError("direct_release_key_description_invalid") from None
    key_id = value.get("key_id")
    if (
        not isinstance(key_id, str)
        or len(public) != 32
        or hashlib.sha256(public).hexdigest() != value.get("public_key_sha256")
        or not key_id.startswith(f"ecorex-direct-{role}-")
    ):
        raise ValueError("direct_release_key_description_invalid")
    return key_id, public


def _release_signer() -> DigestPinnedExternalSigner:
    description, executable_sha256, adapter_sha256 = _describe_adapter()
    release_id, release_public = _key(description.get("release"), "release")
    configuration = {
        "executable_path": str(Path(sys.executable).resolve(strict=True)),
        "executable_sha256": executable_sha256,
        "adapter_path": str(_ADAPTER.resolve(strict=True)),
        "adapter_sha256": adapter_sha256,
        "environment": os.environ,
    }
    return DigestPinnedExternalSigner(
        key_id=release_id,
        public_key=release_public,
        **configuration,
    )


def _external_publication_key(path: Path) -> tuple[str, bytes]:
    payload = read_stable_regular_file(
        path,
        maximum_bytes=64 * 1024,
        code="direct_release_public_key_description_invalid",
    )
    value = strict_json_loads(
        payload, code="direct_release_public_key_description_invalid"
    )
    if not isinstance(value, dict):
        raise ValueError("direct_release_public_key_description_invalid")
    try:
        return parse_external_public_key_description(
            value, expected_role="publication"
        )
    except DirectReleaseWaiverError:
        raise ValueError("direct_release_public_key_description_invalid") from None


def _assert_stage_publication_key(
    root: Path, publication_key_id: str, publication_public_key: bytes
) -> None:
    expected = {
        publication_key_id: base64.b64encode(publication_public_key).decode("ascii")
    }
    for target in _TARGETS:
        path = root / "stages" / target / "bootstrap" / "bootstrap-config.json"
        payload = read_stable_regular_file(
            path,
            maximum_bytes=256 * 1024,
            code="direct_release_stage_bootstrap_config_invalid",
        )
        value = strict_json_loads(
            payload, code="direct_release_stage_bootstrap_config_invalid"
        )
        if not isinstance(value, dict) or value.get("publication_public_keys") != expected:
            raise ValueError("direct_release_stage_publication_key_mismatch")


def _assert_stage_web_bundle(root: Path, web_dist: Path) -> None:
    try:
        expected_digest = scan_web_bundle(
            WebBundleBuildInput(web_dist.resolve(strict=True))
        ).bundle_sha256
    except Exception:
        raise ValueError("direct_release_web_bundle_invalid") from None
    for target in _TARGETS:
        receipt_payload = read_stable_regular_file(
            root / "receipts" / target / "core.json",
            maximum_bytes=2 * 1024 * 1024,
            code="direct_release_stage_core_receipt_invalid",
        )
        receipt = strict_json_loads(
            receipt_payload, code="direct_release_stage_core_receipt_invalid"
        )
        evidence_payload = read_stable_regular_file(
            root / ".evidence" / target / "core" / "dependency-closure.json",
            maximum_bytes=4 * 1024 * 1024,
            code="direct_release_stage_web_evidence_invalid",
        )
        evidence = strict_json_loads(
            evidence_payload, code="direct_release_stage_web_evidence_invalid"
        )
        if not isinstance(receipt, dict) or not isinstance(evidence, dict):
            raise ValueError("direct_release_stage_web_evidence_invalid")
        gates = receipt.get("gates")
        details = evidence.get("details")
        web = details.get("web_bundle") if isinstance(details, dict) else None
        dependency_gate = (
            gates.get("dependency-closure") if isinstance(gates, dict) else None
        )
        if (
            evidence.get("schema_version") != 1
            or evidence.get("status") != "passed"
            or evidence.get("gate") != "dependency-closure"
            or not isinstance(dependency_gate, dict)
            or dependency_gate.get("status") != "passed"
            or dependency_gate.get("evidence_sha256")
            != hashlib.sha256(evidence_payload).hexdigest()
            or not isinstance(web, dict)
            or web.get("bundle_sha256") != expected_digest
        ):
            raise ValueError("direct_release_stage_web_bundle_mismatch")


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _assert_exact_main(args.expected_commit)
        if _SHA256.fullmatch(args.operator_instruction_sha256) is None:
            raise ValueError("direct_release_instruction_hash_invalid")
        input_root = args.input_root.resolve(strict=True)
        release_signer = _release_signer()
        publication_key_id, publication_public_key = _external_publication_key(
            args.publication_key_description
        )
        if (
            release_signer.key_id == publication_key_id
            or release_signer.public_key_bytes == publication_public_key
        ):
            raise ValueError("direct_release_keys_not_independent")
        _assert_stage_publication_key(
            input_root, publication_key_id, publication_public_key
        )
        _assert_stage_web_bundle(input_root, args.web_dist)
        built = build_candidate(
            recipe_path=args.recipe,
            input_root=input_root,
            web_dist=args.web_dist,
            destination=args.output,
            receipt_path=args.receipt,
            expected_commit=args.expected_commit,
            expected_workflow_run_id=args.expected_staging_run_id,
            staging_provenance_path=args.staging_provenance,
            dependency_lock_manifest_path=args.dependency_lock_manifest,
            signer=release_signer,
            delta_base_release_dir=args.delta_base_release_dir,
        )
        manifest_bytes = read_stable_regular_file(
            built.manifest_path,
            maximum_bytes=16 * 1024 * 1024,
            code="direct_release_manifest_invalid",
        )
        manifest = ReleaseManifest.from_json(manifest_bytes)
        receipt_bytes = read_stable_regular_file(
            args.receipt,
            maximum_bytes=4 * 1024 * 1024,
            code="direct_release_candidate_receipt_invalid",
        )
        receipt = strict_json_loads(
            receipt_bytes, code="direct_release_candidate_receipt_invalid"
        )
        if not isinstance(receipt, dict):
            raise ValueError("direct_release_candidate_receipt_invalid")
        waiver = build_direct_release_waiver(
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            candidate_receipt=receipt,
            candidate_receipt_bytes=receipt_bytes,
            commit_sha=args.expected_commit,
            operator_instruction_sha256=args.operator_instruction_sha256,
            signer=release_signer,
            signer_public_key=release_signer.public_key_bytes,
            publication_key_id=publication_key_id,
            publication_public_key=publication_public_key,
        )
        waiver_path = write_new_json_file(
            waiver,
            args.waiver,
            code="direct_release_waiver_output_exists",
        )
        waiver_sha256 = _sha256_file(waiver_path)
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "candidate-signed-with-explicit-operator-waiver",
                    "protected_pipeline_passed": False,
                    "release_id": manifest.release_id,
                    "version": manifest.version,
                    "build_digest": manifest.build_digest,
                    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    "candidate_receipt_sha256": hashlib.sha256(
                        receipt_bytes
                    ).hexdigest(),
                    "waiver_sha256": waiver_sha256,
                    "release_key_id": release_signer.key_id,
                    "publication_key_id": publication_key_id,
                    "publication_completed": False,
                    "live_pointer_authorized": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except Exception as exc:
        if isinstance(exc, CandidateBuildError):
            code = exc.code
        elif isinstance(exc, DirectReleaseWaiverError):
            code = str(exc)
        elif isinstance(exc, ValueError) and re.fullmatch(
            r"[a-z][a-z0-9_]{2,127}", str(exc)
        ):
            code = str(exc)
        else:
            code = "direct_release_build_failed"
        print(json.dumps({"ok": False, "code": code}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
