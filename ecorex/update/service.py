"""Durable Runtime control layer for background update preparation and activation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import hashlib
import inspect
import json
from pathlib import Path
import threading
from typing import Any, Protocol
import uuid

from ecorex.protocol import ActivateUpdateResponse, UpdateSnapshot
from ecorex.runtime.database import SQLiteDatabase, json_dumps, json_loads
from ecorex.runtime.schema_catalog import validate_product_schema

from .coordinator import (
    ActivationResult,
    InstallCoordinator,
    PreparedUpdate,
    RollForwardRequired,
)
from .journal import InstallState
from .manifest import ReleaseChannel, ReleaseManifest
from .transport import UpdateAvailableSignal


class ReleaseFeed(Protocol):
    def latest(
        self,
        *,
        channel: ReleaseChannel,
        platform: str,
        architecture: str,
        current_version: str,
        update_state: str,
    ) -> ReleaseManifest | None:
        ...


class UpdateSignalSource(Protocol):
    def events(self) -> AsyncIterator[UpdateAvailableSignal]:
        ...


class RuntimeActivationDrainer(Protocol):
    async def acquire(self, transaction_id: str) -> Any:
        ...

    def assert_drained(self, lease: Any) -> None:
        ...

    def release(self, lease: Any) -> None:
        ...


RestartRequester = Callable[[str], Any]


class UpdateServiceError(RuntimeError):
    pass


class UpdateActivationUnavailable(UpdateServiceError):
    pass


class UpdateStateConflict(UpdateServiceError):
    pass


_STATES = frozenset(
    {"idle", "available", "downloading", "awaiting_user", "activating", "failed"}
)


class UpdateStateRepository:
    def __init__(
        self,
        database: SQLiteDatabase | str | Path,
        *,
        current_version: str,
        initialize: bool = True,
    ) -> None:
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        self.current_version = current_version
        if initialize:
            self.initialize()
        else:
            self.validate()

    def validate(self) -> None:
        """Validate existing update state without creating or converging it."""

        # Product schema is compiled centrally and is immutable at Runtime
        # startup. A repository may validate it, never create or repair it.
        with self.database.reader() as connection:
            validate_product_schema(connection)
            row = connection.execute(
                "SELECT * FROM runtime_update_state WHERE singleton = 1"
            ).fetchone()
        if row is not None:
            self._from_row(row)

    def initialize(self) -> UpdateSnapshot:
        """Persist and converge update state during healthy startup."""

        self.validate()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO runtime_update_state("
                "singleton, state, requires_refresh, updated_at"
                ") VALUES (1, 'idle', 0, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(singleton) DO NOTHING"
            )
            row = connection.execute(
                "SELECT * FROM runtime_update_state WHERE singleton = 1"
            ).fetchone()
            self._from_row(row)
            # A Runtime may restart after Bootstrap has already switched the
            # slot but before the old process persists its final state. The
            # running product version is authoritative here: no non-idle
            # state for that exact version can describe a real update.
            if row["target_version"] == self.current_version and row["state"] != "idle":
                self._set_in_transaction(
                    connection,
                    state="idle",
                    event_type="update.running_version_confirmed",
                )
            row = connection.execute(
                "SELECT * FROM runtime_update_state WHERE singleton = 1"
            ).fetchone()
        return self._from_row(row)

    def converge_startup(self) -> UpdateSnapshot:
        return self.initialize()

    def snapshot(self, *, can_activate: bool) -> UpdateSnapshot:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_update_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return UpdateSnapshot(current_version=self.current_version)
        return self._from_row(row, can_activate=can_activate)

    def set(
        self,
        *,
        state: str,
        event_type: str,
        target_version: str | None = None,
        release_id: str | None = None,
        build_digest: str | None = None,
        transaction_id: str | None = None,
        requires_refresh: bool = False,
        error_code: str | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            self._require_initialized(connection)
            self._set_in_transaction(
                connection,
                state=state,
                event_type=event_type,
                target_version=target_version,
                release_id=release_id,
                build_digest=build_digest,
                transaction_id=transaction_id,
                requires_refresh=requires_refresh,
                error_code=error_code,
            )

    def _set_in_transaction(
        self,
        connection,
        *,
        state: str,
        event_type: str,
        target_version: str | None = None,
        release_id: str | None = None,
        build_digest: str | None = None,
        transaction_id: str | None = None,
        requires_refresh: bool = False,
        error_code: str | None = None,
    ) -> None:
        if state not in _STATES:
            raise ValueError("Runtime update state is invalid")
        identity = (target_version, release_id, build_digest)
        if state == "idle":
            if any(value is not None for value in (*identity, transaction_id, error_code)):
                raise ValueError("idle update state cannot retain a target")
        elif state != "failed" and any(not value for value in identity):
            raise ValueError("active update state requires an immutable release identity")
        if build_digest is not None and (
            len(build_digest) != 64
            or any(character not in "0123456789abcdef" for character in build_digest)
        ):
            raise ValueError("Runtime update build digest is invalid")
        if requires_refresh and state != "activating":
            raise ValueError("only an activated update can require refresh")
        now = _now()
        connection.execute(
            "UPDATE runtime_update_state SET state = ?, target_version = ?, "
            "release_id = ?, build_digest = ?, transaction_id = ?, "
            "requires_refresh = ?, error_code = ?, updated_at = ? WHERE singleton = 1",
            (
                state,
                target_version,
                release_id,
                build_digest,
                transaction_id,
                int(requires_refresh),
                error_code,
                now,
            ),
        )
        payload = {
            "state": state,
            "target_version": target_version,
            "release_id": release_id,
            "build_digest": build_digest,
            "transaction_id": transaction_id,
            "requires_refresh": requires_refresh,
            "error_code": error_code,
        }
        connection.execute(
            "INSERT INTO runtime_update_events(event_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (uuid.uuid4().hex, event_type, json_dumps(payload), now),
        )

    def record_signal(self, signal: UpdateAvailableSignal) -> bool:
        payload = {
            "event_id": signal.event_id,
            "release_id": signal.release_id,
            "version": signal.version,
            "build_digest": signal.build_digest,
            "channel": signal.channel.value,
        }
        digest = hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()
        with self.database.transaction() as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "SELECT payload_sha256 FROM runtime_update_signals WHERE event_id = ?",
                (signal.event_id,),
            ).fetchone()
            if row is not None:
                if row["payload_sha256"] != digest:
                    raise UpdateStateConflict(
                        "update signal event_id was reused with different content"
                    )
                return False
            connection.execute(
                "INSERT INTO runtime_update_signals(event_id, payload_sha256, created_at) "
                "VALUES (?, ?, ?)",
                (signal.event_id, digest, _now()),
            )
            return True

    def activation_response(
        self,
        *,
        client_request_id: str,
        transaction_id: str,
    ) -> ActivateUpdateResponse | None:
        fingerprint = _activation_fingerprint(transaction_id)
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT request_fingerprint, response_json "
                "FROM runtime_update_activation_requests WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
        if row is None:
            return None
        if row["request_fingerprint"] != fingerprint:
            raise UpdateStateConflict(
                "update activation client_request_id was reused with different content"
            )
        return ActivateUpdateResponse.model_validate(json_loads(row["response_json"], {}))

    def save_activation_response(
        self,
        *,
        client_request_id: str,
        transaction_id: str,
        response: ActivateUpdateResponse,
    ) -> None:
        if not client_request_id or len(client_request_id) > 256:
            raise ValueError("update activation client_request_id is invalid")
        fingerprint = _activation_fingerprint(transaction_id)
        with self.database.transaction() as connection:
            self._require_initialized(connection)
            existing = connection.execute(
                "SELECT request_fingerprint, response_json "
                "FROM runtime_update_activation_requests WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise UpdateStateConflict(
                        "update activation client_request_id was reused with different content"
                    )
                return
            connection.execute(
                "INSERT INTO runtime_update_activation_requests("
                "client_request_id, request_fingerprint, response_json, created_at"
                ") VALUES (?, ?, ?, ?)",
                (
                    client_request_id,
                    fingerprint,
                    response.model_dump_json(),
                    _now(),
                ),
            )

    @staticmethod
    def _require_initialized(connection) -> None:
        row = connection.execute(
            "SELECT 1 FROM runtime_update_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise UpdateServiceError("durable Runtime update state is not initialized")

    def _from_row(self, row, *, can_activate: bool = False) -> UpdateSnapshot:
        if row is None or row["state"] not in _STATES:
            raise UpdateServiceError("durable Runtime update state is invalid")
        try:
            return UpdateSnapshot(
                current_version=self.current_version,
                state=row["state"],
                target_version=row["target_version"],
                release_id=row["release_id"],
                build_digest=row["build_digest"],
                transaction_id=row["transaction_id"],
                can_activate=(
                    can_activate
                    and row["state"] == "awaiting_user"
                    and bool(row["transaction_id"])
                ),
                requires_refresh=bool(row["requires_refresh"]),
                error_code=row["error_code"],
            )
        except (TypeError, ValueError) as error:
            raise UpdateServiceError("durable Runtime update state is malformed") from error


class RuntimeUpdateService:
    def __init__(
        self,
        database: SQLiteDatabase | str | Path,
        *,
        coordinator: InstallCoordinator,
        feed: ReleaseFeed,
        artifact_id: str,
        current_version: str,
        channel: ReleaseChannel,
        platform: str,
        architecture: str,
        signal_source: UpdateSignalSource | None = None,
        restart_requester: RestartRequester | None = None,
        poll_interval_seconds: float = 300,
        initialize: bool = True,
    ) -> None:
        if not artifact_id:
            raise ValueError("update artifact_id is required")
        if not 5 <= poll_interval_seconds <= 86_400:
            raise ValueError("update poll interval must be between 5 seconds and one day")
        self.coordinator = coordinator
        self.feed = feed
        self.artifact_id = artifact_id
        self.current_version = current_version
        self.channel = channel
        self.platform = platform
        self.architecture = architecture
        self.signal_source = signal_source
        self.restart_requester = restart_requester
        self.poll_interval_seconds = poll_interval_seconds
        self.repository = UpdateStateRepository(
            database,
            current_version=current_version,
            initialize=False,
        )
        self._startup_lock = threading.Lock()
        self._startup_converged = False
        self._operation = asyncio.Lock()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._closed = False
        self._activation_drainer: RuntimeActivationDrainer | None = None
        if initialize:
            self.converge_startup()

    @property
    def running(self) -> bool:
        return bool(self._tasks) and any(not task.done() for task in self._tasks)

    def snapshot(self) -> UpdateSnapshot:
        return self.repository.snapshot(can_activate=self.restart_requester is not None)

    @property
    def startup_converged(self) -> bool:
        return self._startup_converged

    def bind_runtime_activation_drainer(
        self, drainer: RuntimeActivationDrainer
    ) -> None:
        """Bind the live Runtime admission barrier exactly once.

        Product composition constructs the updater before the Runtime gate.
        Binding is therefore an explicit phase-B action, completed before any
        update transport or HTTP activation endpoint starts accepting work.
        """

        if any(
            not callable(getattr(drainer, member, None))
            for member in ("acquire", "assert_drained", "release")
        ):
            raise TypeError("Runtime activation drainer is invalid")
        if self.running or self._closed:
            raise UpdateServiceError(
                "Runtime activation drainer must be bound before service start"
            )
        if self._activation_drainer is not None and self._activation_drainer is not drainer:
            raise UpdateServiceError("Runtime activation drainer is already bound")
        self._activation_drainer = drainer

    def converge_startup(self) -> UpdateSnapshot:
        """Converge installer storage and the Runtime projection exactly once."""

        with self._startup_lock:
            if self._startup_converged:
                return self.snapshot()
            converge_coordinator = getattr(self.coordinator, "converge_startup", None)
            if callable(converge_coordinator):
                converge_coordinator()
            snapshot = self.repository.converge_startup()
            self._startup_converged = True
            return snapshot

    async def start(self) -> None:
        if self._closed:
            raise UpdateServiceError("a closed Runtime update service cannot be restarted")
        if not self._startup_converged:
            raise UpdateServiceError("Runtime update startup has not converged")
        if self.running:
            return
        self._stopping = False
        await self._recover()
        self._tasks = [
            asyncio.create_task(self._poll_loop(), name="ecorex-update-poll")
        ]
        if self.signal_source is not None:
            self._tasks.append(
                asyncio.create_task(self._signal_loop(), name="ecorex-update-signal")
            )

    async def stop(self) -> None:
        if self._closed:
            return
        self._stopping = True
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        resources = (
            self.feed,
            self.signal_source,
            getattr(self.coordinator, "fetcher", None),
        )
        seen: set[int] = set()
        failures: list[Exception] = []
        for transport in resources:
            if transport is None or id(transport) in seen:
                continue
            seen.add(id(transport))
            close = getattr(transport, "close", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception as error:
                    failures.append(error)
        self._closed = True
        if failures:
            raise UpdateServiceError("one or more update transports failed to close") from failures[0]

    async def check_now(self) -> UpdateSnapshot:
        if self._closed:
            raise UpdateServiceError("Runtime update service is closed")
        async with self._operation:
            current = await _run_blocking(self.snapshot)
            if current.state in {"downloading", "awaiting_user", "activating"}:
                await self._report_state(current.state)
                return current
            try:
                manifest = await _run_blocking(
                    self.feed.latest,
                    channel=self.channel,
                    platform=self.platform,
                    architecture=self.architecture,
                    current_version=self.current_version,
                    update_state=current.state,
                )
                if manifest is None:
                    await _run_blocking(
                        self.repository.set,
                        state="idle",
                        event_type="update.no_update",
                    )
                    return await _run_blocking(self.snapshot)
                artifact = manifest.artifact(self.artifact_id)
                if artifact.platform != self.platform or artifact.architecture != self.architecture:
                    raise UpdateServiceError("release feed selected the wrong platform artifact")
                identity = {
                    "target_version": manifest.version,
                    "release_id": manifest.release_id,
                    "build_digest": manifest.build_digest,
                }
                await _run_blocking(
                    self.repository.set,
                    state="available",
                    event_type="update.available",
                    **identity,
                )
                await _run_blocking(
                    self.repository.set,
                    state="downloading",
                    event_type="update.download_started",
                    **identity,
                )
                await self._report_state("downloading")
                authorization_provider = getattr(
                    self.feed, "rollback_authorization", None
                )
                rollback_authorization = (
                    await _run_blocking(authorization_provider, manifest)
                    if callable(authorization_provider)
                    else None
                )
                prepared = await _run_blocking(
                    self.coordinator.prepare_update,
                    manifest,
                    self.artifact_id,
                    rollback_authorization=rollback_authorization,
                )
                await self._record_prepared(prepared)
            except Exception as error:
                current = await _run_blocking(self.snapshot)
                await _run_blocking(
                    self.repository.set,
                    state="failed",
                    event_type="update.prepare_failed",
                    target_version=current.target_version,
                    release_id=current.release_id,
                    build_digest=current.build_digest,
                    transaction_id=current.transaction_id,
                    error_code=_error_code(error),
                )
                await self._report_state("failed")
            return await _run_blocking(self.snapshot)

    async def activate(
        self,
        *,
        transaction_id: str,
        client_request_id: str,
    ) -> ActivateUpdateResponse:
        if self._closed:
            raise UpdateServiceError("Runtime update service is closed")
        existing = await _run_blocking(
            self.repository.activation_response,
            client_request_id=client_request_id,
            transaction_id=transaction_id,
        )
        if existing is not None:
            return existing
        if self.restart_requester is None:
            raise UpdateActivationUnavailable(
                "update activation requires a configured Runtime restart controller"
            )
        async with self._operation:
            current = await _run_blocking(self.snapshot)
            if current.transaction_id != transaction_id or current.state not in {
                "awaiting_user",
                "activating",
            }:
                raise UpdateStateConflict("update transaction is not awaiting this user")
            if current.state == "awaiting_user" or (
                current.state == "activating" and not current.requires_refresh
            ):
                authorized = await _run_blocking(
                    self.feed.latest,
                    channel=self.channel,
                    platform=self.platform,
                    architecture=self.architecture,
                    current_version=self.current_version,
                    update_state=current.state,
                )
                if authorized is None or not await _run_blocking(
                    self.coordinator.authorizes_pending,
                    authorized,
                    transaction_id,
                ):
                    error = UpdateStateConflict(
                        "prepared update is no longer authorized by the active rollout"
                    )
                    try:
                        await _run_blocking(
                            self.coordinator.cancel_pending_activation,
                            transaction_id,
                        )
                    except Exception as cancellation_error:
                        await self._record_failure(current, cancellation_error)
                        raise cancellation_error
                    await self._record_failure(current, error)
                    raise error
            return await self._activate_authorized(
                current=current,
                transaction_id=transaction_id,
                client_request_id=client_request_id,
            )

    async def activate_verified_local(
        self,
        *,
        transaction_id: str,
        client_request_id: str,
        execution_guard: Callable[[], None],
    ) -> ActivateUpdateResponse:
        """Activate only an exact, locally re-verified awaiting transaction.

        The host Runtime bearer, loopback Origin and CSRF token are the local
        installer credential. No managed cloud lease or rollout-feed request is
        needed here: rollout authorization was fixed when the signed release
        was downloaded, and every local authority is re-verified immediately
        before activation.
        """

        if self._closed:
            raise UpdateServiceError("Runtime update service is closed")
        if not callable(execution_guard):
            raise ValueError("local update activation requires an execution guard")
        execution_guard()
        existing = await _run_blocking(
            self.repository.activation_response,
            client_request_id=client_request_id,
            transaction_id=transaction_id,
        )
        execution_guard()
        if existing is not None:
            return existing
        if self.restart_requester is None:
            raise UpdateActivationUnavailable(
                "update activation requires a configured Runtime restart controller"
            )
        async with self._operation:
            execution_guard()
            current = await _run_blocking(self.snapshot)
            execution_guard()
            if (
                current.transaction_id != transaction_id
                or current.state != "awaiting_user"
            ):
                raise UpdateStateConflict(
                    "local update transaction is not awaiting this user"
                )
            authorized = await _run_blocking(
                self.coordinator.authorizes_local_pending,
                transaction_id,
            )
            execution_guard()
            if not authorized:
                raise UpdateStateConflict(
                    "local update transaction failed staged verification"
                )
            return await self._activate_authorized(
                current=current,
                transaction_id=transaction_id,
                client_request_id=client_request_id,
                execution_guard=execution_guard,
                report_remote=False,
            )

    async def _activate_authorized(
        self,
        *,
        current: UpdateSnapshot,
        transaction_id: str,
        client_request_id: str,
        execution_guard: Callable[[], None] | None = None,
        report_remote: bool = True,
    ) -> ActivateUpdateResponse:
        def assert_execution() -> None:
            if execution_guard is not None:
                execution_guard()

        assert_execution()
        needs_activation = current.state == "awaiting_user" or (
            current.state == "activating" and not current.requires_refresh
        )
        drain_lease: Any | None = None
        activation_boundary_crossed = False
        activation_attempted = False
        if needs_activation and self._activation_drainer is not None:
            # Do not publish ``activating`` until the reversible live Runtime
            # drain succeeds. A timeout leaves the signed staged candidate in
            # awaiting_user so the user can retry after long work checkpoints.
            drain_lease = await self._activation_drainer.acquire(transaction_id)
            try:
                assert_execution()
                await _run_blocking(
                    self._activation_drainer.assert_drained,
                    drain_lease,
                )
            except BaseException:
                await _run_blocking(
                    self._activation_drainer.release,
                    drain_lease,
                )
                drain_lease = None
                raise
        try:
            if current.state == "awaiting_user":
                await _run_blocking(
                    self.repository.set,
                    state="activating",
                    event_type="update.activation_confirmed",
                    target_version=current.target_version,
                    release_id=current.release_id,
                    build_digest=current.build_digest,
                    transaction_id=current.transaction_id,
                )
                assert_execution()
            if needs_activation:
                try:
                    activation_attempted = True
                    result = await _run_blocking(
                        self.coordinator.activate,
                        transaction_id,
                    )
                except Exception as error:
                    await self._record_failure(current, error)
                    raise
                assert_execution()
                if result.state not in {
                    InstallState.HEALTHCHECKING,
                    InstallState.COMPLETED,
                }:
                    error = UpdateServiceError(
                        f"update activation ended in {result.state.value}"
                    )
                    await self._record_failure(current, error)
                    raise error
                # The slot switch/Bootstrap intent is now authoritative. Keep
                # Runtime admission drained even if later response persistence
                # or restart scheduling fails; recovery must finish the exact
                # activation rather than accepting new work on the old image.
                activation_boundary_crossed = True
                await _run_blocking(
                    self.repository.set,
                    state="activating",
                    event_type=(
                        "update.activation_awaiting_bootstrap_health"
                        if result.state is InstallState.HEALTHCHECKING
                        else "update.activation_completed"
                    ),
                    target_version=current.target_version,
                    release_id=current.release_id,
                    build_digest=current.build_digest,
                    transaction_id=current.transaction_id,
                    requires_refresh=True,
                )
                assert_execution()
                if report_remote:
                    await self._report_state("activating")
                    assert_execution()
        finally:
            if (
                drain_lease is not None
                and not activation_boundary_crossed
                and activation_attempted
            ):
                boundary_probe = getattr(
                    self.coordinator,
                    "activation_boundary_crossed",
                    None,
                )
                if callable(boundary_probe):
                    try:
                        activation_boundary_crossed = bool(
                            await _run_blocking(boundary_probe, transaction_id)
                        )
                    except BaseException:
                        # Once activation was attempted, an unreadable durable
                        # boundary is unsafe to reopen. Bootstrap recovery owns
                        # convergence from this point.
                        activation_boundary_crossed = True
            if (
                drain_lease is not None
                and not activation_boundary_crossed
                and self._activation_drainer is not None
            ):
                await _run_blocking(
                    self._activation_drainer.release,
                    drain_lease,
                )
        assert_execution()
        restart = self.restart_requester(transaction_id)
        if inspect.isawaitable(restart):
            await restart
        assert_execution()
        response = ActivateUpdateResponse(
            update=await _run_blocking(self.snapshot),
            restart_scheduled=True,
        )
        assert_execution()
        await _run_blocking(
            self.repository.save_activation_response,
            client_request_id=client_request_id,
            transaction_id=transaction_id,
            response=response,
        )
        assert_execution()
        return response

    async def _recover(self) -> None:
        current = await _run_blocking(self.snapshot)
        latest_state = self.coordinator.latest_state
        if (
            latest_state in {InstallState.DRAINING, InstallState.ACTIVATING}
            and current.transaction_id is not None
        ):
            reversible = await _run_blocking(
                self.coordinator.activation_is_reversible,
                current.transaction_id,
            )
            if reversible and current.state == "awaiting_user":
                # A prior restart deliberately required fresh user confirmation.
                return
            if reversible and current.state == "activating":
                try:
                    authorized = await _run_blocking(
                        self.feed.latest,
                        channel=self.channel,
                        platform=self.platform,
                        architecture=self.architecture,
                        current_version=self.current_version,
                        update_state=current.state,
                    )
                except Exception as error:
                    await _run_blocking(
                        self.repository.set,
                        state="awaiting_user",
                        event_type="update.activation_reauthorization_required",
                        target_version=current.target_version,
                        release_id=current.release_id,
                        build_digest=current.build_digest,
                        transaction_id=current.transaction_id,
                        error_code=_error_code(error),
                    )
                    return
                if authorized is None or not await _run_blocking(
                    self.coordinator.authorizes_pending,
                    authorized,
                    current.transaction_id,
                ):
                    error = UpdateStateConflict(
                        "recovered activation is no longer authorized by the active rollout"
                    )
                    try:
                        await _run_blocking(
                            self.coordinator.cancel_pending_activation,
                            current.transaction_id,
                        )
                    except RollForwardRequired:
                        # The atomic pointer already moved. Never strand a new
                        # current slot before health/known-good completion.
                        pass
                    except Exception as cancellation_error:
                        await self._record_failure(current, cancellation_error)
                        return
                    else:
                        await self._record_failure(current, error)
                        return
        try:
            recovered = await _run_blocking(self.coordinator.recover)
        except Exception as error:
            current = await _run_blocking(self.snapshot)
            await _run_blocking(
                self.repository.set,
                state="failed",
                event_type="update.recovery_failed",
                target_version=current.target_version,
                release_id=current.release_id,
                build_digest=current.build_digest,
                transaction_id=current.transaction_id,
                error_code=_error_code(error),
            )
            await self._report_state("failed")
            return
        if isinstance(recovered, PreparedUpdate):
            await self._record_prepared(recovered)
        elif isinstance(recovered, ActivationResult):
            current = await _run_blocking(self.snapshot)
            if recovered.state is InstallState.COMPLETED:
                await _run_blocking(
                    self.repository.set,
                    state="activating",
                    event_type="update.activation_recovered",
                    target_version=current.target_version,
                    release_id=current.release_id,
                    build_digest=current.build_digest,
                    transaction_id=recovered.transaction_id,
                    requires_refresh=True,
                )
            elif recovered.state in {InstallState.FAILED, InstallState.ROLLBACK}:
                await self._record_failure(
                    current, UpdateServiceError(recovered.state.value)
                )

    async def _record_prepared(self, prepared: PreparedUpdate) -> None:
        await _run_blocking(
            self.repository.set,
            state="awaiting_user",
            event_type="update.prepared",
            target_version=prepared.version,
            release_id=prepared.release_id,
            build_digest=prepared.build_digest,
            transaction_id=prepared.transaction_id,
        )
        await self._report_state("awaiting_user")

    async def _record_failure(
        self, current: UpdateSnapshot, error: Exception
    ) -> None:
        await _run_blocking(
            self.repository.set,
            state="failed",
            event_type="update.activation_failed",
            target_version=current.target_version,
            release_id=current.release_id,
            build_digest=current.build_digest,
            transaction_id=current.transaction_id,
            error_code=_error_code(error),
        )
        await self._report_state("failed")

    async def _report_state(self, state: str) -> None:
        """Best-effort heartbeat for the administrator distribution projection.

        The signed feed remains the sole rollout authority.  This call only
        makes an already-committed local state observable; transport failure
        must never invalidate a verified download or an atomic local journal.
        """

        if state not in _STATES:
            raise ValueError("Runtime update heartbeat state is invalid")
        try:
            await _run_blocking(
                self.feed.latest,
                channel=self.channel,
                platform=self.platform,
                architecture=self.architecture,
                current_version=self.current_version,
                update_state=state,
            )
        except Exception:
            return

    async def _poll_loop(self) -> None:
        while not self._stopping:
            self._wake.clear()
            await self.check_now()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.poll_interval_seconds
                )
            except TimeoutError:
                pass

    async def _signal_loop(self) -> None:
        delay = 1.0
        while not self._stopping and self.signal_source is not None:
            try:
                async for signal in self.signal_source.events():
                    if self._stopping:
                        return
                    if signal.channel is not self.channel:
                        continue
                    if await _run_blocking(self.repository.record_signal, signal):
                        self._wake.set()
                    delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)


def _activation_fingerprint(transaction_id: str) -> str:
    if not transaction_id or len(transaction_id) > 128:
        raise ValueError("update transaction_id is invalid")
    return hashlib.sha256(
        json_dumps({"transaction_id": transaction_id, "confirmed": True}).encode("utf-8")
    ).hexdigest()


async def _run_blocking(function, /, *args, **kwargs):
    """Let an in-flight transport/filesystem call finish before cancellation closes it."""

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException:
            pass
        raise


def _error_code(error: Exception) -> str:
    return error.__class__.__name__.casefold()[:128]


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
