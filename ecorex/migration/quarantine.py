"""Crash-recoverable removal of the encrypted v0.3 credential quarantine.

The migration intentionally never activates legacy provider keys.  This
service gives the authenticated local user a narrow deletion boundary without
ever decrypting, returning, or accepting a filesystem path.  It does not claim
physical secure erasure from SSD/filesystem history; it removes the product's
encrypted backup and leaves a non-sensitive receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Literal, Mapping

from ecorex.runtime.commit_guard import assert_current_mutation_guard

from .errors import MigrationError, QuarantineStateError
from .migrator import QUARANTINE_NAME, REPORT_NAME
from .path_security import secure_directory, stable_read_bytes, stable_sha256_file


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{7,255}$")
_MAX_METADATA_BYTES = 8 * 1024 * 1024
_MAX_QUARANTINE_BYTES = 32 * 1024 * 1024
_RECEIPT_NAME = "quarantine/legacy-secrets.deleted.json"
_DELETING_NAME = "quarantine/legacy-secrets.aesgcm.deleting"


def _time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("quarantine timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = stable_read_bytes(
            path,
            label=label,
            maximum=_MAX_METADATA_BYTES,
        )
        if not raw:
            raise QuarantineStateError(f"{label} has an invalid size")
        value = json.loads(raw)
    except QuarantineStateError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, MigrationError):
        raise QuarantineStateError(f"{label} is unreadable") from None
    if not isinstance(value, dict):
        raise QuarantineStateError(f"{label} is invalid")
    return value


def _present(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except (OSError, MigrationError):
        raise QuarantineStateError("credential quarantine path is unreadable") from None


def _sha256_file(path: Path) -> str:
    try:
        digest, identity = stable_sha256_file(
            path,
            label="credential quarantine",
            maximum=_MAX_QUARANTINE_BYTES,
        )
        if identity.size < 1:
            raise QuarantineStateError("credential quarantine has an invalid size")
        return digest
    except QuarantineStateError:
        raise
    except OSError:
        raise QuarantineStateError("credential quarantine is unreadable") from None


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise QuarantineStateError("credential quarantine directory is unsafe")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        raise QuarantineStateError("credential quarantine receipt could not be written") from None
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class QuarantineProjection:
    status: Literal["absent", "available", "deleted"]
    entry_count: int
    can_delete: bool
    deleted_at: str | None = None
    items: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "entry_count": self.entry_count,
            "can_delete": self.can_delete,
            "deleted_at": self.deleted_at,
            "items": [dict(item) for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class _MigrationIdentity:
    source_inventory_digest: str
    entry_count: int
    summary: tuple[Mapping[str, Any], ...]


class MigrationQuarantineService:
    """Inspect and remove only the fixed quarantine below one migrated root."""

    def __init__(
        self,
        target_root: str | Path,
        *,
        clock=lambda: datetime.now(UTC),
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        try:
            root = secure_directory(target_root, label="migration target root")
        except MigrationError:
            raise ValueError("migration target root must be a regular directory")
        self.root = root
        self.report_path = root / REPORT_NAME
        self.quarantine_path = root / QUARANTINE_NAME
        self.deleting_path = root / _DELETING_NAME
        self.receipt_path = root / _RECEIPT_NAME
        self.clock = clock
        self.fault_hook = fault_hook or (lambda _phase: None)
        self._lock = threading.Lock()

    def _identity(self) -> _MigrationIdentity | None:
        if not _present(self.report_path):
            if any(
                _present(candidate)
                for candidate in (self.quarantine_path, self.deleting_path, self.receipt_path)
            ):
                raise QuarantineStateError("credential quarantine has no migration report")
            return None
        report = _read_json(self.report_path, label="migration report")
        quarantine = report.get("quarantine")
        digest = report.get("source_inventory_digest")
        count = quarantine.get("entry_count") if isinstance(quarantine, dict) else None
        raw_summary = quarantine.get("summary") if isinstance(quarantine, dict) else None
        if (
            report.get("status") != "completed"
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
        ):
            raise QuarantineStateError("migration report quarantine identity is invalid")
        summary: list[dict[str, Any]] = []
        if raw_summary is not None:
            if not isinstance(raw_summary, list) or len(raw_summary) > 64:
                raise QuarantineStateError("migration report quarantine summary is invalid")
            total = 0
            for item in raw_summary:
                if not isinstance(item, dict) or set(item) != {"kind", "origin", "count"}:
                    raise QuarantineStateError("migration report quarantine summary is invalid")
                kind = item.get("kind")
                origin = item.get("origin")
                item_count = item.get("count")
                if (
                    kind not in {
                        "api_key",
                        "refresh_token",
                        "access_token",
                        "password",
                        "cryptographic_key",
                        "client_secret",
                        "credential",
                    }
                    or origin not in {
                        "product_configuration",
                        "mcp_configuration",
                        "skill_configuration",
                        "permission_configuration",
                    }
                    or isinstance(item_count, bool)
                    or not isinstance(item_count, int)
                    or item_count < 1
                ):
                    raise QuarantineStateError("migration report quarantine summary is invalid")
                summary.append({"kind": kind, "origin": origin, "count": item_count})
                total += item_count
            if total != count:
                raise QuarantineStateError("migration report quarantine summary count is invalid")
        elif count:
            # Import-layout v2 reports created before the summary field remain
            # deletable without exposing guessed provider names.
            summary.append(
                {
                    "kind": "credential",
                    "origin": "product_configuration",
                    "count": count,
                }
            )
        return _MigrationIdentity(digest, count, tuple(summary))

    def _receipt(self, identity: _MigrationIdentity) -> dict[str, Any] | None:
        if not _present(self.receipt_path):
            return None
        receipt = _read_json(self.receipt_path, label="quarantine deletion receipt")
        if (
            set(receipt)
            != {
                "schema_version",
                "state",
                "source_inventory_digest",
                "quarantine_sha256",
                "client_request_id",
                "started_at",
                "deleted_at",
            }
            or receipt.get("schema_version") != 1
            or receipt.get("state") not in {"deleting", "deleted"}
            or receipt.get("source_inventory_digest") != identity.source_inventory_digest
            or not isinstance(receipt.get("quarantine_sha256"), str)
            or _DIGEST.fullmatch(receipt["quarantine_sha256"]) is None
            or not isinstance(receipt.get("client_request_id"), str)
            or _REQUEST_ID.fullmatch(receipt["client_request_id"]) is None
            or not isinstance(receipt.get("started_at"), str)
            or (
                receipt["state"] == "deleted"
                and not isinstance(receipt.get("deleted_at"), str)
            )
            or (receipt["state"] == "deleting" and receipt.get("deleted_at") is not None)
        ):
            raise QuarantineStateError("quarantine deletion receipt is invalid")
        return receipt

    def status(self) -> QuarantineProjection:
        with self._lock:
            return self._status_locked()

    def verified_digest(self) -> str | None:
        """Return the original encrypted backup digest after state verification.

        An absent backup is accepted only when the product deletion flow first
        persisted a matching ``deleting``/``deleted`` receipt.  The returned
        digest therefore remains stable after an authorised deletion and can be
        bound by the immutable migration completion authority.
        """

        _projection, digest = self.verified_authority()
        return digest

    def verified_authority(self) -> tuple[QuarantineProjection, str | None]:
        """Return status and original digest under one filesystem snapshot."""

        with self._lock:
            return self._authority_locked()

    def _status_locked(self) -> QuarantineProjection:
        return self._authority_locked()[0]

    def _authority_locked(self) -> tuple[QuarantineProjection, str | None]:
        identity = self._identity()
        if identity is None or identity.entry_count == 0:
            if any(
                _present(candidate)
                for candidate in (self.quarantine_path, self.deleting_path, self.receipt_path)
            ):
                raise QuarantineStateError("unexpected credential quarantine state")
            return QuarantineProjection("absent", 0, False), None
        if _present(self.quarantine_path) and _present(self.deleting_path):
            raise QuarantineStateError("credential quarantine has ambiguous deletion state")
        receipt = self._receipt(identity)
        content_path = (
            self.quarantine_path
            if _present(self.quarantine_path)
            else self.deleting_path if _present(self.deleting_path) else None
        )
        if content_path is not None:
            digest = _sha256_file(content_path)
            if receipt is not None and receipt["quarantine_sha256"] != digest:
                raise QuarantineStateError("credential quarantine differs from deletion receipt")
            if receipt is not None and receipt["state"] == "deleted":
                raise QuarantineStateError("deleted quarantine unexpectedly reappeared")
            return (
                QuarantineProjection(
                    "available",
                    identity.entry_count,
                    True,
                    items=identity.summary,
                ),
                digest,
            )
        if receipt is not None and receipt["state"] == "deleting":
            return (
                QuarantineProjection(
                    "available",
                    identity.entry_count,
                    True,
                    items=identity.summary,
                ),
                str(receipt["quarantine_sha256"]),
            )
        if receipt is None or receipt["state"] != "deleted":
            raise QuarantineStateError("credential quarantine disappeared without a deletion receipt")
        return (
            QuarantineProjection(
                "deleted",
                identity.entry_count,
                False,
                deleted_at=str(receipt["deleted_at"]),
                items=identity.summary,
            ),
            str(receipt["quarantine_sha256"]),
        )

    def delete(self, *, confirmed: bool, client_request_id: str) -> QuarantineProjection:
        if confirmed is not True:
            raise ValueError("credential quarantine deletion requires confirmation")
        request_id = str(client_request_id or "").strip()
        if _REQUEST_ID.fullmatch(request_id) is None:
            raise ValueError("credential quarantine client request ID is invalid")
        with self._lock:
            current = self._status_locked()
            if current.status in {"absent", "deleted"}:
                return current
            identity = self._identity()
            assert identity is not None
            receipt = self._receipt(identity)
            source = (
                self.quarantine_path
                if _present(self.quarantine_path)
                else self.deleting_path if _present(self.deleting_path) else None
            )
            if source is None:
                if receipt is None or receipt["state"] != "deleting":
                    raise QuarantineStateError("credential quarantine is unavailable")
                deleted_at = _time(self.clock())
                completed = {**receipt, "state": "deleted", "deleted_at": deleted_at}
                self.fault_hook("before_delete_completed")
                assert_current_mutation_guard()
                _atomic_json(self.receipt_path, completed)
                return QuarantineProjection(
                    "deleted",
                    identity.entry_count,
                    False,
                    deleted_at=deleted_at,
                    items=identity.summary,
                )
            digest = _sha256_file(source)
            if receipt is None:
                started_at = _time(self.clock())
                receipt = {
                    "schema_version": 1,
                    "state": "deleting",
                    "source_inventory_digest": identity.source_inventory_digest,
                    "quarantine_sha256": digest,
                    "client_request_id": request_id,
                    "started_at": started_at,
                    "deleted_at": None,
                }
                self.fault_hook("before_delete_intent")
                assert_current_mutation_guard()
                _atomic_json(self.receipt_path, receipt)
                self.fault_hook("after_delete_intent")
            elif receipt["quarantine_sha256"] != digest:
                raise QuarantineStateError("credential quarantine changed during deletion")
            if source == self.quarantine_path:
                self.fault_hook("before_quarantine_staged")
                assert_current_mutation_guard()
                try:
                    os.replace(self.quarantine_path, self.deleting_path)
                except OSError:
                    raise QuarantineStateError("credential quarantine could not be staged for deletion") from None
                self.fault_hook("after_quarantine_staged")
            self.fault_hook("before_quarantine_unlinked")
            assert_current_mutation_guard()
            try:
                self.deleting_path.unlink()
            except OSError:
                raise QuarantineStateError("credential quarantine could not be deleted") from None
            self.fault_hook("after_quarantine_unlinked")
            deleted_at = _time(self.clock())
            completed = {**receipt, "state": "deleted", "deleted_at": deleted_at}
            self.fault_hook("before_delete_completed")
            assert_current_mutation_guard()
            _atomic_json(self.receipt_path, completed)
            return QuarantineProjection(
                "deleted",
                identity.entry_count,
                False,
                deleted_at=deleted_at,
                items=identity.summary,
            )


__all__ = ["MigrationQuarantineService", "QuarantineProjection"]
