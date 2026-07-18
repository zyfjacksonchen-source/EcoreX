"""Fail-closed online verification for an already-published signed release.

The first EcoreX source may be a read-only GitHub download proxy.  This module
therefore proves public bytes with GET requests; it never assumes that each
source implements the replica upload protocol.  The resulting receipt has the
exact shape consumed by :mod:`ecorex.release.public_index` and the Control
Plane promotion path.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Callable, Mapping
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from ecorex.update import (
    MAX_MANIFEST_BYTES,
    ReleaseManifest,
    SignatureVerifier,
    SourceKind,
    verify_artifact_signature,
    verify_manifest_signature,
)

from .identity import release_tag
from .publication_policy import (
    ALL_SOURCES_POLICY,
    publication_receipt_policy,
    required_publication_sources,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})
_RESERVED = ("release-manifest.json", "release-metadata.json", "sbom.cdx.json")
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "release_id",
        "version",
        "manifest_sha256",
        "publication_policy",
        "source_receipts",
    }
)
_GITHUB_REDIRECT_HOSTS = frozenset(
    {
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)
_MAX_RELEASE_FILES = 67
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_GITHUB_API_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024 * 1024


class OnlinePublicationVerificationError(ValueError):
    """A stable machine-readable online publication verification failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class OnlineVerificationLimits:
    attempts: int = 3
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 90.0
    total_timeout_seconds: float = 60.0 * 60.0
    maximum_total_bytes: int = 16 * 1024 * 1024 * 1024
    maximum_redirects: int = 3
    chunk_bytes: int = 1024 * 1024
    inter_request_delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.attempts, bool)
            or not 1 <= self.attempts <= 5
            or not 1 <= self.connect_timeout_seconds <= 60
            or not 1 <= self.read_timeout_seconds <= 300
            or not 1 <= self.total_timeout_seconds <= 6 * 60 * 60
            or isinstance(self.maximum_total_bytes, bool)
            or not 1 <= self.maximum_total_bytes <= _MAX_TOTAL_BYTES
            or isinstance(self.maximum_redirects, bool)
            or not 0 <= self.maximum_redirects <= 5
            or isinstance(self.chunk_bytes, bool)
            or not 64 * 1024 <= self.chunk_bytes <= 4 * 1024 * 1024
            or not 0 <= self.inter_request_delay_seconds <= 10
        ):
            raise OnlinePublicationVerificationError("online_limits_invalid")


@dataclass(slots=True)
class _Budget:
    maximum: int
    deadline: float
    consumed: int = 0

    def add(self, amount: int) -> None:
        self.check_time()
        if amount < 0 or self.consumed + amount > self.maximum:
            raise OnlinePublicationVerificationError("online_total_byte_limit_exceeded")
        self.consumed += amount

    def check_time(self) -> None:
        if time.monotonic() > self.deadline:
            raise OnlinePublicationVerificationError("online_total_timeout_exceeded")


@dataclass(frozen=True, slots=True)
class _ExpectedFile:
    name: str
    size_bytes: int
    sha256: str


