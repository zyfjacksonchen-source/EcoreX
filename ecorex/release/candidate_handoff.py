"""Immutable handoff from protected Candidate acceptance to publication.

Publication is intentionally a separate administrator action.  This module
authenticates the exact successful Candidate workflow attempt and the exact
accepted artifact ID before any release origin or Control Plane is mutated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .evidence_io import read_stable_regular_file, write_new_json_file


CANDIDATE_WORKFLOW_PATH = ".github/workflows/ecorex-v1-candidate.yml"
CANDIDATE_HANDOFF_TYPE = "ecorex-accepted-candidate-handoff"
MAX_HANDOFF_AGE_SECONDS = 30 * 24 * 60 * 60

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CHANNELS = frozenset({"canary", "stable"})
_MAX_METADATA_BYTES = 4 * 1024 * 1024
_HANDOFF_KEYS = {
    "schema_version",
    "receipt_type",
    "status",
    "repository",
    "repository_id",
    "head_repository_id",
    "workflow_path",
    "workflow_run_id",
    "run_attempt",
    "event",
    "protected_ref",
    "commit_sha",
    "channel",
    "run_started_at",
    "completed_at",
    "artifact_id",
    "artifact_name",
    "artifact_archive_sha256",
    "artifact_size_bytes",
    "artifact_created_at",
    "artifact_updated_at",
    "artifact_expires_at",
    "run_metadata_sha256",
    "artifact_metadata_sha256",
}


class CandidateHandoffError(ValueError):
    """Stable, non-sensitive rejected-handoff code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
        raise CandidateHandoffError(code) from None


def _read_json(path: Path, *, code: str) -> tuple[Any, bytes]:
    try:
        payload = read_stable_regular_file(
            path,
            maximum_bytes=_MAX_METADATA_BYTES,
            code=code,
        )
    except ValueError:
        raise CandidateHandoffError(code) from None
    return _strict_json(payload, code=code), payload


