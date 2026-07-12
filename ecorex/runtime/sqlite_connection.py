"""SQLite connection semantics owned by the EcoreX transaction boundary."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable

from .commit_guard import (
    assert_transaction_commit_guard,
    transaction_commit_guard_active,
)


_LEADING_COMMENTS = re.compile(
    r"\A(?:\s+|--[^\r\n]*(?:\r?\n|\Z)|/\*.*?\*/)*",
    re.DOTALL,
)
_TRANSACTION_CONTROL = frozenset(
    {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}
)


class TransactionSafeConnection(sqlite3.Connection):
    """Keep ``executescript`` inside an already-owned transaction.

    CPython's native ``Connection.executescript`` implicitly commits a pending
    transaction before it evaluates the script.  That behavior breaks the
    Runtime contract when schema preparation and an authoritative state/audit
    mutation intentionally share one transaction.  Outside a transaction the
    native method is retained.  Inside one, complete SQLite statements are
    executed through one cursor and transaction-control statements are rejected
    so a callee cannot silently end its caller's unit of work.
    """

    _ecorex_last_finished_changes: int = 0

    def add_after_commit(self, callback: Callable[[], None]) -> None:
        """Run a non-durable in-process publication only after SQLite commits."""

        if not self.in_transaction:
            raise RuntimeError("after-commit callback requires an active transaction")
        if not callable(callback):
            raise TypeError("after-commit callback must be callable")
        callbacks = getattr(self, "_ecorex_after_commit", None)
        if callbacks is None:
            callbacks = []
            self._ecorex_after_commit = callbacks
        callbacks.append(callback)

    def commit(self) -> None:  # type: ignore[override]
        """Fence every write commit made through the shared Runtime database.

        ``total_changes`` is connection-local and monotonic.  Tracking the
        value observed after the last commit/rollback avoids turning read-only
        snapshot commits into semantic mutations while still covering raw
        repositories that call ``Connection.commit`` directly.
        """

        dirty = self.total_changes != self._ecorex_last_finished_changes
        if self.in_transaction and dirty:
            assert_transaction_commit_guard()
        super().commit()
        self._ecorex_last_finished_changes = self.total_changes
        callbacks = tuple(getattr(self, "_ecorex_after_commit", ()))
        self._ecorex_after_commit = []
        for callback in callbacks:
            callback()

    def rollback(self) -> None:  # type: ignore[override]
        super().rollback()
        self._ecorex_last_finished_changes = self.total_changes
        self._ecorex_after_commit = []

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:  # type: ignore[override]
        if not isinstance(sql_script, str):
            raise TypeError("executescript() argument must be str")
        if not self.in_transaction:
            if transaction_commit_guard_active():
                # Native executescript performs an implicit commit in C and
                # would bypass the Python ``commit`` override.  Own the
                # transaction explicitly whenever an execution epoch exists.
                self.execute("BEGIN IMMEDIATE")
                try:
                    cursor = self.executescript(sql_script)
                    self.commit()
                except BaseException:
                    if self.in_transaction:
                        self.rollback()
                    raise
                return cursor
            return super().executescript(sql_script)

        cursor = self.cursor()
        pending: list[str] = []
        for character in sql_script:
            pending.append(character)
            if character != ";":
                continue
            candidate = "".join(pending)
            if not sqlite3.complete_statement(candidate):
                continue
            self._execute_owned_statement(cursor, candidate)
            pending.clear()

        remainder = "".join(pending)
        if _LEADING_COMMENTS.sub("", remainder).strip():
            # Cursor.execute accepts a final statement without a semicolon, as
            # does the native executescript API.
            self._execute_owned_statement(cursor, remainder)
        return cursor

    @staticmethod
    def _execute_owned_statement(cursor: sqlite3.Cursor, statement: str) -> None:
        executable = _LEADING_COMMENTS.sub("", statement).lstrip()
        if not executable:
            return
        token = executable.split(None, 1)[0].rstrip(";").upper()
        if token in _TRANSACTION_CONTROL:
            raise sqlite3.OperationalError(
                "transaction control is forbidden inside an owned executescript"
            )
        cursor.execute(statement)


__all__ = ["TransactionSafeConnection"]
