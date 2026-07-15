"""Public FastAPI adapter for the office-artifact domain.

This router intentionally exposes only user projections. Runtime code mounts it
and injects an event sink that writes intent in the Artifact transaction, then
publishes that already-committed intent after the transaction returns.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import logging
import re
import sqlite3
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol
from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from ecorex.runtime.invariant_guard import RuntimeExecutionDenied

from .errors import (
    ArtifactActionOutcomeUnknown,
    ArtifactActionUnavailable,
    ArtifactError,
    ArtifactExportFailed,
    ArtifactLaunchFailed,
    ArtifactNotFound,
    ContentIntegrityError,
    IdempotencyConflict,
    RetouchConflict,
    RevisionNotFound,
)
from .models import (
    ArtifactAction,
    ArtifactExternalActionReceipt,
    ArtifactScope,
    ArtifactStatus,
    FeedbackRecord,
    FeedbackRequest,
    FeedbackSignal,
    RenditionKind,
    RetouchAnnotation,
    RetouchJob,
    RetouchJobProjection,
    RetouchRequest,
)
from .actions import ArtifactActionExecutor
from .service import ArtifactService
from .wire import (
    ArtifactExternalActionResponse,
    ArtifactListResponse,
    ArtifactProjectionResponse,
    FeedbackProjectionResponse,
    RetouchJobResponse,
    RetouchWorkspaceResponse,
)


LOGGER = logging.getLogger(__name__)
_ASCII_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")
_SAFE_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


class RetouchRequestCoordinator(Protocol):
    def request(
        self,
        artifact_id: str,
        request: RetouchRequest,
        *,
        account_id: str,
        on_persisted: Callable[
            [sqlite3.Connection, RetouchJob, ArtifactScope], None
        ]
        | None = None,
    ) -> RetouchJobProjection:
        ...


@dataclass(frozen=True, slots=True)
class ArtifactApiEvent:
    """Public, idempotent event handed to the Runtime event-store adapter."""

    event_type: str
    idempotency_key: str
    artifact_id: str
    client_request_id: str
    payload: Mapping[str, Any]
    account_id: str = "local-user"
    thread_id: str | None = None
    turn_id: str | None = None
    revision_id: str | None = None
    job_id: str | None = None
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "idempotency_key": self.idempotency_key,
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
            "job_id": self.job_id,
            "client_request_id": self.client_request_id,
            "payload": dict(self.payload),
            "account_id": self.account_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
        }


class ArtifactEventSink(Protocol):
    """Runtime hook separating the domain transaction from notification."""

    def persist_in_transaction(
        self,
        connection: sqlite3.Connection,
        event: ArtifactApiEvent,
    ) -> str | None: ...

    def publish_persisted(
        self, event: ArtifactApiEvent
    ) -> Awaitable[None] | None: ...


class _NullEventSink:
    def persist_in_transaction(
        self,
        connection: sqlite3.Connection,
        event: ArtifactApiEvent,
    ) -> None:
        del connection, event

    async def publish_persisted(self, event: ArtifactApiEvent) -> None:
        del event


class ArtifactEventPersistenceFailed(RuntimeError):
    pass


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def _artifact_error_response(error: ArtifactError) -> JSONResponse:
    if isinstance(error, (ArtifactNotFound, RevisionNotFound)):
        status = 404
    elif isinstance(
        error,
        (
            ArtifactActionOutcomeUnknown,
            ArtifactActionUnavailable,
            IdempotencyConflict,
            RetouchConflict,
        ),
    ):
        status = 409
    elif isinstance(error, ArtifactLaunchFailed):
        status = 502
    elif isinstance(error, ArtifactExportFailed):
        status = 500
    elif isinstance(error, ContentIntegrityError):
        status = 500
    else:
        status = 400
    return _error_response(status, error.code, str(error) or "artifact request failed")


class ArtifactApiRoute(APIRoute):
    """Give every router endpoint the same stable domain error envelope."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error_response(
                    422,
                    "ARTIFACT_INVALID_REQUEST",
                    "artifact request validation failed",
                )
            except ArtifactEventPersistenceFailed:
                return _error_response(
                    503,
                    "ARTIFACT_EVENT_PERSISTENCE_FAILED",
                    "artifact event persistence is temporarily unavailable; retry with the same client request id",
                )
            except ArtifactError as error:
                return _artifact_error_response(error)
            except ValueError as error:
                return _error_response(422, "ARTIFACT_INVALID_REQUEST", str(error))
            except RuntimeExecutionDenied:
                raise
            except Exception:
                LOGGER.exception("unhandled artifact API error")
                return _error_response(
                    500,
                    "ARTIFACT_INTERNAL_ERROR",
                    "artifact request failed unexpectedly",
                )

        return handler


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    revision_id: str = Field(min_length=1, max_length=128)
    signal: FeedbackSignal
    client_request_id: str = Field(min_length=1, max_length=256)

    def to_domain(self) -> FeedbackRequest:
        return FeedbackRequest(
            revision_id=self.revision_id,
            signal=self.signal,
            client_request_id=self.client_request_id,
        )


class ArtifactActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_request_id: str = Field(min_length=1, max_length=256)


class RetouchAnnotationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: str = Field(min_length=1, max_length=32)
    normalized_geometry: dict[str, Any]
    instruction: str = Field(min_length=1, max_length=4000)
    annotation_id: str | None = Field(default=None, min_length=1, max_length=128)

    def to_domain(self) -> RetouchAnnotation:
        return RetouchAnnotation(
            kind=self.kind,
            normalized_geometry=self.normalized_geometry,
            instruction=self.instruction,
            annotation_id=self.annotation_id,
        )


class RetouchBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_revision_id: str = Field(min_length=1, max_length=128)
    selected_artifact_ids: list[str] = Field(min_length=1, max_length=50)
    agent_model_id: str = Field(min_length=1, max_length=256)
    image_model_id: str = Field(min_length=1, max_length=256)
    annotations: list[RetouchAnnotationBody] = Field(default_factory=list, max_length=100)
    reference_artifact_ids: list[str] = Field(default_factory=list, max_length=10)
    global_instruction: str = Field(default="", max_length=8000)
    client_request_id: str = Field(min_length=1, max_length=256)

    def to_domain(self) -> RetouchRequest:
        return RetouchRequest(
            base_revision_id=self.base_revision_id,
            selected_artifact_ids=tuple(self.selected_artifact_ids),
            agent_model_id=self.agent_model_id,
            image_model_id=self.image_model_id,
            annotations=tuple(annotation.to_domain() for annotation in self.annotations),
            reference_artifact_ids=tuple(self.reference_artifact_ids),
            global_instruction=self.global_instruction,
            client_request_id=self.client_request_id,
        )


class RetouchWorkspaceOpenBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_revision_id: str = Field(min_length=1, max_length=128)
    client_request_id: str = Field(min_length=1, max_length=256)


class RetouchWorkspaceUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(ge=1)
    annotations: list[RetouchAnnotationBody] = Field(default_factory=list, max_length=100)
    reference_artifact_ids: list[str] = Field(default_factory=list, max_length=10)
    global_instruction: str = Field(default="", max_length=8000)
    view_state: dict[str, Any] = Field(default_factory=dict)
    client_request_id: str = Field(min_length=1, max_length=256)


class RetouchWorkspaceSubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(ge=1)
    agent_model_id: str = Field(min_length=1, max_length=256)
    image_model_id: str = Field(min_length=1, max_length=256)
    client_request_id: str = Field(min_length=1, max_length=256)


async def _publish_persisted(
    sink: ArtifactEventSink,
    event: ArtifactApiEvent,
) -> None:
    try:
        operation = sink.publish_persisted
        if inspect.iscoroutinefunction(operation):
            result = operation(event)
        else:
            result = await run_in_threadpool(operation, event)
        if inspect.isawaitable(result):
            await result
    except RuntimeExecutionDenied:
        raise
    except Exception as error:
        raise ArtifactEventPersistenceFailed from error


