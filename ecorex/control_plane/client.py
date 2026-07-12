"""Strict HTTPS client for the administrator release Control Plane."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from ecorex.update import ReleaseChannel

from .models import (
    BootstrapFreshnessRunProjection,
    BootstrapFreshnessStatusProjection,
    BootstrapIndexProofProjection,
    CandidateProjection,
    CreateCandidateRequest,
    CreateRollbackRequest,
    CreateRolloutRequest,
    DistributionProjection,
    GateResultRequest,
    KillSwitchProjection,
    RolloutActionRequest,
    RollbackProjection,
    RolloutProjection,
)


class AdminCredentialProvider(Protocol):
    def bearer_token(self) -> str:
        ...


class ControlPlaneClientError(RuntimeError):
    pass


class ControlPlaneAuthenticationError(ControlPlaneClientError):
    pass


class ControlPlaneRequestError(ControlPlaneClientError):
    def __init__(self, message: str, *, status_code: int, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class EnvironmentAdminCredential:
    """Read an administrator token at call time without retaining it in config."""

    def __init__(self, environment: Mapping[str, str], name: str) -> None:
        self.environment = environment
        self.name = name

    def bearer_token(self) -> str:
        try:
            return self.environment[self.name]
        except KeyError:
            raise ControlPlaneAuthenticationError(
                f"administrator credential environment variable {self.name!r} is missing"
            ) from None


Projection = TypeVar("Projection", bound=BaseModel)
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


class AdminControlPlaneClient:
    def __init__(
        self,
        endpoint: str,
        *,
        credentials: AdminCredentialProvider,
        allowed_hosts: frozenset[str],
        client: httpx.Client | None = None,
        max_response_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "Control Plane endpoint must be a credential-free HTTPS URL"
            )
        normalized_hosts = frozenset(
            host.strip().casefold() for host in allowed_hosts if host.strip()
        )
        if (
            not normalized_hosts
            or parsed.hostname.casefold() not in normalized_hosts
        ):
            raise ValueError("Control Plane host is not explicitly allowlisted")
        if not 64 * 1024 <= max_response_bytes <= 16 * 1024 * 1024:
            raise ValueError("Control Plane response limit is invalid")
        self.endpoint = endpoint.rstrip("/")
        self.credentials = credentials
        self.max_response_bytes = max_response_bytes
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=10, read=30, write=30, pool=10),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "AdminControlPlaneClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def create_candidate(
        self, manifest: dict[str, Any], *, client_request_id: str
    ) -> CandidateProjection:
        request = CreateCandidateRequest(
            manifest=manifest, client_request_id=client_request_id
        )
        return self._request(
            "POST", "/api/v1/admin/releases", CandidateProjection, request
        )

    def record_gate(
        self,
        release_id: str,
        gate_name: str,
        *,
        status: str,
        evidence: str,
        client_request_id: str,
    ) -> CandidateProjection:
        request = GateResultRequest(
            status=status,
            evidence=evidence,
            client_request_id=client_request_id,
        )
        return self._request(
            "PUT",
            f"/api/v1/admin/releases/{_segment(release_id)}/gates/{_segment(gate_name)}",
            CandidateProjection,
            request,
        )

    def publish(
        self, release_id: str, *, client_request_id: str
    ) -> CandidateProjection:
        return self._request(
            "POST",
            f"/api/v1/admin/releases/{_segment(release_id)}/publish",
            CandidateProjection,
            RolloutActionRequest(client_request_id=client_request_id),
        )

    def create_rollout(
        self,
        release_id: str,
        *,
        percentage: int,
        organizations: list[str],
        accounts: list[str],
        minimum_compatible_version: str | None,
        client_request_id: str,
    ) -> RolloutProjection:
        request = CreateRolloutRequest(
            release_id=release_id,
            percentage=percentage,
            target_organization_ids=organizations,
            target_account_ids=accounts,
            minimum_compatible_version=minimum_compatible_version,
            client_request_id=client_request_id,
        )
        return self._request(
            "POST", "/api/v1/admin/rollouts", RolloutProjection, request
        )

    def rollout_action(
        self, rollout_id: str, action: str, *, client_request_id: str
    ) -> RolloutProjection:
        if action not in {"activate", "pause", "halt"}:
            raise ValueError("rollout action is invalid")
        return self._request(
            "POST",
            f"/api/v1/admin/rollouts/{_segment(rollout_id)}/{action}",
            RolloutProjection,
            RolloutActionRequest(client_request_id=client_request_id),
        )

    def create_rollback(
        self,
        source_release_id: str,
        target_release_id: str,
        *,
        percentage: int,
        organizations: list[str],
        accounts: list[str],
        authorization_ttl_seconds: int = 300,
        client_request_id: str,
    ) -> RollbackProjection:
        request = CreateRollbackRequest(
            source_release_id=source_release_id,
            target_release_id=target_release_id,
            percentage=percentage,
            target_organization_ids=organizations,
            target_account_ids=accounts,
            authorization_ttl_seconds=authorization_ttl_seconds,
            client_request_id=client_request_id,
        )
        return self._request(
            "POST", "/api/v1/admin/rollbacks", RollbackProjection, request
        )

    def rollback_action(
        self, rollback_id: str, action: str, *, client_request_id: str
    ) -> RollbackProjection:
        if action not in {"activate", "pause", "halt"}:
            raise ValueError("rollback action is invalid")
        return self._request(
            "POST",
            f"/api/v1/admin/rollbacks/{_segment(rollback_id)}/{action}",
            RollbackProjection,
            RolloutActionRequest(client_request_id=client_request_id),
        )

    def distribution(self) -> DistributionProjection:
        return self._request(
            "GET", "/api/v1/admin/distribution", DistributionProjection
        )

    def trusted_bootstrap_index_proof(
        self, release_id: str
    ) -> BootstrapIndexProofProjection:
        return self._request(
            "GET",
            f"/api/v1/admin/bootstrap-index/proofs/{_segment(release_id)}",
            BootstrapIndexProofProjection,
        )

    def bootstrap_freshness_status(self) -> BootstrapFreshnessStatusProjection:
        return self._request(
            "GET",
            "/api/v1/admin/bootstrap-index/freshness",
            BootstrapFreshnessStatusProjection,
        )

    def refresh_bootstrap_freshness(
        self, *, client_request_id: str
    ) -> BootstrapFreshnessRunProjection:
        return self._request(
            "POST",
            "/api/v1/admin/bootstrap-index/freshness/refresh",
            BootstrapFreshnessRunProjection,
            RolloutActionRequest(client_request_id=client_request_id),
        )

    def set_kill_switch(
        self,
        channel: ReleaseChannel,
        *,
        active: bool,
        client_request_id: str,
    ) -> KillSwitchProjection:
        suffix = "kill-switch" if active else "kill-switch/clear"
        return self._request(
            "POST",
            f"/api/v1/admin/channels/{channel.value}/{suffix}",
            KillSwitchProjection,
            RolloutActionRequest(client_request_id=client_request_id),
        )

    def _request(
        self,
        method: str,
        path: str,
        projection: type[Projection],
        body: BaseModel | None = None,
    ) -> Projection:
        try:
            token = self.credentials.bearer_token()
        except ControlPlaneAuthenticationError:
            raise
        except Exception:
            raise ControlPlaneAuthenticationError(
                "administrator credential is unavailable"
            ) from None
        if (
            not isinstance(token, str)
            or not 24 <= len(token) <= 4096
            or any(not 33 <= ord(character) <= 126 for character in token)
        ):
            raise ControlPlaneAuthenticationError(
                "administrator credential is invalid"
            )
        encoded = body.model_dump_json().encode("utf-8") if body is not None else None
        if encoded is not None and len(encoded) > 4 * 1024 * 1024:
            raise ControlPlaneClientError("Control Plane request exceeds its size limit")
        try:
            response = self.client.request(
                method,
                self.endpoint + path,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    **(
                        {"Content-Type": "application/json"}
                        if encoded is not None
                        else {}
                    ),
                },
                content=encoded,
                follow_redirects=False,
            )
        except httpx.TransportError:
            raise ControlPlaneClientError("Control Plane transport failed") from None
        if 300 <= response.status_code < 400:
            raise ControlPlaneClientError("Control Plane redirect was refused")
        payload = self._decode_response(response)
        if not 200 <= response.status_code < 300:
            message, code = _safe_error(payload, response.status_code)
            if response.status_code in {401, 403}:
                raise ControlPlaneAuthenticationError(message)
            raise ControlPlaneRequestError(
                message, status_code=response.status_code, code=code
            )
        try:
            return projection.model_validate(payload)
        except ValidationError:
            raise ControlPlaneClientError(
                "Control Plane returned an invalid response contract"
            ) from None

    def _decode_response(self, response: httpx.Response) -> Any:
        if response.headers.get("content-encoding", "identity").casefold() != "identity":
            raise ControlPlaneClientError("Control Plane response encoding is unsupported")
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                raise ControlPlaneClientError(
                    "Control Plane returned an invalid Content-Length"
                ) from None
            if declared_size < 0 or declared_size > self.max_response_bytes:
                raise ControlPlaneClientError(
                    "Control Plane response exceeds its size limit"
                )
        content = response.content
        if declared is not None and declared_size != len(content):
            raise ControlPlaneClientError(
                "Control Plane response length does not match Content-Length"
            )
        if len(content) > self.max_response_bytes:
            raise ControlPlaneClientError("Control Plane response exceeds its size limit")
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if media_type != "application/json":
            raise ControlPlaneClientError(
                "Control Plane returned an unsupported response type"
            )
        try:
            return json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise ControlPlaneClientError(
                "Control Plane returned invalid JSON"
            ) from None


def _segment(value: str) -> str:
    if not isinstance(value, str) or _SAFE_SEGMENT.fullmatch(value) is None:
        raise ValueError("Control Plane resource identifier is invalid")
    return value


def _safe_error(payload: Any, status_code: int) -> tuple[str, str | None]:
    fallback = f"Control Plane request failed ({status_code})"
    if not isinstance(payload, dict):
        return fallback, None
    detail = payload.get("detail")
    if isinstance(detail, str) and 0 < len(detail) <= 512:
        return detail, None
    if isinstance(detail, dict):
        message = detail.get("message")
        code = detail.get("code")
        return (
            message if isinstance(message, str) and 0 < len(message) <= 512 else fallback,
            code if isinstance(code, str) and len(code) <= 128 else None,
        )
    return fallback, None


__all__ = [
    "AdminControlPlaneClient",
    "AdminCredentialProvider",
    "ControlPlaneAuthenticationError",
    "ControlPlaneClientError",
    "ControlPlaneRequestError",
    "EnvironmentAdminCredential",
]