def _time(value: Any, *, code: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CandidateHandoffError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CandidateHandoffError(code) from None
    if parsed.tzinfo is None:
        raise CandidateHandoffError(code)
    return parsed.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _positive_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def _validate_expected(
    *,
    repository: str,
    commit_sha: str,
    workflow_run_id: int,
    run_attempt: int,
    artifact_id: int,
    channel: str,
    protected_ref: str,
    now: datetime,
    maximum_age: timedelta,
) -> None:
    if (
        _REPOSITORY.fullmatch(repository) is None
        or _COMMIT.fullmatch(commit_sha) is None
        or not _positive_integer(workflow_run_id)
        or not _positive_integer(run_attempt)
        or not _positive_integer(artifact_id)
        or channel not in _CHANNELS
        or protected_ref != "refs/heads/main"
        or now.tzinfo is None
        or maximum_age <= timedelta(0)
        or maximum_age > timedelta(seconds=MAX_HANDOFF_AGE_SECONDS)
    ):
        raise CandidateHandoffError("candidate_handoff_expected_identity_invalid")


def _validate_run(
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
    repository_value = value.get("repository") if isinstance(value, dict) else None
    head_repository = value.get("head_repository") if isinstance(value, dict) else None
    repository_id = (
        repository_value.get("id") if isinstance(repository_value, dict) else None
    )
    head_repository_id = (
        head_repository.get("id") if isinstance(head_repository, dict) else None
    )
    branch = protected_ref.removeprefix("refs/heads/")
    workflow_path = value.get("path") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or not _positive_integer(value.get("id"))
        or value.get("id") != workflow_run_id
        or not _positive_integer(value.get("run_attempt"))
        or value.get("run_attempt") != run_attempt
        or value.get("head_sha") != commit_sha
        or value.get("head_branch") != branch
        or workflow_path
        not in {CANDIDATE_WORKFLOW_PATH, f"{CANDIDATE_WORKFLOW_PATH}@{branch}"}
        or value.get("event") != "workflow_dispatch"
        or value.get("status") != "completed"
        or value.get("conclusion") != "success"
        or value.get("pull_requests") != []
        or not isinstance(repository_value, dict)
        or repository_value.get("full_name") != repository
        or repository_value.get("default_branch") != branch
        or repository_value.get("fork") is not False
        or not _positive_integer(repository_id)
        or not isinstance(head_repository, dict)
        or head_repository.get("full_name") != repository
        or head_repository.get("fork") is not False
        or not _positive_integer(head_repository_id)
    ):
        raise CandidateHandoffError("candidate_handoff_run_untrusted")
    created = _time(value.get("created_at"), code="candidate_handoff_run_time_invalid")
    started = _time(
        value.get("run_started_at"), code="candidate_handoff_run_time_invalid"
    )
    completed = _time(value.get("updated_at"), code="candidate_handoff_run_time_invalid")
    current = now.astimezone(timezone.utc)
    skew = timedelta(minutes=5)
    if (
        created > started + skew
        or started > completed
        or completed > current + skew
        or current - started > maximum_age
    ):
        raise CandidateHandoffError("candidate_handoff_run_stale_or_future")
    return {
        "repository_id": repository_id,
        "head_repository_id": head_repository_id,
        "run_started_at": started,
        "completed_at": completed,
    }


def _validate_artifact(
    value: Any,
    *,
    repository_id: int,
    head_repository_id: int,
    commit_sha: str,
    workflow_run_id: int,
    artifact_id: int,
    channel: str,
    started: datetime,
    completed: datetime,
    now: datetime,
) -> dict[str, Any]:
    artifacts = value.get("artifacts") if isinstance(value, dict) else None
    expected_name = f"ecorex-v1-accepted-{channel}"
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("total_count"), int)
        or isinstance(value.get("total_count"), bool)
        or not isinstance(artifacts, list)
        or value.get("total_count") != len(artifacts)
    ):
        raise CandidateHandoffError("candidate_handoff_artifact_metadata_invalid")
    named = [
        item
        for item in artifacts
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"].casefold() == expected_name.casefold()
    ]
    accepted: list[dict[str, Any]] = []
    for item in named:
        item_created = _time(
            item.get("created_at"), code="candidate_handoff_artifact_time_invalid"
        )
        item_updated = _time(
            item.get("updated_at"), code="candidate_handoff_artifact_time_invalid"
        )
        # GitHub's run-level artifact listing can include a superseded attempt.
        # Only artifacts uploaded during the exact authenticated attempt are
        # eligible; duplicate names inside that attempt remain fatal.
        if started <= item_created <= item_updated <= completed + timedelta(minutes=5):
            accepted.append(item)
    if len(accepted) != 1:
        raise CandidateHandoffError("candidate_handoff_artifact_ambiguous")
    artifact = accepted[0]
    workflow = artifact.get("workflow_run")
    digest = artifact.get("digest")
    size = artifact.get("size_in_bytes")
    if (
        not _positive_integer(artifact.get("id"))
        or artifact.get("id") != artifact_id
        or artifact.get("name") != expected_name
        or artifact.get("expired") is not False
        or not isinstance(digest, str)
        or not digest.startswith("sha256:")
        or _SHA256.fullmatch(digest.removeprefix("sha256:")) is None
        or not _positive_integer(size)
        or not isinstance(workflow, dict)
        or not _positive_integer(workflow.get("id"))
        or workflow.get("id") != workflow_run_id
        or workflow.get("repository_id") != repository_id
        or workflow.get("head_repository_id") != head_repository_id
        or workflow.get("head_branch") != "main"
        or workflow.get("head_sha") != commit_sha
    ):
        raise CandidateHandoffError("candidate_handoff_artifact_untrusted")
    created = _time(
        artifact.get("created_at"), code="candidate_handoff_artifact_time_invalid"
    )
    updated = _time(
        artifact.get("updated_at"), code="candidate_handoff_artifact_time_invalid"
    )
    expires = _time(
        artifact.get("expires_at"), code="candidate_handoff_artifact_time_invalid"
    )
    current = now.astimezone(timezone.utc)
    if (
        created < started
        or updated < created
        or updated > completed + timedelta(minutes=5)
        or expires <= updated
        or expires <= current
    ):
        raise CandidateHandoffError("candidate_handoff_artifact_stale_or_future")
    return {
        "name": expected_name,
        "archive_sha256": digest.removeprefix("sha256:"),
        "size_bytes": size,
        "created_at": created,
        "updated_at": updated,
        "expires_at": expires,
    }


def build_candidate_handoff(
    *,
    run_metadata_path: Path,
    artifact_metadata_path: Path,
    repository: str,
    commit_sha: str,
    workflow_run_id: int,
    run_attempt: int,
    artifact_id: int,
    channel: str,
    protected_ref: str = "refs/heads/main",
    now: datetime,
    maximum_age: timedelta = timedelta(seconds=MAX_HANDOFF_AGE_SECONDS),
) -> dict[str, Any]:
    """Build a typed handoff for exactly one accepted Candidate artifact."""

    _validate_expected(
        repository=repository,
        commit_sha=commit_sha,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        artifact_id=artifact_id,
        channel=channel,
        protected_ref=protected_ref,
        now=now,
        maximum_age=maximum_age,
    )
    run, run_payload = _read_json(
        run_metadata_path, code="candidate_handoff_run_metadata_invalid"
    )
    artifact_set, artifact_payload = _read_json(
        artifact_metadata_path,
        code="candidate_handoff_artifact_metadata_invalid",
    )
    identity = _validate_run(
        run,
        repository=repository,
        commit_sha=commit_sha,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        protected_ref=protected_ref,
        now=now,
        maximum_age=maximum_age,
    )
    artifact = _validate_artifact(
        artifact_set,
        repository_id=identity["repository_id"],
        head_repository_id=identity["head_repository_id"],
        commit_sha=commit_sha,
        workflow_run_id=workflow_run_id,
        artifact_id=artifact_id,
        channel=channel,
        started=identity["run_started_at"],
        completed=identity["completed_at"],
        now=now,
    )
    return {
        "schema_version": 1,
        "receipt_type": CANDIDATE_HANDOFF_TYPE,
        "status": "passed",
        "repository": repository,
        "repository_id": identity["repository_id"],
        "head_repository_id": identity["head_repository_id"],
        "workflow_path": CANDIDATE_WORKFLOW_PATH,
        "workflow_run_id": workflow_run_id,
        "run_attempt": run_attempt,
        "event": "workflow_dispatch",
        "protected_ref": protected_ref,
        "commit_sha": commit_sha,
        "channel": channel,
        "run_started_at": _iso_z(identity["run_started_at"]),
        "completed_at": _iso_z(identity["completed_at"]),
        "artifact_id": artifact_id,
        "artifact_name": artifact["name"],
        "artifact_archive_sha256": artifact["archive_sha256"],
        "artifact_size_bytes": artifact["size_bytes"],
        "artifact_created_at": _iso_z(artifact["created_at"]),
        "artifact_updated_at": _iso_z(artifact["updated_at"]),
        "artifact_expires_at": _iso_z(artifact["expires_at"]),
        "run_metadata_sha256": hashlib.sha256(run_payload).hexdigest(),
        "artifact_metadata_sha256": hashlib.sha256(artifact_payload).hexdigest(),
    }


