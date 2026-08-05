"""Durable automatic freshness renewal for the public Bootstrap pointer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
import threading
from typing import Any

from ecorex.release import (
    ReleaseSigner,
    refresh_public_bootstrap_freshness,
)

from .bootstrap_index_service import BootstrapIndexPublicationService
from .models import ControlPrincipal
from .repository import ControlPlaneRepository


class BootstrapFreshnessRefreshError(RuntimeError):
    pass


_CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class BootstrapFreshnessConfig:
    owner_id: str
    enabled: bool = True
    lead_seconds: int = 8 * 60 * 60
    check_interval_seconds: int = 60 * 60
    lease_seconds: int = 10 * 60

    def __post_init__(self) -> None:
        if (
            not isinstance(self.owner_id, str)
            or not self.owner_id
            or len(self.owner_id) > 128
            or not isinstance(self.enabled, bool)
            or not 60 * 60 <= self.lead_seconds <= 23 * 60 * 60
            or not 5 * 60 <= self.check_interval_seconds <= 6 * 60 * 60
            or not 5 * 60 <= self.lease_seconds <= 30 * 60
            or self.check_interval_seconds > self.lead_seconds // 2
        ):
            raise ValueError("Bootstrap freshness refresher configuration is invalid")


class BootstrapFreshnessRefresher:
    """Startup catch-up plus bounded periodic renewal using the online signer."""

    def __init__(
        self,
        repository: ControlPlaneRepository,
        publication_service: BootstrapIndexPublicationService,
        *,
        signer: ReleaseSigner | None,
        config: BootstrapFreshnessConfig,
    ) -> None:
        self.repository = repository
        self.publication_service = publication_service
        self.signer = signer
        self.config = config
        self.actor = ControlPrincipal(
            subject="system.bootstrap-freshness-refresher",
            client_id=config.owner_id,
            account_id="system.release-control",
            organization_id=None,
            roles=frozenset({"release_admin"}),
        )
        self._run_lock = threading.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._last_heartbeat_at: datetime | None = None
        self._scheduler_last_error_code: str | None = None
        self._heartbeat_max_age_seconds = (
            config.check_interval_seconds + config.lease_seconds + 2 * 60
        )
        self._retry_delay_seconds = min(
            60.0, max(5.0, config.check_interval_seconds / 60)
        )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def ready(self) -> bool:
        status = self.status()
        remaining = status["remaining_seconds"]
        active_is_usable = status["active_expires_at"] is None or (
            isinstance(remaining, int) and remaining > 0
        )
        if not self.config.enabled:
            return active_is_usable
        return bool(
            status["signer_configured"]
            and status["scheduler_ready"]
            and active_is_usable
            and status["status"] not in {"degraded", "unconfigured"}
        )

    async def start(self) -> None:
        if self._closed:
            raise BootstrapFreshnessRefreshError(
                "Bootstrap freshness refresher is closed"
            )
        if self._task is not None or not self.config.enabled:
            return
        startup_ok = await self._run_scheduled_once()
        self._task = asyncio.create_task(
            self._loop(startup_ok=startup_ok),
            name="ecorex-bootstrap-freshness-refresher",
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self, *, startup_ok: bool) -> None:
        delay = (
            self.config.check_interval_seconds
            if startup_ok
            else self._retry_delay_seconds
        )
        while True:
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=delay,
                )
                return
            except TimeoutError:
                succeeded = await self._run_scheduled_once()
                delay = (
                    self.config.check_interval_seconds
                    if succeeded
                    else self._retry_delay_seconds
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._record_scheduler_failure(exc)
                delay = self._retry_delay_seconds

    async def _run_scheduled_once(self) -> bool:
        try:
            await asyncio.to_thread(self.run_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._record_scheduler_failure(exc)
            return False
        self._last_heartbeat_at = _observed(None)
        self._scheduler_last_error_code = None
        return True

    async def _record_scheduler_failure(self, error: BaseException) -> None:
        observed = _observed(None)
        error_code = _safe_error_code("scheduler", error)
        self._last_heartbeat_at = observed
        self._scheduler_last_error_code = error_code
        try:
            await asyncio.to_thread(
                self.repository.fail_bootstrap_freshness_refresh,
                attempt_record_id=None,
                owner_id=None,
                error_code=error_code,
                actor=self.actor,
                check_interval_seconds=self.config.check_interval_seconds,
                signer_configured=self.signer is not None,
                now=observed,
            )
        except Exception:
            pass

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        observed = _observed(now)
        return self._decorate_status(
            self.repository.bootstrap_freshness_refresh_status(now=observed),
            now=observed,
        )

    def _decorate_status(
        self,
        result: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        observed = _observed(now)
        heartbeat_age = (
            int((observed - self._last_heartbeat_at).total_seconds())
            if self._last_heartbeat_at is not None
            else None
        )
        scheduler_ready = bool(
            self.config.enabled
            and self.running
            and self._scheduler_last_error_code is None
            and heartbeat_age is not None
            and 0 <= heartbeat_age <= self._heartbeat_max_age_seconds
        )
        return {
            **result,
            "automation_enabled": self.config.enabled,
            "signer_configured": self.signer is not None,
            "lead_seconds": self.config.lead_seconds,
            "check_interval_seconds": self.config.check_interval_seconds,
            "lease_seconds": self.config.lease_seconds,
            "scheduler_running": self.running,
            "scheduler_ready": scheduler_ready,
            "scheduler_last_heartbeat_at": (
                self._last_heartbeat_at.isoformat()
                if self._last_heartbeat_at is not None
                else None
            ),
            "scheduler_last_error_code": self._scheduler_last_error_code,
            "scheduler_heartbeat_max_age_seconds": self._heartbeat_max_age_seconds,
        }

    def run_once(
        self,
        *,
        force: bool = False,
        now: datetime | None = None,
        raise_on_failure: bool = False,
        actor: ControlPrincipal | None = None,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        observed = _observed(now)
        run_actor = actor or self.actor
        if client_request_id is not None:
            if _CLIENT_REQUEST_ID.fullmatch(client_request_id) is None:
                raise ValueError("Bootstrap freshness request identity is invalid")
            replay = self.repository.replay_bootstrap_freshness_manual_refresh(
                actor=run_actor,
                client_request_id=client_request_id,
            )
            if replay is not None:
                return replay
        if not self._run_lock.acquire(blocking=False):
            return {**self.status(now=observed), "run_state": "busy"}
        attempt_id: str | None = None
        try:
            recovered = self.repository.acquire_bootstrap_freshness_completion(
                owner_id=self.config.owner_id,
                lease_seconds=self.config.lease_seconds,
                check_interval_seconds=self.config.check_interval_seconds,
                now=observed,
            )
            if recovered is not None:
                if recovered["state"] == "busy":
                    return {**self.status(now=observed), "run_state": "busy"}
                attempt_id = str(recovered["attempt_record_id"])
                result = self.repository.complete_bootstrap_freshness_refresh(
                    attempt_record_id=attempt_id,
                    owner_id=self.config.owner_id,
                    activation_record_id=str(recovered["activation_record_id"]),
                    proof_record_id=str(recovered["proof_record_id"]),
                    actor=run_actor,
                    check_interval_seconds=self.config.check_interval_seconds,
                    now=observed,
                )
                if self.signer is None:
                    result = self.repository.fail_bootstrap_freshness_refresh(
                        attempt_record_id=None,
                        owner_id=None,
                        error_code="signer-unavailable",
                        actor=run_actor,
                        check_interval_seconds=self.config.check_interval_seconds,
                        signer_configured=False,
                        now=observed,
                    )
                    if raise_on_failure:
                        raise BootstrapFreshnessRefreshError(
                            "Bootstrap freshness signer is unavailable"
                        )
                    return self._terminal_result(
                        result,
                        run_state="unconfigured",
                        actor=run_actor,
                        client_request_id=client_request_id,
                    )
                return self._terminal_result(
                    result,
                    run_state="succeeded",
                    actor=run_actor,
                    client_request_id=client_request_id,
                )
            if self.signer is None:
                result = self.repository.fail_bootstrap_freshness_refresh(
                    attempt_record_id=None,
                    owner_id=None,
                    error_code="signer-unavailable",
                    actor=run_actor,
                    check_interval_seconds=self.config.check_interval_seconds,
                    signer_configured=False,
                    now=observed,
                )
                if raise_on_failure:
                    raise BootstrapFreshnessRefreshError(
                        "Bootstrap freshness signer is unavailable"
                    )
                return self._terminal_result(
                    result,
                    run_state="unconfigured",
                    actor=run_actor,
                    client_request_id=client_request_id,
                )
            begun = self.repository.begin_bootstrap_freshness_refresh(
                owner_id=self.config.owner_id,
                force=force,
                lead_seconds=self.config.lead_seconds,
                check_interval_seconds=self.config.check_interval_seconds,
                lease_seconds=self.config.lease_seconds,
                actor=run_actor,
                now=observed,
            )
            if begun["state"] != "acquired":
                status = self.status(now=observed)
                if begun["state"] == "busy":
                    return {**status, "run_state": "busy"}
                return self._terminal_result(
                    status,
                    run_state=str(begun["state"]),
                    actor=run_actor,
                    client_request_id=client_request_id,
                    decorated=True,
                )
            attempt_id = str(begun["attempt_record_id"])
            candidate_bytes = begun.get("candidate_index_bytes")
            if candidate_bytes is None:
                source = json.loads(bytes(begun["source_index_bytes"]).decode("utf-8"))
                refreshed = refresh_public_bootstrap_freshness(
                    source,
                    verifier=self.repository.verifier,
                    freshness_verifier=(
                        self.repository._require_bootstrap_freshness_verifier()
                    ),
                    freshness_signer=self.signer,
                    issued_at=str(begun["issued_at"]),
                    expires_at=str(begun["expires_at"]),
                    now=observed,
                    allow_legacy_v1017_sequence=True,
                )
                candidate_bytes = (
                    json.dumps(
                        refreshed,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                    + b"\n"
                )
                prepared = self.repository.store_bootstrap_freshness_preparation(
                    attempt_record_id=attempt_id,
                    owner_id=self.config.owner_id,
                    index_bytes=candidate_bytes,
                    signer_key_id=self.signer.key_id,
                    actor=run_actor,
                    now=_phase_time(now, observed),
                )
                candidate_bytes = prepared["index_bytes"]
            self.repository.renew_bootstrap_freshness_refresh_lease(
                attempt_record_id=attempt_id,
                owner_id=self.config.owner_id,
                lease_seconds=self.config.lease_seconds,
                now=_phase_time(now, observed),
            )
            stage_request_id = _request_id(attempt_id, "stage")
            staged = self.publication_service.stage(
                bytes(candidate_bytes),
                actor=run_actor,
                client_request_id=stage_request_id,
            )
            self.repository.renew_bootstrap_freshness_refresh_lease(
                attempt_record_id=attempt_id,
                owner_id=self.config.owner_id,
                lease_seconds=self.config.lease_seconds,
                now=_phase_time(now, observed),
            )
            active = self.publication_service.activate(
                release_id=str(staged["release_id"]),
                request=_activation_request(staged),
                actor=run_actor,
                client_request_id=_request_id(attempt_id, "activate"),
            )
            proof = active["readback"]
            result = self.repository.complete_bootstrap_freshness_refresh(
                attempt_record_id=attempt_id,
                owner_id=self.config.owner_id,
                activation_record_id=str(active["active_activation_record_id"]),
                proof_record_id=str(proof["record_id"]),
                actor=run_actor,
                check_interval_seconds=self.config.check_interval_seconds,
                now=_phase_time(now, observed),
            )
            return self._terminal_result(
                result,
                run_state="succeeded",
                actor=run_actor,
                client_request_id=client_request_id,
            )
        except Exception as exc:
            code = "refresh-" + type(exc).__name__.casefold().replace("_", "-")
            code = code[:120]
            try:
                result = self.repository.fail_bootstrap_freshness_refresh(
                    attempt_record_id=attempt_id,
                    owner_id=self.config.owner_id if attempt_id is not None else None,
                    error_code=code,
                    actor=run_actor,
                    check_interval_seconds=self.config.check_interval_seconds,
                    now=observed,
                )
            except Exception:
                result = self.repository.bootstrap_freshness_refresh_status(
                    now=observed
                )
            if raise_on_failure:
                raise BootstrapFreshnessRefreshError(
                    f"Bootstrap freshness refresh failed safely: {type(exc).__name__}"
                ) from None
            return self._terminal_result(
                result,
                run_state="failed",
                actor=run_actor,
                client_request_id=client_request_id,
            )
        finally:
            self._run_lock.release()

    def _terminal_result(
        self,
        result: dict[str, Any],
        *,
        run_state: str,
        actor: ControlPrincipal,
        client_request_id: str | None,
        decorated: bool = False,
    ) -> dict[str, Any]:
        projection = {
            **(result if decorated else self._decorate_status(result)),
            "run_state": run_state,
        }
        if client_request_id is None:
            return projection
        return self.repository.remember_bootstrap_freshness_manual_refresh(
            actor=actor,
            client_request_id=client_request_id,
            response=projection,
        )


def _activation_request(staged: dict[str, object]) -> dict[str, object]:
    return {
        "revision_id": staged["revision_id"],
        "index_sha256": staged["index_sha256"],
        "expected_previous_activation_record_id": staged["active_activation_record_id"],
        "expected_previous_sequence": staged["active_sequence"],
        "expected_previous_authority_revision_id": staged[
            "active_authority_revision_id"
        ],
        "expected_previous_index_sha256": staged["active_index_sha256"],
        "expected_previous_target": staged["active_target"],
    }


def _request_id(attempt_id: str, phase: str) -> str:
    return (
        "release_"
        + hashlib.sha256((attempt_id + "\0" + phase).encode("ascii")).hexdigest()[:32]
    )


def _observed(value: datetime | None) -> datetime:
    observed = datetime.now(UTC) if value is None else value
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Bootstrap freshness clock must be timezone-aware")
    return observed.astimezone(UTC).replace(microsecond=0)


def _phase_time(requested: datetime | None, initial: datetime) -> datetime:
    """Advance production leases while keeping injected test clocks deterministic."""

    return initial if requested is not None else _observed(None)


def _safe_error_code(prefix: str, error: BaseException) -> str:
    return (prefix + "-" + type(error).__name__.casefold().replace("_", "-"))[:120]


__all__ = [
    "BootstrapFreshnessConfig",
    "BootstrapFreshnessRefreshError",
    "BootstrapFreshnessRefresher",
]