class OnlinePublicationVerifier:
    """Verify all three origins and create one canonical publication receipt."""

    def __init__(
        self,
        *,
        verifier: SignatureVerifier,
        client: httpx.Client | None = None,
        limits: OnlineVerificationLimits | None = None,
        github_token: str | None = None,
        checkpoint_key: bytes,
        allowed_redirect_hosts: Mapping[str, frozenset[str]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.verifier = verifier
        self.limits = limits or OnlineVerificationLimits()
        self._client = client or httpx.Client(follow_redirects=False)
        self._owns_client = client is None
        self._sleep = sleep
        if github_token is not None and (
            not isinstance(github_token, str)
            or not 1 <= len(github_token) <= 4096
            or any(
                ord(character) < 0x21 or ord(character) > 0x7E
                for character in github_token
            )
        ):
            raise OnlinePublicationVerificationError("github_token_invalid")
        self._github_token = github_token
        if not isinstance(checkpoint_key, bytes) or len(checkpoint_key) != 32:
            raise OnlinePublicationVerificationError("checkpoint_key_invalid")
        self._checkpoint_key = checkpoint_key
        redirects: dict[str, frozenset[str]] = {}
        for source_id, hosts in (allowed_redirect_hosts or {}).items():
            if not isinstance(source_id, str) or not isinstance(hosts, frozenset):
                raise OnlinePublicationVerificationError("redirect_allowlist_invalid")
            normalized = frozenset(_host(item) for item in hosts)
            if not normalized:
                raise OnlinePublicationVerificationError("redirect_allowlist_invalid")
            redirects[source_id] = normalized
        self._redirect_hosts = redirects

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OnlinePublicationVerifier":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def verify(
        self,
        *,
        release_dir: Path,
        output: Path,
        checkpoint: Path,
        temporary_directory: Path | None = None,
    ) -> dict[str, object]:
        output = _new_output_path(output, "online_receipt_output_invalid")
        checkpoint = _mutable_output_path(checkpoint, "online_checkpoint_path_invalid")
        if os.path.lexists(output):
            raise OnlinePublicationVerificationError("online_receipt_exists")
        root = _safe_directory(release_dir, "online_release_directory_invalid")
        temp_root = _safe_directory(
            temporary_directory or output.parent,
            "online_temporary_directory_invalid",
        )
        manifest_path = root / "release-manifest.json"
        manifest_bytes = _read_regular_file(
            manifest_path,
            maximum_bytes=MAX_MANIFEST_BYTES,
            code="online_manifest_invalid",
        )
        manifest_raw = _strict_json(manifest_bytes, "online_manifest_invalid")
        if not isinstance(manifest_raw, dict):
            raise OnlinePublicationVerificationError("online_manifest_invalid")
        try:
            manifest = ReleaseManifest.from_dict(manifest_raw)
            verify_manifest_signature(manifest, self.verifier)
        except Exception:
            raise OnlinePublicationVerificationError(
                "online_manifest_signature_invalid"
            ) from None
        expected = _local_release_files(root, manifest, manifest_bytes, self.verifier)
        required_sources = required_publication_sources(manifest)
        policy = publication_receipt_policy(manifest)
        expected_total = sum(item.size_bytes for item in expected.values()) * len(
            required_sources
        )
        if expected_total > self.limits.maximum_total_bytes:
            raise OnlinePublicationVerificationError(
                "online_total_byte_limit_too_small"
            )
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        budget = _Budget(
            self.limits.maximum_total_bytes,
            time.monotonic() + self.limits.total_timeout_seconds,
        )
        if policy == ALL_SOURCES_POLICY:
            self._github_release(manifest, expected, budget)
        completed = _load_checkpoint(
            checkpoint,
            checkpoint_key=self._checkpoint_key,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            publication_policy=policy,
            expected=expected,
        )
        for source in required_sources:
            allowed = self._allowed_hosts(
                source.source_id, source.kind, source.base_url
            )
            for name in sorted(expected):
                key = (source.source_id, name)
                if key in completed:
                    continue
                item = expected[name]
                url = f"{source.base_url}/{quote(name, safe='')}"
                self._download(
                    url,
                    expected=item,
                    allowed_hosts=allowed,
                    budget=budget,
                    temporary_directory=temp_root,
                )
                completed[key] = {
                    "source_id": source.source_id,
                    "name": name,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                    "url": url,
                }
                _write_checkpoint(
                    checkpoint,
                    checkpoint_key=self._checkpoint_key,
                    manifest=manifest,
                    manifest_sha256=manifest_sha256,
                    publication_policy=policy,
                    completed=completed,
                )
                if self.limits.inter_request_delay_seconds:
                    self._sleep(self.limits.inter_request_delay_seconds)
        source_receipts: dict[str, list[dict[str, object]]] = {}
        for source in required_sources:
            source_receipts[source.source_id] = [
                {
                    "name": name,
                    "size_bytes": expected[name].size_bytes,
                    "sha256": expected[name].sha256,
                    "url": f"{source.base_url}/{quote(name, safe='')}",
                }
                for name in sorted(expected)
            ]
        receipt: dict[str, object] = {
            "schema_version": 2,
            "release_id": manifest.release_id,
            "version": manifest.version,
            "manifest_sha256": manifest_sha256,
            "publication_policy": policy,
            "source_receipts": source_receipts,
        }
        if set(receipt) != _RECEIPT_KEYS:
            raise OnlinePublicationVerificationError("online_receipt_contract_invalid")
        _remove_regular_checkpoint(checkpoint)
        _write_new_canonical_json(output, receipt, "online_receipt_write_failed")
        return receipt

    def _allowed_hosts(
        self, source_id: str, kind: SourceKind, base_url: str
    ) -> frozenset[str]:
        origin = _url(base_url, "online_source_url_invalid").hostname
        assert origin is not None
        hosts = {origin.casefold().rstrip(".")}
        if kind is SourceKind.GITHUB_RELEASE:
            hosts.update(_GITHUB_REDIRECT_HOSTS)
        hosts.update(self._redirect_hosts.get(source_id, frozenset()))
        return frozenset(hosts)

    def _github_release(
        self,
        manifest: ReleaseManifest,
        expected: Mapping[str, _ExpectedFile],
        budget: _Budget,
    ) -> int:
        source = next(
            (
                item
                for item in manifest.sources
                if item.kind is SourceKind.GITHUB_RELEASE
            ),
            None,
        )
        if source is None:
            raise OnlinePublicationVerificationError("github_source_missing")
        parsed = _url(source.base_url, "github_source_url_invalid")
        if _host(parsed.hostname or "") != "github.com" or parsed.port not in {
            None,
            443,
        }:
            raise OnlinePublicationVerificationError("github_source_url_invalid")
        parts = [unquote(item) for item in parsed.path.split("/") if item]
        if len(parts) != 5 or parts[2:4] != ["releases", "download"]:
            raise OnlinePublicationVerificationError("github_source_url_invalid")
        owner, repository, tag = parts[0], parts[1], parts[4]
        expected_tag = release_tag(
            manifest.version, manifest.channel, release_id=manifest.release_id
        )
        if tag != expected_tag or not owner or not repository:
            raise OnlinePublicationVerificationError("github_release_tag_mismatch")
        api_url = (
            f"https://api.github.com/repos/{quote(owner, safe='')}/"
            f"{quote(repository, safe='')}/releases/tags/{quote(tag, safe='')}"
        )
        payload = self._get_small_json(api_url, budget=budget)
        release_id = payload.get("id") if isinstance(payload, dict) else None
        assets = payload.get("assets") if isinstance(payload, dict) else None
        if (
            isinstance(release_id, bool)
            or not isinstance(release_id, int)
            or release_id < 1
            or payload.get("draft") is not False
            or payload.get("tag_name") != tag
            or not isinstance(assets, list)
            or len(assets) != len(expected)
        ):
            raise OnlinePublicationVerificationError("github_release_identity_invalid")
        seen: set[str] = set()
        for raw in assets:
            if not isinstance(raw, dict):
                raise OnlinePublicationVerificationError(
                    "github_release_assets_invalid"
                )
            name = raw.get("name")
            size = raw.get("size")
            url = raw.get("browser_download_url")
            if (
                not isinstance(name, str)
                or name not in expected
                or name in seen
                or isinstance(size, bool)
                or size != expected[name].size_bytes
                or url != f"{source.base_url}/{quote(name, safe='')}"
            ):
                raise OnlinePublicationVerificationError(
                    "github_release_assets_invalid"
                )
            digest = raw.get("digest")
            if digest is not None and digest != f"sha256:{expected[name].sha256}":
                raise OnlinePublicationVerificationError(
                    "github_release_asset_digest_drift"
                )
            seen.add(name)
        if seen != set(expected):
            raise OnlinePublicationVerificationError("github_release_assets_invalid")
        return release_id

    def _get_small_json(self, url: str, *, budget: _Budget) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "identity",
        }
        if self._github_token:
            headers["Authorization"] = f"Bearer {self._github_token}"
        payload = self._request_bytes(
            url,
            maximum_bytes=_MAX_GITHUB_API_BYTES,
            allowed_hosts=frozenset({"api.github.com"}),
            budget=budget,
            headers=headers,
        )
        value = _strict_json(payload, "github_release_response_invalid")
        if not isinstance(value, dict):
            raise OnlinePublicationVerificationError("github_release_response_invalid")
        return value

    def _download(
        self,
        url: str,
        *,
        expected: _ExpectedFile,
        allowed_hosts: frozenset[str],
        budget: _Budget,
        temporary_directory: Path,
    ) -> None:
        last_retryable = False
        for attempt in range(1, self.limits.attempts + 1):
            budget.check_time()
            try:
                self._download_once(
                    url,
                    expected=expected,
                    allowed_hosts=allowed_hosts,
                    budget=budget,
                    temporary_directory=temporary_directory,
                )
                return
            except (httpx.TimeoutException, httpx.NetworkError):
                last_retryable = True
            except _RetryableResponse:
                last_retryable = True
            if attempt < self.limits.attempts and last_retryable:
                self._sleep(min(0.25 * (2 ** (attempt - 1)), 1.0))
                continue
            break
        raise OnlinePublicationVerificationError(
            "online_download_retry_exhausted"
            if last_retryable
            else "online_download_failed"
        )

    def _download_once(
        self,
        url: str,
        *,
        expected: _ExpectedFile,
        allowed_hosts: frozenset[str],
        budget: _Budget,
        temporary_directory: Path,
    ) -> None:
        current = url
        visited: set[str] = set()
        for redirects in range(self.limits.maximum_redirects + 1):
            parsed = _url(current, "online_download_url_invalid")
            if _host(parsed.hostname or "") not in allowed_hosts:
                raise OnlinePublicationVerificationError(
                    "online_redirect_host_forbidden"
                )
            headers = {
                "Accept": "application/octet-stream",
                "Accept-Encoding": "identity",
                # Several domestic read-through mirrors keep the previous
                # response socket alive but stop delivering the next large
                # asset.  Publication verification values correctness over
                # connection reuse, so isolate every immutable asset transfer.
                "Connection": "close",
            }
            if (
                self._github_token
                and _host(parsed.hostname or "") in _GITHUB_REDIRECT_HOSTS
            ):
                headers["Authorization"] = f"Bearer {self._github_token}"
            timeout = httpx.Timeout(
                connect=self.limits.connect_timeout_seconds,
                read=self.limits.read_timeout_seconds,
                write=self.limits.connect_timeout_seconds,
                pool=self.limits.connect_timeout_seconds,
            )
            with self._client.stream(
                "GET", current, headers=headers, timeout=timeout
            ) as response:
                if response.status_code in _REDIRECTS:
                    if redirects >= self.limits.maximum_redirects:
                        raise OnlinePublicationVerificationError(
                            "online_redirect_limit_exceeded"
                        )
                    location = response.headers.get("location")
                    if not location:
                        raise OnlinePublicationVerificationError(
                            "online_redirect_invalid"
                        )
                    target = urljoin(current, location)
                    target_parsed = _url(target, "online_redirect_invalid")
                    if _host(target_parsed.hostname or "") not in allowed_hosts:
                        raise OnlinePublicationVerificationError(
                            "online_redirect_host_forbidden"
                        )
                    if target in visited:
                        raise OnlinePublicationVerificationError("online_redirect_loop")
                    visited.add(target)
                    current = target
                    continue
                if response.status_code in _RETRYABLE:
                    raise _RetryableResponse()
                if response.status_code != 200:
                    raise OnlinePublicationVerificationError(
                        "online_download_http_status_invalid"
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        length = int(content_length)
                    except ValueError:
                        raise OnlinePublicationVerificationError(
                            "online_download_content_length_invalid"
                        ) from None
                    if length != expected.size_bytes:
                        raise OnlinePublicationVerificationError(
                            "online_download_size_drift"
                        )
                descriptor, temporary = tempfile.mkstemp(
                    dir=temporary_directory, prefix=".ecorex-online-", suffix=".part"
                )
                try:
                    digest = hashlib.sha256()
                    written = 0
                    with os.fdopen(descriptor, "wb") as stream:
                        for chunk in response.iter_raw(self.limits.chunk_bytes):
                            if not chunk:
                                continue
                            budget.add(len(chunk))
                            written += len(chunk)
                            if written > expected.size_bytes:
                                raise OnlinePublicationVerificationError(
                                    "online_download_size_drift"
                                )
                            digest.update(chunk)
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    metadata = Path(temporary).lstat()
                    if (
                        not _regular(metadata)
                        or metadata.st_size != expected.size_bytes
                        or written != expected.size_bytes
                    ):
                        raise OnlinePublicationVerificationError(
                            "online_download_size_drift"
                        )
                    if digest.hexdigest() != expected.sha256:
                        raise OnlinePublicationVerificationError(
                            "online_download_digest_drift"
                        )
                finally:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        raise OnlinePublicationVerificationError(
                            "online_temporary_cleanup_failed"
                        ) from None
                return
        raise OnlinePublicationVerificationError("online_redirect_limit_exceeded")

    def _request_bytes(
        self,
        url: str,
        *,
        maximum_bytes: int,
        allowed_hosts: frozenset[str],
        budget: _Budget,
        headers: Mapping[str, str],
    ) -> bytes:
        last_retryable = False
        for attempt in range(1, self.limits.attempts + 1):
            try:
                parsed = _url(url, "online_api_url_invalid")
                if _host(parsed.hostname or "") not in allowed_hosts:
                    raise OnlinePublicationVerificationError(
                        "online_api_host_forbidden"
                    )
                with self._client.stream(
                    "GET",
                    url,
                    headers=dict(headers),
                    follow_redirects=False,
                    timeout=httpx.Timeout(
                        connect=self.limits.connect_timeout_seconds,
                        read=self.limits.read_timeout_seconds,
                        write=self.limits.connect_timeout_seconds,
                        pool=self.limits.connect_timeout_seconds,
                    ),
                ) as response:
                    if response.status_code in _RETRYABLE:
                        raise _RetryableResponse()
                    if response.status_code != 200:
                        raise OnlinePublicationVerificationError(
                            "online_api_http_status_invalid"
                        )
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared = int(content_length)
                        except ValueError:
                            raise OnlinePublicationVerificationError(
                                "online_api_response_too_large"
                            ) from None
                        if not 1 <= declared <= maximum_bytes:
                            raise OnlinePublicationVerificationError(
                                "online_api_response_too_large"
                            )
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes(64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        budget.add(len(chunk))
                        if total > maximum_bytes:
                            raise OnlinePublicationVerificationError(
                                "online_api_response_too_large"
                            )
                        chunks.append(chunk)
                    if total < 1:
                        raise OnlinePublicationVerificationError(
                            "online_api_response_too_large"
                        )
                    return b"".join(chunks)
            except (httpx.TimeoutException, httpx.NetworkError, _RetryableResponse):
                last_retryable = True
            if attempt < self.limits.attempts and last_retryable:
                self._sleep(min(0.25 * (2 ** (attempt - 1)), 1.0))
                continue
            break
        raise OnlinePublicationVerificationError("online_api_retry_exhausted")


class _RetryableResponse(Exception):
    pass


def _local_release_files(
    root: Path,
    manifest: ReleaseManifest,
    manifest_bytes: bytes,
    verifier: SignatureVerifier,
) -> dict[str, _ExpectedFile]:
    names = [artifact.file_name for artifact in manifest.artifacts]
    if (
        len(names) != len(set(names))
        or any(_SAFE_FILE.fullmatch(name) is None for name in names)
        or set(names).intersection(_RESERVED)
        or len(names) + len(_RESERVED) > _MAX_RELEASE_FILES
    ):
        raise OnlinePublicationVerificationError("online_release_file_set_invalid")
    result: dict[str, _ExpectedFile] = {}
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    result["release-manifest.json"] = _ExpectedFile(
        "release-manifest.json", len(manifest_bytes), manifest_digest
    )
    artifacts = {artifact.file_name: artifact for artifact in manifest.artifacts}
    for name in sorted(set(names).union(_RESERVED) - {"release-manifest.json"}):
        artifact = artifacts.get(name)
        maximum = artifact.size_bytes if artifact is not None else _MAX_METADATA_BYTES
        path = root / name
        size, digest = _hash_regular_file(path, maximum_bytes=maximum)
        if artifact is not None:
            if size != artifact.size_bytes or digest != artifact.sha256:
                raise OnlinePublicationVerificationError("online_local_artifact_drift")
            try:
                verify_artifact_signature(manifest, artifact, verifier)
            except Exception:
                raise OnlinePublicationVerificationError(
                    "online_local_artifact_signature_invalid"
                ) from None
        result[name] = _ExpectedFile(name, size, digest)
    return result


def _load_checkpoint(
    path: Path,
    *,
    checkpoint_key: bytes,
    manifest: ReleaseManifest,
    manifest_sha256: str,
    publication_policy: str,
    expected: Mapping[str, _ExpectedFile],
) -> dict[tuple[str, str], dict[str, object]]:
    if not os.path.lexists(path):
        return {}
    payload = _read_regular_file(
        path, maximum_bytes=8 * 1024 * 1024, code="online_checkpoint_invalid"
    )
    value = _strict_json(payload, "online_checkpoint_invalid")
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "document_type",
            "release_id",
            "manifest_sha256",
            "publication_policy",
            "completed",
            "checkpoint_mac",
        }
        or value.get("schema_version") != 2
        or value.get("document_type")
        != "ecorex.online-publication-verification-checkpoint"
        or value.get("release_id") != manifest.release_id
        or value.get("manifest_sha256") != manifest_sha256
        or value.get("publication_policy") != publication_policy
        or not isinstance(value.get("completed"), list)
    ):
        raise OnlinePublicationVerificationError("online_checkpoint_identity_conflict")
    unsigned = dict(value)
    mac = unsigned.pop("checkpoint_mac", None)
    if (
        not isinstance(mac, str)
        or _SHA256.fullmatch(mac) is None
        or not hmac.compare_digest(mac, _checkpoint_mac(checkpoint_key, unsigned))
    ):
        raise OnlinePublicationVerificationError(
            "online_checkpoint_authentication_failed"
        )
    sources = {
        source.source_id: source for source in required_publication_sources(manifest)
    }
    completed: dict[tuple[str, str], dict[str, object]] = {}
    for raw in value["completed"]:
        if not isinstance(raw, dict) or set(raw) != {
            "source_id",
            "name",
            "size_bytes",
            "sha256",
            "url",
        }:
            raise OnlinePublicationVerificationError("online_checkpoint_invalid")
        source_id = raw.get("source_id")
        name = raw.get("name")
        if not isinstance(source_id, str) or not isinstance(name, str):
            raise OnlinePublicationVerificationError("online_checkpoint_invalid")
        source = sources.get(source_id)
        item = expected.get(name)
        if (
            source is None
            or item is None
            or raw.get("size_bytes") != item.size_bytes
            or raw.get("sha256") != item.sha256
            or raw.get("url") != f"{source.base_url}/{quote(name, safe='')}"
            or (source_id, name) in completed
        ):
            raise OnlinePublicationVerificationError(
                "online_checkpoint_identity_conflict"
            )
        completed[(source_id, name)] = dict(raw)
    return completed