def _persist_event_in_transaction(
    sink: ArtifactEventSink,
    connection: sqlite3.Connection,
    event: ArtifactApiEvent,
) -> None:
    """Write an event intent on the caller-owned Artifact transaction."""

    try:
        result = sink.persist_in_transaction(connection, event)
        if inspect.isawaitable(result):
            raise TypeError("artifact event transaction sink must be synchronous")
    except RuntimeExecutionDenied:
        raise
    except Exception as error:
        raise ArtifactEventPersistenceFailed from error


def _content_headers(display_name: str, sha256: str, *, disposition: str) -> dict[str, str]:
    fallback = _ASCII_FILENAME.sub("_", display_name).strip(" .") or "artifact"
    fallback = fallback.replace('"', "_")
    encoded = quote(display_name, safe="")
    return {
        "Content-Disposition": (
            f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'
        ),
        "Cache-Control": "private, no-store",
        "ETag": f'"{sha256}"',
        "X-Content-Type-Options": "nosniff",
    }


def _safe_media_type(value: str) -> str:
    normalized = str(value or "").strip()
    return normalized if _SAFE_MEDIA_TYPE.fullmatch(normalized) else "application/octet-stream"


def create_artifact_router(
    service: ArtifactService,
    *,
    event_sink: ArtifactEventSink | None = None,
    account_id: str = "local-user",
    retouch_coordinator: RetouchRequestCoordinator | None = None,
    action_executor: ArtifactActionExecutor | None = None,
) -> APIRouter:
    """Build the mountable `/api/v1` user-artifact router.

    Mount with ``app.include_router(create_artifact_router(service,
    event_sink=runtime_sink))``. The sink must deduplicate using the supplied
    event idempotency key, append its intent in the Artifact transaction, and
    only publish after that transaction commits.
    """

    sink: ArtifactEventSink = event_sink or _NullEventSink()
    external_actions = action_executor or ArtifactActionExecutor(service)
    router = APIRouter(prefix="/api/v1", tags=["artifacts"], route_class=ArtifactApiRoute)

    def public_projection(artifact_id: str):
        projection = service.get_user_artifact(artifact_id, account_id=account_id)
        if projection.status is ArtifactStatus.DELETED:
            raise ArtifactNotFound(f"artifact {artifact_id!r} was not found")
        return projection

    def workspace_response(workspace_id: str) -> dict[str, Any]:
        workspace = service.get_retouch_workspace(workspace_id, account_id=account_id)
        projection = workspace.to_dict()
        projection["surface_url"] = (
            f"/api/v1/retouch-workspaces/{workspace.workspace_id}/surface"
        )
        projection["references"] = [
            {
                **reference.to_dict(),
                "preview_url": (
                    f"/api/v1/retouch-workspaces/{workspace.workspace_id}/references/"
                    f"{reference.artifact_id}/preview"
                ),
            }
            for reference in workspace.references
        ]
        projection["job"] = None
        projection["result"] = None
        projection["result_surface"] = None
        projection["result_url"] = None
        if workspace.submitted_job_id:
            job = service.get_retouch_job(workspace.submitted_job_id, account_id=account_id)
            projection["job"] = job.to_dict()
            if job.result_revision_id:
                result = service.repository.get_revision_projection(
                    workspace.artifact_id,
                    job.result_revision_id,
                    account_id=account_id,
                )
                projection["result"] = result.to_dict()
                try:
                    projection["result_surface"] = service.describe_retouch_surface(
                        workspace.artifact_id,
                        result.revision_id,
                        account_id=account_id,
                    ).to_dict()
                except ValueError:
                    # The result remains downloadable/previewable. Geometry
                    # overlays fail closed until its raster format exposes a
                    # canonical coordinate surface.
                    projection["result_surface"] = None
                projection["result_url"] = (
                    f"/api/v1/retouch-workspaces/{workspace.workspace_id}/result"
                )
        return projection

    @router.post(
        "/artifacts/{artifact_id}/retouch-workspaces",
        status_code=201,
        response_model=RetouchWorkspaceResponse,
    )
    def open_retouch_workspace(
        artifact_id: str, body: RetouchWorkspaceOpenBody
    ) -> dict[str, Any]:
        workspace = service.open_retouch_workspace(
            artifact_id,
            body.base_revision_id,
            account_id=account_id,
        )
        return workspace_response(workspace.workspace_id)

    @router.get(
        "/retouch-workspaces/{workspace_id}",
        response_model=RetouchWorkspaceResponse,
    )
    def get_retouch_workspace(workspace_id: str) -> dict[str, Any]:
        return workspace_response(workspace_id)

    @router.patch(
        "/retouch-workspaces/{workspace_id}",
        response_model=RetouchWorkspaceResponse,
    )
    def update_retouch_workspace(
        workspace_id: str, body: RetouchWorkspaceUpdateBody
    ) -> dict[str, Any]:
        workspace = service.update_retouch_workspace(
            workspace_id,
            expected_version=body.expected_version,
            annotations=tuple(item.to_domain() for item in body.annotations),
            reference_artifact_ids=tuple(body.reference_artifact_ids),
            global_instruction=body.global_instruction,
            view_state=body.view_state,
            client_request_id=body.client_request_id,
            account_id=account_id,
        )
        return workspace_response(workspace.workspace_id)

    @router.get(
        "/retouch-workspaces/{workspace_id}/surface",
        response_model=None,
        response_class=Response,
    )
    def get_retouch_workspace_surface(workspace_id: str) -> Response:
        workspace = service.get_retouch_workspace(workspace_id, account_id=account_id)
        content = service.read_user_content(
            workspace.artifact_id,
            workspace.edit_surface.base_revision_id,
            account_id=account_id,
        )
        return Response(
            content=content,
            media_type=_safe_media_type(workspace.edit_surface.mime_type),
            headers=_content_headers(
                "retouch-source",
                workspace.edit_surface.raster_digest,
                disposition="inline",
            ),
        )

    @router.get(
        "/retouch-workspaces/{workspace_id}/references/{reference_artifact_id}/preview",
        response_model=None,
        response_class=Response,
    )
    def get_retouch_reference_preview(
        workspace_id: str, reference_artifact_id: str
    ) -> Response:
        workspace = service.get_retouch_workspace(workspace_id, account_id=account_id)
        reference = next(
            (
                item
                for item in workspace.references
                if item.artifact_id == reference_artifact_id
            ),
            None,
        )
        if reference is None:
            raise ArtifactNotFound("retouch reference was not found")
        content = service.read_user_content(
            reference.artifact_id,
            reference.revision_id,
            account_id=account_id,
        )
        return Response(
            content=content,
            media_type=_safe_media_type(reference.mime_type),
            headers=_content_headers(
                reference.display_name,
                reference.sha256,
                disposition="inline",
            ),
        )

    @router.get(
        "/retouch-workspaces/{workspace_id}/result",
        response_model=None,
        response_class=Response,
    )
    def get_retouch_workspace_result(workspace_id: str) -> Response:
        workspace = service.get_retouch_workspace(workspace_id, account_id=account_id)
        if not workspace.submitted_job_id:
            raise ArtifactActionUnavailable("retouch workspace has no submitted result")
        job = service.get_retouch_job(workspace.submitted_job_id, account_id=account_id)
        if not job.result_revision_id:
            raise ArtifactActionUnavailable("retouch result is not ready")
        result = service.repository.get_revision_projection(
            workspace.artifact_id,
            job.result_revision_id,
            account_id=account_id,
        )
        content = service.read_user_content(
            workspace.artifact_id,
            result.revision_id,
            account_id=account_id,
        )
        return Response(
            content=content,
            media_type=_safe_media_type(result.mime_type),
            headers=_content_headers(
                result.display_name,
                result.sha256,
                disposition="inline",
            ),
        )

    @router.post(
        "/retouch-workspaces/{workspace_id}/reopen",
        response_model=RetouchWorkspaceResponse,
    )
    def reopen_retouch_workspace(
        workspace_id: str, body: RetouchWorkspaceSubmitBody
    ) -> dict[str, Any]:
        workspace = service.reopen_failed_retouch_workspace(
            workspace_id,
            expected_version=body.expected_version,
            account_id=account_id,
        )
        return workspace_response(workspace.workspace_id)

    @router.get("/artifacts", response_model=ArtifactListResponse)
    def list_artifacts(
        thread_id: str | None = Query(default=None, min_length=1, max_length=256),
    ) -> dict[str, Any]:
        items = service.list_user_artifacts(
            account_id=account_id,
            thread_id=thread_id,
        )
        return {"items": [item.to_dict() for item in items], "count": len(items)}

    @router.get(
        "/artifacts/{artifact_id}",
        response_model=ArtifactProjectionResponse,
    )
    def get_artifact(artifact_id: str) -> dict[str, Any]:
        return public_projection(artifact_id).to_dict()

    @router.get(
        "/artifacts/{artifact_id}/content",
        response_model=None,
        response_class=Response,
    )
    def get_content(artifact_id: str) -> Response:
        projection = public_projection(artifact_id)
        if ArtifactAction.DOWNLOAD not in projection.actions:
            raise ArtifactActionUnavailable("content download is unavailable for this artifact")
        content = service.read_user_content(
            artifact_id,
            projection.revision_id,
            account_id=account_id,
        )
        return Response(
            content=content,
            media_type=_safe_media_type(projection.mime_type),
            headers=_content_headers(
                projection.display_name,
                projection.sha256,
                disposition="attachment",
            ),
        )

    @router.get(
        "/artifacts/{artifact_id}/preview",
        response_model=None,
        response_class=Response,
    )
    def get_preview(artifact_id: str) -> Response:
        projection = public_projection(artifact_id)
        if ArtifactAction.PREVIEW not in projection.actions:
            raise ArtifactActionUnavailable("preview is unavailable for this artifact")
        rendition = next(
            (
                item
                for item in projection.renditions
                if item.kind is RenditionKind.PREVIEW
            ),
            None,
        )
        if rendition is not None:
            content = service.blobs.read_bytes(rendition.sha256)
            mime_type = rendition.mime_type
            sha256 = rendition.sha256
        else:
            content = service.read_user_content(
                artifact_id,
                projection.revision_id,
                account_id=account_id,
            )
            mime_type = projection.mime_type
            sha256 = projection.sha256
        return Response(
            content=content,
            media_type=_safe_media_type(mime_type),
            headers=_content_headers(
                projection.display_name,
                sha256,
                disposition="inline",
            ),
        )

    @router.post(
        "/artifacts/{artifact_id}/actions/{action}",
        response_model=ArtifactExternalActionResponse,
    )
    async def perform_external_action(
        artifact_id: str,
        action: Literal["open", "reveal"],
        body: ArtifactActionBody,
    ) -> dict[str, Any]:
        # Preparation durably binds the current public revision and produces a
        # server-selected launch target. The target never crosses this boundary.
        event_holder: dict[str, ArtifactApiEvent] = {}

        def persist_action_event(
            connection: sqlite3.Connection,
            receipt: ArtifactExternalActionReceipt,
            scope: ArtifactScope,
        ) -> None:
            event = ArtifactApiEvent(
                event_type="artifact.action.requested",
                idempotency_key=(
                    f"artifact.action:{artifact_id}:{body.client_request_id}"
                ),
                artifact_id=artifact_id,
                revision_id=receipt.revision_id,
                client_request_id=body.client_request_id,
                payload={
                    "artifact_id": artifact_id,
                    "revision_id": receipt.revision_id,
                    "action": action,
                },
                account_id=scope.account_id,
                thread_id=scope.thread_id,
                turn_id=scope.turn_id,
            )
            _persist_event_in_transaction(sink, connection, event)
            event_holder["event"] = event

        prepared = await run_in_threadpool(
            external_actions.prepare,
            artifact_id,
            ArtifactAction(action),
            body.client_request_id,
            account_id=account_id,
            on_prepared=persist_action_event,
        )
        await _publish_persisted(sink, event_holder["event"])
        receipt = await run_in_threadpool(external_actions.launch, prepared)
        return receipt.to_dict()

    @router.post(
        "/artifacts/{artifact_id}/feedback",
        response_model=FeedbackProjectionResponse,
    )
    async def record_feedback(artifact_id: str, body: FeedbackBody) -> dict[str, Any]:
        await run_in_threadpool(public_projection, artifact_id)
        event_holder: dict[str, ArtifactApiEvent] = {}

        def persist_feedback_event(
            connection: sqlite3.Connection,
            stored: FeedbackRecord,
            scope: ArtifactScope,
        ) -> None:
            event = ArtifactApiEvent(
                event_type="artifact.feedback.recorded",
                idempotency_key=(
                    f"artifact.feedback:{artifact_id}:{body.client_request_id}"
                ),
                artifact_id=artifact_id,
                revision_id=body.revision_id,
                client_request_id=body.client_request_id,
                payload=stored.projection().to_dict(),
                account_id=scope.account_id,
                thread_id=scope.thread_id,
                turn_id=scope.turn_id,
            )
            _persist_event_in_transaction(sink, connection, event)
            event_holder["event"] = event

        record = await run_in_threadpool(
            service.record_feedback,
            artifact_id,
            body.to_domain(),
            account_id=account_id,
            on_recorded=persist_feedback_event,
        )
        projection = record.projection()
        await _publish_persisted(sink, event_holder["event"])
        return projection.to_dict()

    @router.post(
        "/artifacts/{artifact_id}/retouch",
        status_code=202,
        response_model=RetouchJobResponse,
    )
    async def request_retouch(artifact_id: str, body: RetouchBody) -> dict[str, Any]:
        operation = (
            retouch_coordinator.request
            if retouch_coordinator is not None
            else service.request_retouch
        )
        request = body.to_domain()
        event_holder: dict[str, ArtifactApiEvent] = {}

        def persist_retouch_event(
            connection: sqlite3.Connection,
            stored: RetouchJob,
            scope: ArtifactScope,
        ) -> None:
            projection = stored.public_projection()
            event = ArtifactApiEvent(
                event_type="artifact.retouch.requested",
                idempotency_key=(
                    f"artifact.retouch:{artifact_id}:{body.client_request_id}"
                ),
                artifact_id=artifact_id,
                revision_id=body.base_revision_id,
                job_id=projection.job_id,
                client_request_id=body.client_request_id,
                payload=projection.to_dict(),
                account_id=scope.account_id,
                thread_id=scope.thread_id,
                turn_id=scope.turn_id,
            )
            _persist_event_in_transaction(sink, connection, event)
            event_holder["event"] = event

        job = await run_in_threadpool(
            operation,
            artifact_id,
            request,
            account_id=account_id,
            on_persisted=persist_retouch_event,
        )
        await _publish_persisted(sink, event_holder["event"])
        return job.to_dict()

    @router.post(
        "/retouch-workspaces/{workspace_id}/submit",
        status_code=202,
        response_model=RetouchWorkspaceResponse,
    )
    async def submit_retouch_workspace(
        workspace_id: str, body: RetouchWorkspaceSubmitBody
    ) -> dict[str, Any]:
        workspace = await run_in_threadpool(
            service.claim_retouch_workspace_submission,
            workspace_id,
            expected_version=body.expected_version,
            client_request_id=body.client_request_id,
            account_id=account_id,
        )
        job: RetouchJobProjection | None = (
            await run_in_threadpool(
                service.get_retouch_job,
                workspace.submitted_job_id,
                account_id=account_id,
            )
            if workspace.submitted_job_id
            else None
        )
        for reference in workspace.references if job is None else ():
            current = await run_in_threadpool(
                service.get_user_artifact,
                reference.artifact_id,
                account_id=account_id,
            )
            if current.revision_id != reference.revision_id:
                await run_in_threadpool(
                    service.release_retouch_workspace_submission,
                    workspace_id,
                    client_request_id=body.client_request_id,
                    account_id=account_id,
                )
                raise RetouchConflict(
                    f"reference image {reference.display_name!r} changed; review the new revision before submitting"
                )
        operation = (
            retouch_coordinator.request
            if retouch_coordinator is not None
            else service.request_retouch
        )
        retouch_request = RetouchRequest(
            base_revision_id=workspace.edit_surface.base_revision_id,
            selected_artifact_ids=(workspace.artifact_id,),
            agent_model_id=body.agent_model_id,
            image_model_id=body.image_model_id,
            annotations=workspace.annotations,
            reference_artifact_ids=tuple(
                item.artifact_id for item in workspace.references
            ),
            pinned_reference_revision_ids={
                item.artifact_id: item.revision_id for item in workspace.references
            },
            global_instruction=workspace.global_instruction,
            client_request_id=body.client_request_id,
            edit_surface=workspace.edit_surface.to_dict(),
            mask=dict(workspace.mask) if workspace.mask is not None else None,
        )
        event_holder: dict[str, ArtifactApiEvent] = {}

        def workspace_event(
            projection: RetouchJobProjection,
            scope: ArtifactScope,
        ) -> ArtifactApiEvent:
            return ArtifactApiEvent(
                event_type="artifact.retouch.requested",
                idempotency_key=(
                    f"artifact.retouch:{workspace.artifact_id}:{body.client_request_id}"
                ),
                artifact_id=workspace.artifact_id,
                revision_id=workspace.edit_surface.base_revision_id,
                job_id=projection.job_id,
                client_request_id=body.client_request_id,
                payload={
                    **projection.to_dict(),
                    "workspace_id": workspace.workspace_id,
                    "workspace_version": workspace.version,
                    "edit_surface": workspace.edit_surface.to_dict(),
                    "mask": dict(workspace.mask) if workspace.mask else None,
                },
                account_id=scope.account_id,
                thread_id=scope.thread_id,
                turn_id=scope.turn_id,
            )

        def persist_workspace_event(
            connection: sqlite3.Connection,
            stored: RetouchJob,
            scope: ArtifactScope,
        ) -> None:
            service.complete_retouch_workspace_submission_in_transaction(
                connection,
                workspace_id,
                client_request_id=body.client_request_id,
                job_id=stored.job_id,
                account_id=account_id,
            )
            event = workspace_event(stored.public_projection(), scope)
            _persist_event_in_transaction(sink, connection, event)
            event_holder["event"] = event

        try:
            if job is None:
                job = await run_in_threadpool(
                    operation,
                    workspace.artifact_id,
                    retouch_request,
                    account_id=account_id,
                    on_persisted=persist_workspace_event,
                )
        except Exception:
            if job is None:
                await run_in_threadpool(
                    service.release_retouch_workspace_submission,
                    workspace_id,
                    client_request_id=body.client_request_id,
                    account_id=account_id,
                )
            raise
        assert job is not None
        event = event_holder.get("event")
        if event is None:
            # An idempotent replay of an already-submitted workspace performs no
            # new business write. Rebuild the same event only to drain/recover
            # the intent that the original transaction already committed.
            event = workspace_event(
                job,
                service.get_artifact_scope(workspace.artifact_id),
            )
        await _publish_persisted(sink, event)
        return workspace_response(workspace.workspace_id)

    @router.get(
        "/retouch-jobs/{job_id}",
        response_model=RetouchJobResponse,
    )
    def get_retouch_job(job_id: str) -> dict[str, Any]:
        job = service.get_retouch_job(job_id, account_id=account_id)
        # Recheck the target through the public projection boundary so a future
        # internal-only job type cannot become queryable through this route.
        public_projection(job.artifact_id)
        return job.to_dict()

    return router


__all__ = [
    "ArtifactActionBody",
    "ArtifactApiEvent",
    "ArtifactApiRoute",
    "ArtifactEventSink",
    "FeedbackBody",
    "RetouchAnnotationBody",
    "RetouchBody",
    "RetouchRequestCoordinator",
    "create_artifact_router",
]
