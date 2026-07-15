from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import sqlite3
import struct

from ecorex.artifacts import ArtifactService, RetouchRequest
from ecorex.artifacts.api import create_artifact_router
from ecorex.artifacts.errors import ArtifactActionUnavailable
from ecorex.artifacts.models import InspectionRegion
from ecorex.artifacts.retouch_surface import compile_annotation_mask, inspect_raster
from ecorex.integration import ArtifactEventOutbox


def png(width: int = 32, height: int = 20) -> bytes:
    return compile_annotation_mask(width, height, ()).png_bytes


def client_for(service: ArtifactService, *, coordinator=None, event_sink=None) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_artifact_router(
            service,
            retouch_coordinator=coordinator,
            event_sink=event_sink,
        )
    )
    return TestClient(app)


def open_workspace(client: TestClient, artifact) -> dict:
    response = client.post(
        f"/api/v1/artifacts/{artifact.artifact_id}/retouch-workspaces",
        json={
            "base_revision_id": artifact.revision_id,
            "client_request_id": "open-workspace",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def rectangle(annotation_id: str = "ann_rectangle") -> dict:
    return {
        "annotation_id": annotation_id,
        "kind": "rectangle",
        "normalized_geometry": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2},
        "instruction": "remove the marked object",
    }


def test_workspace_binds_exact_raster_and_survives_restart(tmp_path) -> None:
    service = ArtifactService(tmp_path)
    image = service.create_artifact(
        png(64, 40), requested_name="poster.png", mime_type="image/png"
    )
    client = client_for(service)
    opened = open_workspace(client, image)

    assert opened["edit_surface"] == {
        "base_revision_id": image.revision_id,
        "raster_digest": image.sha256,
        "width_px": 64,
        "height_px": 40,
        "orientation": 1,
        "color_space": "gray",
        "mime_type": "image/png",
        "coordinate_space_version": "oriented-normalized-v1",
    }
    surface = client.get(opened["surface_url"])
    assert surface.status_code == 200
    assert surface.content == png(64, 40)
    assert surface.headers["etag"] == f'"{image.sha256}"'

    saved = client.patch(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}",
        json={
            "expected_version": opened["version"],
            "annotations": [rectangle()],
            "reference_artifact_ids": [],
            "global_instruction": "keep every unmarked area stable",
            "view_state": {
                "zoom": 2,
                "pan_x": 0.4,
                "pan_y": 0.6,
                "selected_annotation_id": "ann_rectangle",
                "tool": "select",
            },
            "client_request_id": "save-workspace",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["mask"]["sha256"]
    assert service.blobs.exists(saved.json()["mask"]["sha256"])

    restarted = client_for(ArtifactService(tmp_path))
    restored = restarted.get(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}"
    )
    assert restored.status_code == 200
    assert restored.json()["annotations"] == [rectangle()]
    assert restored.json()["view_state"]["zoom"] == 2


def test_workspace_version_fence_and_submit_error_preserve_draft(tmp_path) -> None:
    class Unavailable:
        def request(self, artifact_id, request, *, account_id, on_persisted=None):
            del artifact_id, request, account_id, on_persisted
            raise ArtifactActionUnavailable("managed image edit is unavailable")

    service = ArtifactService(tmp_path)
    image = service.create_artifact(
        png(), requested_name="poster.png", mime_type="image/png"
    )
    client = client_for(service, coordinator=Unavailable())
    opened = open_workspace(client, image)
    payload = {
        "expected_version": opened["version"],
        "annotations": [rectangle()],
        "reference_artifact_ids": [],
        "global_instruction": "",
        "view_state": {},
        "client_request_id": "save-once",
    }
    saved = client.patch(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}", json=payload
    ).json()
    stale = client.patch(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}",
        json={**payload, "client_request_id": "save-stale"},
    )
    assert stale.status_code == 409

    failed = client.post(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}/submit",
        json={
            "expected_version": saved["version"],
            "agent_model_id": "ecorex-chat",
            "image_model_id": "gpt-image-2",
            "client_request_id": "submit-unavailable",
        },
    )
    assert failed.status_code == 409
    recovered = client.get(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}"
    ).json()
    assert recovered["status"] == "editing"
    assert recovered["annotations"] == [rectangle()]
    assert recovered["version"] == saved["version"] + 1


