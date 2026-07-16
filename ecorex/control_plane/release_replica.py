"""Authenticated, no-clobber CDN release replica publication service.

The uploader is deliberately narrower than the public release API: it owns one
fixed ``source_id=cdn`` and accepts only the exact immutable v1 publication
set.  Bytes remain below a non-public staging directory until finalize has
verified the release keyring, manifest, every Artifact signature and all file
digests.  Final visibility is the last permission transition on a separately
reserved directory; no operation replaces an existing public path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat as stat_module
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ecorex.protocol import AuditRecordProjection
from ecorex.runtime.database import json_dumps
from ecorex.update import (
    Ed25519SignatureVerifier,
    MAX_ARTIFACT_BYTES,
    MAX_MANIFEST_BYTES,
    ReleaseChannel,
    ReleaseManifest,
    SourceKind,
    VerificationError,
    verify_artifact_file,
    verify_artifact_signature,
    verify_manifest_signature,
)

from .audit import CloudAuditRepository
from .models import ControlPrincipal


CDN_SOURCE_ID = "cdn"
PRODUCTION_RELEASE_REPLICA_ROOT = Path(
    "/srv/ecorex-agent-download/v1-artifacts"
)
PRODUCTION_RELEASE_REPLICA_PUBLIC_ROOT = (
    "https://dl.ecoremedia.net/ecorex-agent/releases"
)
RELEASE_REPLICA_TOKEN_CURRENT_ENV = "ECOREX_CP_RELEASE_REPLICA_TOKEN_CURRENT"
RELEASE_REPLICA_TOKEN_NEXT_ENV = "ECOREX_CP_RELEASE_REPLICA_TOKEN_NEXT"

_RELEASE_ID = re.compile(r"^release-(stable|canary)-[0-9a-f]{24}$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_RELEASE_NAMESPACE = re.compile(
    r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_TOKEN = re.compile(r"^[\x21-\x7e]{32,4096}$")
_TEMP_FILE = re.compile(
    r"^\.[A-Za-z0-9][A-Za-z0-9._-]{0,179}\.[0-9a-f]{32}\.part$"
)
_READY_NAME = ".ready.json"
_RESERVED_FILES = frozenset(
    {"release-manifest.json", "release-metadata.json", "sbom.cdx.json"}
)
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_FINALIZE_BYTES = 1024
_REPARSE_FLAG = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class ReleaseReplicaServiceError(RuntimeError):
    """One redacted service error safe to map to an HTTP response."""

    def __init__(self, code: str, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@runtime_checkable
class ReleaseReplicaTokenVerifier(Protocol):
    def verify(self, presented: str) -> bool: ...


class EnvironmentRotatingReleaseReplicaTokenVerifier:
    """Read current and next Bearer tokens at request time for instant rotation."""

    __slots__ = ("_environment",)

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def verify(self, presented: str) -> bool:
        if not isinstance(presented, str) or _TOKEN.fullmatch(presented) is None:
            return False
        current = self._environment.get(RELEASE_REPLICA_TOKEN_CURRENT_ENV, "")
        following = self._environment.get(RELEASE_REPLICA_TOKEN_NEXT_ENV, "")
        current_valid = _TOKEN.fullmatch(current) is not None
        following_valid = _TOKEN.fullmatch(following) is not None
        # Compare both slots unconditionally.  This avoids revealing which
        # credential is live and permits a zero-downtime current -> next flip.
        presented_digest = hashlib.sha256(presented.encode("ascii")).digest()
        current_digest = hashlib.sha256((current or " " * 32).encode("ascii")).digest()
        following_digest = hashlib.sha256(
            (following or " " * 32).encode("ascii")
        ).digest()
        current_match = hmac.compare_digest(presented_digest, current_digest)
        following_match = hmac.compare_digest(presented_digest, following_digest)
        return bool(
            (current_valid and current_match)
            or (following_valid and following_match)
        )

    def configured(self) -> bool:
        return _TOKEN.fullmatch(
            self._environment.get(RELEASE_REPLICA_TOKEN_CURRENT_ENV, "")
        ) is not None

    def __repr__(self) -> str:
        return "<EnvironmentRotatingReleaseReplicaTokenVerifier tokens=<redacted>>"


@runtime_checkable
class ReleaseReplicaAuditSink(Protocol):
    def record(
        self,
        *,
        event_type: str,
        source_event_id: str,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> None: ...


class CloudReleaseReplicaAuditSink:
    """Persist deterministic, redacted replica facts in the cloud audit ledger."""

    def __init__(self, repository: CloudAuditRepository) -> None:
        self.repository = repository
        self.principal = ControlPrincipal(
            subject="system:release-replica",
            client_id="ecorex-release-replica",
            account_id="system-release-replica",
            roles=frozenset(),
        )

    def record(
        self,
        *,
        event_type: str,
        source_event_id: str,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> None:
        safe_payload = dict(payload)
        encoded = json_dumps(safe_payload).encode("utf-8")
        audit_id = "audit_" + hashlib.sha256(
            (event_type + "\0" + source_event_id).encode("utf-8")
        ).hexdigest()
        record = AuditRecordProjection(
            audit_id=audit_id,
            source_event_id=source_event_id,
            category="artifact",
            event_type=event_type,
            account_id=self.principal.account_id,
            payload=safe_payload,
            payload_sha256=hashlib.sha256(encoded).hexdigest(),
            binary_included=False,
            delivery_status="published",
            attempts=1,
            created_at=created_at.astimezone(UTC),
            published_at=created_at.astimezone(UTC),
        )
        self.repository.ingest(
            self.principal, record, idempotency_key=record.audit_id
        )


class _NullAuditSink:
    def record(self, **_values: Any) -> None:
        return None


class CDNReleaseReplicaService:
    """Store one immutable CDN copy and finalize only fully authenticated sets."""

    def __init__(
        self,
        *,
        storage_root: Path,
        public_root: str,
        release_namespace: str,
        product_version: str,
        verifier: Ed25519SignatureVerifier,
        token_verifier: ReleaseReplicaTokenVerifier,
        audit_sink: ReleaseReplicaAuditSink | None = None,
        max_asset_bytes: int = MAX_ARTIFACT_BYTES,
    ) -> None:
        self.storage_root = Path(storage_root)
        self.public_root = public_root.rstrip("/")
        self.release_namespace = release_namespace
        self.product_version = product_version
        self.namespace_root = self.storage_root / release_namespace
        self.verifier = verifier
        self.token_verifier = token_verifier
        self.audit_sink = audit_sink or _NullAuditSink()
        self.max_asset_bytes = max_asset_bytes
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        parsed = urlsplit(self.public_root)
        if (
            not self.storage_root.is_absolute()
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or _SEMVER.fullmatch(product_version) is None
            or _RELEASE_NAMESPACE.fullmatch(release_namespace) is None
            or release_namespace != f"v{product_version}"
            or len(product_version.encode("utf-8")) > 128
            or len(release_namespace.encode("utf-8")) > 129
            or not isinstance(verifier, Ed25519SignatureVerifier)
            or not isinstance(token_verifier, ReleaseReplicaTokenVerifier)
            or not 1 <= max_asset_bytes <= MAX_ARTIFACT_BYTES
        ):
            raise ValueError("release replica service configuration is invalid")
        self._validate_root()
        _validate_directory(self.namespace_root, root=self.storage_root)
        for channel in ReleaseChannel:
            _validate_directory(
                self.namespace_root / channel.value, root=self.storage_root
            )

    def authorize(self, authorization: str) -> None:
        prefix = "Bearer "
        token = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
        try:
            accepted = self.token_verifier.verify(token)
        finally:
            token = ""
        if accepted is not True:
            raise ReleaseReplicaServiceError("replica_authentication_failed", 401)

    async def upload(
        self,
        request: Request,
        *,
        release_id: str,
        name: str,
        size_bytes: int,
        sha256: str,
    ) -> tuple[dict[str, Any], bool]:
        channel = _release_channel(release_id)
        if _SAFE_FILE.fullmatch(name) is None or _SHA256.fullmatch(sha256) is None:
            raise ReleaseReplicaServiceError("replica_asset_identity_invalid", 422)
        if not 1 <= size_bytes <= self.max_asset_bytes:
            raise ReleaseReplicaServiceError("replica_asset_size_invalid", 413)
        lock = await self._release_lock(release_id)
        async with lock:
            stage, published = self._release_directories(channel, release_id)
            if _lexists(published):
                if _lexists(published / _READY_NAME):
                    verified = self._validate_published_directory(
                        published,
                        channel=channel,
                        release_id=release_id,
                        expected_manifest_sha256=None,
                        allow_private=True,
                    )
                    self._finish_visibility_and_cleanup(published, stage, verified)
                    self._audit_release(verified)
                    actual = verified["assets"].get(name)
                    if actual != {"sha256": sha256, "size_bytes": size_bytes}:
                        raise ReleaseReplicaServiceError("replica_asset_conflict", 409)
                    self._audit_asset(
                        release_id, name, actual, _file_datetime(published / name)
                    )
                    return self._asset_receipt(
                        channel, release_id, name, actual, state="ready"
                    ), False
                self._recover_unpublished_directory(published, stage)
            self._ensure_stage_directory(stage)
            destination = stage / name
            if _lexists(destination):
                actual = _regular_file_identity(destination)
                if actual != {"sha256": sha256, "size_bytes": size_bytes}:
                    raise ReleaseReplicaServiceError("replica_asset_conflict", 409)
                self._audit_asset(
                    release_id, name, actual, _file_datetime(destination)
                )
                return self._asset_receipt(
                    channel, release_id, name, actual, state="ready"
                ), False

            temporary = stage / f".{name}.{uuid.uuid4().hex}.part"
            descriptor = -1
            created = False
            digest = hashlib.sha256()
            observed = 0
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(temporary, flags, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    async for chunk in request.stream():
                        if not isinstance(chunk, bytes):
                            raise ReleaseReplicaServiceError(
                                "replica_asset_stream_invalid", 400
                            )
                        observed += len(chunk)
                        if observed > size_bytes or observed > self.max_asset_bytes:
                            raise ReleaseReplicaServiceError(
                                "replica_asset_size_mismatch", 413
                            )
                        if chunk:
                            stream.write(chunk)
                            digest.update(chunk)
                    if observed != size_bytes or digest.hexdigest() != sha256:
                        raise ReleaseReplicaServiceError(
                            "replica_asset_digest_mismatch", 422
                        )
                    stream.flush()
                    os.fsync(stream.fileno())
                    if hasattr(os, "fchmod"):
                        os.fchmod(stream.fileno(), 0o600)
                    else:
                        os.chmod(temporary, 0o600)
                try:
                    os.link(temporary, destination, follow_symlinks=False)
                    created = True
                except FileExistsError:
                    actual = _regular_file_identity(destination)
                    if actual != {"sha256": sha256, "size_bytes": size_bytes}:
                        raise ReleaseReplicaServiceError(
                            "replica_asset_conflict", 409
                        )
                _fsync_directory(stage)
            except ReleaseReplicaServiceError:
                raise
            except (OSError, RuntimeError):
                raise ReleaseReplicaServiceError(
                    "replica_storage_unavailable", 503
                ) from None
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                try:
                    temporary.unlink(missing_ok=True)
                    _fsync_directory(stage)
                except OSError:
                    pass
            actual = _regular_file_identity(destination)
            self._audit_asset(
                release_id, name, actual, _file_datetime(destination)
            )
            return self._asset_receipt(
                channel, release_id, name, actual, state="ready"
            ), created

    async def finalize(
        self, *, release_id: str, manifest_sha256: str
    ) -> dict[str, Any]:
        channel = _release_channel(release_id)
        if _SHA256.fullmatch(manifest_sha256) is None:
            raise ReleaseReplicaServiceError("replica_manifest_identity_invalid", 422)
        lock = await self._release_lock(release_id)
        async with lock:
            return await asyncio.to_thread(
                self._finalize_locked,
                channel,
                release_id,
                manifest_sha256,
            )

    def health_check(self, *, write_probe: bool) -> None:
        self._validate_root()
        for channel in ReleaseChannel:
            directory = self.namespace_root / channel.value
            _validate_directory(directory, root=self.storage_root)
        if write_probe:
            probe = self.storage_root / f".health-{uuid.uuid4().hex}.tmp"
            descriptor = -1
            try:
                descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(descriptor, b"ok")
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
                probe.unlink()
                _fsync_directory(self.storage_root)
            except OSError:
                raise ReleaseReplicaServiceError(
                    "replica_storage_unavailable", 503
                ) from None
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                try:
                    probe.unlink(missing_ok=True)
                except OSError:
                    pass

    async def _release_lock(self, release_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(release_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[release_id] = lock
            return lock

    def _validate_root(self) -> None:
        _validate_directory(self.storage_root, root=self.storage_root)
        resolved = self.storage_root.resolve(strict=True)
        if resolved != self.storage_root:
            raise ValueError("release replica storage root must be canonical")

    def _release_directories(
        self, channel: ReleaseChannel, release_id: str
    ) -> tuple[Path, Path]:
        directory = self.namespace_root / channel.value
        _validate_directory(directory, root=self.storage_root)
        return directory / f".{release_id}.staging", directory / release_id

    def _ensure_stage_directory(self, stage: Path) -> None:
        if not _lexists(stage):
            try:
                stage.mkdir(mode=0o700)
                _fsync_directory(stage.parent)
            except FileExistsError:
                pass
            except OSError:
                raise ReleaseReplicaServiceError(
                    "replica_storage_unavailable", 503
                ) from None
        _validate_directory(stage, root=self.storage_root, private=True)

    def _finalize_locked(
        self,
        channel: ReleaseChannel,
        release_id: str,
        manifest_sha256: str,
    ) -> dict[str, Any]:
        stage, published = self._release_directories(channel, release_id)
        if _lexists(published):
            if _lexists(published / _READY_NAME):
                verified = self._validate_published_directory(
                    published,
                    channel=channel,
                    release_id=release_id,
                    expected_manifest_sha256=manifest_sha256,
                    allow_private=True,
                )
                self._finish_visibility_and_cleanup(published, stage, verified)
                self._audit_release(verified)
                return self._finalize_receipt(release_id, manifest_sha256)
            self._recover_unpublished_directory(published, stage)
        self._ensure_stage_directory(stage)
        self._remove_crash_temporaries(stage)
        verified = self._validate_release_directory(
            stage,
            channel=channel,
            release_id=release_id,
            expected_manifest_sha256=manifest_sha256,
        )
        try:
            published.mkdir(mode=0o700)
            _fsync_directory(published.parent)
        except FileExistsError:
            if _lexists(published / _READY_NAME):
                existing = self._validate_published_directory(
                    published,
                    channel=channel,
                    release_id=release_id,
                    expected_manifest_sha256=manifest_sha256,
                    allow_private=True,
                )
                self._finish_visibility_and_cleanup(published, stage, existing)
                self._audit_release(existing)
                return self._finalize_receipt(release_id, manifest_sha256)
            raise ReleaseReplicaServiceError("replica_finalize_conflict", 409) from None
        except OSError:
            raise ReleaseReplicaServiceError("replica_storage_unavailable", 503) from None

        try:
            for name in sorted(verified["assets"]):
                os.link(stage / name, published / name, follow_symlinks=False)
            ready = {
                "schema_version": 1,
                "source_id": CDN_SOURCE_ID,
                "release_namespace": self.release_namespace,
                "release_id": release_id,
                "version": verified["version"],
                "channel": channel.value,
                "manifest_sha256": manifest_sha256,
                "assets": verified["assets"],
                "finalized_at": datetime.now(UTC).isoformat(),
            }
            _write_new_file(
                published / _READY_NAME,
                (json.dumps(ready, sort_keys=True, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                ),
                mode=0o600,
            )
            _fsync_directory(published)
            completed = self._validate_published_directory(
                published,
                channel=channel,
                release_id=release_id,
                expected_manifest_sha256=manifest_sha256,
                allow_private=True,
            )
            self._finish_visibility_and_cleanup(published, stage, completed)
        except ReleaseReplicaServiceError:
            raise
        except (FileExistsError, OSError):
            raise ReleaseReplicaServiceError("replica_finalize_conflict", 409) from None
        self._audit_release(completed)
        return self._finalize_receipt(release_id, manifest_sha256)

    def _validate_release_directory(
        self,
        directory: Path,
        *,
        channel: ReleaseChannel,
        release_id: str,
        expected_manifest_sha256: str,
        allow_ready: bool = False,
    ) -> dict[str, Any]:
        _validate_directory(directory, root=self.storage_root)
        manifest_path = directory / "release-manifest.json"
        manifest_bytes = _read_regular_bytes(manifest_path, MAX_MANIFEST_BYTES)
        if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha256:
            raise ReleaseReplicaServiceError("replica_manifest_digest_mismatch", 422)
        try:
            manifest = ReleaseManifest.from_json(manifest_bytes)
            verify_manifest_signature(manifest, self.verifier)
        except (ValueError, VerificationError):
            raise ReleaseReplicaServiceError("replica_manifest_signature_invalid", 422) from None
        if (
            manifest.release_id != release_id
            or manifest.channel is not channel
            or manifest.version != self.product_version
        ):
            raise ReleaseReplicaServiceError("replica_manifest_identity_mismatch", 422)
        cdn_sources = tuple(
            source
            for source in manifest.sources
            if source.kind is SourceKind.ECOREX_CDN
        )
        expected_base = (
            f"{self.public_root}/{self.release_namespace}/{release_id}"
        )
        if (
            len(cdn_sources) != 1
            or cdn_sources[0].source_id != CDN_SOURCE_ID
            or cdn_sources[0].base_url != expected_base
        ):
            raise ReleaseReplicaServiceError("replica_manifest_source_mismatch", 422)
        artifact_names = {artifact.file_name for artifact in manifest.artifacts}
        expected_names = set(_RESERVED_FILES) | artifact_names
        observed = _scan_regular_directory(directory)
        permitted_names = expected_names | ({_READY_NAME} if allow_ready else set())
        if set(observed) != permitted_names:
            raise ReleaseReplicaServiceError("replica_asset_set_incomplete", 409)
        assets: dict[str, dict[str, Any]] = {}
        for artifact in manifest.artifacts:
            try:
                verify_artifact_signature(manifest, artifact, self.verifier)
                verify_artifact_file(
                    directory / artifact.file_name,
                    manifest,
                    artifact,
                    self.verifier,
                )
            except (OSError, VerificationError):
                raise ReleaseReplicaServiceError(
                    "replica_artifact_verification_failed", 422
                ) from None
            assets[artifact.file_name] = {
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            }
        assets["release-manifest.json"] = {
            "sha256": expected_manifest_sha256,
            "size_bytes": len(manifest_bytes),
        }
        metadata_bytes = _read_regular_bytes(
            directory / "release-metadata.json", _MAX_METADATA_BYTES
        )
        sbom = _regular_file_identity(directory / "sbom.cdx.json")
        try:
            metadata = json.loads(metadata_bytes)
            sbom_value = json.loads(
                _read_regular_bytes(directory / "sbom.cdx.json", self.max_asset_bytes)
            )
        except (UnicodeError, json.JSONDecodeError):
            raise ReleaseReplicaServiceError("replica_release_metadata_invalid", 422) from None
        artifact_kinds = self._validate_sbom(sbom_value, manifest)
        self._validate_metadata(
            metadata,
            manifest,
            expected_manifest_sha256,
            sbom,
            artifact_kinds,
        )
        assets["sbom.cdx.json"] = sbom
        assets["release-metadata.json"] = {
            "sha256": hashlib.sha256(metadata_bytes).hexdigest(),
            "size_bytes": len(metadata_bytes),
        }
        return {
            "release_id": release_id,
            "version": manifest.version,
            "channel": channel.value,
            "manifest_sha256": expected_manifest_sha256,
            "assets": dict(sorted(assets.items())),
        }

    def _validate_metadata(
        self,
        metadata: Any,
        manifest: ReleaseManifest,
        manifest_sha256: str,
        sbom: Mapping[str, Any],
        artifact_kinds: Mapping[str, str],
    ) -> None:
        if not isinstance(metadata, dict):
            raise ReleaseReplicaServiceError("replica_release_metadata_invalid", 422)
        required = {
            "schema_version",
            "release_id",
            "version",
            "channel",
            "created_at",
            "build_digest",
            "manifest",
            "manifest_sha256",
            "manifest_signature",
            "sbom",
            "sbom_sha256",
            "artifacts",
        }
        if not required.issubset(metadata) or set(metadata) - (
            required | {"python_dependency_lock_sha256"}
        ):
            raise ReleaseReplicaServiceError("replica_release_metadata_invalid", 422)
        if (
            metadata.get("schema_version") != 1
            or metadata.get("release_id") != manifest.release_id
            or metadata.get("version") != manifest.version
            or metadata.get("channel") != manifest.channel.value
            or metadata.get("created_at") != manifest.created_at
            or metadata.get("build_digest") != manifest.build_digest
            or metadata.get("manifest") != "release-manifest.json"
            or metadata.get("manifest_sha256") != manifest_sha256
            or metadata.get("manifest_signature") != manifest.signature.to_dict()
            or metadata.get("sbom") != "sbom.cdx.json"
            or metadata.get("sbom_sha256") != sbom.get("sha256")
        ):
            raise ReleaseReplicaServiceError("replica_release_metadata_invalid", 422)
        records = metadata.get("artifacts")
        if not isinstance(records, list) or len(records) != len(manifest.artifacts):
            raise ReleaseReplicaServiceError("replica_release_metadata_invalid", 422)
        by_name: dict[str, Mapping[str, Any]] = {}
        for record in records:
            if (
                not isinstance(record, dict)
                or set(record)
                != {
                    "artifact_id",
                    "kind",
                    "platform",
                    "architecture",
                    "file_name",
                    "size_bytes",
                    "sha256",
                    "signature",
                }
                or not isinstance(record.get("file_name"), str)
                or not isinstance(record.get("kind"), str)
                or re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", str(record.get("kind")))
                is None
            ):
                raise ReleaseReplicaServiceError("replica_release_metadata_invalid", 422)
            name = str(record["file_name"])
            if name in by_name:
                raise ReleaseReplicaServiceError("replica_release_metadata_invalid", 422)
            by_name[name] = record
        dependency_digest = metadata.get("python_dependency_lock_sha256")
        if dependency_digest is not None and (
            not isinstance(dependency_digest, str)
            or _SHA256.fullmatch(dependency_digest) is None
        ):
            raise ReleaseReplicaServiceError("replica_release_metadata_invalid", 422)
        for artifact in manifest.artifacts:
            record = by_name.get(artifact.file_name)
            if (
                record is None
                or record.get("artifact_id") != artifact.artifact_id
                or record.get("kind") != artifact_kinds.get(artifact.artifact_id)
                or record.get("platform") != artifact.platform
                or record.get("architecture") != artifact.architecture
                or record.get("size_bytes") != artifact.size_bytes
                or record.get("sha256") != artifact.sha256
                or record.get("signature") != artifact.signature.to_dict()
            ):
                raise ReleaseReplicaServiceError("replica_release_metadata_invalid", 422)

    def _validate_sbom(
        self, value: Any, manifest: ReleaseManifest
    ) -> Mapping[str, str]:
        if (
            not isinstance(value, dict)
            or value.get("bomFormat") != "CycloneDX"
            or value.get("specVersion") != "1.5"
            or value.get("version") != 1
            or not isinstance(value.get("metadata"), dict)
            or not isinstance(value.get("components"), list)
        ):
            raise ReleaseReplicaServiceError("replica_sbom_invalid", 422)
        metadata = value["metadata"]
        properties = metadata.get("properties")
        component = metadata.get("component")
        if (
            metadata.get("timestamp") != manifest.created_at
            or not isinstance(properties, list)
            or not isinstance(component, dict)
            or component.get("name") != "EcoreX"
            or component.get("version") != manifest.version
        ):
            raise ReleaseReplicaServiceError("replica_sbom_invalid", 422)
        property_map: dict[str, str] = {}
        for item in properties:
            if (
                not isinstance(item, dict)
                or set(item) != {"name", "value"}
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("value"), str)
                or item["name"] in property_map
            ):
                raise ReleaseReplicaServiceError("replica_sbom_invalid", 422)
            property_map[str(item["name"])] = str(item["value"])
        if (
            property_map.get("ecorex:build-digest") != manifest.build_digest
            or property_map.get("ecorex:channel") != manifest.channel.value
        ):
            raise ReleaseReplicaServiceError("replica_sbom_invalid", 422)
        artifact_components: dict[str, Mapping[str, Any]] = {}
        artifact_kinds: dict[str, str] = {}
        for item in value["components"]:
            if not isinstance(item, dict):
                raise ReleaseReplicaServiceError("replica_sbom_invalid", 422)
            raw_properties = item.get("properties")
            if not isinstance(raw_properties, list):
                continue
            projected = {
                str(prop.get("name")): str(prop.get("value"))
                for prop in raw_properties
                if isinstance(prop, dict)
                and isinstance(prop.get("name"), str)
                and isinstance(prop.get("value"), str)
            }
            artifact_id = projected.get("ecorex:artifact-id")
            if artifact_id is None:
                continue
            if artifact_id in artifact_components:
                raise ReleaseReplicaServiceError("replica_sbom_invalid", 422)
            artifact_kind = projected.get("ecorex:kind")
            if (
                artifact_kind is None
                or re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", artifact_kind) is None
            ):
                raise ReleaseReplicaServiceError("replica_sbom_invalid", 422)
            artifact_components[artifact_id] = {**item, "_properties": projected}
            artifact_kinds[artifact_id] = artifact_kind
        if set(artifact_components) != {
            artifact.artifact_id for artifact in manifest.artifacts
        }:
            raise ReleaseReplicaServiceError("replica_sbom_invalid", 422)
        for artifact in manifest.artifacts:
            item = artifact_components[artifact.artifact_id]
            projected = item["_properties"]
            hashes = item.get("hashes")
            if (
                item.get("name") != artifact.file_name
                or item.get("version") != manifest.version
                or not isinstance(hashes, list)
                or {"alg": "SHA-256", "content": artifact.sha256} not in hashes
                or projected.get("ecorex:platform") != artifact.platform
                or projected.get("ecorex:architecture") != artifact.architecture
                or projected.get("ecorex:size-bytes") != str(artifact.size_bytes)
            ):
                raise ReleaseReplicaServiceError("replica_sbom_invalid", 422)
        return artifact_kinds

    def _validate_published_directory(
        self,
        directory: Path,
        *,
        channel: ReleaseChannel,
        release_id: str,
        expected_manifest_sha256: str | None,
        allow_private: bool = False,
    ) -> dict[str, Any]:
        _validate_directory(directory, root=self.storage_root)
        ready_bytes = _read_regular_bytes(directory / _READY_NAME, _MAX_METADATA_BYTES)
        try:
            ready = json.loads(ready_bytes)
        except (UnicodeError, json.JSONDecodeError):
            raise ReleaseReplicaServiceError("replica_ready_marker_invalid", 409) from None
        if not isinstance(ready, dict) or set(ready) != {
            "schema_version",
            "source_id",
            "release_namespace",
            "release_id",
            "version",
            "channel",
            "manifest_sha256",
            "assets",
            "finalized_at",
        }:
            raise ReleaseReplicaServiceError("replica_ready_marker_invalid", 409)
        marker_digest = ready.get("manifest_sha256")
        if (
            ready.get("schema_version") != 1
            or ready.get("source_id") != CDN_SOURCE_ID
            or ready.get("release_namespace") != self.release_namespace
            or ready.get("release_id") != release_id
            or ready.get("version") != self.product_version
            or ready.get("channel") != channel.value
            or not isinstance(marker_digest, str)
            or _SHA256.fullmatch(marker_digest) is None
            or (
                expected_manifest_sha256 is not None
                and marker_digest != expected_manifest_sha256
            )
            or not isinstance(ready.get("finalized_at"), str)
        ):
            raise ReleaseReplicaServiceError("replica_ready_marker_invalid", 409)
        verified = self._validate_release_directory(
            directory,
            channel=channel,
            release_id=release_id,
            expected_manifest_sha256=marker_digest,
            allow_ready=True,
        )
        observed = _scan_regular_directory(directory)
        if set(observed) != set(verified["assets"]) | {_READY_NAME}:
            raise ReleaseReplicaServiceError("replica_ready_directory_tampered", 409)
        if ready.get("assets") != verified["assets"]:
            raise ReleaseReplicaServiceError("replica_ready_marker_invalid", 409)
        mode = stat_module.S_IMODE(directory.lstat().st_mode)
        if os.name != "nt" and (
            (allow_private and mode not in {0o700, 0o755})
            or (not allow_private and mode != 0o755)
        ):
            raise ReleaseReplicaServiceError("replica_ready_directory_private", 409)
        return {**verified, "finalized_at": str(ready["finalized_at"])}

    def _recover_unpublished_directory(self, published: Path, stage: Path) -> None:
        metadata = published.lstat()
        if (
            not stat_module.S_ISDIR(metadata.st_mode)
            or stat_module.S_ISLNK(metadata.st_mode)
            or (
                os.name != "nt"
                and stat_module.S_IMODE(metadata.st_mode) != 0o700
            )
        ):
            raise ReleaseReplicaServiceError("replica_ready_directory_tampered", 409)
        if not _lexists(stage):
            raise ReleaseReplicaServiceError("replica_finalize_conflict", 409)
        _validate_directory(stage, root=self.storage_root, private=True)
        for entry in published.iterdir():
            if entry.name == _READY_NAME or _SAFE_FILE.fullmatch(entry.name) is None:
                raise ReleaseReplicaServiceError("replica_ready_directory_tampered", 409)
            published_stat = _regular_file_stat(entry)
            source = stage / entry.name
            source_stat = _regular_file_stat(source)
            if (published_stat.st_dev, published_stat.st_ino) != (
                source_stat.st_dev,
                source_stat.st_ino,
            ):
                raise ReleaseReplicaServiceError("replica_ready_directory_tampered", 409)
            entry.unlink()
        published.rmdir()
        _fsync_directory(published.parent)

    def _finish_visibility_and_cleanup(
        self, published: Path, stage: Path, verified: Mapping[str, Any]
    ) -> None:
        for name in sorted(verified["assets"]):
            path = published / name
            _chmod_regular_file(path, 0o644)
        _chmod_regular_file(published / _READY_NAME, 0o644)
        _fsync_directory(published)
        os.chmod(published, 0o755)
        _fsync_directory(published.parent)
        if _lexists(stage):
            self._remove_crash_temporaries(stage)
            for entry in stage.iterdir():
                if _SAFE_FILE.fullmatch(entry.name) is None:
                    raise ReleaseReplicaServiceError(
                        "replica_staging_directory_tampered", 409
                    )
                _regular_file_stat(entry)
                entry.unlink()
            stage.rmdir()
            _fsync_directory(stage.parent)

    def _remove_crash_temporaries(self, stage: Path) -> None:
        removed = False
        for entry in stage.iterdir():
            if not _TEMP_FILE.fullmatch(entry.name):
                continue
            _regular_file_stat(entry)
            entry.unlink()
            removed = True
        if removed:
            _fsync_directory(stage)

    def _asset_receipt(
        self,
        channel: ReleaseChannel,
        release_id: str,
        name: str,
        identity: Mapping[str, Any],
        *,
        state: str,
    ) -> dict[str, Any]:
        return {
            "release_id": release_id,
            "source_id": CDN_SOURCE_ID,
            "name": name,
            "size_bytes": int(identity["size_bytes"]),
            "sha256": str(identity["sha256"]),
            "url": (
                f"{self.public_root}/{quote(self.release_namespace, safe='')}/"
                f"{quote(release_id, safe='')}/"
                f"{quote(name, safe='')}"
            ),
            "state": state,
        }

    @staticmethod
    def _finalize_receipt(release_id: str, manifest_sha256: str) -> dict[str, Any]:
        return {
            "release_id": release_id,
            "source_id": CDN_SOURCE_ID,
            "state": "ready",
            "manifest_sha256": manifest_sha256,
        }

    def _audit_asset(
        self,
        release_id: str,
        name: str,
        identity: Mapping[str, Any],
        created_at: datetime,
    ) -> None:
        namespace_identity = hashlib.sha256(
            self.release_namespace.encode("utf-8")
        ).hexdigest()[:16]
        self.audit_sink.record(
            event_type="release.replica.asset.ready",
            source_event_id=(
                f"replica:cdn:asset:{namespace_identity}:{release_id}:"
                f"{name}:{identity['sha256']}"
            ),
            payload={
                "source_id": CDN_SOURCE_ID,
                "release_namespace": self.release_namespace,
                "release_id": release_id,
                "name": name,
                "size_bytes": int(identity["size_bytes"]),
                "sha256": str(identity["sha256"]),
                "state": "ready",
            },
            created_at=created_at,
        )

    def _audit_release(self, verified: Mapping[str, Any]) -> None:
        finalized_at = datetime.fromisoformat(str(verified["finalized_at"]))
        namespace_identity = hashlib.sha256(
            self.release_namespace.encode("utf-8")
        ).hexdigest()[:16]
        self.audit_sink.record(
            event_type="release.replica.finalized",
            source_event_id=(
                f"replica:cdn:finalize:{namespace_identity}:"
                f"{verified['release_id']}:"
                f"{verified['manifest_sha256']}"
            ),
            payload={
                "source_id": CDN_SOURCE_ID,
                "release_namespace": self.release_namespace,
                "release_id": str(verified["release_id"]),
                "version": str(verified["version"]),
                "channel": str(verified["channel"]),
                "manifest_sha256": str(verified["manifest_sha256"]),
                "asset_count": len(verified["assets"]),
                "state": "ready",
            },
            created_at=finalized_at,
        )


def create_cdn_release_replica_router(
    service: CDNReleaseReplicaService,
) -> APIRouter:
    router = APIRouter()

    @router.put(
        "/api/v1/releases/{release_id}/replicas/cdn/assets/{name}",
        response_model=None,
    )
    async def upload_asset(release_id: str, name: str, request: Request) -> JSONResponse:
        try:
            service.authorize(_authorization_header(request))
            _reject_transfer_encoding(request)
            length = _decimal_header(request, "content-length")
            declared = _decimal_header(request, "x-ecorex-size")
            digest = _one_header(request, "x-ecorex-sha256")
            if length != declared:
                raise ReleaseReplicaServiceError("replica_asset_size_mismatch", 422)
            expected_idempotency = f"cdn:{release_id}:{name}:{digest}"
            if not hmac.compare_digest(
                _one_header(request, "idempotency-key"), expected_idempotency
            ):
                raise ReleaseReplicaServiceError("replica_idempotency_invalid", 422)
            receipt, created = await service.upload(
                request,
                release_id=release_id,
                name=name,
                size_bytes=declared,
                sha256=digest,
            )
            return JSONResponse(status_code=201 if created else 200, content=receipt)
        except ReleaseReplicaServiceError as error:
            raise _http_error(error) from error

    @router.post(
        "/api/v1/releases/{release_id}/replicas/cdn/finalize",
        response_model=None,
    )
    async def finalize_release(release_id: str, request: Request) -> JSONResponse:
        try:
            service.authorize(_authorization_header(request))
            _reject_transfer_encoding(request)
            length = _decimal_header(request, "content-length")
            if not 1 <= length <= _MAX_FINALIZE_BYTES:
                raise ReleaseReplicaServiceError("replica_finalize_body_invalid", 413)
            body = await request.body()
            if len(body) != length:
                raise ReleaseReplicaServiceError("replica_finalize_body_invalid", 422)
            try:
                value = json.loads(body)
            except (UnicodeError, json.JSONDecodeError):
                raise ReleaseReplicaServiceError(
                    "replica_finalize_body_invalid", 422
                ) from None
            if not isinstance(value, dict) or set(value) != {"manifest_sha256"}:
                raise ReleaseReplicaServiceError("replica_finalize_body_invalid", 422)
            digest = value.get("manifest_sha256")
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise ReleaseReplicaServiceError("replica_finalize_body_invalid", 422)
            expected_idempotency = f"finalize:cdn:{release_id}:{digest}"
            if not hmac.compare_digest(
                _one_header(request, "idempotency-key"), expected_idempotency
            ):
                raise ReleaseReplicaServiceError("replica_idempotency_invalid", 422)
            receipt = await service.finalize(
                release_id=release_id, manifest_sha256=digest
            )
            return JSONResponse(status_code=200, content=receipt)
        except ReleaseReplicaServiceError as error:
            raise _http_error(error) from error

    return router


def _http_error(error: ReleaseReplicaServiceError) -> HTTPException:
    headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": "release replica request rejected"},
        headers=headers,
    )


def _one_header(request: Request, name: str) -> str:
    encoded = name.casefold().encode("ascii")
    values = [
        value
        for key, value in request.scope.get("headers", [])
        if key.lower() == encoded
    ]
    if len(values) != 1:
        raise ReleaseReplicaServiceError("replica_header_invalid", 422)
    try:
        decoded = values[0].decode("ascii")
    except UnicodeDecodeError:
        raise ReleaseReplicaServiceError("replica_header_invalid", 422) from None
    if not decoded or len(decoded) > 8192:
        raise ReleaseReplicaServiceError("replica_header_invalid", 422)
    return decoded


def _authorization_header(request: Request) -> str:
    try:
        return _one_header(request, "authorization")
    except ReleaseReplicaServiceError:
        return ""


def _decimal_header(request: Request, name: str) -> int:
    value = _one_header(request, name)
    if len(value) > 20 or not value.isdigit():
        raise ReleaseReplicaServiceError("replica_header_invalid", 422)
    return int(value)


def _reject_transfer_encoding(request: Request) -> None:
    if any(
        key.lower() == b"transfer-encoding"
        for key, _value in request.scope.get("headers", [])
    ):
        raise ReleaseReplicaServiceError("replica_transfer_encoding_forbidden", 422)
    encodings = [
        value
        for key, value in request.scope.get("headers", [])
        if key.lower() == b"content-encoding"
    ]
    if len(encodings) > 1 or (encodings and encodings[0].lower() != b"identity"):
        raise ReleaseReplicaServiceError("replica_content_encoding_forbidden", 422)


def _release_channel(release_id: str) -> ReleaseChannel:
    match = _RELEASE_ID.fullmatch(release_id)
    if match is None:
        raise ReleaseReplicaServiceError("replica_release_identity_invalid", 422)
    return ReleaseChannel(match.group(1))


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _validate_directory(path: Path, *, root: Path, private: bool = False) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise ReleaseReplicaServiceError("replica_storage_unavailable", 503) from None
    try:
        resolved.relative_to(root)
    except ValueError:
        if resolved != root:
            raise ReleaseReplicaServiceError("replica_storage_path_invalid", 503) from None
    if (
        not stat_module.S_ISDIR(metadata.st_mode)
        or stat_module.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_FLAG)
        or resolved != path
        or (
            os.name != "nt"
            and (
                (private and stat_module.S_IMODE(metadata.st_mode) != 0o700)
                or (not private and stat_module.S_IMODE(metadata.st_mode) & 0o022)
            )
        )
    ):
        raise ReleaseReplicaServiceError("replica_storage_path_invalid", 503)


def _regular_file_stat(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        raise ReleaseReplicaServiceError("replica_asset_missing", 409) from None
    if (
        not stat_module.S_ISREG(metadata.st_mode)
        or stat_module.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_FLAG)
        or metadata.st_nlink < 1
    ):
        raise ReleaseReplicaServiceError("replica_asset_path_invalid", 409)
    return metadata


def _regular_file_identity(path: Path) -> dict[str, Any]:
    before = _regular_file_stat(path)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise OSError
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except OSError:
        raise ReleaseReplicaServiceError("replica_asset_unstable", 409) from None
    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    ):
        raise ReleaseReplicaServiceError("replica_asset_unstable", 409)
    return {"sha256": digest.hexdigest(), "size_bytes": before.st_size}


def _read_regular_bytes(path: Path, maximum: int) -> bytes:
    metadata = _regular_file_stat(path)
    if not 1 <= metadata.st_size <= maximum:
        raise ReleaseReplicaServiceError("replica_asset_size_invalid", 422)
    try:
        payload = path.read_bytes()
    except OSError:
        raise ReleaseReplicaServiceError("replica_asset_unstable", 409) from None
    after = _regular_file_stat(path)
    if (
        len(payload) != metadata.st_size
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
    ):
        raise ReleaseReplicaServiceError("replica_asset_unstable", 409)
    return payload


def _scan_regular_directory(path: Path) -> dict[str, os.stat_result]:
    observed: dict[str, os.stat_result] = {}
    try:
        entries = tuple(path.iterdir())
    except OSError:
        raise ReleaseReplicaServiceError("replica_storage_unavailable", 503) from None
    for entry in entries:
        if entry.name != _READY_NAME and _SAFE_FILE.fullmatch(entry.name) is None:
            raise ReleaseReplicaServiceError("replica_asset_path_invalid", 409)
        observed[entry.name] = _regular_file_stat(entry)
    return observed


def _write_new_file(path: Path, payload: bytes, *, mode: int) -> None:
    temporary = path.parent / f".{path.name.lstrip('.')}.{uuid.uuid4().hex}.part"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), mode)
            else:
                os.chmod(temporary, mode)
        os.link(temporary, path, follow_symlinks=False)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _chmod_regular_file(path: Path, mode: int) -> None:
    _regular_file_stat(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode):
            raise ReleaseReplicaServiceError("replica_asset_path_invalid", 409)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            os.chmod(path, mode)
        try:
            os.fsync(descriptor)
        except OSError:
            if os.name != "nt":
                raise
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except PermissionError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_datetime(path: Path) -> datetime:
    return datetime.fromtimestamp(_regular_file_stat(path).st_mtime, tz=UTC)


__all__ = [
    "CDNReleaseReplicaService",
    "CDN_SOURCE_ID",
    "CloudReleaseReplicaAuditSink",
    "EnvironmentRotatingReleaseReplicaTokenVerifier",
    "PRODUCTION_RELEASE_REPLICA_PUBLIC_ROOT",
    "PRODUCTION_RELEASE_REPLICA_ROOT",
    "RELEASE_REPLICA_TOKEN_CURRENT_ENV",
    "RELEASE_REPLICA_TOKEN_NEXT_ENV",
    "ReleaseReplicaServiceError",
    "create_cdn_release_replica_router",
]
