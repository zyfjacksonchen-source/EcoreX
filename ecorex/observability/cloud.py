"""Strict managed-session transport for the cloud audit ingestion boundary."""

from __future__ import annotations

import json
import ssl
from typing import Final
from urllib.parse import urlsplit

import httpx

from ecorex.protocol import AuditRecordProjection
from ecorex.session import ManagedSessionService


AUDIT_INGESTION_PATH: Final = "/api/v1/audit/records"
DEFAULT_MAX_AUDIT_REQUEST_BYTES: Final = 1024 * 1024
DEFAULT_MAX_AUDIT_RESPONSE_BYTES: Final = 64 * 1024


class AuditPublishError(RuntimeError):
    """A deliberately non-sensitive cloud publication failure."""

    retryable: bool = False

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RetryableAuditPublishError(AuditPublishError):
    retryable = True


class PermanentAuditPublishError(AuditPublishError):
    retryable = False


def _canonical_json_bytes(record: AuditRecordProjection) -> bytes:
    return json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validated_endpoint(base_url: str, allowed_hosts: frozenset[str]) -> str:
    parsed = urlsplit(base_url)
    normalized_hosts = frozenset(
        value.casefold().rstrip(".") for value in allowed_hosts if value
    )
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("audit endpoint origin is invalid") from error
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname not in normalized_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/", AUDIT_INGESTION_PATH}
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise ValueError("audit endpoint must be an allowlisted HTTPS origin")
    return f"https://{hostname}{AUDIT_INGESTION_PATH}"


def _validate_tls_context(context: ssl.SSLContext | None) -> None:
    if context is not None and (
        context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname
    ):
        raise ValueError("audit TLS context must verify certificates and hostnames")


class ManagedHTTPSAuditPublisher:
    """Publish one redacted record to the fixed Control Plane audit endpoint.

    The class intentionally owns no token cache.  Every attempt obtains a
    freshly validated bearer from ``ManagedSessionService`` and fences it to
    the record's account before any bytes leave the process.
    """

    def __init__(
        self,
        *,
        base_url: str,
        allowed_hosts: frozenset[str],
        session: ManagedSessionService,
        client: httpx.Client | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_request_bytes: int = DEFAULT_MAX_AUDIT_REQUEST_BYTES,
        max_response_bytes: int = DEFAULT_MAX_AUDIT_RESPONSE_BYTES,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("at least one audit host must be allowlisted")
        if session is None:
            raise ValueError("managed session authority is required")
        if not 4096 <= max_request_bytes <= 8 * 1024 * 1024:
            raise ValueError("audit request size limit is invalid")
        if not 1024 <= max_response_bytes <= 1024 * 1024:
            raise ValueError("audit response size limit is invalid")
        _validate_tls_context(ssl_context)
        if client is not None and ssl_context is not None:
            raise ValueError("an injected audit client owns its TLS configuration")
        self.endpoint = _validated_endpoint(base_url, allowed_hosts)
        self.session = session
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=10, read=30, write=30, pool=10),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            verify=ssl_context or True,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def aclose(self) -> None:
        """Expose the Runtime lifecycle contract without adding an async client."""

        self.close()

    def __enter__(self) -> "ManagedHTTPSAuditPublisher":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def publish(self, record: AuditRecordProjection) -> None:
        if not isinstance(record, AuditRecordProjection):
            raise PermanentAuditPublishError(
                "invalid_record", "audit record projection is invalid"
            )
        try:
            before = self.session.snapshot()
            if before.account_id != record.account_id:
                raise PermanentAuditPublishError(
                    "account_mismatch", "audit record account does not match the session"
                )
            token = self.session.bearer_token()
            after = self.session.snapshot()
            if (
                before.account_id != after.account_id
                or before.lease_digest != after.lease_digest
                or before.generation != after.generation
            ):
                raise RetryableAuditPublishError(
                    "session_changed", "managed session changed during audit publication"
                )
        except AuditPublishError:
            raise
        except Exception:
            raise RetryableAuditPublishError(
                "session_unavailable", "managed session is unavailable for audit publication"
            ) from None

        body = _canonical_json_bytes(record)
        if len(body) > self.max_request_bytes:
            raise PermanentAuditPublishError(
                "request_too_large", "audit record exceeds the publication size limit"
            )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Idempotency-Key": record.audit_id,
        }
        try:
            request = self.client.build_request(
                "POST", self.endpoint, headers=headers, content=body
            )
            response = self.client.send(request, stream=True, follow_redirects=False)
            try:
                self._consume_response(response)
            finally:
                response.close()
        except AuditPublishError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise RetryableAuditPublishError(
                "transport_unavailable", "cloud audit transport is unavailable"
            ) from None
        finally:
            # Do not retain a second token reference beyond the request attempt.
            token = ""

    def _consume_response(self, response: httpx.Response) -> None:
        if response.is_redirect or response.history:
            raise PermanentAuditPublishError(
                "redirect_refused", "cloud audit redirects are forbidden"
            )
        if response.headers.get("content-encoding", "identity").casefold() != "identity":
            raise PermanentAuditPublishError(
                "compressed_response", "compressed cloud audit responses are forbidden"
            )
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                parsed_length = int(content_length)
            except ValueError:
                raise PermanentAuditPublishError(
                    "invalid_response", "cloud audit response metadata is invalid"
                ) from None
            if parsed_length < 0 or parsed_length > self.max_response_bytes:
                raise PermanentAuditPublishError(
                    "response_too_large", "cloud audit response exceeds its size limit"
                )
        received = 0
        for chunk in response.iter_bytes():
            received += len(chunk)
            if received > self.max_response_bytes:
                raise PermanentAuditPublishError(
                    "response_too_large", "cloud audit response exceeds its size limit"
                )
        status = response.status_code
        if status in {200, 201, 202, 204}:
            return
        if status in {401, 408, 425, 429} or 500 <= status <= 599:
            code = "authentication_stale" if status == 401 else "remote_retryable"
            raise RetryableAuditPublishError(
                code, "cloud audit publication should be retried"
            )
        raise PermanentAuditPublishError(
            "remote_rejected", "cloud audit publication was permanently rejected"
        )


__all__ = [
    "AUDIT_INGESTION_PATH",
    "AuditPublishError",
    "ManagedHTTPSAuditPublisher",
    "PermanentAuditPublishError",
    "RetryableAuditPublishError",
]
