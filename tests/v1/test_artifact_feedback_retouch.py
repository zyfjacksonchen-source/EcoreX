from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ecorex.artifacts import (
    ArtifactAction,
    ArtifactActionUnavailable,
    ArtifactFamily,
    ArtifactNotFound,
    ArtifactService,
    ArtifactVisibility,
    FeedbackRequest,
    FeedbackSignal,
    IdempotencyConflict,
    QualityCheck,
    QualityEvidence,
    QualityStatus,
    RetouchAnnotation,
    RetouchConflict,
    RetouchJobStatus,
    RetouchRequest,
)


FIXED_NOW = datetime(2026, 7, 10, 15, 34, tzinfo=timezone(timedelta(hours=8)))
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"ecorex-test-image"


def test_feedback_is_idempotent_and_latest_signal_is_projected(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    artifact = service.create_artifact(
        b"pdf", requested_name="report.pdf", mime_type="application/pdf"
    )
    request = FeedbackRequest(
        revision_id=artifact.revision_id,
        signal=FeedbackSignal.THUMBS_UP,
        client_request_id="feedback-request-1",
    )

    first = service.record_feedback(artifact.artifact_id, request)
    duplicate = service.record_feedback(artifact.artifact_id, request)
    assert duplicate == first

    with pytest.raises(IdempotencyConflict):
        service.record_feedback(
            artifact.artifact_id,
            FeedbackRequest(
                revision_id=artifact.revision_id,
                signal=FeedbackSignal.THUMBS_DOWN,
                client_request_id="feedback-request-1",
            ),
        )

    changed = service.record_feedback(
        artifact.artifact_id,
        FeedbackRequest(
            revision_id=artifact.revision_id,
            signal=FeedbackSignal.THUMBS_DOWN,
            client_request_id="feedback-request-2",
        ),
    )
    projected = service.get_user_artifact(artifact.artifact_id)
    assert projected.feedback is not None
    assert projected.feedback.feedback_id == changed.feedback_id
    assert projected.feedback.signal is FeedbackSignal.THUMBS_DOWN


def test_feedback_cannot_expose_or_target_internal_artifact(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    source = service.create_artifact(
        b"print('secret')", requested_name="worker.py", mime_type="text/x-python"
    )
    with pytest.raises(ArtifactNotFound):
        service.record_feedback(
            source.artifact_id,
            FeedbackRequest(source.revision_id, FeedbackSignal.THUMBS_UP, "hidden-feedback"),
        )


def test_structured_retouch_creates_internal_annotation_and_new_lineaged_revision(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    image = service.create_artifact(
        b"original-image", requested_name="poster.png", mime_type="image/png"
    )
    reference = service.create_artifact(
        b"reference-image", requested_name="reference.png", mime_type="image/png"
    )
    assert ArtifactAction.PRECISE_RETOUCH in image.actions
    request = RetouchRequest(
        base_revision_id=image.revision_id,
        selected_artifact_ids=(image.artifact_id,),
        annotations=(
            RetouchAnnotation(
                kind="rectangle",
                normalized_geometry={"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2},
                instruction="把错字改正",
            ),
        ),
        reference_artifact_ids=(reference.artifact_id,),
        global_instruction="保持其他画面不变",
        client_request_id="retouch-request-1",
    )

    job = service.request_retouch(image.artifact_id, request)
    duplicate = service.request_retouch(image.artifact_id, request)
    assert duplicate == job
    assert job.status is RetouchJobStatus.QUEUED
    internal_job = service.get_internal_retouch_job(job.job_id)
    annotation = service.get_internal_artifact(internal_job.annotation_layer_artifact_id)
    assert annotation.family is ArtifactFamily.TEMPORARY
    assert annotation.visibility is ArtifactVisibility.INTERNAL
    assert annotation.actions == ()
    assert {item.artifact_id for item in service.list_user_artifacts()} == {
        image.artifact_id,
        reference.artifact_id,
    }

    running = service.mark_retouch_running(job.job_id)
    assert running.status is RetouchJobStatus.RUNNING
    result = service.complete_retouch(
        job.job_id,
        PNG_BYTES + b"retouched-image",
        mime_type="image/png",
        requested_name="poster-retouched.png",
        change_summary="已修正标注区域的错字",
        inspection_regions=[
            {
                "normalized_geometry": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2},
                "summary": "文字区域已检查",
            }
        ],
        quality_evidence=QualityEvidence(
            status=QualityStatus.PASSED,
            score=1.0,
            checks=(QualityCheck("target-region", QualityStatus.PASSED),),
        ),
    )

    assert result.job.status is RetouchJobStatus.COMPLETED
    assert result.artifact.artifact_id == image.artifact_id
    assert result.artifact.revision_id != image.revision_id
    assert result.artifact.lineage.supersedes_revision_id == image.revision_id
    assert set(result.artifact.lineage.source_artifact_ids) == {
        image.artifact_id,
        reference.artifact_id,
    }
    assert annotation.artifact_id in service.get_internal_artifact(
        image.artifact_id
    ).lineage.source_artifact_ids
    assert result.job.inspection_regions[0].summary == "文字区域已检查"
    assert result.artifact.quality_evidence.status is QualityStatus.PASSED
    assert len(service.list_user_artifacts()) == 2
    assert service.read_user_content(image.artifact_id) == PNG_BYTES + b"retouched-image"
    assert service.read_user_content(image.artifact_id, image.revision_id) == b"original-image"

    repeated = service.complete_retouch(
        job.job_id,
        PNG_BYTES + b"retouched-image",
        mime_type="image/png",
        requested_name="poster-retouched.png",
        change_summary="已修正标注区域的错字",
        inspection_regions=[
            {
                "normalized_geometry": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2},
                "summary": "文字区域已检查",
            }
        ],
        quality_evidence=QualityEvidence(
            status=QualityStatus.PASSED,
            score=1.0,
            checks=(QualityCheck("target-region", QualityStatus.PASSED),),
        ),
    )
    assert repeated.artifact.revision_id == result.artifact.revision_id
    with pytest.raises(IdempotencyConflict):
        service.complete_retouch(
            job.job_id,
            PNG_BYTES + b"different-image",
            mime_type="image/png",
            change_summary="conflicting completion",
        )

    with pytest.raises(RetouchConflict):
        service.request_retouch(
            image.artifact_id,
            RetouchRequest(
                base_revision_id=image.revision_id,
                selected_artifact_ids=(image.artifact_id,),
                global_instruction="stale edit",
                client_request_id="retouch-stale",
            ),
        )


def test_retouch_contract_rejects_bad_geometry_and_non_image_targets(tmp_path):
    with pytest.raises(ValueError, match="between 0 and 1"):
        RetouchAnnotation(
            kind="rectangle",
            normalized_geometry={"x": 1.1, "y": 0.0},
            instruction="invalid",
        )

    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    document = service.create_artifact(
        b"pdf", requested_name="document.pdf", mime_type="application/pdf"
    )
    with pytest.raises(ArtifactActionUnavailable):
        service.request_retouch(
            document.artifact_id,
            RetouchRequest(
                base_revision_id=document.revision_id,
                selected_artifact_ids=(document.artifact_id,),
                global_instruction="edit",
                client_request_id="retouch-pdf",
            ),
        )


def test_artifact_state_survives_service_restart(tmp_path):
    first = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    artifact = first.create_artifact(
        b"persisted", requested_name="persisted.pdf", mime_type="application/pdf"
    )
    first.record_feedback(
        artifact.artifact_id,
        FeedbackRequest(artifact.revision_id, FeedbackSignal.THUMBS_UP, "persisted-feedback"),
    )

    restarted = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    restored = restarted.get_user_artifact(artifact.artifact_id)
    assert restored.feedback is not None
    assert restored.feedback.signal is FeedbackSignal.THUMBS_UP
    assert restarted.read_user_content(artifact.artifact_id) == b"persisted"
