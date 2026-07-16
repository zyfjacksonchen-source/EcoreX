"""Managed account/session authority backed by a signed cloud lease."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import base64
from datetime import UTC, datetime
import json
from pathlib import Path

from ecorex.connectors.vault import CredentialVault
from ecorex.runtime.database import SQLiteDatabase

from .models import (
    LeaseSignatureError,
    LeaseValidationError,
    ManagedSessionSnapshot,
    SessionConflict,
    SessionLogoutReceipt,
    SessionRecoveryReport,
    SessionRefreshContext,
    SessionRestartRequired,
    SessionUnavailable,
    SessionVaultError,
    SignedManagedSessionLease,
    StaleSessionRequest,
)
from .repository import (
    ActiveSessionRecord,
    ManagedSessionRepository,
    SessionInstallIntent,
    client_request_hash,
    install_request_fingerprint,
)
from .verification import SessionLeaseVerifier, require_verified


Clock = Callable[[], datetime]
PhaseHook = Callable[[str, str], None]


class ManagedSessionService:
    """Install, validate and revoke cloud-managed session credentials.

    SQLite contains the signed lease, digests and a durable two-phase journal.
    Plaintext access/refresh tokens are written only to ``CredentialVault``.
    """

    def __init__(
        self,
        database: SQLiteDatabase | str | Path,
        *,
        vault: CredentialVault,
        verifier: SessionLeaseVerifier,
        clock: Clock | None = None,
        phase_hook: PhaseHook | None = None,
        initialize: bool = True,
    ) -> None:
        if vault is None or verifier is None:
            raise ValueError("managed session vault and verifier are required")
        self.repository = ManagedSessionRepository(database, initialize=initialize)
        self.vault = vault
        self.verifier = verifier
        self.clock = clock or (lambda: datetime.now(UTC))
        self.phase_hook = phase_hook or (lambda _phase, _identity: None)
        self._runtime_binding: tuple[object, ...] | None = None

    @property
    def startup_converged(self) -> bool:
        return self.repository.startup_converged

    def converge_startup(self) -> None:
        self.repository.converge_startup()

    def bind_runtime(self, snapshot: ManagedSessionSnapshot) -> None:
        """Fence this service instance to one account/policy until restart."""

        if not isinstance(snapshot, ManagedSessionSnapshot):
            raise ValueError("managed session runtime binding is invalid")
        binding = _runtime_binding(snapshot)
        if self._runtime_binding is not None and self._runtime_binding != binding:
            raise SessionRestartRequired(
                "managed session runtime binding changed; restart is required"
            )
        self._runtime_binding = binding

    def install(
        self,
        lease: SignedManagedSessionLease,
        *,
        access_token: str,
        refresh_token: str,
        client_request_id: str,
        before_commit: Callable[[], None] | None = None,
    ) -> ManagedSessionSnapshot:
        request_hash = client_request_hash(client_request_id)
        requested_binding = _lease_runtime_binding(lease)
        if (
            self._runtime_binding is not None
            and requested_binding != self._runtime_binding
        ):
            raise SessionRestartRequired(
                "managed account or signed policy changed; restart is required"
            )
        try:
            self._verify(
                lease,
                access_token=access_token,
                refresh_token=refresh_token,
                expected_digest=lease.digest,
            )
        except (LeaseValidationError, SessionUnavailable) as error:
            self._record_failure(
                "session.install.rejected",
                lease=lease,
                client_request_hash_value=request_hash,
                reason_code=_failure_code(error),
            )
            raise
        try:
            staged = self.repository.stage_install(
                lease,
                client_request_hash=request_hash,
                request_fingerprint=install_request_fingerprint(lease),
                now=self._timestamp(),
                before_commit=before_commit,
            )
        except (SessionConflict, SessionUnavailable):
            self._record_failure(
                "session.install.rejected",
                lease=lease,
                client_request_hash_value=request_hash,
                reason_code="session_conflict",
            )
            raise
        intent = staged.intent
        if staged.already_committed:
            return self.snapshot()
        if intent.lease_digest != lease.digest:
            self._abort(intent, "lease_digest_changed")
            raise SessionUnavailable("managed session install identity changed")
        self._verify_intent(intent, access_token, refresh_token)
        self.phase_hook("staged", intent.intent_id)

        try:
            self.vault.put(
                intent.credential_ref,
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                },
            )
        except Exception:
            self._abort(intent, "vault_write_failed")
            self._process_cleanup()
            raise SessionVaultError(
                "managed session credential installation failed"
            ) from None

        # A crash here is recoverable because the durable staged row names the
        # vault reference and commits only signed token hashes.
        self.phase_hook("vault_put", intent.intent_id)
        mark = self.repository.mark_vault_written(
            intent.intent_id,
            credential_ref=intent.credential_ref,
            expected_lease_digest=intent.lease_digest,
            now=self._timestamp(),
            before_commit=before_commit,
        )
        if mark == "stale":
            self._delete_reference(intent.credential_ref)
            raise StaleSessionRequest(
                "managed session install was superseded before credential commit"
            )
        self.phase_hook("vault_written", intent.intent_id)

        stored_access, stored_refresh = self._read_credentials(intent.credential_ref)
        self._verify_intent(intent, stored_access, stored_refresh)
        outcome, _generation = self.repository.finalize_install(
            intent.intent_id,
            credential_ref=intent.credential_ref,
            expected_lease_digest=intent.lease_digest,
            now=self._timestamp(),
            before_commit=before_commit,
        )
        if outcome != "committed":
            self._delete_reference(intent.credential_ref)
            raise StaleSessionRequest(
                "managed session install was superseded before activation"
            )
        self.phase_hook("committed", intent.intent_id)
        self._process_cleanup()
        return self.snapshot()

    def snapshot(self) -> ManagedSessionSnapshot:
        """Validate current authorization and append an explicit audit fact.

        Request middleware and GET handlers must use :meth:`read_snapshot` so
        authorization checks cannot mutate SQLite before the Runtime execution
        gate has admitted a semantic write.
        """

        return self._snapshot(record_audit=True)

    def read_snapshot(self) -> ManagedSessionSnapshot:
        """Cryptographically validate the active session without any write."""

        return self._snapshot(record_audit=False)

    def _snapshot(self, *, record_audit: bool) -> ManagedSessionSnapshot:
        active, access_token, _refresh_token = self._validated_active(
            record_failure=record_audit
        )
        snapshot = ManagedSessionSnapshot.from_lease(
            active.intent.lease,
            generation=active.state.generation,
        )
        if (
            self._runtime_binding is not None
            and _runtime_binding(snapshot) != self._runtime_binding
        ):
            raise SessionRestartRequired(
                "managed account or signed policy changed; restart is required"
            )
        if record_audit:
            self.repository.record_audit(
                event_type="session.snapshot.validated",
                outcome="success",
                reason_code=None,
                client_request_hash=None,
                lease=active.intent.lease,
                generation=active.state.generation,
                details={
                    "roles": len(snapshot.roles),
                    "models": len(snapshot.model_allowlist),
                    "admin_denies": len(snapshot.admin_denies),
                },
                now=self._timestamp(),
            )
        del access_token
        if not self.repository.identity_is_current(active):
            raise SessionConflict("managed session changed during snapshot validation")
        return snapshot

    def data_scope_snapshot(self) -> ManagedSessionSnapshot:
        """Return a cryptographically verified identity for read-only scoping.

        Expiry is intentionally not authorization here.  The method exists so
        an expired account can still open its local history and artifacts; all
        mutations and bearer issuance continue to require ``snapshot()``.
        """

        return self._data_scope_snapshot(record_audit=True)

    def read_data_scope_snapshot(self) -> ManagedSessionSnapshot:
        """Verify read-only account scoping without appending session audit."""

        return self._data_scope_snapshot(record_audit=False)

    def _data_scope_snapshot(self, *, record_audit: bool) -> ManagedSessionSnapshot:
        active = self.repository.active(require_quiescent=True)
        access_token, refresh_token = self._read_credentials(
            active.intent.credential_ref
        )
        if active.intent.lease.digest != active.intent.lease_digest:
            raise LeaseValidationError("managed session lease storage was modified")
        verdict = self.verifier.verify_identity(
            active.intent.lease,
            access_token=access_token,
            refresh_token=refresh_token,
            expected_digest=active.intent.lease_digest,
        )
        require_verified(verdict)
        if not self.repository.identity_is_current(active):
            raise SessionConflict(
                "managed session changed during data-scope validation"
            )
        snapshot = ManagedSessionSnapshot.from_lease(
            active.intent.lease,
            generation=active.state.generation,
        )
        if (
            self._runtime_binding is not None
            and _runtime_binding(snapshot) != self._runtime_binding
        ):
            raise SessionRestartRequired(
                "managed account or signed policy changed; restart is required"
            )
        if record_audit:
            self.repository.record_audit(
                event_type="session.data_scope.validated",
                outcome="success",
                reason_code=None,
                client_request_hash=None,
                lease=active.intent.lease,
                generation=active.state.generation,
                details={},
                now=self._timestamp(),
            )
        return snapshot

    def bearer_token(self) -> str:
        active, access_token, _refresh_token = self._validated_active()
        self.repository.record_audit(
            event_type="session.bearer.issued",
            outcome="success",
            reason_code=None,
            client_request_hash=None,
            lease=active.intent.lease,
            generation=active.state.generation,
            details={},
            now=self._timestamp(),
        )
        if not self.repository.identity_is_current(active):
            raise SessionConflict("managed session changed during bearer validation")
        return access_token

    def refresh_context(self) -> SessionRefreshContext:
        """Return credential-bearing state only to the refresh coordinator.

        The access token is first committed by and verified against the signed
        lease. Only its bounded ``exp`` claim is decoded; token material is
        never returned or persisted by this projection.
        """

        active, access_token, refresh_token = self._validated_active()
        expires_at = _access_token_expiry(access_token)
        if not self.repository.identity_is_current(active):
            raise SessionConflict("managed session changed during refresh projection")
        return SessionRefreshContext(
            lease=active.intent.lease,
            access_expires_at=expires_at,
            refresh_token=refresh_token,
        )

    def logout(
        self,
        *,
        client_request_id: str,
        expected_lease_digest: str | None,
    ) -> SessionLogoutReceipt:
        request_hash = client_request_hash(client_request_id)
        receipt = self.repository.logout(
            client_request_hash=request_hash,
            expected_lease_digest=expected_lease_digest,
            now=self._timestamp(),
        )
        self.phase_hook("logout_committed", request_hash)
        self._process_cleanup()
        return receipt

    def recover(self) -> SessionRecoveryReport:
        finalized = 0
        aborted = 0
        blocked = 0
        pending = self.repository.pending_install()
        if pending is not None:
            try:
                access_token, refresh_token = self._read_credentials(
                    pending.credential_ref
                )
            except KeyError:
                if self._abort(pending, "vault_material_missing"):
                    aborted += 1
            except SessionVaultError:
                blocked += 1
            else:
                try:
                    self._verify_intent(pending, access_token, refresh_token)
                    mark = self.repository.mark_vault_written(
                        pending.intent_id,
                        credential_ref=pending.credential_ref,
                        expected_lease_digest=pending.lease_digest,
                        now=self._timestamp(),
                    )
                    if mark == "stale":
                        self._delete_reference(pending.credential_ref)
                    else:
                        outcome, _generation = self.repository.finalize_install(
                            pending.intent_id,
                            credential_ref=pending.credential_ref,
                            expected_lease_digest=pending.lease_digest,
                            now=self._timestamp(),
                        )
                        if outcome == "committed":
                            finalized += 1
                        else:
                            self._delete_reference(pending.credential_ref)
                except (LeaseValidationError, SessionUnavailable):
                    if self._abort(pending, "recovery_validation_failed"):
                        aborted += 1
        cleaned, cleanup_blocked = self._process_cleanup(include_terminal=True)
        blocked += cleanup_blocked
        self.repository.record_audit(
            event_type="session.recovery.completed",
            outcome="success" if blocked == 0 else "partial",
            reason_code=None if blocked == 0 else "vault_unavailable",
            client_request_hash=None,
            lease=None,
            generation=self.repository.state().generation,
            details={
                "finalized": finalized,
                "aborted": aborted,
                "cleaned": cleaned,
                "blocked": blocked,
            },
            now=self._timestamp(),
        )
        return SessionRecoveryReport(
            finalized_installs=finalized,
            aborted_installs=aborted,
            cleaned_credentials=cleaned,
            blocked_operations=blocked,
        )

    def _validated_active(
        self, *, record_failure: bool = True
    ) -> tuple[ActiveSessionRecord, str, str]:
        try:
            active = self.repository.active(require_quiescent=True)
            access_token, refresh_token = self._read_credentials(
                active.intent.credential_ref
            )
            self._verify_intent(active.intent, access_token, refresh_token)
            if not self.repository.identity_is_current(active):
                raise SessionConflict(
                    "managed session changed during credential validation"
                )
            if (
                self._runtime_binding is not None
                and _lease_runtime_binding(active.intent.lease) != self._runtime_binding
            ):
                raise SessionRestartRequired(
                    "managed account or signed policy changed; restart is required"
                )
            return active, access_token, refresh_token
        except (
            LeaseValidationError,
            SessionConflict,
            SessionUnavailable,
            SessionVaultError,
        ) as error:
            if record_failure:
                self._record_failure(
                    "session.validation.rejected",
                    lease=None,
                    client_request_hash_value=None,
                    reason_code=_failure_code(error),
                )
            raise

    def _verify_intent(
        self,
        intent: SessionInstallIntent,
        access_token: str,
        refresh_token: str,
    ) -> None:
        if intent.lease.digest != intent.lease_digest:
            raise LeaseValidationError("managed session lease storage was modified")
        self._verify(
            intent.lease,
            access_token=access_token,
            refresh_token=refresh_token,
            expected_digest=intent.lease_digest,
        )

    def _verify(
        self,
        lease: SignedManagedSessionLease,
        *,
        access_token: str,
        refresh_token: str,
        expected_digest: str,
    ) -> None:
        verdict = self.verifier.verify(
            lease,
            now=self._now(),
            access_token=access_token,
            refresh_token=refresh_token,
            expected_digest=expected_digest,
        )
        require_verified(verdict)

    def _read_credentials(self, credential_ref: str) -> tuple[str, str]:
        try:
            material = self.vault.get(credential_ref)
        except KeyError:
            raise
        except Exception:
            raise SessionVaultError(
                "managed session credential vault is unavailable"
            ) from None
        if not isinstance(material, Mapping) or set(material) != {
            "access_token",
            "refresh_token",
        }:
            raise SessionVaultError(
                "managed session credential vault returned invalid data"
            )
        access_token = material.get("access_token")
        refresh_token = material.get("refresh_token")
        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            raise SessionVaultError(
                "managed session credential vault returned invalid data"
            )
        return access_token, refresh_token

    def _abort(self, intent: SessionInstallIntent, reason_code: str) -> bool:
        return self.repository.abort_install(
            intent.intent_id,
            credential_ref=intent.credential_ref,
            reason_code=reason_code,
            now=self._timestamp(),
        )

    def _delete_reference(self, credential_ref: str) -> bool:
        if self.repository.reference_is_live(credential_ref):
            return False
        try:
            self.vault.delete(credential_ref)
        except Exception:
            return False
        self.repository.mark_cleanup_done(
            credential_ref,
            now=self._timestamp(),
        )
        return True

    def _process_cleanup(self, *, include_terminal: bool = False) -> tuple[int, int]:
        cleaned = 0
        blocked = 0
        targets = set(self.repository.cleanup_pending())
        if include_terminal:
            targets.update(self.repository.terminal_credential_references())
        for credential_ref in sorted(targets):
            if self.repository.reference_is_live(credential_ref):
                # A live reference is never deleted.  This should only be
                # possible after external state tampering, so leave it pending.
                blocked += 1
                continue
            try:
                self.vault.delete(credential_ref)
            except Exception:
                blocked += 1
                continue
            self.repository.mark_cleanup_done(
                credential_ref,
                now=self._timestamp(),
            )
            cleaned += 1
        return cleaned, blocked

    def _record_failure(
        self,
        event_type: str,
        *,
        lease: SignedManagedSessionLease | None,
        client_request_hash_value: str | None,
        reason_code: str,
    ) -> None:
        try:
            generation = self.repository.state().generation
            self.repository.record_audit(
                event_type=event_type,
                outcome="failed",
                reason_code=reason_code,
                client_request_hash=client_request_hash_value,
                lease=lease,
                generation=generation,
                details={},
                now=self._timestamp(),
            )
        except Exception:
            # Never replace the original authorization failure with diagnostics.
            return

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise LeaseValidationError("managed session clock must be timezone-aware")
        return value.astimezone(UTC)

    def _timestamp(self) -> str:
        return self._now().isoformat().replace("+00:00", "Z")


def _failure_code(error: Exception) -> str:
    if isinstance(error, LeaseSignatureError):
        return "signature_invalid"
    if isinstance(error, LeaseValidationError):
        return "lease_invalid"
    if isinstance(error, SessionConflict):
        return "session_conflict"
    if isinstance(error, SessionVaultError):
        return "vault_unavailable"
    return "session_unavailable"


def _lease_runtime_binding(lease: SignedManagedSessionLease) -> tuple[object, ...]:
    claims = lease.claims
    return (
        claims.account_id,
        claims.organization_id,
        frozenset(claims.model_allowlist),
        frozenset(claims.admin_denies),
    )


def _runtime_binding(snapshot: ManagedSessionSnapshot) -> tuple[object, ...]:
    return (
        snapshot.account_id,
        snapshot.organization_id,
        frozenset(snapshot.model_allowlist),
        frozenset(snapshot.admin_denies),
    )


def _access_token_expiry(access_token: str) -> datetime:
    try:
        segments = access_token.split(".")
        if len(segments) != 3 or len(segments[1]) > 16 * 1024:
            raise ValueError
        payload = base64.urlsafe_b64decode(segments[1] + "=" * (-len(segments[1]) % 4))
        claims = json.loads(payload.decode("utf-8"))
        expires = claims.get("exp")
        if isinstance(expires, bool) or not isinstance(expires, int):
            raise ValueError
        result = datetime.fromtimestamp(expires, UTC)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        raise LeaseValidationError(
            "managed session access token expiry is invalid"
        ) from None
    return result


__all__ = ["ManagedSessionService"]
