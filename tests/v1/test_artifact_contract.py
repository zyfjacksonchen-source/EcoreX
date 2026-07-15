from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ecorex.artifacts import (
    ArtifactAction,
    ArtifactFamily,
    ArtifactService,
    ArtifactVisibility,
    QualityCheck,
    QualityEvidence,
    QualityStatus,
)


def test_artifact_projection_contains_complete_transport_contract_without_storage_path(tmp_path):
    now = datetime(2026, 7, 10, 15, 34, tzinfo=timezone(timedelta(hours=8)))
    service = ArtifactService(tmp_path, clock=lambda: now)
    projection = service.create_artifact(
        b"pdf-content",
        requested_name="产品化方案.pdf",
        mime_type="application/pdf",
        quality_evidence=QualityEvidence(
            status=QualityStatus.PASSED,
            checks=(QualityCheck("openable", QualityStatus.PASSED, "PDF parsed"),),
            score=0.99,
            summary="ready",
        ),
    )

    payload = projection.to_dict()
    assert payload["family"] == ArtifactFamily.PDF.value
    assert payload["visibility"] == ArtifactVisibility.PRIMARY.value
    assert payload["display_name"] == "产品化方案_20260710-1534_01.pdf"
    assert payload["lineage"] == {
        "source_artifact_ids": [],
        "supersedes_revision_id": None,
    }
    assert payload["renditions"] == []
    assert payload["feedback"] is None
    assert payload["quality_evidence"]["checks"][0]["name"] == "openable"
    assert ArtifactAction.FEEDBACK.value in payload["actions"]
    assert "path" not in payload
    assert "locator" not in payload
    assert "requested_name" not in payload
