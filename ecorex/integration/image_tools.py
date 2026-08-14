"""Executable image/vision pack handlers and the Runtime Artifact backend."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any

import httpx

from ecorex.artifacts import (
    ArtifactError,
    ArtifactFamily,
    ArtifactLineage,
    ArtifactScope,
    ArtifactService,
    QualityEvidence,
)
from ecorex.capabilities import (
    ModelModality,
    ToolInvocationContext,
    VerifiedCapabilityPack,
)
from ecorex.image_orchestrator import ImageOperation, ImageSubmitRequest
from ecorex.protocol import ItemKind, ItemStatus
from ecorex.runtime.database import SQLiteDatabase
from ecorex.input_attachments import InputAttachmentError, InputAttachmentService
from ecorex.gateway import GatewayImageInput

from .managed_image import (
    ManagedImageClientError,
    ManagedImageDownloadedResult,
    ManagedImageInputAsset,
    ManagedImageOrchestrationClient,
)


class ImageToolError(RuntimeError):
    code = "image_tool_failed"

    def __init__(self, code: str | None = None, *, retryable: bool = False) -> None:
        self.code = str(code or self.code)
        self.retryable = bool(retryable)
        super().__init__(self.code)


class ImageToolUnavailable(ImageToolError):
    code = "managed_image_orchestration_not_configured"


class ImageToolPublicationBusy(ImageToolError):
    code = "image_artifact_publication_busy"

    def __init__(self, message: str = "image artifact publication is busy") -> None:
        super().__init__(self.code, retryable=True)


class ImageGenerationToolHandler:
    async def __call__(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> dict[str, Any]:
        backend = context.backend
        operation = getattr(backend, "generate_image", None)
        if not callable(operation):
            raise ImageToolUnavailable(ImageToolUnavailable.code)
        result = operation(arguments, context)
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, dict):
            raise ImageToolError("image backend returned an invalid result")
        return result


class ImageVisionToolHandler:
    async def __call__(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> dict[str, Any]:
        backend = context.backend
        operation = getattr(backend, "inspect_images", None)
        if not callable(operation):
            raise ImageToolUnavailable(ImageToolUnavailable.code)
        result = operation(arguments, context)
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, dict):
            raise ImageToolError("image vision backend returned an invalid result")
        return result


def production_pack_adapter_resolver(
    pack: VerifiedCapabilityPack,
) -> Mapping[str, Callable[..., Any]]:
    """CLI-owned resolver: signed contracts select trusted implementations."""

    factories: dict[str, Callable[[], Callable[..., Any]]] = {
        "imagegen": ImageGenerationToolHandler,
        "vision": ImageVisionToolHandler,
    }
    handlers: dict[str, Callable[..., Any]] = {}
    for binding in pack.manifest.tools:
        factory = factories.get(binding.tool_id)
        if factory is None:
            raise ValueError(
                f"no production adapter exists for packed tool {binding.tool_id!r}"
            )
        handlers[binding.tool_id] = factory()
    return handlers


class _ImagePublicationRepository:
    def __init__(self, database_path: SQLiteDatabase | str | Path) -> None:
        self.database = (
            database_path
            if isinstance(database_path, SQLiteDatabase)
            else SQLiteDatabase(database_path)
        )
        self.path = self.database.path

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

    @staticmethod
    def marker(publication_key: str) -> str:
        return "image-publication:" + hashlib.sha256(
            publication_key.encode("utf-8")
        ).hexdigest()

    def row(self, publication_key: str) -> sqlite3.Row | None:
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT * FROM image_tool_publications WHERE publication_key=?",
                (publication_key,),
            ).fetchone()
        finally:
            connection.close()

    def claim(
        self,
        publication_key: str,
        *,
        account_id: str,
        request_sha256: str,
        lease_seconds: float = 30,
    ) -> tuple[str, str]:
        now = datetime.now(UTC)
        marker = self.marker(publication_key)
        token = secrets.token_urlsafe(32)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM image_tool_publications WHERE publication_key=?",
                (publication_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO image_tool_publications(publication_key,marker,account_id,"
                    "request_sha256,status,lease_token,lease_expires_at,created_at,updated_at) "
                    "VALUES(?,?,?,?,'publishing',?,?,?,?)",
                    (
                        publication_key,
                        marker,
                        account_id,
                        request_sha256,
                        token,
                        (now + timedelta(seconds=lease_seconds)).isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            else:
                if row["account_id"] != account_id or row["request_sha256"] != request_sha256:
                    raise ImageToolError("image publication key was reused with different content")
                if row["status"] == "completed":
                    connection.commit()
                    return marker, ""
                expires = (
                    datetime.fromisoformat(row["lease_expires_at"])
                    if row["lease_expires_at"]
                    else None
                )
                if expires is not None and expires > now:
                    raise ImageToolPublicationBusy("image publication is already active")
                connection.execute(
                    "UPDATE image_tool_publications SET lease_token=?,lease_expires_at=?,updated_at=? "
                    "WHERE publication_key=?",
                    (
                        token,
                        (now + timedelta(seconds=lease_seconds)).isoformat(),
                        now.isoformat(),
                        publication_key,
                    ),
                )
            connection.commit()
            return marker, token
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def heartbeat(
        self,
        publication_key: str,
        *,
        token: str,
        lease_seconds: float,
    ) -> bool:
        """Renew a live publisher lease; a replacement token fences this owner."""

        now = datetime.now(UTC)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE image_tool_publications SET lease_expires_at=?,updated_at=? "
                "WHERE publication_key=? AND lease_token=? AND status='publishing'",
                (
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    publication_key,
                    token,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete(
        self,
        publication_key: str,
        *,
        token: str,
        cloud_job_id: str,
        result_sha256: str,
        artifact_id: str,
        revision_id: str,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM image_tool_publications WHERE publication_key=?",
                (publication_key,),
            ).fetchone()
            if row is None:
                raise ImageToolError("image publication state is missing")
            if row["status"] == "completed":
                expected = (cloud_job_id, result_sha256, artifact_id, revision_id)
                actual = (
                    row["cloud_job_id"], row["result_sha256"],
                    row["artifact_id"], row["revision_id"],
                )
                if actual != expected:
                    raise ImageToolError("image publication completion conflicts")
                connection.commit()
                return
            if not token or not secrets.compare_digest(str(row["lease_token"] or ""), token):
                raise ImageToolPublicationBusy("image publication lease was lost")
            connection.execute(
                "UPDATE image_tool_publications SET status='completed',lease_token=NULL,"
                "lease_expires_at=NULL,cloud_job_id=?,result_sha256=?,artifact_id=?,"
                "revision_id=?,updated_at=? WHERE publication_key=?",
                (
                    cloud_job_id,
                    result_sha256,
                    artifact_id,
                    revision_id,
                    datetime.now(UTC).isoformat(),
                    publication_key,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def stage_cloud_result(
        self,
        publication_key: str,
        *,
        token: str,
        cloud_job_id: str,
        result_sha256: str,
    ) -> None:
        """Persist the remote commitment before the local Artifact is created."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM image_tool_publications WHERE publication_key=?",
                (publication_key,),
            ).fetchone()
            if row is None:
                raise ImageToolError("image publication state is missing")
            if not token or not secrets.compare_digest(
                str(row["lease_token"] or ""), token
            ):
                raise ImageToolPublicationBusy("image publication lease was lost")
            existing = (row["cloud_job_id"], row["result_sha256"])
            expected = (cloud_job_id, result_sha256)
            if any(existing) and existing != expected:
                raise ImageToolError("image cloud result commitment conflicts")
            connection.execute(
                "UPDATE image_tool_publications SET cloud_job_id=?,result_sha256=?,"
                "updated_at=? WHERE publication_key=?",
                (
                    cloud_job_id,
                    result_sha256,
                    datetime.now(UTC).isoformat(),
                    publication_key,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_from_artifact(
        self,
        publication_key: str,
        *,
        account_id: str,
        request_sha256: str,
        artifact_id: str,
        revision_id: str,
        artifact_sha256: str,
    ) -> str:
        """Finish a staged publication without contacting the image service again."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM image_tool_publications WHERE publication_key=?",
                (publication_key,),
            ).fetchone()
            if row is None:
                raise ImageToolError("image publication state is missing")
            if (
                row["account_id"] != account_id
                or row["request_sha256"] != request_sha256
                or row["marker"] != self.marker(publication_key)
            ):
                raise ImageToolError(
                    "image publication identity does not match recovered Artifact"
                )
            cloud_job_id = str(row["cloud_job_id"] or "")
            result_sha256 = str(row["result_sha256"] or "")
            if not cloud_job_id or result_sha256 != artifact_sha256:
                raise ImageToolError(
                    "image publication has no matching staged cloud commitment"
                )
            expected_artifact = (artifact_id, revision_id)
            if row["status"] == "completed":
                if (row["artifact_id"], row["revision_id"]) != expected_artifact:
                    raise ImageToolError("image publication completion conflicts")
                connection.commit()
                return cloud_job_id
            connection.execute(
                "UPDATE image_tool_publications SET status='completed',lease_token=NULL,"
                "lease_expires_at=NULL,artifact_id=?,revision_id=?,updated_at=? "
                "WHERE publication_key=?",
                (
                    artifact_id,
                    revision_id,
                    datetime.now(UTC).isoformat(),
                    publication_key,
                ),
            )
            connection.commit()
            return cloud_job_id
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release(self, publication_key: str, token: str) -> None:
        if not token:
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE image_tool_publications SET lease_token=NULL,lease_expires_at=NULL,"
                "updated_at=? WHERE publication_key=? AND lease_token=? AND status='publishing'",
                (datetime.now(UTC).isoformat(), publication_key, token),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class RuntimeImageToolBackend:
    """Publishes cloud results once and creates inline Runtime Artifact items."""

    def __init__(
        self,
        *,
        database_path: SQLiteDatabase | str | Path,
        artifacts: ArtifactService,
        kernel: Any,
        account_id: str,
        client: ManagedImageOrchestrationClient | None,
        input_attachments: InputAttachmentService | None = None,
        fault_hook: Callable[[str, str], None] | None = None,
        publication_lease_seconds: float = 30,
        workspace_root: Path | str | None = None,
    ) -> None:
        if not 0.15 <= publication_lease_seconds <= 300:
            raise ValueError("image publication lease must be between 0.15 and 300 seconds")
        self.database_path = (
            database_path.path
            if isinstance(database_path, SQLiteDatabase)
            else Path(database_path)
        )
        self.artifacts = artifacts
        self.kernel = kernel
        self.account_id = account_id
        self.client = client
        self.input_attachments = input_attachments
        self.publications = _ImagePublicationRepository(database_path)
        self.fault_hook = fault_hook or (lambda _phase, _key: None)
        self.publication_lease_seconds = float(publication_lease_seconds)
        self.workspace_root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else None
        )

    async def generate_image(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> dict[str, Any]:
        if "tasks" in arguments:
            raise ImageToolError("imagegen_tasks_unsupported")
        task = self._canonical_task(arguments)
        task = self._bind_turn_image_references(task, context)
        fence_key = self._edit_fence_key(task, context)
        failures = getattr(self, "_terminal_edit_failures", None)
        if failures is None:
            failures = self._terminal_edit_failures = {}
        if fence_key is not None and fence_key in failures:
            raise ImageToolError(failures[fence_key])
        try:
            return await self._generate_single(task, context)
        except ImageToolError as error:
            if fence_key is not None and not error.retryable:
                failures[fence_key] = error.code
                if len(failures) > 1024:
                    failures.pop(next(iter(failures)))
            raise

    def _bind_turn_image_references(
        self,
        task: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> dict[str, Any]:
        scope = context.execution_scope
        if scope is None:
            return dict(task)
        metadata = getattr(self.kernel.get_turn(scope.turn_id), "metadata", {})
        raw = metadata.get("input_attachments") if isinstance(metadata, Mapping) else None
        bound = [
            str(item["attachment_id"])
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, Mapping)
            and isinstance(item.get("attachment_id"), str)
            and (
                item.get("media_kind") == "image"
                or str(item.get("mime_type") or "").startswith("image/")
            )
        ]
        if not bound:
            return dict(task)
        existing = task.get("image_url")
        sources = existing if isinstance(existing, list) else [existing] if existing else []
        merged = list(dict.fromkeys([*bound, *sources]))
        if len(merged) > 16:
            raise ImageToolError("image_url contains too many references")
        return {
            **dict(task),
            "image_url": merged[0] if len(merged) == 1 else merged,
        }

    @staticmethod
    def _edit_fence_key(
        task: Mapping[str, Any], context: ToolInvocationContext
    ) -> str | None:
        scope = context.execution_scope
        sources = task.get("image_url")
        if scope is None or not sources:
            return None
        return hashlib.sha256(
            json.dumps(
                {
                    "turn_id": scope.turn_id,
                    "image_url": sources,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _canonical_task(raw: Mapping[str, Any]) -> dict[str, Any]:
        """Use CowAgent names internally; legacy names only resume old calls."""

        task = dict(raw)
        if not set(task) <= {
            "prompt",
            "image_url",
            "size",
            "quality",
            "aspect_ratio",
            # These three are accepted only for durable calls created by an
            # older e-Mate ToolSpec. The current model-visible schema does not
            # advertise them.
            "instruction",
            "reference_artifact_ids",
            "attachment_ids",
        }:
            raise ImageToolError("image task contract is invalid")
        prompt = task.get("prompt", task.get("instruction"))
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 20_000:
            raise ImageToolError("imagegen requires a prompt")
        if (
            isinstance(task.get("prompt"), str)
            and isinstance(task.get("instruction"), str)
            and task["prompt"].strip() != task["instruction"].strip()
        ):
            raise ImageToolError("image prompt aliases disagree")

        sources: list[str] = []
        image_url = task.get("image_url")
        if image_url is not None:
            values = image_url if isinstance(image_url, list) else [image_url]
            if (
                not isinstance(values, list)
                or not 1 <= len(values) <= 16
                or any(
                    not isinstance(value, str) or not 1 <= len(value.strip()) <= 4096
                    for value in values
                )
            ):
                raise ImageToolError("image_url is invalid")
            sources.extend(value.strip() for value in values)
        for field, maximum in (
            ("reference_artifact_ids", 16),
            ("attachment_ids", 4),
        ):
            values = task.get(field, [])
            if (
                not isinstance(values, list)
                or len(values) > maximum
                or any(
                    not isinstance(value, str) or not 1 <= len(value.strip()) <= 128
                    for value in values
                )
            ):
                raise ImageToolError("image reference contract is invalid")
            sources.extend(value.strip() for value in values)
        sources = list(dict.fromkeys(sources))
        if len(sources) > 16:
            raise ImageToolError("image_url contains too many references")

        canonical: dict[str, Any] = {"prompt": prompt.strip()}
        if sources:
            canonical["image_url"] = sources[0] if len(sources) == 1 else sources
        for field in ("size", "quality", "aspect_ratio"):
            value = task.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 64:
                raise ImageToolError("image task contract is invalid")
            canonical[field] = value.strip()
        if canonical.get("quality") not in {None, "low", "medium", "high", "auto"}:
            raise ImageToolError("image quality is unsupported")
        ratio = canonical.pop("aspect_ratio", None)
        if ratio is not None and re.fullmatch(r"[1-9]\d{0,2}:[1-9]\d{0,2}", ratio) is None:
            raise ImageToolError("image aspect ratio is invalid")
        size = str(canonical.get("size") or "auto").casefold()
        canonical["size"] = (
            size
            if ratio is None
            and size in {"auto", "1024x1024", "1536x1024", "1024x1536"}
            else "auto"
        )
        return canonical

    @staticmethod
    def _result_images(result: Mapping[str, Any]) -> list[dict[str, str]]:
        images = result.get("images")
        if isinstance(images, list):
            first = next((image for image in images if isinstance(image, Mapping)), None)
            return [dict(first)] if first is not None else []
        preview = result.get("preview_url")
        if not isinstance(preview, str) or not preview:
            return []
        image = {"url": preview}
        for field in ("artifact_id", "revision_id"):
            if isinstance(result.get(field), str):
                image[field] = str(result[field])
        return [image]

    async def _generate_single(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> dict[str, Any]:
        if context.execution_scope is None or not context.idempotency_key:
            raise ImageToolError("imagegen requires a durable Runtime execution scope")
        if self.client is None:
            raise ImageToolUnavailable("managed image orchestration is unavailable")
        scope = context.execution_scope
        turn = self.kernel.get_turn(scope.turn_id)
        if scope.thread_id != turn.thread_id:
            raise ImageToolError("imagegen execution scope is inconsistent")
        if not turn.image_model_id:
            raise ImageToolUnavailable("Turn has no frozen image model")
        publication_key = f"imagegen:{self.account_id}:{context.idempotency_key}"
        request_sha = hashlib.sha256(
            json.dumps(
                {
                    "arguments": dict(arguments),
                    "image_model_id": turn.image_model_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        marker = self.publications.marker(publication_key)
        recovered = await asyncio.to_thread(self._find_marker_artifact, marker)
        if recovered is not None:
            recovered = await asyncio.to_thread(
                self.artifacts.ensure_image_renditions,
                recovered.artifact_id,
                revision_id=recovered.revision_id,
            )
            cloud_job_id = await asyncio.to_thread(
                self._finalize_recovered_publication,
                publication_key,
                request_sha,
                recovered,
            )
            result = await asyncio.to_thread(
                self._emit_artifact_item,
                recovered,
                context,
                publication_key,
                cloud_job_id,
            )
            return self._cow_result(result, turn.image_model_id)
        row = await asyncio.to_thread(self.publications.row, publication_key)
        if row is not None and row["status"] == "completed":
            if (
                row["account_id"] != self.account_id
                or row["request_sha256"] != request_sha
            ):
                raise ImageToolError(
                    "image publication key was reused with different content"
                )
            recovered = await asyncio.to_thread(self._find_marker_artifact, marker)
            if recovered is None:
                raise ImageToolError(
                    "completed image publication Artifact marker is missing"
                )
            recovered = await asyncio.to_thread(
                self.artifacts.ensure_image_renditions,
                recovered.artifact_id,
                revision_id=recovered.revision_id,
            )
            cloud_job_id = await asyncio.to_thread(
                self._finalize_recovered_publication,
                publication_key,
                request_sha,
                recovered,
            )
            result = await asyncio.to_thread(
                self._emit_artifact_item,
                recovered,
                context,
                publication_key,
                cloud_job_id,
            )
            return self._cow_result(result, turn.image_model_id)
        assets, source_ids = await self._image_sources(
            arguments.get("image_url"),
            scope=context.execution_scope,
        )
        width, height = self._size(
            str(arguments.get("size") or "auto"),
            str(arguments.get("aspect_ratio") or "") or None,
        )
        client_request_id = self._client_request_id(context.idempotency_key)
        operation = ImageOperation.RETOUCH if assets else ImageOperation.GENERATE
        request = ImageSubmitRequest(
            operation=operation,
            model_id=turn.image_model_id,
            client_request_id=client_request_id,
            prompt=str(arguments["prompt"]),
            width=width,
            height=height,
            input_sha256=tuple(dict.fromkeys(asset.sha256 for asset in assets)),
            instruction=(str(arguments["prompt"]) if assets else None),
            metadata={
                "request_kind": "imagegen_tool",
                "quality": str(arguments.get("quality") or "auto")[:64],
                "size": str(arguments.get("size") or "auto"),
            },
        )
        _marker, token = await asyncio.to_thread(
            self.publications.claim,
            publication_key,
            account_id=self.account_id,
            request_sha256=request_sha,
            lease_seconds=self.publication_lease_seconds,
        )
        if not token:
            row = await asyncio.to_thread(self.publications.row, publication_key)
            if row is None or row["status"] != "completed":
                raise ImageToolError("image publication completion disappeared")
            if (
                row["account_id"] != self.account_id
                or row["request_sha256"] != request_sha
            ):
                raise ImageToolError(
                    "image publication key was reused with different content"
                )
            artifact = self.artifacts.get_user_artifact(
                row["artifact_id"], account_id=self.account_id
            )
            artifact = await asyncio.to_thread(
                self.artifacts.ensure_image_renditions,
                artifact.artifact_id,
                revision_id=artifact.revision_id,
            )
            result = await asyncio.to_thread(
                self._emit_artifact_item,
                artifact,
                context,
                publication_key,
                row["cloud_job_id"],
            )
            return self._cow_result(result, turn.image_model_id)
        lease_stop, lease_heartbeat = self._start_publication_lease(
            publication_key, token
        )
        lease_stopped = False
        try:
            try:
                downloaded = await self._execute_with_publication_lease(
                    request,
                    assets=tuple(assets),
                    heartbeat=lease_heartbeat,
                    publication_key=publication_key,
                )
            except ManagedImageClientError as error:
                raise ImageToolError(
                    error.code, retryable=error.retryable
                ) from error
            descriptor = downloaded.job.result
            if descriptor is None:
                raise ImageToolError("managed_image_result_descriptor_missing")
            await asyncio.to_thread(
                self.publications.stage_cloud_result,
                publication_key,
                token=token,
                cloud_job_id=downloaded.job.job_id,
                result_sha256=descriptor.sha256,
            )
            self._raise_if_publication_lease_lost(lease_heartbeat)
            extension = {
                "image/png": "png", "image/jpeg": "jpg",
                "image/webp": "webp", "image/avif": "avif",
            }[descriptor.mime_type]
            artifact = await asyncio.to_thread(
                self.artifacts.create_artifact,
                downloaded.content,
                requested_name=f"generated-image-{downloaded.job.job_id[-12:]}.{extension}",
                mime_type=descriptor.mime_type,
                declaration=self.artifacts.issue_trusted_deliverable_declaration(
                    "imagegen", family=ArtifactFamily.IMAGE
                ),
                quality_evidence=QualityEvidence(
                    summary="云端图片结果已通过 digest、MIME、长度和 ETag 校验。"
                ),
                lineage=ArtifactLineage(source_artifact_ids=tuple(source_ids)),
                scope=ArtifactScope(
                    account_id=self.account_id,
                    thread_id=scope.thread_id,
                    turn_id=scope.turn_id,
                    created_by_tool_id=marker,
                ),
            )
            artifact = await asyncio.to_thread(
                self.artifacts.ensure_image_renditions,
                artifact.artifact_id,
                revision_id=artifact.revision_id,
            )
            self._raise_if_publication_lease_lost(lease_heartbeat)
            self.fault_hook("after_artifact", publication_key)
            await self._stop_publication_lease(
                lease_stop, lease_heartbeat, propagate=True
            )
            lease_stopped = True
            await asyncio.to_thread(
                self.publications.complete,
                publication_key,
                token=token,
                cloud_job_id=downloaded.job.job_id,
                result_sha256=descriptor.sha256,
                artifact_id=artifact.artifact_id,
                revision_id=artifact.revision_id,
            )
        except BaseException:
            if not lease_stopped:
                await self._stop_publication_lease(
                    lease_stop, lease_heartbeat, propagate=False
                )
            await asyncio.shield(
                asyncio.to_thread(self.publications.release, publication_key, token)
            )
            raise
        result = await asyncio.to_thread(
            self._emit_artifact_item,
            artifact,
            context,
            publication_key,
            downloaded.job.job_id,
        )
        return self._cow_result(result, downloaded.job.model_id)

    async def _image_sources(
        self,
        raw: Any,
        *,
        scope: Any,
    ) -> tuple[list[ManagedImageInputAsset], list[str]]:
        sources = [] if raw is None else raw if isinstance(raw, list) else [raw]
        assets: list[ManagedImageInputAsset] = []
        source_ids: list[str] = []
        seen: set[str] = set()
        for source in sources:
            asset, source_id = await self._image_source(str(source), scope=scope)
            if asset.sha256 in seen:
                continue
            seen.add(asset.sha256)
            assets.append(asset)
            if source_id is not None:
                source_ids.append(source_id)
        return assets, source_ids

    async def _image_source(
        self,
        source: str,
        *,
        scope: Any,
    ) -> tuple[ManagedImageInputAsset, str | None]:
        source = source.strip()
        path = Path(source).expanduser()
        workspace_root = getattr(self, "workspace_root", None)
        if not path.is_absolute() and workspace_root is not None:
            path = workspace_root / path
        if path.is_file():
            if path.stat().st_size > 64 * 1024 * 1024:
                raise ImageToolError("image_url file is oversized")
            content = await asyncio.to_thread(path.read_bytes)
            return self._input_asset(content), None

        artifact_id = self._artifact_id_from_image_url(source)
        if artifact_id is not None:
            try:
                projection = self.artifacts.get_user_artifact(
                    artifact_id, account_id=self.account_id
                )
                if projection.family is not ArtifactFamily.IMAGE:
                    raise ImageToolError("image_url artifact is not an image")
                content = self.artifacts.read_user_content(
                    artifact_id,
                    projection.revision_id,
                    account_id=self.account_id,
                )
                return (
                    ManagedImageInputAsset(
                        sha256=projection.sha256,
                        mime_type=projection.mime_type,
                        content=content,
                    ),
                    artifact_id,
                )
            except ArtifactError:
                pass
            if self.input_attachments is not None:
                try:
                    projection, rendition = self.input_attachments.read_bound_visual(
                        artifact_id,
                        thread_id=scope.thread_id,
                        turn_id=scope.turn_id,
                        max_bytes=8 * 1024 * 1024,
                    )
                    return (
                        ManagedImageInputAsset(
                            sha256=rendition.sha256,
                            mime_type=rendition.mime_type,
                            content=rendition.content,
                        ),
                        projection.attachment_id,
                    )
                except (ArtifactError, InputAttachmentError):
                    pass
            raise ImageToolError("image_url artifact or attachment is unavailable")

        if not source.startswith(("http://", "https://")):
            raise ImageToolError("image_url is not a readable path, URL, or image identity")
        try:
            timeout = httpx.Timeout(60, connect=15)
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                trust_env=True,
            ) as client:
                async with client.stream("GET", source) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 64 * 1024 * 1024:
                            raise ImageToolError("image_url response is oversized")
                        chunks.append(chunk)
        except ImageToolError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise ImageToolError("image_url could not be downloaded") from error
        return self._input_asset(b"".join(chunks)), None

    @staticmethod
    def _artifact_id_from_image_url(source: str) -> str | None:
        if source.startswith("art_") and "/" not in source:
            return source
        matched = re.search(r"/api/v1/artifacts/(art_[A-Za-z0-9_-]+)/preview(?:[?#].*)?$", source)
        return matched.group(1) if matched is not None else None

    @staticmethod
    def _input_asset(content: bytes) -> ManagedImageInputAsset:
        if not 1 <= len(content) <= 64 * 1024 * 1024:
            raise ImageToolError("image_url content is empty or oversized")
        avif = False
        if len(content) >= 16 and content[4:8] == b"ftyp":
            box_size = int.from_bytes(content[:4], "big")
            if 16 <= box_size <= len(content):
                brands = {content[8:12]}
                brands.update(
                    content[offset : offset + 4]
                    for offset in range(16, box_size - 3, 4)
                )
                avif = bool(brands & {b"avif", b"avis"})
        mime_type = (
            "image/png"
            if content.startswith(b"\x89PNG\r\n\x1a\n")
            else "image/jpeg"
            if content.startswith(b"\xff\xd8\xff")
            else "image/webp"
            if len(content) >= 12
            and content.startswith(b"RIFF")
            and content[8:12] == b"WEBP"
            else "image/avif"
            if avif
            else None
        )
        if mime_type is None:
            raise ImageToolError("image_url content is not a supported image")
        return ManagedImageInputAsset(
            sha256=hashlib.sha256(content).hexdigest(),
            mime_type=mime_type,
            content=content,
        )

    @classmethod
    def _cow_result(cls, result: Mapping[str, Any], model: str) -> dict[str, Any]:
        return {**dict(result), "model": model, "images": cls._result_images(result)}

    async def _execute_with_publication_lease(
        self,
        request: ImageSubmitRequest,
        *,
        assets: tuple[ManagedImageInputAsset, ...],
        heartbeat: asyncio.Task[None],
        publication_key: str,
    ) -> ManagedImageDownloadedResult:
        client = self.client
        if client is None:
            raise ImageToolUnavailable("managed image orchestration is unavailable")
        execution = asyncio.create_task(
            client.execute(request, inputs=assets),
            name=f"image-cloud-{publication_key[-16:]}",
        )
        try:
            done, _pending = await asyncio.wait(
                {execution, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                try:
                    self._raise_if_publication_lease_lost(heartbeat)
                finally:
                    execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
            return await execution
        except BaseException:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
            raise

    def _start_publication_lease(
        self, publication_key: str, token: str
    ) -> tuple[asyncio.Event, asyncio.Task[None]]:
        stop = asyncio.Event()

        async def maintain() -> None:
            interval = self.publication_lease_seconds / 3
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                except TimeoutError:
                    renewed = await asyncio.to_thread(
                        self.publications.heartbeat,
                        publication_key,
                        token=token,
                        lease_seconds=self.publication_lease_seconds,
                    )
                    if not renewed:
                        raise ImageToolPublicationBusy(
                            "image publication lease was replaced"
                        )

        heartbeat = asyncio.create_task(
            maintain(),
            name=f"image-publication-heartbeat-{publication_key[-16:]}",
        )
        return stop, heartbeat

    @staticmethod
    def _raise_if_publication_lease_lost(heartbeat: asyncio.Task[None]) -> None:
        if not heartbeat.done():
            return
        if heartbeat.cancelled():
            raise ImageToolPublicationBusy("image publication heartbeat was cancelled")
        error = heartbeat.exception()
        if error is not None:
            raise error
        raise ImageToolError("image publication heartbeat stopped unexpectedly")

    @staticmethod
    async def _stop_publication_lease(
        stop: asyncio.Event,
        heartbeat: asyncio.Task[None],
        *,
        propagate: bool,
    ) -> None:
        stop.set()
        outcome = await asyncio.gather(heartbeat, return_exceptions=True)
        error = outcome[0]
        if propagate and isinstance(error, BaseException):
            raise error

    async def inspect_images(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> dict[str, Any]:
        if context.execution_scope is None:
            raise ImageToolError("vision requires a durable Runtime execution scope")
        artifact_ids = tuple(arguments.get("artifact_ids", ()))
        attachment_ids = tuple(arguments.get("attachment_ids", ()))
        if not artifact_ids and not attachment_ids:
            raise ImageToolError("vision requires at least one image identity")
        if len(artifact_ids) + len(attachment_ids) > 8:
            raise ImageToolError("vision image selection exceeds the product limit")
        inspected = []
        evidence: list[dict[str, str]] = []
        for artifact_id in artifact_ids:
            projection = self.artifacts.get_user_artifact(
                str(artifact_id), account_id=self.account_id
            )
            if projection.family is not ArtifactFamily.IMAGE:
                raise ImageToolError("vision inputs must be image artifacts")
            if projection.size_bytes > 64 * 1024 * 1024:
                raise ImageToolError("vision source exceeds the product limit")
            # Reading through ArtifactService re-verifies the CAS digest.
            content = self.artifacts.read_user_content(
                projection.artifact_id,
                projection.revision_id,
                account_id=self.account_id,
            )
            if hashlib.sha256(content).hexdigest() != projection.sha256:
                raise ImageToolError("vision input digest changed")
            inspected.append(
                {
                    "artifact_id": projection.artifact_id,
                    "revision_id": projection.revision_id,
                    "display_name": projection.display_name,
                    "mime_type": projection.mime_type,
                    "size_bytes": projection.size_bytes,
                    "sha256": projection.sha256,
                    "quality_evidence": projection.quality_evidence.to_dict(),
                }
            )
            evidence.append(
                {
                    "kind": "artifact",
                    "artifact_id": projection.artifact_id,
                    "revision_id": projection.revision_id,
                    "source_sha256": projection.sha256,
                }
            )
        attachment_runtime = self.input_attachments
        if attachment_ids and attachment_runtime is None:
            raise ImageToolUnavailable("input attachment vision runtime is unavailable")
        scope = context.execution_scope
        for attachment_id in attachment_ids:
            projection, rendition = attachment_runtime.read_bound_visual(
                str(attachment_id),
                thread_id=scope.thread_id,
                turn_id=scope.turn_id,
                max_bytes=8 * 1024 * 1024,
            )
            # OCR is deliberately best-effort for visual inspection: it gives
            # the chat model actual textual evidence without weakening the
            # attachment authority boundary. The dedicated OCR tool remains
            # available when exact extraction is the user's requested action.
            ocr: dict[str, Any] | None = None
            try:
                from ecorex.integration.ocr import extract_image_text

                raw_ocr = await asyncio.to_thread(
                    extract_image_text, rendition.content, timeout_seconds=2.0
                )
                if isinstance(raw_ocr, dict):
                    ocr = {
                        "status": str(raw_ocr.get("status") or "unknown"),
                        "provider": str(raw_ocr.get("provider") or "unknown"),
                        "text": str(raw_ocr.get("text") or "")[:12000],
                    }
            except Exception:
                ocr = {"status": "unavailable", "provider": "unavailable", "text": ""}
            inspected.append(
                {
                    "attachment_id": projection.attachment_id,
                    "revision_id": projection.revision_id,
                    "display_name": projection.display_name,
                    "mime_type": projection.mime_type,
                    "size_bytes": projection.size_bytes,
                    "sha256": projection.sha256,
                    "ocr": ocr,
                }
            )
            evidence.append(
                {
                    "kind": "attachment",
                    "attachment_id": projection.attachment_id,
                    "revision_id": projection.revision_id,
                    "source_sha256": projection.sha256,
                }
            )
        return {
            "status": "input_verified",
            "instruction": str(arguments["instruction"]),
            "images": inspected,
            "semantic_result": {
                "status": "pending_model_vision",
                "delivery": "next_assistant_message",
            },
            "requires_model_vision": True,
            "_ecorex_model_visual_evidence": {
                "schema_version": 1,
                "instruction": str(arguments["instruction"]),
                "images": evidence,
            },
            "note": (
                "已验证图片身份与完整性，并附上可用的本地 OCR 证据；"
                "本结果不代表语义视觉已完成，语义答案必须来自已绑定图片的当前对话模型。"
            ),
        }

    def resolve_model_visual_evidence(
        self,
        result: Mapping[str, Any],
        *,
        thread_id: str,
        turn_id: str,
    ) -> tuple[GatewayImageInput, ...]:
        """Re-authorize and render a persisted vision result for one continuation.

        Tool output stores only opaque IDs and digests. Binary bytes are read
        again through the account/Turn authorities and converted by the shared
        bounded visual rendition boundary; paths and original CAS bytes never
        enter the Gateway request.
        """

        raw = result.get("_ecorex_model_visual_evidence")
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            return ()
        images = raw.get("images")
        if not isinstance(images, list) or not 1 <= len(images) <= 4:
            raise ImageToolError("vision evidence contract is invalid")
        if self.input_attachments is None:
            raise ImageToolUnavailable("visual rendition runtime is unavailable")
        projected: list[GatewayImageInput] = []
        for item in images:
            if not isinstance(item, Mapping):
                raise ImageToolError("vision evidence contract is invalid")
            expected_revision = item.get("revision_id")
            expected_source = item.get("source_sha256")
            if not isinstance(expected_revision, str) or not isinstance(
                expected_source, str
            ):
                raise ImageToolError("vision evidence identity is invalid")
            if item.get("kind") == "artifact":
                artifact_id = item.get("artifact_id")
                if not isinstance(artifact_id, str):
                    raise ImageToolError("vision artifact identity is invalid")
                projection = self.artifacts.repository.get_revision_projection(
                    artifact_id,
                    expected_revision,
                    account_id=self.account_id,
                )
                if (
                    projection.family is not ArtifactFamily.IMAGE
                    or projection.sha256 != expected_source
                    or projection.size_bytes > 64 * 1024 * 1024
                ):
                    raise ImageToolError("vision artifact revision changed")
                source = self.artifacts.read_user_content(
                    artifact_id,
                    expected_revision,
                    account_id=self.account_id,
                )
                rendition = self.input_attachments._render_visual(
                    projection,
                    source,
                    max_bytes=384 * 1024,
                    max_dimension=2048,
                )
                opaque_id = artifact_id
            elif item.get("kind") == "attachment":
                attachment_id = item.get("attachment_id")
                if not isinstance(attachment_id, str):
                    raise ImageToolError("vision attachment identity is invalid")
                projection, rendition = self.input_attachments.read_bound_visual(
                    attachment_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
                if (
                    projection.revision_id != expected_revision
                    or projection.sha256 != expected_source
                ):
                    raise ImageToolError("vision attachment revision changed")
                opaque_id = attachment_id
            else:
                raise ImageToolError("vision evidence kind is invalid")
            projected.append(
                GatewayImageInput(
                    attachment_id=opaque_id,
                    revision_id=expected_revision,
                    mime_type=rendition.mime_type,
                    data_base64=base64.b64encode(rendition.content).decode("ascii"),
                    sha256=rendition.sha256,
                    source_sha256=rendition.source_sha256,
                )
            )
        return tuple(projected)

    def _find_marker_artifact(self, marker: str):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT artifact_id FROM artifact_entities "
                "WHERE owner_account_id=? AND created_by_tool_id=?",
                (self.account_id, marker),
            ).fetchall()
        finally:
            connection.close()
        if len(rows) > 1:
            raise ImageToolError("image publication marker has duplicate artifacts")
        if not rows:
            return None
        return self.artifacts.get_user_artifact(
            rows[0]["artifact_id"], account_id=self.account_id
        )

    def _finalize_recovered_publication(
        self, publication_key: str, request_sha: str, artifact: Any
    ) -> str:
        return self.publications.recover_from_artifact(
            publication_key,
            account_id=self.account_id,
            request_sha256=request_sha,
            artifact_id=artifact.artifact_id,
            revision_id=artifact.revision_id,
            artifact_sha256=artifact.sha256,
        )

    def _emit_artifact_item(
        self,
        artifact: Any,
        context: ToolInvocationContext,
        publication_key: str,
        cloud_job_id: str | None,
    ) -> dict[str, Any]:
        scope = context.execution_scope
        if scope is None:
            raise ImageToolError("image Artifact publication requires execution scope")
        item_id = "itm_" + hashlib.sha256(
            f"{publication_key}\0artifact-item".encode("utf-8")
        ).hexdigest()[:32]
        payload = {
            "image_job_id": cloud_job_id,
            "artifact": artifact.to_dict(),
            "change_summary": "图片已生成并保存为办公工件。",
            "preview": {
                "artifact_id": artifact.artifact_id,
                "revision_id": artifact.revision_id,
                "mime_type": artifact.mime_type,
                "url": f"/api/v1/artifacts/{artifact.artifact_id}/preview",
            },
        }
        with self.kernel.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM items WHERE item_id=?", (item_id,)
            ).fetchone()
            if existing is None:
                self.kernel._create_item_in_transaction(
                    connection,
                    item_id=item_id,
                    thread_id=scope.thread_id,
                    turn_id=scope.turn_id,
                    kind=ItemKind.ARTIFACT,
                    content=payload,
                    status=ItemStatus.COMPLETED,
                    idempotency_key=f"{publication_key}:artifact-item",
                )
            event_payload = {
                "artifact_id": artifact.artifact_id,
                "revision_id": artifact.revision_id,
                "sha256": artifact.sha256,
                "image_job_id": cloud_job_id,
            }
            self.kernel.events.append_in_transaction(
                connection,
                thread_id=scope.thread_id,
                turn_id=scope.turn_id,
                item_id=item_id,
                job_id=scope.job_id,
                event_type="artifact.image.generated",
                payload=event_payload,
                correlation_id=context.idempotency_key,
                idempotency_key=f"{publication_key}:generated-event",
            )
        result = {
            "status": "completed",
            "image_job_id": cloud_job_id,
            "artifact_id": artifact.artifact_id,
            "revision_id": artifact.revision_id,
            "mime_type": artifact.mime_type,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
            "preview_url": f"/api/v1/artifacts/{artifact.artifact_id}/preview",
            "summary": "图片已生成，并已在消息和工件列表中展示。",
        }
        return result

    @staticmethod
    def _size(value: str, aspect_ratio: str | None = None) -> tuple[int, int]:
        sizes = {
            "auto": (1024, 1024),
            "1024x1024": (1024, 1024),
            "1536x1024": (1536, 1024),
            "1024x1536": (1024, 1536),
        }
        if aspect_ratio is not None or value.casefold() not in sizes:
            return sizes["auto"]
        return sizes[value.casefold()]

    @staticmethod
    def _client_request_id(value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{7,255}", value):
            return value
        return "imgtool_" + hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "ImageGenerationToolHandler",
    "ImageToolError",
    "ImageToolPublicationBusy",
    "ImageToolUnavailable",
    "ImageVisionToolHandler",
    "RuntimeImageToolBackend",
    "production_pack_adapter_resolver",
]
