"""Durable user permission preference with immutable, verified policy facts."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import re
import sqlite3
import threading
from typing import Iterator, Literal

from ecorex.protocol import PermissionSnapshot

from .database import SQLiteDatabase, json_dumps
from .errors import ConflictError, IdempotencyConflictError


PermissionProfileName = Literal["default", "full_access"]
_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_FACTORY_DEFAULT_UPDATED_AT = datetime(1970, 1, 1, tzinfo=UTC)


class PermissionIntegrityError(RuntimeError):
    """The mutable permission row no longer matches its append-only ledger."""


@dataclass(frozen=True, slots=True)
class VerifiedPermissionSample:
    """One fresh, fully verified permission projection and ledger head."""

    snapshot: PermissionSnapshot
    state_digest: str


@dataclass(frozen=True, slots=True)
class _PermissionCacheKey:
    schema_version: int
    profile: str | None
    revision: int | None
    updated_at: str | None
    state_digest: str | None
    ledger_revision: int | None
    ledger_digest: str | None
    audit_rowid: int | None


class PermissionAuthority:
    """Own the mutable preference while every Turn captures an immutable fact.

    The current row is deliberately small and mutable.  Every actual change is
    also written to an append-only hash chain in the same SQLite transaction.
    Reads verify the complete chain and fail closed instead of silently issuing
    a new trusted snapshot from an edited row.
    """

    def __init__(
        self,
        database: SQLiteDatabase | str | Path,
        *,
        account_id: str,
        initial_full_access: bool,
        admin_hard_denies: frozenset[str] = frozenset(),
        initialize: bool = True,
    ) -> None:
        if not account_id or len(account_id) > 256:
            raise ValueError("permission account_id is invalid")
        normalized_denies = {
            value.strip().casefold() for value in admin_hard_denies if value.strip()
        }
        if any(not _CAPABILITY_ID.fullmatch(value) for value in normalized_denies):
            raise ValueError("administrator hard-deny capability ID is invalid")
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        self.account_id = account_id
        self.admin_hard_denies = frozenset(normalized_denies)
        self.initial_profile: PermissionProfileName = (
            "full_access" if initial_full_access else "default"
        )
        # Covers the authority transaction plus its Runtime snapshot/default-event
        # publication in the API adapter. SQLite still arbitrates other processes.
        self.mutation_lock = threading.RLock()
        self._verified_cache_lock = threading.Lock()
        self._verified_cache: tuple[
            _PermissionCacheKey, VerifiedPermissionSample
        ] | None = None
        self._verified_sample_context: ContextVar[
            VerifiedPermissionSample | None
        ] = ContextVar(
            f"ecorex_permission_verified_sample_{id(self)}",
            default=None,
        )
        if initialize:
            self.initialize()

    def initialize(self) -> PermissionSnapshot:
        """Persist or verify the factory authority during startup convergence.

        Projection-only composition passes ``initialize=False`` to the
        constructor and may call :meth:`current` without creating a trusted
        permission fact.  The healthy startup coordinator calls this method
        explicitly before mutations or Turn admission are enabled.
        """

        now = datetime.now(UTC).isoformat()
        with self.mutation_lock:
            with self.database.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM runtime_permission_state WHERE account_id = ?",
                    (self.account_id,),
                ).fetchone()
                if row is None:
                    self._require_factory_state_absent(connection)
                    digest = self._state_digest(
                        profile=self.initial_profile,
                        revision=1,
                        updated_at=now,
                        previous_digest=None,
                        client_request_id="__initial__",
                    )
                    connection.execute(
                        "INSERT INTO permission_state_ledger("
                        "account_id, revision, profile, previous_digest, state_digest, "
                        "client_request_id, created_at) VALUES (?, 1, ?, NULL, ?, ?, ?)",
                        (
                            self.account_id,
                            self.initial_profile,
                            digest,
                            "__initial__",
                            now,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO runtime_permission_state("
                        "account_id, profile, revision, updated_at, state_digest"
                        ") VALUES (?, ?, 1, ?, ?)",
                        (self.account_id, self.initial_profile, now, digest),
                    )
                profile, revision, updated_at, _digest = self._read_verified_state(
                    connection
                )
        return self._projection(profile, revision, updated_at)

    def converge_startup(self) -> PermissionSnapshot:
        """Alias used by the healthy startup convergence coordinator."""

        return self.initialize()

    def current(self) -> PermissionSnapshot:
        return self._operation_sample().snapshot

    def current_state_digest(self) -> str:
        """Return the verified append-only permission ledger chain head."""

        return self._operation_sample().state_digest

    @contextmanager
    def verified_sample_scope(self) -> Iterator[VerifiedPermissionSample]:
        """Reuse one verified sample inside one synchronous governance call.

        The ContextVar is authority-instance and execution-context local. The
        caller must keep this scope synchronous; Runtime closes it before any
        tool/provider dispatch or await boundary. Nested governance helpers
        reuse the outer sample and the final exit removes it deterministically.
        """

        active = self._verified_sample_context.get()
        if active is not None:
            yield active
            return
        sample = self.verified_sample()
        token = self._verified_sample_context.set(sample)
        try:
            yield sample
        finally:
            self._verified_sample_context.reset(token)

    def verified_sample(self) -> VerifiedPermissionSample:
        """Return a fresh projection backed by a verified immutable chain.

        SQLite triggers make ledger/audit rows append-only and the mutable
        state row ledger-backed. The hot path therefore compares a constant-
        size chain-head fingerprint. A permission/schema change misses this
        cache and re-verifies the complete ledger plus request-audit graph.
        """

        with self.database.reader() as connection:
            cache_key = self._cache_key(connection)
            with self._verified_cache_lock:
                cached = self._verified_cache
            if cached is not None and cached[0] == cache_key:
                return cached[1]
            if cache_key.profile is not None:
                profile, revision, updated_at, digest = self._read_verified_state(
                    connection
                )
            else:
                profile, revision, updated_at, digest = self._project_verified_state(
                    connection
                )
        sample = VerifiedPermissionSample(
            snapshot=self._projection(profile, revision, updated_at),
            state_digest=digest,
        )
        with self._verified_cache_lock:
            self._verified_cache = (cache_key, sample)
        return sample

    def _cache_key(self, connection: sqlite3.Connection) -> _PermissionCacheKey:
        schema_row = connection.execute("PRAGMA schema_version").fetchone()
        head = connection.execute(
            "SELECT state.profile,state.revision,state.updated_at,state.state_digest,"
            "ledger.revision AS ledger_revision,"
            "ledger.state_digest AS ledger_digest,"
            "(SELECT MAX(rowid) FROM permission_change_requests) AS audit_rowid "
            "FROM (SELECT ? AS account_id) AS authority "
            "LEFT JOIN runtime_permission_state AS state "
            "ON state.account_id=authority.account_id "
            "LEFT JOIN permission_state_ledger AS ledger "
            "ON ledger.account_id=authority.account_id "
            "AND ledger.revision=(SELECT MAX(revision) "
            "FROM permission_state_ledger WHERE account_id=authority.account_id)",
            (self.account_id,),
        ).fetchone()
        return _PermissionCacheKey(
            schema_version=int(schema_row[0]),
            profile=None if head["profile"] is None else str(head["profile"]),
            revision=None if head["revision"] is None else int(head["revision"]),
            updated_at=(
                None if head["updated_at"] is None else str(head["updated_at"])
            ),
            state_digest=(
                None
                if head["state_digest"] is None
                else str(head["state_digest"])
            ),
            ledger_revision=(
                None
                if head["ledger_revision"] is None
                else int(head["ledger_revision"])
            ),
            ledger_digest=(
                None
                if head["ledger_digest"] is None
                else str(head["ledger_digest"])
            ),
            audit_rowid=(
                None
                if head["audit_rowid"] is None
                else int(head["audit_rowid"])
            ),
        )

    def _operation_sample(self) -> VerifiedPermissionSample:
        active = self._verified_sample_context.get()
        return active if active is not None else self.verified_sample()

    def _project_verified_state(
        self, connection: sqlite3.Connection
    ) -> tuple[PermissionProfileName, int, str, str]:
        row = connection.execute(
            "SELECT 1 FROM runtime_permission_state WHERE account_id = ?",
            (self.account_id,),
        ).fetchone()
        if row is not None:
            return self._read_verified_state(connection)
        self._require_factory_state_absent(connection)
        updated_at = _FACTORY_DEFAULT_UPDATED_AT.isoformat()
        digest = self._state_digest(
            profile=self.initial_profile,
            revision=1,
            updated_at=updated_at,
            previous_digest=None,
            client_request_id="__initial__",
        )
        return self.initial_profile, 1, updated_at, digest

    def _require_factory_state_absent(self, connection: sqlite3.Connection) -> None:
        ledger = connection.execute(
            "SELECT 1 FROM permission_state_ledger WHERE account_id = ? LIMIT 1",
            (self.account_id,),
        ).fetchone()
        requests = connection.execute(
            "SELECT 1 FROM permission_change_requests WHERE account_id = ? LIMIT 1",
            (self.account_id,),
        ).fetchone()
        if ledger is not None or requests is not None:
            raise PermissionIntegrityError(
                "durable permission authority is incomplete"
            )

    def update(
        self,
        profile: str,
        *,
        expected_revision: int,
        client_request_id: str,
    ) -> PermissionSnapshot:
        # Every mutation path, including non-HTTP callers and tests, shares the
        # same process admission fence as final tool dispatch.  The API may
        # already hold this RLock while publishing projections; re-entry is
        # intentional.
        with self.mutation_lock:
            return self._update_unlocked(
                profile,
                expected_revision=expected_revision,
                client_request_id=client_request_id,
            )

    def update_in_transaction(
        self,
        connection: sqlite3.Connection,
        profile: str,
        *,
        expected_revision: int,
        client_request_id: str,
    ) -> PermissionSnapshot:
        """Mutate authority inside a caller-owned permission publication unit."""

        if not connection.in_transaction:
            raise RuntimeError("permission update requires an active transaction")
        with self.mutation_lock:
            return self._update_unlocked(
                profile,
                expected_revision=expected_revision,
                client_request_id=client_request_id,
                connection=connection,
            )

    def _update_unlocked(
        self,
        profile: str,
        *,
        expected_revision: int,
        client_request_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> PermissionSnapshot:
        if profile not in {"default", "full_access"}:
            raise ValueError("permission profile is invalid")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValueError("permission expected_revision is invalid")
        if expected_revision < 1:
            raise ValueError("permission expected_revision is invalid")
        if not client_request_id or len(client_request_id) > 256:
            raise ValueError("permission client_request_id is invalid")
        typed_profile: PermissionProfileName = profile
        fingerprint = self._request_fingerprint(typed_profile, expected_revision)
        legacy_fingerprint = hashlib.sha256(
            json_dumps({"profile": typed_profile}).encode("utf-8")
        ).hexdigest()
        def mutate(active_connection: sqlite3.Connection) -> PermissionSnapshot:
            connection = active_connection
            current_profile, revision, updated_at, state_digest = (
                self._read_verified_state(connection)
            )
            duplicate = connection.execute(
                "SELECT * FROM permission_change_requests "
                "WHERE account_id = ? AND client_request_id = ?",
                (self.account_id, client_request_id),
            ).fetchone()
            if duplicate is not None:
                self._verify_audit_row(connection, duplicate)
                if duplicate["request_fingerprint"] not in {
                    fingerprint,
                    legacy_fingerprint,
                }:
                    raise IdempotencyConflictError(
                        "permission client_request_id was reused with different content"
                    )
                # Never replay the old outcome over a later revocation/admin policy.
                return self._projection(current_profile, revision, updated_at)
            if expected_revision != revision:
                raise ConflictError(
                    "permission revision changed; refresh settings before retrying"
                )

            now = self._next_timestamp(updated_at)
            if current_profile != typed_profile:
                revision += 1
                previous_digest = state_digest
                state_digest = self._state_digest(
                    profile=typed_profile,
                    revision=revision,
                    updated_at=now,
                    previous_digest=previous_digest,
                    client_request_id=client_request_id,
                )
                connection.execute(
                    "INSERT INTO permission_state_ledger("
                    "account_id, revision, profile, previous_digest, state_digest, "
                    "client_request_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.account_id,
                        revision,
                        typed_profile,
                        previous_digest,
                        state_digest,
                        client_request_id,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE runtime_permission_state "
                    "SET profile = ?, revision = ?, updated_at = ?, state_digest = ? "
                    "WHERE account_id = ?",
                    (typed_profile, revision, now, state_digest, self.account_id),
                )
                current_profile = typed_profile
                updated_at = now
            projection = self._projection(current_profile, revision, updated_at)
            response_json = projection.model_dump_json()
            audit_digest = self._audit_digest(
                account_id=self.account_id,
                client_request_id=client_request_id,
                request_fingerprint=fingerprint,
                response_json=response_json,
                expected_revision=expected_revision,
                resulting_revision=revision,
                state_digest=state_digest,
                created_at=now,
            )
            connection.execute(
                "INSERT INTO permission_change_requests("
                "account_id, client_request_id, request_fingerprint, response_json, "
                "expected_revision, resulting_revision, state_digest, audit_digest, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.account_id,
                    client_request_id,
                    fingerprint,
                    response_json,
                    expected_revision,
                    revision,
                    state_digest,
                    audit_digest,
                    now,
                ),
            )
            return projection

        if connection is not None:
            if not connection.in_transaction:
                raise RuntimeError("permission update requires an active transaction")
            return mutate(connection)
        with self.database.transaction() as owned_connection:
            return mutate(owned_connection)

    def _read_verified_state(
        self, connection: sqlite3.Connection
    ) -> tuple[PermissionProfileName, int, str, str]:
        row = connection.execute(
            "SELECT * FROM runtime_permission_state WHERE account_id = ?",
            (self.account_id,),
        ).fetchone()
        if row is None:
            raise PermissionIntegrityError("durable permission state is missing")
        profile, revision, updated_at = self._validate_state_values(row)
        state_digest = row["state_digest"]
        if not isinstance(state_digest, str) or len(state_digest) != 64:
            raise PermissionIntegrityError("durable permission state digest is invalid")

        ledger = connection.execute(
            "SELECT * FROM permission_state_ledger WHERE account_id = ? "
            "ORDER BY revision",
            (self.account_id,),
        ).fetchall()
        if not ledger:
            raise PermissionIntegrityError("durable permission ledger is missing")
        previous_digest: str | None = None
        previous_revision: int | None = None
        ledger_by_revision: dict[int, sqlite3.Row] = {}
        for event in ledger:
            event_profile, event_revision, event_time = self._validate_state_values(event)
            if previous_revision is not None and event_revision != previous_revision + 1:
                raise PermissionIntegrityError("permission ledger revision sequence is invalid")
            if event["previous_digest"] != previous_digest:
                raise PermissionIntegrityError("permission ledger previous digest is invalid")
            expected = self._state_digest(
                profile=event_profile,
                revision=event_revision,
                updated_at=event_time,
                previous_digest=previous_digest,
                client_request_id=str(event["client_request_id"]),
            )
            if event["state_digest"] != expected:
                raise PermissionIntegrityError("permission ledger digest is invalid")
            previous_digest = expected
            previous_revision = event_revision
            ledger_by_revision[event_revision] = event
        latest = ledger[-1]
        if (
            latest["profile"] != profile
            or int(latest["revision"]) != revision
            or latest["created_at"] != updated_at
            or latest["state_digest"] != state_digest
        ):
            raise PermissionIntegrityError(
                "durable permission state does not match its ledger"
            )
        audit_rows = connection.execute(
            "SELECT * FROM permission_change_requests WHERE account_id = ?",
            (self.account_id,),
        ).fetchall()
        audit_request_ids = {str(row["client_request_id"]) for row in audit_rows}
        for event in ledger:
            request_id = str(event["client_request_id"])
            if not request_id.startswith("__") and request_id not in audit_request_ids:
                raise PermissionIntegrityError(
                    "permission ledger change is missing its request audit"
                )
        for audit_row in audit_rows:
            self._verify_audit_row(
                connection,
                audit_row,
                ledger_by_revision=ledger_by_revision,
            )
        return profile, revision, updated_at, state_digest

    @staticmethod
    def _validate_state_values(
        row: sqlite3.Row,
    ) -> tuple[PermissionProfileName, int, str]:
        profile = row["profile"]
        revision = row["revision"]
        updated_at = row["updated_at"] if "updated_at" in row.keys() else row["created_at"]
        if profile not in {"default", "full_access"}:
            raise PermissionIntegrityError("durable permission profile is invalid")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise PermissionIntegrityError("durable permission revision is invalid")
        try:
            parsed = datetime.fromisoformat(str(updated_at))
        except (TypeError, ValueError) as error:
            raise PermissionIntegrityError("durable permission timestamp is invalid") from error
        if parsed.tzinfo is None:
            raise PermissionIntegrityError("durable permission timestamp is not timezone-aware")
        return profile, revision, parsed.astimezone(UTC).isoformat()

    def _verify_audit_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        ledger_by_revision: dict[int, sqlite3.Row] | None = None,
    ) -> None:
        expected = self._audit_digest(
            account_id=str(row["account_id"]),
            client_request_id=str(row["client_request_id"]),
            request_fingerprint=str(row["request_fingerprint"]),
            response_json=str(row["response_json"]),
            expected_revision=row["expected_revision"],
            resulting_revision=row["resulting_revision"],
            state_digest=row["state_digest"],
            created_at=str(row["created_at"]),
        )
        if row["audit_digest"] != expected:
            raise PermissionIntegrityError("permission request audit digest is invalid")
        # A signed storage migration may seal a legacy audit whose original
        # request did not record revision fields. Runtime startup never creates
        # or repairs that compatibility fact.
        if row["expected_revision"] is None:
            return
        try:
            expected_revision = int(row["expected_revision"])
            resulting_revision = int(row["resulting_revision"])
            response = PermissionSnapshot.model_validate_json(str(row["response_json"]))
        except (TypeError, ValueError) as error:
            raise PermissionIntegrityError(
                "permission request audit payload is invalid"
            ) from error
        if resulting_revision not in {expected_revision, expected_revision + 1}:
            raise PermissionIntegrityError(
                "permission request audit revision transition is invalid"
            )
        if response.revision != resulting_revision:
            raise PermissionIntegrityError(
                "permission request response revision is invalid"
            )
        if row["request_fingerprint"] != self._request_fingerprint(
            response.profile, expected_revision
        ):
            raise PermissionIntegrityError(
                "permission request fingerprint does not match its response"
            )
        event = (
            ledger_by_revision.get(resulting_revision)
            if ledger_by_revision is not None
            else connection.execute(
                "SELECT * FROM permission_state_ledger "
                "WHERE account_id = ? AND revision = ?",
                (self.account_id, resulting_revision),
            ).fetchone()
        )
        if (
            event is None
            or event["state_digest"] != row["state_digest"]
            or event["profile"] != response.profile
            or event["created_at"] != response.updated_at.isoformat()
        ):
            raise PermissionIntegrityError(
                "permission request audit does not match the permission ledger"
            )

    def _projection(
        self, profile: PermissionProfileName, revision: int, updated_at: str
    ) -> PermissionSnapshot:
        return PermissionSnapshot.issue(
            profile=profile,
            revision=revision,
            updated_at=datetime.fromisoformat(updated_at),
            admin_hard_denies=sorted(self.admin_hard_denies),
        )

    @staticmethod
    def _next_timestamp(previous: str) -> str:
        """Return a UTC timestamp strictly newer than the persisted state."""

        previous_time = datetime.fromisoformat(previous).astimezone(UTC)
        now = datetime.now(UTC)
        if now <= previous_time:
            now = previous_time + timedelta(microseconds=1)
        return now.isoformat()

    def _state_digest(
        self,
        *,
        profile: PermissionProfileName,
        revision: int,
        updated_at: str,
        previous_digest: str | None,
        client_request_id: str,
    ) -> str:
        return hashlib.sha256(
            json_dumps(
                {
                    "account_id": self.account_id,
                    "profile": profile,
                    "revision": revision,
                    "updated_at": updated_at,
                    "previous_digest": previous_digest,
                    "client_request_id": client_request_id,
                }
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _request_fingerprint(
        profile: PermissionProfileName, expected_revision: int
    ) -> str:
        return hashlib.sha256(
            json_dumps(
                {"profile": profile, "expected_revision": expected_revision}
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _audit_digest(
        *,
        account_id: str,
        client_request_id: str,
        request_fingerprint: str,
        response_json: str,
        expected_revision: object,
        resulting_revision: object,
        state_digest: object,
        created_at: str,
    ) -> str:
        return hashlib.sha256(
            json_dumps(
                {
                    "account_id": account_id,
                    "client_request_id": client_request_id,
                    "request_fingerprint": request_fingerprint,
                    "response_json": response_json,
                    "expected_revision": expected_revision,
                    "resulting_revision": resulting_revision,
                    "state_digest": state_digest,
                    "created_at": created_at,
                }
            ).encode("utf-8")
        ).hexdigest()
