"""Append-only, hash-chained journal for recoverable install transitions."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping


class InstallState(StrEnum):
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    STAGING = "staging"
    AWAITING_USER = "awaiting_user"
    DRAINING = "draining"
    ACTIVATING = "activating"
    HEALTHCHECKING = "healthchecking"
    COMPLETED = "completed"
    ROLLBACK = "rollback"
    FAILED = "failed"


TERMINAL_STATES = frozenset(
    {InstallState.COMPLETED, InstallState.ROLLBACK, InstallState.FAILED}
)

_ALLOWED_TRANSITIONS: Mapping[InstallState, frozenset[InstallState]] = {
    InstallState.RESOLVING: frozenset(
        {InstallState.DOWNLOADING, InstallState.FAILED}
    ),
    InstallState.DOWNLOADING: frozenset(
        {InstallState.VERIFYING, InstallState.FAILED}
    ),
    # A corrupt mirror is rejected and the coordinator returns to downloading
    # from the next source in the signed priority list.
    InstallState.VERIFYING: frozenset(
        {InstallState.DOWNLOADING, InstallState.STAGING, InstallState.FAILED}
    ),
    InstallState.STAGING: frozenset(
        {InstallState.AWAITING_USER, InstallState.FAILED}
    ),
    InstallState.AWAITING_USER: frozenset(
        {InstallState.DRAINING, InstallState.FAILED}
    ),
    InstallState.DRAINING: frozenset(
        {InstallState.ACTIVATING, InstallState.FAILED}
    ),
    InstallState.ACTIVATING: frozenset(
        {
            InstallState.HEALTHCHECKING,
            InstallState.ROLLBACK,
            InstallState.FAILED,
        }
    ),
    InstallState.HEALTHCHECKING: frozenset(
        {InstallState.COMPLETED, InstallState.ROLLBACK, InstallState.FAILED}
    ),
    # Bootstrap may discover that the confirmed full Runtime could not reach
    # its durable data barrier. This is the only safe post-confirmation rollback
    # window; the activation receipt and signed prior pointers gate the append.
    InstallState.COMPLETED: frozenset({InstallState.ROLLBACK}),
    InstallState.ROLLBACK: frozenset(),
    InstallState.FAILED: frozenset(),
}


class JournalError(RuntimeError):
    pass


class InvalidTransition(JournalError):
    pass


class JournalCorruption(JournalError):
    pass


@dataclass(frozen=True, slots=True)
class JournalEntry:
    sequence: int
    transaction_id: str
    state: InstallState
    event: str
    created_at: str
    details: Mapping[str, Any]
    previous_checksum: str
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "transaction_id": self.transaction_id,
            "state": self.state.value,
            "event": self.event,
            "created_at": self.created_at,
            "details": dict(self.details),
            "previous_checksum": self.previous_checksum,
            "checksum": self.checksum,
        }


class InstallJournal:
    """A durable NDJSON journal.

    ``append`` fsyncs every transition.  Readers tolerate only a final partial
    line (the expected power-loss shape); all other sequence or checksum damage
    fails closed.
    """

    GENESIS_CHECKSUM = "0" * 64

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def entries(self) -> tuple[JournalEntry, ...]:
        if not os.path.lexists(self.path):
            return ()
        _reject_link_or_reparse(self.path)
        try:
            payload = self.path.read_bytes()
        except OSError as exc:
            raise JournalError(f"cannot read install journal {self.path}") from exc

        raw_lines = payload.splitlines(keepends=True)
        parsed: list[JournalEntry] = []
        previous = self.GENESIS_CHECKSUM
        for index, raw_line in enumerate(raw_lines):
            is_final = index == len(raw_lines) - 1
            terminated = raw_line.endswith((b"\n", b"\r"))
            try:
                raw = json.loads(raw_line)
                entry = self._parse_entry(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, JournalCorruption) as exc:
                if is_final and not terminated:
                    break
                if isinstance(exc, JournalCorruption):
                    raise
                raise JournalCorruption(f"invalid JSON at journal line {index + 1}") from exc

            expected_sequence = index + 1
            if entry.sequence != expected_sequence:
                raise JournalCorruption(
                    f"journal sequence mismatch at line {index + 1}: "
                    f"expected {expected_sequence}, got {entry.sequence}"
                )
            if entry.previous_checksum != previous:
                raise JournalCorruption(
                    f"journal checksum chain is broken at sequence {entry.sequence}"
                )
            expected_checksum = _entry_checksum(
                sequence=entry.sequence,
                transaction_id=entry.transaction_id,
                state=entry.state,
                event=entry.event,
                created_at=entry.created_at,
                details=entry.details,
                previous_checksum=entry.previous_checksum,
            )
            if entry.checksum != expected_checksum:
                raise JournalCorruption(
                    f"journal entry checksum is invalid at sequence {entry.sequence}"
                )
            if parsed:
                _validate_transition(parsed[-1], entry.transaction_id, entry.state)
            elif entry.state is not InstallState.RESOLVING:
                raise JournalCorruption("the first journal entry must be resolving")
            parsed.append(entry)
            previous = entry.checksum
        return tuple(parsed)

    def latest(self) -> JournalEntry | None:
        entries = self.entries()
        return entries[-1] if entries else None

    def append(
        self,
        *,
        transaction_id: str,
        state: InstallState,
        event: str,
        details: Mapping[str, Any] | None = None,
    ) -> JournalEntry:
        if not transaction_id or not event:
            raise ValueError("transaction_id and event are required")
        current_entries = self.entries()
        latest = current_entries[-1] if current_entries else None
        if latest:
            _validate_transition(latest, transaction_id, state)
        elif state is not InstallState.RESOLVING:
            raise InvalidTransition("the first journal transition must be resolving")

        sequence = len(current_entries) + 1
        previous_checksum = latest.checksum if latest else self.GENESIS_CHECKSUM
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        safe_details = _json_object(details or {})
        checksum = _entry_checksum(
            sequence=sequence,
            transaction_id=transaction_id,
            state=state,
            event=event,
            created_at=created_at,
            details=safe_details,
            previous_checksum=previous_checksum,
        )
        entry = JournalEntry(
            sequence=sequence,
            transaction_id=transaction_id,
            state=state,
            event=event,
            created_at=created_at,
            details=safe_details,
            previous_checksum=previous_checksum,
            checksum=checksum,
        )
        encoded = (
            json.dumps(
                entry.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(self.path):
            _reject_link_or_reparse(self.path)
        self._repair_partial_tail(valid_entry_count=len(current_entries))
        created = not self.path.exists()
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise JournalError("short write while persisting install journal")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created and os.name != "nt":
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return entry

    def _repair_partial_tail(self, *, valid_entry_count: int) -> None:
        """Make a power-loss tail appendable without discarding valid entries."""

        if not self.path.exists():
            return
        payload = self.path.read_bytes()
        if not payload or payload.endswith((b"\n", b"\r")):
            return
        raw_lines = payload.splitlines()
        if len(raw_lines) == valid_entry_count:
            # The last entry was complete JSON and passed its hash chain, but
            # the final newline was lost. Preserve it and restore the delimiter.
            descriptor = os.open(self.path, os.O_APPEND | os.O_WRONLY)
            try:
                os.write(descriptor, b"\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return
        # ``entries`` accepted only the prefix before an incomplete last line.
        # Remove precisely that tail before writing the next transition.
        last_delimiter = payload.rfind(b"\n")
        valid_size = last_delimiter + 1 if last_delimiter >= 0 else 0
        with self.path.open("r+b") as stream:
            stream.truncate(valid_size)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _parse_entry(raw: Any) -> JournalEntry:
        if not isinstance(raw, Mapping):
            raise JournalCorruption("journal entry must be an object")
        required = {
            "sequence",
            "transaction_id",
            "state",
            "event",
            "created_at",
            "details",
            "previous_checksum",
            "checksum",
        }
        if set(raw) != required:
            raise JournalCorruption("journal entry fields do not match the v1 schema")
        try:
            state = InstallState(raw["state"])
        except (TypeError, ValueError) as exc:
            raise JournalCorruption("journal entry contains an unknown state") from exc
        sequence = raw["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise JournalCorruption("journal sequence must be a positive integer")
        details = raw["details"]
        if not isinstance(details, Mapping):
            raise JournalCorruption("journal details must be an object")
        strings = {
            key: raw[key]
            for key in (
                "transaction_id",
                "event",
                "created_at",
                "previous_checksum",
                "checksum",
            )
        }
        if any(not isinstance(value, str) or not value for value in strings.values()):
            raise JournalCorruption("journal string fields must be non-empty")
        return JournalEntry(
            sequence=sequence,
            transaction_id=strings["transaction_id"],
            state=state,
            event=strings["event"],
            created_at=strings["created_at"],
            details=dict(details),
            previous_checksum=strings["previous_checksum"],
            checksum=strings["checksum"],
        )


def _validate_transition(
    latest: JournalEntry,
    next_transaction_id: str,
    next_state: InstallState,
) -> None:
    if next_transaction_id != latest.transaction_id:
        if latest.state not in TERMINAL_STATES:
            raise InvalidTransition(
                f"transaction {latest.transaction_id!r} is still {latest.state.value}"
            )
        if next_state is not InstallState.RESOLVING:
            raise InvalidTransition("a new transaction must start in resolving")
        return
    if next_state is latest.state:
        # Same-state progress records are useful for mirror attempts and crash
        # recovery and do not alter the state machine.
        return
    if next_state not in _ALLOWED_TRANSITIONS[latest.state]:
        raise InvalidTransition(
            f"invalid install transition {latest.state.value} -> {next_state.value}"
        )


def _entry_checksum(
    *,
    sequence: int,
    transaction_id: str,
    state: InstallState,
    event: str,
    created_at: str,
    details: Mapping[str, Any],
    previous_checksum: str,
) -> str:
    unsigned = {
        "sequence": sequence,
        "transaction_id": transaction_id,
        "state": state.value,
        "event": event,
        "created_at": created_at,
        "details": dict(details),
        "previous_checksum": previous_checksum,
    }
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("journal details must be JSON serializable") from exc
    if not isinstance(decoded, dict):  # defensive; input type is already Mapping
        raise ValueError("journal details must serialize as an object")
    return decoded


def _reject_link_or_reparse(path: Path) -> None:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag):
        raise JournalError(f"install journal cannot be a link or reparse point: {path}")
