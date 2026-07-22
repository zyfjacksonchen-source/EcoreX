from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

from ecorex.artifacts import (
    ArtifactFamily,
    ArtifactService,
    FeedbackSignal,
    RenditionKind,
)
from ecorex.artifacts.api import ArtifactApiEvent, create_artifact_router
from ecorex.runtime import RuntimeExecutionDenied


FIXED_NOW = datetime(2026, 7, 10, 15, 34, tzinfo=timezone(timedelta(hours=8)))
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"api-test-image"


class RecordingEventSink:
    def __init__(self) -> None:
        self.persisted: dict[str, ArtifactApiEvent] = {}
        self.published: list[str] = []
        self.calls: list[str] = []
        self.fail_once = False

    def persist_in_transaction(self, _connection, event: ArtifactApiEvent) -> None:
        prior = self.persisted.get(event.idempotency_key)
        if prior is not None:
            assert prior.to_dict() == event.to_dict()
            return
        self.persisted[event.idempotency_key] = event

    async def publish_persisted(self, event: ArtifactApiEvent) -> None:
        self.calls.append(event.idempotency_key)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("event store unavailable")
        if event.idempotency_key in self.published:
            return
        self.published.append(event.idempotency_key)


class DeniedEventSink:
    def persist_in_transaction(self, _connection, _event: ArtifactApiEvent) -> None:
        raise RuntimeExecutionDenied("execution epoch closed")

    async def publish_persisted(self, _event: ArtifactApiEvent) -> None:
        raise RuntimeExecutionDenied("execution epoch closed")


def make_client(tmp_path, *, sink: RecordingEventSink | None = None):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    app = FastAPI()
    app.include_router(create_artifact_router(service, event_sink=sink))
    return service, TestClient(app)


def assert_stable_error(response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert isinstance(response.json()["error"]["message"], str)


def test_user_list_and_get_never_project_internal_artifacts(tmp_path):
    service, client = make_client(tmp_path)
    visible = service.create_artifact(
        b"pdf", requested_name="report.pdf", mime_type="application/pdf"
    )
    internal = service.create_artifact(
        b"print('secret')", requested_name="worker.py", mime_type="text/x-python"
    )

    response = client.get("/api/v1/artifacts")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert [item["artifact_id"] for item in response.json()["items"]] == [visible.artifact_id]
    wire = json.dumps(response.json(), ensure_ascii=False)
    assert internal.artifact_id not in wire
    assert "worker.py" not in wire
    assert '"internal"' not in wire
    assert "requested_name" not in wire
    assert "path" not in wire

    assert client.get(f"/api/v1/artifacts/{visible.artifact_id}").json()["artifact_id"] == visible.artifact_id
    assert_stable_error(
        client.get(f"/api/v1/artifacts/{internal.artifact_id}"),
        404,
        "ARTIFACT_NOT_FOUND",
    )


def test_content_and_preview_are_user_scoped_and_never_expose_storage_paths(tmp_path):
    service, client = make_client(tmp_path)
    document = service.create_artifact(
        b"office-document",
        requested_name=r"C:\private\proposal.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    service.attach_rendition(
        document.artifact_id,
        content=PNG_BYTES,
        requested_name="proposal-preview.png",
        mime_type="image/png",
        kind="preview",
        family_hint=ArtifactFamily.IMAGE,
    )
    internal = service.create_artifact(
        b"secret", requested_name="secret.py", mime_type="text/x-python"
    )

    content = client.get(f"/api/v1/artifacts/{document.artifact_id}/content")
    assert content.status_code == 200
    assert content.content == b"office-document"
    assert content.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "proposal_" in content.headers["content-disposition"]
    assert "C:" not in content.headers["content-disposition"]
    assert "private" not in content.headers["content-disposition"]
    assert "x-ecorex-path" not in content.headers

    preview = client.get(f"/api/v1/artifacts/{document.artifact_id}/preview")
    assert preview.status_code == 200
    assert preview.content == PNG_BYTES
    assert preview.headers["content-type"].startswith("image/png")

    assert_stable_error(
        client.get(f"/api/v1/artifacts/{internal.artifact_id}/content"),
        404,
        "ARTIFACT_NOT_FOUND",
    )
    assert_stable_error(
        client.get(f"/api/v1/artifacts/{internal.artifact_id}/preview"),
        404,
        "ARTIFACT_NOT_FOUND",
    )


def test_image_thumbnail_and_preview_are_bounded_persisted_and_idempotent(tmp_path):
    from PIL import Image

    service, client = make_client(tmp_path)
    source = io.BytesIO()
    Image.new("RGB", (2400, 1600), (25, 90, 210)).save(source, format="PNG")
    image = service.create_artifact(
        source.getvalue(),
        requested_name="campaign.png",
        mime_type="image/png",
    )

    thumbnail = client.get(f"/api/v1/artifacts/{image.artifact_id}/thumbnail")
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"].startswith("image/jpeg")
    assert len(thumbnail.content) <= 64 * 1024
    with Image.open(io.BytesIO(thumbnail.content)) as decoded:
        assert max(decoded.size) <= 320

    preview = client.get(f"/api/v1/artifacts/{image.artifact_id}/preview")
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("image/jpeg")
    assert len(preview.content) <= 2 * 1024 * 1024
    with Image.open(io.BytesIO(preview.content)) as decoded:
        assert max(decoded.size) <= 1600

    first = service.get_user_artifact(image.artifact_id)
    assert {item.kind for item in first.renditions} == {
        RenditionKind.THUMBNAIL,
        RenditionKind.PREVIEW,
    }
    service.ensure_image_renditions(
        image.artifact_id,
        revision_id=image.revision_id,
    )
    with service.repository.database.reader() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_renditions WHERE parent_revision_id = ?",
            (image.revision_id,),
        ).fetchone()[0] == 2