def _write_checkpoint(
    path: Path,
    *,
    checkpoint_key: bytes,
    manifest: ReleaseManifest,
    manifest_sha256: str,
    publication_policy: str,
    completed: Mapping[tuple[str, str], Mapping[str, object]],
) -> None:
    value = {
        "schema_version": 2,
        "document_type": "ecorex.online-publication-verification-checkpoint",
        "release_id": manifest.release_id,
        "manifest_sha256": manifest_sha256,
        "publication_policy": publication_policy,
        "completed": [dict(completed[key]) for key in sorted(completed)],
    }
    value["checkpoint_mac"] = _checkpoint_mac(checkpoint_key, value)
    payload = _canonical_json(value)
    _atomic_replace_regular(path, payload, "online_checkpoint_write_failed")


def _write_new_canonical_json(path: Path, value: object, code: str) -> None:
    payload = _canonical_json(value)
    descriptor = -1
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".new"
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # A same-directory hard link publishes the already-fsynced inode only
        # if the destination is still absent.  Unlike os.replace it cannot
        # overwrite a concurrently-created operator receipt.
        os.link(temporary, path)
        _fsync_directory(path.parent)
    except FileExistsError:
        raise OnlinePublicationVerificationError("online_receipt_exists") from None
    except OSError:
        raise OnlinePublicationVerificationError(code) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _atomic_replace_regular(path: Path, payload: bytes, code: str) -> None:
    if os.path.lexists(path) and not _regular(path.lstat()):
        raise OnlinePublicationVerificationError("online_checkpoint_path_invalid")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise OnlinePublicationVerificationError(code) from None


