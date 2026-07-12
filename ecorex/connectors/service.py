"""Durable backend connector orchestration with no client-controlled secrets."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import Context, ContextVar, copy_context
from datetime import UTC, datetime
from functools import wraps
import hashlib
import inspect
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Callable, Iterator, Literal, Protocol
from urllib.parse import parse_qs, urlsplit
import uuid

from ecorex.capabilities.schema import SchemaInstanceError, validate_schema_instance
from ecorex.runtime.commit_guard import (
    transaction_commit_guard,
    transaction_commit_guard_active,
)
from ecorex.runtime.invariant_guard import (
    RuntimeExecutionDenied,
    RuntimeExecutionGate,
    RuntimeExecutionPermit,
)

from .errors import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorIdempotencyConflict,
    ConnectorIdempotencyRequired,
    ConnectorInputInvalid,
    ConnectorInvocationUncertain,
    ConnectorNotFound,
    ConnectorPermissionDenied,
    ConnectorUnavailable,
)
from .models import (
    AuthChallenge,
    AuthGrant,
    ConnectorAuthKind,
    ConnectorCatalogItem,
    ConnectorEffect,
    ConnectorHealth,
    ConnectorHealthResult,
    ConnectorInstance,
    ConnectorInvocationContext,
    ConnectorInvocationRecord,
)
from .registry import ConnectorRegistry, RevocableConnectorAdapter
from .repository import (
    ConnectorOperationLease,
    ConnectorOutboxEvent,
    LifecycleRequestReservation,
    SQLiteConnectorRepository,
)
from .vault import CredentialVault, RejectingCredentialVault


_MAX_ACTION_JSON_BYTES = 8 * 1024 * 1024
_MAX_IDEMPOTENCY_KEY_BYTES = 512
# Provider latency and local admission latency are separate budgets.  Starting
# the provider timeout while a call is still waiting for the process-local
# limiter or the final SQLite/policy fence can incorrectly classify a write as
# a known pre-dispatch failure on a busy Runtime.
_ADAPTER_ADMISSION_TIMEOUT_FLOOR_SECONDS = 1.0
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_CLIENT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SENSITIVE_COMPACT_NAMES = frozenset(
    {
        "token",
        "accesstoken",
        "refreshtoken",
        "apitoken",
        "apikey",
        "password",
        "secret",
        "secretkey",
        "clientsecret",
        "privatekey",
        "authorization",
        "cookie",
        "setcookie",
        "sessiontoken",
        "credential",
        "credentials",
    }
)


@dataclass(slots=True)
class _OperationLeaseGuard:
    retained: bool = False

    def retain(self) -> None:
        self.retained = True


@dataclass(frozen=True, slots=True)
class ConnectorOutboxDeliveryHealth:
    """Bounded, secret-free observation of Connector event delivery."""

    status: Literal["disabled", "idle", "draining", "degraded", "stuck"]
    pending: int
    requested_generation: int
    completed_generation: int
    active: bool
    stuck_event_id: str | None = None
    last_error_code: str | None = None


@dataclass(slots=True)
class _OutboxPublishAttempt:
    event: ConnectorOutboxEvent
    permit: RuntimeExecutionPermit | None
    done: threading.Event
    heartbeat_stop: threading.Event
    sink_succeeded: bool = False
    terminal_recorded: bool = False
    marked_published: bool = False
    error_code: str | None = None


class ConnectorResultCoordinator(Protocol):
    def complete_result(
        self,
        record: ConnectorInvocationRecord,
        operation_lease: ConnectorOperationLease,
        *,
        result: Any,
        encoded_result: bytes,
        requested_name: str,
        created_by_tool_id: Literal["connector_read", "connector_write"],
        completion_path: Literal["provider_result", "late_provider_result"],
    ) -> Mapping[str, Any]: ...

    def complete_unavailable(
        self,
        record: ConnectorInvocationRecord,
        operation_lease: ConnectorOperationLease,
        *,
        error_code: str,
        requested_name: str,
        created_by_tool_id: Literal["connector_read", "connector_write"],
        completion_path: Literal["provider_result", "late_provider_result"],
    ) -> Mapping[str, Any]: ...

    def finalize_staged(self, invocation_id: str) -> Mapping[str, Any]: ...

    def recover_pending(self, *, limit: int = 1000) -> Mapping[str, int]: ...


class _RejectedConnectorResult(ValueError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _execution_scoped(scope: str):
    """Issue one signed epoch permit around an async Connector operation."""

    def decorate(operation):
        @wraps(operation)
        async def guarded(self, *args, **kwargs):
            subject = operation.__name__
            if args and isinstance(args[0], str):
                digest = hashlib.sha256(args[0].encode("utf-8")).hexdigest()[:24]
                subject = f"{subject}:{digest}"
            with self._execution_scope(scope=scope, subject=subject):
                return await operation(self, *args, **kwargs)

        return guarded

    return decorate


class ConnectorService:
    """Authoritative connector service backed by the Runtime SQLite database.

    ``allowed_return_uris`` is deliberately mandatory. The WebUI cannot choose
    an OAuth redirect target; Runtime composition supplies an exact loopback
    allowlist. Outbox publishers must deduplicate the immutable ``event_id``
    because delivery is intentionally at-least-once.
    """

    def __init__(
        self,
        registry: ConnectorRegistry,
        *,
        allowed_return_uris: frozenset[str],
        vault: CredentialVault | None = None,
        audit_sink: Callable[[ConnectorInvocationRecord], None] | None = None,
        repository: SQLiteConnectorRepository | None = None,
        outbox_publisher: Callable[[ConnectorOutboxEvent], None] | None = None,
        outbox_publish_timeout_seconds: float = 2.0,
        adapter_timeout_seconds: float = 65.0,
        max_concurrent_adapter_calls: int = 16,
        reauthorization_drain_timeout: float = 30.0,
        initialize: bool = True,
        execution_gate: RuntimeExecutionGate | None = None,
    ) -> None:
        if not allowed_return_uris:
            raise ValueError("connector OAuth return URI allowlist is required")
        for return_uri in allowed_return_uris:
            _validate_loopback_return_uri(return_uri)
        self.allowed_return_uris = frozenset(allowed_return_uris)
        self.registry = registry
        self.vault = vault or RejectingCredentialVault()
        self.audit_sink = audit_sink
        self._execution_gate: RuntimeExecutionGate | None = None
        self._execution_permit_context: ContextVar[
            RuntimeExecutionPermit | None
        ] = ContextVar(
            f"connector_execution_permit_{id(self):x}",
            default=None,
        )
        self.repository = repository or SQLiteConnectorRepository.volatile(
            initialize=initialize and execution_gate is None
        )
        if execution_gate is not None:
            self.bind_execution_gate(execution_gate)
        self.outbox_publisher = outbox_publisher
        if not 0.05 <= outbox_publish_timeout_seconds <= 60:
            raise ValueError("connector outbox publish timeout is invalid")
        self.outbox_publish_timeout_seconds = float(outbox_publish_timeout_seconds)
        if not 0.05 <= adapter_timeout_seconds <= 300:
            raise ValueError("connector adapter timeout is invalid")
        if not 1 <= max_concurrent_adapter_calls <= 64:
            raise ValueError("connector adapter concurrency is invalid")
        self.adapter_timeout_seconds = float(adapter_timeout_seconds)
        self.max_concurrent_adapter_calls = int(max_concurrent_adapter_calls)
        if not 0.1 <= reauthorization_drain_timeout <= 300:
            raise ValueError("connector reauthorization drain timeout is invalid")
        self.reauthorization_drain_timeout = float(reauthorization_drain_timeout)
        self._adapter_loop: asyncio.AbstractEventLoop | None = None
        self._adapter_limiter: asyncio.BoundedSemaphore | None = None
        self._auth_limiter_loop: asyncio.AbstractEventLoop | None = None
        self._auth_limiters: dict[str, asyncio.BoundedSemaphore] = {}
        self._auth_completion_tasks: set[asyncio.Task[Any]] = set()
        # Drain requests are a generation-based single flight. A request that
        # arrives while an owner is publishing increments the generation; the
        # owner must observe and rescan it before becoming idle. This closes the
        # old try-lock lost-wakeup window without accumulating publisher work.
        self._outbox_condition = threading.Condition()
        self._outbox_requested_generation = 0
        self._outbox_completed_generation = 0
        self._outbox_drain_active = False
        self._outbox_stuck_attempt: _OutboxPublishAttempt | None = None
        self._outbox_last_error_code: str | None = None
        self._outbox_deadline_context: ContextVar[float | None] = ContextVar(
            f"connector_outbox_deadline_{id(self):x}",
            default=None,
        )
        self._maintenance_cycle_lock = threading.Lock()
        self._startup_convergence_lock = threading.Lock()
        self._startup_converged = False
        self._uncertain_watchers: set[asyncio.Task[Any]] = set()
        self._result_coordinator: ConnectorResultCoordinator | None = None
        if initialize:
            self.converge_startup()
        # Pending events are drained by the lifecycle-managed maintenance
        # supervisor. Calling an arbitrary synchronous publisher here would
        # make Product construction and crash recovery unbounded.

    def converge_startup(self) -> None:
        """Persist catalog facts and perform local recovery exactly once.

        Projection-only composition passes ``initialize=False`` so public
        catalogs can be served without syncing definitions, changing leases,
        or touching the credential vault.  Healthy startup calls this method
        before Connector mutations and maintenance are admitted.
        """

        with self._startup_convergence_lock:
            if self._startup_converged:
                return
            with self._execution_scope(
                scope="connector_startup",
                subject="startup_convergence",
            ):
                self.repository.converge_startup()
                self.repository.sync_definitions(self.registry.definitions())
                self._recover_transitional_state()
            self._startup_converged = True

    @property
    def execution_gate(self) -> RuntimeExecutionGate | None:
        return self._execution_gate

    def bind_execution_gate(self, gate: RuntimeExecutionGate) -> None:
        if not isinstance(gate, RuntimeExecutionGate):
            raise TypeError("connector Runtime execution gate is invalid")
        if self._execution_gate is not None and self._execution_gate is not gate:
            raise RuntimeError("Connector service already has an execution gate")
        self._execution_gate = gate
        self.repository.set_before_commit_validator(self._assert_current_permit)

    @contextmanager
    def _execution_scope(
        self,
        *,
        scope: str,
        subject: str,
    ) -> Iterator[RuntimeExecutionPermit | None]:
        gate = self._execution_gate
        if gate is None:
            yield None
            return
        current = self._execution_permit_context.get()
        if current is not None:
            self._assert_permit(current)
            with transaction_commit_guard(lambda: self._assert_permit(current)):
                yield current
                self._assert_permit(current)
            return
        try:
            permit = gate.issue_permit(scope=scope, subject=subject)
        except RuntimeExecutionDenied as error:
            raise ConnectorUnavailable(
                "connector execution is unavailable while Runtime is read-only"
            ) from error
        token = self._execution_permit_context.set(permit)
        try:
            with transaction_commit_guard(lambda: self._assert_permit(permit)):
                yield permit
                self._assert_permit(permit)
        finally:
            self._execution_permit_context.reset(token)

    def _assert_permit(self, permit: RuntimeExecutionPermit) -> None:
        gate = self._execution_gate
        if gate is None:
            raise ConnectorUnavailable("connector Runtime execution gate is unavailable")
        try:
            gate.assert_permit(permit)
        except RuntimeExecutionDenied as error:
            raise ConnectorUnavailable(
                "connector execution epoch closed before completion"
            ) from error

    def _assert_current_permit(self) -> None:
        if self._execution_gate is None:
            return
        permit = self._execution_permit_context.get()
        if permit is None:
            if transaction_commit_guard_active():
                return
            raise ConnectorUnavailable(
                "connector mutation has no Runtime execution permit"
            )
        self._assert_permit(permit)

    @contextmanager
    def control_admission(
        self,
        *,
        operation: str,
        subject: str,
    ) -> Iterator[RuntimeExecutionPermit | None]:
        """Fence a synchronous lifecycle/recovery mutation outside HTTP."""

        safe_operation = re.sub(r"[^a-z0-9_.:-]+", "_", operation.casefold())
        safe_operation = safe_operation[:64].strip("_") or "control"
        digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:24]
        with self._execution_scope(
            scope="connector_lifecycle",
            subject=f"{safe_operation}:{digest}",
        ) as permit:
            yield permit

    def bind_result_coordinator(
        self, coordinator: ConnectorResultCoordinator
    ) -> None:
        if self._result_coordinator is not None and self._result_coordinator is not coordinator:
            raise RuntimeError("Connector result coordinator is already bound")
        self._result_coordinator = coordinator

    def catalog(self) -> tuple[ConnectorCatalogItem, ...]:
        # Catalog is used by bootstrap, health, and the public GET endpoint.
        # It must remain a strict projection even when Runtime is serving in
        # critical read-only mode. Lease expiry and credential cleanup belong
        # exclusively to startup/maintenance or explicit mutations.
        instances = self.repository.list_instances()
        items: list[ConnectorCatalogItem] = []
        for definition in self.registry.definitions():
            projected = tuple(
                instance.to_projection(definition)
                for instance in instances
                if instance.connector_id == definition.connector_id
            )
            available = self.registry.has_adapter(definition.connector_id)
            unavailable_reason = None
            if not available:
                unavailable_reason = "adapter_not_installed"
            elif (
                ConnectorAuthKind.OAUTH2 not in definition.auth_kinds
                and any(
                    kind in {
                        ConnectorAuthKind.API_TOKEN,
                        ConnectorAuthKind.APP_CREDENTIALS,
                    }
                    for kind in definition.auth_kinds
                )
            ):
                unavailable_reason = "secure_credential_submission_unavailable"
            items.append(
                ConnectorCatalogItem(
                    definition=definition,
                    adapter_available=available,
                    instances=projected,
                    unavailable_reason=unavailable_reason,
                )
            )
        return tuple(items)

    def _reserve_lifecycle_request(
        self,
        client_request_id: str | None,
        *,
        operation_kind: str,
        fingerprint: Mapping[str, Any],
    ) -> tuple[str, LifecycleRequestReservation]:
        request_id = _normalize_client_request_id(client_request_id)
        reservation = self.repository.reserve_lifecycle_request(
            client_request_id=request_id,
            operation_kind=operation_kind,
            request_sha256=_json_digest(
                fingerprint,
                label="connector lifecycle request",
            ),
            lease_seconds=300,
        )
        if reservation.outcome == "conflict":
            raise ConnectorIdempotencyConflict(
                "client request ID was reused for a different connector lifecycle request"
            )
        if reservation.outcome == "in_progress":
            raise ConnectorUnavailable("matching connector lifecycle request is in progress")
        if reservation.outcome == "failed":
            _raise_replayed_lifecycle_error(reservation.error_code)
        return request_id, reservation

    def _fail_lifecycle_request(
        self,
        request_id: str,
        reservation: LifecycleRequestReservation,
        error_code: str,
    ) -> None:
        if reservation.lease_token is None:
            return
        self.repository.fail_lifecycle_request(
            request_id,
            reservation.lease_token,
            error_code=error_code,
        )

    @_execution_scoped("connector_lifecycle")
    async def begin_connect(
        self,
        connector_id: str,
        *,
        auth_kind: ConnectorAuthKind,
        return_uri: str,
        client_request_id: str | None = None,
        interaction_binding: tuple[str, int, str] | None = None,
    ) -> AuthChallenge:
        return await self._begin_authorization(
            connector_id,
            auth_kind=auth_kind,
            return_uri=return_uri,
            client_request_id=client_request_id,
            reauthorize_instance_id=None,
            interaction_binding=interaction_binding,
        )

    @_execution_scoped("connector_lifecycle")
    async def begin_reauthorize(
        self,
        instance_id: str,
        *,
        auth_kind: ConnectorAuthKind,
        return_uri: str,
        client_request_id: str | None = None,
        interaction_binding: tuple[str, int, str] | None = None,
    ) -> AuthChallenge:
        state = await asyncio.to_thread(
            self.repository.get_instance_state, instance_id
        )
        if state is None:
            raise ConnectorNotFound(f"unknown connector instance: {instance_id!r}")
        instance, lifecycle = state
        if lifecycle != "active" or not instance.enabled:
            raise ConnectorUnavailable(
                "connector instance is not available for reauthorization"
            )
        return await self._begin_authorization(
            instance.connector_id,
            auth_kind=auth_kind,
            return_uri=return_uri,
            client_request_id=client_request_id,
            reauthorize_instance_id=instance_id,
            interaction_binding=interaction_binding,
        )

    async def _begin_authorization(
        self,
        connector_id: str,
        *,
        auth_kind: ConnectorAuthKind,
        return_uri: str,
        client_request_id: str | None,
        reauthorize_instance_id: str | None,
        interaction_binding: tuple[str, int, str] | None,
    ) -> AuthChallenge:
        await asyncio.to_thread(self._recover_transitional_state)
        operation_kind = (
            "connector.reauthorize"
            if reauthorize_instance_id is not None
            else "connector.auth.begin"
        )
        request_id, reservation = await asyncio.to_thread(
            self._reserve_lifecycle_request,
            client_request_id,
            operation_kind=operation_kind,
            fingerprint={
                "connector_id": connector_id,
                "auth_kind": auth_kind.value,
                "return_uri": return_uri,
                "reauthorize_instance_id": reauthorize_instance_id,
            },
        )
        if reservation.outcome == "replay":
            flow_id = str((reservation.result or {}).get("flow_id", ""))
            challenge = await self._replay_auth_challenge(flow_id)
            if interaction_binding is not None:
                await asyncio.to_thread(
                    self.repository.bind_active_flow_to_interaction,
                    flow_id,
                    interaction_id=interaction_binding[0],
                    generation=interaction_binding[1],
                    operation_token=interaction_binding[2],
                )
            return challenge
        assert reservation.lease_token is not None

        try:
            if return_uri not in self.allowed_return_uris:
                raise ConnectorAuthError("connector OAuth return URI is not allowed")
            definition = self.registry.definition(connector_id)
            if auth_kind not in definition.auth_kinds:
                raise ConnectorAuthError(
                    f"connector {connector_id!r} does not support {auth_kind.value}"
                )
            if auth_kind in {
                ConnectorAuthKind.API_TOKEN,
                ConnectorAuthKind.APP_CREDENTIALS,
            }:
                raise ConnectorUnavailable(
                    "secure one-time credential submission is not available"
                )
            try:
                adapter = self.registry.adapter(connector_id)
            except ConnectorNotFound as exc:
                raise ConnectorUnavailable(str(exc)) from exc

            flow_id = "connflow_" + uuid.uuid4().hex
            state = secrets.token_urlsafe(32)
            pkce_verifier = secrets.token_urlsafe(64)
            pkce_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(pkce_verifier.encode("ascii")).digest()
            ).rstrip(b"=").decode("ascii")
            try:
                challenge = await self._call_adapter_bounded(
                    adapter.begin_auth,
                    flow_id=flow_id,
                    auth_kind=auth_kind,
                    return_uri=return_uri,
                    state=state,
                    code_challenge=pkce_challenge,
                    code_challenge_method="S256",
                )
                _validate_auth_challenge(
                    challenge,
                    flow_id=flow_id,
                    connector_id=connector_id,
                    auth_kind=auth_kind,
                    expected_state=state,
                    expected_code_challenge=pkce_challenge,
                )
            except ConnectorError:
                raise
            except Exception:
                raise ConnectorAuthError(
                    "connector authorization could not be started"
                ) from None

            operation_token = "connflowlease_" + uuid.uuid4().hex
            private_ref = f"ecorex/connector-flow/{flow_id}"
            try:
                await asyncio.to_thread(
                    self.repository.create_preparing_flow,
                    flow_id=flow_id,
                    connector_id=connector_id,
                    auth_kind=auth_kind,
                    state_sha256=hashlib.sha256(state.encode("utf-8")).hexdigest(),
                    private_ref=private_ref,
                    expires_at=challenge.expires_at,
                    operation_token=operation_token,
                    reauthorize_instance_id=reauthorize_instance_id,
                    lease_seconds=300,
                )
            except sqlite3.IntegrityError:
                raise ConnectorUnavailable(
                    "connector reauthorization is already in progress"
                ) from None
            try:
                await self._vault_put_cancellation_safe(
                    private_ref,
                    {
                        "state": state,
                        "pkce_verifier": pkce_verifier,
                        "challenge_json": _canonical_private_challenge(challenge),
                    },
                )
                activation_task = asyncio.create_task(
                    asyncio.to_thread(
                        self.repository.activate_flow,
                        flow_id,
                        operation_token,
                        lifecycle_request=(request_id, reservation.lease_token),
                        interaction_binding=interaction_binding,
                    )
                )
                activated = False
                try:
                    await asyncio.shield(activation_task)
                    activated = True
                except asyncio.CancelledError:
                    # SQLite work already running in a thread cannot be
                    # cancelled. Observe its durable outcome before deciding
                    # whether the vault intent may be removed.
                    await activation_task
                    activated = True
                    raise
            except BaseException:
                if "activated" in locals() and activated:
                    raise
                try:
                    await asyncio.to_thread(self.vault.delete, private_ref)
                except BaseException:
                    raise
                else:
                    await asyncio.to_thread(
                        self.repository.remove_flow, flow_id, operation_token
                    )
                raise
            return challenge
        except ConnectorError as error:
            await asyncio.to_thread(
                self._fail_lifecycle_request, request_id, reservation, error.code
            )
            raise
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                await asyncio.to_thread(
                    self._fail_lifecycle_request,
                    request_id,
                    reservation,
                    ConnectorUnavailable.code,
                )
                raise
            if isinstance(error, Exception):
                await asyncio.to_thread(
                    self._fail_lifecycle_request,
                    request_id,
                    reservation,
                    ConnectorUnavailable.code,
                )
                raise ConnectorUnavailable(
                    "connector credential vault state could not be persisted"
                ) from None
            raise

    async def _replay_auth_challenge(self, flow_id: str) -> AuthChallenge:
        if not flow_id:
            raise ConnectorAuthError("connector authorization replay is unavailable")
        flow = await asyncio.to_thread(self.repository.get_active_flow, flow_id)
        if flow is None:
            raise ConnectorAuthError(
                "connector authorization request was already consumed or expired"
            )
        try:
            private_state = await asyncio.to_thread(self.vault.get, flow.private_ref)
            return _private_auth_challenge(
                private_state,
                flow=flow,
            )
        except ConnectorAuthError:
            raise
        except Exception:
            raise ConnectorUnavailable(
                "connector authorization replay state is unavailable"
            ) from None

    @_execution_scoped("connector_lifecycle")
    async def complete_connect(
        self,
        flow_id: str,
        response: Mapping[str, str],
    ) -> ConnectorInstance:
        """Run one exact OAuth completion saga with bounded caller latency."""

        task = asyncio.create_task(
            self._complete_connect_saga(flow_id, response)
        )
        self._auth_completion_tasks.add(task)

        def settled(completed: asyncio.Task[Any]) -> None:
            self._auth_completion_tasks.discard(completed)
            _consume_background_task(completed)

        task.add_done_callback(settled)
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=max(1.0, self.adapter_timeout_seconds),
            )
        except TimeoutError:
            raise ConnectorUnavailable(
                "connector authorization completion is still pending"
            ) from None

    async def _complete_connect_saga(
        self,
        flow_id: str,
        response: Mapping[str, str],
    ) -> ConnectorInstance:
        operation_token = "connflowconsume_" + uuid.uuid4().hex
        # Flow consumption is a durable state transition, not a repository
        # convenience read.  Keep the exact mutation inside a named Runtime
        # control admission so a future caller cannot accidentally inherit a
        # broad lifecycle scope and bypass the execution gate.
        with self.control_admission(operation="consume_flow", subject=flow_id):
            consumption = await asyncio.to_thread(
                self.repository.consume_flow,
                flow_id,
                operation_token=operation_token,
                lease_seconds=300,
            )
        if consumption.reason == "unavailable":
            raise ConnectorAuthError("unknown or already consumed connector auth flow")
        if consumption.reason == "expired":
            if consumption.cleanup_ref is not None:
                await asyncio.to_thread(self.vault.delete, consumption.cleanup_ref)
            await asyncio.to_thread(
                self.repository.finalize_flow_cleanup, flow_id, operation_token
            )
            raise ConnectorAuthError("connector auth flow expired")
        flow = consumption.record
        assert flow is not None
        flow_heartbeat_stop = asyncio.Event()
        flow_heartbeat = asyncio.create_task(
            self._consumed_flow_heartbeat(flow, flow_heartbeat_stop)
        )
        completion_committed = False
        cancellation_requested = False
        try:
            private_state = await asyncio.to_thread(self.vault.get, flow.private_ref)
            if flow.auth_kind is ConnectorAuthKind.OAUTH2:
                expected_state = str(private_state.get("state", ""))
                received_state = str(response.get("state", ""))
                try:
                    state_matches = secrets.compare_digest(expected_state, received_state)
                except TypeError:
                    state_matches = False
                if not expected_state or not state_matches:
                    raise ConnectorAuthError("connector OAuth state validation failed")
                if not str(private_state.get("pkce_verifier", "")):
                    raise ConnectorAuthError("connector PKCE verifier is unavailable")
            adapter = self.registry.adapter(flow.connector_id)
            try:
                async def complete_provider_auth() -> Any:
                    async with self._auth_limiter_for_running_loop(
                        flow.connector_id
                    ):
                        self._assert_current_permit()
                        try:
                            result = await _call_adapter(
                                adapter.complete_auth,
                                flow_id=flow_id,
                                response=response,
                                private_state=private_state,
                            )
                        except BaseException:
                            self._assert_current_permit()
                            raise
                        self._assert_current_permit()
                        return result

                auth_task = asyncio.create_task(complete_provider_auth())
                try:
                    grant = await asyncio.shield(auth_task)
                except asyncio.CancelledError:
                    # OAuth code exchange is one-shot. Settle the exact task and
                    # durably commit (or fail) its grant before honoring caller
                    # cancellation; never discard a background token exchange.
                    grant = await auth_task
                    cancellation_requested = True
                normalized = _normalize_grant(grant)
            except ConnectorAuthError:
                raise
            except Exception:
                raise ConnectorAuthError(
                    "connector authorization could not be completed"
                ) from None
            account_subject, account_display_name, granted_scopes, credential_material = (
                normalized
            )
            if flow.reauthorize_instance_id is not None:
                instance = await self._complete_reauthorization(
                    flow,
                    account_subject=account_subject,
                    account_display_name=account_display_name,
                    granted_scopes=granted_scopes,
                    credential_material=credential_material,
                )
                completion_committed = True
                if cancellation_requested:
                    raise asyncio.CancelledError
                return instance
            instance_id = "conn_" + uuid.uuid4().hex
            credential_ref = f"ecorex/connectors/{instance_id}"
            now = datetime.now(UTC)
            pending = ConnectorInstance(
                instance_id=instance_id,
                connector_id=flow.connector_id,
                account_subject=account_subject,
                account_display_name=account_display_name,
                credential_ref=credential_ref,
                granted_scopes=granted_scopes,
                health=ConnectorHealth.AUTHENTICATING,
                created_at=now,
                updated_at=now,
            )
            transition_token = "connpending_" + uuid.uuid4().hex
            try:
                await asyncio.to_thread(
                    self.repository.insert_pending_instance,
                    pending,
                    transition_token=transition_token,
                    lease_seconds=300,
                )
            except sqlite3.IntegrityError:
                raise ConnectorAuthError(
                    "this connector account is already connected"
                ) from None
            try:
                await self._vault_put_cancellation_safe(
                    credential_ref, credential_material
                )
                activation_task = asyncio.create_task(
                    asyncio.to_thread(
                        self.repository.activate_instance,
                        instance_id,
                        transition_token,
                        auth_flow_id=flow.flow_id,
                        auth_connector_id=flow.connector_id,
                    )
                )
                activation_committed = False
                try:
                    instance = await asyncio.shield(activation_task)
                    activation_committed = True
                except asyncio.CancelledError:
                    instance = await activation_task
                    activation_committed = True
                    raise
            except BaseException:
                if not (
                    "activation_committed" in locals() and activation_committed
                ):
                    await asyncio.to_thread(self.vault.delete, credential_ref)
                    await asyncio.to_thread(
                        self.repository.remove_pending_instance,
                        instance_id,
                        transition_token,
                    )
                raise
            completion_committed = True
            await self.publish_pending_best_effort()
            if cancellation_requested:
                raise asyncio.CancelledError
            return instance
        except BaseException as error:
            if isinstance(error, Exception):
                await asyncio.to_thread(
                    self.repository.fail_interaction_login_by_flow,
                    flow.flow_id,
                    operation_token=flow.operation_token,
                    error_code=str(
                        getattr(error, "code", ConnectorAuthError.code)
                    ),
                )
            raise
        finally:
            flow_heartbeat_stop.set()
            await flow_heartbeat
            # The consumed DB row is the recovery intent. Cleanup failure must
            # never undo an atomically activated account or strand the HITL.
            try:
                await asyncio.to_thread(self.vault.delete, flow.private_ref)
                await asyncio.to_thread(
                    self.repository.finalize_flow_cleanup,
                    flow_id,
                    flow.operation_token,
                )
            except Exception:
                if not completion_committed:
                    # The binding has already been terminalized above. Startup
                    # recovery owns the opaque flow reference from this point.
                    pass

    @_execution_scoped("connector_lifecycle")
    async def cancel_interaction_login(self, interaction_id: str) -> None:
        cleanup = await asyncio.to_thread(
            self.repository.cancel_interaction_login,
            interaction_id,
        )
        if cleanup is None:
            return
        flow_id, private_ref, operation_token = cleanup
        await asyncio.to_thread(self.vault.delete, private_ref)
        await asyncio.to_thread(
            self.repository.finalize_flow_cleanup,
            flow_id,
            operation_token,
        )

    @_execution_scoped("connector_lifecycle")
    async def cancel_auth_flow(self, flow_id: str) -> None:
        """Fence and scrub a flow that lost its Interaction handoff."""

        cleanup = await asyncio.to_thread(self.repository.cancel_auth_flow, flow_id)
        if cleanup is None:
            return
        claimed_flow_id, private_ref, operation_token = cleanup
        await asyncio.to_thread(self.vault.delete, private_ref)
        await asyncio.to_thread(
            self.repository.finalize_flow_cleanup,
            claimed_flow_id,
            operation_token,
        )

    async def _vault_put_cancellation_safe(
        self,
        reference: str,
        material: Mapping[str, str],
    ) -> None:
        self._assert_current_permit()
        put_task = asyncio.create_task(
            asyncio.to_thread(self.vault.put, reference, material)
        )
        try:
            await asyncio.shield(put_task)
        except asyncio.CancelledError:
            await put_task
            self._assert_current_permit()
            raise
        except BaseException:
            self._assert_current_permit()
            raise
        self._assert_current_permit()

    async def _complete_reauthorization(
        self,
        flow,
        *,
        account_subject: str,
        account_display_name: str,
        granted_scopes: frozenset[str],
        credential_material: Mapping[str, str],
    ) -> ConnectorInstance:
        instance_id = str(flow.reauthorize_instance_id)
        state = await asyncio.to_thread(
            self.repository.get_instance_state, instance_id
        )
        if state is None:
            raise ConnectorAuthError("connector instance no longer exists")
        current, lifecycle = state
        if (
            lifecycle != "active"
            or not current.enabled
            or current.connector_id != flow.connector_id
            or current.account_subject != account_subject
        ):
            raise ConnectorAuthError(
                "connector reauthorization account does not match the existing instance"
            )

        transition_id = "connreauth_" + uuid.uuid4().hex
        operation_token = "connreauthlease_" + uuid.uuid4().hex
        new_credential_ref = (
            f"ecorex/connectors/{instance_id}/reauth/{flow.flow_id}"
        )
        try:
            prepared = await asyncio.to_thread(
                self.repository.prepare_reauthorization,
                transition_id=transition_id,
                instance_id=instance_id,
                new_credential_ref=new_credential_ref,
                operation_token=operation_token,
                lease_seconds=300,
            )
        except (KeyError, sqlite3.IntegrityError, RuntimeError):
            raise ConnectorUnavailable(
                "connector reauthorization state changed concurrently"
            ) from None

        committed = False
        try:
            await self._vault_put_cancellation_safe(
                new_credential_ref,
                credential_material,
            )
            deadline = (
                asyncio.get_running_loop().time()
                + self.reauthorization_drain_timeout
            )
            while await asyncio.to_thread(
                self.repository.has_live_instance_operations,
                instance_id,
            ):
                if asyncio.get_running_loop().time() >= deadline:
                    raise ConnectorUnavailable(
                        "connector reauthorization is waiting for active operations"
                    )
                await asyncio.sleep(0.05)
            commit_task = asyncio.create_task(
                asyncio.to_thread(
                    self.repository.commit_reauthorization,
                    transition_id,
                    operation_token,
                    account_subject=account_subject,
                    account_display_name=account_display_name,
                    granted_scopes=granted_scopes,
                    lease_seconds=300,
                    auth_flow_id=flow.flow_id,
                    auth_connector_id=flow.connector_id,
                )
            )
            try:
                updated = await asyncio.shield(commit_task)
                committed = True
            except asyncio.CancelledError:
                updated = await commit_task
                committed = True
                raise
        except BaseException:
            if not committed:
                await asyncio.to_thread(self.vault.delete, new_credential_ref)
                await asyncio.to_thread(
                    self.repository.cancel_reauthorization,
                    transition_id,
                    operation_token,
                )
            raise

        try:
            await asyncio.to_thread(self.vault.delete, prepared.credential_ref)
            updated = await asyncio.to_thread(
                self.repository.finalize_reauthorization,
                transition_id,
                operation_token,
            )
        except Exception:
            # The database already points at the new credential. The durable
            # transition keeps the old opaque reference for maintenance cleanup;
            # returning the degraded projection avoids inviting a second swap.
            pass
        await self.publish_pending_best_effort()
        return updated

    @_execution_scoped("connector_lifecycle")
    async def refresh_health(
        self,
        instance_id: str,
        *,
        client_request_id: str | None = None,
    ) -> ConnectorInstance:
        request_id, reservation = await asyncio.to_thread(
            self._reserve_lifecycle_request,
            client_request_id,
            operation_kind="connector.health",
            fingerprint={"instance_id": instance_id},
        )
        if reservation.outcome == "replay":
            replay_instance_id = str(
                (reservation.result or {}).get("instance_id", "")
            )
            replayed = await asyncio.to_thread(
                self.repository.get_instance,
                replay_instance_id,
                include_transitional=True,
            )
            if replayed is None:
                raise ConnectorNotFound(
                    "connector instance no longer exists for the replayed health request"
                )
            return replayed
        assert reservation.lease_token is not None

        try:
            operation_id = "connop_" + uuid.uuid4().hex
            operation_lease_token = "connlease_" + uuid.uuid4().hex
            acquire_task = asyncio.create_task(
                asyncio.to_thread(
                    self.repository.acquire_instance_operation,
                    instance_id,
                    operation_kind="health",
                    operation_id=operation_id,
                    lease_token=operation_lease_token,
                    uncertainty_policy="auto_release",
                )
            )
            try:
                acquired = await asyncio.shield(acquire_task)
            except asyncio.CancelledError:
                acquired = await acquire_task
                if acquired is not None:
                    await asyncio.to_thread(
                        self.repository.release_instance_operation,
                        acquired[1],
                    )
                raise
            if acquired is None:
                await self._raise_instance_unavailable_async(instance_id)
            instance, lease = acquired
            async with self._operation_lease(lease):
                adapter = self.registry.adapter(instance.connector_id)
                try:
                    credentials = await asyncio.to_thread(
                        self.vault.get, instance.credential_ref
                    )
                    result = await self._call_adapter_bounded(
                        adapter.check_health, credentials
                    )
                    if not isinstance(result, ConnectorHealthResult):
                        raise ValueError("invalid health result")
                    error_code = _validated_health_error_code(
                        result.error_code, credentials
                    )
                except Exception:
                    await asyncio.to_thread(
                        self.repository.update_health,
                        instance_id,
                        health=ConnectorHealth.ERROR,
                        last_error_code="health_check_failed",
                        operation_lease=lease,
                        lifecycle_request=(request_id, reservation.lease_token),
                        lifecycle_failure_code=ConnectorUnavailable.code,
                    )
                    await self.publish_pending_best_effort()
                    raise ConnectorUnavailable("connector health check failed") from None
                updated = await asyncio.to_thread(
                    self.repository.update_health,
                    instance_id,
                    health=result.health,
                    last_error_code=error_code,
                    operation_lease=lease,
                    lifecycle_request=(request_id, reservation.lease_token),
                )
            await self.publish_pending_best_effort()
            return updated
        except ConnectorError as error:
            await asyncio.to_thread(
                self._fail_lifecycle_request, request_id, reservation, error.code
            )
            raise
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                await asyncio.to_thread(
                    self._fail_lifecycle_request,
                    request_id,
                    reservation,
                    ConnectorUnavailable.code,
                )
                raise
            if isinstance(error, Exception):
                await asyncio.to_thread(
                    self._fail_lifecycle_request,
                    request_id,
                    reservation,
                    ConnectorUnavailable.code,
                )
                raise ConnectorUnavailable("connector health check failed") from None
            raise

    @_execution_scoped("connector_invocation")
    async def invoke(
        self,
        instance_id: str,
        action_id: str,
        inputs: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        admin_hard_denies: frozenset[str] = frozenset(),
        admin_hard_denies_provider: Callable[[], frozenset[str]] | None = None,
        runtime_context: Mapping[str, str] | ConnectorInvocationContext | None = None,
        max_result_bytes: int | None = None,
    ) -> Any:
        normalized_runtime_context = _normalize_runtime_context(runtime_context)
        if normalized_runtime_context is not None and self._result_coordinator is None:
            raise ConnectorUnavailable(
                "model-facing Connector result Runtime is not bound"
            )
        if max_result_bytes is not None and (
            isinstance(max_result_bytes, bool)
            or not isinstance(max_result_bytes, int)
            or not 1024 <= max_result_bytes <= _MAX_ACTION_JSON_BYTES
        ):
            raise ValueError("connector result projection limit is invalid")
        operation_id = "connop_" + uuid.uuid4().hex
        operation_lease_token = "connlease_" + uuid.uuid4().hex
        acquire_task = asyncio.create_task(
            asyncio.to_thread(
                self.repository.acquire_instance_operation,
                instance_id,
                operation_kind="invoke",
                operation_id=operation_id,
                lease_token=operation_lease_token,
                uncertainty_policy="auto_release",
            )
        )
        try:
            acquired = await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            acquired = await acquire_task
            if acquired is not None:
                await asyncio.to_thread(
                    self.repository.release_instance_operation,
                    acquired[1],
                )
            raise
        if acquired is None:
            await self._raise_instance_unavailable_async(instance_id)
        instance, lease = acquired
        async with self._operation_lease(lease) as lease_guard:
            if instance.health not in {
                ConnectorHealth.CONNECTED,
                ConnectorHealth.DEGRADED,
            }:
                raise ConnectorUnavailable(
                    f"connector instance is {instance.health.value}"
                )
            definition = self.registry.definition(instance.connector_id)
            try:
                action = definition.action(action_id)
            except KeyError as exc:
                raise ConnectorNotFound(
                    f"unknown connector action: {action_id!r}"
                ) from exc
            current_denies = (
                admin_hard_denies_provider()
                if admin_hard_denies_provider is not None
                else frozenset()
            )
            deny_set = {
                str(value).casefold()
                for value in (*admin_hard_denies, *current_denies)
            }
            admission_policy_sha256 = hashlib.sha256(
                json.dumps(
                    sorted(deny_set),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if (
                instance.connector_id.casefold() in deny_set
                or action.action_id.casefold() in deny_set
            ):
                raise ConnectorPermissionDenied(
                    "connector action is blocked by administrator policy"
                )
            missing_scopes = action.required_scopes - instance.granted_scopes
            if missing_scopes:
                raise ConnectorPermissionDenied(
                    "connector grant is missing scopes: "
                    + ", ".join(sorted(missing_scopes))
                )
            try:
                validate_schema_instance(
                    dict(inputs),
                    action.input_schema,
                    label="connector action input",
                )
            except (SchemaInstanceError, TypeError, ValueError):
                # Provider implementations never receive data that failed the
                # backend-owned contract.  Keep the public error deliberately
                # generic so schema paths cannot become an oracle for hidden
                # adapter behavior.
                raise ConnectorInputInvalid(
                    "connector action input is invalid"
                ) from None
            _validate_idempotency_key(action.requires_idempotency_key, idempotency_key)
            input_digest = _json_digest(inputs, label="connector inputs")
            running_record = ConnectorInvocationRecord(
                invocation_id="conninvoke_" + uuid.uuid4().hex,
                instance_id=instance.instance_id,
                connector_id=instance.connector_id,
                action_id=action.action_id,
                input_sha256=input_digest,
                idempotency_key_sha256=(
                    hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
                    if idempotency_key
                    else None
                ),
                status="running",
                created_at=datetime.now(UTC),
                runtime_context=normalized_runtime_context,
                admission_policy_sha256=admission_policy_sha256,
            )
            reservation = await asyncio.to_thread(
                self.repository.reserve_invocation,
                running_record,
                operation_lease=lease,
                retain_on_uncertainty=_connector_effect_may_mutate(action.effects),
            )
            if reservation.outcome == "conflict":
                raise ConnectorIdempotencyConflict(
                    "idempotency key was reused with different connector inputs"
                )
            if reservation.outcome == "in_progress":
                if normalized_runtime_context is None:
                    raise ConnectorInvocationUncertain(
                        "matching Connector invocation is still in progress",
                        invocation_id=reservation.invocation_id,
                    )
                return await self._await_invocation_completion(
                    reservation.invocation_id,
                    runtime_context=normalized_runtime_context,
                )
            if reservation.outcome == "uncertain":
                raise ConnectorInvocationUncertain(
                    "the prior connector invocation may have completed; "
                    "manual reconciliation is required",
                    invocation_id=reservation.invocation_id,
                )
            if reservation.outcome == "staged":
                assert self._result_coordinator is not None
                staged_result = await asyncio.to_thread(
                    self._result_coordinator.finalize_staged,
                    reservation.invocation_id,
                )
                return staged_result
            if reservation.outcome == "replay":
                if normalized_runtime_context is not None:
                    await asyncio.to_thread(
                        self.repository.record_invocation_replay,
                        reservation.invocation_id,
                        normalized_runtime_context,
                    )
                return reservation.result

            adapter_tasks: list[asyncio.Task[Any]] = []
            dispatch_state = {"started": False}

            async def pre_dispatch() -> None:
                nonlocal running_record
                latest = (
                    admin_hard_denies_provider()
                    if admin_hard_denies_provider is not None
                    else frozenset()
                )
                latest_denies = {
                    str(value).casefold()
                    for value in (*admin_hard_denies, *latest)
                }
                latest_digest = hashlib.sha256(
                    json.dumps(
                        sorted(latest_denies),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                denied = (
                    instance.connector_id.casefold() in latest_denies
                    or action.action_id.casefold() in latest_denies
                )
                await asyncio.to_thread(
                    self.repository.refresh_invocation_admission,
                    running_record,
                    lease,
                    admission_policy_sha256=latest_digest,
                    denied=denied,
                )
                running_record = replace(
                    running_record,
                    admission_policy_sha256=latest_digest,
                )
                if denied:
                    raise ConnectorPermissionDenied(
                        "connector action is blocked by administrator policy"
                    )

            def provider_dispatched() -> None:
                # This callback runs in the event-loop task immediately before
                # handing control to the adapter.  It is intentionally later
                # than the policy/SQLite pre-dispatch fence: only this point
                # makes a missing provider acknowledgement uncertain.
                dispatch_state["started"] = True

            try:
                credentials = await asyncio.to_thread(
                    self.vault.get, instance.credential_ref
                )
                result = await self._call_adapter_bounded(
                    self.registry.adapter(instance.connector_id).invoke,
                    action_id=action.action_id,
                    inputs=inputs,
                    credentials=credentials,
                    idempotency_key=idempotency_key,
                    _background_task_sink=adapter_tasks.append,
                    _pre_dispatch=pre_dispatch,
                    _on_dispatch=provider_dispatched,
                )
            except BaseException as exc:
                mutating = _connector_effect_may_mutate(action.effects)
                dispatched_task = adapter_tasks[-1] if adapter_tasks else None
                if not dispatch_state["started"]:
                    if dispatched_task is not None and not dispatched_task.done():
                        dispatched_task.cancel()
                        try:
                            await dispatched_task
                        except BaseException:
                            pass
                    await asyncio.to_thread(
                        self.repository.abort_invocation_before_dispatch,
                        running_record,
                        lease,
                    )
                elif mutating and dispatched_task is not None:
                    await asyncio.to_thread(
                        self.repository.mark_invocation_operation_unknown,
                        running_record,
                        lease,
                        adapter_running=not dispatched_task.done(),
                    )
                    lease_guard.retain()
                    if not dispatched_task.done():
                        # A late provider result outlives the Worker lease that
                        # initiated it.  Start its durable finalizer in a clean
                        # context so it cannot inherit that now-stale Job commit
                        # guard; the watcher obtains its own Runtime control
                        # admission before performing any local mutation.
                        watcher = asyncio.create_task(
                            self._finish_uncertain_adapter_task(
                                dispatched_task,
                                lease,
                                running_record,
                                credentials,
                                action.output_schema,
                                requested_name=(
                                    f"{definition.display_name}_{action.display_name}_结果.json"
                                ),
                                created_by_tool_id=(
                                    "connector_read"
                                    if action.effects == frozenset({ConnectorEffect.READ})
                                    else "connector_write"
                                ),
                            ),
                            context=Context(),
                        )
                        self._uncertain_watchers.add(watcher)
                        watcher.add_done_callback(self._uncertain_watchers.discard)
                else:
                    await asyncio.to_thread(
                        self.repository.mark_invocation_unknown,
                        running_record,
                    )
                if isinstance(exc, ConnectorPermissionDenied):
                    raise
                if isinstance(exc, Exception):
                    if (
                        mutating
                        and dispatched_task is not None
                        and dispatch_state["started"]
                    ):
                        raise ConnectorInvocationUncertain(
                            "connector write outcome is unknown; "
                            "manual reconciliation is required",
                            invocation_id=running_record.invocation_id,
                        ) from None
                    raise ConnectorUnavailable("connector invocation failed") from None
                raise
            try:
                invocation_committed = False
                result_unavailable = False
                try:
                    _assert_result_is_persistable_and_secret_free(
                        result,
                        credentials,
                        output_schema=action.output_schema,
                    )
                    encoded_result = _encoded_connector_result(result)
                except _RejectedConnectorResult as rejection:
                    if normalized_runtime_context is None:
                        raise
                    assert self._result_coordinator is not None
                    completion_task = asyncio.create_task(
                        asyncio.to_thread(
                            self._result_coordinator.complete_unavailable,
                            running_record,
                            lease,
                            error_code=rejection.error_code,
                            requested_name=(
                                f"{definition.display_name}_{action.display_name}_结果.json"
                            ),
                            created_by_tool_id=(
                                "connector_read"
                                if action.effects == frozenset({ConnectorEffect.READ})
                                else "connector_write"
                            ),
                            completion_path="provider_result",
                        )
                    )
                    try:
                        completed_result = await asyncio.shield(completion_task)
                        invocation_committed = True
                    except asyncio.CancelledError:
                        completed_result = await completion_task
                        invocation_committed = True
                        raise
                    result = completed_result
                    result_unavailable = True

                if result_unavailable:
                    pass
                elif normalized_runtime_context is not None:
                    assert self._result_coordinator is not None
                    created_by_tool_id: Literal[
                        "connector_read", "connector_write"
                    ] = (
                        "connector_read"
                        if action.effects == frozenset({ConnectorEffect.READ})
                        else "connector_write"
                    )
                    completion_task = asyncio.create_task(
                        asyncio.to_thread(
                            self._result_coordinator.complete_result,
                            running_record,
                            lease,
                            result=result,
                            encoded_result=encoded_result,
                            requested_name=(
                                f"{definition.display_name}_{action.display_name}_结果.json"
                            ),
                            created_by_tool_id=created_by_tool_id,
                            completion_path="provider_result",
                        )
                    )
                else:
                    if (
                        max_result_bytes is not None
                        and len(encoded_result) > max_result_bytes
                    ):
                        raise ConnectorUnavailable(
                            "connector result exceeds the direct response limit"
                        )
                    completion_task = asyncio.create_task(
                        asyncio.to_thread(
                            self.repository.complete_invocation,
                            running_record,
                            result=result,
                            operation_lease=lease,
                        )
                    )
                try:
                    completed_result = await asyncio.shield(completion_task)
                    invocation_committed = True
                except asyncio.CancelledError:
                    completed_result = await completion_task
                    invocation_committed = True
                    raise
                if normalized_runtime_context is not None:
                    result = completed_result
            except BaseException as exc:
                if "invocation_committed" in locals() and invocation_committed:
                    raise
                mutating = _connector_effect_may_mutate(action.effects)
                if normalized_runtime_context is not None:
                    await asyncio.to_thread(
                        self.repository.mark_invocation_operation_unknown,
                        running_record,
                        lease,
                        adapter_running=False,
                    )
                    lease_guard.retain()
                elif mutating:
                    await asyncio.to_thread(
                        self.repository.mark_invocation_operation_unknown,
                        running_record,
                        lease,
                        adapter_running=False,
                    )
                    lease_guard.retain()
                else:
                    await asyncio.to_thread(
                        self.repository.mark_invocation_unknown,
                        running_record,
                    )
                if isinstance(exc, Exception):
                    if _connector_effect_may_mutate(action.effects):
                        raise ConnectorInvocationUncertain(
                            "connector write result was rejected; "
                            "manual reconciliation is required",
                            invocation_id=running_record.invocation_id,
                        ) from None
                    raise ConnectorUnavailable(
                        "connector result failed validation"
                    ) from None
                raise

            completed_record = ConnectorInvocationRecord(
                invocation_id=running_record.invocation_id,
                instance_id=running_record.instance_id,
                connector_id=running_record.connector_id,
                action_id=running_record.action_id,
                input_sha256=running_record.input_sha256,
                idempotency_key_sha256=running_record.idempotency_key_sha256,
                status="completed",
                created_at=running_record.created_at,
                runtime_context=running_record.runtime_context,
                admission_policy_sha256=running_record.admission_policy_sha256,
            )
            if self.audit_sink is not None:
                await asyncio.to_thread(self.audit_sink, completed_record)
        await self.publish_pending_best_effort()
        return result

    async def _await_invocation_completion(
        self,
        invocation_id: str,
        *,
        runtime_context: ConnectorInvocationContext | None,
    ) -> Any:
        deadline = asyncio.get_running_loop().time() + min(
            300.0, self.adapter_timeout_seconds + 5.0
        )
        while True:
            state = await asyncio.to_thread(
                self.repository.invocation_completion_state,
                invocation_id,
            )
            if state.outcome == "replay":
                if runtime_context is not None:
                    await asyncio.to_thread(
                        self.repository.record_invocation_replay,
                        invocation_id,
                        runtime_context,
                    )
                return state.result
            if state.outcome == "staged":
                if self._result_coordinator is None:
                    raise ConnectorUnavailable(
                        "model-facing Connector result Runtime is not bound"
                    )
                return await asyncio.to_thread(
                    self._result_coordinator.finalize_staged,
                    invocation_id,
                )
            if state.outcome == "uncertain":
                raise ConnectorInvocationUncertain(
                    "the matching Connector invocation outcome is unknown",
                    invocation_id=invocation_id,
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise ConnectorUnavailable(
                    "matching Connector invocation is still in progress"
                )
            await asyncio.sleep(0.02)

    @_execution_scoped("connector_lifecycle")
    async def disconnect(
        self,
        instance_id: str,
        *,
        drain_timeout: float = 30.0,
        client_request_id: str | None = None,
    ) -> None:
        await asyncio.to_thread(self._recover_transitional_state)
        if drain_timeout <= 0:
            raise ValueError("connector drain timeout must be positive")
        request_id, reservation = await asyncio.to_thread(
            self._reserve_lifecycle_request,
            client_request_id,
            operation_kind="connector.disconnect",
            fingerprint={"instance_id": instance_id},
        )
        if reservation.outcome == "replay":
            return
        assert reservation.lease_token is not None
        lifecycle_request = (request_id, reservation.lease_token)

        try:
            await self._continue_disconnect(
                instance_id,
                drain_timeout=drain_timeout,
                lifecycle_request=lifecycle_request,
            )
            await self.publish_pending_best_effort()
        except ConnectorError as error:
            await asyncio.to_thread(
                self._fail_lifecycle_request, request_id, reservation, error.code
            )
            raise
        except BaseException as error:
            if isinstance(error, Exception):
                await asyncio.to_thread(
                    self._fail_lifecycle_request,
                    request_id,
                    reservation,
                    ConnectorUnavailable.code,
                )
                raise ConnectorUnavailable("connector disconnect failed") from None
            raise

    async def _continue_disconnect(
        self,
        instance_id: str,
        *,
        drain_timeout: float,
        lifecycle_request: tuple[str, str] | None,
    ) -> None:
        """Advance one durable disconnect saga without creating a request row.

        User requests wrap this in their own idempotent lifecycle request;
        maintenance uses it directly after restart. Provider revocation is
        protected by the repository's durable per-instance claim and a stable
        provider idempotency key.
        """

        if await asyncio.to_thread(
            self.repository.has_pending_reauthorization, instance_id
        ):
            raise ConnectorUnavailable(
                "connector reauthorization cleanup is still pending"
            )
        state = await asyncio.to_thread(
            self.repository.get_instance_state, instance_id
        )
        if state is None:
            raise ConnectorNotFound(f"unknown connector instance: {instance_id!r}")
        instance, lifecycle = state
        if lifecycle == "disconnecting":
            claimed = await asyncio.to_thread(
                self.repository.claim_disconnect_cleanup, instance_id
            )
            if claimed is None:
                raise ConnectorUnavailable(
                    "connector credential cleanup is already pending"
                )
            instance, transition_token = claimed
            await asyncio.to_thread(self.vault.delete, instance.credential_ref)
            await asyncio.to_thread(
                self.repository.finalize_disconnect,
                instance_id,
                transition_token,
                lifecycle_request=lifecycle_request,
            )
            return

        adapter = self.registry.adapter(instance.connector_id)
        if not isinstance(adapter, RevocableConnectorAdapter):
            raise ConnectorUnavailable(
                "connector does not support provider-side authorization revocation"
            )
        instance = await asyncio.to_thread(
            self.repository.begin_draining, instance_id
        )
        if instance is None:
            raise ConnectorNotFound(f"unknown connector instance: {instance_id!r}")
        deadline = asyncio.get_running_loop().time() + drain_timeout
        while await asyncio.to_thread(
            self.repository.has_live_instance_operations, instance_id
        ):
            if asyncio.get_running_loop().time() >= deadline:
                raise ConnectorUnavailable(
                    "connector is still draining active operations"
                )
            await asyncio.sleep(0.05)
        claimed_revocation = await asyncio.to_thread(
            self.repository.claim_revocation,
            instance_id,
            lease_seconds=max(30, math.ceil(self.adapter_timeout_seconds + 5)),
        )
        if claimed_revocation is None:
            raise ConnectorUnavailable("connector revocation is already in progress")
        instance, transition_token = claimed_revocation
        try:
            credentials = await asyncio.to_thread(
                self.vault.get, instance.credential_ref
            )
            revoked = await self._call_adapter_bounded(
                adapter.revoke,
                credentials=credentials,
                idempotency_key=f"ecorex-disconnect:{instance_id}",
            )
            if revoked is not True:
                raise RuntimeError("provider did not confirm revocation")
        except Exception:
            await asyncio.to_thread(
                self.repository.mark_revocation_uncertain,
                instance_id,
                transition_token=transition_token,
                lifecycle_request=lifecycle_request,
            )
            raise ConnectorInvocationUncertain(
                "provider authorization revocation is uncertain; credentials were retained"
            ) from None

        await asyncio.to_thread(
            self.repository.mark_remote_revoked,
            instance_id,
            transition_token=transition_token,
            lease_seconds=300,
        )
        await asyncio.to_thread(self.vault.delete, instance.credential_ref)
        await asyncio.to_thread(
            self.repository.finalize_disconnect,
            instance_id,
            transition_token,
            lifecycle_request=lifecycle_request,
        )

    def drain_outbox(self, *, limit: int = 100) -> int:
        """Request one serialized outbox drain without losing busy nudges.

        A busy caller does not wait behind publisher I/O. It advances the
        requested generation and returns; the current owner is obliged to run
        another scan before it can transition to idle.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("connector outbox drain limit is invalid")
        with self._execution_scope(
            scope="connector_maintenance",
            subject="drain_outbox",
        ):
            _generation, owns_drain, stuck = self._request_outbox_drain()
            if not owns_drain or stuck:
                return 0
            return self._run_outbox_drain_owner(limit=limit)

    def flush_pending_outbox(
        self,
        *,
        timeout_seconds: float = 5.0,
        limit: int = 100,
    ) -> int:
        """Boundedly converge all currently pending Connector event intents.

        This method is the shutdown barrier. It never reports success while a
        publisher thread is stuck or while a durable, non-dead-letter event is
        still pending. On timeout the row and its immutable ``event_id`` remain
        recoverable for the next healthy maintenance pass or process restart.
        """

        if not 0.05 <= timeout_seconds <= 120:
            raise ValueError("connector outbox flush timeout is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("connector outbox flush limit is invalid")
        if self.outbox_publisher is None:
            return 0
        deadline = time.monotonic() + timeout_seconds
        published = 0
        with self._execution_scope(
            scope="connector_maintenance",
            subject="flush_pending_outbox",
        ):
            while True:
                generation, owns_drain, stuck = self._request_outbox_drain()
                if stuck:
                    raise ConnectorUnavailable("connector outbox publisher is stuck")
                if owns_drain:
                    published += self._run_outbox_drain_owner(
                        limit=limit,
                        deadline=deadline,
                    )
                else:
                    self._wait_for_outbox_generation(generation, deadline=deadline)
                if self.repository.pending_outbox_count() == 0:
                    return published
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectorUnavailable("connector outbox flush timed out")
                # Pending rows may still carry a short retry/lease deadline.
                # Sleep outside the condition and all SQLite transactions.
                time.sleep(min(0.05, remaining))

    async def flush_pending_outbox_async(
        self,
        *,
        timeout_seconds: float = 5.0,
        limit: int = 100,
    ) -> int:
        """Run the bounded shutdown flush outside asyncio's default executor."""

        return await _run_sync_daemon(
            self.flush_pending_outbox,
            timeout_seconds=timeout_seconds,
            limit=limit,
            thread_name="ecorex-connector-outbox-flush",
        )

    def outbox_delivery_health(self) -> ConnectorOutboxDeliveryHealth:
        # Seqlock the in-memory single-flight state around the durable read.
        # The database query deliberately runs without the Condition so a
        # publisher completing its SQLite transaction never waits behind an
        # observability read. If generation/active/stuck changes while pending
        # is sampled, discard the mixed view and retry.
        for _attempt in range(16):
            with self._outbox_condition:
                before, _stuck_before = self._outbox_state_locked()
            pending = self.repository.pending_outbox_count()
            with self._outbox_condition:
                after, stuck = self._outbox_state_locked()
                if before == after:
                    return self._outbox_health_from_locked_state(
                        pending=pending,
                        state=after,
                        stuck=stuck,
                    )
        # Sustained churn is unusual but must not make health collection
        # unbounded. The fallback takes one unified fence; Connector writers
        # release SQLite transactions before acquiring this Condition.
        with self._outbox_condition:
            pending = self.repository.pending_outbox_count()
            state, stuck = self._outbox_state_locked()
            return self._outbox_health_from_locked_state(
                pending=pending,
                state=state,
                stuck=stuck,
            )

    def _outbox_state_locked(
        self,
    ) -> tuple[tuple[int, int, bool, int | None, str | None], _OutboxPublishAttempt | None]:
        stuck = self._live_stuck_attempt_locked()
        return (
            (
                self._outbox_requested_generation,
                self._outbox_completed_generation,
                self._outbox_drain_active,
                id(stuck) if stuck is not None else None,
                self._outbox_last_error_code,
            ),
            stuck,
        )

    def _outbox_health_from_locked_state(
        self,
        *,
        pending: int,
        state: tuple[int, int, bool, int | None, str | None],
        stuck: _OutboxPublishAttempt | None,
    ) -> ConnectorOutboxDeliveryHealth:
        requested, completed, active, _stuck_identity, last_error = state
        status: Literal["disabled", "idle", "draining", "degraded", "stuck"]
        if stuck is not None:
            status = "stuck"
        elif active:
            status = "draining"
        elif pending or last_error is not None:
            status = "degraded"
        elif self.outbox_publisher is None:
            status = "disabled"
        else:
            status = "idle"
        return ConnectorOutboxDeliveryHealth(
            status=status,
            pending=pending,
            requested_generation=requested,
            completed_generation=completed,
            active=active,
            stuck_event_id=(stuck.event.event_id if stuck is not None else None),
            last_error_code=last_error,
        )

    def _request_outbox_drain(self) -> tuple[int, bool, bool]:
        with self._outbox_condition:
            self._outbox_requested_generation += 1
            generation = self._outbox_requested_generation
            if self._live_stuck_attempt_locked() is not None:
                self._outbox_last_error_code = "connector_outbox_publish_stuck"
                self._outbox_condition.notify_all()
                return generation, False, True
            if self._outbox_drain_active:
                self._outbox_condition.notify_all()
                return generation, False, False
            self._outbox_drain_active = True
            self._outbox_condition.notify_all()
            return generation, True, False

    def _run_outbox_drain_owner(
        self,
        *,
        limit: int,
        deadline: float | None = None,
    ) -> int:
        published = 0
        deadline_token = self._outbox_deadline_context.set(deadline)
        try:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    raise ConnectorUnavailable("connector outbox flush timed out")
                with self._outbox_condition:
                    generation = self._outbox_requested_generation
                    stuck = self._live_stuck_attempt_locked()
                if stuck is None:
                    published += self._drain_outbox_locked(limit=limit)
                with self._outbox_condition:
                    self._outbox_completed_generation = max(
                        self._outbox_completed_generation,
                        generation,
                    )
                    stuck = self._live_stuck_attempt_locked()
                    if stuck is not None:
                        self._outbox_last_error_code = "connector_outbox_publish_stuck"
                        self._outbox_drain_active = False
                        self._outbox_condition.notify_all()
                        return published
                    if self._outbox_requested_generation > generation:
                        # A mutation/nudge landed during the previous scan.
                        # Stay owner and consume that generation before idle.
                        continue
                    self._outbox_drain_active = False
                    if self.repository.pending_outbox_count() == 0:
                        self._outbox_last_error_code = None
                    self._outbox_condition.notify_all()
                    return published
        except BaseException:
            with self._outbox_condition:
                self._outbox_drain_active = False
                if self._outbox_last_error_code is None:
                    self._outbox_last_error_code = "connector_outbox_drain_failed"
                self._outbox_condition.notify_all()
            raise
        finally:
            self._outbox_deadline_context.reset(deadline_token)

    def _wait_for_outbox_generation(self, generation: int, *, deadline: float) -> None:
        with self._outbox_condition:
            while self._outbox_completed_generation < generation:
                if self._live_stuck_attempt_locked() is not None:
                    raise ConnectorUnavailable("connector outbox publisher is stuck")
                if not self._outbox_drain_active:
                    # The prior owner failed between generations. The caller's
                    # next loop iteration will become the replacement owner.
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectorUnavailable("connector outbox flush timed out")
                self._outbox_condition.wait(timeout=remaining)

    def _live_stuck_attempt_locked(self) -> _OutboxPublishAttempt | None:
        attempt = self._outbox_stuck_attempt
        if attempt is not None and attempt.done.is_set():
            self._outbox_stuck_attempt = None
            return None
        return attempt

    def _drain_outbox_locked(self, *, limit: int) -> int:
        if self.outbox_publisher is None:
            return 0
        published = 0
        processed = 0
        while processed < limit:
            deadline = self._outbox_deadline_context.get()
            if deadline is not None and time.monotonic() >= deadline:
                break
            claimed = self.repository.claim_outbox(limit=1, lease_seconds=30)
            if not claimed:
                break
            event = claimed[0]
            processed += 1
            attempt = _OutboxPublishAttempt(
                event=event,
                permit=self._execution_permit_context.get(),
                done=threading.Event(),
                heartbeat_stop=threading.Event(),
            )
            heartbeat = threading.Thread(
                target=self._outbox_heartbeat,
                args=(
                    event,
                    attempt.heartbeat_stop,
                    attempt.permit,
                ),
                daemon=True,
                name="ecorex-connector-outbox-heartbeat",
            )
            heartbeat.start()
            publisher = threading.Thread(
                target=self._run_outbox_publish_attempt,
                args=(attempt,),
                daemon=True,
                name="ecorex-connector-outbox-publisher",
            )
            publisher.start()
            publish_wait = self.outbox_publish_timeout_seconds
            if deadline is not None:
                publish_wait = min(
                    publish_wait,
                    max(0.001, deadline - time.monotonic()),
                )
            finished = attempt.done.wait(publish_wait)
            if not finished:
                # Python cannot safely kill a blocked integration thread. Stop
                # renewing its lease, open one observable circuit, release the
                # logical single-flight owner, and retain the durable row.
                attempt.heartbeat_stop.set()
                heartbeat.join(timeout=self._outbox_join_budget())
                with self._outbox_condition:
                    if not attempt.done.is_set():
                        self._outbox_stuck_attempt = attempt
                        self._outbox_last_error_code = (
                            "connector_outbox_publish_stuck"
                        )
                    self._outbox_condition.notify_all()
                if not attempt.done.is_set():
                    break
            attempt.heartbeat_stop.set()
            heartbeat.join(timeout=self._outbox_join_budget())
            if attempt.marked_published:
                published += 1
                continue
            if attempt.terminal_recorded:
                continue
            # Sink success without a matching outbox commit, a closed Runtime
            # epoch, or a lost lease is uncertain. Keep the row for event-id
            # deduplicated recovery and stop this scan.
            break
        return published

    def _outbox_join_budget(self) -> float:
        deadline = self._outbox_deadline_context.get()
        if deadline is None:
            return 2.0
        return max(0.0, min(2.0, deadline - time.monotonic()))

    def _run_outbox_publish_attempt(self, attempt: _OutboxPublishAttempt) -> None:
        token = self._execution_permit_context.set(attempt.permit)
        try:
            if attempt.permit is None:
                self._execute_outbox_publish_attempt(attempt)
            else:
                with transaction_commit_guard(
                    lambda: self._assert_permit(attempt.permit)
                ):
                    self._execute_outbox_publish_attempt(attempt)
        finally:
            attempt.heartbeat_stop.set()
            self._execution_permit_context.reset(token)
            attempt.done.set()
            with self._outbox_condition:
                if attempt.marked_published:
                    self._outbox_last_error_code = None
                elif attempt.error_code is not None:
                    self._outbox_last_error_code = attempt.error_code
                if self._outbox_stuck_attempt is attempt:
                    self._outbox_stuck_attempt = None
                self._outbox_condition.notify_all()

    def _execute_outbox_publish_attempt(self, attempt: _OutboxPublishAttempt) -> None:
        assert self.outbox_publisher is not None
        try:
            self._assert_current_permit()
            result = self.outbox_publisher(attempt.event)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("connector outbox publisher must be synchronous")
            attempt.sink_succeeded = True
            self._assert_current_permit()
            self.repository.mark_outbox_published(
                attempt.event.event_id,
                attempt.event.lease_token,
            )
            attempt.marked_published = True
            attempt.terminal_recorded = True
        except Exception:
            if attempt.sink_succeeded:
                # External/EventStore success followed by a lost local commit is
                # outcome-unknown. Never schedule an eager second delivery.
                attempt.error_code = "connector_outbox_publish_commit_uncertain"
                return
            try:
                self.repository.fail_outbox(
                    attempt.event.event_id,
                    attempt.event.lease_token,
                    attempts=attempt.event.attempts,
                )
            except Exception:
                attempt.error_code = "connector_outbox_failure_commit_failed"
                return
            attempt.error_code = "connector_outbox_publish_failed"
            attempt.terminal_recorded = True
        except BaseException:
            attempt.error_code = "connector_outbox_publish_interrupted"

    def _outbox_heartbeat(
        self,
        event: ConnectorOutboxEvent,
        stop: threading.Event,
        permit: RuntimeExecutionPermit | None,
    ) -> None:
        token = self._execution_permit_context.set(permit)
        try:
            while not stop.wait(5):
                try:
                    self._assert_current_permit()
                    if not self.repository.renew_outbox(
                        event.event_id,
                        event.lease_token,
                        lease_seconds=30,
                    ):
                        return
                    self._assert_current_permit()
                except Exception:
                    return
        finally:
            self._execution_permit_context.reset(token)

    @asynccontextmanager
    async def _operation_lease(
        self,
        lease: ConnectorOperationLease,
    ) -> AsyncIterator[_OperationLeaseGuard]:
        stop = asyncio.Event()
        guard = _OperationLeaseGuard()
        heartbeat = asyncio.create_task(self._operation_heartbeat(lease, stop))
        try:
            yield guard
        finally:
            stop.set()
            async def finish_lease() -> None:
                await heartbeat
                if not guard.retained:
                    await asyncio.to_thread(
                        self.repository.release_instance_operation, lease
                    )

            cleanup_task = asyncio.create_task(finish_lease())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise

    async def _finish_uncertain_adapter_task(
        self,
        task: asyncio.Task[Any],
        lease: ConnectorOperationLease,
        record: ConnectorInvocationRecord,
        credentials: Mapping[str, str],
        output_schema: Mapping[str, Any],
        *,
        requested_name: str,
        created_by_tool_id: Literal["connector_read", "connector_write"],
    ) -> None:
        with self.control_admission(
            operation="finalize_late_invocation",
            subject=record.invocation_id,
        ):
            await self._finish_uncertain_adapter_task_admitted(
                task,
                lease,
                record,
                credentials,
                output_schema,
                requested_name=requested_name,
                created_by_tool_id=created_by_tool_id,
            )

    async def _finish_uncertain_adapter_task_admitted(
        self,
        task: asyncio.Task[Any],
        lease: ConnectorOperationLease,
        record: ConnectorInvocationRecord,
        credentials: Mapping[str, str],
        output_schema: Mapping[str, Any],
        *,
        requested_name: str,
        created_by_tool_id: Literal["connector_read", "connector_write"],
    ) -> None:
        while not task.done():
            done, _pending = await asyncio.wait({task}, timeout=5)
            if done:
                break
            try:
                renewed = await asyncio.to_thread(
                    self.repository.renew_instance_operation,
                    lease,
                    lease_seconds=30,
                )
            except Exception:
                return
            if not renewed:
                return
        try:
            result = await task
            try:
                _assert_result_is_persistable_and_secret_free(
                    result,
                    credentials,
                    output_schema=output_schema,
                )
                encoded_result = _encoded_connector_result(result)
            except _RejectedConnectorResult as rejection:
                if record.runtime_context is None or self._result_coordinator is None:
                    raise
                await asyncio.to_thread(
                    self._result_coordinator.complete_unavailable,
                    record,
                    lease,
                    error_code=rejection.error_code,
                    requested_name=requested_name,
                    created_by_tool_id=created_by_tool_id,
                    completion_path="late_provider_result",
                )
                if self.audit_sink is not None:
                    await asyncio.to_thread(
                        self.audit_sink,
                        replace(record, status="completed"),
                    )
                return
            if record.runtime_context is not None:
                if self._result_coordinator is None:
                    raise ConnectorUnavailable(
                        "model-facing Connector result Runtime is not bound"
                    )
                await asyncio.to_thread(
                    self._result_coordinator.complete_result,
                    record,
                    lease,
                    result=result,
                    encoded_result=encoded_result,
                    requested_name=requested_name,
                    created_by_tool_id=created_by_tool_id,
                    completion_path="late_provider_result",
                )
            else:
                await asyncio.to_thread(
                    self.repository.complete_late_invocation,
                    record,
                    result=result,
                    operation_lease=lease,
                )
            if self.audit_sink is not None:
                await asyncio.to_thread(
                    self.audit_sink,
                    replace(record, status="completed"),
                )
            return
        except BaseException:
            pass
        try:
            await asyncio.to_thread(
                self.repository.mark_operation_outcome_unknown,
                lease,
            )
        except Exception:
            # If Runtime dies or the watcher loses its fence, lease expiry is
            # independently promoted to outcome_unknown by repository recovery.
            return

    async def _operation_heartbeat(
        self,
        lease: ConnectorOperationLease,
        stop: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
                return
            except TimeoutError:
                try:
                    renewed = await asyncio.to_thread(
                        self.repository.renew_instance_operation,
                        lease,
                        lease_seconds=30,
                    )
                except Exception:
                    return
                if not renewed:
                    return

    def _raise_instance_unavailable(self, instance_id: str) -> None:
        state = self.repository.get_instance_state(instance_id)
        if state is None:
            raise ConnectorNotFound(f"unknown connector instance: {instance_id!r}")
        raise ConnectorUnavailable("connector instance is not accepting new operations")

    async def _raise_instance_unavailable_async(self, instance_id: str) -> None:
        await asyncio.to_thread(self._raise_instance_unavailable, instance_id)

    def _recover_transitional_state(self) -> None:
        self.repository.recover_expired_operation_leases()
        self.repository.recover_expired_interaction_logins()
        for reference in self.repository.recovery_references():
            try:
                self.vault.delete(reference.credential_ref)
                if reference.kind == "flow":
                    self.repository.finalize_flow_cleanup(
                        reference.record_id, reference.recovery_token
                    )
                elif reference.kind == "pending_instance":
                    self.repository.remove_pending_instance(
                        reference.record_id, reference.recovery_token
                    )
                else:
                    self.repository.finalize_disconnect(
                        reference.record_id, reference.recovery_token
                    )
            except Exception:
                # The opaque DB pointer is the recovery authority. One
                # transient Keychain failure must not prevent Runtime startup
                # or skip unrelated recovery work; a later maintenance cycle
                # reclaims the expired recovery lease.
                try:
                    self.repository.record_recovery_deferred(
                        recovery_kind=reference.kind,
                        record_id=reference.record_id,
                    )
                except Exception:
                    pass
                continue
        for transition in self.repository.claim_reauthorization_recovery():
            try:
                self.vault.delete(transition.cleanup_ref)
                if transition.status == "preparing":
                    self.repository.cancel_reauthorization(
                        transition.transition_id,
                        transition.recovery_token,
                    )
                else:
                    self.repository.finalize_reauthorization(
                        transition.transition_id,
                        transition.recovery_token,
                    )
            except Exception:
                try:
                    self.repository.record_recovery_deferred(
                        recovery_kind=f"reauthorization_{transition.status}",
                        record_id=transition.transition_id,
                    )
                except Exception:
                    pass
                continue

    @_execution_scoped("connector_maintenance")
    async def maintenance_once(self) -> None:
        """Recover expired transition leases and retry the durable outbox."""

        await _run_sync_daemon(
            self._maintenance_once_blocking,
            thread_name="ecorex-connector-maintenance-pass",
        )
        candidates = await asyncio.to_thread(
            self.repository.list_disconnect_recovery_candidates
        )
        recovered_any = False
        for instance_id, lifecycle in candidates:
            try:
                await self._continue_disconnect(
                    instance_id,
                    drain_timeout=min(5.0, self.reauthorization_drain_timeout),
                    lifecycle_request=None,
                )
                recovered_any = True
            except asyncio.CancelledError:
                raise
            except Exception:
                try:
                    await asyncio.to_thread(
                        self.repository.record_recovery_deferred,
                        recovery_kind=f"disconnect_{lifecycle}",
                        record_id=instance_id,
                    )
                except Exception:
                    pass
        if recovered_any:
            await self.publish_pending_best_effort()

    def _maintenance_once_blocking(self) -> None:
        if not self._maintenance_cycle_lock.acquire(blocking=False):
            return
        try:
            self._recover_transitional_state()
            if self._result_coordinator is not None:
                self._result_coordinator.recover_pending()
            if self.outbox_publisher is not None:
                self.drain_outbox()
        finally:
            self._maintenance_cycle_lock.release()

    @_execution_scoped("connector_maintenance")
    async def publish_pending_best_effort(self, *, wait_seconds: float = 2.0) -> None:
        """Nudge durable event delivery without putting publisher I/O on ASGI.

        The outbox is already committed before this method runs. A slow
        synchronous sink may continue in its one serialized worker thread; the
        request is released after ``wait_seconds`` and maintenance retries the
        same immutable event later.
        """

        if self.outbox_publisher is None:
            return
        task = asyncio.create_task(
            _run_sync_daemon(
                self._publish_best_effort,
                thread_name="ecorex-connector-outbox-nudge",
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
        except TimeoutError:
            task.add_done_callback(_consume_background_task)

    def _adapter_limiter_for_running_loop(self) -> asyncio.BoundedSemaphore:
        loop = asyncio.get_running_loop()
        if self._adapter_loop is not loop or self._adapter_limiter is None:
            self._adapter_loop = loop
            self._adapter_limiter = asyncio.BoundedSemaphore(
                self.max_concurrent_adapter_calls
            )
        return self._adapter_limiter

    def _auth_limiter_for_running_loop(
        self,
        connector_id: str,
    ) -> asyncio.BoundedSemaphore:
        loop = asyncio.get_running_loop()
        if self._auth_limiter_loop is not loop:
            self._auth_limiter_loop = loop
            self._auth_limiters = {}
        limiter = self._auth_limiters.get(connector_id)
        if limiter is None:
            limiter = asyncio.BoundedSemaphore(2)
            self._auth_limiters[connector_id] = limiter
        return limiter

    async def _consumed_flow_heartbeat(
        self,
        flow,
        stop: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
                return
            except TimeoutError:
                try:
                    renewed = await asyncio.to_thread(
                        self.repository.renew_consumed_flow,
                        flow.flow_id,
                        flow.operation_token,
                        lease_seconds=300,
                    )
                except Exception:
                    return
                if not renewed:
                    return

    async def _call_adapter_bounded(
        self,
        callable_: Callable[..., Any],
        /,
        *args: Any,
        _background_task_sink: Callable[[asyncio.Task[Any]], None] | None = None,
        _pre_dispatch: Callable[[], Any] | None = None,
        _on_dispatch: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> Any:
        limiter = self._adapter_limiter_for_running_loop()
        loop = asyncio.get_running_loop()
        provider_dispatched: asyncio.Future[None] = loop.create_future()

        async def invoke() -> Any:
            async with limiter:
                if _pre_dispatch is not None:
                    prepared = _pre_dispatch()
                    if inspect.isawaitable(prepared):
                        await prepared
                self._assert_current_permit()
                if _on_dispatch is not None:
                    _on_dispatch()
                if not provider_dispatched.done():
                    provider_dispatched.set_result(None)
                try:
                    result = await _call_adapter(callable_, *args, **kwargs)
                except BaseException:
                    self._assert_current_permit()
                    raise
                self._assert_current_permit()
                return result

        task = asyncio.create_task(invoke())
        try:
            # Queueing and the final local admission fence must not consume the
            # provider's response budget.  They have their own bounded phase,
            # after which a call that never reached the adapter remains a known
            # pre-dispatch failure.  Once ``provider_dispatched`` wins, any
            # timeout is conservatively retained as an uncertain external
            # write and finalized by the late-result watcher.
            done, _pending = await asyncio.wait(
                {task, provider_dispatched},
                timeout=max(
                    _ADAPTER_ADMISSION_TIMEOUT_FLOOR_SECONDS,
                    self.adapter_timeout_seconds,
                ),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return await task
            if provider_dispatched not in done:
                # Cancellation is a pre-dispatch fence: even if a synchronous
                # SQLite refresh is still finishing in its worker thread, the
                # coroutine cannot advance past that await into the adapter.
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise TimeoutError("connector adapter admission timed out")
            # Shield keeps the limiter held until an uncancellable synchronous
            # adapter actually returns. The caller still receives a bounded
            # timeout and records an uncertain write outcome where required.
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self.adapter_timeout_seconds,
            )
        except BaseException:
            if _background_task_sink is None:
                task.add_done_callback(_consume_background_task)
            else:
                _background_task_sink(task)
            raise
        finally:
            if not provider_dispatched.done():
                provider_dispatched.cancel()

    def _publish_best_effort(self) -> None:
        if self.outbox_publisher is None:
            return
        try:
            self.drain_outbox()
        except Exception:
            # The business mutation and event are already committed. Delivery
            # failure must never invite the caller to repeat that mutation.
            return


async def _call_adapter(callable_: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    result = await asyncio.to_thread(callable_, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _run_sync_daemon(
    callable_: Callable[..., Any],
    /,
    *args: Any,
    thread_name: str,
    **kwargs: Any,
) -> Any:
    """Await synchronous Connector work without owning executor shutdown.

    Cancellation stops awaiting immediately. The isolated daemon may finish in
    the background, but every long Connector operation passed here owns its own
    monotonic deadline and cannot keep ``asyncio.run`` or ASGI loop teardown
    waiting on the process-global default executor.
    """

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    context = copy_context()

    def settle_result(value: Any) -> None:
        if not future.done():
            future.set_result(value)

    def settle_error(error: BaseException) -> None:
        if not future.done():
            future.set_exception(error)

    def run() -> None:
        try:
            result = context.run(callable_, *args, **kwargs)
        except BaseException as error:
            callback = settle_error
            value: Any = error
        else:
            callback = settle_result
            value = result
        try:
            loop.call_soon_threadsafe(callback, value)
        except RuntimeError:
            # The waiter was cancelled and its event loop has already closed.
            return

    threading.Thread(
        target=run,
        daemon=True,
        name=thread_name,
    ).start()
    return await future


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return


def _normalize_client_request_id(value: str | None) -> str:
    if value is None:
        return "server_" + uuid.uuid4().hex
    if not isinstance(value, str) or not _CLIENT_REQUEST_ID_RE.fullmatch(value):
        raise ConnectorIdempotencyRequired(
            "a valid connector client request ID is required"
        )
    return value


def _normalize_runtime_context(
    value: Mapping[str, str] | ConnectorInvocationContext | None,
) -> ConnectorInvocationContext | None:
    if value is None:
        return None
    if isinstance(value, ConnectorInvocationContext):
        return value
    fields = {
        "job_id",
        "thread_id",
        "turn_id",
        "execution_batch_id",
        "tool_call_id",
        "capability_snapshot_id",
        "permission_snapshot_id",
        "connector_catalog_snapshot_id",
        "discovery_id",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or any(not isinstance(item, str) for item in value.values())
    ):
        raise ValueError("connector invocation Runtime context is invalid")
    return ConnectorInvocationContext(**dict(value))


def _raise_replayed_lifecycle_error(error_code: str | None) -> None:
    error_types: dict[str, type[ConnectorError]] = {
        ConnectorAuthError.code: ConnectorAuthError,
        ConnectorIdempotencyConflict.code: ConnectorIdempotencyConflict,
        ConnectorIdempotencyRequired.code: ConnectorIdempotencyRequired,
        ConnectorInvocationUncertain.code: ConnectorInvocationUncertain,
        ConnectorNotFound.code: ConnectorNotFound,
        ConnectorPermissionDenied.code: ConnectorPermissionDenied,
        ConnectorUnavailable.code: ConnectorUnavailable,
    }
    error_type = error_types.get(str(error_code), ConnectorUnavailable)
    raise error_type("the prior connector lifecycle request failed")


def _canonical_private_challenge(challenge: AuthChallenge) -> str:
    encoded = json.dumps(
        challenge.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > 32 * 1024:
        raise ConnectorAuthError("connector authorization challenge is too large")
    return encoded


def _private_auth_challenge(
    private_state: Mapping[str, str],
    *,
    flow: Any,
) -> AuthChallenge:
    try:
        raw = json.loads(str(private_state["challenge_json"]))
        expected_keys = {
            "flow_id",
            "connector_id",
            "auth_kind",
            "expires_at",
            "authorization_url",
            "user_code",
            "verification_url",
        }
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise ValueError("invalid connector authorization challenge")
        challenge = AuthChallenge(
            flow_id=str(raw["flow_id"]),
            connector_id=str(raw["connector_id"]),
            auth_kind=ConnectorAuthKind(str(raw["auth_kind"])),
            expires_at=datetime.fromisoformat(str(raw["expires_at"])),
            authorization_url=(
                str(raw["authorization_url"])
                if raw["authorization_url"] is not None
                else None
            ),
            user_code=(
                str(raw["user_code"])
                if raw["user_code"] is not None
                else None
            ),
            verification_url=(
                str(raw["verification_url"])
                if raw["verification_url"] is not None
                else None
            ),
        )
        state = str(private_state["state"])
        verifier = str(private_state["pkce_verifier"])
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        _validate_auth_challenge(
            challenge,
            flow_id=flow.flow_id,
            connector_id=flow.connector_id,
            auth_kind=flow.auth_kind,
            expected_state=state,
            expected_code_challenge=code_challenge,
        )
        if challenge.expires_at != flow.expires_at:
            raise ValueError("connector authorization expiry mismatch")
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ConnectorAuthError(
            "connector authorization replay state is invalid"
        ) from None
    return challenge


def _validate_loopback_return_uri(return_uri: str) -> None:
    parsed = urlsplit(return_uri)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("connector OAuth return URI must be an exact loopback HTTP URI")


def _validate_auth_challenge(
    challenge: Any,
    *,
    flow_id: str,
    connector_id: str,
    auth_kind: ConnectorAuthKind,
    expected_state: str,
    expected_code_challenge: str,
) -> None:
    if not isinstance(challenge, AuthChallenge):
        raise ValueError("invalid auth challenge")
    if challenge.flow_id != flow_id or challenge.connector_id != connector_id:
        raise ValueError("mismatched auth challenge")
    if challenge.auth_kind is not auth_kind:
        raise ValueError("mismatched auth kind")
    if challenge.expires_at.tzinfo is None or challenge.expires_at <= datetime.now(UTC):
        raise ValueError("expired auth challenge")
    if auth_kind is ConnectorAuthKind.OAUTH2:
        if challenge.authorization_url is None:
            raise ValueError("OAuth authorization URL is required")
        parsed = urlsplit(challenge.authorization_url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("OAuth authorization URL is unsafe")
        states = parse_qs(parsed.query, keep_blank_values=True).get("state", [])
        if len(states) != 1 or not secrets.compare_digest(states[0], expected_state):
            raise ValueError("OAuth authorization URL did not preserve state")
        parameters = parse_qs(parsed.query, keep_blank_values=True)
        challenges = parameters.get("code_challenge", [])
        methods = parameters.get("code_challenge_method", [])
        if (
            len(challenges) != 1
            or not secrets.compare_digest(challenges[0], expected_code_challenge)
            or methods != ["S256"]
        ):
            raise ValueError("OAuth authorization URL did not preserve PKCE")


def _normalize_grant(
    grant: Any,
) -> tuple[str, str, frozenset[str], dict[str, str]]:
    if not isinstance(grant, AuthGrant):
        raise ConnectorAuthError("connector returned an invalid authorization grant")
    if not isinstance(grant.credential_material, Mapping) or not grant.credential_material:
        raise ConnectorAuthError("connector returned empty credential material")
    credential_material: dict[str, str] = {}
    for key, value in grant.credential_material.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConnectorAuthError("connector returned invalid credential material")
        if not key or not value or _CONTROL_RE.search(key):
            raise ConnectorAuthError("connector returned invalid credential material")
        credential_material[key] = value
    if not isinstance(grant.account_subject, str) or not isinstance(
        grant.account_display_name, str
    ):
        raise ConnectorAuthError("connector returned an invalid account identity")
    account_subject = grant.account_subject.strip()
    account_display_name = grant.account_display_name.strip()
    if (
        not account_subject
        or not account_display_name
        or len(account_subject) > 512
        or len(account_display_name) > 512
        or _CONTROL_RE.search(account_subject)
        or _CONTROL_RE.search(account_display_name)
    ):
        raise ConnectorAuthError("connector returned an invalid account identity")
    if any(not isinstance(scope, str) for scope in grant.granted_scopes):
        raise ConnectorAuthError("connector returned invalid granted scopes")
    granted_scopes = frozenset(grant.granted_scopes)
    if len(granted_scopes) > 256 or any(
        not scope or len(scope) > 256 or _CONTROL_RE.search(scope)
        for scope in granted_scopes
    ):
        raise ConnectorAuthError("connector returned invalid granted scopes")
    public_values = [account_subject, account_display_name, *sorted(granted_scopes)]
    for secret_value in credential_material.values():
        if secret_value and any(secret_value in value for value in public_values):
            raise ConnectorAuthError(
                "connector exposed credential material as account metadata"
            )
    return account_subject, account_display_name, granted_scopes, credential_material


def _validate_idempotency_key(required: bool, idempotency_key: str | None) -> None:
    if required and not str(idempotency_key or "").strip():
        raise ConnectorIdempotencyRequired(
            "connector write requires an idempotency key"
        )
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ConnectorIdempotencyRequired("connector idempotency key cannot be blank")
        if len(idempotency_key.encode("utf-8")) > _MAX_IDEMPOTENCY_KEY_BYTES:
            raise ValueError("connector idempotency key is too large")


def _connector_effect_may_mutate(effects: frozenset[ConnectorEffect]) -> bool:
    return bool(effects & {ConnectorEffect.WRITE, ConnectorEffect.SUBSCRIBE})


def _encoded_connector_result(result: Any) -> bytes:
    return json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_json_value(value: Any, *, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number at {path}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"non-string JSON object key at {path}")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise ValueError(f"unsupported JSON value at {path}")


def _json_digest(value: Any, *, label: str) -> str:
    try:
        _validate_json_value(value)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON values") from exc
    if len(encoded) > _MAX_ACTION_JSON_BYTES:
        raise ValueError(f"{label} exceed the connector JSON size limit")
    return hashlib.sha256(encoded).hexdigest()


def _compact_field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _is_sensitive_field_name(value: str) -> bool:
    compact = _compact_field_name(value)
    return (
        compact in _SENSITIVE_COMPACT_NAMES
        or compact.endswith("token")
        or compact.endswith("apikey")
        or "secret" in compact
        or "password" in compact
        or "credential" in compact
        or compact == "auth"
        or compact.startswith(("oauth", "authorization", "authentication"))
        or compact.endswith("authorization")
        or "bearer" in compact
        or compact.endswith("cookie")
    )


def _validate_output_schema(value: Any, schema: Mapping[str, Any], *, path: str = "$") -> None:
    declared_type = schema.get("type")
    allowed_types = (
        set(declared_type)
        if isinstance(declared_type, (list, tuple))
        else {declared_type}
    )
    actual_type = (
        "null" if value is None else
        "boolean" if isinstance(value, bool) else
        "integer" if isinstance(value, int) else
        "number" if isinstance(value, float) else
        "string" if isinstance(value, str) else
        "array" if isinstance(value, list) else
        "object" if isinstance(value, dict) else
        "invalid"
    )
    if actual_type not in allowed_types and not (
        actual_type == "integer" and "number" in allowed_types
    ):
        raise ValueError(f"connector result violates output schema at {path}")
    if actual_type == "object":
        if schema.get("additionalProperties") is not False:
            raise ValueError("connector output schema must fail closed on unknown fields")
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            raise ValueError("connector output schema properties are required")
        required = set(schema.get("required", []))
        if not required <= set(value):
            raise ValueError(f"connector result is missing required fields at {path}")
        unknown = set(value) - set(properties)
        if unknown:
            raise ValueError(f"connector result contains unknown fields at {path}")
        for key, item in value.items():
            child_schema = properties[key]
            if not isinstance(child_schema, Mapping):
                raise ValueError("connector output property schema is invalid")
            _validate_output_schema(item, child_schema, path=f"{path}.{key}")
    elif actual_type == "array":
        item_schema = schema.get("items")
        if not isinstance(item_schema, Mapping):
            raise ValueError("connector output array item schema is required")
        max_items = schema.get("maxItems")
        if not isinstance(max_items, int) or max_items <= 0 or len(value) > max_items:
            raise ValueError(f"connector result array violates output schema at {path}")
        for index, item in enumerate(value):
            _validate_output_schema(item, item_schema, path=f"{path}[{index}]")
    elif actual_type == "string":
        max_length = schema.get("maxLength")
        if not isinstance(max_length, int) or max_length <= 0 or len(value) > max_length:
            raise ValueError(f"connector result string violates output schema at {path}")
        public_kind = schema.get("x-ecorex-public-kind")
        if public_kind not in {
            "text",
            "public_id",
            "public_uri",
            "timestamp",
            "mime_type",
            "enum",
            "connector_cursor",
        }:
            raise ValueError(f"connector output string lacks public semantics at {path}")


def _standard_output_validation_schema(value: Any) -> Any:
    """Remove Connector public annotations, preserving the exact JSON Schema."""

    if isinstance(value, Mapping):
        return {
            str(key): _standard_output_validation_schema(item)
            for key, item in value.items()
            if key != "x-ecorex-public-kind"
        }
    if isinstance(value, (list, tuple)):
        return [_standard_output_validation_schema(item) for item in value]
    return value


def _looks_like_credential(value: str) -> bool:
    lowered = value.casefold()
    if any(marker in lowered for marker in ("credential", "access_token", "refresh_token")):
        return True
    if lowered.startswith(("sk-", "sk_", "xox", "ghp_", "github_pat_", "bearer ")):
        return True
    if lowered.startswith("eyj") and value.count(".") >= 2:
        return True
    return False


def _assert_result_is_persistable_and_secret_free(
    result: Any,
    credentials: Mapping[str, str],
    *,
    output_schema: Mapping[str, Any],
) -> None:
    try:
        _validate_json_value(result)
        validate_schema_instance(
            result,
            _standard_output_validation_schema(output_schema),
            label="connector action result",
        )
        _validate_output_schema(result, output_schema)
    except (KeyError, SchemaInstanceError, TypeError, ValueError, RecursionError):
        raise _RejectedConnectorResult(
            "connector_result_schema_invalid"
        ) from None

    def walk(value: Any, schema: Mapping[str, Any]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if _is_sensitive_field_name(key):
                    raise ValueError("connector result contains credential material")
                properties = schema.get("properties", {})
                walk(item, properties[key])
        elif isinstance(value, list):
            for item in value:
                walk(item, schema["items"])
        elif isinstance(value, str):
            if any(
                secret_value and secret_value in value
                for secret_value in credentials.values()
            ):
                raise ValueError("connector result contains credential material")
            public_kind = schema.get("x-ecorex-public-kind")
            if public_kind != "public_uri" and _looks_like_credential(value):
                raise ValueError("connector result resembles credential material")
            parsed = urlsplit(value)
            if public_kind == "public_uri":
                if (
                    parsed.scheme not in {"https", "http"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                ):
                    raise ValueError("connector result contains an invalid public URL")
                sensitive_url = parsed.username is not None or parsed.password is not None
                sensitive_url = sensitive_url or any(
                    _is_sensitive_field_name(key)
                    for key in parse_qs(parsed.query, keep_blank_values=True)
                )
                fragment_fields = parse_qs(
                    parsed.fragment.replace(";", "&"), keep_blank_values=True
                )
                sensitive_url = sensitive_url or any(
                    _is_sensitive_field_name(key) for key in fragment_fields
                )
                if sensitive_url or _looks_like_credential(parsed.fragment):
                    raise ValueError("connector result contains a credential-bearing URL")

    try:
        walk(result, output_schema)
    except (KeyError, TypeError, ValueError, RecursionError):
        raise _RejectedConnectorResult(
            "connector_result_secret_rejected"
        ) from None
    try:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise _RejectedConnectorResult(
            "connector_result_schema_invalid"
        ) from None
    if len(encoded) > _MAX_ACTION_JSON_BYTES:
        raise _RejectedConnectorResult("connector_result_too_large")


def _validated_health_error_code(
    error_code: str | None,
    credentials: Mapping[str, str],
) -> str | None:
    if error_code is None:
        return None
    if not isinstance(error_code, str) or not _ERROR_CODE_RE.fullmatch(error_code):
        raise ValueError("connector returned an invalid health error code")
    if any(
        secret_value and secret_value in error_code
        for secret_value in credentials.values()
    ):
        raise ValueError("connector health result contains credential material")
    return error_code


__all__ = ["ConnectorResultCoordinator", "ConnectorService"]
