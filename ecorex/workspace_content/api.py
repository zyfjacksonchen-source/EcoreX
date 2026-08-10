"""HTTP contract for the Product knowledge workspace."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from .service import (
    MAX_DOCUMENT_BYTES,
    MAX_IMPORT_BYTES,
    MAX_IMPORT_FILES,
    WorkspaceContentConflict,
    WorkspaceContentNotFound,
    WorkspaceContentRejected,
    WorkspaceContentService,
    WorkspaceContentUnavailable,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StrictResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class KnowledgeNodeResponse(_StrictResponseModel):
    path: str = Field(max_length=1024)
    name: str = Field(min_length=1, max_length=128)
    kind: Literal["category", "document"]
    size_bytes: int = Field(ge=0, le=MAX_DOCUMENT_BYTES)
    updated_at: datetime = Field(strict=False)
    children: list["KnowledgeNodeResponse"] = Field(max_length=10_000)


class KnowledgeTreeResponse(_StrictResponseModel):
    root: Literal["knowledge"]
    query: str | None = Field(default=None, max_length=256)
    items: list[KnowledgeNodeResponse] = Field(max_length=10_000)


class KnowledgeDocumentResponse(_StrictResponseModel):
    path: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=128)
    content: str = Field(max_length=MAX_DOCUMENT_BYTES)
    size_bytes: int = Field(ge=0, le=MAX_DOCUMENT_BYTES)
    updated_at: datetime = Field(strict=False)
    links: list[str] = Field(max_length=10_000)


class KnowledgeGraphNodeResponse(_StrictResponseModel):
    path: str = Field(min_length=1, max_length=1024)
    label: str = Field(min_length=1, max_length=128)


class KnowledgeGraphEdgeResponse(_StrictResponseModel):
    source: str = Field(min_length=1, max_length=1024)
    target: str = Field(min_length=1, max_length=1024)


class KnowledgeGraphResponse(_StrictResponseModel):
    nodes: list[KnowledgeGraphNodeResponse] = Field(max_length=5_000)
    edges: list[KnowledgeGraphEdgeResponse] = Field(max_length=20_000)


class KnowledgeCategoryCreateRequest(_StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    client_request_id: str = Field(min_length=8, max_length=256)


class KnowledgeDocumentCreateRequest(_StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(default="", max_length=MAX_DOCUMENT_BYTES)
    client_request_id: str = Field(min_length=8, max_length=256)


class KnowledgeImportItemResponse(_StrictResponseModel):
    original_name: str = Field(max_length=512)
    name: str | None = Field(default=None, max_length=128)
    path: str | None = Field(default=None, max_length=1024)
    status: Literal["imported", "renamed", "rejected"]
    reason: str | None = Field(default=None, max_length=1024)


class KnowledgeImportResponse(_StrictResponseModel):
    imported_count: int = Field(ge=0, le=MAX_IMPORT_FILES)
    rejected_count: int = Field(ge=0, le=MAX_IMPORT_FILES)
    total_bytes: int = Field(ge=0, le=MAX_IMPORT_BYTES)
    items: list[KnowledgeImportItemResponse] = Field(min_length=1, max_length=MAX_IMPORT_FILES)


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, WorkspaceContentNotFound):
        return HTTPException(404, detail={"code": "knowledge_not_found", "message": "没有找到这项知识内容。"})
    if isinstance(error, WorkspaceContentConflict):
        return HTTPException(409, detail={"code": "knowledge_conflict", "message": "同名知识内容已存在。"})
    if isinstance(error, WorkspaceContentRejected):
        return HTTPException(422, detail={"code": "knowledge_request_rejected", "message": str(error)})
    return HTTPException(503, detail={"code": "knowledge_unavailable", "message": "知识目录暂时不可用，请稍后重试。"})


def create_workspace_content_router(service: WorkspaceContentService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])

    @router.get("/tree", response_model=KnowledgeTreeResponse)
    def tree(query: Annotated[str | None, Query(max_length=256)] = None) -> KnowledgeTreeResponse:
        try:
            return KnowledgeTreeResponse.model_validate(service.tree(query))
        except (WorkspaceContentRejected, WorkspaceContentUnavailable) as error:
            raise _http_error(error) from None

    @router.get("/document", response_model=KnowledgeDocumentResponse)
    def document(path: Annotated[str, Query(min_length=1, max_length=1024)]) -> KnowledgeDocumentResponse:
        try:
            return KnowledgeDocumentResponse.model_validate(service.document(path))
        except (WorkspaceContentNotFound, WorkspaceContentRejected, WorkspaceContentUnavailable) as error:
            raise _http_error(error) from None

    @router.get("/graph", response_model=KnowledgeGraphResponse)
    def graph() -> KnowledgeGraphResponse:
        try:
            return KnowledgeGraphResponse.model_validate(service.graph())
        except (WorkspaceContentRejected, WorkspaceContentUnavailable) as error:
            raise _http_error(error) from None

    @router.post("/categories", response_model=KnowledgeNodeResponse)
    def create_category(request: KnowledgeCategoryCreateRequest) -> KnowledgeNodeResponse:
        try:
            return KnowledgeNodeResponse.model_validate(
                service.create_category(
                    request.path,
                    client_request_id=request.client_request_id,
                )
            )
        except (
            WorkspaceContentConflict,
            WorkspaceContentNotFound,
            WorkspaceContentRejected,
            WorkspaceContentUnavailable,
        ) as error:
            raise _http_error(error) from None

    @router.post("/documents", response_model=KnowledgeDocumentResponse)
    def create_document(request: KnowledgeDocumentCreateRequest) -> KnowledgeDocumentResponse:
        try:
            return KnowledgeDocumentResponse.model_validate(
                service.create_document(
                    request.path,
                    request.content,
                    client_request_id=request.client_request_id,
                )
            )
        except (
            WorkspaceContentConflict,
            WorkspaceContentNotFound,
            WorkspaceContentRejected,
            WorkspaceContentUnavailable,
        ) as error:
            raise _http_error(error) from None

    @router.post("/imports", response_model=KnowledgeImportResponse)
    async def import_documents(
        files: Annotated[list[UploadFile], File(min_length=1, max_length=MAX_IMPORT_FILES)],
        client_request_id: Annotated[str, Form(min_length=8, max_length=256)],
        category_path: Annotated[str, Form(max_length=1024)] = "",
    ) -> KnowledgeImportResponse:
        if len(files) > MAX_IMPORT_FILES:
            raise _http_error(WorkspaceContentRejected("knowledge import exceeds 100 files"))
        contents: list[tuple[str, bytes]] = []
        total = 0
        try:
            for upload in files:
                chunks = bytearray()
                while chunk := await upload.read(64 * 1024):
                    chunks.extend(chunk)
                    total += len(chunk)
                    if total > MAX_IMPORT_BYTES:
                        raise WorkspaceContentRejected("knowledge import exceeds 200 MiB")
                contents.append((upload.filename or "", bytes(chunks)))
            return KnowledgeImportResponse.model_validate(
                service.import_documents(
                    category_path,
                    contents,
                    client_request_id=client_request_id,
                )
            )
        except (
            WorkspaceContentConflict,
            WorkspaceContentNotFound,
            WorkspaceContentRejected,
            WorkspaceContentUnavailable,
        ) as error:
            raise _http_error(error) from None
        finally:
            for upload in files:
                await upload.close()

    return router


__all__ = [
    "KnowledgeDocumentResponse",
    "KnowledgeGraphResponse",
    "KnowledgeImportItemResponse",
    "KnowledgeImportResponse",
    "KnowledgeNodeResponse",
    "KnowledgeTreeResponse",
    "create_workspace_content_router",
]