def test_concurrent_rendition_recovery_has_one_authority_and_no_orphans(tmp_path):
    from PIL import Image

    service, _client = make_client(tmp_path)
    source = io.BytesIO()
    Image.new("RGB", (640, 480), (25, 90, 210)).save(source, format="PNG")
    image = service.create_artifact(
        source.getvalue(), requested_name="parallel.png", mime_type="image/png"
    )

    def ensure(_index: int):
        return service.ensure_image_renditions(
            image.artifact_id,
            revision_id=image.revision_id,
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        projections = list(executor.map(ensure, range(100)))

    expected = {
        item.kind: item.sha256
        for item in service.get_user_artifact(image.artifact_id).renditions
    }
    assert set(expected) == {RenditionKind.THUMBNAIL, RenditionKind.PREVIEW}
    assert all(
        {item.kind: item.sha256 for item in projection.renditions} == expected
        for projection in projections
    )
    with service.repository.database.reader() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_renditions WHERE parent_revision_id = ?",
            (image.revision_id,),
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_entities WHERE role = 'rendition'"
        ).fetchone()[0] == 2


def test_preview_unavailable_uses_stable_action_error(tmp_path):
    service, client = make_client(tmp_path)
    declaration = service.issue_trusted_deliverable_declaration(
        "tests.archive", family=ArtifactFamily.ARCHIVE
    )
    archive = service.create_artifact(
        b"PK-test",
        requested_name="delivery.zip",
        mime_type="application/zip",
        declaration=declaration,
    )

    assert_stable_error(
        client.get(f"/api/v1/artifacts/{archive.artifact_id}/preview"),
        409,
        "ARTIFACT_ACTION_UNAVAILABLE",
    )


def test_content_response_fail_closes_invalid_persisted_media_type(tmp_path):
    service, client = make_client(tmp_path)
    artifact = service.create_artifact(
        b"pdf",
        requested_name="report.pdf",
        mime_type="application/pdf\r\nX-Injected: yes",
    )

    response = client.get(f"/api/v1/artifacts/{artifact.artifact_id}/content")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert "x-injected" not in response.headers