def test_workspace_job_completion_and_event_intent_roll_back_as_one_unit(
    tmp_path,
) -> None:
    class RejectingIntentSink:
        def persist_in_transaction(self, _connection, _event) -> None:
            raise OSError("outbox unavailable")

        async def publish_persisted(self, _event) -> None:
            raise AssertionError("a rejected intent must never publish")

    service = ArtifactService(tmp_path)
    image = service.create_artifact(
        png(), requested_name="poster.png", mime_type="image/png"
    )
    draft_client = client_for(service)
    opened = open_workspace(draft_client, image)
    saved = draft_client.patch(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}",
        json={
            "expected_version": opened["version"],
            "annotations": [rectangle()],
            "reference_artifact_ids": [],
            "global_instruction": "",
            "view_state": {},
            "client_request_id": "workspace-atomic-save",
        },
    ).json()
    submit = {
        "expected_version": saved["version"],
        "agent_model_id": "ecorex-chat",
        "image_model_id": "gpt-image-2",
        "client_request_id": "workspace-atomic-submit",
    }

    rejected = client_for(service, event_sink=RejectingIntentSink()).post(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}/submit",
        json=submit,
    )
    assert rejected.status_code == 503
    current = service.get_retouch_workspace(opened["workspace_id"])
    assert current.status.value == "editing"
    assert current.submitted_job_id is None
    with sqlite3.connect(service.repository.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_retouch_jobs"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_event_outbox"
        ).fetchone()[0] == 0

    durable = ArtifactEventOutbox(service.repository.database)
    retried = client_for(service, event_sink=durable).post(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}/submit",
        json={**submit, "expected_version": current.version},
    )
    assert retried.status_code == 202
    completed = service.get_retouch_workspace(opened["workspace_id"])
    assert completed.status.value == "submitted"
    assert completed.submitted_job_id == retried.json()["submitted_job_id"]
    with sqlite3.connect(service.repository.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_retouch_jobs"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_event_outbox"
        ).fetchone()[0] == 1


def test_restart_recovers_interrupted_workspace_submission_claim(tmp_path) -> None:
    service = ArtifactService(tmp_path)
    image = service.create_artifact(
        png(), requested_name="poster.png", mime_type="image/png"
    )
    client = client_for(service)
    opened = open_workspace(client, image)
    saved = client.patch(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}",
        json={
            "expected_version": opened["version"],
            "annotations": [rectangle()],
            "reference_artifact_ids": [],
            "global_instruction": "",
            "view_state": {},
            "client_request_id": "save-restart",
        },
    ).json()
    service.claim_retouch_workspace_submission(
        opened["workspace_id"],
        expected_version=saved["version"],
        client_request_id="claim-without-job",
    )

    restarted = ArtifactService(tmp_path)
    projected = restarted.get_retouch_workspace(opened["workspace_id"])
    assert projected.status.value == "submitting"
    assert projected.submitted_job_id is None

    def close_epoch_before_commit() -> None:
        raise RuntimeError("execution epoch closed")

    with pytest.raises(RuntimeError, match="execution epoch closed"):
        restarted.recover_interrupted_retouch_workspace_submissions(
            before_commit=close_epoch_before_commit
        )
    assert restarted.get_retouch_workspace(opened["workspace_id"]).status.value == (
        "submitting"
    )
    assert restarted.recover_interrupted_retouch_workspace_submissions() == 1
    recovered = restarted.get_retouch_workspace(opened["workspace_id"])
    assert recovered.status.value == "editing"
    claimed = restarted.claim_retouch_workspace_submission(
        opened["workspace_id"],
        expected_version=recovered.version,
        client_request_id="claim-with-job",
    )
    job = restarted.request_retouch(
        image.artifact_id,
        RetouchRequest(
            base_revision_id=image.revision_id,
            selected_artifact_ids=(image.artifact_id,),
            annotations=claimed.annotations,
            client_request_id="claim-with-job",
            edit_surface=claimed.edit_surface.to_dict(),
            mask=claimed.mask,
        ),
    )

    restarted_with_job = ArtifactService(tmp_path)
    projected_with_job = restarted_with_job.get_retouch_workspace(opened["workspace_id"])
    assert projected_with_job.status.value == "submitting"
    assert restarted_with_job.recover_interrupted_retouch_workspace_submissions() == 1
    recovered_with_job = restarted_with_job.get_retouch_workspace(opened["workspace_id"])
    assert recovered_with_job.status.value == "submitted"
    assert recovered_with_job.submitted_job_id == job.job_id