def _remove_regular_checkpoint(path: Path) -> None:
    if not os.path.lexists(path):
        return
    try:
        if not _regular(path.lstat()):
            raise OnlinePublicationVerificationError("online_checkpoint_cleanup_failed")
        path.unlink()
        _fsync_directory(path.parent)
    except OnlinePublicationVerificationError:
        raise
    except OSError:
        raise OnlinePublicationVerificationError(
            "online_checkpoint_cleanup_failed"
        ) from None


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise OnlinePublicationVerificationError("online_json_invalid") from None


def _checkpoint_mac(key: bytes, value: object) -> str:
    return hmac.new(
        key,
        b"ecorex-online-publication-checkpoint-v1\n" + _canonical_json(value),
        hashlib.sha256,
    ).hexdigest()


def _strict_json(payload: bytes, code: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(code)
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError(code)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise OnlinePublicationVerificationError(code) from None


def _read_regular_file(path: Path, *, maximum_bytes: int, code: str) -> bytes:
    size, _digest, payload = _read_or_hash(
        path, maximum_bytes=maximum_bytes, retain=True, code=code
    )
    if not 1 <= size <= maximum_bytes or payload is None:
        raise OnlinePublicationVerificationError(code)
    return payload


def _hash_regular_file(path: Path, *, maximum_bytes: int) -> tuple[int, str]:
    size, digest, _payload = _read_or_hash(
        path,
        maximum_bytes=maximum_bytes,
        retain=False,
        code="online_local_file_invalid",
    )
    return size, digest


def _read_or_hash(
    path: Path, *, maximum_bytes: int, retain: bool, code: str
) -> tuple[int, str, bytes | None]:
    try:
        _reject_link_components(path)
        before = path.lstat()
        if not _regular(before) or not 1 <= before.st_size <= maximum_bytes:
            raise OnlinePublicationVerificationError(code)
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before) or not _regular(opened):
                raise OnlinePublicationVerificationError(code)
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > maximum_bytes:
                    raise OnlinePublicationVerificationError(code)
                digest.update(chunk)
                if retain:
                    chunks.append(chunk)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        if (
            total != before.st_size
            or _descriptor_identity(after) != _descriptor_identity(opened)
            or _identity(current) != _identity(before)
            or not _regular(current)
        ):
            raise OnlinePublicationVerificationError(code)
        with path.open("rb") as current_stream:
            current_opened = os.fstat(current_stream.fileno())
            current_digest = hashlib.sha256()
            current_total = 0
            while chunk := current_stream.read(1024 * 1024):
                current_total += len(chunk)
                if current_total > maximum_bytes:
                    raise OnlinePublicationVerificationError(code)
                current_digest.update(chunk)
        if (
            not _regular(current_opened)
            or _descriptor_identity(current_opened)
            != _descriptor_identity(opened)
            or current_total != total
            or current_digest.digest() != digest.digest()
        ):
            raise OnlinePublicationVerificationError(code)
        return total, digest.hexdigest(), b"".join(chunks) if retain else None
    except OnlinePublicationVerificationError:
        raise
    except OSError:
        raise OnlinePublicationVerificationError(code) from None