def test_feedback_api_is_idempotent_and_event_sink_receives_stable_public_event(tmp_path):
    sink = RecordingEventSink()
    service, client = make_client(tmp_path, sink=sink)
    artifact = service.create_artifact(
        b"pdf", requested_name="report.pdf", mime_type="application/pdf"
    )
    payload = {
        "revision_id": artifact.revision_id,
        "signal": FeedbackSignal.THUMBS_UP.value,
        "client_request_id": "api-feedback-1",
    }

    first = client.post(f"/api/v1/artifacts/{artifact.artifact_id}/feedback", json=payload)
    duplicate = client.post(f"/api/v1/artifacts/{artifact.artifact_id}/feedback", json=payload)
    assert first.status_code == 200
    assert duplicate.json() == first.json()
    assert len(sink.persisted) == 1
    assert sink.published == [f"artifact.feedback:{artifact.artifact_id}:api-feedback-1"]
    event = next(iter(sink.persisted.values()))
    event_wire = json.dumps(event.to_dict(), ensure_ascii=False)
    assert event.event_type == "artifact.feedback.recorded"
    assert "internal" not in event_wire
    assert "path" not in event_wire

    conflict_payload = {**payload, "signal": FeedbackSignal.THUMBS_DOWN.value}
    assert_stable_error(
        client.post(
            f"/api/v1/artifacts/{artifact.artifact_id}/feedback",
            json=conflict_payload,
        ),
        409,
        "ARTIFACT_IDEMPOTENCY_CONFLICT",
    )


def test_structured_retouch_and_job_api_only_return_public_projection(tmp_path):
    sink = RecordingEventSink()
    service, client = make_client(tmp_path, sink=sink)
    image = service.create_artifact(PNG_BYTES, requested_name="poster.png", mime_type="image/png")
    request = {
        "base_revision_id": image.revision_id,
        "selected_artifact_ids": [image.artifact_id],
        "agent_model_id": "ecorex-chat",
        "image_model_id": "gpt-image-2",
        "annotations": [
            {
                "kind": "rectangle",
                "normalized_geometry": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2},
                "instruction": "fix typo",
            }
        ],
        "reference_artifact_ids": [],
        "global_instruction": "keep the rest unchanged",
        "client_request_id": "api-retouch-1",
    }

    first = client.post(f"/api/v1/artifacts/{image.artifact_id}/retouch", json=request)
    duplicate = client.post(f"/api/v1/artifacts/{image.artifact_id}/retouch", json=request)
    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert duplicate.json() == first.json()
    job_id = first.json()["job_id"]
    internal_job = service.get_internal_retouch_job(job_id)
    wire = json.dumps(first.json(), ensure_ascii=False)
    assert internal_job.annotation_layer_artifact_id not in wire
    assert internal_job.annotation_layer_revision_id not in wire
    assert "annotation_layer" not in wire

    fetched = client.get(f"/api/v1/retouch-jobs/{job_id}")
    assert fetched.status_code == 200
    assert fetched.json() == first.json()
    assert len(sink.persisted) == 1
    event = next(iter(sink.persisted.values()))
    assert event.event_type == "artifact.retouch.requested"
    assert internal_job.annotation_layer_artifact_id not in json.dumps(event.to_dict())

    assert_stable_error(
        client.get("/api/v1/retouch-jobs/rtj_missing"),
        404,
        "ARTIFACT_NOT_FOUND",
    )


def test_api_rejects_internal_retouch_reference_and_invalid_body_stably(tmp_path):
    service, client = make_client(tmp_path)
    image = service.create_artifact(PNG_BYTES, requested_name="poster.png", mime_type="image/png")
    secret = service.create_artifact(
        b"secret", requested_name="worker.py", mime_type="text/x-python"
    )
    payload = {
        "base_revision_id": image.revision_id,
        "selected_artifact_ids": [image.artifact_id],
        "agent_model_id": "ecorex-chat",
        "image_model_id": "gpt-image-2",
        "annotations": [],
        "reference_artifact_ids": [secret.artifact_id],
        "global_instruction": "use secret",
        "client_request_id": "api-internal-reference",
    }

    assert_stable_error(
        client.post(f"/api/v1/artifacts/{image.artifact_id}/retouch", json=payload),
        404,
        "ARTIFACT_NOT_FOUND",
    )
    assert_stable_error(
        client.post(
            f"/api/v1/artifacts/{image.artifact_id}/retouch",
            json={"client_request_id": "incomplete"},
        ),
        422,
        "ARTIFACT_INVALID_REQUEST",
    )
    assert_stable_error(
        client.post(
            f"/api/v1/artifacts/{image.artifact_id}/retouch",
            content="{",
            headers={"Content-Type": "application/json"},
        ),
        422,
        "ARTIFACT_INVALID_REQUEST",
    )
    assert_stable_error(
        client.post(
            f"/api/v1/artifacts/{image.artifact_id}/retouch",
            json={**payload, "annotation_layer_artifact_id": secret.artifact_id},
        ),
        422,
        "ARTIFACT_INVALID_REQUEST",
    )