def test_reference_limit_revision_pin_and_exact_preview(tmp_path) -> None:
    service = ArtifactService(tmp_path)
    target = service.create_artifact(
        png(), requested_name="target.png", mime_type="image/png"
    )
    references = [
        service.create_artifact(
            png(8 + index, 8),
            requested_name=f"reference-{index}.png",
            mime_type="image/png",
        )
        for index in range(11)
    ]
    client = client_for(service)
    opened = open_workspace(client, target)
    too_many = client.patch(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}",
        json={
            "expected_version": opened["version"],
            "annotations": [rectangle()],
            "reference_artifact_ids": [item.artifact_id for item in references],
            "global_instruction": "",
            "view_state": {},
            "client_request_id": "eleven-references",
        },
    )
    assert too_many.status_code == 422

    saved_response = client.patch(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}",
        json={
            "expected_version": opened["version"],
            "annotations": [rectangle()],
            "reference_artifact_ids": [item.artifact_id for item in references[:10]],
            "global_instruction": "",
            "view_state": {},
            "client_request_id": "ten-references",
        },
    )
    assert saved_response.status_code == 200, saved_response.text
    saved = saved_response.json()
    assert len(saved["references"]) == 10
    exact = client.get(saved["references"][0]["preview_url"])
    assert exact.content == png(8, 8)

    reference = references[0]
    change = service.request_retouch(
        reference.artifact_id,
        RetouchRequest(
            base_revision_id=reference.revision_id,
            selected_artifact_ids=(reference.artifact_id,),
            global_instruction="change reference",
            client_request_id="advance-reference",
        ),
    )
    service.complete_retouch(
        change.job_id,
        png(9, 9),
        mime_type="image/png",
        change_summary="advanced",
    )
    conflict = client.post(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}/submit",
        json={
            "expected_version": saved["version"],
            "agent_model_id": "ecorex-chat",
            "image_model_id": "gpt-image-2",
            "client_request_id": "submit-with-stale-reference",
        },
    )
    assert conflict.status_code == 409
    assert client.get(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}"
    ).json()["status"] == "editing"


def test_workspace_submit_is_idempotent_and_projects_result_beside_original(tmp_path) -> None:
    service = ArtifactService(tmp_path)
    image = service.create_artifact(
        png(40, 24), requested_name="poster.png", mime_type="image/png"
    )
    client = client_for(service)
    opened = open_workspace(client, image)
    saved = client.patch(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}",
        json={
            "expected_version": opened["version"],
            "annotations": [rectangle()],
            "reference_artifact_ids": [],
            "global_instruction": "",
            "view_state": {},
            "client_request_id": "save-before-submit",
        },
    ).json()
    submit_body = {
        "expected_version": saved["version"],
        "agent_model_id": "ecorex-chat",
        "image_model_id": "gpt-image-2",
        "client_request_id": "submit-idempotent",
    }
    first = client.post(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}/submit",
        json=submit_body,
    )
    duplicate = client.post(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}/submit",
        json=submit_body,
    )
    assert first.status_code == 202, first.text
    assert duplicate.status_code == 202
    assert duplicate.json()["submitted_job_id"] == first.json()["submitted_job_id"]

    job_id = first.json()["submitted_job_id"]
    internal = service.get_internal_retouch_job(job_id)
    assert internal.request.edit_surface["raster_digest"] == image.sha256
    assert internal.request.mask["coordinate_space_version"] == "oriented-normalized-v1"
    assert "path" not in internal.request.to_dict()["edit_surface"]
    assert "bytes" not in internal.request.to_dict()["mask"]
    service.mark_retouch_running(job_id)
    result_bytes = png(40, 24) + b"retouched-result"
    service.complete_retouch(
        job_id,
        result_bytes,
        mime_type="image/png",
        change_summary="removed the marked object",
        inspection_regions=[
            {
                "normalized_geometry": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2},
                "summary": "target region checked",
            }
        ],
    )
    completed = client.get(
        f"/api/v1/retouch-workspaces/{opened['workspace_id']}"
    ).json()
    assert completed["job"]["status"] == "completed"
    assert completed["result"]["revision_id"] != image.revision_id
    assert completed["result_surface"]["raster_digest"] == completed["result"]["sha256"]
    assert completed["result"]["lineage"]["supersedes_revision_id"] == image.revision_id
    assert client.get(completed["surface_url"]).content == png(40, 24)
    assert client.get(completed["result_url"]).content == result_bytes