def _safe_directory(value: Path, code: str) -> Path:
    try:
        path = Path(os.path.abspath(value.expanduser()))
        _reject_link_components(path)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or _linked(metadata):
            raise OnlinePublicationVerificationError(code)
        return path
    except OnlinePublicationVerificationError:
        raise
    except (OSError, TypeError):
        raise OnlinePublicationVerificationError(code) from None


def _new_output_path(value: Path, code: str) -> Path:
    return _output_path(value, code)


def _mutable_output_path(value: Path, code: str) -> Path:
    return _output_path(value, code)


def _output_path(value: Path, code: str) -> Path:
    try:
        raw = Path(value).expanduser()
        if not raw.name or raw.name in {".", ".."}:
            raise OnlinePublicationVerificationError(code)
        parent = _safe_directory(raw.absolute().parent, code)
        return parent / raw.name
    except OnlinePublicationVerificationError:
        raise
    except (OSError, TypeError):
        raise OnlinePublicationVerificationError(code) from None


def _url(value: str, code: str):
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise OnlinePublicationVerificationError(code)
        return parsed
    except (ValueError, TypeError):
        raise OnlinePublicationVerificationError(code) from None


def _host(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise OnlinePublicationVerificationError("online_host_invalid")
    host = value.casefold().rstrip(".")
    if not host or any(character.isspace() for character in host):
        raise OnlinePublicationVerificationError("online_host_invalid")
    return host


def _reject_link_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if os.path.lexists(current) and _linked(current.lstat()):
            raise OnlinePublicationVerificationError("online_path_link_forbidden")


def _linked(value: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & reparse
    )


def _regular(value: os.stat_result) -> bool:
    return stat.S_ISREG(value.st_mode) and not _linked(value)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    # Since Python 3.12, Windows path stat and descriptor fstat can expose
    # different st_ctime meanings for the same file: path stat reports the
    # creation time while fstat reports the NTFS change time.  st_birthtime is
    # the stable creation identity on that platform.  Change time still guards
    # the read below through descriptor-to-descriptor comparisons.
    creation_or_change_ns = (
        getattr(value, "st_birthtime_ns", value.st_ctime_ns)
        if os.name == "nt"
        else value.st_ctime_ns
    )
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        creation_or_change_ns,
    )


def _descriptor_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "OnlinePublicationVerificationError",
    "OnlinePublicationVerifier",
    "OnlineVerificationLimits",
]
