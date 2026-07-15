"""Bounded image workers with uncertainty recovery and durable retries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import random
import re
import secrets
from typing import Awaitable, Callable

from .cas import ImageContentAddressedStore, ImageContentReference
from .models import (
    ImageJob,
    ImageJobStatus,
    ImageLeaseLost,
    ImageResult,
    ImageResultRejected,
    ImageUsage,
    utc_now,
)
from .provider import (
    ImageProvider,
    ProviderError,
    ProviderOutOfMemory,
    ProviderRateLimited,
    ProviderRejected,
    ProviderResult,
    ProviderState,
    ProviderUncertain,
    ProviderUnavailable,
)
from .store import ImageJobStore


class ImageWorkerOutcome(StrEnum):
    IDLE = "idle"
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class ImageWorkerResult:
    outcome: ImageWorkerOutcome
    job_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ImageSupervisorSnapshot:
    running: bool
    accepting: bool
    healthy: bool
    concurrency: int
    in_flight: int
    completed: int
    retries: int
    failures: int
    lost: int
    loop_errors: int


class ImageJobWorker:
    def __init__(
        self,
        store: ImageJobStore,
        provider: ImageProvider,
        content_store: ImageContentAddressedStore,
        *,
        clock: Callable[[], datetime] = utc_now,
        lease_seconds: int = 30,
        heartbeat_seconds: float = 5,
        base_retry_seconds: float = 1,
        max_retry_seconds: float = 300,
        random_source: random.Random | None = None,
        breaker_threshold: int = 5,
        breaker_cooldown_seconds: int = 30,
        breaker_probe_seconds: int = 300,
    ) -> None:
        if not isinstance(store, ImageJobStore):
            raise TypeError("store does not implement ImageJobStore")
        if not isinstance(provider, ImageProvider):
            raise TypeError("provider does not implement ImageProvider")
        if not isinstance(content_store, ImageContentAddressedStore):
            raise TypeError("content_store does not implement image CAS")
        if (
            store.deployment_scope not in {"local", "shared"}
            or content_store.deployment_scope not in {"local", "shared"}
            or store.deployment_scope != content_store.deployment_scope
        ):
            raise ValueError(
                "shared image jobs require shared content-addressed storage"
            )
        if not 5 <= lease_seconds <= 300:
            raise ValueError("lease_seconds is invalid")
        if not 0.1 <= heartbeat_seconds < lease_seconds:
            raise ValueError("heartbeat_seconds must be below the lease")
        if not 0.01 <= base_retry_seconds <= max_retry_seconds <= 3600:
            raise ValueError("retry bounds are invalid")
        if isinstance(breaker_threshold, bool) or not 1 <= breaker_threshold <= 100:
            raise ValueError("breaker threshold is invalid")
        if (
            isinstance(breaker_cooldown_seconds, bool)
            or not 1 <= breaker_cooldown_seconds <= 3600
        ):
            raise ValueError("breaker cooldown is invalid")
        if (
            isinstance(breaker_probe_seconds, bool)
            or not 1 <= breaker_probe_seconds <= 3600
        ):
            raise ValueError("breaker probe duration is invalid")
        self.store = store
        self.provider = provider
        self.content_store = content_store
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.base_retry_seconds = base_retry_seconds
        self.max_retry_seconds = max_retry_seconds
        self.random = random_source or random.SystemRandom()
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown_seconds = breaker_cooldown_seconds
        self.breaker_probe_seconds = breaker_probe_seconds

    async def run_once(self, worker_id: str) -> ImageWorkerResult:
        job = await asyncio.to_thread(
            self.store.lease_next, worker_id, lease_seconds=self.lease_seconds
        )
        if job is None:
            return ImageWorkerResult(ImageWorkerOutcome.IDLE)
        assert job.lease_token is not None
        token = job.lease_token
        scope = self._breaker_scope(job)
        recover_first = False
        try:
            if self._has_staged_result(job):
                return await self._resume_staged_result(job, token, scope)

            circuit = await asyncio.to_thread(
                self.store.admit_provider_call,
                scope,
                probe_seconds=self.breaker_probe_seconds,
            )
            if not circuit.admitted:
                assert circuit.retry_at is not None
                checkpoint = dict(job.checkpoint)
                checkpoint["phase"] = "provider_circuit_wait"
                return await self._retry(
                    job,
                    token,
                    "provider_circuit_open",
                    checkpoint=checkpoint,
                    minimum=circuit.retry_at,
                )
            checkpoint = dict(job.checkpoint)
            recover_first = bool(
                checkpoint.get("provider_started")
                or checkpoint.get("provider_uncertain")
                or job.provider_request_id
            )
            checkpoint.update(
                {
                    "provider_started": True,
                    "provider_uncertain": recover_first,
                    "phase": "provider",
                    "circuit_half_open": circuit.half_open,
                }
            )
            provider_request_id = job.provider_request_id or checkpoint.get(
                "provider_request_id"
            )
            job = await asyncio.to_thread(
                self.store.transition,
                job.job_id,
                token,
                expected=(ImageJobStatus.LEASED.value,),
                target=ImageJobStatus.RUNNING.value,
                checkpoint=checkpoint,
                provider_request_id=provider_request_id,
            )
            if recover_first:
                result = await self._invoke(
                    job,
                    token,
                    lambda: self.provider.recover(
                        job,
                        idempotency_key=job.provider_idempotency_key,
                        provider_request_id=job.provider_request_id,
                    ),
                )
                if result.state is ProviderState.NOT_FOUND:
                    result = await self._invoke(
                        job,
                        token,
                        lambda: self.provider.submit(
                            job, idempotency_key=job.provider_idempotency_key
                        ),
                    )
            else:
                result = await self._invoke(
                    job,
                    token,
                    lambda: self.provider.submit(
                        job, idempotency_key=job.provider_idempotency_key
                    ),
                )
            return await self._accept_provider_result(job, token, scope, result)
        except ImageLeaseLost:
            await self._cancel_provider_best_effort(job)
            current = await self._safe_get(job.job_id)
            if current is not None:
                if current.status is ImageJobStatus.COMPLETED:
                    return ImageWorkerResult(
                        ImageWorkerOutcome.COMPLETED,
                        job.job_id,
                        "commit_observed",
                    )
                if current.status is ImageJobStatus.CANCELLED:
                    return ImageWorkerResult(
                        ImageWorkerOutcome.CANCELLED,
                        job.job_id,
                        "cancelled",
                    )
                if current.status in {
                    ImageJobStatus.FAILED,
                    ImageJobStatus.DEAD_LETTER,
                }:
                    return ImageWorkerResult(
                        ImageWorkerOutcome.FAILED,
                        job.job_id,
                        current.last_error_code,
                    )
            return ImageWorkerResult(ImageWorkerOutcome.LOST, job.job_id, "lease_lost")
        except asyncio.CancelledError:
            # A draining process must not leave an upstream request running
            # without even attempting cancellation.  The database lease is
            # deliberately left fenced and will be recovered as uncertain by
            # another worker after expiry.
            await self._cancel_provider_best_effort(job)
            raise
        except ProviderRateLimited as error:
            should_recover = recover_first or error.recovery_required
            checkpoint = dict(job.checkpoint)
            checkpoint.update(
                {
                    "provider_started": should_recover,
                    "provider_uncertain": should_recover,
                    "phase": "provider_rate_limited",
                }
            )
            now = self._now()
            retry_delay = (
                error.retry_after_seconds
                if error.retry_after_seconds is not None
                else max(1.0, self._retry_delay(job))
            )
            minimum = await asyncio.to_thread(
                self.store.record_provider_rate_limit,
                scope,
                retry_at=now + timedelta(seconds=retry_delay),
                cooldown_seconds=self.breaker_cooldown_seconds,
            )
            return await self._retry(
                job,
                token,
                error.code,
                checkpoint=checkpoint,
                minimum=minimum,
            )
        except ProviderRejected as error:
            # A bounded explicit rejection proves the provider transport is
            # responsive; it must close a half-open availability circuit even
            # though this individual job is terminal.
            await self._record_success_best_effort(scope)
            return await self._fail(job, token, error.code)
        except ProviderError as error:
            if error.retryable:
                await asyncio.to_thread(
                    self.store.record_provider_failure,
                    scope,
                    threshold=self.breaker_threshold,
                    cooldown_seconds=self.breaker_cooldown_seconds,
                )
                return await self._retry(
                    job,
                    token,
                    error.code,
                    checkpoint={
                        **dict(job.checkpoint),
                        "provider_started": True,
                        "provider_uncertain": isinstance(error, ProviderUncertain),
                    },
                )
            return await self._fail(job, token, error.code)
        except MemoryError:
            await self._record_failure(scope)
            return await self._retry(
                job,
                token,
                ProviderOutOfMemory.code,
                checkpoint={**dict(job.checkpoint), "provider_uncertain": True},
            )
        except (TimeoutError, ConnectionError, OSError):
            await self._record_failure(scope)
            return await self._retry(
                job,
                token,
                ProviderUnavailable.code,
                checkpoint={**dict(job.checkpoint), "provider_uncertain": True},
            )
        except Exception:
            # The external call may have crossed the provider boundary. Never
            # blind-submit a new request; the next attempt must recover first.
            await self._record_failure(scope)
            return await self._retry(
                job,
                token,
                ProviderUncertain.code,
                checkpoint={
                    **dict(job.checkpoint),
                    "provider_started": True,
                    "provider_uncertain": True,
                },
            )

    async def _invoke(
        self,
        job: ImageJob,
        lease_token: str,
        call: Callable[[], Awaitable[ProviderResult]],
    ) -> ProviderResult:
        task = asyncio.create_task(call())
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {task}, timeout=self.heartbeat_seconds
                )
                if task in done:
                    result = task.result()
                    if not isinstance(result, ProviderResult):
                        raise ProviderUncertain("provider returned an invalid result")
                    return result
                await asyncio.to_thread(
                    self.store.heartbeat,
                    job.job_id,
                    lease_token,
                    lease_seconds=self.lease_seconds,
                )
        except BaseException:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

    async def _accept_provider_result(
        self,
        job: ImageJob,
        token: str,
        scope: str,
        result: ProviderResult,
    ) -> ImageWorkerResult:
        if result.state is ProviderState.PENDING:
            return await self._retry(
                job,
                token,
                "provider_pending",
                checkpoint={
                    "provider_started": True,
                    "provider_uncertain": True,
                    "phase": "provider_pending",
                    "provider_request_id": result.provider_request_id,
                },
                provider_request_id=result.provider_request_id,
            )
        if result.state is ProviderState.NOT_FOUND:
            return await self._retry(
                job,
                token,
                ProviderUncertain.code,
                checkpoint={"provider_started": True, "provider_uncertain": True},
            )
        if result.state is ProviderState.FAILED:
            code = result.error_code or ProviderRejected.code
            code = self._safe_error_code(code)
            await self._record_success_best_effort(scope)
            return await self._fail(job, token, code)
        assert result.state is ProviderState.COMPLETED
        checkpoint = {
            "provider_started": True,
            "provider_uncertain": False,
            "phase": "verifying",
        }
        job = await asyncio.to_thread(
            self.store.transition,
            job.job_id,
            token,
            expected=(ImageJobStatus.RUNNING.value,),
            target=ImageJobStatus.VERIFYING.value,
            checkpoint=checkpoint,
            provider_request_id=result.provider_request_id,
        )
        stored = await self._invoke_blocking(
            job,
            token,
            lambda: self.content_store.put(
                result.payload,
                mime_type=result.mime_type,
                expected_sha256=result.sha256,
                reference=ImageContentReference("job-result", job.job_id),
            ),
        )
        assert result.usage is not None
        staged_checkpoint = self._staged_checkpoint(stored, result.usage)
        job = await asyncio.to_thread(
            self.store.transition,
            job.job_id,
            token,
            expected=(ImageJobStatus.VERIFYING.value,),
            target=ImageJobStatus.COMMITTING.value,
            checkpoint=staged_checkpoint,
            provider_request_id=result.provider_request_id,
        )
        try:
            completed = await asyncio.to_thread(
                self.store.complete,
                job.job_id,
                token,
                result=stored,
                usage=result.usage,
            )
        except Exception:
            # The CAS bytes and their exact usage commitment are already
            # durable.  Persist them through a retry so restart recovery can
            # finish the local commit without another provider submission.
            return await self._retry(
                job,
                token,
                ProviderUncertain.code,
                checkpoint={
                    **staged_checkpoint,
                    "provider_started": True,
                    "provider_uncertain": True,
                },
            )
        await self._record_success_best_effort(scope)
        return ImageWorkerResult(ImageWorkerOutcome.COMPLETED, completed.job_id)

    async def _invoke_blocking(
        self,
        job: ImageJob,
        lease_token: str,
        call: Callable[[], object],
    ) -> object:
        """Run blocking CAS work while continuously renewing the job lease."""

        task = asyncio.create_task(asyncio.to_thread(call))
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {task}, timeout=self.heartbeat_seconds
                )
                if task in done:
                    return task.result()
                await asyncio.to_thread(
                    self.store.heartbeat,
                    job.job_id,
                    lease_token,
                    lease_seconds=self.lease_seconds,
                )
        except BaseException:
            if not task.done():
                # Cancelling ``to_thread`` cannot roll back an already-started
                # CAS write.  That write is content-addressed and idempotent;
                # fencing still prevents this worker from publishing it.
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

    @staticmethod
    def _staged_checkpoint(
        result: ImageResult,
        usage: ImageUsage,
    ) -> dict[str, object]:
        return {
            "phase": "committing",
            "provider_started": True,
            "provider_uncertain": False,
            "staged_result": {
                "sha256": result.sha256,
                "size_bytes": result.size_bytes,
                "mime_type": result.mime_type,
            },
            "staged_usage": usage.to_dict(),
        }

    @staticmethod
    def _has_staged_result(job: ImageJob) -> bool:
        checkpoint = job.checkpoint
        return "staged_result" in checkpoint or "staged_usage" in checkpoint

    def _parse_staged_result(
        self,
        job: ImageJob,
    ) -> tuple[ImageResult, ImageUsage]:
        result = job.checkpoint.get("staged_result")
        usage = job.checkpoint.get("staged_usage")
        if not isinstance(result, dict) or set(result) != {
            "sha256",
            "size_bytes",
            "mime_type",
        }:
            raise ValueError("staged image result commitment is invalid")
        if not isinstance(usage, dict) or set(usage) != {
            "provider",
            "model_id",
            "input_units",
            "output_units",
            "billed_units",
        }:
            raise ValueError("staged image usage commitment is invalid")
        parsed_result = ImageResult(**result)
        parsed_usage = ImageUsage(**usage)
        if (
            parsed_usage.model_id != job.request.model_id
            or parsed_usage.provider != self.provider.provider_id
        ):
            raise ValueError("staged image usage identity changed")
        return parsed_result, parsed_usage

    async def _resume_staged_result(
        self,
        job: ImageJob,
        token: str,
        scope: str,
    ) -> ImageWorkerResult:
        try:
            result, usage = self._parse_staged_result(job)
        except (TypeError, ValueError):
            return await self._fail(job, token, "staged_result_invalid")

        checkpoint = dict(job.checkpoint)
        checkpoint["phase"] = "staged_result_recovery"
        job = await asyncio.to_thread(
            self.store.transition,
            job.job_id,
            token,
            expected=(ImageJobStatus.LEASED.value,),
            target=ImageJobStatus.RUNNING.value,
            checkpoint=checkpoint,
            provider_request_id=job.provider_request_id,
        )
        job = await asyncio.to_thread(
            self.store.transition,
            job.job_id,
            token,
            expected=(ImageJobStatus.RUNNING.value,),
            target=ImageJobStatus.VERIFYING.value,
            checkpoint=checkpoint,
            provider_request_id=job.provider_request_id,
        )
        try:
            metadata = await self._invoke_blocking(
                job,
                token,
                lambda: self.content_store.describe(result.sha256),
            )
            if metadata.result != result:
                raise ImageResultRejected("staged image commitment changed")
            payload = await self._invoke_blocking(
                job,
                token,
                lambda: self.content_store.read(result.sha256),
            )
            if not isinstance(payload, bytes) or len(payload) != result.size_bytes:
                raise ImageResultRejected("staged image bytes changed")
            del payload
        except ImageResultRejected:
            # The provider effect may already exist, but the staged copy is not
            # trustworthy.  Recover by the stable provider identity rather
            # than publishing corrupt bytes or blindly submitting again.
            recovery = {
                key: value
                for key, value in checkpoint.items()
                if key not in {"staged_result", "staged_usage"}
            }
            recovery.update(
                {
                    "phase": "staged_result_rejected",
                    "provider_started": True,
                    "provider_uncertain": True,
                }
            )
            return await self._retry(
                job,
                token,
                "staged_result_rejected",
                checkpoint=recovery,
            )

        checkpoint["phase"] = "committing"
        job = await asyncio.to_thread(
            self.store.transition,
            job.job_id,
            token,
            expected=(ImageJobStatus.VERIFYING.value,),
            target=ImageJobStatus.COMMITTING.value,
            checkpoint=checkpoint,
            provider_request_id=job.provider_request_id,
        )
        completed = await asyncio.to_thread(
            self.store.complete,
            job.job_id,
            token,
            result=result,
            usage=usage,
        )
        await self._record_success_best_effort(scope)
        return ImageWorkerResult(ImageWorkerOutcome.COMPLETED, completed.job_id)

    async def _record_failure(self, scope: str) -> None:
        await asyncio.to_thread(
            self.store.record_provider_failure,
            scope,
            threshold=self.breaker_threshold,
            cooldown_seconds=self.breaker_cooldown_seconds,
        )

    async def _record_success_best_effort(self, scope: str) -> None:
        try:
            await asyncio.to_thread(self.store.record_provider_success, scope)
        except Exception:
            # A durable result or explicit provider response remains the fact;
            # breaker bookkeeping cannot turn it into a duplicate execution.
            return

    async def _retry(
        self,
        job: ImageJob,
        token: str,
        error_code: str,
        *,
        checkpoint: dict,
        minimum: datetime | None = None,
        provider_request_id: str | None = None,
    ) -> ImageWorkerResult:
        if provider_request_id is not None:
            checkpoint["provider_request_id"] = provider_request_id
        delay = self._retry_delay(job)
        available = self._now() + timedelta(seconds=delay)
        if minimum is not None:
            available = max(available, minimum)
        retried = await asyncio.to_thread(
            self.store.schedule_retry,
            job.job_id,
            token,
            error_code=self._safe_error_code(error_code),
            available_at=available,
            checkpoint=checkpoint,
        )
        outcome = (
            ImageWorkerOutcome.FAILED
            if retried.status is ImageJobStatus.DEAD_LETTER
            else ImageWorkerOutcome.RETRY_SCHEDULED
        )
        return ImageWorkerResult(outcome, job.job_id, retried.last_error_code)

    def _retry_delay(self, job: ImageJob) -> float:
        delay = min(
            self.max_retry_seconds,
            self.base_retry_seconds * (2 ** max(0, job.attempt - 1)),
        )
        return delay * self.random.uniform(0.75, 1.25)

    async def _fail(self, job: ImageJob, token: str, code: str) -> ImageWorkerResult:
        failed = await asyncio.to_thread(
            self.store.fail,
            job.job_id,
            token,
            error_code=self._safe_error_code(code),
        )
        return ImageWorkerResult(ImageWorkerOutcome.FAILED, failed.job_id, code)

    async def _cancel_provider_best_effort(self, job: ImageJob) -> None:
        try:
            await self.provider.cancel(
                job,
                idempotency_key=job.provider_idempotency_key,
                provider_request_id=job.provider_request_id,
            )
        except Exception:
            return

    async def _safe_get(self, job_id: str) -> ImageJob | None:
        try:
            return await asyncio.to_thread(self.store.get, job_id)
        except Exception:
            return None

    def _breaker_scope(self, job: ImageJob) -> str:
        return "/".join(
            (
                self.provider.provider_id,
                job.request.model_id,
                job.request.operation.value,
                job.request.size_class,
            )
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("worker clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _safe_error_code(value: str) -> str:
        normalized = str(value or "provider_error").strip().casefold().replace("-", "_")
        if not re.fullmatch(r"[a-z][a-z0-9_.:]{0,127}", normalized):
            return "provider_error"
        return normalized


class ImageWorkerSupervisor:
    def __init__(
        self,
        worker: ImageJobWorker,
        *,
        concurrency: int = 8,
        idle_poll_seconds: float = 0.25,
        shutdown_seconds: float = 10,
        worker_id_prefix: str | None = None,
    ) -> None:
        if not 1 <= concurrency <= 256:
            raise ValueError("image worker concurrency is invalid")
        if not 0.01 <= idle_poll_seconds <= 60 or not 0.1 <= shutdown_seconds <= 300:
            raise ValueError("image worker timing is invalid")
        resolved_prefix = worker_id_prefix or (
            "imgworker-" + secrets.token_hex(12)
        )
        if (
            not isinstance(resolved_prefix, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{2,239}", resolved_prefix)
        ):
            raise ValueError("image worker identity prefix is invalid")
        self.worker = worker
        self.concurrency = concurrency
        self.idle_poll_seconds = idle_poll_seconds
        self.shutdown_seconds = shutdown_seconds
        self.worker_id_prefix = resolved_prefix
        self._semaphore = asyncio.Semaphore(concurrency)
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._completed = 0
        self._retries = 0
        self._failures = 0
        self._lost = 0
        self._in_flight = 0
        self._loop_errors = 0
        self._last_loop_failed = False

    @property
    def running(self) -> bool:
        return (
            len(self._tasks) == self.concurrency
            and all(not task.done() for task in self._tasks)
        )

    @property
    def accepting(self) -> bool:
        return self.running and not self._stopping

    @property
    def healthy(self) -> bool:
        return self.accepting and not self._last_loop_failed

    async def start(self) -> None:
        if self.running:
            return
        self._stopping = False
        self._last_loop_failed = False
        await asyncio.to_thread(self.worker.store.reclaim_expired)
        self._tasks = [
            asyncio.create_task(self._loop(index), name=f"image-worker-{index}")
            for index in range(self.concurrency)
        ]

    async def stop(self) -> None:
        self.begin_drain()
        tasks = list(self._tasks)
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self.shutdown_seconds,
                )
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def begin_drain(self) -> None:
        """Stop leasing new work while allowing current calls to checkpoint."""

        self._stopping = True
        self._wake.set()

    def notify(self) -> None:
        self._wake.set()

    def snapshot(self) -> ImageSupervisorSnapshot:
        return ImageSupervisorSnapshot(
            running=self.running,
            accepting=self.accepting,
            healthy=self.healthy,
            concurrency=self.concurrency,
            in_flight=self._in_flight,
            completed=self._completed,
            retries=self._retries,
            failures=self._failures,
            lost=self._lost,
            loop_errors=self._loop_errors,
        )

    async def _loop(self, index: int) -> None:
        worker_id = f"{self.worker_id_prefix}-{index:03d}"
        while not self._stopping:
            async with self._semaphore:
                self._in_flight += 1
                try:
                    result = await self.worker.run_once(worker_id)
                    self._last_loop_failed = False
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._failures += 1
                    self._loop_errors += 1
                    self._last_loop_failed = True
                    result = ImageWorkerResult(ImageWorkerOutcome.FAILED)
                finally:
                    self._in_flight -= 1
            if result.outcome is ImageWorkerOutcome.IDLE:
                self._wake.clear()
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.idle_poll_seconds
                    )
                except TimeoutError:
                    pass
            elif result.outcome is ImageWorkerOutcome.COMPLETED:
                self._completed += 1
            elif result.outcome is ImageWorkerOutcome.RETRY_SCHEDULED:
                self._retries += 1
            elif result.outcome is ImageWorkerOutcome.LOST:
                self._lost += 1
            elif result.outcome is ImageWorkerOutcome.FAILED:
                self._failures += 1


__all__ = [
    "ImageJobWorker",
    "ImageSupervisorSnapshot",
    "ImageWorkerOutcome",
    "ImageWorkerResult",
    "ImageWorkerSupervisor",
]