def test_all_geometry_kinds_compile_to_one_bounded_deterministic_mask() -> None:
    annotations = [
        rectangle(),
        {
            "annotation_id": "ann_ellipse",
            "kind": "ellipse",
            "normalized_geometry": {"x": 0.5, "y": 0.1, "width": 0.2, "height": 0.3},
            "instruction": "ellipse",
        },
        {
            "annotation_id": "ann_point",
            "kind": "point",
            "normalized_geometry": {"x": 0.2, "y": 0.8},
            "instruction": "point",
        },
        {
            "annotation_id": "ann_polygon",
            "kind": "polygon",
            "normalized_geometry": {"points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.1}, {"x": 0.15, "y": 0.2}]},
            "instruction": "polygon",
        },
        {
            "annotation_id": "ann_polyline",
            "kind": "polyline",
            "normalized_geometry": {"points": [{"x": 0.4, "y": 0.4}, {"x": 0.6, "y": 0.5}]},
            "instruction": "polyline",
        },
        {
            "annotation_id": "ann_brush",
            "kind": "brush",
            "normalized_geometry": {"points": [{"x": 0.7, "y": 0.7}, {"x": 0.9, "y": 0.8}], "width": 0.03},
            "instruction": "brush",
        },
    ]
    first = compile_annotation_mask(10_000, 5_000, annotations)
    second = compile_annotation_mask(10_000, 5_000, annotations)
    assert first.sha256 == second.sha256
    assert first.width_px <= 2048
    assert first.width_px * first.height_px <= 4_194_304
    assert 0 < first.covered_fraction < 1
    assert inspect_raster(first.png_bytes, "image/png").width_px == first.width_px


def test_inspection_regions_reject_untyped_geometry() -> None:
    with pytest.raises(ValueError, match="inspection region geometry"):
        InspectionRegion(
            normalized_geometry={"left": 0.1, "top": 0.2},
            summary="ambiguous",
        )
    with pytest.raises(ValueError, match="unsupported fields"):
        RetouchRequest(
            base_revision_id="rev_one",
            selected_artifact_ids=("art_one",),
            global_instruction="change",
            client_request_id="invalid-surface",
            edit_surface={
                "base_revision_id": "rev_one",
                "raster_digest": "a" * 64,
                "width_px": 10,
                "height_px": 10,
                "orientation": 1,
                "color_space": "srgb",
                "mime_type": "image/png",
                "coordinate_space_version": "oriented-normalized-v1",
                "path": "C:/secret.png",
            },
        )


def test_jpeg_exif_orientation_is_part_of_the_canonical_surface() -> None:
    tiff = (
        b"II"
        + struct.pack("<H", 42)
        + struct.pack("<I", 8)
        + struct.pack("<H", 1)
        + struct.pack("<HHI", 0x0112, 3, 1)
        + struct.pack("<H", 6)
        + b"\x00\x00"
        + b"\x00\x00\x00\x00"
    )
    exif = b"Exif\x00\x00" + tiff
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    frame = b"\x08" + struct.pack(">HHB", 2, 3, 3) + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    sof = b"\xff\xc0" + struct.pack(">H", len(frame) + 2) + frame
    descriptor = inspect_raster(b"\xff\xd8" + app1 + sof + b"\xff\xd9", "image/jpeg")
    assert descriptor.orientation == 6
    assert (descriptor.width_px, descriptor.height_px) == (2, 3)
    assert descriptor.color_space == "srgb"
