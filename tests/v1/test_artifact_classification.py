from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from ecorex.artifacts import (
    ArtifactFamily,
    ArtifactNotFound,
    ArtifactRole,
    ArtifactService,
    ArtifactVisibility,
)


FIXED_NOW = datetime(2026, 7, 10, 15, 34, tzinfo=timezone(timedelta(hours=8)))


@pytest.mark.parametrize(
    ("name", "mime_type", "role"),
    [
        ("automation.py", "text/x-python", ArtifactRole.DELIVERABLE),
        ("frontend.ts", "application/typescript", ArtifactRole.DELIVERABLE),
        ("bootstrap.sh", "text/x-shellscript", ArtifactRole.DELIVERABLE),
        ("changes.diff", "text/x-diff", ArtifactRole.DELIVERABLE),
        ("runtime.log", "text/x-log", ArtifactRole.DELIVERABLE),
        ("turn.state", "application/octet-stream", ArtifactRole.DELIVERABLE),
        ("diagnostic.pdf", "application/pdf", ArtifactRole.DIAGNOSTIC),
        ("document-source.pdf", "application/pdf", ArtifactRole.SOURCE),
    ],
)
def test_implementation_and_diagnostic_files_have_zero_user_projection_leakage(
    tmp_path, name, mime_type, role
):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    declaration = (
        service.issue_trusted_deliverable_declaration(
            "tests.office-export", family=ArtifactFamily.DOCUMENT
        )
        if role is ArtifactRole.DELIVERABLE
        else None
    )
    internal = service.create_artifact(
        b"private implementation detail",
        requested_name=name,
        mime_type=mime_type,
        role=role,
        requested_visibility=ArtifactVisibility.PRIMARY,
        declaration=declaration,
    )

    assert internal.visibility is ArtifactVisibility.INTERNAL
    assert internal.actions == ()
    assert service.list_user_artifacts() == ()
    with pytest.raises(ArtifactNotFound):
        service.get_user_artifact(internal.artifact_id)
    assert name not in json.dumps(
        [item.to_dict() for item in service.list_user_artifacts()], ensure_ascii=False
    )


@pytest.mark.parametrize(
    ("name", "mime_type", "family"),
    [
        ("brief.md", "text/markdown", ArtifactFamily.DOCUMENT),
        ("records.csv", "text/csv", ArtifactFamily.DATA_EXPORT),
        ("records.json", "application/json", ArtifactFamily.DATA_EXPORT),
        ("report.html", "text/html", ArtifactFamily.WEB_REPORT),
        ("delivery.zip", "application/zip", ArtifactFamily.ARCHIVE),
    ],
)
def test_ambiguous_formats_require_trusted_explicit_deliverable_declaration(
    tmp_path, name, mime_type, family
):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    implicit = service.create_artifact(
        b"payload", requested_name=name, mime_type=mime_type
    )
    declaration = service.issue_trusted_deliverable_declaration(
        "tests.office-export", family=family
    )
    explicit = service.create_artifact(
        b"payload",
        requested_name=f"final-{name}",
        mime_type=mime_type,
        declaration=declaration,
    )

    assert implicit.visibility is ArtifactVisibility.INTERNAL
    assert explicit.visibility is ArtifactVisibility.PRIMARY
    assert explicit.family is family
    assert [item.artifact_id for item in service.list_user_artifacts()] == [explicit.artifact_id]


def test_rendition_is_nested_and_never_duplicates_the_user_artifact(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    document = service.create_artifact(
        b"office-document",
        requested_name="proposal.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    updated = service.attach_rendition(
        document.artifact_id,
        content=b"preview-png",
        requested_name="proposal-preview.png",
        mime_type="image/png",
        kind="preview",
        family_hint=ArtifactFamily.IMAGE,
    )

    assert len(updated.renditions) == 1
    assert updated.renditions[0].kind.value == "preview"
    assert len(service.list_user_artifacts()) == 1
    audit_rows = service.list_internal_artifacts()
    rendition = next(item for item in audit_rows if item.role is ArtifactRole.RENDITION)
    assert rendition.visibility is ArtifactVisibility.INTERNAL
