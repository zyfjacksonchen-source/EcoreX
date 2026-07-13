from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import sys
import zipfile

import pytest

from ecorex.control_plane.cli import PromotionJournal
from ecorex.release.candidate_handoff import (
    CANDIDATE_WORKFLOW_PATH,
    CandidateHandoffError,
    build_candidate_handoff,
    load_candidate_handoff,
    write_candidate_handoff,
)


COMMIT = "a" * 40
RUN_ID = 7001
ATTEMPT = 2
ARTIFACT_ID = 8801
REPOSITORY = "ecorex/ecorex"
NOW = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)


def _module(name: str, relative: str):
    path = Path(__file__).resolve().parents[2] / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, value: object) -> Path:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _run() -> dict[str, object]:
    return {
        "id": RUN_ID,
        "run_attempt": ATTEMPT,
        "head_sha": COMMIT,
        "head_branch": "main",
        "path": CANDIDATE_WORKFLOW_PATH,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "pull_requests": [],
        "created_at": "2026-07-14T12:00:00Z",
        "run_started_at": "2026-07-14T12:00:05Z",
        "updated_at": "2026-07-14T13:00:00Z",
        "repository": {
            "id": 41,
            "full_name": REPOSITORY,
            "default_branch": "main",
            "fork": False,
        },
        "head_repository": {
            "id": 41,
            "full_name": REPOSITORY,
            "fork": False,
        },
    }


def _accepted(*, artifact_id: int = ARTIFACT_ID, name: str = "ecorex-v1-accepted-stable") -> dict[str, object]:
    return {
        "id": artifact_id,
        "name": name,
        "size_in_bytes": 4096,
        "digest": "sha256:" + "b" * 64,
        "expired": False,
        "created_at": "2026-07-14T12:50:00Z",
        "updated_at": "2026-07-14T12:51:00Z",
        "expires_at": "2026-08-13T12:51:00Z",
        "workflow_run": {
            "id": RUN_ID,
            "repository_id": 41,
            "head_repository_id": 41,
            "head_branch": "main",
            "head_sha": COMMIT,
        },
    }


def _artifacts() -> dict[str, object]:
    values = [
        _accepted(),
        _accepted(artifact_id=7701, name="ecorex-v1-candidate-stable"),
    ]
    return {"total_count": len(values), "artifacts": values}


def _build(tmp_path: Path) -> dict[str, object]:
    return build_candidate_handoff(
        run_metadata_path=_write(tmp_path / "run.json", _run()),
        artifact_metadata_path=_write(tmp_path / "artifacts.json", _artifacts()),
        repository=REPOSITORY,
        commit_sha=COMMIT,
        workflow_run_id=RUN_ID,
        run_attempt=ATTEMPT,
        artifact_id=ARTIFACT_ID,
        channel="stable",
        now=NOW,
        maximum_age=timedelta(days=30),
    )


def test_handoff_binds_exact_successful_attempt_and_artifact_id(tmp_path: Path) -> None:
    handoff = _build(tmp_path)
    assert handoff["workflow_run_id"] == RUN_ID
    assert handoff["run_attempt"] == ATTEMPT
    assert handoff["artifact_id"] == ARTIFACT_ID
    assert handoff["artifact_archive_sha256"] == "b" * 64
    assert handoff["commit_sha"] == COMMIT

    receipt = write_candidate_handoff(handoff, tmp_path / "handoff.json")
    loaded = load_candidate_handoff(
        receipt,
        repository=REPOSITORY,
        commit_sha=COMMIT,
        workflow_run_id=RUN_ID,
        run_attempt=ATTEMPT,
        artifact_id=ARTIFACT_ID,
        channel="stable",
    )
    assert loaded == handoff


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda run, artifacts: run.__setitem__("conclusion", "failure"), "candidate_handoff_run_untrusted"),
        (lambda run, artifacts: run.__setitem__("head_sha", "c" * 40), "candidate_handoff_run_untrusted"),
        (lambda run, artifacts: run.__setitem__("run_attempt", ATTEMPT + 1), "candidate_handoff_run_untrusted"),
        (lambda run, artifacts: artifacts["artifacts"][0].__setitem__("id", ARTIFACT_ID + 1), "candidate_handoff_artifact_untrusted"),
        (lambda run, artifacts: artifacts["artifacts"][0].__setitem__("expired", True), "candidate_handoff_artifact_untrusted"),
        (
            lambda run, artifacts: artifacts["artifacts"].append(_accepted(artifact_id=ARTIFACT_ID + 2)),
            "candidate_handoff_artifact_metadata_invalid",
        ),
    ],
)
def test_handoff_fails_closed_on_foreign_failed_or_ambiguous_input(
    tmp_path: Path, mutation, code: str
) -> None:
    run = _run()
    artifacts = _artifacts()
    mutation(run, artifacts)
    with pytest.raises(CandidateHandoffError, match=code):
        build_candidate_handoff(
            run_metadata_path=_write(tmp_path / "run.json", run),
            artifact_metadata_path=_write(tmp_path / "artifacts.json", artifacts),
            repository=REPOSITORY,
            commit_sha=COMMIT,
            workflow_run_id=RUN_ID,
            run_attempt=ATTEMPT,
            artifact_id=ARTIFACT_ID,
            channel="stable",
            now=NOW,
        )


