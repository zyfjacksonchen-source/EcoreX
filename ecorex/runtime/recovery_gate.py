"""Independent execution authority for the two local recovery mutations.

The Runtime invariant gate deliberately fail-closes all business mutation.
Revoking a managed session and activating an already verified local update are
different authorities: they reduce exposure or repair the running Runtime.
This gate is process-local, has a fixed scope set, and never derives health
from the business Runtime gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import re
import secrets
import threading
from typing import Literal


RecoveryExecutionScope = Literal["session_logout", "session_password", "update_activate"]
RECOVERY_EXECUTION_SCOPES: frozenset[str] = frozenset(
    {"session_logout", "session_password", "update_activate"}
)
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_:-]{1,160}$")


class RecoveryExecutionDenied(RuntimeError):
    """The local recovery lane is closed or a permit is invalid."""


@dataclass(frozen=True, slots=True)
class RecoveryExecutionPermit:
    gate_id: str
    epoch: int
    scope: RecoveryExecutionScope
    subject: str
    nonce: str
    signature: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RecoveryExecutionGateSnapshot:
    status: Literal["open", "closed"]
    epoch: int
    error_code: str | None

    @property
    def open(self) -> bool:
        return self.status == "open"


class RecoveryExecutionGate:
    """Issue short-lived, scope-bound recovery permits.

    No admission lock is exposed. Async callers capture a permit immediately
    before dispatch, assert the same permit after awaiting, and install it as a
    transaction commit guard. ``request_close`` is therefore non-blocking and
    can invalidate a commit that is already between ``BEGIN`` and ``COMMIT``.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._closure_requested = threading.Event()
        self._gate_id = secrets.token_hex(16)
        self._secret = secrets.token_bytes(32)
        self._epoch = 1
        self._error_code: str | None = None

    def snapshot(self) -> RecoveryExecutionGateSnapshot:
        with self._lock:
            closed = self._closure_requested.is_set()
            return RecoveryExecutionGateSnapshot(
                status="closed" if closed else "open",
                epoch=self._epoch,
                error_code=self._error_code,
            )

    def issue_permit(
        self,
        *,
        scope: RecoveryExecutionScope,
        subject: str,
    ) -> RecoveryExecutionPermit:
        if scope not in RECOVERY_EXECUTION_SCOPES:
            raise ValueError("recovery execution scope is not allowed")
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise ValueError("recovery execution subject is invalid")
        with self._lock:
            if self._closure_requested.is_set():
                raise RecoveryExecutionDenied("recovery execution lane is closed")
            nonce = secrets.token_hex(16)
            signature = self._sign(
                epoch=self._epoch,
                scope=scope,
                subject=subject,
                nonce=nonce,
            )
            return RecoveryExecutionPermit(
                gate_id=self._gate_id,
                epoch=self._epoch,
                scope=scope,
                subject=subject,
                nonce=nonce,
                signature=signature,
            )

    def assert_permit(self, permit: RecoveryExecutionPermit) -> None:
        if not isinstance(permit, RecoveryExecutionPermit):
            raise RecoveryExecutionDenied("recovery execution permit is invalid")
        with self._lock:
            if self._closure_requested.is_set():
                raise RecoveryExecutionDenied("recovery execution lane is closed")
            if permit.gate_id != self._gate_id or permit.epoch != self._epoch:
                raise RecoveryExecutionDenied("recovery execution permit is stale")
            expected = self._sign(
                epoch=permit.epoch,
                scope=permit.scope,
                subject=permit.subject,
                nonce=permit.nonce,
            )
            if not hmac.compare_digest(permit.signature, expected):
                raise RecoveryExecutionDenied("recovery execution permit is invalid")

    def request_close(self, *, error_code: str) -> None:
        normalized = str(error_code or "").strip().casefold()
        if _SAFE_ERROR_CODE.fullmatch(normalized) is None:
            raise ValueError("recovery execution error code is invalid")
        # Publish closure before waiting for the metadata lock. A transaction
        # pre-commit assertion can observe it immediately.
        self._closure_requested.set()
        with self._lock:
            if self._error_code is None:
                self._error_code = normalized
                self._epoch += 1

    def _sign(
        self,
        *,
        epoch: int,
        scope: str,
        subject: str,
        nonce: str,
    ) -> str:
        payload = "\0".join(
            (self._gate_id, str(epoch), scope, subject, nonce)
        ).encode("utf-8")
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()


__all__ = [
    "RECOVERY_EXECUTION_SCOPES",
    "RecoveryExecutionDenied",
    "RecoveryExecutionGate",
    "RecoveryExecutionGateSnapshot",
    "RecoveryExecutionPermit",
    "RecoveryExecutionScope",
]
