"""Crash-safe files and side-by-side slot storage for the v1 updater."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
import ctypes
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .manifest import (
    ManifestError,
    ReleaseArtifact,
    ReleaseManifest,
    portable_path_segment_key,
    validate_portable_path_segment,
)


class StorageError(RuntimeError):
    pass


class UnsafePackage(StorageError):
    pass


MAX_PAYLOAD_SCAN_WORKERS = 16


@dataclass(frozen=True, slots=True)
class SlotPointers:
    current: str | None = None
    previous: str | None = None
    known_good: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "previous": self.previous,
            "known_good": list(self.known_good),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SlotPointers":
        if set(raw) not in ({"current", "previous"}, {"current", "previous", "known_good"}):
            raise StorageError("slot pointer file has invalid fields")
        current = raw["current"]
        previous = raw["previous"]
        if current is not None and not isinstance(current, str):
            raise StorageError("current slot pointer must be a string or null")
        if previous is not None and not isinstance(previous, str):
            raise StorageError("previous slot pointer must be a string or null")
        known_good_raw = raw.get("known_good", ())
        if not isinstance(known_good_raw, (list, tuple)) or any(
            not isinstance(item, str) for item in known_good_raw
        ):
            raise StorageError("known_good slot pointers must be an array of strings")
        if len(known_good_raw) > 3 or len(set(known_good_raw)) != len(known_good_raw):
            raise StorageError("known_good slot pointers are duplicated or exceed retention")
        known_good = tuple(known_good_raw)
        for slot_id in (*known_good, current, previous):
            if slot_id is not None:
                _require_safe_slot_id(slot_id)
        return cls(current=current, previous=previous, known_good=known_good)


class SlotStore:
    """Stores immutable release slots and atomically selects current/previous."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        create_storage: bool = True,
    ) -> None:
        self.root = Path(root)
        if self.root.exists() and _is_link_or_reparse(self.root):
            raise StorageError("slot root cannot be a link or reparse point")
        if create_storage:
            self.root.mkdir(parents=True, exist_ok=True)
        if self.root.exists():
            _require_real_directory(self.root, label="slot root")
        self.slots_dir = self.root / "slots"
        self._pointers_path = self.root / "slot-pointers.json"
        self.current_label = self.root / "current"
        self.previous_label = self.root / "previous"
        if create_storage:
            self.slots_dir.mkdir(parents=True, exist_ok=True)
        if self.slots_dir.exists():
            _require_real_directory(self.slots_dir, label="slots directory")

    def initialize(self) -> None:
        """Prepare slot directories during explicit startup convergence."""

        if self.root.exists() and _is_link_or_reparse(self.root):
            raise StorageError("slot root cannot be a link or reparse point")
        self.root.mkdir(parents=True, exist_ok=True)
        _require_real_directory(self.root, label="slot root")
        self.slots_dir.mkdir(parents=True, exist_ok=True)
        _require_real_directory(self.slots_dir, label="slots directory")

    def converge_startup(self) -> None:
        self.initialize()

    def slot_path(self, slot_id: str) -> Path:
        _require_safe_slot_id(slot_id)
        return self.slots_dir / slot_id

    def cleanup_staging_orphans(
        self,
        *,
        before_remove: Callable[[Path], None] | None = None,
    ) -> tuple[str, ...]:
        """Remove only crash-left staging directories while the product lock is held."""

        removed: list[str] = []
        pattern = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._+\-]{0,239}\.staging-[A-Za-z0-9_\-]+$")
        for path in self.slots_dir.iterdir():
            if not pattern.fullmatch(path.name):
                continue
            if _is_link_or_reparse(path) or not path.is_dir():
                raise StorageError("staging orphan is not a real directory")
            if before_remove is not None:
                before_remove(path)
            _safe_remove_tree(path, parent=self.slots_dir)
            removed.append(path.name)
        return tuple(sorted(removed))

    def marker(self, slot_id: str) -> Mapping[str, Any]:
        return _read_marker(self.slot_path(slot_id))

    def release_manifest(self, slot_id: str) -> ReleaseManifest:
        slot = self.slot_path(slot_id)
        _require_real_directory(slot, label="slot")
        path = slot / "release-manifest.json"
        _require_real_file(path, label="slot release manifest")
        try:
            return ReleaseManifest.from_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise StorageError("slot release manifest is unreadable") from exc

    def pointers(self) -> SlotPointers:
        if not self._pointers_path.exists():
            pointers = SlotPointers()
        else:
            try:
                raw = json.loads(self._pointers_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise StorageError("slot pointer file is unreadable") from exc
            if not isinstance(raw, Mapping):
                raise StorageError("slot pointer file must contain an object")
            pointers = SlotPointers.from_dict(raw)
        return pointers

    def switch_to(self, slot_id: str) -> SlotPointers:
        candidate = self.slot_path(slot_id)
        _require_real_directory(candidate, label="candidate slot")
        _require_real_file(candidate / ".slot.json", label="candidate slot marker")
        _require_real_file(
            candidate / "release-manifest.json", label="candidate release manifest"
        )
        _require_real_file(candidate / ".release-package", label="candidate release package")
        if not candidate.is_dir() or not (candidate / ".slot.json").is_file():
            raise StorageError(f"candidate slot is incomplete: {slot_id!r}")
        prior = self.pointers()
        if prior.current == slot_id:
            return prior
        prior_known_good = tuple(
            dict.fromkeys(
                item for item in (prior.current, *prior.known_good) if item is not None
            )
        )
        self.write_pointers(
            SlotPointers(
                current=slot_id,
                previous=prior.current,
                known_good=prior_known_good[:2],
            )
        )
        return prior

    def mark_known_good(self, slot_id: str, *, keep: int = 3) -> SlotPointers:
        if keep < 1:
            raise ValueError("keep must be positive")
        pointers = self.pointers()
        if pointers.current != slot_id:
            raise StorageError("only the current slot can be marked known-good")
        ordered = tuple(dict.fromkeys((slot_id, *pointers.known_good)))[:keep]
        previous = next((item for item in ordered if item != slot_id), None)
        updated = SlotPointers(current=slot_id, previous=previous, known_good=ordered)
        self.write_pointers(updated)
        return updated

    def write_pointers(self, pointers: SlotPointers) -> None:
        for slot_id in (*pointers.known_good, pointers.current, pointers.previous):
            if slot_id is not None:
                path = self.slot_path(slot_id)
                _require_real_directory(path, label="slot pointer target")
                _require_real_file(path / ".slot.json", label="slot pointer marker")
                _require_real_file(
                    path / "release-manifest.json", label="slot release manifest"
                )
                _require_real_file(path / ".release-package", label="slot release package")
                if not path.is_dir() or not (path / ".slot.json").is_file():
                    raise StorageError(f"cannot point to missing or incomplete slot {slot_id!r}")
        atomic_write_json(self._pointers_path, pointers.to_dict())
        # Labels are operator conveniences, never transaction authority.  A
        # Windows sharing violation must not turn a committed pointer switch
        # into a false FAILED result.  The next successful write/repair heals
        # them from the authoritative JSON record.
        try:
            self._sync_human_labels(pointers)
        except OSError:
            pass

    def repair_human_labels(self) -> None:
        self._sync_human_labels(self.pointers())

    def restore(self, pointers: SlotPointers) -> None:
        self.write_pointers(pointers)

    def discard(self, slot_id: str) -> None:
        path = self.slot_path(slot_id)
        pointers = self.pointers()
        protected = {
            item
            for item in (*pointers.known_good, pointers.current, pointers.previous)
            if item
        }
        if slot_id in protected:
            raise StorageError(f"refusing to discard protected slot {slot_id!r}")
        if path.exists() or _is_link_or_reparse(path):
            _safe_remove_tree(path, parent=self.slots_dir)

    def stage(
        self,
        package_path: Path,
        *,
        slot_id: str,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        max_members: int = 50_000,
        max_unpacked_bytes: int = 2 * 1024 * 1024 * 1024,
        payload_enricher: Callable[[Path], Mapping[str, Any]] | None = None,
        payload_preparer: Callable[[Path, Path], Mapping[str, Any]] | None = None,
        payload_attester: Callable[
            [Path, Path, Mapping[str, Any]], Mapping[str, Any]
        ]
        | None = None,
        payload_cleanup: Callable[[Path, Path, Mapping[str, Any]], None] | None = None,
    ) -> Path:
        target = self.slot_path(slot_id)
        expected_marker = {
            "slot_id": slot_id,
            "release_id": manifest.release_id,
            "version": manifest.version,
            "build_digest": manifest.build_digest,
            "artifact_id": artifact.artifact_id,
            "artifact_sha256": artifact.sha256,
            "channel": manifest.channel.value,
        }
        if target.exists() or _is_link_or_reparse(target):
            _require_real_directory(target, label="existing slot")
            marker = _read_marker(target)
            if all(marker.get(key) == value for key, value in expected_marker.items()):
                self.validate(
                    slot_id=slot_id,
                    package_path=package_path,
                    manifest=manifest,
                    artifact=artifact,
                )
                return target
            raise StorageError(f"slot {slot_id!r} already exists with different content")

        temporary = Path(
            tempfile.mkdtemp(prefix=f".{slot_id}.staging-", dir=self.slots_dir)
        )
        security_preparation: Mapping[str, Any] | None = None
        committed = False
        try:
            payload_root = temporary / "payload"
            payload_root.mkdir()
            if payload_preparer is not None:
                prepared_security = payload_preparer(temporary, payload_root)
                if not isinstance(prepared_security, Mapping) or not prepared_security:
                    raise UnsafePackage("payload security preparer returned no identity")
                security_preparation = dict(prepared_security)
            if zipfile.is_zipfile(package_path):
                _extract_zip_safely(
                    package_path,
                    payload_root,
                    max_members=max_members,
                    max_unpacked_bytes=max_unpacked_bytes,
                )
            else:
                copied = payload_root / artifact.file_name
                shutil.copyfile(package_path, copied)
                if os.name != "nt":
                    os.chmod(copied, 0o644)
                with copied.open("r+b") as stream:
                    os.fsync(stream.fileno())
            expected_payload_digest = _package_payload_digest(package_path, artifact)
            core_payload_digest = _payload_tree_digest(payload_root)
            if core_payload_digest != expected_payload_digest:
                raise UnsafePackage("extracted Core payload does not match the release package")
            supplemental: Mapping[str, Any] | None = None
            if payload_enricher is not None:
                candidate = payload_enricher(payload_root)
                if not isinstance(candidate, Mapping) or not candidate:
                    raise UnsafePackage("payload enricher did not return a Pack-set identity")
                supplemental = dict(candidate)

            receipt_package = temporary / ".release-package"
            shutil.copyfile(package_path, receipt_package)
            if os.name != "nt":
                os.chmod(receipt_package, 0o600)
            with receipt_package.open("r+b") as stream:
                os.fsync(stream.fileno())
            if _sha256_path(receipt_package) != artifact.sha256:
                raise UnsafePackage("retained release package does not match signed SHA-256")
            marker = dict(expected_marker)
            actual_payload_digest = _payload_tree_digest(payload_root)
            if supplemental is None and actual_payload_digest != expected_payload_digest:
                raise UnsafePackage("extracted payload does not match the release package")
            if supplemental is not None:
                marker["core_payload_digest"] = expected_payload_digest
                marker["supplemental"] = supplemental
            if security_preparation is not None:
                if payload_attester is None:
                    raise UnsafePackage("payload security attester is unavailable")
                attested_security = payload_attester(
                    temporary, payload_root, security_preparation
                )
                if not isinstance(attested_security, Mapping) or not attested_security:
                    raise UnsafePackage("payload security attester returned no identity")
                marker["security_provision"] = dict(attested_security)
            marker["payload_digest"] = actual_payload_digest
            marker["created_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            atomic_write_text(
                temporary / "release-manifest.json",
                manifest.to_json(include_signature=True, pretty=True) + "\n",
            )
            atomic_write_json(temporary / ".slot.json", marker)
            _fsync_tree(temporary)
            try:
                _durable_replace(temporary, target, replace_existing=False)
            except FileExistsError:
                marker = _read_marker(target)
                if not all(marker.get(key) == value for key, value in expected_marker.items()):
                    raise StorageError(
                        f"slot {slot_id!r} was concurrently staged with different content"
                    )
            _fsync_directory(self.slots_dir)
            committed = True
            return target
        finally:
            try:
                if (
                    not committed
                    and security_preparation is not None
                    and payload_cleanup is not None
                ):
                    payload_cleanup(
                        temporary, temporary / "payload", security_preparation
                    )
            finally:
                if temporary.exists():
                    _safe_remove_tree(temporary, parent=self.slots_dir)

    def validate(
        self,
        *,
        slot_id: str,
        package_path: Path,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> Path:
        target = self.slot_path(slot_id)
        _require_real_directory(target, label="staged slot")
        marker_path = target / ".slot.json"
        _require_real_file(marker_path, label="staged slot marker")
        marker = _read_marker(target)
        _require_real_file(package_path, label="transaction release package")
        if _sha256_path(package_path) != artifact.sha256:
            raise StorageError("transaction release package SHA-256 is invalid")
        if self.release_manifest(slot_id) != manifest:
            raise StorageError("staged slot release manifest does not match the signed target")
        receipt_package = target / ".release-package"
        _require_real_file(receipt_package, label="retained release package")
        if _sha256_path(receipt_package) != artifact.sha256:
            raise StorageError("retained release package SHA-256 is invalid")
        expected = {
            "slot_id": slot_id,
            "release_id": manifest.release_id,
            "version": manifest.version,
            "build_digest": manifest.build_digest,
            "artifact_id": artifact.artifact_id,
            "artifact_sha256": artifact.sha256,
            "channel": manifest.channel.value,
        }
        if any(marker.get(key) != value for key, value in expected.items()):
            raise StorageError("staged slot marker does not match the signed release")
        _validate_payload_receipt(marker, receipt_package, artifact, target / "payload")
        return target

    def validate_receipt(
        self,
        *,
        slot_id: str,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> Path:
        """Validate a retained slot after its transaction package was cleaned."""

        target = self.slot_path(slot_id)
        _require_real_directory(target, label="retained slot")
        marker = _read_marker(target)
        if self.release_manifest(slot_id) != manifest:
            raise StorageError("retained slot release manifest does not match")
        receipt_package = target / ".release-package"
        _require_real_file(receipt_package, label="retained release package")
        if _sha256_path(receipt_package) != artifact.sha256:
            raise StorageError("retained release package SHA-256 is invalid")
        expected = {
            "slot_id": slot_id,
            "release_id": manifest.release_id,
            "version": manifest.version,
            "build_digest": manifest.build_digest,
            "artifact_id": artifact.artifact_id,
            "artifact_sha256": artifact.sha256,
            "channel": manifest.channel.value,
        }
        if any(marker.get(key) != value for key, value in expected.items()):
            raise StorageError("retained slot marker does not match its signed manifest")
        _validate_payload_receipt(marker, receipt_package, artifact, target / "payload")
        return target

    def prune(
        self,
        *,
        extra_keep: set[str] | None = None,
        max_slots: int = 3,
        before_discard: Callable[[str], None] | None = None,
    ) -> tuple[str, ...]:
        """Prune oldest inactive slots while always retaining current/previous."""

        if max_slots < 2:
            raise ValueError("max_slots must retain at least current and previous")
        pointers = self.pointers()
        protected = {
            item
            for item in (*pointers.known_good, pointers.current, pointers.previous)
            if item
        }
        protected.update(extra_keep or set())
        candidates = [
            path
            for path in self.slots_dir.iterdir()
            if (path.is_dir() or _is_link_or_reparse(path))
            and not path.name.startswith(".")
            and path.name not in protected
        ]
        candidates.sort(key=lambda path: path.lstat().st_mtime, reverse=True)
        retained_budget = max(0, max_slots - len(protected))
        removed: list[str] = []
        for path in candidates[retained_budget:]:
            if before_discard is not None:
                before_discard(path.name)
            _safe_remove_tree(path, parent=self.slots_dir)
            removed.append(path.name)
        return tuple(removed)

    def _sync_human_labels(self, pointers: SlotPointers) -> None:
        # ``slot-pointers.json`` is the one atomic authority.  These two plain
        # labels make the side-by-side layout inspectable by operators.
        for label, slot_id in (
            (self.current_label, pointers.current),
            (self.previous_label, pointers.previous),
        ):
            if slot_id is None:
                try:
                    label.unlink()
                except FileNotFoundError:
                    pass
            else:
                atomic_write_text(label, slot_id + "\n")


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        _durable_replace(temporary, path, replace_existing=True)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _extract_zip_safely(
    package_path: Path,
    destination: Path,
    *,
    max_members: int,
    max_unpacked_bytes: int,
) -> None:
    with zipfile.ZipFile(package_path) as archive:
        members = _validated_zip_members(archive)
        if len(members) > max_members:
            raise UnsafePackage(f"archive contains more than {max_members} members")
        total_size = sum(member.file_size for member in members)
        if total_size > max_unpacked_bytes:
            raise UnsafePackage(
                f"archive expands to {total_size} bytes, above limit {max_unpacked_bytes}"
            )
        destination_resolved = destination.resolve()
        directory_modes: list[tuple[Path, int]] = []
        for member in members:
            normalized = member.filename.replace("\\", "/")
            relative = PurePosixPath(normalized)
            unix_mode = _zip_unix_mode(member)
            output = destination.joinpath(*relative.parts)
            if os.name == "nt" and len(str(output.resolve(strict=False))) >= 248:
                raise UnsafePackage(
                    f"archive member exceeds the safe Windows path limit: {member.filename!r}"
                )
            try:
                output.resolve().relative_to(destination_resolved)
            except ValueError as exc:
                raise UnsafePackage(f"archive member escapes destination: {member.filename!r}") from exc
            if member.is_dir():
                output.mkdir(parents=True, exist_ok=True)
                directory_modes.append((output, _sanitized_mode(unix_mode, directory=True)))
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source, output.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            if os.name != "nt":
                os.chmod(output, _sanitized_mode(unix_mode, directory=False))
        if os.name != "nt":
            # Apply explicit directory modes only after all children exist.
            for output, mode in reversed(directory_modes):
                os.chmod(output, mode)


def _validated_zip_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    seen: dict[str, str] = {}
    file_keys: set[str] = set()
    for member in members:
        normalized = member.filename.replace("\\", "/")
        relative = PurePosixPath(normalized)
        unix_mode = _zip_unix_mode(member)
        if (
            not normalized
            or relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or stat.S_ISLNK(unix_mode)
        ):
            raise UnsafePackage(f"archive member is unsafe: {member.filename!r}")
        try:
            for part in relative.parts:
                validate_portable_path_segment(part, label="archive member")
        except ManifestError as exc:
            raise UnsafePackage(f"archive member is unsafe: {member.filename!r}") from exc
        key_parts = [portable_path_segment_key(part) for part in relative.parts]
        key = "/".join(key_parts)
        if key in seen:
            raise UnsafePackage(
                f"archive contains a cross-platform path collision: {member.filename!r}"
            )
        for index in range(1, len(key_parts)):
            if "/".join(key_parts[:index]) in file_keys:
                raise UnsafePackage(
                    f"archive member has a file as its parent: {member.filename!r}"
                )
        if not member.is_dir():
            prefix = key + "/"
            if any(existing.startswith(prefix) for existing in seen):
                raise UnsafePackage(
                    f"archive member conflicts with a directory: {member.filename!r}"
                )
            file_keys.add(key)
        seen[key] = member.filename
    return members


def _sanitized_mode(unix_mode: int, *, directory: bool) -> int:
    permissions = stat.S_IMODE(unix_mode)
    if not permissions:
        permissions = 0o755 if directory else 0o644
    permissions |= 0o700 if directory else 0o600
    # Never restore setuid/setgid/sticky bits from a release archive.
    return permissions & 0o777


def _zip_unix_mode(member: zipfile.ZipInfo) -> int:
    return member.external_attr >> 16 if member.create_system == 3 else 0


def _package_payload_digest(package_path: Path, artifact: ReleaseArtifact) -> str:
    records: list[tuple[str, int, str]] = []
    if zipfile.is_zipfile(package_path):
        with zipfile.ZipFile(package_path) as archive:
            for member in _validated_zip_members(archive):
                if member.is_dir():
                    continue
                relative = PurePosixPath(member.filename.replace("\\", "/")).as_posix()
                digest = hashlib.sha256()
                with archive.open(member, "r") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                mode = _sanitized_mode(_zip_unix_mode(member), directory=False)
                records.append((relative, mode if os.name != "nt" else 0, digest.hexdigest()))
    else:
        records.append(
            (
                artifact.file_name,
                0o644 if os.name != "nt" else 0,
                _sha256_path(package_path),
            )
        )
    return _records_digest(records)


def _payload_tree_digest(root: Path) -> str:
    return _records_digest(_payload_tree_records(root))


def _validate_payload_receipt(
    marker: Mapping[str, Any],
    receipt_package: Path,
    artifact: ReleaseArtifact,
    payload_root: Path,
) -> None:
    """Validate either a Core-only or Core+signed-supplemental payload."""

    expected_core = _package_payload_digest(receipt_package, artifact)
    records = _payload_tree_records(payload_root)
    actual = _records_digest(records)
    supplemental = marker.get("supplemental")
    if supplemental is None:
        if "core_payload_digest" in marker:
            raise StorageError("Core-only slot has an unexpected supplemental digest")
        if marker.get("payload_digest") != expected_core or actual != expected_core:
            raise StorageError(
                "retained slot payload was modified and no longer matches its receipt"
            )
        return
    if not isinstance(supplemental, Mapping) or not supplemental:
        raise StorageError("supplemental slot identity is invalid")
    if marker.get("core_payload_digest") != expected_core:
        raise StorageError("supplemental slot Core digest is invalid")
    # ``payload_digest`` is a crash/tamper receipt, but the marker that stores
    # it is local metadata rather than signed release material. Reconstruct
    # the Core sub-tree independently of the fixed Pack projection and bind it
    # back to the retained, signed Core archive on every verification.
    actual_core = _records_digest(
        [
            record
            for record in records
            if PurePosixPath(record[0]).parts[0] != "capability-packs"
        ]
    )
    if actual_core != expected_core:
        raise StorageError("supplemental slot Core payload was modified")
    recorded = marker.get("payload_digest")
    if not isinstance(recorded, str) or recorded != actual:
        raise StorageError("supplemental slot payload was modified after staging")


def _payload_tree_digest_excluding_top_level(
    root: Path,
    excluded: frozenset[str],
) -> str:
    records = [
        record
        for record in _payload_tree_records(root)
        if PurePosixPath(record[0]).parts[0] not in excluded
    ]
    return _records_digest(records)


def _payload_tree_records(root: Path) -> list[tuple[str, int, str]]:
    _require_real_directory(root, label="slot payload")
    files = _walk_regular_files(root)
    if not files:
        return []
    workers = min(MAX_PAYLOAD_SCAN_WORKERS, len(files))
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="ecorex-slot-verify",
    ) as executor:
        records = list(
            executor.map(
                _payload_file_record,
                ((root, path) for path in files),
            )
        )
    records.sort(key=lambda item: item[0])
    return records


def _payload_file_record(candidate: tuple[Path, Path]) -> tuple[str, int, str]:
    root, path = candidate
    try:
        before = path.lstat()
        if _metadata_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise StorageError(f"slot payload contains an unsafe file: {path}")
        digest = hashlib.sha256()
        observed_size = 0
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            while chunk := stream.read(1024 * 1024):
                observed_size += len(chunk)
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(f"slot payload cannot be verified: {path}") from exc
    identity = _stat_identity(before)
    path_identity = _path_identity(before)
    if (
        _stat_identity(opened) != identity
        or _stat_identity(after) != identity
        or _path_identity(current) != path_identity
        or observed_size != before.st_size
    ):
        raise StorageError(f"slot payload changed while being verified: {path}")
    mode = stat.S_IMODE(before.st_mode) if os.name != "nt" else 0
    return path.relative_to(root).as_posix(), mode, digest.hexdigest()


def _walk_regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise StorageError(f"cannot inspect slot payload directory: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            if _is_link_or_reparse(path):
                raise StorageError(
                    f"slot payload contains a link or reparse point: {path}"
                )
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                files.append(path)
            else:
                raise StorageError(f"slot payload contains a special file: {path}")
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _records_digest(records: list[tuple[str, int, str]]) -> str:
    digest = hashlib.sha256()
    for relative, mode, content_digest in sorted(records, key=lambda item: item[0]):
        digest.update(f"F\0{relative}\0{mode:o}\0{content_digest}\n".encode("utf-8"))
    return digest.hexdigest()


def _sha256_path(path: Path) -> str:
    before = path.lstat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise StorageError(f"slot payload changed while being opened: {path}")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (
        after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise StorageError(f"slot payload changed while being verified: {path}")
    return digest.hexdigest()


def _read_marker(slot: Path) -> dict[str, Any]:
    _require_real_directory(slot, label="slot")
    _require_real_file(slot / ".slot.json", label="slot marker")
    try:
        raw = json.loads((slot / ".slot.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StorageError(f"slot marker is unreadable: {slot}") from exc
    if not isinstance(raw, dict):
        raise StorageError(f"slot marker must contain an object: {slot}")
    return raw


def _require_safe_slot_id(slot_id: str) -> None:
    if (
        not slot_id
        or len(slot_id) > 240
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-+" for character in slot_id)
        or slot_id in {".", ".."}
    ):
        raise StorageError(f"unsafe slot id: {slot_id!r}")


def _safe_remove_tree(path: Path, *, parent: Path) -> None:
    if _is_link_or_reparse(path):
        # Delete the alias itself; never resolve it into a protected sibling or
        # an external location.
        try:
            path.unlink()
        except (IsADirectoryError, PermissionError):
            os.rmdir(path)
        _fsync_directory(parent)
        return
    resolved = path.resolve()
    parent_resolved = parent.resolve()
    try:
        relative = resolved.relative_to(parent_resolved)
    except ValueError as exc:
        raise StorageError(f"refusing to remove path outside slot root: {path}") from exc
    if not relative.parts or resolved == parent_resolved:
        raise StorageError(f"refusing to remove slot root: {path}")
    shutil.rmtree(resolved)
    _fsync_directory(parent)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return _metadata_is_link_or_reparse(metadata)


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _path_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        *_stat_identity(metadata),
        metadata.st_mode,
        int(getattr(metadata, "st_file_attributes", 0)),
    )


def _require_real_directory(path: Path, *, label: str) -> None:
    if _is_link_or_reparse(path) or not path.is_dir():
        raise StorageError(f"{label} must be a real directory: {path}")


def ensure_real_directory(path: Path, *, label: str) -> None:
    """Public update-domain guard for directories used as trust boundaries."""

    _require_real_directory(path, label=label)


def _require_real_file(path: Path, *, label: str) -> None:
    if _is_link_or_reparse(path) or not path.is_file():
        raise StorageError(f"{label} must be a regular file: {path}")


def _fsync_directory(path: Path) -> None:
    # Windows does not provide a portable directory fsync.  File-level fsync +
    # os.replace still gives the strongest stdlib guarantee available there.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, destination: Path, *, replace_existing: bool) -> None:
    """Atomically rename and request metadata durability on Windows."""

    if os.name != "nt":
        if replace_existing:
            os.replace(source, destination)
        else:
            source.replace(destination)
        return
    flags = 0x8 | (0x1 if replace_existing else 0)  # WRITE_THROUGH | REPLACE_EXISTING
    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
    move.restype = ctypes.c_int
    if move(str(source.resolve(strict=True)), str(destination.absolute()), flags):
        return
    error = ctypes.get_last_error()
    if not replace_existing and error in {80, 183}:
        raise FileExistsError(error, "destination already exists", str(destination))
    raise OSError(error, "durable Windows rename failed", str(destination))


def _fsync_tree(root: Path) -> None:
    if os.name == "nt":
        return
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(root)
