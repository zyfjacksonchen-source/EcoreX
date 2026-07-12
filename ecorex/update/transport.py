"""Fail-closed HTTPS/WSS transport for signed release delivery."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import ssl
import stat
import secrets
import threading
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import httpx

from .fetching import ArtifactFetcher, FetchError
from .manifest import (
    MAX_MANIFEST_BYTES,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
)
from .verification import SignatureVerifier, verify_manifest_signature
from .rollback import (
    ROLLBACK_AUTHORIZATION_HEADER,
    RollbackAuthorizationError,
    SingleUseRollbackAuthorizer,
)


class UpdateTransportError(RuntimeError):
    pass


class UpdateAuthenticationError(UpdateTransportError):
    pass


class UpdateProtocolError(UpdateTransportError):
    pass


class UpdateUnavailable(UpdateTransportError):
    pass


class ControlPlaneCredentialProvider(Protocol):
    def bearer_token(self) -> str:
        ...


class RejectingControlPlaneCredentialProvider:
    def bearer_token(self) -> str:
        raise UpdateAuthenticationError("Control Plane session is unavailable")


def _validate_tls_context(context: ssl.SSLContext | None) -> None:
    if context is not None and (
        context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname
    ):
        raise ValueError("update TLS context must verify certificates and hostnames")


def _token(provider: ControlPlaneCredentialProvider) -> str:
    value = provider.bearer_token()
    if not isinstance(value, str) or len(value) < 24 or any(ch.isspace() for ch in value):
        raise UpdateAuthenticationError("Control Plane session token is invalid")
    return value


def _allowed_url(value: str, *, scheme: str, allowed_hosts: frozenset[str]) -> str:
    parsed = urlsplit(value)
    normalized_hosts = frozenset(host.casefold() for host in allowed_hosts if host)
    if (
        parsed.scheme != scheme
        or not parsed.hostname
        or parsed.hostname.casefold() not in normalized_hosts
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(f"update endpoint must be an allowlisted {scheme.upper()} URL")
    return value


def _safe_destination(destination: Path, *, resume_from: int) -> int:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        parent_metadata = destination.parent.lstat()
    except OSError as error:
        raise FetchError("download destination parent is unavailable") from error
    parent_attributes = getattr(parent_metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or bool(parent_attributes & reparse)
    ):
        raise FetchError("download destination parent is unsafe")
    exists = os.path.lexists(destination)
    expected_identity: tuple[int, int] | None = None
    if exists:
        metadata = destination.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(attributes & reparse)
            or metadata.st_size != resume_from
            or metadata.st_nlink != 1
        ):
            raise FetchError("download destination is unsafe or has the wrong resume size")
        expected_identity = (metadata.st_dev, metadata.st_ino)
    elif resume_from:
        raise FetchError("cannot resume a missing download destination")
    flags = os.O_WRONLY
    if not exists:
        flags |= os.O_CREAT | os.O_EXCL
    flags |= os.O_APPEND if resume_from else os.O_TRUNC
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as error:
        raise FetchError("download destination could not be opened safely") from error
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_size != resume_from
        or opened.st_nlink != 1
        or (
            expected_identity is not None
            and (opened.st_dev, opened.st_ino) != expected_identity
        )
    ):
        os.close(descriptor)
        raise FetchError("download destination changed while it was opened")
    return descriptor


class HTTPArtifactFetcher(ArtifactFetcher):
    """Range-aware artifact downloader constrained to signed, allowlisted sources."""

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        client: httpx.Client | None = None,
        chunk_size: int = 1024 * 1024,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("at least one release artifact host must be allowlisted")
        if not 4096 <= chunk_size <= 4 * 1024 * 1024:
            raise ValueError("update download chunk size is invalid")
        self.allowed_hosts = frozenset(host.casefold() for host in allowed_hosts)
        self.chunk_size = chunk_size
        _validate_tls_context(ssl_context)
        if client is not None and ssl_context is not None:
            raise ValueError("an injected artifact client owns its TLS configuration")
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=10, read=60, write=30, pool=10),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            verify=ssl_context or True,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def fetch(
        self,
        source: ReleaseSource,
        artifact: ReleaseArtifact,
        destination: Path,
        *,
        resume_from: int,
        max_bytes: int,
    ) -> None:
        if max_bytes != artifact.size_bytes or not 0 <= resume_from <= max_bytes:
            raise FetchError("download bounds do not match the signed artifact")
        url = source.artifact_url(artifact)
        try:
            _allowed_url(url, scheme="https", allowed_hosts=self.allowed_hosts)
        except ValueError as error:
            raise FetchError("release source host is not allowlisted") from error
        headers = {"Accept": "application/octet-stream", "Accept-Encoding": "identity"}
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
        descriptor: int | None = None
        try:
            with self.client.stream("GET", url, headers=headers) as response:
                expected_status = 206 if resume_from else 200
                if response.status_code != expected_status:
                    raise FetchError("release source did not honor the bounded download request")
                if response.headers.get("content-encoding", "identity").casefold() != "identity":
                    raise FetchError("compressed transfer encoding is forbidden for signed artifacts")
                remaining = max_bytes - resume_from
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        parsed_length = int(length)
                    except ValueError as error:
                        raise FetchError("release source returned an invalid Content-Length") from error
                    if parsed_length != remaining:
                        raise FetchError("release source length does not match the signed artifact")
                content_range = response.headers.get("content-range")
                if resume_from:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range or "")
                    if (
                        match is None
                        or int(match.group(1)) != resume_from
                        or int(match.group(2)) != max_bytes - 1
                        or int(match.group(3)) != max_bytes
                    ):
                        raise FetchError("release source returned an invalid resume range")
                elif content_range is not None:
                    raise FetchError("unexpected Content-Range on a fresh download")
                descriptor = _safe_destination(destination, resume_from=resume_from)
                written = resume_from
                for chunk in response.iter_bytes(self.chunk_size):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise FetchError("release source exceeded the signed artifact size")
                    view = memoryview(chunk)
                    while view:
                        count = os.write(descriptor, view)
                        if count <= 0:
                            raise FetchError("short write while downloading release artifact")
                        view = view[count:]
                if written != max_bytes:
                    raise FetchError("release source ended before the signed artifact was complete")
                os.fsync(descriptor)
        except httpx.TimeoutException as error:
            raise FetchError("release source timed out") from error
        except httpx.TransportError as error:
            raise FetchError("release source transport failed") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)


class HTTPSReleaseFeedClient:
    """Fetches and verifies the latest signed manifest selected by Control Plane."""

    def __init__(
        self,
        endpoint: str,
        *,
        credentials: ControlPlaneCredentialProvider,
        verifier: SignatureVerifier,
        allowed_hosts: frozenset[str],
        client: httpx.Client | None = None,
        ssl_context: ssl.SSLContext | None = None,
        rollback_authorizer: SingleUseRollbackAuthorizer | None = None,
        current_identity_provider: Callable[[], Mapping[str, Any] | None]
        | None = None,
    ) -> None:
        self.endpoint = _allowed_url(
            endpoint, scheme="https", allowed_hosts=allowed_hosts
        )
        if urlsplit(self.endpoint).query:
            raise ValueError("release feed endpoint cannot contain a query")
        self.credentials = credentials
        self.verifier = verifier
        if (rollback_authorizer is None) != (current_identity_provider is None):
            raise ValueError(
                "rollback authorizer and current identity provider must be configured together"
            )
        self.rollback_authorizer = rollback_authorizer
        self.current_identity_provider = current_identity_provider
        self._rollback_tokens: dict[tuple[str, str, str], str] = {}
        self._rollback_lock = threading.RLock()
        _validate_tls_context(ssl_context)
        if client is not None and ssl_context is not None:
            raise ValueError("an injected release feed client owns its TLS configuration")
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            verify=ssl_context or True,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def latest(
        self,
        *,
        channel: ReleaseChannel,
        platform: str,
        architecture: str,
        current_version: str,
        update_state: str,
    ) -> ReleaseManifest | None:
        if update_state not in {
            "idle",
            "available",
            "downloading",
            "awaiting_user",
            "activating",
            "failed",
        }:
            raise ValueError("update feed state is invalid")
        with self._rollback_lock:
            self._rollback_tokens.clear()
        query_values = {
            "channel": channel.value,
            "platform": platform,
            "architecture": architecture,
            "current_version": current_version,
            "update_state": update_state,
        }
        current_identity: Mapping[str, Any] | None = None
        rollback_nonce: str | None = None
        if self.current_identity_provider is not None:
            try:
                current_identity = self.current_identity_provider()
            except Exception as error:
                raise UpdateProtocolError(
                    "current release identity could not be verified"
                ) from error
            if current_identity is not None:
                release_id = current_identity.get("release_id")
                build_digest = current_identity.get("build_digest")
                if (
                    not isinstance(release_id, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", release_id)
                    is None
                    or not isinstance(build_digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", build_digest) is None
                ):
                    raise UpdateProtocolError("current release identity is invalid")
                rollback_nonce = secrets.token_urlsafe(32)
                query_values.update(
                    {
                        "current_release_id": release_id,
                        "current_build_digest": build_digest,
                        "rollback_nonce": rollback_nonce,
                    }
                )
        query = urlencode(query_values)
        headers = {
            "Authorization": f"Bearer {_token(self.credentials)}",
            "Accept": "application/vnd.ecorex.release+json",
            "Accept-Encoding": "identity",
        }
        try:
            with self.client.stream("GET", f"{self.endpoint}?{query}", headers=headers) as response:
                if response.status_code == 204:
                    return None
                if response.status_code in {401, 403}:
                    raise UpdateAuthenticationError("release feed authentication was rejected")
                if response.status_code == 429 or response.status_code >= 500:
                    raise UpdateUnavailable("release feed is temporarily unavailable")
                if response.status_code != 200:
                    raise UpdateProtocolError("release feed rejected the request")
                media_type = response.headers.get("content-type", "").split(";", 1)[0]
                if media_type not in {
                    "application/json",
                    "application/vnd.ecorex.release+json",
                }:
                    raise UpdateProtocolError("release feed returned an unsupported content type")
                if response.headers.get("content-encoding", "identity").casefold() != "identity":
                    raise UpdateProtocolError("compressed release feed responses are forbidden")
                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    try:
                        parsed_length = int(declared_length)
                    except ValueError as error:
                        raise UpdateProtocolError(
                            "release feed returned an invalid Content-Length"
                        ) from error
                    if not 1 <= parsed_length <= MAX_MANIFEST_BYTES:
                        raise UpdateProtocolError(
                            "release feed Content-Length exceeds the manifest bound"
                        )
                payload = bytearray()
                rollback_token = response.headers.get(ROLLBACK_AUTHORIZATION_HEADER)
                if rollback_token is not None and len(rollback_token) > 8192:
                    raise UpdateProtocolError(
                        "rollback authorization header exceeds its size limit"
                    )
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_MANIFEST_BYTES:
                        raise UpdateProtocolError("release manifest exceeds its size limit")
        except httpx.TimeoutException as error:
            raise UpdateUnavailable("release feed timed out") from error
        except httpx.TransportError as error:
            raise UpdateUnavailable("release feed transport failed") from error
        try:
            manifest = ReleaseManifest.from_json(bytes(payload))
            verify_manifest_signature(manifest, self.verifier)
        except Exception as error:
            raise UpdateProtocolError("release feed returned an invalid signed manifest") from error
        if manifest.channel is not channel:
            raise UpdateProtocolError("release feed returned the wrong release channel")
        if rollback_token is not None:
            if (
                self.rollback_authorizer is None
                or current_identity is None
                or rollback_nonce is None
            ):
                raise UpdateProtocolError(
                    "release feed returned unsolicited rollback authorization"
                )
            try:
                self.rollback_authorizer.accept(
                    rollback_token,
                    current=current_identity,
                    target=manifest,
                    expected_nonce=rollback_nonce,
                )
            except RollbackAuthorizationError as error:
                raise UpdateProtocolError(
                    "release feed returned an invalid rollback authorization"
                ) from error
            with self._rollback_lock:
                self._rollback_tokens[
                    (manifest.release_id, manifest.version, manifest.build_digest)
                ] = rollback_token
        return manifest

    def rollback_authorization(self, manifest: ReleaseManifest) -> str | None:
        """Consume the request-bound grant paired with one verified feed response."""

        identity = (manifest.release_id, manifest.version, manifest.build_digest)
        with self._rollback_lock:
            return self._rollback_tokens.pop(identity, None)


@dataclass(frozen=True, slots=True)
class UpdateAvailableSignal:
    event_id: str
    release_id: str
    version: str
    build_digest: str
    channel: ReleaseChannel

    @classmethod
    def from_json(cls, payload: str | bytes) -> "UpdateAvailableSignal":
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError as error:
                raise UpdateProtocolError("update signal is not UTF-8") from error
        if len(payload.encode("utf-8")) > 64 * 1024:
            raise UpdateProtocolError("update signal exceeds its size limit")
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as error:
            raise UpdateProtocolError("update signal is invalid JSON") from error
        expected = {
            "schema_version",
            "event_id",
            "event_type",
            "release_id",
            "version",
            "build_digest",
            "channel",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise UpdateProtocolError("update signal fields do not match the v1 contract")
        if raw["schema_version"] != 1 or raw["event_type"] != "update.available":
            raise UpdateProtocolError("update signal type is unsupported")
        try:
            channel = ReleaseChannel(raw["channel"])
        except (TypeError, ValueError) as error:
            raise UpdateProtocolError("update signal channel is invalid") from error
        values = [raw.get(key) for key in ("event_id", "release_id", "version", "build_digest")]
        if any(not isinstance(value, str) or not value for value in values):
            raise UpdateProtocolError("update signal identity is invalid")
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", raw["event_id"])
            is None
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", raw["release_id"])
            is None
            or re.fullmatch(
                r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
                r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
                r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
                raw["version"],
            )
            is None
        ):
            raise UpdateProtocolError("update signal identity is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", raw["build_digest"]):
            raise UpdateProtocolError("update signal build digest is invalid")
        return cls(
            event_id=raw["event_id"],
            release_id=raw["release_id"],
            version=raw["version"],
            build_digest=raw["build_digest"],
            channel=channel,
        )


class WebSocketUpdateSignalSource:
    def __init__(
        self,
        endpoint: str,
        *,
        credentials: ControlPlaneCredentialProvider,
        allowed_hosts: frozenset[str],
        channel: ReleaseChannel,
        platform: str,
        architecture: str,
        current_version: str,
        current_release_id: str | None = None,
        current_build_digest: str | None = None,
        current_identity_provider: Callable[[], Mapping[str, Any] | None]
        | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.endpoint = _allowed_url(
            endpoint, scheme="wss", allowed_hosts=allowed_hosts
        )
        if urlsplit(self.endpoint).query:
            raise ValueError("update signal endpoint cannot contain a query")
        if not isinstance(channel, ReleaseChannel):
            raise ValueError("update signal channel is invalid")
        for label, value in (("platform", platform), ("architecture", architecture)):
            if not isinstance(value, str) or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value
            ) is None:
                raise ValueError(f"update signal {label} is invalid")
        if not isinstance(current_version, str) or re.fullmatch(
            r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
            r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
            current_version,
        ) is None:
            raise ValueError("update signal current_version is invalid")
        if (current_release_id is None) != (current_build_digest is None):
            raise ValueError("update signal current release identity is incomplete")
        if current_identity_provider is not None and current_release_id is not None:
            raise ValueError(
                "update signal current identity must be static or provided, not both"
            )
        if current_release_id is not None and (
            re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", current_release_id
            )
            is None
            or re.fullmatch(r"[0-9a-f]{64}", str(current_build_digest)) is None
        ):
            raise ValueError("update signal current release identity is invalid")
        _validate_tls_context(ssl_context)
        self.credentials = credentials
        self.channel = channel
        self.platform = platform
        self.architecture = architecture
        self.current_version = current_version
        self.current_release_id = current_release_id
        self.current_build_digest = current_build_digest
        self.current_identity_provider = current_identity_provider
        self.ssl_context = ssl_context
        self._closed = False
        self._sockets: set[Any] = set()

    @property
    def url(self) -> str:
        values = {
            "channel": self.channel.value,
            "platform": self.platform,
            "architecture": self.architecture,
            "current_version": self.current_version,
        }
        release_id = self.current_release_id
        build_digest = self.current_build_digest
        if self.current_identity_provider is not None:
            identity = self.current_identity_provider()
            if identity is not None:
                release_id = identity.get("release_id")
                build_digest = identity.get("build_digest")
        if release_id is not None:
            if (
                not isinstance(release_id, str)
                or re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", release_id
                )
                is None
                or not isinstance(build_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", build_digest) is None
            ):
                raise UpdateProtocolError(
                    "update signal current release identity is invalid"
                )
            values["current_release_id"] = release_id
            values["current_build_digest"] = build_digest
        query = urlencode(values)
        return f"{self.endpoint}?{query}"

    async def events(self) -> AsyncIterator[UpdateAvailableSignal]:
        if self._closed:
            raise UpdateUnavailable("update signal source is closed")
        try:
            from websockets.asyncio.client import connect
        except ImportError as error:  # pragma: no cover - packaging gate covers it
            raise UpdateUnavailable("WebSocket update transport is unavailable") from error
        headers = {"Authorization": f"Bearer {_token(self.credentials)}"}

        class NoRedirectConnect(connect):
            def process_redirect(self, error: Exception) -> Exception | str:
                return error

        try:
            async with NoRedirectConnect(
                self.url,
                additional_headers=headers,
                open_timeout=10,
                close_timeout=5,
                ping_interval=20,
                ping_timeout=20,
                max_size=64 * 1024,
                max_queue=16,
                compression=None,
                proxy=None,
                **({"ssl": self.ssl_context} if self.ssl_context is not None else {}),
            ) as socket:
                if self._closed:
                    await socket.close(code=1001, reason="update service stopped")
                    return
                self._sockets.add(socket)
                try:
                    async for message in socket:
                        yield UpdateAvailableSignal.from_json(message)
                finally:
                    self._sockets.discard(socket)
        except UpdateTransportError:
            raise
        except Exception as error:
            raise UpdateUnavailable("update signal connection failed") from error

    async def close(self) -> None:
        self._closed = True
        sockets = tuple(self._sockets)
        self._sockets.clear()
        if sockets:
            await asyncio.gather(
                *(
                    socket.close(code=1001, reason="update service stopped")
                    for socket in sockets
                ),
                return_exceptions=True,
            )
