"""Fail-closed provenance for cross-runner v1 byte reproducibility.

The source evidence deliberately does not sign or construct a Candidate.  It
authenticates one protected GitHub Actions run, snapshots the four downloaded
byte contracts without following links, and delegates byte comparison to the
checked-in reproducibility gate.  A separate binder can then join that typed
evidence to an already signed Candidate and release manifest.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from typing import Any, Callable, Mapping

from ecorex.pack_catalog import REQUIRED_CAPABILITY_PACK_IDS
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseManifest,
    SignatureEnvelope,
    verify_manifest_signature,
)

from .candidate import STAGE_WORKFLOW_PATH, TARGETS, candidate_receipt_signing_payload


CI_WORKFLOW_PATH = ".github/workflows/ecorex-v1-ci.yml"
SOURCE_EVIDENCE_TYPE = "ecorex-cross-runner-reproducibility"
BOUND_EVIDENCE_TYPE = "ecorex-release-bound-reproducibility"
EXPECTED_TARGETS = ("macos-arm64", "macos-x64", "ubuntu-x64", "windows-x64")
ALLOWED_EVENTS = frozenset({"push", "workflow_dispatch"})
MAX_RUN_AGE_SECONDS = 86_400
_ARTIFACT_PREFIX = "ecorex-v1-byte-"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_METADATA_BYTES = 2 * 1024 * 1024
_MAX_CONTRACT_BYTES = 8 * 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_CANDIDATE_KEYS = {
    "schema_version",
    "receipt_type",
    "status",
    "code",
    "commit_sha",
    "staging_provenance",
    "release_id",
    "version",
    "channel",
    "build_digest",
    "python_dependency_lock_sha256",
    "manifest_sha256",
    "web_tree_sha256",
    "stage_receipts",
    "artifacts",
    "signing",
    "signature",
}


class CiReproducibilityError(ValueError):
    """A stable, non-sensitive CI provenance failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, RecursionError):
        raise CiReproducibilityError("ci_evidence_json_invalid") from None


def _strict_json(payload: bytes, *, code: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(code)
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError(code)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise CiReproducibilityError(code) from None


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IFMT(metadata.st_mode),
    )


def _directory_identity(path: Path, *, code: str) -> tuple[int, int, int, int, int, int]:
    try:
        metadata = path.lstat()
    except OSError:
        raise CiReproducibilityError(code) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
    ):
        raise CiReproducibilityError(code)
    return _identity(metadata)


def _absolute_without_following(path: Path) -> Path:
    """Return an absolute lexical path without resolving its final link."""

    return Path(os.path.abspath(path.expanduser()))


def _read_stable_regular_file(
    path: Path,
    *,
    maximum: int,
    code: str,
    parent_identity: tuple[int, int, int, int, int, int] | None = None,
) -> bytes:
    """Read one file while detecting links and path replacement.

    The parent identity is re-attested after the read.  ``O_NOFOLLOW`` closes
    the POSIX open race; Windows reparse state is checked before and after the
    handle read, and the handle/path identities must remain equal.
    """

    try:
        before = path.lstat()
    except OSError:
        raise CiReproducibilityError(code) from None
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or _is_reparse(before)
        or before.st_nlink != 1
        or not 1 <= before.st_size <= maximum
    ):
        raise CiReproducibilityError(code)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before) or _is_reparse(opened):
            raise CiReproducibilityError("ci_input_changed")
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - observed))
            if not chunk:
                break
            observed += len(chunk)
            if observed > maximum:
                raise CiReproducibilityError(code)
            chunks.append(chunk)
        after_handle = os.fstat(descriptor)
        if _identity(after_handle) != _identity(opened):
            raise CiReproducibilityError("ci_input_changed")
    except CiReproducibilityError:
        raise
    except OSError:
        raise CiReproducibilityError(code) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        after_path = path.lstat()
    except OSError:
        raise CiReproducibilityError("ci_input_changed") from None
    if _identity(after_path) != _identity(before) or _is_reparse(after_path):
        raise CiReproducibilityError("ci_input_changed")
    if parent_identity is not None and _directory_identity(
        path.parent, code="ci_input_changed"
    ) != parent_identity:
        raise CiReproducibilityError("ci_input_changed")
    payload = b"".join(chunks)
    if len(payload) != before.st_size:
        raise CiReproducibilityError("ci_input_changed")
    return payload


