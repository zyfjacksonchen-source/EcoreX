"""Control Plane persistence and rendering for immutable public shares."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import html
from pathlib import Path
import re
import secrets
import sqlite3
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit
import uuid

from ecorex.sharing import (
    MAX_SHARED_MEDIA_BYTES,
    MAX_SHARED_MEDIA_TOTAL_BYTES,
    PublishedShare,
    SharePayload,
    shared_media_declarations,
)

from .share_markdown import render_share_markdown
from .share_objects import (
    LocalShareObjectStore,
    ShareObjectCapacityError,
    ShareObjectError,
    ShareObjectRead,
    ShareObjectStore,
    ShareStoredObject,
)
from .share_schema import (
    CloudShareSchemaError,
    CloudShareSchemaManager as FixedCloudShareSchemaManager,
    CloudShareSchemaReceipt as FixedCloudShareSchemaReceipt,
)


_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_REMOTE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MEDIA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_MAX_MEDIA_BYTES = MAX_SHARED_MEDIA_BYTES
_MAX_SHARE_MEDIA_BYTES = MAX_SHARED_MEDIA_TOTAL_BYTES
_MAX_ACCOUNT_ORPHAN_BYTES = 256 * 1024 * 1024
_MAX_ACCOUNT_ORPHAN_SOURCES = 8
_ORPHAN_MAX_AGE = timedelta(hours=24)
_MEDIA_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "image/avif",
    }
)
_LEGACY_KEY_ID = "legacy-v1"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cloud share timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


class CloudShareError(RuntimeError):
    pass


class CloudShareConflict(CloudShareError):
    pass


class CloudShareNotFound(CloudShareError):
    pass


@dataclass(frozen=True, slots=True)
class PublicShareMedia:
    """A verified public rendition resolved through an active share token."""

    stream: ShareObjectRead
    mime_type: str
    sha256: str
    size_bytes: int
    etag: str


@dataclass(frozen=True, slots=True)
class CloudShareKeyRing:
    """Bounded token/state/audit signing keys with one issuance authority.

    Retired keys remain in ``keys`` for verification and revocation, while only
    ``active_key_id`` signs new snapshots and audit entries. Key identifiers are
    non-secret database metadata; key material never enters a projection or URL.

    ``legacy_key_id`` is only needed when opening a populated database created
    before key identities were persisted. Requiring it for an ambiguous upgrade
    prevents silently assigning historical rows to the wrong key.
    """

    active_key_id: str
    keys: Mapping[str, bytes] = field(repr=False)
    legacy_key_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.active_key_id, str) or not _KEY_ID.fullmatch(
            self.active_key_id
        ):
            raise ValueError("cloud share active key identity is invalid")
        if not isinstance(self.keys, Mapping) or not 1 <= len(self.keys) <= 16:
            raise ValueError("cloud share keyring must contain between 1 and 16 keys")
        normalized: dict[str, bytes] = {}
        for key_id, material in self.keys.items():
            if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
                raise ValueError("cloud share key identity is invalid")
            if not isinstance(material, bytes) or len(material) != 32:
                raise ValueError("cloud share signing keys must contain 32 bytes")
            normalized[key_id] = bytes(material)
        if self.active_key_id not in normalized:
            raise ValueError("cloud share active key is missing from the keyring")
        if self.legacy_key_id is not None and (
            not isinstance(self.legacy_key_id, str)
            or not _KEY_ID.fullmatch(self.legacy_key_id)
            or self.legacy_key_id not in normalized
        ):
            raise ValueError("cloud share legacy key identity is invalid")
        object.__setattr__(self, "keys", MappingProxyType(normalized))

    def key(self, key_id: str) -> bytes:
        try:
            return self.keys[key_id]
        except (KeyError, TypeError):
            raise CloudShareConflict(
                "cloud share signing key is unavailable"
            ) from None


class CloudShareRepository:
    def __init__(
        self,
        path: str | Path,
        *,
        token_key: bytes | None = None,
        keyring: CloudShareKeyRing | None = None,
        public_base_url: str,
        object_store: ShareObjectStore | None = None,
        clock=_utcnow,
    ) -> None:
        if keyring is not None and token_key is not None:
            raise ValueError("configure either a cloud share keyring or one legacy key")
        if keyring is None:
            if not isinstance(token_key, bytes) or len(token_key) != 32:
                raise ValueError("cloud share token key must contain 32 bytes")
            keyring = CloudShareKeyRing(
                active_key_id=_LEGACY_KEY_ID,
                keys={_LEGACY_KEY_ID: bytes(token_key)},
                legacy_key_id=_LEGACY_KEY_ID,
            )
        elif not isinstance(keyring, CloudShareKeyRing):
            raise ValueError("cloud share keyring is invalid")
        parsed = urlsplit(public_base_url.rstrip("/"))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/s"
        ):
            raise ValueError("public share base URL must be credential-free HTTPS ending in /s")
        self.path = Path(path).expanduser().resolve()
        if object_store is None:
            object_store = LocalShareObjectStore(
                self.path.with_name(self.path.name + ".share-objects")
            )
        if not isinstance(object_store, ShareObjectStore):
            raise TypeError("cloud share object store is invalid")
        self.object_store = object_store
        self._keyring = keyring
        self.public_base_url = public_base_url.rstrip("/")
        self.clock = clock
        self.schema_receipt: FixedCloudShareSchemaReceipt = FixedCloudShareSchemaManager(
            self.path,
            keyring=self._keyring,
        ).validate()

    def _connect(self) -> sqlite3.Connection:
        # A repository is a runtime consumer, never a storage bootstrapper.  In
        # particular, the default sqlite3 path form would silently create a new
        # empty database if an operator deleted or swapped the file after
        # composition.  ``mode=rw`` preserves fail-closed behavior on every
        # business operation, not only in the constructor validation.
        try:
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=rw&nofollow=1",
                uri=True,
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as error:
            raise CloudShareSchemaError(
                "cloud share schema database is unavailable"
            ) from error
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        return connection

    def _token(self, account_id: str, share_id: str, *, key_id: str) -> str:
        digest = hmac.new(
            self._keyring.key(key_id),
            b"ecorex-cloud-share-token-v1\n"
            + account_id.encode("utf-8")
            + b"\0"
            + share_id.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _now(self) -> datetime:
        now = self.clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise CloudShareConflict("cloud share clock is invalid")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _state_mac(
        self,
        *,
        remote_snapshot_id: str,
        account_id: str,
        source_share_id: str,
        thread_id: str,
        source_watermark: int,
        payload_sha256: str,
        token_sha256: str,
        status: str,
        expires_at: str,
        created_at: str,
        revoked_at: str | None,
        key_id: str,
        mac_version: int,
    ) -> str:
        if mac_version == 1:
            encoded = (
                f"ecorex-cloud-share-state-v1\n{remote_snapshot_id}\0{account_id}\0"
                f"{source_share_id}\0{thread_id}\0{source_watermark}\0{payload_sha256}\0"
                f"{token_sha256}\0{status}\0{expires_at}\0{created_at}\0{revoked_at or ''}"
            ).encode("utf-8")
        elif mac_version == 2:
            encoded = (
                f"ecorex-cloud-share-state-v2\n{key_id}\0{remote_snapshot_id}\0{account_id}\0"
                f"{source_share_id}\0{thread_id}\0{source_watermark}\0{payload_sha256}\0"
                f"{token_sha256}\0{status}\0{expires_at}\0{created_at}\0{revoked_at or ''}"
            ).encode("utf-8")
        else:
            raise CloudShareConflict("cloud share state MAC version is invalid")
        return hmac.new(self._keyring.key(key_id), encoded, hashlib.sha256).hexdigest()

    @staticmethod
    def _media_has_expected_signature(content: bytes, mime_type: str) -> bool:
        if mime_type == "image/png":
            return content.startswith(b"\x89PNG\r\n\x1a\n")
        if mime_type == "image/jpeg":
            return content.startswith(b"\xff\xd8\xff")
        if mime_type == "image/webp":
            return (
                len(content) >= 12
                and content.startswith(b"RIFF")
                and content[8:12] == b"WEBP"
            )
        if mime_type == "image/gif":
            return content.startswith((b"GIF87a", b"GIF89a"))
        if mime_type == "image/avif":
            header = content[:64]
            return (
                len(header) >= 16
                and header[4:8] == b"ftyp"
                and (b"avif" in header[8:] or b"avis" in header[8:])
            )
        return False

    @staticmethod
    def _media_declarations(
        payload: SharePayload,
    ) -> dict[str, tuple[str, str, int, str]]:
        return shared_media_declarations(
            payload, require_publishable_schema=False
        )

    def _validate_media_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        verify_object: bool = False,
    ) -> ShareStoredObject:
        try:
            created_at = datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            raise CloudShareConflict("cloud share media storage is invalid") from None
        expected_idempotency = f'{row["source_share_id"]}:{row["media_id"]}'
        if (
            not isinstance(row["account_id"], str)
            or not _ACCOUNT_ID.fullmatch(row["account_id"])
            or not isinstance(row["source_share_id"], str)
            or not re.fullmatch(r"shr_[0-9a-f]{32}", row["source_share_id"])
            or not isinstance(row["media_id"], str)
            or not _MEDIA_ID.fullmatch(row["media_id"])
            or row["idempotency_key"] != expected_idempotency
            or row["kind"] not in {"preview", "thumbnail"}
            or row["mime_type"] not in _MEDIA_TYPES
            or not isinstance(row["size_bytes"], int)
            or isinstance(row["size_bytes"], bool)
            or not 1 <= row["size_bytes"] <= _MAX_MEDIA_BYTES
            or not isinstance(row["sha256"], str)
            or not _SHA256.fullmatch(row["sha256"])
            or created_at.tzinfo is None
            or created_at.utcoffset() is None
        ):
            raise CloudShareConflict("cloud share media integrity check failed")
        object_row = connection.execute(
            "SELECT * FROM cloud_share_objects WHERE object_key=?",
            (row["object_key"],),
        ).fetchone()
        if (
            object_row is None
            or object_row["state"] != "active"
            or not isinstance(object_row["ref_count"], int)
            or object_row["ref_count"] < 1
            or object_row["sha256"] != row["sha256"]
            or object_row["size_bytes"] != row["size_bytes"]
            or object_row["mime_type"] != row["mime_type"]
        ):
            raise CloudShareConflict("cloud share object reference is invalid")
        try:
            descriptor = ShareStoredObject(
                object_key=str(object_row["object_key"]),
                sha256=str(object_row["sha256"]),
                size_bytes=int(object_row["size_bytes"]),
                mime_type=str(object_row["mime_type"]),
                etag=str(object_row["etag"]),
            )
            if verify_object:
                self.object_store.open(
                    descriptor.object_key,
                    sha256=descriptor.sha256,
                    size_bytes=descriptor.size_bytes,
                    mime_type=descriptor.mime_type,
                ).close()
        except (ShareObjectError, TypeError, ValueError):
            raise CloudShareConflict("cloud share object integrity check failed") from None
        return descriptor

    def _purge_expired_orphans(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
    ) -> tuple[ShareStoredObject, ...]:
        cutoff = _iso(now - _ORPHAN_MAX_AGE)
        rows = connection.execute(
            "SELECT media.* FROM cloud_share_media AS media WHERE media.created_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM cloud_share_media_links AS link "
            "WHERE link.account_id=media.account_id "
            "AND link.source_share_id=media.source_share_id "
            "AND link.media_id=media.media_id AND link.released_at IS NULL)",
            (cutoff,),
        ).fetchall()
        return self._delete_media_references(connection, rows)

    def _delete_media_references(
        self,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
    ) -> tuple[ShareStoredObject, ...]:
        counts: dict[str, int] = {}
        for row in rows:
            counts[str(row["object_key"])] = counts.get(str(row["object_key"]), 0) + 1
            connection.execute(
                "DELETE FROM cloud_share_media WHERE account_id=? "
                "AND source_share_id=? AND media_id=?",
                (row["account_id"], row["source_share_id"], row["media_id"]),
            )
        pending: list[ShareStoredObject] = []
        for object_key, decrement in counts.items():
            object_row = connection.execute(
                "SELECT * FROM cloud_share_objects WHERE object_key=?",
                (object_key,),
            ).fetchone()
            if (
                object_row is None
                or object_row["state"] != "active"
                or int(object_row["ref_count"]) < decrement
            ):
                raise CloudShareConflict("cloud share object reference count is invalid")
            remaining = int(object_row["ref_count"]) - decrement
            connection.execute(
                "UPDATE cloud_share_objects SET ref_count=?, state=? WHERE object_key=?",
                (remaining, "deleting" if remaining == 0 else "active", object_key),
            )
            if remaining == 0:
                pending.append(
                    ShareStoredObject(
                        object_key=object_key,
                        sha256=str(object_row["sha256"]),
                        size_bytes=int(object_row["size_bytes"]),
                        mime_type=str(object_row["mime_type"]),
                        etag=str(object_row["etag"]),
                    )
                )
        return tuple(pending)

    def _drain_object_deletions(
        self,
        pending: tuple[ShareStoredObject, ...] = (),
    ) -> int:
        if not pending:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT * FROM cloud_share_objects "
                    "WHERE state='deleting' AND ref_count=0"
                ).fetchall()
                pending = tuple(
                    ShareStoredObject(
                        object_key=str(row["object_key"]),
                        sha256=str(row["sha256"]),
                        size_bytes=int(row["size_bytes"]),
                        mime_type=str(row["mime_type"]),
                        etag=str(row["etag"]),
                    )
                    for row in rows
                )
            finally:
                connection.close()
        deleted = 0
        for descriptor in pending:
            try:
                self.object_store.delete(
                    descriptor.object_key,
                    sha256=descriptor.sha256,
                )
            except ShareObjectError:
                continue
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "DELETE FROM cloud_share_objects WHERE object_key=? "
                    "AND state='deleting' AND ref_count=0",
                    (descriptor.object_key,),
                )
                connection.commit()
                deleted += max(0, cursor.rowcount)
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
        return deleted

    def stage_media(
        self,
        account_id: str,
        source_share_id: str,
        media_id: str,
        *,
        content: bytes,
        kind: str,
        mime_type: str,
        content_sha256: str,
        idempotency_key: str,
    ) -> None:
        """Stage one immutable image before its snapshot becomes public."""

        if (
            not isinstance(account_id, str)
            or not _ACCOUNT_ID.fullmatch(account_id)
            or not isinstance(source_share_id, str)
            or not re.fullmatch(r"shr_[0-9a-f]{32}", source_share_id)
            or not isinstance(media_id, str)
            or not _MEDIA_ID.fullmatch(media_id)
            or idempotency_key != f"{source_share_id}:{media_id}"
            or not _IDEMPOTENCY.fullmatch(idempotency_key)
            or not isinstance(content, bytes)
            or not 1 <= len(content) <= _MAX_MEDIA_BYTES
            or kind not in {"preview", "thumbnail"}
            or mime_type not in _MEDIA_TYPES
            or not isinstance(content_sha256, str)
            or not _SHA256.fullmatch(content_sha256)
            or hashlib.sha256(content).hexdigest() != content_sha256
            or not self._media_has_expected_signature(content, mime_type)
        ):
            raise CloudShareConflict("cloud share media upload is invalid")
        try:
            stored = self.object_store.put(
                content,
                sha256=content_sha256,
                mime_type=mime_type,
            )
        except ShareObjectError:
            raise CloudShareConflict("cloud share media object storage failed") from None
        if (
            stored.sha256 != content_sha256
            or stored.size_bytes != len(content)
            or stored.mime_type != mime_type
            or stored.etag != content_sha256
        ):
            raise CloudShareConflict("cloud share media object storage is invalid")
        now = self._now()
        pending_deletions: tuple[ShareStoredObject, ...] = ()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_audit(connection)
            pending_deletions = self._purge_expired_orphans(connection, now=now)
            existing = connection.execute(
                "SELECT * FROM cloud_share_media WHERE account_id=? "
                "AND source_share_id=? AND media_id=?",
                (account_id, source_share_id, media_id),
            ).fetchone()
            if existing is not None:
                descriptor = self._validate_media_row(connection, existing)
                if (
                    existing["idempotency_key"] != idempotency_key
                    or existing["kind"] != kind
                    or existing["mime_type"] != mime_type
                    or existing["sha256"] != content_sha256
                    or descriptor.object_key != stored.object_key
                ):
                    raise CloudShareConflict(
                        "cloud share media identity was reused with different input"
                )
                connection.commit()
                self._drain_object_deletions(pending_deletions)
                return
            reused_operation = connection.execute(
                "SELECT account_id, source_share_id, media_id FROM cloud_share_media "
                "WHERE account_id=? AND idempotency_key=?",
                (account_id, idempotency_key),
            ).fetchone()
            if reused_operation is not None:
                raise CloudShareConflict("cloud share media idempotency key was reused")
            published = connection.execute(
                "SELECT 1 FROM cloud_share_snapshots WHERE account_id=? AND source_share_id=?",
                (account_id, source_share_id),
            ).fetchone()
            if published is not None:
                raise CloudShareConflict("published cloud share media is immutable")
            orphan_totals = connection.execute(
                "SELECT COUNT(DISTINCT media.source_share_id), "
                "COALESCE(SUM(media.size_bytes), 0) FROM cloud_share_media AS media "
                "WHERE media.account_id=? AND NOT EXISTS ("
                "SELECT 1 FROM cloud_share_media_links AS link "
                "WHERE link.account_id=media.account_id "
                "AND link.source_share_id=media.source_share_id "
                "AND link.media_id=media.media_id)",
                (account_id,),
            ).fetchone()
            source_exists = connection.execute(
                "SELECT 1 FROM cloud_share_media WHERE account_id=? AND source_share_id=?",
                (account_id, source_share_id),
            ).fetchone()
            if (
                source_exists is None
                and int(orphan_totals[0]) >= _MAX_ACCOUNT_ORPHAN_SOURCES
            ):
                raise CloudShareConflict(
                    "cloud share account has too many unpublished media sources"
                )
            if int(orphan_totals[1]) + len(content) > _MAX_ACCOUNT_ORPHAN_BYTES:
                raise CloudShareConflict(
                    "cloud share account unpublished media exceeds its size limit"
                )
            staged_bytes = int(
                connection.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) FROM cloud_share_media "
                    "WHERE account_id=? AND source_share_id=?",
                    (account_id, source_share_id),
                ).fetchone()[0]
            )
            if staged_bytes + len(content) > _MAX_SHARE_MEDIA_BYTES:
                raise CloudShareConflict("cloud share media exceeds its total size limit")
            object_row = connection.execute(
                "SELECT * FROM cloud_share_objects WHERE sha256=?",
                (stored.sha256,),
            ).fetchone()
            if object_row is None:
                connection.execute(
                    "INSERT INTO cloud_share_objects(object_key, sha256, size_bytes, "
                    "mime_type, etag, ref_count, state, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, 'active', ?)",
                    (
                        stored.object_key,
                        stored.sha256,
                        stored.size_bytes,
                        stored.mime_type,
                        stored.etag,
                        _iso(now),
                    ),
                )
            elif (
                object_row["object_key"] != stored.object_key
                or object_row["size_bytes"] != stored.size_bytes
                or object_row["mime_type"] != stored.mime_type
                or object_row["etag"] != stored.etag
            ):
                raise CloudShareConflict("cloud share object identity conflicts")
            elif object_row["state"] == "deleting" and object_row["ref_count"] == 0:
                connection.execute(
                    "UPDATE cloud_share_objects SET ref_count=1, state='active' "
                    "WHERE object_key=? AND state='deleting' AND ref_count=0",
                    (stored.object_key,),
                )
                pending_deletions = tuple(
                    item
                    for item in pending_deletions
                    if item.object_key != stored.object_key
                )
            elif object_row["state"] != "active":
                raise CloudShareConflict("cloud share object is unavailable")
            else:
                connection.execute(
                    "UPDATE cloud_share_objects SET ref_count=ref_count+1 "
                    "WHERE object_key=? AND state='active'",
                    (stored.object_key,),
                )
            connection.execute(
                "INSERT INTO cloud_share_media(account_id, source_share_id, media_id, "
                "idempotency_key, kind, mime_type, size_bytes, sha256, object_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account_id,
                    source_share_id,
                    media_id,
                    idempotency_key,
                    kind,
                    mime_type,
                    len(content),
                    content_sha256,
                    stored.object_key,
                    _iso(now),
                ),
            )
            connection.commit()
            self._drain_object_deletions(pending_deletions)
        except sqlite3.IntegrityError as error:
            if connection.in_transaction:
                connection.rollback()
            raise CloudShareConflict("cloud share media conflicts with existing state") from error
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _require_declared_media(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        payload: SharePayload,
    ) -> None:
        for media_id, expected in self._media_declarations(payload).items():
            row = connection.execute(
                "SELECT * FROM cloud_share_media WHERE account_id=? "
                "AND source_share_id=? AND media_id=?",
                (account_id, payload.share_id, media_id),
            ).fetchone()
            if row is None:
                raise CloudShareConflict("cloud share media is not staged")
            self._validate_media_row(connection, row, verify_object=True)
            if (
                row["kind"],
                row["mime_type"],
                row["size_bytes"],
                row["sha256"],
            ) != expected:
                raise CloudShareConflict("cloud share media does not match its declaration")

    def _link_declared_media(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        payload: SharePayload,
        remote_snapshot_id: str,
        now: datetime,
    ) -> None:
        for media_id in self._media_declarations(payload):
            existing = connection.execute(
                "SELECT * FROM cloud_share_media_links WHERE account_id=? "
                "AND source_share_id=? AND media_id=?",
                (account_id, payload.share_id, media_id),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO cloud_share_media_links(account_id, source_share_id, "
                    "media_id, remote_snapshot_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        account_id,
                        payload.share_id,
                        media_id,
                        remote_snapshot_id,
                        _iso(now),
                    ),
                )
                continue
            if existing["remote_snapshot_id"] != remote_snapshot_id:
                raise CloudShareConflict("cloud share media link conflicts with snapshot")

    def _require_media_links(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        payload: SharePayload,
        remote_snapshot_id: str,
    ) -> None:
        expected_ids = set(self._media_declarations(payload))
        rows = connection.execute(
            "SELECT media_id, remote_snapshot_id, released_at "
            "FROM cloud_share_media_links "
            "WHERE account_id=? AND source_share_id=?",
            (account_id, payload.share_id),
        ).fetchall()
        if {row["media_id"] for row in rows} != expected_ids or any(
            row["remote_snapshot_id"] != remote_snapshot_id
            or row["released_at"] is not None
            for row in rows
        ):
            raise CloudShareConflict("cloud share media links are invalid")

    def _release_snapshot_media(
        self,
        connection: sqlite3.Connection,
        *,
        remote_snapshot_id: str,
        now: datetime,
    ) -> tuple[ShareStoredObject, ...]:
        rows = connection.execute(
            "SELECT media.* FROM cloud_share_media_links AS link "
            "JOIN cloud_share_media AS media "
            "ON media.account_id=link.account_id "
            "AND media.source_share_id=link.source_share_id "
            "AND media.media_id=link.media_id "
            "WHERE link.remote_snapshot_id=? AND link.released_at IS NULL",
            (remote_snapshot_id,),
        ).fetchall()
        connection.execute(
            "UPDATE cloud_share_media_links SET released_at=? "
            "WHERE remote_snapshot_id=? AND released_at IS NULL",
            (_iso(now), remote_snapshot_id),
        )
        return self._delete_media_references(connection, rows)

    def publish(
        self,
        account_id: str,
        payload: SharePayload,
        *,
        idempotency_key: str,
    ) -> PublishedShare:
        if (
            not isinstance(account_id, str)
            or not _ACCOUNT_ID.fullmatch(account_id)
            or idempotency_key != payload.share_id
        ):
            raise CloudShareConflict("cloud share idempotency identity is invalid")
        encoded = payload.canonical_bytes()
        if len(encoded) > _MAX_PAYLOAD_BYTES:
            raise CloudShareConflict("cloud share payload exceeds its size limit")
        # New publication accepts only the image-safe v2 contract. Schema v1
        # remains readable so historical signatures/canonical bytes stay
        # valid, but it is no longer an issuance protocol.
        shared_media_declarations(payload, require_publishable_schema=True)
        # Refuse a snapshot that cannot be rendered within the public response
        # ceiling.  This keeps publish success from creating a permanently
        # unreadable public identity after HTML escaping expands the content.
        render_public_share(payload)
        now = self._now()
        if (
            payload.created_at > now + timedelta(minutes=5)
            or payload.expires_at <= now
            or payload.expires_at - payload.created_at > timedelta(days=30)
            or payload.expires_at > now + timedelta(days=31)
        ):
            raise CloudShareConflict("cloud share timestamps are invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_audit(connection)
            self._require_declared_media(connection, account_id, payload)
            existing = connection.execute(
                "SELECT * FROM cloud_share_snapshots WHERE account_id=? AND source_share_id=?",
                (account_id, payload.share_id),
            ).fetchone()
            if existing is not None:
                stored_payload = self._validate_row(existing)
                if stored_payload.sha256 != payload.sha256:
                    raise CloudShareConflict(
                        "cloud share identity was reused with different input"
                    )
                if existing["status"] != "active":
                    raise CloudShareConflict("revoked cloud share cannot be republished")
                if datetime.fromisoformat(existing["expires_at"]) <= now:
                    raise CloudShareConflict("expired cloud share cannot be republished")
                self._link_declared_media(
                    connection,
                    account_id=account_id,
                    payload=payload,
                    remote_snapshot_id=existing["remote_snapshot_id"],
                    now=now,
                )
                token = self._token(
                    account_id,
                    payload.share_id,
                    key_id=existing["token_key_id"],
                )
                connection.commit()
                return PublishedShare(
                    remote_snapshot_id=existing["remote_snapshot_id"],
                    public_url=f"{self.public_base_url}/{token}",
                )
            key_id = self._keyring.active_key_id
            mac_version = 2
            token = self._token(account_id, payload.share_id, key_id=key_id)
            token_digest = self._token_digest(token)
            remote_id = "cshr_" + uuid.uuid4().hex
            expires_at = _iso(payload.expires_at)
            created_at = _iso(now)
            state_mac = self._state_mac(
                remote_snapshot_id=remote_id,
                account_id=account_id,
                source_share_id=payload.share_id,
                thread_id=payload.thread_id,
                source_watermark=payload.source_watermark,
                payload_sha256=payload.sha256,
                token_sha256=token_digest,
                status="active",
                expires_at=expires_at,
                created_at=created_at,
                revoked_at=None,
                key_id=key_id,
                mac_version=mac_version,
            )
            connection.execute(
                "INSERT INTO cloud_share_snapshots("
                "remote_snapshot_id, account_id, source_share_id, thread_id, "
                "source_watermark, payload_json, payload_sha256, token_sha256, "
                "token_key_id, state_mac_version, state_mac, status, expires_at, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    remote_id,
                    account_id,
                    payload.share_id,
                    payload.thread_id,
                    payload.source_watermark,
                    encoded.decode("utf-8"),
                    payload.sha256,
                    token_digest,
                    key_id,
                    mac_version,
                    state_mac,
                    expires_at,
                    created_at,
                ),
            )
            self._link_declared_media(
                connection,
                account_id=account_id,
                payload=payload,
                remote_snapshot_id=remote_id,
                now=now,
            )
            self._append_audit(
                connection,
                account_id=account_id,
                action="share.publish",
                target_id=remote_id,
                payload_sha256=payload.sha256,
                now=now,
            )
            connection.commit()
            return PublishedShare(
                remote_snapshot_id=remote_id,
                public_url=f"{self.public_base_url}/{token}",
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def revoke(
        self,
        account_id: str,
        remote_snapshot_id: str,
        *,
        idempotency_key: str,
    ) -> None:
        if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
            raise CloudShareNotFound("cloud share was not found")
        if not isinstance(remote_snapshot_id, str) or not _REMOTE_ID.fullmatch(
            remote_snapshot_id
        ):
            raise CloudShareNotFound("cloud share was not found")
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY.fullmatch(
            idempotency_key
        ):
            raise CloudShareConflict("cloud share revoke idempotency key is invalid")
        fingerprint = hashlib.sha256(
            f"ecorex-cloud-share-revoke-v1\n{account_id}\0{remote_snapshot_id}".encode(
                "utf-8"
            )
        ).hexdigest()
        now = self._now()
        pending_deletions: tuple[ShareStoredObject, ...] = ()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_audit(connection)
            row = connection.execute(
                "SELECT * FROM cloud_share_snapshots WHERE remote_snapshot_id=? AND account_id=?",
                (remote_snapshot_id, account_id),
            ).fetchone()
            if row is None:
                raise CloudShareNotFound("cloud share was not found")
            self._validate_row(row)
            operation = connection.execute(
                "SELECT * FROM cloud_share_operations WHERE account_id=? AND idempotency_key=?",
                (account_id, idempotency_key),
            ).fetchone()
            if operation is not None:
                if operation["request_fingerprint"] != fingerprint:
                    raise CloudShareConflict(
                        "cloud share revoke idempotency key was reused"
                    )
            else:
                connection.execute(
                    "INSERT INTO cloud_share_operations(operation_id, account_id, "
                    "idempotency_key, action, target_id, request_fingerprint, created_at) "
                    "VALUES (?, ?, ?, 'share.revoke', ?, ?, ?)",
                    (
                        "csop_" + uuid.uuid4().hex,
                        account_id,
                        idempotency_key,
                        remote_snapshot_id,
                        fingerprint,
                        _iso(now),
                    ),
                )
            if row["status"] == "active":
                revoked_at = _iso(now)
                state_mac = self._state_mac(
                    remote_snapshot_id=row["remote_snapshot_id"],
                    account_id=row["account_id"],
                    source_share_id=row["source_share_id"],
                    thread_id=row["thread_id"],
                    source_watermark=row["source_watermark"],
                    payload_sha256=row["payload_sha256"],
                    token_sha256=row["token_sha256"],
                    status="revoked",
                    expires_at=row["expires_at"],
                    created_at=row["created_at"],
                    revoked_at=revoked_at,
                    key_id=row["token_key_id"],
                    mac_version=row["state_mac_version"],
                )
                connection.execute(
                    "UPDATE cloud_share_snapshots SET status='revoked', revoked_at=?, state_mac=? "
                    "WHERE remote_snapshot_id=?",
                    (revoked_at, state_mac, remote_snapshot_id),
                )
                self._append_audit(
                    connection,
                    account_id=account_id,
                    action="share.revoke",
                    target_id=remote_snapshot_id,
                    payload_sha256=row["payload_sha256"],
                    now=now,
                )
            pending_deletions = self._release_snapshot_media(
                connection,
                remote_snapshot_id=remote_snapshot_id,
                now=now,
            )
            connection.commit()
            self._drain_object_deletions(pending_deletions)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def resolve_public(self, token: str) -> SharePayload:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise CloudShareNotFound("cloud share was not found")
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM cloud_share_snapshots WHERE token_sha256=?",
                (self._token_digest(token),),
            ).fetchone()
            if row is None:
                raise CloudShareNotFound("cloud share was not found")
            payload = self._validate_row(row)
            if row["status"] != "active" or payload.expires_at <= now:
                raise CloudShareNotFound("cloud share was not found")
            self._require_declared_media(connection, row["account_id"], payload)
            self._require_media_links(
                connection,
                account_id=row["account_id"],
                payload=payload,
                remote_snapshot_id=row["remote_snapshot_id"],
            )
            connection.commit()
            return payload
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def reap_expired_media(self) -> int:
        """Release media references for expired snapshots and retry tombstones."""

        now = self._now()
        pending: list[ShareStoredObject] = []
        released = 0
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                "SELECT remote_snapshot_id FROM cloud_share_snapshots "
                "WHERE expires_at<=? AND EXISTS ("
                "SELECT 1 FROM cloud_share_media_links AS link "
                "WHERE link.remote_snapshot_id=cloud_share_snapshots.remote_snapshot_id "
                "AND link.released_at IS NULL)",
                (_iso(now),),
            ).fetchall()
            for row in expired:
                released += int(
                    connection.execute(
                        "SELECT COUNT(*) FROM cloud_share_media_links "
                        "WHERE remote_snapshot_id=? AND released_at IS NULL",
                        (row["remote_snapshot_id"],),
                    ).fetchone()[0]
                )
                pending.extend(
                    self._release_snapshot_media(
                        connection,
                        remote_snapshot_id=str(row["remote_snapshot_id"]),
                        now=now,
                    )
                )
            pending.extend(self._purge_expired_orphans(connection, now=now))
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        self._drain_object_deletions(tuple(pending))
        self._drain_object_deletions()
        return released

    def resolve_public_media(self, token: str, media_id: str) -> PublicShareMedia:
        """Resolve only media referenced by one active, unexpired snapshot."""

        if (
            not isinstance(token, str)
            or not _TOKEN.fullmatch(token)
            or not isinstance(media_id, str)
            or not _MEDIA_ID.fullmatch(media_id)
        ):
            raise CloudShareNotFound("cloud share media was not found")
        now = self._now()
        opened: ShareObjectRead | None = None
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            snapshot = connection.execute(
                "SELECT * FROM cloud_share_snapshots WHERE token_sha256=?",
                (self._token_digest(token),),
            ).fetchone()
            if snapshot is None:
                raise CloudShareNotFound("cloud share media was not found")
            payload = self._validate_row(snapshot)
            if snapshot["status"] != "active" or payload.expires_at <= now:
                raise CloudShareNotFound("cloud share media was not found")
            declaration = self._media_declarations(payload).get(media_id)
            if declaration is None:
                raise CloudShareNotFound("cloud share media was not found")
            self._require_media_links(
                connection,
                account_id=snapshot["account_id"],
                payload=payload,
                remote_snapshot_id=snapshot["remote_snapshot_id"],
            )
            media = connection.execute(
                "SELECT * FROM cloud_share_media WHERE account_id=? "
                "AND source_share_id=? AND media_id=?",
                (snapshot["account_id"], payload.share_id, media_id),
            ).fetchone()
            if media is None:
                raise CloudShareNotFound("cloud share media was not found")
            descriptor = self._validate_media_row(connection, media)
            if (
                media["kind"],
                media["mime_type"],
                media["size_bytes"],
                media["sha256"],
            ) != declaration:
                raise CloudShareConflict("cloud share media declaration was tampered")
            try:
                opened = self.object_store.open(
                    descriptor.object_key,
                    sha256=descriptor.sha256,
                    size_bytes=descriptor.size_bytes,
                    mime_type=descriptor.mime_type,
                )
            except ShareObjectCapacityError:
                raise
            except ShareObjectError:
                raise CloudShareConflict("cloud share media object is unavailable") from None
            connection.commit()
            result = PublicShareMedia(
                stream=opened,
                mime_type=media["mime_type"],
                sha256=media["sha256"],
                size_bytes=descriptor.size_bytes,
                etag=descriptor.etag,
            )
            opened = None
            self._record_object_access(descriptor.object_key, now=now)
            return result
        except BaseException:
            if opened is not None:
                opened.close()
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _record_object_access(self, object_key: str, *, now: datetime) -> None:
        """Best-effort observability must never weaken public availability."""

        connection = self._connect()
        try:
            connection.execute("PRAGMA busy_timeout=50")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE cloud_share_objects SET last_accessed_at=?, "
                "access_count=access_count+1 WHERE object_key=? AND state='active'",
                (_iso(now), object_key),
            )
            connection.commit()
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()

    def _validate_row(self, row: sqlite3.Row) -> SharePayload:
        encoded = row["payload_json"].encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != row["payload_sha256"]:
            raise CloudShareConflict("cloud share payload integrity check failed")
        try:
            payload = SharePayload.model_validate_json(encoded)
        except ValueError:
            raise CloudShareConflict("cloud share payload is invalid") from None
        if (
            payload.share_id != row["source_share_id"]
            or payload.thread_id != row["thread_id"]
            or payload.source_watermark != row["source_watermark"]
            or _iso(payload.expires_at) != row["expires_at"]
        ):
            raise CloudShareConflict("cloud share payload identity is invalid")
        key_id = row["token_key_id"]
        mac_version = row["state_mac_version"]
        if (
            not isinstance(key_id, str)
            or not _KEY_ID.fullmatch(key_id)
            or not isinstance(mac_version, int)
            or isinstance(mac_version, bool)
            or mac_version not in {1, 2}
        ):
            raise CloudShareConflict("cloud share key identity is invalid")
        if (
            not isinstance(row["account_id"], str)
            or not _ACCOUNT_ID.fullmatch(row["account_id"])
            or not isinstance(row["remote_snapshot_id"], str)
            or not _REMOTE_ID.fullmatch(row["remote_snapshot_id"])
            or row["token_sha256"]
            != self._token_digest(
                self._token(
                    row["account_id"], payload.share_id, key_id=key_id
                )
            )
        ):
            raise CloudShareConflict("cloud share storage identity is invalid")
        try:
            created_at = datetime.fromisoformat(row["created_at"])
            expires_at = datetime.fromisoformat(row["expires_at"])
            revoked_at = (
                None
                if row["revoked_at"] is None
                else datetime.fromisoformat(row["revoked_at"])
            )
        except (TypeError, ValueError):
            raise CloudShareConflict("cloud share timestamp is invalid") from None
        if (
            created_at.tzinfo is None
            or expires_at.tzinfo is None
            or (revoked_at is not None and revoked_at.tzinfo is None)
            or expires_at <= payload.created_at
            or expires_at <= created_at
            or row["status"] not in {"active", "revoked"}
            or (row["status"] == "active" and revoked_at is not None)
            or (row["status"] == "revoked" and revoked_at is None)
            or (revoked_at is not None and revoked_at < created_at)
        ):
            raise CloudShareConflict("cloud share state is invalid")
        expected_state_mac = self._state_mac(
            remote_snapshot_id=row["remote_snapshot_id"],
            account_id=row["account_id"],
            source_share_id=row["source_share_id"],
            thread_id=row["thread_id"],
            source_watermark=row["source_watermark"],
            payload_sha256=row["payload_sha256"],
            token_sha256=row["token_sha256"],
            status=row["status"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
            key_id=key_id,
            mac_version=mac_version,
        )
        if not isinstance(row["state_mac"], str) or not secrets.compare_digest(
            expected_state_mac, row["state_mac"]
        ):
            raise CloudShareConflict("cloud share state authentication failed")
        return payload

    def _audit_digest(
        self,
        *,
        sequence: int,
        event_id: str,
        account_id: str,
        action: str,
        target_id: str,
        payload_sha256: str,
        previous_digest: str,
        created_at: str,
        key_id: str,
        mac_version: int,
    ) -> str:
        if mac_version == 1:
            encoded = (
                f"ecorex-cloud-share-audit-v1\n{sequence}\0{event_id}\0{account_id}\0"
                f"{action}\0{target_id}\0{payload_sha256}\0{previous_digest}\0{created_at}"
            ).encode("utf-8")
            domain = b"ecorex-cloud-share-audit-mac-v1\n"
        elif mac_version == 2:
            encoded = (
                f"ecorex-cloud-share-audit-v2\n{key_id}\0{sequence}\0{event_id}\0"
                f"{account_id}\0{action}\0{target_id}\0{payload_sha256}\0"
                f"{previous_digest}\0{created_at}"
            ).encode("utf-8")
            domain = b"ecorex-cloud-share-audit-mac-v2\n"
        else:
            raise CloudShareConflict("cloud share audit MAC version is invalid")
        return hmac.new(
            self._keyring.key(key_id),
            domain + encoded,
            hashlib.sha256,
        ).hexdigest()

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        action: str,
        target_id: str,
        payload_sha256: str,
        now: datetime,
    ) -> None:
        previous = connection.execute(
            "SELECT sequence, entry_digest FROM cloud_share_audit "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        sequence = 1 if previous is None else int(previous["sequence"]) + 1
        previous_digest = "0" * 64 if previous is None else previous["entry_digest"]
        event_id = "csaud_" + uuid.uuid4().hex
        created_at = _iso(now)
        key_id = self._keyring.active_key_id
        mac_version = 2
        digest = self._audit_digest(
            sequence=sequence,
            event_id=event_id,
            account_id=account_id,
            action=action,
            target_id=target_id,
            payload_sha256=payload_sha256,
            previous_digest=previous_digest,
            created_at=created_at,
            key_id=key_id,
            mac_version=mac_version,
        )
        connection.execute(
            "INSERT INTO cloud_share_audit(sequence, event_id, account_id, action, "
            "target_id, payload_sha256, previous_digest, key_id, mac_version, "
            "entry_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sequence,
                event_id,
                account_id,
                action,
                target_id,
                payload_sha256,
                previous_digest,
                key_id,
                mac_version,
                digest,
                created_at,
            ),
        )

    def _verify_audit(self, connection: sqlite3.Connection) -> None:
        previous_digest = "0" * 64
        expected_sequence = 1
        ledger: dict[str, tuple[str, str, str]] = {}
        for row in connection.execute(
            "SELECT * FROM cloud_share_audit ORDER BY sequence"
        ).fetchall():
            if row["sequence"] != expected_sequence or row["previous_digest"] != previous_digest:
                raise CloudShareConflict("cloud share audit chain is invalid")
            key_id = row["key_id"]
            mac_version = row["mac_version"]
            if (
                not isinstance(key_id, str)
                or not _KEY_ID.fullmatch(key_id)
                or not isinstance(mac_version, int)
                or isinstance(mac_version, bool)
                or mac_version not in {1, 2}
            ):
                raise CloudShareConflict("cloud share audit key identity is invalid")
            expected = self._audit_digest(
                sequence=row["sequence"],
                event_id=row["event_id"],
                account_id=row["account_id"],
                action=row["action"],
                target_id=row["target_id"],
                payload_sha256=row["payload_sha256"],
                previous_digest=row["previous_digest"],
                created_at=row["created_at"],
                key_id=key_id,
                mac_version=mac_version,
            )
            if not secrets.compare_digest(expected, row["entry_digest"]):
                raise CloudShareConflict("cloud share audit digest is invalid")
            if row["action"] == "share.publish":
                if row["target_id"] in ledger:
                    raise CloudShareConflict("cloud share audit lifecycle is invalid")
                ledger[row["target_id"]] = (
                    row["account_id"],
                    row["payload_sha256"],
                    "active",
                )
            elif row["action"] == "share.revoke":
                prior = ledger.get(row["target_id"])
                if (
                    prior is None
                    or prior[0] != row["account_id"]
                    or prior[1] != row["payload_sha256"]
                    or prior[2] != "active"
                ):
                    raise CloudShareConflict("cloud share audit lifecycle is invalid")
                ledger[row["target_id"]] = (prior[0], prior[1], "revoked")
            else:
                raise CloudShareConflict("cloud share audit action is invalid")
            previous_digest = row["entry_digest"]
            expected_sequence += 1

        snapshots = connection.execute(
            "SELECT * FROM cloud_share_snapshots ORDER BY remote_snapshot_id"
        ).fetchall()
        if len(snapshots) != len(ledger):
            raise CloudShareConflict("cloud share audit coverage is invalid")
        for snapshot in snapshots:
            self._validate_row(snapshot)
            expected = ledger.get(snapshot["remote_snapshot_id"])
            if expected != (
                snapshot["account_id"],
                snapshot["payload_sha256"],
                snapshot["status"],
            ):
                raise CloudShareConflict("cloud share audit state is invalid")


def render_public_share(payload: SharePayload, *, public_token: str | None = None) -> bytes:
    """Render an immutable snapshot as a real, script-free chat transcript."""

    if public_token is not None and not _TOKEN.fullmatch(public_token):
        raise CloudShareConflict("public share token is invalid")
    # Historical schema-v1 text/file snapshots remain readable. Any v2 image
    # that lacks its token-bound rendition is corrupt and must never degrade to
    # a visually successful file row.
    shared_media_declarations(payload, require_publishable_schema=False)
    # Publishing renders once before a token is issued to enforce the exact
    # response ceiling.  A fixed same-length token keeps that estimate honest.
    media_token = public_token or ("A" * 43)

    def display_time(value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def display_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes / (1024 * 1024):.1f} MB"

    rendered_image_count = 0

    def render_artifacts(items: list[object]) -> str:
        nonlocal rendered_image_count
        cards: list[str] = []
        for artifact in items:
            name = html.escape(str(getattr(artifact, "display_name", "未命名产物")), quote=True)
            family = html.escape(str(getattr(artifact, "family", "artifact")), quote=True)
            mime_type = html.escape(str(getattr(artifact, "mime_type", "application/octet-stream")), quote=True)
            size = display_size(int(getattr(artifact, "size_bytes", 0)))
            created_at = getattr(artifact, "created_at", None)
            timestamp = (
                f'<time datetime="{html.escape(created_at.isoformat(), quote=True)}">'
                f"{display_time(created_at)}</time>"
                if isinstance(created_at, datetime)
                else ""
            )
            preview = getattr(artifact, "preview", None)
            if getattr(artifact, "family", None) == "image" and preview is not None:
                media_id = getattr(preview, "media_id", "")
                if not isinstance(media_id, str) or not _MEDIA_ID.fullmatch(media_id):
                    raise CloudShareConflict("cloud share media identity is invalid")
                media_url = f"/s/{media_token}/media/{media_id}"
                escaped_url = html.escape(media_url, quote=True)
                is_first_shared_image = rendered_image_count == 0
                rendered_image_count += 1
                image_loading = (
                    'loading="eager" fetchpriority="high"'
                    if is_first_shared_image
                    else 'loading="lazy"'
                )
                cards.append(
                    '<figure class="image-artifact">'
                    f'<a class="image-link" href="{escaped_url}" target="_blank" '
                    f'rel="noopener" aria-label="打开完整图片：{name}">'
                    f'<img src="{escaped_url}" alt="{name}" {image_loading} decoding="async">'
                    "</a>"
                    '<figcaption><span class="artifact-name">'
                    f"{name}</span><span>{mime_type} · {size}</span>{timestamp}</figcaption>"
                    "</figure>"
                )
            else:
                cards.append(
                    '<div class="file-artifact"><span class="file-mark" aria-hidden="true">◇</span>'
                    '<span class="file-copy"><strong>'
                    f"{name}</strong><small>{family} · {mime_type} · {size}</small>{timestamp}"
                    "</span></div>"
                )
        if not cards:
            return ""
        return '<section class="artifacts" aria-label="本轮产物">' + "".join(cards) + "</section>"

    artifacts_by_turn: dict[str, list[object]] = {}
    unassociated: list[object] = []
    message_turns = {message.turn_id for message in payload.messages}
    for artifact in payload.artifacts:
        turn_id = getattr(artifact, "turn_id", None)
        if isinstance(turn_id, str) and turn_id in message_turns:
            artifacts_by_turn.setdefault(turn_id, []).append(artifact)
        else:
            unassociated.append(artifact)

    last_by_turn: dict[str, int] = {}
    assistant_by_turn: dict[str, int] = {}
    for index, message in enumerate(payload.messages):
        last_by_turn[message.turn_id] = index
        if message.role == "assistant":
            assistant_by_turn[message.turn_id] = index
    anchor_by_turn = last_by_turn | assistant_by_turn

    message_html: list[str] = []
    for index, message in enumerate(payload.messages):
        is_user = message.role == "user"
        author = "你的指令" if is_user else "e-Mate"
        body = render_share_markdown(message.text)
        created = html.escape(message.created_at.isoformat(), quote=True)
        attached = ""
        if anchor_by_turn.get(message.turn_id) == index:
            attached = render_artifacts(artifacts_by_turn.pop(message.turn_id, []))
        message_html.append(
            f'<article class="message {message.role}"><div class="message-meta">'
            f'<strong>{author}</strong><time datetime="{created}">{display_time(message.created_at)}</time>'
            f'</div><div class="bubble"><div class="markdown-body">{body}</div>{attached}</div></article>'
        )
    for items in artifacts_by_turn.values():
        unassociated.extend(items)
    remaining_artifacts = ""
    if unassociated:
        remaining_artifacts = (
            '<section class="remaining"><h2>任务产物</h2>'
            f"{render_artifacts(unassociated)}</section>"
        )

    title_text = payload.title or "e-Mate 分享会话"
    title = html.escape(title_text, quote=True)
    created_label = display_time(payload.created_at)
    expires_label = display_time(payload.expires_at)
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{title}</title><style>
:root{{--canvas:oklch(0.968 0.006 72);--surface:oklch(0.995 0.003 72);--raised:oklch(0.985 0.006 72);--ink:oklch(0.205 0.018 55);--muted:oklch(0.50 0.018 55);--rule:oklch(0.885 0.012 68);--accent:oklch(0.62 0.14 49);--user:oklch(0.94 0.032 63);--focus:oklch(0.58 0.16 250);--radius-compact:8px;--radius-control:10px;--radius-card:12px;--radius-panel:16px}}
@media(prefers-color-scheme:dark){{:root{{--canvas:oklch(0.16 0.012 55);--surface:oklch(0.205 0.012 55);--raised:oklch(0.235 0.014 55);--ink:oklch(0.93 0.008 72);--muted:oklch(0.70 0.014 72);--rule:oklch(0.31 0.014 55);--accent:oklch(0.74 0.13 56);--user:oklch(0.285 0.035 55);--focus:oklch(0.76 0.13 245)}}}}
*{{box-sizing:border-box}}html{{background:var(--canvas)}}body{{margin:0;color:var(--ink);font:14px/1.58 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}.workspace{{width:min(900px,calc(100% - 24px));min-height:calc(100dvh - 24px);margin:12px auto;background:var(--surface);border:1px solid var(--rule);border-radius:var(--radius-panel);overflow:hidden}}.topbar{{padding:24px 28px 20px;border-bottom:1px solid var(--rule)}}.topbar h1{{margin:0;font-size:20px;line-height:1.35;font-weight:650;letter-spacing:-.01em}}.topbar p,.message-meta,time,figcaption span,.file-copy small,.footer{{color:var(--muted);font-size:12px}}.topbar p{{margin:7px 0 0}}.timeline{{padding:26px 28px 42px}}.message{{display:flex;flex-direction:column;align-items:flex-start;margin:0 0 28px}}.message.user{{align-items:flex-end}}.message-meta{{display:flex;align-items:center;gap:10px;margin:0 4px 7px}}.message-meta strong{{color:var(--ink);font-size:12px;font-weight:620}}.message-meta time{{font-variant-numeric:tabular-nums}}.bubble{{width:min(100%,760px);padding:16px 18px;border:1px solid var(--rule);border-radius:var(--radius-card);background:var(--raised)}}.user .bubble{{width:min(82%,680px);background:var(--user);border-color:color-mix(in oklch,var(--accent) 24%,var(--rule))}}.markdown-body{{min-width:0;overflow-wrap:anywhere}}.markdown-body>:first-child{{margin-top:0}}.markdown-body>:last-child{{margin-bottom:0}}.markdown-body p{{margin:0 0 10px;white-space:normal}}.markdown-body h1,.markdown-body h2,.markdown-body h3,.markdown-body h4,.markdown-body h5,.markdown-body h6{{margin:18px 0 8px;line-height:1.35;font-weight:650}}.markdown-body h1{{font-size:18px}}.markdown-body h2{{font-size:16px}}.markdown-body h3{{font-size:15px}}.markdown-body h4,.markdown-body h5,.markdown-body h6{{font-size:14px}}.markdown-body ul,.markdown-body ol{{margin:8px 0 12px;padding-left:24px}}.markdown-body li+li{{margin-top:4px}}.markdown-body a{{color:var(--focus);text-decoration-thickness:1px;text-underline-offset:2px}}.markdown-body a:focus-visible,.markdown-table-wrap:focus-visible{{outline:2px solid var(--focus);outline-offset:2px}}.markdown-body code,.markdown-body pre{{font-family:ui-monospace,"SFMono-Regular",Consolas,"Liberation Mono",monospace}}.markdown-body :not(pre)>code{{padding:2px 5px;border-radius:var(--radius-compact);background:var(--canvas);font-size:.92em}}.markdown-body pre{{max-width:100%;margin:10px 0 14px;padding:12px 14px;overflow:auto;border:1px solid var(--rule);border-radius:var(--radius-control);background:var(--canvas);font-size:12px;line-height:1.55;white-space:pre}}.markdown-body pre code{{font:inherit}}.markdown-table-wrap{{max-width:100%;margin:10px 0 14px;overflow:auto;border:1px solid var(--rule);border-radius:var(--radius-control)}}.markdown-body table{{width:100%;border-collapse:collapse;background:var(--surface);font-size:13px}}.markdown-body th,.markdown-body td{{min-width:96px;padding:8px 10px;border-right:1px solid var(--rule);border-bottom:1px solid var(--rule);text-align:left;vertical-align:top}}.markdown-body th{{background:var(--raised);font-weight:650}}.markdown-body tr:last-child td{{border-bottom:0}}.markdown-body th:last-child,.markdown-body td:last-child{{border-right:0}}.markdown-body .align-center{{text-align:center}}.markdown-body .align-right{{text-align:right}}.markdown-body .align-left{{text-align:left}}.artifacts{{display:grid;gap:10px;margin-top:16px;padding-top:14px;border-top:1px solid var(--rule)}}.image-artifact{{margin:0;border:1px solid var(--rule);border-radius:var(--radius-card);overflow:hidden;background:var(--surface)}}.image-link{{display:grid;width:100%;max-height:min(72dvh,760px);place-items:center;background:var(--canvas)}}.image-link img{{display:block;width:auto;height:auto;max-width:100%;max-height:min(72dvh,760px);object-fit:contain}}.image-link:focus-visible,.file-artifact:focus-visible{{outline:2px solid var(--focus);outline-offset:2px}}figcaption{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 12px;border-top:1px solid var(--rule)}}figcaption .artifact-name{{margin-right:auto;color:var(--ink);font-size:13px;font-weight:600;overflow-wrap:anywhere}}.file-artifact{{display:flex;align-items:center;gap:12px;padding:11px 12px;border:1px solid var(--rule);border-radius:var(--radius-control);background:var(--surface)}}.file-mark{{display:grid;width:32px;height:32px;flex:0 0 auto;place-items:center;border-radius:var(--radius-compact);background:var(--raised);color:var(--accent);font-size:18px}}.file-copy{{display:flex;min-width:0;flex:1;flex-direction:column}}.file-copy strong{{font-size:13px;overflow-wrap:anywhere}}.file-copy time{{margin-top:2px}}.remaining{{margin-top:36px;padding-top:22px;border-top:1px solid var(--rule)}}.remaining h2{{margin:0 0 12px;font-size:14px}}.remaining>.artifacts{{margin:0;padding:0;border:0}}.footer{{padding:14px 28px;border-top:1px solid var(--rule);background:var(--raised)}}
@media(max-width:640px){{.workspace{{width:100%;min-height:100dvh;margin:0;border-width:0;border-radius:0}}.topbar{{padding:20px 16px 17px}}.timeline{{padding:22px 12px 34px}}.message{{margin-bottom:24px}}.message-meta{{align-items:flex-start;flex-direction:column;gap:0}}.user .bubble,.bubble{{width:100%;padding:14px}}figcaption{{align-items:flex-start;flex-direction:column;gap:2px}}.footer{{padding:14px 16px}}}}
@media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto!important}}}}
.message{{margin-bottom:30px}}.bubble{{width:min(100%,760px);padding:2px 0;border:0;background:transparent}}.user .bubble{{width:min(82%,680px);padding:14px 16px;border:0;border-radius:12px;background:var(--user)}}
@media(max-width:640px){{.bubble,.user .bubble{{width:100%}}}}
</style></head><body><main class="workspace"><header class="topbar"><h1>{title}</h1><p>只读会话分享 · 创建于 {created_label}</p></header>
<section class="timeline" aria-label="会话详情">{''.join(message_html)}{remaining_artifacts}</section>
<footer class="footer">此分享将于 {expires_label} 失效</footer></main></body></html>"""
    encoded = document.encode("utf-8")
    if len(encoded) > 12 * 1024 * 1024:
        raise CloudShareConflict("rendered cloud share exceeds its size limit")
    return encoded
