from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading

import pytest

from ecorex.artifacts import (
    ArtifactActionUnavailable,
    ArtifactFamily,
    ArtifactLineage,
    ArtifactNotFound,
    ArtifactRepository,
    ArtifactRole,
    ArtifactService,
    ArtifactVisibility,
    FeedbackRequest,
    FeedbackSignal,
    IdempotencyConflict,
    RetouchAnnotation,
    RetouchConflict,
    RetouchRequest,
)
from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.errors import SchemaVersionError


FIXED_NOW = datetime(2026, 7, 10, 15, 34, tzinfo=timezone(timedelta(hours=8)))
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"ecorex-test-image"


@pytest.mark.parametrize("name", ["worker.py ", "worker.py.", "worker\uff0epy"])
def test_canonicalized_source_suffix_cannot_be_declared_visible(tmp_path, name):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    declaration = service.issue_trusted_deliverable_declaration(
        "tests.office-export", family=ArtifactFamily.DATA_EXPORT
    )

    artifact = service.create_artifact(
        b"print('secret')",
        requested_name=name,
        mime_type="application/octet-stream",
        declaration=declaration,
    )

    assert artifact.family is ArtifactFamily.SOURCE_CODE
    assert artifact.visibility is ArtifactVisibility.INTERNAL
    assert service.list_user_artifacts() == ()


@pytest.mark.parametrize("name", ["payload.exe", ".env", "private.pem", "state.sqlite"])
def test_trusted_declaration_is_an_allowlist_not_an_unknown_format_escape(tmp_path, name):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    declaration = service.issue_trusted_deliverable_declaration(
        "tests.office-export", family=ArtifactFamily.DOCUMENT
    )

    artifact = service.create_artifact(
        b"not an office document",
        requested_name=name,
        declaration=declaration,
    )

    assert artifact.visibility is ArtifactVisibility.INTERNAL
    assert service.list_user_artifacts() == ()


@pytest.mark.parametrize(
    ("name", "mime_type"),
    [
        ("payload.exe", "text/csv"),
        ("report.pdf.exe", "application/pdf"),
        ("poster.exe", "image/png"),
        (".env", "text/csv"),
    ],
)
def test_mime_hint_cannot_relabel_an_unsafe_filename(tmp_path, name, mime_type):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    declaration = service.issue_trusted_deliverable_declaration(
        "tests.office-export"
    )
    artifact = service.create_artifact(
        b"payload",
        requested_name=name,
        mime_type=mime_type,
        declaration=declaration,
    )

    assert artifact.visibility is ArtifactVisibility.INTERNAL
    assert service.list_user_artifacts() == ()


def test_declaration_is_bound_to_issuing_service(tmp_path):
    first = ArtifactService(tmp_path / "first")
    second = ArtifactService(tmp_path / "second")
    declaration = first.issue_trusted_deliverable_declaration(
        "tests.csv-export", family=ArtifactFamily.DATA_EXPORT
    )

    with pytest.raises(ValueError, match="trusted deliverable declaration"):
        second.create_artifact(
            b"a,b\n1,2\n",
            requested_name="report.csv",
            mime_type="text/csv",
            declaration=declaration,
        )


