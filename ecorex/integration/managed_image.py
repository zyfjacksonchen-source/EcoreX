"""Strict managed client for the cloud-authoritative image job service."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

import httpx

from ecorex.image_orchestrator import ImageOperation, ImageSubmitRequest
from ecorex.runtime.database import SQLiteDatabase
from ecorex.session import ManagedSessionService, ManagedSessionSnapshot


_JOB_ID = re.compile(r"^imgjob_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_MIME = frozenset({"image/png", "image/jpeg", "image/webp", "image/avif"})
_TERMINAL = frozenset({"completed", "cancelled", "failed", "dead_letter"})


def _session_continuity(snapshot: ManagedSessionSnapshot) -> tuple[object, ...]:
    """Logical session identity that credential-only refreshes cannot change."""

    return (
        snapshot.account_id,
        snapshot.organization_id,
        frozenset(snapshot.roles),
        frozenset(snapshot.model_allowlist),
        tuple(sorted(snapshot.quota.items())),
        frozenset(snapshot.admin_denies),
        snapshot.expires_at,
    )


def _strict_json_int(value: Any) -> int:
    if type(value) is not int:
        raise ValueError("managed image protocol integer is invalid")
    return value


def _strict_json_str(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("managed image protocol string is invalid")
    return value


class ManagedImageClientError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        normalized = str(code or "managed_image_error").strip().casefold().replace("-", "_")
        if not re.fullmatch(r"[a-z][a-z0-9_.:]{0,127}", normalized):
            normalized = "managed_image_error"
        super().__init__(normalized)
        self.code = normalized
        self.retryable = bool(retryable)


@dataclass(frozen=True, slots=True)
class ManagedImageResultDescriptor:
    sha256: str
    size_bytes: int
    mime_type: str

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("managed image result digest is invalid")
        if not 1 <= self.size_bytes <= 256 * 1024 * 1024:
            raise ValueError("managed image result size is invalid")
        if self.mime_type not in _MIME:
            raise ValueError("managed image result MIME type is invalid")


@dataclass(frozen=True, slots=True)
class ManagedImageJob:
    job_id: str
    operation: str
    model_id: str
    status: str
    attempt: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    deadline: datetime
    result: ManagedImageResultDescriptor | None
    last_error_code: str | None

    def __post_init__(self) -> None:
        if not _JOB_ID.fullmatch(self.job_id):
            raise ValueError("managed image job identity is invalid")
        if self.operation not in {"generate", "retouch"}:
            raise ValueError("managed image job operation is invalid")
        if not isinstance(self.model_id, str) or not _MODEL_ID.fullmatch(self.model_id):
            raise ValueError("managed image job model identity is invalid")
        if self.status not in {
            "accepted", "queued", "leased", "running", "verifying", "committing",
            "retry_wait", "completed", "cancelled", "failed", "dead_letter",
        }:
            raise ValueError("managed image job status is invalid")
        if (
            self.created_at.utcoffset() is None
            or self.updated_at.utcoffset() is None
            or self.deadline.utcoffset() is None
        ):
            raise ValueError("managed image job timestamps must be timezone-aware")
        if self.updated_at < self.created_at or self.deadline <= self.created_at:
            raise ValueError("managed image job timestamps are inconsistent")
        if (
            type(self.attempt) is not int
            or type(self.max_attempts) is not int
            or not 1 <= self.max_attempts <= 10
            or not 0 <= self.attempt <= self.max_attempts
        ):
            raise ValueError("managed image job attempt counters are invalid")
        if self.status == "completed" and self.result is None:
            raise ValueError("completed managed image job has no result")
        if self.status != "completed" and self.result is not None:
            raise ValueError("non-completed managed image job cannot expose a result")
        if self.last_error_code is not None and (
            not isinstance(self.last_error_code, str)
            or not _ERROR_CODE.fullmatch(self.last_error_code)
        ):
            raise ValueError("managed image job error code is invalid")


@dataclass(frozen=True, slots=True)
class ManagedImageInputAsset:
    sha256: str
    mime_type: str
    content: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        content = bytes(self.content)
        if not _SHA256.fullmatch(self.sha256) or hashlib.sha256(content).hexdigest() != self.sha256:
            raise ValueError("managed image input digest does not match content")
        if self.mime_type not in _MIME:
            raise ValueError("managed image input MIME type is unsupported")
        if not 1 <= len(content) <= 256 * 1024 * 1024:
            raise ValueError("managed image input size is invalid")
        object.__setattr__(self, "content", content)


@dataclass(frozen=True, slots=True)
class ManagedImageDownloadedResult:
    job: ManagedImageJob
    content: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        content = bytes(self.content)
        if self.job.result is None:
            raise ValueError("downloaded image requires a result descriptor")
        if len(content) != self.job.result.size_bytes:
            raise ValueError("downloaded image size does not match its descriptor")
        if hashlib.sha256(content).hexdigest() != self.job.result.sha256:
            raise ValueError("downloaded image digest does not match its descriptor")
        object.__setattr__(self, "content", content)


@dataclass(frozen=True, slots=True)
class _JournalRecord:
    account_id: str
    client_request_id: str
    request_fingerprint: str
    job_id: str
    status: str


class ManagedImageJobJournal:
    """Local crash-recovery index; prompt and image bytes are never stored."""

    def __init__(self, database_path: str | Path) -> None:
        self.database = SQLiteDatabase(database_path)
        self.path = self.database.path

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

    def get(self, account_id: str, client_request_id: str) -> _JournalRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM managed_image_job_journal WHERE account_id=? AND client_request_id=?",
                (account_id, client_request_id),
            ).fetchone()
        finally:
            connection.close()
        return self._record(row) if row is not None else None

    def bind(
        self,
        account_id: str,
        request: ImageSubmitRequest,
        job: ManagedImageJob,
    ) -> _JournalRecord:
        fingerprint = request.fingerprint()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM managed_image_job_journal WHERE account_id=? AND client_request_id=?",
                (account_id, request.client_request_id),
            ).fetchone()
            if row is not None:
                record = self._record(row)
                if record.request_fingerprint != fingerprint or record.job_id != job.job_id:
                    raise ManagedImageClientError("managed_image_journal_conflict", retryable=False)
                connection.execute(
                    "UPDATE managed_image_job_journal SET status=?,updated_at=? "
                    "WHERE account_id=? AND client_request_id=?",
                    (job.status, datetime.now(UTC).isoformat(), account_id, request.client_request_id),
                )
            else:
                connection.execute(
                    "INSERT INTO managed_image_job_journal(account_id,client_request_id,"
                    "request_fingerprint,job_id,status,updated_at) VALUES(?,?,?,?,?,?)",
                    (
                        account_id,
                        request.client_request_id,
                        fingerprint,
                        job.job_id,
                        job.status,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return _JournalRecord(
            account_id, request.client_request_id, fingerprint, job.job_id, job.status
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> _JournalRecord:
        return _JournalRecord(
            account_id=str(row["account_id"]),
            client_request_id=str(row["client_request_id"]),
            request_fingerprint=str(row["request_fingerprint"]),
            job_id=str(row["job_id"]),
            status=str(row["status"]),
        )


class ManagedImageOrchestrationClient:
    """Managed-session-fenced client for one fixed image orchestration root."""

    def __init__(
        self,
        root_url: str,
        *,
        session: ManagedSessionService,
        allowed_hosts: frozenset[str],
        database_path: str | Path,
        client: httpx.AsyncClient | None = None,
        poll_interval_seconds: float = 0.5,
        max_poll_seconds: float = 900,
        max_json_bytes: int = 2 * 1024 * 1024,
        max_result_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        parsed = urlsplit(root_url)
        hosts = frozenset(str(host).casefold() for host in allowed_hosts if host)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname.casefold() not in hosts
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/api/v1/images"
        ):
            raise ValueError("image orchestration root must be an allowlisted fixed HTTPS v1 root")
        if not isinstance(session, ManagedSessionService):
            raise TypeError("managed image client requires the exact ManagedSessionService")
        if not 0.05 <= poll_interval_seconds <= 10:
            raise ValueError("managed image poll interval is invalid")
        if not 1 <= max_poll_seconds <= 86_400:
            raise ValueError("managed image polling deadline is invalid")
        if not 1024 <= max_json_bytes <= 16 * 1024 * 1024:
            raise ValueError("managed image JSON limit is invalid")
        if not 1024 <= max_result_bytes <= 256 * 1024 * 1024:
            raise ValueError("managed image result limit is invalid")
        self.root_url = root_url.rstrip("/")
        self.session = session
        self.journal = ManagedImageJobJournal(database_path)
        self.poll_interval_seconds = poll_interval_seconds
        self.max_poll_seconds = max_poll_seconds
        self.max_json_bytes = max_json_bytes
        self.max_result_bytes = max_result_bytes
        self._account_id: str | None = None
        self._binding_lock = threading.Lock()
        self._operation_binding: ContextVar[
            tuple[object, ...] | None
        ] = ContextVar("managed_image_operation_binding", default=None)
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=60, write=120, pool=10),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _session(self) -> tuple[ManagedSessionSnapshot, tuple[object, ...], str]:
        try:
            snapshot = self.session.snapshot()
            token = self.session.bearer_token()
        except Exception:
            raise ManagedImageClientError("managed_image_auth_unavailable", retryable=True) from None
        binding = _session_continuity(snapshot)
        with self._binding_lock:
            if self._account_id is None:
                self._account_id = snapshot.account_id
            elif self._account_id != snapshot.account_id:
                raise ManagedImageClientError("managed_image_session_changed", retryable=False)
        expected = self._operation_binding.get()
        if expected is not None and expected != binding:
            raise ManagedImageClientError("managed_image_session_changed", retryable=False)
        if (
            not isinstance(token, str)
            or not 24 <= len(token) <= 4096
            or any(not 33 <= ord(character) <= 126 for character in token)
        ):
            raise ManagedImageClientError("managed_image_auth_invalid", retryable=False)
        return snapshot, binding, token

    def _verify_session(self, expected: tuple[object, ...]) -> None:
        try:
            snapshot = self.session.snapshot()
        except Exception:
            raise ManagedImageClientError("managed_image_session_changed", retryable=False) from None
        actual = _session_continuity(snapshot)
        if actual != expected:
            raise ManagedImageClientError("managed_image_session_changed", retryable=False)

    @contextmanager
    def operation_scope(self) -> Iterator[None]:
        """Freeze one logical session across all child requests in an operation."""

        _snapshot, binding, _token = self._session()
        operation = self._operation_binding.set(binding)
        try:
            yield
        finally:
            self._operation_binding.reset(operation)

    async def upload_input(self, asset: ManagedImageInputAsset) -> None:
        response, _account = await self._request(
            "PUT",
            f"/inputs/{asset.sha256}",
            content=asset.content,
            headers={"Content-Type": asset.mime_type},
        )
        payload = self._decode_json(response, expected={"sha256", "size_bytes", "mime_type"})
        if (
            payload["sha256"] != asset.sha256
            or payload["mime_type"] != asset.mime_type
            or payload["size_bytes"] != len(asset.content)
        ):
            raise ManagedImageClientError("managed_image_input_commitment_mismatch", retryable=False)

    async def submit(self, request: ImageSubmitRequest) -> tuple[ManagedImageJob, bool]:
        response, account_id = await self._request(
            "POST",
            "/jobs",
            json_body={
                "operation": request.operation.value,
                "model_id": request.model_id,
                "client_request_id": request.client_request_id,
                "prompt": request.prompt,
                "width": request.width,
                "height": request.height,
                "count": request.count,
                "input_sha256": list(request.input_sha256),
                "instruction": request.instruction,
                "priority": request.priority,
                "max_attempts": request.max_attempts,
                "deadline_seconds": request.deadline_seconds,
                "metadata": dict(request.metadata),
            },
        )
        payload = self._decode_json(response, expected={"created", "job"})
        if not isinstance(payload["created"], bool):
            raise ManagedImageClientError("managed_image_protocol", retryable=False)
        job = self._job(payload["job"])
        self.journal.bind(account_id, request, job)
        return job, bool(payload["created"])

    async def get(self, job_id: str) -> ManagedImageJob:
        self._require_job_id(job_id)
        response, _account = await self._request("GET", f"/jobs/{job_id}")
        return self._job(self._decode_json(response, expected=self._job_keys()))

    async def cancel(self, job_id: str) -> ManagedImageJob:
        self._require_job_id(job_id)
        response, _account = await self._request("POST", f"/jobs/{job_id}/cancel")
        return self._job(self._decode_json(response, expected=self._job_keys()))

    async def recover(self, job_id: str, *, recovery_request_id: str) -> ManagedImageJob:
        self._require_job_id(job_id)
        response, _account = await self._request(
            "POST",
            f"/jobs/{job_id}/recover",
            json_body={"recovery_request_id": recovery_request_id},
        )
        return self._job(self._decode_json(response, expected=self._job_keys()))

    async def poll(
        self,
        job: ManagedImageJob,
        *,
        timeout_seconds: float | None = None,
    ) -> ManagedImageJob:
        deadline_remaining = (job.deadline - datetime.now(UTC)).total_seconds()
        if deadline_remaining <= 0:
            raise ManagedImageClientError("managed_image_deadline", retryable=True)
        timeout = min(
            float(timeout_seconds or self.max_poll_seconds),
            self.max_poll_seconds,
            deadline_remaining,
        )
        if timeout <= 0:
            raise ManagedImageClientError("managed_image_deadline", retryable=True)
        stop = time.monotonic() + timeout
        current = job
        while current.status not in _TERMINAL:
            remaining = stop - time.monotonic()
            if remaining <= 0:
                raise ManagedImageClientError("managed_image_poll_timeout", retryable=True)
            await asyncio.sleep(min(self.poll_interval_seconds, remaining))
            current = await self.get(current.job_id)
        if current.status != "completed":
            retryable = (
                current.status == "dead_letter"
                and current.last_error_code != "provider_uncertain"
            )
            raise ManagedImageClientError(
                current.last_error_code or f"managed_image_{current.status}",
                retryable=retryable,
            )
        return current

    async def download_result(self, job: ManagedImageJob) -> ManagedImageDownloadedResult:
        if job.status != "completed" or job.result is None:
            raise ManagedImageClientError("managed_image_result_not_ready", retryable=True)
        response, _account = await self._request(
            "GET", f"/jobs/{job.job_id}/result", accept_json=False
        )
        descriptor = job.result
        if response.headers.get("content-encoding", "identity").casefold() != "identity":
            raise ManagedImageClientError("managed_image_result_encoding", retryable=False)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        if content_type != descriptor.mime_type:
            raise ManagedImageClientError("managed_image_result_mime_mismatch", retryable=False)
        if response.headers.get("etag") != f'"{descriptor.sha256}"':
            raise ManagedImageClientError("managed_image_result_etag_mismatch", retryable=False)
        if response.headers.get("x-content-sha256") != descriptor.sha256:
            raise ManagedImageClientError("managed_image_result_digest_header_mismatch", retryable=False)
        declared = response.headers.get("content-length")
        try:
            declared_size = int(declared) if declared is not None else -1
        except ValueError:
            declared_size = -1
        if declared_size != descriptor.size_bytes or len(response.content) != descriptor.size_bytes:
            raise ManagedImageClientError("managed_image_result_size_mismatch", retryable=False)
        if len(response.content) > self.max_result_bytes:
            raise ManagedImageClientError("managed_image_result_too_large", retryable=False)
        if hashlib.sha256(response.content).hexdigest() != descriptor.sha256:
            raise ManagedImageClientError("managed_image_result_digest_mismatch", retryable=False)
        return ManagedImageDownloadedResult(job=job, content=bytes(response.content))

    async def execute(
        self,
        request: ImageSubmitRequest,
        *,
        inputs: tuple[ManagedImageInputAsset, ...] = (),
    ) -> ManagedImageDownloadedResult:
        snapshot, binding, _token = self._session()
        operation = self._operation_binding.set(binding)
        try:
            record = await asyncio.to_thread(
                self.journal.get, snapshot.account_id, request.client_request_id
            )
            if record is not None:
                if record.request_fingerprint != request.fingerprint():
                    raise ManagedImageClientError(
                        "managed_image_journal_conflict", retryable=False
                    )
                recovery_id = "imgrecover_" + hashlib.sha256(
                    f"{snapshot.account_id}\0{request.client_request_id}".encode("utf-8")
                ).hexdigest()
                # A known cloud identity is always recovered before any submit.
                job = await self.recover(record.job_id, recovery_request_id=recovery_id)
            else:
                by_digest = {asset.sha256: asset for asset in inputs}
                if set(by_digest) != set(request.input_sha256):
                    raise ManagedImageClientError(
                        "managed_image_inputs_incomplete", retryable=False
                    )
                for digest in request.input_sha256:
                    await self.upload_input(by_digest[digest])
                job, _created = await self.submit(request)
            completed = await self.poll(job, timeout_seconds=request.deadline_seconds)
            return await self.download_result(completed)
        finally:
            self._operation_binding.reset(operation)

    async def recover_result(
        self,
        client_request_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ManagedImageDownloadedResult | None:
        snapshot, binding, _token = self._session()
        operation = self._operation_binding.set(binding)
        try:
            record = await asyncio.to_thread(
                self.journal.get, snapshot.account_id, client_request_id
            )
            if record is None:
                return None
            recovery_id = "imgrecover_" + hashlib.sha256(
                f"{snapshot.account_id}\0{client_request_id}".encode("utf-8")
            ).hexdigest()
            job = await self.recover(record.job_id, recovery_request_id=recovery_id)
            completed = await self.poll(job, timeout_seconds=timeout_seconds)
            return await self.download_result(completed)
        finally:
            self._operation_binding.reset(operation)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        accept_json: bool = True,
    ) -> tuple[httpx.Response, str]:
        snapshot, binding, token = self._session()
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json" if accept_json else "image/*",
            "Accept-Encoding": "identity",
            "X-EcoreX-Protocol": "1",
            "X-EcoreX-Session-Generation": str(snapshot.generation),
            **dict(headers or {}),
        }
        try:
            response = await self.client.request(
                method,
                self.root_url + path,
                headers=request_headers,
                json=dict(json_body) if json_body is not None else None,
                content=content,
            )
        except httpx.TimeoutException as error:
            raise ManagedImageClientError("managed_image_timeout", retryable=True) from error
        except httpx.TransportError as error:
            raise ManagedImageClientError("managed_image_transport", retryable=True) from error
        self._verify_session(binding)
        if response.status_code in {401, 403}:
            raise ManagedImageClientError("managed_image_auth_rejected", retryable=False)
        if response.status_code == 404:
            raise ManagedImageClientError("managed_image_not_found", retryable=False)
        if response.status_code == 409:
            raise ManagedImageClientError("managed_image_conflict", retryable=False)
        if response.status_code == 429 or response.status_code >= 500:
            raise ManagedImageClientError("managed_image_unavailable", retryable=True)
        if not 200 <= response.status_code < 300:
            raise ManagedImageClientError("managed_image_rejected", retryable=False)
        if len(response.content) > (self.max_json_bytes if accept_json else self.max_result_bytes):
            raise ManagedImageClientError("managed_image_response_too_large", retryable=False)
        return response, snapshot.account_id

    def _decode_json(self, response: httpx.Response, *, expected: set[str]) -> dict[str, Any]:
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        if media_type != "application/json":
            raise ManagedImageClientError("managed_image_protocol", retryable=False)
        try:
            payload = json.loads(response.content)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise ManagedImageClientError("managed_image_protocol", retryable=False) from None
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ManagedImageClientError("managed_image_protocol", retryable=False)
        return payload

    @classmethod
    def _job(cls, payload: Any) -> ManagedImageJob:
        if not isinstance(payload, Mapping) or set(payload) != cls._job_keys():
            raise ManagedImageClientError("managed_image_protocol", retryable=False)
        try:
            result_raw = payload["result"]
            result = None
            if result_raw is not None:
                if not isinstance(result_raw, Mapping) or set(result_raw) != {"sha256", "size_bytes", "mime_type"}:
                    raise ValueError("invalid result")
                result = ManagedImageResultDescriptor(
                    sha256=_strict_json_str(result_raw["sha256"]),
                    size_bytes=_strict_json_int(result_raw["size_bytes"]),
                    mime_type=_strict_json_str(result_raw["mime_type"]),
                )
            return ManagedImageJob(
                job_id=_strict_json_str(payload["job_id"]),
                operation=_strict_json_str(payload["operation"]),
                model_id=_strict_json_str(payload["model_id"]),
                status=_strict_json_str(payload["status"]),
                attempt=_strict_json_int(payload["attempt"]),
                max_attempts=_strict_json_int(payload["max_attempts"]),
                created_at=datetime.fromisoformat(
                    _strict_json_str(payload["created_at"]).replace("Z", "+00:00")
                ),
                updated_at=datetime.fromisoformat(
                    _strict_json_str(payload["updated_at"]).replace("Z", "+00:00")
                ),
                deadline=datetime.fromisoformat(
                    _strict_json_str(payload["deadline"]).replace("Z", "+00:00")
                ),
                result=result,
                last_error_code=(
                    _strict_json_str(payload["last_error_code"])
                    if payload["last_error_code"] is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ManagedImageClientError("managed_image_protocol", retryable=False) from None

    @staticmethod
    def _job_keys() -> set[str]:
        return {
            "job_id", "operation", "model_id", "status", "attempt", "max_attempts",
            "created_at", "updated_at", "deadline", "result", "last_error_code",
        }

    @staticmethod
    def _require_job_id(job_id: str) -> None:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ValueError("managed image job identity is invalid")


__all__ = [
    "ManagedImageClientError",
    "ManagedImageDownloadedResult",
    "ManagedImageInputAsset",
    "ManagedImageJob",
    "ManagedImageJobJournal",
    "ManagedImageOrchestrationClient",
    "ManagedImageResultDescriptor",
]