def test_handoff_rejects_duplicate_accepted_name(tmp_path: Path) -> None:
    artifacts = _artifacts()
    artifacts["artifacts"].append(_accepted(artifact_id=ARTIFACT_ID + 1))
    artifacts["total_count"] = len(artifacts["artifacts"])
    with pytest.raises(CandidateHandoffError, match="candidate_handoff_artifact_ambiguous"):
        build_candidate_handoff(
            run_metadata_path=_write(tmp_path / "run.json", _run()),
            artifact_metadata_path=_write(tmp_path / "artifacts.json", artifacts),
            repository=REPOSITORY,
            commit_sha=COMMIT,
            workflow_run_id=RUN_ID,
            run_attempt=ATTEMPT,
            artifact_id=ARTIFACT_ID,
            channel="stable",
            now=NOW,
        )


def test_promotion_request_ids_survive_lost_local_journal(tmp_path: Path) -> None:
    arguments = (
        "release-stable-000000000000000000000001",
        "a" * 64,
        "publication-receipt:sha256:" + "b" * 64,
        "c" * 64,
        "d" * 64,
        None,
    )
    first = PromotionJournal(tmp_path / "first.json", *arguments)
    second = PromotionJournal(tmp_path / "second.json", *arguments)
    assert first.request_id("candidate.create") == second.request_id("candidate.create")
    assert first.request_id("rollout.create") == second.request_id("rollout.create")
    assert first.request_id("candidate.create") != first.request_id("rollout.create")

    changed_target = PromotionJournal(
        tmp_path / "changed.json", *arguments[:3], "e" * 64, *arguments[4:]
    )
    assert changed_target.request_id("rollout.create") != first.request_id("rollout.create")


def _zip(path: Path, entries: list[tuple[str, bytes, int | None]]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload, mode in entries:
            if mode is None:
                archive.writestr(name, payload)
            else:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                archive.writestr(info, payload)
    return path


def test_workflow_artifact_extractor_requires_exact_digest_and_safe_roots(
    tmp_path: Path,
) -> None:
    extractor = _module(
        "ecorex_v1_workflow_artifact_extractor",
        "scripts/extract-v1-workflow-artifact.py",
    )
    archive = _zip(
        tmp_path / "candidate.zip",
        [
            ("output/release/release-manifest.json", b"{}\n", None),
            ("gates/live-model.json", b"{}\n", None),
        ],
    )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    output = tmp_path / "candidate"
    result = extractor.extract_workflow_artifact(
        archive,
        expected_sha256=digest,
        output=output,
        required_roots=("output", "gates"),
    )
    assert result["archive_sha256"] == digest
    assert result["file_count"] == 2
    assert (output / "output/release/release-manifest.json").read_bytes() == b"{}\n"

    with pytest.raises(extractor.ArtifactExtractionError, match="digest_mismatch"):
        extractor.extract_workflow_artifact(
            archive,
            expected_sha256="0" * 64,
            output=tmp_path / "bad-digest",
            required_roots=("output", "gates"),
        )


@pytest.mark.parametrize(
    "entries",
    [
        [("../escape.txt", b"x", None), ("output/a", b"x", None), ("gates/a", b"x", None)],
        [("output/A", b"x", None), ("output/a", b"y", None), ("gates/a", b"x", None)],
        [
            ("output/link", b"target", stat.S_IFLNK | 0o777),
            ("gates/a", b"x", None),
        ],
        [("output/a", b"x", None), ("unexpected/a", b"x", None), ("gates/a", b"x", None)],
    ],
)
def test_workflow_artifact_extractor_rejects_unsafe_members(
    tmp_path: Path, entries: list[tuple[str, bytes, int | None]]
) -> None:
    extractor = _module(
        "ecorex_v1_workflow_artifact_extractor_" + str(abs(hash(str(entries)))),
        "scripts/extract-v1-workflow-artifact.py",
    )
    archive = _zip(tmp_path / "unsafe.zip", entries)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(extractor.ArtifactExtractionError):
        extractor.extract_workflow_artifact(
            archive,
            expected_sha256=digest,
            output=tmp_path / "output",
            required_roots=("output", "gates"),
        )
    assert not (tmp_path / "escape.txt").exists()