def _parse_time(value: Any, *, code: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CiReproducibilityError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CiReproducibilityError(code) from None
    if parsed.tzinfo is None:
        raise CiReproducibilityError(code)
    return parsed.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def validate_run_metadata(
    value: Any,
    *,
    repository: str,
    commit_sha: str,
    workflow_run_id: int,
    run_attempt: int,
    protected_ref: str,
    now: datetime,
    maximum_age: timedelta,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CiReproducibilityError("ci_run_metadata_invalid")
    if (
        _REPOSITORY.fullmatch(repository) is None
        or _COMMIT.fullmatch(commit_sha) is None
        or isinstance(workflow_run_id, bool)
        or workflow_run_id < 1
        or isinstance(run_attempt, bool)
        or run_attempt < 1
        or protected_ref != "refs/heads/main"
        or now.tzinfo is None
        or maximum_age <= timedelta(0)
        or maximum_age > timedelta(seconds=MAX_RUN_AGE_SECONDS)
    ):
        raise CiReproducibilityError("ci_expected_identity_invalid")
    branch = protected_ref.removeprefix("refs/heads/")
    repository_value = value.get("repository")
    head_repository = value.get("head_repository")
    pull_requests = value.get("pull_requests")
    event = value.get("event")
    workflow_path = value.get("path")
    repository_id = repository_value.get("id") if isinstance(repository_value, dict) else None
    head_repository_id = (
        head_repository.get("id") if isinstance(head_repository, dict) else None
    )
    if (
        isinstance(value.get("id"), bool)
        or not isinstance(value.get("id"), int)
        or value.get("id") != workflow_run_id
        or isinstance(value.get("run_attempt"), bool)
        or not isinstance(value.get("run_attempt"), int)
        or value.get("run_attempt") != run_attempt
        or value.get("head_sha") != commit_sha
        or value.get("head_branch") != branch
        or workflow_path not in {CI_WORKFLOW_PATH, f"{CI_WORKFLOW_PATH}@{branch}"}
        or value.get("status") != "completed"
        or value.get("conclusion") != "success"
        or event not in ALLOWED_EVENTS
        or not isinstance(repository_value, dict)
        or repository_value.get("full_name") != repository
        or repository_value.get("default_branch") != branch
        or repository_value.get("fork") is not False
        or isinstance(repository_id, bool)
        or not isinstance(repository_id, int)
        or repository_id < 1
        or not isinstance(head_repository, dict)
        or head_repository.get("full_name") != repository
        or head_repository.get("fork") is not False
        or isinstance(head_repository_id, bool)
        or not isinstance(head_repository_id, int)
        or head_repository_id < 1
        or pull_requests != []
    ):
        raise CiReproducibilityError("ci_run_identity_untrusted")
    if event == "push":
        head_commit = value.get("head_commit")
        if not isinstance(head_commit, dict) or head_commit.get("id") != commit_sha:
            raise CiReproducibilityError("ci_run_identity_untrusted")
    created = _parse_time(value.get("created_at"), code="ci_run_time_invalid")
    started = _parse_time(value.get("run_started_at"), code="ci_run_time_invalid")
    updated = _parse_time(value.get("updated_at"), code="ci_run_time_invalid")
    current = now.astimezone(timezone.utc)
    skew = timedelta(minutes=5)
    if (
        created > started + skew
        or started > updated
        or updated > current + skew
        or current - started > maximum_age
    ):
        raise CiReproducibilityError("ci_run_stale_or_future")
    return {
        "repository": repository,
        "repository_id": repository_id,
        "head_repository_id": head_repository_id,
        "workflow_path": CI_WORKFLOW_PATH,
        "workflow_run_id": workflow_run_id,
        "run_attempt": run_attempt,
        "event": event,
        "protected_ref": protected_ref,
        "run_started_at": _iso_z(started),
        "completed_at": _iso_z(updated),
    }


def validate_artifact_metadata(
    value: Any,
    *,
    identity: Mapping[str, Any],
    commit_sha: str,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    """Bind the exact downloaded artifact IDs to the selected CI attempt."""

    artifacts = value.get("artifacts") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != {"total_count", "artifacts"}
        or value.get("total_count") != len(EXPECTED_TARGETS)
        or not isinstance(artifacts, list)
        or len(artifacts) != len(EXPECTED_TARGETS)
        or now.tzinfo is None
    ):
        raise CiReproducibilityError("ci_artifact_metadata_invalid")
    started = _parse_time(
        identity.get("run_started_at"), code="ci_artifact_metadata_invalid"
    )
    completed = _parse_time(
        identity.get("completed_at"), code="ci_artifact_metadata_invalid"
    )
    expected_names = {_ARTIFACT_PREFIX + target for target in EXPECTED_TARGETS}
    observed_names: list[str] = []
    observed_ids: set[int] = set()
    projection: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        workflow_run = artifact.get("workflow_run") if isinstance(artifact, dict) else None
        name = artifact.get("name") if isinstance(artifact, dict) else None
        artifact_id = artifact.get("id") if isinstance(artifact, dict) else None
        digest = artifact.get("digest") if isinstance(artifact, dict) else None
        size = artifact.get("size_in_bytes") if isinstance(artifact, dict) else None
        if (
            not isinstance(artifact, dict)
            or not isinstance(name, str)
            or name not in expected_names
            or isinstance(artifact_id, bool)
            or not isinstance(artifact_id, int)
            or artifact_id < 1
            or artifact_id in observed_ids
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or _SHA256.fullmatch(digest.removeprefix("sha256:")) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
            or artifact.get("expired") is not False
            or not isinstance(workflow_run, dict)
            or workflow_run.get("id") != identity.get("workflow_run_id")
            or workflow_run.get("repository_id") != identity.get("repository_id")
            or workflow_run.get("head_repository_id")
            != identity.get("head_repository_id")
            or workflow_run.get("head_branch") != "main"
            or workflow_run.get("head_sha") != commit_sha
        ):
            raise CiReproducibilityError("ci_artifact_metadata_invalid")
        created = _parse_time(
            artifact.get("created_at"), code="ci_artifact_metadata_invalid"
        )
        updated = _parse_time(
            artifact.get("updated_at"), code="ci_artifact_metadata_invalid"
        )
        expires = _parse_time(
            artifact.get("expires_at"), code="ci_artifact_metadata_invalid"
        )
        if (
            created < started
            or updated < created
            or updated > completed + timedelta(minutes=5)
            or expires <= updated
            or expires <= now.astimezone(timezone.utc)
        ):
            raise CiReproducibilityError("ci_artifact_attempt_untrusted")
        target = name.removeprefix(_ARTIFACT_PREFIX)
        observed_names.append(name)
        observed_ids.add(artifact_id)
        projection[target] = {
            "artifact_id": artifact_id,
            "archive_sha256": digest.removeprefix("sha256:"),
            "size_bytes": size,
            "created_at": _iso_z(created),
            "updated_at": _iso_z(updated),
        }
    if (
        set(observed_names) != expected_names
        or len({name.casefold() for name in observed_names}) != len(observed_names)
        or tuple(sorted(projection)) != EXPECTED_TARGETS
    ):
        raise CiReproducibilityError("ci_artifact_target_set_invalid")
    return {target: projection[target] for target in EXPECTED_TARGETS}


def _provenance_inputs(
    *,
    run_metadata_path: Path,
    artifact_metadata_path: Path,
    repository: str,
    commit_sha: str,
    workflow_run_id: int,
    run_attempt: int,
    protected_ref: str,
    now: datetime,
    maximum_age: timedelta,
) -> tuple[dict[str, Any], bytes, dict[str, dict[str, Any]], bytes]:
    metadata_payload = _read_stable_regular_file(
        _absolute_without_following(run_metadata_path),
        maximum=_MAX_METADATA_BYTES,
        code="ci_run_metadata_invalid",
    )
    metadata = _strict_json(metadata_payload, code="ci_run_metadata_invalid")
    identity = validate_run_metadata(
        metadata,
        repository=repository,
        commit_sha=commit_sha,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        protected_ref=protected_ref,
        now=now,
        maximum_age=maximum_age,
    )
    artifact_payload = _read_stable_regular_file(
        _absolute_without_following(artifact_metadata_path),
        maximum=_MAX_METADATA_BYTES,
        code="ci_artifact_metadata_invalid",
    )
    artifact_value = _strict_json(
        artifact_payload, code="ci_artifact_metadata_invalid"
    )
    artifacts = validate_artifact_metadata(
        artifact_value,
        identity=identity,
        commit_sha=commit_sha,
        now=now,
    )
    return identity, metadata_payload, artifacts, artifact_payload


def build_artifact_selection(
    *,
    run_metadata_path: Path,
    artifact_metadata_path: Path,
    repository: str,
    commit_sha: str,
    workflow_run_id: int,
    run_attempt: int,
    protected_ref: str,
    now: datetime,
    maximum_age: timedelta,
) -> dict[str, Any]:
    identity, run_payload, artifacts, artifact_payload = _provenance_inputs(
        run_metadata_path=run_metadata_path,
        artifact_metadata_path=artifact_metadata_path,
        repository=repository,
        commit_sha=commit_sha,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        protected_ref=protected_ref,
        now=now,
        maximum_age=maximum_age,
    )
    return {
        "schema_version": 1,
        "evidence_type": "ecorex-ci-artifact-selection",
        "status": "passed",
        "commit_sha": commit_sha,
        "ci_run": identity,
        "run_metadata_sha256": hashlib.sha256(run_payload).hexdigest(),
        "artifact_metadata_sha256": hashlib.sha256(artifact_payload).hexdigest(),
        "artifacts": artifacts,
    }


def _load_comparison_gate(repository_root: Path) -> Any:
    script = repository_root / "scripts" / "check-v1-reproducibility.py"
    spec = importlib.util.spec_from_file_location(
        "ecorex_checked_in_reproducibility_gate", script
    )
    if spec is None or spec.loader is None:
        raise CiReproducibilityError("ci_comparison_gate_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        raise CiReproducibilityError("ci_comparison_gate_unavailable") from None
    return module


def _artifact_directories(root: Path) -> dict[str, Path]:
    root_identity = _directory_identity(root, code="ci_contract_root_invalid")
    try:
        entries = list(os.scandir(root))
    except OSError:
        raise CiReproducibilityError("ci_contract_root_invalid") from None
    expected_names = {_ARTIFACT_PREFIX + target for target in EXPECTED_TARGETS}
    names = [entry.name for entry in entries]
    if (
        len(names) != len(expected_names)
        or set(names) != expected_names
        or len({name.casefold() for name in names}) != len(names)
    ):
        raise CiReproducibilityError("ci_contract_target_set_invalid")
    result: dict[str, Path] = {}
    for target in EXPECTED_TARGETS:
        directory = root / f"{_ARTIFACT_PREFIX}{target}"
        _directory_identity(directory, code="ci_contract_target_invalid")
        result[target] = directory
    if _directory_identity(root, code="ci_input_changed") != root_identity:
        raise CiReproducibilityError("ci_input_changed")
    return result


def read_contracts(contracts_root: Path) -> dict[str, bytes]:
    root = _absolute_without_following(contracts_root)
    root_identity = _directory_identity(root, code="ci_contract_root_invalid")
    result: dict[str, bytes] = {}
    for target, directory in _artifact_directories(root).items():
        parent_identity = _directory_identity(directory, code="ci_contract_target_invalid")
        try:
            entries = list(os.scandir(directory))
        except OSError:
            raise CiReproducibilityError("ci_contract_target_invalid") from None
        if [entry.name for entry in entries] != ["byte-contract.json"]:
            raise CiReproducibilityError("ci_contract_contents_invalid")
        result[target] = _read_stable_regular_file(
            directory / "byte-contract.json",
            maximum=_MAX_CONTRACT_BYTES,
            code="ci_contract_invalid",
            parent_identity=parent_identity,
        )
    if _directory_identity(root, code="ci_input_changed") != root_identity:
        raise CiReproducibilityError("ci_input_changed")
    return result


def _validate_contract(value: Any) -> tuple[str, int, int]:
    if (
        not isinstance(value, dict)
        or set(value) != {"document_type", "files", "schema_version"}
        or value.get("document_type") != "ecorex.v1-byte-contract"
        or value.get("schema_version") != 1
        or not isinstance(value.get("files"), list)
        or not value["files"]
    ):
        raise CiReproducibilityError("ci_contract_schema_invalid")
    paths: set[str] = set()
    web_records: list[dict[str, Any]] = []
    web_entry: PurePosixPath | None = None
    for item in value["files"]:
        if not isinstance(item, dict) or set(item) != {
            "kind",
            "path",
            "sha256",
            "size_bytes",
        }:
            raise CiReproducibilityError("ci_contract_schema_invalid")
        kind = item.get("kind")
        raw_path = item.get("path")
        digest = item.get("sha256")
        size = item.get("size_bytes")
        if (
            not isinstance(kind, str)
            or not isinstance(raw_path, str)
            or not raw_path
            or "\\" in raw_path
            or PurePosixPath(raw_path).is_absolute()
            or any(part in {"", ".", ".."} for part in PurePosixPath(raw_path).parts)
            or raw_path in paths
            or _SHA256.fullmatch(str(digest)) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
        ):
            raise CiReproducibilityError("ci_contract_schema_invalid")
        paths.add(raw_path)
        if kind == "web-entry":
            if web_entry is not None:
                raise CiReproducibilityError("ci_web_bundle_contract_invalid")
            web_entry = PurePosixPath(raw_path)
        if kind in {"web-entry", "web-content-addressed-asset"}:
            web_records.append(item)
    if web_entry is None or web_entry.name != "index.html" or len(web_records) < 2:
        raise CiReproducibilityError("ci_web_bundle_contract_invalid")
    root = web_entry.parent
    normalized: list[dict[str, Any]] = []
    for item in web_records:
        try:
            relative = PurePosixPath(str(item["path"])).relative_to(root).as_posix()
        except ValueError:
            raise CiReproducibilityError("ci_web_bundle_contract_invalid") from None
        if relative in {"", "."}:
            raise CiReproducibilityError("ci_web_bundle_contract_invalid")
        normalized.append(
            {
                "path": relative,
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
            }
        )
    normalized.sort(key=lambda item: str(item["path"]))
    web_digest = hashlib.sha256(
        b"ecorex-candidate-stage-v1\n"
        + canonical_json_bytes(normalized).removesuffix(b"\n")
        + b"\n"
    ).hexdigest()
    return web_digest, len(normalized), sum(int(item["size_bytes"]) for item in normalized)


def compare_contracts(
    *, repository_root: Path, contracts: Mapping[str, bytes]
) -> tuple[dict[str, str], str, int, int]:
    if set(contracts) != set(EXPECTED_TARGETS):
        raise CiReproducibilityError("ci_contract_target_set_invalid")
    gate = _load_comparison_gate(repository_root.resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="ecorex-ci-contract-snapshot-") as raw:
        snapshot = Path(raw)
        for target in EXPECTED_TARGETS:
            path = snapshot / target / "byte-contract.json"
            path.parent.mkdir()
            path.write_bytes(contracts[target])
        try:
            gate.compare_contracts(snapshot, len(EXPECTED_TARGETS))
        except Exception:
            raise CiReproducibilityError("ci_contracts_not_reproducible") from None
    reference = contracts[EXPECTED_TARGETS[0]]
    value = _strict_json(reference, code="ci_contract_schema_invalid")
    web_digest, file_count, size_bytes = _validate_contract(value)
    return (
        {
            target: hashlib.sha256(contracts[target]).hexdigest()
            for target in EXPECTED_TARGETS
        },
        web_digest,
        file_count,
        size_bytes,
    )


def build_source_evidence(
    *,
    repository_root: Path,
    run_metadata_path: Path,
    artifact_metadata_path: Path,
    contracts_root: Path,
    repository: str,
    commit_sha: str,
    workflow_run_id: int,
    run_attempt: int,
    protected_ref: str,
    now: datetime,
    maximum_age: timedelta,
) -> dict[str, Any]:
    identity, metadata_payload, artifacts, artifact_payload = _provenance_inputs(
        run_metadata_path=run_metadata_path,
        artifact_metadata_path=artifact_metadata_path,
        repository=repository,
        commit_sha=commit_sha,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        protected_ref=protected_ref,
        now=now,
        maximum_age=maximum_age,
    )
    contract_digests, web_digest, web_file_count, web_size = compare_contracts(
        repository_root=repository_root,
        contracts=read_contracts(contracts_root),
    )
    return {
        "schema_version": 2,
        "evidence_type": SOURCE_EVIDENCE_TYPE,
        "status": "passed",
        "commit_sha": commit_sha,
        "ci_run": identity,
        "run_metadata_sha256": hashlib.sha256(metadata_payload).hexdigest(),
        "artifact_metadata_sha256": hashlib.sha256(artifact_payload).hexdigest(),
        "artifacts": artifacts,
        "byte_contract_sha256": contract_digests,
        "canonical_web_bundle_sha256": web_digest,
        "canonical_web_bundle_file_count": web_file_count,
        "canonical_web_bundle_size_bytes": web_size,
    }


def validate_source_evidence(value: Any) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "evidence_type",
        "status",
        "commit_sha",
        "ci_run",
        "run_metadata_sha256",
        "artifact_metadata_sha256",
        "artifacts",
        "byte_contract_sha256",
        "canonical_web_bundle_sha256",
        "canonical_web_bundle_file_count",
        "canonical_web_bundle_size_bytes",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("schema_version") != 2
        or value.get("evidence_type") != SOURCE_EVIDENCE_TYPE
        or value.get("status") != "passed"
        or _COMMIT.fullmatch(str(value.get("commit_sha"))) is None
        or _SHA256.fullmatch(str(value.get("run_metadata_sha256"))) is None
        or _SHA256.fullmatch(str(value.get("artifact_metadata_sha256"))) is None
        or _SHA256.fullmatch(str(value.get("canonical_web_bundle_sha256"))) is None
        or isinstance(value.get("canonical_web_bundle_file_count"), bool)
        or not isinstance(value.get("canonical_web_bundle_file_count"), int)
        or value["canonical_web_bundle_file_count"] < 2
        or isinstance(value.get("canonical_web_bundle_size_bytes"), bool)
        or not isinstance(value.get("canonical_web_bundle_size_bytes"), int)
        or value["canonical_web_bundle_size_bytes"] < 1
    ):
        raise CiReproducibilityError("ci_reproducibility_evidence_invalid")
    ci_run = value.get("ci_run")
    digests = value.get("byte_contract_sha256")
    artifacts = value.get("artifacts")
    if (
        not isinstance(ci_run, dict)
        or set(ci_run)
        != {
            "repository",
            "repository_id",
            "head_repository_id",
            "workflow_path",
            "workflow_run_id",
            "run_attempt",
            "event",
            "protected_ref",
            "run_started_at",
            "completed_at",
        }
        or ci_run.get("workflow_path") != CI_WORKFLOW_PATH
        or ci_run.get("event") not in ALLOWED_EVENTS
        or ci_run.get("protected_ref") != "refs/heads/main"
        or not isinstance(ci_run.get("repository"), str)
        or _REPOSITORY.fullmatch(ci_run["repository"]) is None
        or isinstance(ci_run.get("repository_id"), bool)
        or not isinstance(ci_run.get("repository_id"), int)
        or ci_run["repository_id"] < 1
        or isinstance(ci_run.get("head_repository_id"), bool)
        or not isinstance(ci_run.get("head_repository_id"), int)
        or ci_run["head_repository_id"] < 1
        or isinstance(ci_run.get("workflow_run_id"), bool)
        or not isinstance(ci_run.get("workflow_run_id"), int)
        or ci_run["workflow_run_id"] < 1
        or isinstance(ci_run.get("run_attempt"), bool)
        or not isinstance(ci_run.get("run_attempt"), int)
        or ci_run["run_attempt"] < 1
        or not isinstance(digests, dict)
        or tuple(sorted(digests)) != EXPECTED_TARGETS
        or any(_SHA256.fullmatch(str(item)) is None for item in digests.values())
        or len(set(digests.values())) != 1
        or not isinstance(artifacts, dict)
        or tuple(sorted(artifacts)) != EXPECTED_TARGETS
    ):
        raise CiReproducibilityError("ci_reproducibility_evidence_invalid")
    started = _parse_time(
        ci_run.get("run_started_at"), code="ci_reproducibility_evidence_invalid"
    )
    completed = _parse_time(
        ci_run.get("completed_at"), code="ci_reproducibility_evidence_invalid"
    )
    if completed < started:
        raise CiReproducibilityError("ci_reproducibility_evidence_invalid")
    artifact_ids: set[int] = set()
    for artifact in artifacts.values():
        if (
            not isinstance(artifact, dict)
            or set(artifact)
            != {
                "artifact_id",
                "archive_sha256",
                "size_bytes",
                "created_at",
                "updated_at",
            }
            or isinstance(artifact.get("artifact_id"), bool)
            or not isinstance(artifact.get("artifact_id"), int)
            or artifact["artifact_id"] < 1
            or _SHA256.fullmatch(str(artifact.get("archive_sha256"))) is None
            or isinstance(artifact.get("size_bytes"), bool)
            or not isinstance(artifact.get("size_bytes"), int)
            or artifact["size_bytes"] < 1
        ):
            raise CiReproducibilityError("ci_reproducibility_evidence_invalid")
        created = _parse_time(
            artifact.get("created_at"), code="ci_reproducibility_evidence_invalid"
        )
        updated = _parse_time(
            artifact.get("updated_at"), code="ci_reproducibility_evidence_invalid"
        )
        artifact_id = artifact["artifact_id"]
        if (
            artifact_id in artifact_ids
            or created < started
            or updated < created
            or updated > completed + timedelta(minutes=5)
        ):
            raise CiReproducibilityError("ci_reproducibility_evidence_invalid")
        artifact_ids.add(artifact_id)
    return value


def _trusted_public_key(encoded: str) -> bytes:
    try:
        value = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError):
        raise CiReproducibilityError("trusted_release_public_key_invalid") from None
    if len(value) != 32:
        raise CiReproducibilityError("trusted_release_public_key_invalid")
    return value


def _signed_candidate(
    *,
    candidate: dict[str, Any],
    candidate_payload: bytes,
    manifest: ReleaseManifest,
    manifest_payload: bytes,
    evidence: dict[str, Any],
    public_key: bytes,
) -> str:
    signature = candidate.get("signature")
    stage_receipts = candidate.get("stage_receipts")
    artifacts = candidate.get("artifacts")
    staging_provenance = candidate.get("staging_provenance")
    signing = candidate.get("signing")
    expected_stages = {
        f"{kind}-{platform}-{architecture}"
        for platform, architecture in TARGETS
        for kind in ("core", "bootstrap", *REQUIRED_CAPABILITY_PACK_IDS)
    }
    expected_artifacts = {
        artifact.artifact_id: {
            "file_name": artifact.file_name,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
        }
        for artifact in sorted(manifest.artifacts, key=lambda item: item.artifact_id)
    }
    manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
    if (
        set(candidate) != _CANDIDATE_KEYS
        or candidate.get("schema_version") != 2
        or candidate.get("receipt_type") != "ecorex-candidate-build"
        or candidate.get("status") != "passed"
        or candidate.get("code") is not None
        or candidate.get("commit_sha") != evidence["commit_sha"]
        or candidate.get("release_id") != manifest.release_id
        or candidate.get("version") != manifest.version
        or candidate.get("channel") != manifest.channel.value
        or candidate.get("build_digest") != manifest.build_digest
        or candidate.get("manifest_sha256") != manifest_sha256
        or candidate.get("web_tree_sha256")
        != evidence["canonical_web_bundle_sha256"]
        or _SHA256.fullmatch(str(candidate.get("python_dependency_lock_sha256")))
        is None
        or not isinstance(staging_provenance, dict)
        or set(staging_provenance)
        != {"workflow_path", "workflow_run_id", "run_attempt", "receipt_sha256"}
        or staging_provenance.get("workflow_path") != STAGE_WORKFLOW_PATH
        or isinstance(staging_provenance.get("workflow_run_id"), bool)
        or not isinstance(staging_provenance.get("workflow_run_id"), int)
        or staging_provenance["workflow_run_id"] < 1
        or isinstance(staging_provenance.get("run_attempt"), bool)
        or not isinstance(staging_provenance.get("run_attempt"), int)
        or staging_provenance["run_attempt"] < 1
        or _SHA256.fullmatch(str(staging_provenance.get("receipt_sha256"))) is None
        or not isinstance(stage_receipts, dict)
        or set(stage_receipts) != expected_stages
        or any(_SHA256.fullmatch(str(item)) is None for item in stage_receipts.values())
        or artifacts != expected_artifacts
        or not isinstance(signing, dict)
        or set(signing)
        != {
            "algorithm",
            "key_id",
            "operation_count",
            "executable_sha256",
            "adapter_sha256",
        }
        or signing.get("algorithm") != "ed25519"
        or signing.get("key_id") != manifest.signature.key_id
        or isinstance(signing.get("operation_count"), bool)
        or not isinstance(signing.get("operation_count"), int)
        or signing["operation_count"] < 1
        or _SHA256.fullmatch(str(signing.get("executable_sha256"))) is None
        or (
            signing.get("adapter_sha256") is not None
            and _SHA256.fullmatch(str(signing.get("adapter_sha256"))) is None
        )
        or not isinstance(signature, dict)
    ):
        raise CiReproducibilityError("candidate_reproducibility_binding_invalid")
    try:
        envelope = SignatureEnvelope.from_dict(signature)
        if envelope.key_id != manifest.signature.key_id:
            raise ValueError
        verifier = Ed25519SignatureVerifier({envelope.key_id: public_key})
        verifier.verify(candidate_receipt_signing_payload(candidate), envelope)
    except Exception:
        raise CiReproducibilityError("candidate_build_receipt_untrusted") from None
    # Keep the exact signed bytes in the binding rather than a normalized copy.
    return hashlib.sha256(candidate_payload).hexdigest()


def bind_to_candidate(
    *,
    evidence_path: Path,
    candidate_receipt_path: Path,
    release_manifest_path: Path,
    trusted_public_key: str,
) -> dict[str, Any]:
    evidence_payload = _read_stable_regular_file(
        _absolute_without_following(evidence_path),
        maximum=_MAX_METADATA_BYTES,
        code="ci_reproducibility_evidence_invalid",
    )
    evidence = validate_source_evidence(
        _strict_json(evidence_payload, code="ci_reproducibility_evidence_invalid")
    )
    if evidence_payload != canonical_json_bytes(evidence):
        raise CiReproducibilityError("ci_reproducibility_evidence_invalid")
    candidate_payload = _read_stable_regular_file(
        _absolute_without_following(candidate_receipt_path),
        maximum=_MAX_METADATA_BYTES,
        code="candidate_build_receipt_invalid",
    )
    candidate = _strict_json(candidate_payload, code="candidate_build_receipt_invalid")
    if not isinstance(candidate, dict):
        raise CiReproducibilityError("candidate_build_receipt_invalid")
    if candidate_payload != canonical_json_bytes(candidate):
        raise CiReproducibilityError("candidate_build_receipt_invalid")
    manifest_payload = _read_stable_regular_file(
        _absolute_without_following(release_manifest_path),
        maximum=16 * 1024 * 1024,
        code="release_manifest_untrusted",
    )
    public = _trusted_public_key(trusted_public_key)
    try:
        manifest = ReleaseManifest.from_json(manifest_payload)
        verifier = Ed25519SignatureVerifier({manifest.signature.key_id: public})
        verify_manifest_signature(manifest, verifier)
    except Exception:
        raise CiReproducibilityError("release_manifest_untrusted") from None
    candidate_sha256 = _signed_candidate(
        candidate=candidate,
        candidate_payload=candidate_payload,
        manifest=manifest,
        manifest_payload=manifest_payload,
        evidence=evidence,
        public_key=public,
    )
    return {
        "schema_version": 2,
        "evidence_type": BOUND_EVIDENCE_TYPE,
        "status": "passed",
        "commit_sha": evidence["commit_sha"],
        "ci_run": evidence["ci_run"],
        "artifact_metadata_sha256": evidence["artifact_metadata_sha256"],
        "artifacts": evidence["artifacts"],
        "reproducibility_evidence_sha256": hashlib.sha256(
            evidence_payload
        ).hexdigest(),
        "byte_contract_sha256": evidence["byte_contract_sha256"],
        "canonical_web_bundle_sha256": evidence["canonical_web_bundle_sha256"],
        "candidate_receipt_sha256": candidate_sha256,
        "release_id": manifest.release_id,
        "version": manifest.version,
        "channel": manifest.channel.value,
        "build_digest": manifest.build_digest,
        "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "web_tree_sha256": candidate["web_tree_sha256"],
        "candidate_signature_key_id": manifest.signature.key_id,
    }


def atomic_create_json(path: Path, value: Mapping[str, Any]) -> None:
    output = path.expanduser().resolve()
    if os.path.lexists(output):
        raise CiReproducibilityError("ci_evidence_output_exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as stream:
            stream.write(canonical_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except CiReproducibilityError:
        raise
    except OSError:
        raise CiReproducibilityError("ci_evidence_write_failed") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def parse_now(value: str | None) -> datetime:
    return datetime.now(timezone.utc) if value is None else _parse_time(value, code="ci_now_invalid")


__all__ = [
    "ALLOWED_EVENTS",
    "BOUND_EVIDENCE_TYPE",
    "CI_WORKFLOW_PATH",
    "CiReproducibilityError",
    "EXPECTED_TARGETS",
    "MAX_RUN_AGE_SECONDS",
    "SOURCE_EVIDENCE_TYPE",
    "atomic_create_json",
    "bind_to_candidate",
    "build_artifact_selection",
    "build_source_evidence",
    "canonical_json_bytes",
    "compare_contracts",
    "parse_now",
    "read_contracts",
    "validate_run_metadata",
    "validate_artifact_metadata",
    "validate_source_evidence",
]