def test_publish_failure_returns_retryable_error_with_durable_domain_and_intent(tmp_path):
    sink = RecordingEventSink()
    sink.fail_once = True
    service, client = make_client(tmp_path, sink=sink)
    artifact = service.create_artifact(
        b"pdf", requested_name="report.pdf", mime_type="application/pdf"
    )
    payload = {
        "revision_id": artifact.revision_id,
        "signal": "thumbs_up",
        "client_request_id": "feedback-after-sink-failure",
    }

    first = client.post(f"/api/v1/artifacts/{artifact.artifact_id}/feedback", json=payload)
    assert_stable_error(first, 503, "ARTIFACT_EVENT_PERSISTENCE_FAILED")
    retry = client.post(f"/api/v1/artifacts/{artifact.artifact_id}/feedback", json=payload)
    assert retry.status_code == 200
    assert len(sink.persisted) == 1
    assert service.get_user_artifact(artifact.artifact_id).feedback is not None


def test_runtime_execution_denial_is_not_hidden_by_artifact_error_envelope(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)
    artifact = service.create_artifact(
        b"pdf", requested_name="report.pdf", mime_type="application/pdf"
    )
    app = FastAPI()

    @app.exception_handler(RuntimeExecutionDenied)
    async def execution_denied(_request, _error):
        return JSONResponse(
            status_code=503,
            content={"code": "RUNTIME_READ_ONLY"},
        )

    app.include_router(create_artifact_router(service, event_sink=DeniedEventSink()))
    response = TestClient(app).post(
        f"/api/v1/artifacts/{artifact.artifact_id}/feedback",
        json={
            "revision_id": artifact.revision_id,
            "signal": "thumbs_up",
            "client_request_id": "feedback-runtime-epoch-closed",
        },
    )

    assert response.status_code == 503
    assert response.json() == {"code": "RUNTIME_READ_ONLY"}


def test_router_does_not_publish_internal_worker_or_declaration_routes(tmp_path):
    _, client = make_client(tmp_path)
    paths = set(client.app.openapi()["paths"])
    assert paths == {
        "/api/v1/artifacts",
        "/api/v1/artifacts/{artifact_id}",
        "/api/v1/artifacts/{artifact_id}/content",
        "/api/v1/artifacts/{artifact_id}/thumbnail",
        "/api/v1/artifacts/{artifact_id}/preview",
        "/api/v1/artifacts/{artifact_id}/actions/{action}",
        "/api/v1/artifacts/{artifact_id}/feedback",
        "/api/v1/artifacts/{artifact_id}/retouch",
        "/api/v1/artifacts/{artifact_id}/retouch-workspaces",
        "/api/v1/retouch-jobs/{job_id}",
        "/api/v1/retouch-workspaces/{workspace_id}",
        "/api/v1/retouch-workspaces/{workspace_id}/surface",
        "/api/v1/retouch-workspaces/{workspace_id}/references/{reference_artifact_id}/preview",
        "/api/v1/retouch-workspaces/{workspace_id}/result",
        "/api/v1/retouch-workspaces/{workspace_id}/submit",
        "/api/v1/retouch-workspaces/{workspace_id}/reopen",
    }
    for forbidden in (
        "/api/v1/artifacts/internal",
        "/api/v1/artifacts/trusted-declaration",
        "/api/v1/retouch-jobs/job/complete",
        "/api/v1/artifacts/worker",
    ):
        assert client.get(forbidden).status_code == 404