def write_candidate_handoff(value: Mapping[str, Any], output: Path) -> Path:
    """Write a new immutable handoff receipt."""

    if set(value) != _HANDOFF_KEYS:
        raise CandidateHandoffError("candidate_handoff_receipt_invalid")
    try:
        return write_new_json_file(value, output, code="candidate_handoff_output_exists")
    except ValueError as exc:
        raise CandidateHandoffError(str(exc)) from None


def validate_candidate_handoff(
    value: Any,
    *,
    repository: str,
    commit_sha: str,
    workflow_run_id: int,
    run_attempt: int,
    artifact_id: int,
    channel: str,
) -> dict[str, Any]:
    """Revalidate the typed receipt after artifact extraction."""

    if (
        not isinstance(value, dict)
        or set(value) != _HANDOFF_KEYS
        or value.get("schema_version") != 1
        or value.get("receipt_type") != CANDIDATE_HANDOFF_TYPE
        or value.get("status") != "passed"
        or value.get("repository") != repository
        or value.get("workflow_path") != CANDIDATE_WORKFLOW_PATH
        or not _positive_integer(value.get("workflow_run_id"))
        or value.get("workflow_run_id") != workflow_run_id
        or not _positive_integer(value.get("run_attempt"))
        or value.get("run_attempt") != run_attempt
        or value.get("event") != "workflow_dispatch"
        or value.get("protected_ref") != "refs/heads/main"
        or value.get("commit_sha") != commit_sha
        or value.get("channel") != channel
        or not _positive_integer(value.get("artifact_id"))
        or value.get("artifact_id") != artifact_id
        or value.get("artifact_name") != f"ecorex-v1-accepted-{channel}"
        or not _positive_integer(value.get("repository_id"))
        or not _positive_integer(value.get("head_repository_id"))
        or not _positive_integer(value.get("artifact_size_bytes"))
        or any(
            _SHA256.fullmatch(str(value.get(key))) is None
            for key in (
                "artifact_archive_sha256",
                "run_metadata_sha256",
                "artifact_metadata_sha256",
            )
        )
    ):
        raise CandidateHandoffError("candidate_handoff_receipt_invalid")
    started = _time(value.get("run_started_at"), code="candidate_handoff_receipt_invalid")
    completed = _time(value.get("completed_at"), code="candidate_handoff_receipt_invalid")
    created = _time(
        value.get("artifact_created_at"), code="candidate_handoff_receipt_invalid"
    )
    updated = _time(
        value.get("artifact_updated_at"), code="candidate_handoff_receipt_invalid"
    )
    expires = _time(
        value.get("artifact_expires_at"), code="candidate_handoff_receipt_invalid"
    )
    if not (started <= created <= updated <= completed + timedelta(minutes=5) < expires):
        raise CandidateHandoffError("candidate_handoff_receipt_invalid")
    return dict(value)


def load_candidate_handoff(
    path: Path,
    *,
    repository: str,
    commit_sha: str,
    workflow_run_id: int,
    run_attempt: int,
    artifact_id: int,
    channel: str,
) -> dict[str, Any]:
    value, _payload = _read_json(path, code="candidate_handoff_receipt_invalid")
    return validate_candidate_handoff(
        value,
        repository=repository,
        commit_sha=commit_sha,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        artifact_id=artifact_id,
        channel=channel,
    )


__all__ = [
    "CANDIDATE_HANDOFF_TYPE",
    "CANDIDATE_WORKFLOW_PATH",
    "MAX_HANDOFF_AGE_SECONDS",
    "CandidateHandoffError",
    "build_candidate_handoff",
    "load_candidate_handoff",
    "validate_candidate_handoff",
    "write_candidate_handoff",
]
