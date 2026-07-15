"""Public-object activation and trusted readback for the Bootstrap pointer."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from ecorex.release.public_index import (
    MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES,
    PUBLIC_BOOTSTRAP_INDEX_FILE_NAME,
)
from ecorex.update.locking import ProductFileLock

from .models import ControlPrincipal
from .repository import ControlPlaneRepository


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class BootstrapIndexPublicationError(RuntimeError):
    pass


@runtime_checkable
class PublicIndexObjectStore(Protocol):
    def activate(
        self,
        payload: bytes,
        *,
        expected_previous_sha256: str | None,
        candidate_sha256: str,
    ) -> str: ...


@runtime_checkable
class PublicIndexReader(Protocol):
    def read_exact(self, public_url: str) -> bytes: ...


class FilesystemPublicIndexObjectStore:
    """Atomic CAS adapter for a pointer file served by the public web tier."""

    def __init__(self, path: str | Path) -> None:
        raw = Path(path).expanduser()
        raw.parent.mkdir(parents=True, exist_ok=True)
        parent = raw.parent.resolve(strict=True)
        if not parent.is_dir() or raw.name != PUBLIC_BOOTSTRAP_INDEX_FILE_NAME:
            raise ValueError("public Bootstrap object path is invalid")
        self.path = parent / raw.name
        lock_id = hashlib.sha256(str(self.path).encode("utf-8")).hexdigest()[:24]
        self.lock_path = parent / f".{raw.name}.{lock_id}.lock"

    def activate(
        self,
        payload: bytes,
        *,
        expected_previous_sha256: str | None,
        candidate_sha256: str,
    ) -> str:
        if (
            not isinstance(payload, bytes)
            or not 1 <= len(payload) <= MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES
            or _SHA256.fullmatch(candidate_sha256) is None
            or hashlib.sha256(payload).hexdigest() != candidate_sha256
            or (
                expected_previous_sha256 is not None
                and _SHA256.fullmatch(expected_previous_sha256) is None
            )
        ):
            raise BootstrapIndexPublicationError(
                "public Bootstrap object identity is invalid"
            )
        with ProductFileLock(self.lock_path, timeout=0):
            current = self._current_bytes()
            current_sha256 = hashlib.sha256(current).hexdigest() if current else None
            if current_sha256 == candidate_sha256:
                return "pobj_" + candidate_sha256[:32]
            if expected_previous_sha256 is None:
                if current is not None and not _is_unpublished_pointer(current):
                    raise BootstrapIndexPublicationError(
                        "public Bootstrap object compare-and-swap failed"
                    )
            elif current_sha256 != expected_previous_sha256:
                raise BootstrapIndexPublicationError(
                    "public Bootstrap object compare-and-swap failed"
                )
            temporary = self.path.with_name(
                f".{self.path.name}.activate-{os.getpid()}-{secrets.token_hex(8)}"
            )
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                _durable_replace(temporary, self.path)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            return "pobj_" + candidate_sha256[:32]

    def _current_bytes(self) -> bytes | None:
        if not os.path.lexists(self.path):
            return None
        metadata = self.path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(metadata.st_mode)
            or not 1 <= metadata.st_size <= MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES
        ):
            raise BootstrapIndexPublicationError(
                "public Bootstrap object is unsafe"
            )
        before = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        with self.path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = self.path.lstat()
        if any(
            (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns) != before
            for item in (opened, after, current)
        ) or len(payload) != metadata.st_size:
            raise BootstrapIndexPublicationError(
                "public Bootstrap object changed while reading"
            )
        return payload


class HTTPSPublicIndexReader:
    """Allowlisted no-cache exact-byte readback from the real public URL."""

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        client: httpx.Client | None = None,
    ) -> None:
        normalized = frozenset(host.casefold().rstrip(".") for host in allowed_hosts)
        if not normalized or any(
            not host
            or any(_HOST.fullmatch(label) is None for label in host.split("."))
            for host in normalized
        ):
            raise ValueError("public Bootstrap readback hosts are invalid")
        self.allowed_hosts = normalized
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=15, read=60, write=15, pool=15),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def read_exact(self, public_url: str) -> bytes:
        parsed = urlsplit(public_url)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold().rstrip(".")
            not in self.allowed_hosts
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or not parsed.path.endswith("/" + PUBLIC_BOOTSTRAP_INDEX_FILE_NAME)
            or parsed.query
            or parsed.fragment
        ):
            raise BootstrapIndexPublicationError(
                "public Bootstrap readback URL is refused"
            )
        request = self.client.build_request(
            "GET",
            public_url,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )
        response = self.client.send(request, stream=True, follow_redirects=False)
        try:
            if (
                response.is_redirect
                or response.history
                or response.status_code != 200
                or response.headers.get("content-encoding", "identity").casefold()
                != "identity"
                or response.headers.get("content-type", "").split(";", 1)[0]
                != "application/json"
            ):
                raise BootstrapIndexPublicationError(
                    "public Bootstrap readback was rejected"
                )
            directives = {
                part.strip().casefold()
                for part in response.headers.get("cache-control", "").split(",")
            }
            if "no-store" not in directives:
                raise BootstrapIndexPublicationError(
                    "public Bootstrap readback is cacheable"
                )
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES:
                    raise BootstrapIndexPublicationError(
                        "public Bootstrap readback exceeded its bound"
                    )
            payload = bytes(body)
            if not payload:
                raise BootstrapIndexPublicationError(
                    "public Bootstrap readback exceeded its bound"
                )
            return payload
        finally:
            response.close()


class BootstrapIndexPublicationService:
    def __init__(
        self,
        repository: ControlPlaneRepository,
        *,
        public_url: str,
        object_store: PublicIndexObjectStore,
        public_reader: PublicIndexReader,
    ) -> None:
        if not isinstance(object_store, PublicIndexObjectStore):
            raise TypeError("public Bootstrap object store is invalid")
        if not isinstance(public_reader, PublicIndexReader):
            raise TypeError("public Bootstrap reader is invalid")
        self.repository = repository
        self.public_url = public_url
        self.object_store = object_store
        self.public_reader = public_reader

    def close(self) -> None:
        close = getattr(self.public_reader, "close", None)
        if callable(close):
            close()

    def stage(
        self,
        payload: bytes,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> dict[str, object]:
        staged = self.repository.stage_bootstrap_index(
            payload,
            public_url=self.public_url,
            actor=actor,
            client_request_id=client_request_id,
        )
        authority = _mapping(staged.get("authority"), "staged authority")
        freshness = _mapping(staged.get("freshness"), "staged freshness")
        return {
            "schema_version": 1,
            "release_id": staged["release_id"],
            "version": _mapping(authority.get("target"), "authority target")[
                "version"
            ],
            "state": "staged",
            "index_sha256": staged["index_sha256"],
            "index_size_bytes": staged["index_size_bytes"],
            "public_url": staged["public_url"],
            "revision_id": staged["revision_id"],
            "authority_sequence": authority["sequence"],
            "authority_revision_id": authority["revision"],
            "authority_target": authority["target"],
            "freshness_issued_at": freshness["issued_at"],
            "freshness_expires_at": freshness["expires_at"],
            "active_activation_record_id": staged[
                "active_activation_record_id"
            ],
            "active_sequence": staged["active_sequence"],
            "active_authority_revision_id": staged["active_revision_id"],
            "active_index_sha256": staged["active_index_sha256"],
            "active_target": staged["active_target"],
        }

    def activate(
        self,
        *,
        release_id: str,
        request: Mapping[str, object],
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> dict[str, object]:
        expected = _activation_request(request)
        prepared = self.repository.prepare_bootstrap_index_activation(
            release_id=release_id,
            stage_record_id=expected["revision_id"],
            index_sha256=expected["index_sha256"],
            expected_previous_activation_record_id=expected.get(
                "expected_previous_activation_record_id"
            ),
            expected_previous_sequence=expected.get("expected_previous_sequence"),
            expected_previous_revision=expected.get(
                "expected_previous_authority_revision_id"
            ),
            expected_previous_index_sha256=expected.get(
                "expected_previous_index_sha256"
            ),
            expected_previous_target=expected.get("expected_previous_target"),
            actor=actor,
            client_request_id=client_request_id,
        )
        material = self.repository.bootstrap_index_publication_material(
            str(prepared["publication_intent_record_id"])
        )
        if material["public_url"] != self.public_url:
            raise BootstrapIndexPublicationError(
                "public Bootstrap publication target changed"
            )
        try:
            public_object_revision_id = self.object_store.activate(
                material["payload"],
                expected_previous_sha256=material["previous_index_sha256"],
                candidate_sha256=material["candidate_index_sha256"],
            )
        except BootstrapIndexPublicationError:
            raise
        except OSError:
            raise BootstrapIndexPublicationError(
                "public Bootstrap object store is unavailable"
            ) from None
        try:
            observed = self.public_reader.read_exact(self.public_url)
        except BootstrapIndexPublicationError:
            raise
        except (httpx.HTTPError, OSError):
            raise BootstrapIndexPublicationError(
                "public Bootstrap readback is unavailable"
            ) from None
        active = self.repository.finalize_bootstrap_index_activation(
            intent_record_id=str(prepared["publication_intent_record_id"]),
            observed_bytes=observed,
            public_object_revision_id=public_object_revision_id,
            actor=actor,
            client_request_id="release_"
            + hashlib.sha256(
                (client_request_id + "\0finalize").encode("utf-8")
            ).hexdigest()[:32],
        )
        proof = _mapping(active.get("proof"), "trusted readback proof")
        authority = _mapping(active.get("authority"), "active authority")
        target = _mapping(authority.get("target"), "active authority target")
        return {
            "schema_version": 1,
            "release_id": active["release_id"],
            "version": target["version"],
            "state": "active-and-read-back",
            "index_sha256": active["index_sha256"],
            "index_size_bytes": active["index_size_bytes"],
            "public_url": active["public_url"],
            "staged_revision_id": active["staged_revision_id"],
            "active_activation_record_id": active["active_revision_id"],
            "active_sequence": authority["sequence"],
            "active_authority_revision_id": authority["revision"],
            "active_target": target,
            "public_object_revision_id": active["public_object_revision_id"],
            "previous_activation_record_id": active[
                "previous_activation_record_id"
            ],
            "previous_sequence": active["previous_sequence"],
            "previous_authority_revision_id": active["previous_revision_id"],
            "previous_index_sha256": active["previous_index_sha256"],
            "previous_target": active["previous_target"],
            "readback": dict(proof),
        }


def _is_unpublished_pointer(payload: bytes) -> bool:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and value.get("status") == "unpublished"
        and value.get("authority") is None
        and value.get("freshness") is None
        and value.get("release") is None
    )


def _activation_request(value: Mapping[str, object]) -> dict[str, Any]:
    expected_keys = {
        "revision_id",
        "index_sha256",
        "expected_previous_activation_record_id",
        "expected_previous_sequence",
        "expected_previous_authority_revision_id",
        "expected_previous_index_sha256",
        "expected_previous_target",
    }
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise ValueError("public Bootstrap activation request shape is invalid")
    required = (value.get("revision_id"), value.get("index_sha256"))
    prior = (
        value.get("expected_previous_activation_record_id"),
        value.get("expected_previous_sequence"),
        value.get("expected_previous_authority_revision_id"),
        value.get("expected_previous_index_sha256"),
        value.get("expected_previous_target"),
    )
    if (
        not isinstance(required[0], str)
        or re.fullmatch(r"bstage_[0-9a-f]{32}", required[0]) is None
        or not isinstance(required[1], str)
        or _SHA256.fullmatch(required[1]) is None
        or (any(item is None for item in prior) and any(item is not None for item in prior))
    ):
        raise ValueError("public Bootstrap activation request identity is invalid")
    if prior[0] is not None:
        if (
            not isinstance(prior[0], str)
            or re.fullmatch(r"bactive_[0-9a-f]{32}", prior[0]) is None
            or isinstance(prior[1], bool)
            or not isinstance(prior[1], int)
            or prior[1] < 1
            or not isinstance(prior[2], str)
            or re.fullmatch(r"release-stable-[0-9a-f]{24}", prior[2]) is None
            or not isinstance(prior[3], str)
            or _SHA256.fullmatch(prior[3]) is None
            or not isinstance(prior[4], dict)
        ):
            raise ValueError("public Bootstrap activation predecessor is invalid")
    return dict(value)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BootstrapIndexPublicationError(f"{label} is invalid")
    return value


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, target: Path) -> None:
    if os.name != "nt":
        os.replace(source, target)
        _fsync_directory(target.parent)
        return
    # MoveFileExW with WRITE_THROUGH is the Windows durability equivalent of
    # rename(2) followed by fsync(parent).  The temporary file was fsynced
    # above, so a successful return covers both content and directory metadata.
    import ctypes
    from ctypes import wintypes

    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    replace_existing = 0x1
    write_through = 0x8
    if not move_file(str(source), str(target), replace_existing | write_through):
        raise OSError(ctypes.get_last_error(), "durable public pointer replace failed")


__all__ = [
    "BootstrapIndexPublicationError",
    "BootstrapIndexPublicationService",
    "FilesystemPublicIndexObjectStore",
    "HTTPSPublicIndexReader",
    "PublicIndexObjectStore",
    "PublicIndexReader",
]