def test_artifact_json_routes_publish_strict_response_models_and_binary_routes_do_not_claim_json(
    tmp_path,
):
    _, client = make_client(tmp_path)
    openapi = client.app.openapi()
    expected = {
        ("/api/v1/artifacts", "get", "200"): "ArtifactListResponse",
        (
            "/api/v1/artifacts/{artifact_id}",
            "get",
            "200",
        ): "ArtifactProjectionResponse",
        (
            "/api/v1/artifacts/{artifact_id}/actions/{action}",
            "post",
            "200",
        ): "ArtifactExternalActionResponse",
        (
            "/api/v1/artifacts/{artifact_id}/feedback",
            "post",
            "200",
        ): "FeedbackProjectionResponse",
        (
            "/api/v1/artifacts/{artifact_id}/retouch",
            "post",
            "202",
        ): "RetouchJobResponse",
        (
            "/api/v1/artifacts/{artifact_id}/retouch-workspaces",
            "post",
            "201",
        ): "RetouchWorkspaceResponse",
        (
            "/api/v1/retouch-workspaces/{workspace_id}",
            "get",
            "200",
        ): "RetouchWorkspaceResponse",
        (
            "/api/v1/retouch-workspaces/{workspace_id}",
            "patch",
            "200",
        ): "RetouchWorkspaceResponse",
        (
            "/api/v1/retouch-workspaces/{workspace_id}/submit",
            "post",
            "202",
        ): "RetouchWorkspaceResponse",
        (
            "/api/v1/retouch-workspaces/{workspace_id}/reopen",
            "post",
            "200",
        ): "RetouchWorkspaceResponse",
        (
            "/api/v1/retouch-jobs/{job_id}",
            "get",
            "200",
        ): "RetouchJobResponse",
    }
    for (path, method, status), model_name in expected.items():
        response = openapi["paths"][path][method]["responses"][status]
        assert response["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{model_name}"
        }

    fixed_models = {
        "ArtifactExternalActionResponse",
        "ArtifactLineageResponse",
        "ArtifactListResponse",
        "ArtifactProjectionResponse",
        "FeedbackProjectionResponse",
        "QualityCheckResponse",
        "QualityEvidenceResponse",
        "RenditionProjectionResponse",
        "RetouchAnnotationResponse",
        "RetouchEditSurfaceResponse",
        "RetouchInspectionRegionResponse",
        "RetouchJobResponse",
        "RetouchMaskResponse",
        "RetouchPixelRegionResponse",
        "RetouchReferenceResponse",
        "RetouchRequestResponse",
        "RetouchViewStateResponse",
        "RetouchWorkspaceResponse",
    }
    schemas = openapi["components"]["schemas"]
    for model_name in fixed_models:
        assert schemas[model_name]["additionalProperties"] is False

    binary_routes = {
        "/api/v1/artifacts/{artifact_id}/content",
        "/api/v1/artifacts/{artifact_id}/preview",
        "/api/v1/retouch-workspaces/{workspace_id}/surface",
        "/api/v1/retouch-workspaces/{workspace_id}/references/{reference_artifact_id}/preview",
        "/api/v1/retouch-workspaces/{workspace_id}/result",
    }
    for path in binary_routes:
        response = openapi["paths"][path]["get"]["responses"]["200"]
        assert "application/json" not in response.get("content", {})


def test_response_model_fail_closes_an_internal_artifact_projection(tmp_path):
    service, client = make_client(tmp_path)
    internal = service.create_artifact(
        b"print('secret')",
        requested_name="worker.py",
        mime_type="text/x-python",
    )
    service.get_user_artifact = lambda *_args, **_kwargs: internal

    response = client.get("/api/v1/artifacts/artifact-requested-as-public")

    assert_stable_error(response, 500, "ARTIFACT_INTERNAL_ERROR")
    wire = json.dumps(response.json(), ensure_ascii=False)
    assert internal.artifact_id not in wire
    assert "worker.py" not in wire
    assert "source_code" not in wire