def test_retouch_revision_cannot_downgrade_to_source_or_spoof_raster_bytes(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    image = service.create_artifact(PNG_BYTES, requested_name="poster.png", mime_type="image/png")
    job = service.request_retouch(
        image.artifact_id,
        RetouchRequest(
            base_revision_id=image.revision_id,
            selected_artifact_ids=(image.artifact_id,),
            global_instruction="fix",
            client_request_id="retouch-classification",
        ),
    )

    with pytest.raises(ArtifactActionUnavailable):
        service.complete_retouch(
            job.job_id,
            PNG_BYTES,
            mime_type="image/png",
            requested_name="worker.py",
            change_summary="must not become source",
        )
    with pytest.raises(ValueError, match="raster image content"):
        service.complete_retouch(
            job.job_id,
            b"print('not an image')",
            mime_type="image/png",
            requested_name="poster.png",
            change_summary="spoofed",
        )

    assert service.get_user_artifact(image.artifact_id).revision_id == image.revision_id


def test_retouch_rejects_internal_references_without_existence_oracle(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    image = service.create_artifact(PNG_BYTES, requested_name="poster.png", mime_type="image/png")
    secret = service.create_artifact(
        b"secret", requested_name="private.py", mime_type="text/x-python"
    )

    for reference_id in (secret.artifact_id, "art_does_not_exist"):
        with pytest.raises(ArtifactNotFound):
            service.request_retouch(
                image.artifact_id,
                RetouchRequest(
                    base_revision_id=image.revision_id,
                    selected_artifact_ids=(image.artifact_id,),
                    reference_artifact_ids=(reference_id,),
                    global_instruction="use reference",
                    client_request_id=f"reference-{reference_id}",
                ),
            )


def test_public_retouch_dto_and_lineage_hide_internal_annotation_identity(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    image = service.create_artifact(PNG_BYTES, requested_name="poster.png", mime_type="image/png")
    reference = service.create_artifact(
        PNG_BYTES + b"reference", requested_name="reference.png", mime_type="image/png"
    )
    request = RetouchRequest(
        base_revision_id=image.revision_id,
        selected_artifact_ids=(image.artifact_id,),
        reference_artifact_ids=(reference.artifact_id,),
        annotations=(
            RetouchAnnotation(
                "rectangle",
                {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2},
                "fix",
            ),
        ),
        client_request_id="public-retouch-dto",
    )
    job = service.request_retouch(image.artifact_id, request)
    internal_job = service.get_internal_retouch_job(job.job_id)
    result = service.complete_retouch(
        job.job_id,
        PNG_BYTES + b"result",
        mime_type="image/png",
        requested_name="poster-fixed.png",
        change_summary="fixed",
    )

    public_json = json.dumps(result.to_dict(), ensure_ascii=False)
    assert not hasattr(job, "annotation_layer_artifact_id")
    assert not hasattr(job, "annotation_layer_revision_id")
    assert internal_job.annotation_layer_artifact_id not in public_json
    assert internal_job.annotation_layer_revision_id not in public_json
    assert "annotation_layer_artifact_id" not in public_json
    assert "annotation_layer_revision_id" not in public_json
    assert result.artifact.lineage.source_artifact_ids == (
        image.artifact_id,
        reference.artifact_id,
    )

    internal = service.get_internal_artifact(image.artifact_id)
    assert internal_job.annotation_layer_artifact_id in internal.lineage.source_artifact_ids


def test_lineage_rejects_dangling_and_cross_artifact_supersedes(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    first = service.create_artifact(b"one", requested_name="one.pdf", mime_type="application/pdf")

    with pytest.raises(ArtifactNotFound):
        service.create_artifact(
            b"bad",
            requested_name="bad.pdf",
            mime_type="application/pdf",
            lineage=ArtifactLineage(source_artifact_ids=("art_missing",)),
        )
    with pytest.raises(RetouchConflict):
        service.create_artifact(
            b"bad",
            requested_name="bad.pdf",
            mime_type="application/pdf",
            lineage=ArtifactLineage(supersedes_revision_id=first.revision_id),
        )


def test_public_lineage_filters_valid_internal_sources(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    source = service.create_artifact(
        b"implementation", requested_name="generator.py", mime_type="text/x-python"
    )
    derived = service.create_artifact(
        b"pdf",
        requested_name="report.pdf",
        mime_type="application/pdf",
        lineage=ArtifactLineage(source_artifact_ids=(source.artifact_id,)),
    )

    assert source.artifact_id in derived.lineage.source_artifact_ids
    assert service.get_user_artifact(derived.artifact_id).lineage.source_artifact_ids == ()


def test_duplicate_retouch_request_is_atomic_across_service_instances(tmp_path):
    seed = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    image = seed.create_artifact(PNG_BYTES, requested_name="poster.png", mime_type="image/png")
    request = RetouchRequest(
        base_revision_id=image.revision_id,
        selected_artifact_ids=(image.artifact_id,),
        global_instruction="fix",
        client_request_id="concurrent-retouch",
    )
    services = [
        ArtifactService(tmp_path, clock=lambda: FIXED_NOW),
        ArtifactService(tmp_path, clock=lambda: FIXED_NOW),
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda item: item.request_retouch(image.artifact_id, request), services))

    assert len({job.job_id for job in jobs}) == 1
    intermediates = [
        artifact
        for artifact in seed.list_internal_artifacts()
        if artifact.role is ArtifactRole.INTERMEDIATE
    ]
    assert len(intermediates) == 1


def test_feedback_idempotency_is_transactional_across_service_instances(tmp_path):
    seed = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    artifact = seed.create_artifact(b"pdf", requested_name="report.pdf", mime_type="application/pdf")
    services = [
        ArtifactService(tmp_path, clock=lambda: FIXED_NOW),
        ArtifactService(tmp_path, clock=lambda: FIXED_NOW),
    ]
    request = FeedbackRequest(
        artifact.revision_id,
        FeedbackSignal.THUMBS_UP,
        "concurrent-feedback",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(
            pool.map(lambda item: item.record_feedback(artifact.artifact_id, request), services)
        )

    assert records[0] == records[1]
    with sqlite3.connect(tmp_path / "artifacts.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_feedback").fetchone()[0] == 1


def test_feedback_same_key_different_payload_conflicts_under_concurrency(tmp_path):
    seed = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    artifact = seed.create_artifact(b"pdf", requested_name="report.pdf", mime_type="application/pdf")
    services = [ArtifactService(tmp_path), ArtifactService(tmp_path)]
    barrier = threading.Barrier(2)

    def submit(index: int):
        barrier.wait()
        signal = FeedbackSignal.THUMBS_UP if index == 0 else FeedbackSignal.THUMBS_DOWN
        try:
            record = services[index].record_feedback(
                artifact.artifact_id,
                FeedbackRequest(artifact.revision_id, signal, "same-feedback-key"),
            )
            return "accepted", record.signal
        except IdempotencyConflict:
            return "conflict", signal

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))

    assert sorted(outcome[0] for outcome in outcomes) == ["accepted", "conflict"]
    with sqlite3.connect(tmp_path / "artifacts.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_feedback").fetchone()[0] == 1


def test_sqlite_name_claims_are_unique_across_repository_instances(tmp_path):
    services = [ArtifactService(tmp_path, clock=lambda: FIXED_NOW) for _ in range(12)]

    def create(index_and_service):
        index, service = index_and_service
        return service.create_artifact(
            f"pdf-{index}".encode(),
            requested_name="Report.pdf" if index % 2 == 0 else "report.pdf",
            mime_type="application/pdf",
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        artifacts = list(pool.map(create, enumerate(services)))

    assert len({artifact.display_name.casefold() for artifact in artifacts}) == len(artifacts)


def test_retouch_completion_idempotency_covers_all_metadata(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    image = service.create_artifact(PNG_BYTES, requested_name="poster.png", mime_type="image/png")
    job = service.request_retouch(
        image.artifact_id,
        RetouchRequest(
            image.revision_id,
            (image.artifact_id,),
            global_instruction="fix",
            client_request_id="completion-digest",
        ),
    )
    first = service.complete_retouch(
        job.job_id,
        PNG_BYTES + b"same",
        mime_type="image/png",
        requested_name="one.png",
        change_summary="first summary",
    )
    exact_replay = service.complete_retouch(
        job.job_id,
        PNG_BYTES + b"same",
        mime_type="image/png",
        requested_name="one.png",
        change_summary="first summary",
    )
    assert exact_replay == first

    with pytest.raises(IdempotencyConflict):
        service.complete_retouch(
            job.job_id,
            PNG_BYTES + b"same",
            mime_type="image/png",
            requested_name="one.png",
            change_summary="different summary",
        )


def test_rendition_current_revision_check_and_child_creation_are_atomic(tmp_path, monkeypatch):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    racer = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    image = service.create_artifact(PNG_BYTES, requested_name="poster.png", mime_type="image/png")
    job = racer.request_retouch(
        image.artifact_id,
        RetouchRequest(
            image.revision_id,
            (image.artifact_id,),
            global_instruction="advance",
            client_request_id="advance-before-rendition",
        ),
    )
    original = service.repository.create_and_attach_rendition

    def race(**kwargs):
        racer.complete_retouch(
            job.job_id,
            PNG_BYTES + b"advanced",
            mime_type="image/png",
            requested_name="advanced.png",
            change_summary="advanced",
        )
        return original(**kwargs)

    monkeypatch.setattr(service.repository, "create_and_attach_rendition", race)
    with pytest.raises(RetouchConflict, match="stale"):
        service.attach_rendition(
            image.artifact_id,
            content=PNG_BYTES + b"preview",
            requested_name="preview.png",
            mime_type="image/png",
            kind="preview",
        )

    assert not any(
        artifact.role is ArtifactRole.RENDITION
        for artifact in service.list_internal_artifacts()
    )


def test_display_names_fit_windows_and_macos_and_claim_casefold_uniquely(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    long_name = "\U0001f600" * 200 + ".pdf"
    long_artifact = service.create_artifact(
        b"pdf", requested_name=long_name, mime_type="application/pdf"
    )
    upper = service.create_artifact(b"a", requested_name="Report.pdf", mime_type="application/pdf")
    lower = service.create_artifact(b"b", requested_name="report.pdf", mime_type="application/pdf")

    assert len(long_artifact.display_name.encode("utf-8")) <= 255
    assert len(long_artifact.display_name.encode("utf-16-le")) // 2 <= 255
    assert upper.display_name.casefold() != lower.display_name.casefold()


def test_database_does_not_persist_caller_absolute_path(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    service.create_artifact(
        b"pdf",
        requested_name=r"C:\Users\alice\private\report.pdf",
        mime_type="application/pdf",
    )

    with sqlite3.connect(tmp_path / "artifacts.sqlite3") as connection:
        requested_name = connection.execute(
            "SELECT requested_name FROM artifact_revisions"
        ).fetchone()[0]
    assert requested_name == "report.pdf"
    assert "alice" not in requested_name


def test_pre_hardening_name_claim_schema_fails_closed_without_repair(tmp_path):
    database_path = tmp_path / "legacy.sqlite3"
    SQLiteDatabase(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            DROP TABLE artifact_display_name_claims;
            CREATE TABLE artifact_display_name_claims (
                display_name TEXT PRIMARY KEY,
                claimed_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO artifact_display_name_claims VALUES (?, ?)",
            ("Report_20260710-1534_01.pdf", "2026-07-10T07:34:00.000Z"),
        )
        before_schema = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type='table' AND name='artifact_display_name_claims'"
        ).fetchone()[0]
        before_rows = tuple(
            connection.execute(
                "SELECT display_name,claimed_at FROM artifact_display_name_claims"
            )
        )

    with pytest.raises(
        SchemaVersionError,
        match="product schema fragment artifacts is incompatible",
    ):
        ArtifactRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        after_schema = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type='table' AND name='artifact_display_name_claims'"
        ).fetchone()[0]
        after_rows = tuple(
            connection.execute(
                "SELECT display_name,claimed_at FROM artifact_display_name_claims"
            )
        )
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(artifact_display_name_claims)"
            )
        }
    assert after_schema == before_schema
    assert after_rows == before_rows
    assert "claim_key" not in columns


@pytest.mark.parametrize(
    ("kind", "geometry"),
    [
        ("rectangle", {"x": 0.1, "y": 0.1}),
        ("polygon", {"points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.2}]}),
        ("unknown", {"x": 0.1, "y": 0.1}),
    ],
)
def test_retouch_annotation_enforces_kind_specific_geometry(kind, geometry):
    with pytest.raises(ValueError):
        RetouchAnnotation(kind, geometry, "invalid")
