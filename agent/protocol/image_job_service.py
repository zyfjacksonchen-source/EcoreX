"""Backend-owned image job event service.

The service is intentionally small: providers/routing can evolve around it, but
job truth must be durable runtime events that RuntimeProjection can replay.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from config import conf
from common.image_quality_runtime import (
    aggregate_image_finalization_decisions,
    attach_image_finalization_evidence,
    build_image_finalization_decision,
    build_image_quality_evidence,
)

from .run_event_ledger import RunEventLedger, get_run_event_ledger


ImageJobRunner = Callable[
    [Dict[str, Any], Callable[..., Dict[str, Any]], threading.Event],
    Any,
]
ImageJobOcrProvider = Callable[[Dict[str, Any]], Any]
IMAGE_JOB_PRODUCTION_HARD_MAX_PARALLEL = 8
IMAGE_JOB_BASELINE_SECONDS = 120.0


class ImageJobCancelled(RuntimeError):
    """Raised by an image task runner after observing its cancel event."""


@dataclass
class _ImageJobState:
    job_id: str
    request_id: str
    session_id: str = ""
    turn_id: str = ""
    status: str = "queued"
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    finished_at: float = 0.0
    in_flight: bool = True
    observation_started_at: float = 0.0
    observation_started_wall_time: float = 0.0
    observation_last_progress_at: float = 0.0
    observation_last_heartbeat_at: float = 0.0
    observation_intervention_at: float = 0.0
    observation_soft_deadline_seconds: float = 1200.0
    observation_hard_deadline_seconds: float = 1800.0
    observation_stall_seconds: float = 1200.0
    observation_heartbeat_seconds: float = 60.0
    observation_watchdog_interval_seconds: float = 5.0
    observation_per_image_baseline_seconds: float = IMAGE_JOB_BASELINE_SECONDS
    observation_last_deadline_extension_at: float = 0.0
    observation_lease_count: int = 0
    observation_health: str = "running"
    observation_backgrounded: bool = False
    observation_terminal_emitted: bool = False
    observation_watchdog_stop: threading.Event = field(default_factory=threading.Event)
    observation_watchdog_thread: Optional[threading.Thread] = None


class ImageJobService:
    """Start, observe, collect, and cancel backend-led image jobs."""

    def __init__(self, event_ledger: Optional[RunEventLedger] = None):
        self.event_ledger = event_ledger or get_run_event_ledger()
        self._jobs: Dict[str, _ImageJobState] = {}
        self._ocr_cache = _ImageJobOcrBriefCache()
        self._lock = threading.RLock()

    def start(
        self,
        *,
        request_id: str,
        session_id: str = "",
        turn_id: str = "",
        operation: str = "generate",
        tasks: Iterable[Dict[str, Any]],
        runner: ImageJobRunner,
        job_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        max_parallel: int = 1,
        ocr_provider: Optional[ImageJobOcrProvider] = None,
        ocr_reuse: bool = False,
        synchronous: bool = False,
    ) -> Dict[str, Any]:
        request_id = str(request_id or "").strip()
        if not request_id:
            raise ValueError("request_id is required")
        operation = _safe_operation(operation)
        task_list = _prepare_task_list(tasks)
        if not task_list:
            raise ValueError("at least one image task is required")
        parallelism = _bounded_parallelism(max_parallel, len(task_list))
        job_id = _safe_job_id(job_id, fallback=f"image-job-{uuid.uuid4().hex[:16]}")
        state = _ImageJobState(job_id=job_id, request_id=request_id, session_id=session_id, turn_id=turn_id)
        _apply_observation_policy(state, metadata or {})
        ocr_cache_enabled = bool(ocr_reuse and ocr_provider)
        safe_started_metadata = dict(metadata or {})
        if ocr_reuse:
            safe_started_metadata["ocr_cache_enabled"] = ocr_cache_enabled
        with self._lock:
            self._jobs[job_id] = state
        self._emit(
            state,
            "image_job.started",
            {
                "job_id": job_id,
                "operation": operation,
                "task_count": len(task_list),
                "max_parallel": parallelism,
                "tasks": [_safe_task_projection(task, index) for index, task in enumerate(task_list)],
                **_safe_metadata(safe_started_metadata),
            },
            suffix="started",
        )
        self._start_observation(state, operation=operation, task_count=len(task_list), max_parallel=parallelism, metadata=safe_started_metadata)
        self._start_observation_watchdog(state)

        def _target() -> None:
            try:
                self._run_job(
                    state,
                    operation=operation,
                    tasks=task_list,
                    runner=runner,
                    metadata=metadata or {},
                    max_parallel=parallelism,
                    ocr_provider=ocr_provider if ocr_cache_enabled else None,
                )
            finally:
                with self._lock:
                    state.in_flight = False
                    if state.status in {"completed", "failed", "cancelled"} and not state.finished_at:
                        state.finished_at = time.monotonic()
                    state.observation_watchdog_stop.set()

        if synchronous:
            _target()
        else:
            thread = threading.Thread(target=_target, daemon=True, name=f"image-job-{job_id[:24]}")
            state.thread = thread
            thread.start()
        return self.status(job_id)

    def status(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            state = self._jobs.get(str(job_id or ""))
            if not state:
                return {"job_id": str(job_id or ""), "status": "unknown", "artifacts": []}
            return {
                "job_id": state.job_id,
                "request_id": state.request_id,
                "session_id": state.session_id,
                "status": state.status,
                "artifacts": _sort_artifacts_for_display([dict(item) for item in state.artifacts if isinstance(item, dict)]),
                "cancel_requested": state.cancel_event.is_set(),
                "running": bool(state.in_flight or (state.thread and state.thread.is_alive())),
                "finished_at": state.finished_at,
            }

    def collect(self, job_id: str, *, wait: bool = False, timeout: Optional[float] = None) -> Dict[str, Any]:
        state = self._jobs.get(str(job_id or ""))
        if state and wait and state.thread:
            state.thread.join(timeout=timeout)
        return self.status(job_id)

    def resource_snapshot(self) -> Dict[str, Any]:
        """Return redacted resource counters for lifecycle/performance gates."""
        with self._lock:
            states = list(self._jobs.values())
        running = 0
        terminal = 0
        artifact_count = 0
        for state in states:
            if state.status in {"completed", "failed", "cancelled"}:
                terminal += 1
            if state.in_flight or (state.thread and state.thread.is_alive()):
                running += 1
            artifact_count += len(state.artifacts)
        return {
            "jobCount": len(states),
            "runningJobCount": running,
            "terminalJobCount": terminal,
            "artifactCount": artifact_count,
            "ocrCacheEntries": self._ocr_cache.size(),
        }

    def cleanup_finished_jobs(self, *, max_age_seconds: float = 300.0, max_jobs: int = 128) -> Dict[str, Any]:
        """Prune completed image-job state without touching running jobs.

        Runtime truth remains in RunEventLedger; this only bounds the in-memory
        observer cache used for status polling and complex-task workflows.
        """
        now = time.monotonic()
        try:
            age = max(0.0, float(max_age_seconds))
        except (TypeError, ValueError):
            age = 300.0
        try:
            keep_limit = max(0, int(max_jobs))
        except (TypeError, ValueError):
            keep_limit = 128
        removed: List[str] = []
        with self._lock:
            terminal_items = [
                (job_id, state)
                for job_id, state in self._jobs.items()
                if state.status in {"completed", "failed", "cancelled"}
                and not state.in_flight
                and not (state.thread and state.thread.is_alive())
            ]
            for job_id, state in terminal_items:
                finished_at = float(state.finished_at or now)
                if now - finished_at >= age:
                    removed.append(job_id)
            removed_set = set(removed)
            remaining_terminal = [
                (job_id, state)
                for job_id, state in terminal_items
                if job_id not in removed_set
            ]
            if keep_limit >= 0 and len(remaining_terminal) > keep_limit:
                overflow = len(remaining_terminal) - keep_limit
                remaining_terminal.sort(key=lambda item: float(item[1].finished_at or 0.0))
                removed.extend(job_id for job_id, _state in remaining_terminal[:overflow])
            for job_id in removed:
                self._jobs.pop(job_id, None)
        return {
            "removedJobCount": len(set(removed)),
            "remaining": self.resource_snapshot(),
        }

    def cancel(self, job_id: str, *, reason: str = "cancel_requested") -> Dict[str, Any]:
        with self._lock:
            state = self._jobs.get(str(job_id or ""))
            if not state:
                return {"job_id": str(job_id or ""), "status": "unknown", "cancelled": False}
            state.cancel_event.set()
            if state.status in {"completed", "failed", "cancelled"}:
                return {**self.status(job_id), "cancelled": state.status == "cancelled"}
            state.status = "cancelled"
            state.finished_at = time.monotonic()
        self._finish_observation(state, "cancelled", reason=_safe_cancel_reason(reason))
        self._emit(
            state,
            "image_job.cancelled",
            {"job_id": state.job_id, "reason": _safe_cancel_reason(reason)},
            suffix="cancelled",
        )
        return {**self.status(job_id), "cancelled": True}

    def observation_action(self, job_id: str, *, action: str) -> Dict[str, Any]:
        """Apply a user/runtime decision to an active image-job observation."""
        safe_action = str(action or "").strip().lower().replace("-", "_")
        if safe_action == "continue":
            safe_action = "extend"
        if safe_action not in {"extend", "background"}:
            return {"job_id": str(job_id or ""), "status": "error", "message": "unsupported observation action"}
        with self._lock:
            state = self._jobs.get(str(job_id or ""))
            if not state:
                return {"job_id": str(job_id or ""), "status": "unknown", "action": safe_action}
            if state.status in {"completed", "failed", "cancelled"}:
                return {**self.status(job_id), "action": safe_action, "observation_applied": False}
            now = time.monotonic()
            state.observation_lease_count += 1
            state.observation_intervention_at = 0.0
            state.observation_last_progress_at = now
            if safe_action == "background":
                state.observation_backgrounded = True
                state.observation_health = "backgrounded"
                state.observation_watchdog_stop.set()
                next_health = "backgrounded"
            else:
                extension = max(state.observation_soft_deadline_seconds, state.observation_stall_seconds, 1.0)
                state.observation_soft_deadline_seconds += extension
                state.observation_hard_deadline_seconds = max(
                    state.observation_hard_deadline_seconds + extension,
                    state.observation_soft_deadline_seconds,
                )
                state.observation_health = "extended"
                next_health = "extended"
        self._emit_task_event(
            state,
            "task.health_changed",
            {
                "status": next_health,
                "health": next_health,
                "action": safe_action,
                "reason": f"image_job_observation_{safe_action}",
            },
            suffix=f"observation-action:{safe_action}:{time.monotonic_ns()}",
        )
        return {**self.status(job_id), "action": safe_action, "observation_applied": True}

    def _start_observation(
        self,
        state: _ImageJobState,
        *,
        operation: str,
        task_count: int,
        max_parallel: int,
        metadata: Dict[str, Any],
    ) -> None:
        now = time.monotonic()
        wall_time = time.time()
        with self._lock:
            state.observation_started_at = now
            state.observation_started_wall_time = wall_time
            state.observation_last_progress_at = now
            state.observation_last_heartbeat_at = now
            state.observation_health = "running"
        self._emit_task_event(
            state,
            "task.started",
            {
                "status": "running",
                "health": "running",
                "operation": _safe_operation(operation),
                "task_count": max(0, int(task_count or 0)),
                "max_parallel": max(1, int(max_parallel or 1)),
                **_safe_metadata(metadata),
            },
            suffix="task-started",
        )

    def _start_observation_watchdog(self, state: _ImageJobState) -> None:
        with self._lock:
            if state.observation_watchdog_thread is not None:
                return
            interval = max(0.05, float(state.observation_watchdog_interval_seconds or 5.0))
            stop_event = state.observation_watchdog_stop

        def _watchdog() -> None:
            while not stop_event.wait(interval):
                now = time.monotonic()
                heartbeat_due = False
                intervention_due = False
                reason = ""
                next_actions = ["continue", "stop", "background"]
                with self._lock:
                    if (
                        state.status in {"completed", "failed", "cancelled"}
                        or not state.in_flight
                        or state.observation_terminal_emitted
                        or state.observation_backgrounded
                    ):
                        return
                    started_at = state.observation_started_at or now
                    last_progress_at = state.observation_last_progress_at or started_at
                    elapsed = max(0.0, now - started_at)
                    stalled = max(0.0, now - last_progress_at)
                    if state.observation_heartbeat_seconds > 0 and now - state.observation_last_heartbeat_at >= state.observation_heartbeat_seconds:
                        heartbeat_due = True
                        state.observation_last_heartbeat_at = now
                    if not state.observation_intervention_at:
                        if elapsed >= state.observation_hard_deadline_seconds:
                            intervention_due = True
                            reason = "hard_deadline_exceeded"
                            next_actions = ["stop", "background", "continue"]
                        elif elapsed >= state.observation_soft_deadline_seconds:
                            intervention_due = True
                            reason = "soft_deadline_exceeded"
                        elif stalled >= state.observation_stall_seconds:
                            intervention_due = True
                            reason = "progress_stalled"
                        if intervention_due:
                            state.observation_intervention_at = now
                            state.observation_health = "waiting_user_decision"
                if heartbeat_due:
                    self._emit_task_event(
                        state,
                        "task.heartbeat",
                        {
                            "status": "running",
                            "health": state.observation_health,
                            "reason": "image_job_watchdog",
                        },
                        suffix=f"watchdog-heartbeat:{time.monotonic_ns()}",
                    )
                if intervention_due:
                    payload = {
                        "status": "waiting_user_decision",
                        "health": "waiting_user_decision",
                        "reason": reason or "image_job_observation",
                        "next_actions": next_actions,
                    }
                    self._emit_task_event(
                        state,
                        "task.health_changed",
                        payload,
                        suffix=f"watchdog-health:{reason}:{time.monotonic_ns()}",
                    )
                    self._emit_task_event(
                        state,
                        "task.intervention_requested",
                        payload,
                        suffix=f"watchdog-intervention:{reason}:{time.monotonic_ns()}",
                    )

        thread = threading.Thread(target=_watchdog, daemon=True, name=f"image-job-observer-{state.job_id[:18]}")
        with self._lock:
            state.observation_watchdog_thread = thread
        thread.start()

    def _emit_observation_progress(
        self,
        state: _ImageJobState,
        *,
        status: str,
        progress: Optional[float] = None,
        index: int = 0,
    ) -> None:
        safe_status = _safe_progress_status(status)
        detail = _image_observation_status_policy(safe_status)
        with self._lock:
            if state.observation_terminal_emitted:
                return
            now = time.monotonic()
            previous_health = state.observation_health
            started_at = state.observation_started_at or now
            elapsed = max(0.0, now - started_at)
            state.observation_last_progress_at = now
            state.observation_last_heartbeat_at = now
            extended = False
            extension_seconds = 0.0
            if detail.get("extends_deadline"):
                baseline = max(30.0, float(state.observation_per_image_baseline_seconds or IMAGE_JOB_BASELINE_SECONDS))
                extension_seconds = max(30.0, float(detail.get("extension_factor", 1.0)) * baseline)
                next_soft = elapsed + extension_seconds
                if next_soft > state.observation_soft_deadline_seconds + 5.0:
                    state.observation_soft_deadline_seconds = min(next_soft, 86400.0)
                    state.observation_hard_deadline_seconds = min(
                        max(state.observation_hard_deadline_seconds, state.observation_soft_deadline_seconds + baseline),
                        86400.0,
                    )
                    state.observation_last_deadline_extension_at = now
                    extended = True
            if previous_health == "waiting_user_decision":
                state.observation_health = "running"
                recovered = True
            else:
                recovered = False
        if recovered:
            self._emit_task_event(
                state,
                "task.health_changed",
                {
                    "status": "running",
                    "health": "running",
                    "reason": "image_job_progress_resumed",
                },
                suffix=f"progress-resumed:{time.monotonic_ns()}",
            )
        payload: Dict[str, Any] = {
            "status": "running",
            "health": state.observation_health,
            "image_job_status": safe_status,
            "task_index": max(0, int(index or 0)),
        }
        if progress is not None:
            payload["progress"] = max(0.0, min(float(progress), 1.0))
        if extended:
            payload["deadline_extended"] = True
            payload["extension_seconds"] = int(extension_seconds)
            payload["soft_deadline_seconds"] = int(state.observation_soft_deadline_seconds or 0)
            payload["hard_deadline_seconds"] = int(state.observation_hard_deadline_seconds or 0)
        self._emit_task_event(
            state,
            "task.heartbeat",
            payload,
            suffix=f"progress-heartbeat:{index}:{safe_status}:{time.monotonic_ns()}",
        )

    def _finish_observation(self, state: _ImageJobState, status: str, **payload: Any) -> None:
        terminal_status = str(status or "").strip().lower() or "completed"
        if terminal_status == "success":
            terminal_status = "completed"
        event_type = {
            "completed": "task.completed",
            "cancelled": "task.cancelled",
            "canceled": "task.cancelled",
            "failed": "task.failed",
            "timeout": "task.failed",
            "error": "task.failed",
        }.get(terminal_status, "task.completed")
        with self._lock:
            if state.observation_terminal_emitted:
                return
            state.observation_terminal_emitted = True
            state.observation_health = terminal_status
            state.observation_watchdog_stop.set()
        self._emit_task_event(
            state,
            event_type,
            {
                "status": terminal_status,
                "health": terminal_status,
                **payload,
            },
            suffix=f"task-terminal:{terminal_status}",
        )

    def _emit_task_event(self, state: _ImageJobState, event_type: str, payload: Dict[str, Any], *, suffix: str) -> Dict[str, Any]:
        return self.event_ledger.append_event(
            request_id=state.request_id,
            session_id=state.session_id,
            turn_id=state.turn_id,
            event_type=event_type,
            payload=self._task_payload(state, payload),
            idempotency_key=f"{state.request_id}:{state.job_id}:task:{suffix}",
            source="image_job_service",
        )

    def _task_payload(self, state: _ImageJobState, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            started_at = state.observation_started_at or time.monotonic()
            elapsed_seconds = max(0.0, time.monotonic() - started_at)
            base = {
                "task_id": state.job_id,
                "kind": "image_job",
                "title": "image generation",
                "request_id": state.request_id,
                "parent_id": state.job_id,
                "job_id": state.job_id,
                "started_at": state.observation_started_wall_time or time.time(),
                "elapsed_seconds": round(elapsed_seconds, 2),
                "soft_deadline_seconds": int(state.observation_soft_deadline_seconds or 0),
                "hard_deadline_seconds": int(state.observation_hard_deadline_seconds or 0),
                "per_image_baseline_seconds": int(state.observation_per_image_baseline_seconds or 0),
                "lease_count": int(state.observation_lease_count or 0),
                "health": state.observation_health or "running",
                "backgrounded": bool(state.observation_backgrounded),
            }
        base.update(payload or {})
        return base

    def _run_job(
        self,
        state: _ImageJobState,
        *,
        operation: str,
        tasks: List[Dict[str, Any]],
        runner: ImageJobRunner,
        metadata: Dict[str, Any],
        max_parallel: int = 1,
        ocr_provider: Optional[ImageJobOcrProvider] = None,
    ) -> None:
        started_at = time.monotonic()
        with self._lock:
            if state.status != "cancelled":
                state.status = "running"
        try:
            parallelism = _bounded_parallelism(max_parallel, len(tasks))
            if parallelism <= 1:
                for index, task in enumerate(tasks):
                    self._run_task(
                        state,
                        operation=operation,
                        task=task,
                        index=index,
                        runner=runner,
                        ocr_provider=ocr_provider,
                    )
            else:
                self._run_tasks_parallel(
                    state,
                    operation=operation,
                    tasks=tasks,
                    runner=runner,
                    parallelism=parallelism,
                    ocr_provider=ocr_provider,
                )
            with self._lock:
                if state.status != "cancelled":
                    state.status = "completed"
                    state.finished_at = time.monotonic()
            if state.status == "completed":
                self._finish_observation(
                    state,
                    "completed",
                    artifact_count=len(state.artifacts),
                    total_latency_ms=int((time.monotonic() - started_at) * 1000),
                )
                self._emit(
                    state,
                    "image_job.completed",
                    {
                        "job_id": state.job_id,
                        "artifact_count": len(state.artifacts),
                        "total_latency_ms": int((time.monotonic() - started_at) * 1000),
                        **_safe_metadata(metadata),
                    },
                    suffix="completed",
                )
        except ImageJobCancelled as exc:
            with self._lock:
                already_cancelled = state.status == "cancelled"
                state.status = "cancelled"
                state.finished_at = time.monotonic()
            if not already_cancelled:
                self._finish_observation(state, "cancelled", reason=_safe_cancel_reason(str(exc) or "cancelled"))
                self._emit(
                    state,
                    "image_job.cancelled",
                    {"job_id": state.job_id, "reason": _safe_cancel_reason(str(exc) or "cancelled")},
                    suffix="cancelled",
                )
        except Exception as exc:
            with self._lock:
                if state.status in {"failed", "cancelled"}:
                    return
                state.status = "failed"
                state.finished_at = time.monotonic()
            self._finish_observation(state, "failed", error_type=_safe_error_type(exc))
            self._emit(
                state,
                "image_job.failed",
                {
                    "job_id": state.job_id,
                    "error_message": _safe_error_message(exc),
                    "error_type": _safe_error_type(exc),
                    **_safe_metadata(metadata),
                },
                suffix="failed",
            )

    def _run_tasks_parallel(
        self,
        state: _ImageJobState,
        *,
        operation: str,
        tasks: List[Dict[str, Any]],
        runner: ImageJobRunner,
        parallelism: int,
        ocr_provider: Optional[ImageJobOcrProvider] = None,
    ) -> None:
        next_index = 0
        index_lock = threading.Lock()
        first_error: List[BaseException] = []

        def take_next() -> Optional[tuple[int, Dict[str, Any]]]:
            nonlocal next_index
            with index_lock:
                if state.cancel_event.is_set() or first_error:
                    return None
                if next_index >= len(tasks):
                    return None
                task_item = (next_index, tasks[next_index])
                next_index += 1
                return task_item

        def remember_error(exc: BaseException) -> None:
            with index_lock:
                if not first_error:
                    first_error.append(exc)
                else:
                    state.cancel_event.set()
                    return
            state.cancel_event.set()
            if isinstance(exc, ImageJobCancelled):
                with self._lock:
                    already_terminal = state.status in {"completed", "failed", "cancelled"}
                    if not already_terminal:
                        state.status = "cancelled"
                        state.finished_at = time.monotonic()
                if not already_terminal:
                    self._finish_observation(state, "cancelled", reason=_safe_cancel_reason(str(exc) or "cancelled"))
                    self._emit(
                        state,
                        "image_job.cancelled",
                        {"job_id": state.job_id, "reason": _safe_cancel_reason(str(exc) or "cancelled")},
                        suffix="cancelled",
                    )
                return
            with self._lock:
                already_terminal = state.status in {"completed", "failed", "cancelled"}
                if not already_terminal:
                    state.status = "failed"
                    state.finished_at = time.monotonic()
            if not already_terminal:
                self._finish_observation(state, "failed", error_type=_safe_error_type(exc))
                self._emit(
                    state,
                        "image_job.failed",
                    {
                        "job_id": state.job_id,
                        "error_message": _safe_error_message(exc),
                            "error_type": _safe_error_type(exc),
                        },
                    suffix="failed",
                )

        def worker() -> None:
            while True:
                task_item = take_next()
                if task_item is None:
                    return
                index, task = task_item
                try:
                    self._run_task(
                        state,
                        operation=operation,
                        task=task,
                        index=index,
                        runner=runner,
                        ocr_provider=ocr_provider,
                    )
                except ImageJobCancelled as exc:
                    remember_error(exc)
                    return
                except Exception as exc:
                    remember_error(exc)
                    return

        with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix=f"image-job-{state.job_id[:12]}") as executor:
            futures = [executor.submit(worker) for _ in range(parallelism)]
            for future in as_completed(futures):
                future.result()
        if first_error:
            raise first_error[0]

    def _run_task(
        self,
        state: _ImageJobState,
        *,
        operation: str,
        task: Dict[str, Any],
        index: int,
        runner: ImageJobRunner,
        ocr_provider: Optional[ImageJobOcrProvider] = None,
    ) -> None:
        if state.cancel_event.is_set():
            raise ImageJobCancelled("image job cancelled")
        task_id = _task_id_for(task, index)
        self._emit_progress(state, task_id, "running", index=index, detail={"operation": operation})
        self._maybe_apply_ocr_brief(state, task=task, task_id=task_id, index=index, ocr_provider=ocr_provider)
        max_quality_retries = _quality_retry_limit(task)
        retry_count = 0
        reference_images = _authorized_quality_reference_images(task)
        while True:
            attempt_task = dict(task)
            if retry_count:
                attempt_task["_quality_retry_attempt"] = retry_count
            provider_started = time.monotonic()
            result = runner(
                attempt_task,
                lambda status, progress=None, detail=None, task_id=task_id: self._emit_progress(
                    state,
                    task_id,
                    status,
                    progress=progress,
                    index=index,
                    detail=detail,
                ),
                state.cancel_event,
            )
            provider_latency_ms = int((time.monotonic() - provider_started) * 1000)
            if state.cancel_event.is_set():
                raise ImageJobCancelled("image job cancelled")
            self._emit_progress(
                state,
                task_id,
                "provider_response",
                progress=0.8,
                index=index,
                detail={
                    "source": "image_job_service",
                    "provider_latency_ms": provider_latency_ms,
                },
            )
            quality_started = time.monotonic()
            artifacts = _coerce_artifacts(result)
            safe_artifacts = [
                _safe_artifact(
                    artifact,
                    reference_images=reference_images,
                )
                for artifact in artifacts
            ]
            quality_latency_ms = int((time.monotonic() - quality_started) * 1000)
            finalization_started = time.monotonic()
            safe_artifacts, finalization = _finalize_safe_artifacts(
                safe_artifacts,
                retry_count=retry_count,
                max_retries=max_quality_retries,
            )
            finalization_latency_ms = int((time.monotonic() - finalization_started) * 1000)
            self._emit_progress(
                state,
                task_id,
                "quality_check",
                progress=0.84,
                index=index,
                detail={
                    "source": "image_job_service",
                    "quality_latency_ms": quality_latency_ms,
                    "finalization_latency_ms": finalization_latency_ms,
                    "postprocess_latency_ms": quality_latency_ms + finalization_latency_ms,
                    "retry_count": retry_count,
                    "max_retries": max_quality_retries,
                },
            )
            if finalization.get("status") == "retry" and retry_count < max_quality_retries:
                retry_count += 1
                self._emit_progress(
                    state,
                    task_id,
                    "retry",
                    progress=0.82,
                    index=index,
                    detail={
                        "source": "image_job_service",
                        "retry_count": retry_count,
                        "max_retries": max_quality_retries,
                        "retry_gate": finalization.get("retryGate") or "none",
                        "retry_reason": finalization.get("retryGate") or "none",
                        "quality_status": finalization.get("retryGateStatus") or "unknown",
                    },
                )
                if state.cancel_event.is_set():
                    raise ImageJobCancelled("image job cancelled")
                continue
            break
        for artifact_index, artifact in enumerate(artifacts):
            safe_artifact = safe_artifacts[artifact_index] if artifact_index < len(safe_artifacts) else _safe_artifact(artifact)
            safe_artifact = {
                **safe_artifact,
                "task_index": max(0, int(index or 0)),
                "artifact_index": max(0, int(artifact_index or 0)),
                "task_id": task_id,
            }
            with self._lock:
                if state.cancel_event.is_set() or state.status in {"completed", "failed", "cancelled"}:
                    raise ImageJobCancelled("image job cancelled")
                state.artifacts.append(safe_artifact)
                state.artifacts = _sort_artifacts_for_display(state.artifacts)
                self._emit(
                    state,
                    "artifact.created",
                    {
                        "artifact": safe_artifact,
                        "job_id": state.job_id,
                        "task_id": task_id,
                        "task_index": index,
                        "artifact_index": artifact_index,
                    },
                    suffix=f"artifact-created:{index}:{artifact_index}",
                )
                self._emit(
                    state,
                    "image_job.artifact",
                    {
                        "job_id": state.job_id,
                        "task_id": task_id,
                        "task_index": index,
                        "artifact": safe_artifact,
                        "artifact_index": artifact_index,
                    },
                    suffix=f"artifact:{index}:{artifact_index}",
                )
        self._emit_progress(state, task_id, "completed", progress=1.0, index=index)

    def _maybe_apply_ocr_brief(
        self,
        state: _ImageJobState,
        *,
        task: Dict[str, Any],
        task_id: str,
        index: int,
        ocr_provider: Optional[ImageJobOcrProvider],
    ) -> None:
        if not ocr_provider:
            return
        refs = _image_input_refs(task)
        if not refs:
            return
        cache_key = _ocr_cache_key(refs)
        started_at = time.monotonic()
        try:
            hit, brief = self._ocr_cache.get_or_create(
                cache_key,
                lambda: _coerce_ocr_brief(ocr_provider(_ocr_provider_task_payload(task, refs))),
            )
        except Exception:
            self._emit_progress(
                state,
                task_id,
                "ocr",
                progress=0.1,
                index=index,
                detail={
                    "ocr_cache_key": cache_key,
                    "ocr_cache_hit": False,
                    "ocr_input_image_count": len(refs),
                    "ocr_ms": int((time.monotonic() - started_at) * 1000),
                    "taxonomy": "ocr_failed",
                },
            )
            return
        if not brief:
            self._emit_progress(
                state,
                task_id,
                "ocr",
                progress=0.1,
                index=index,
                detail={
                    "ocr_cache_key": cache_key,
                    "ocr_cache_hit": False,
                    "ocr_input_image_count": len(refs),
                    "ocr_ms": int((time.monotonic() - started_at) * 1000),
                    "taxonomy": "ocr_empty",
                },
            )
            return
        brief_hash = _ocr_brief_hash(brief)
        task["_ocr_brief"] = brief
        task["_ocr_cache_key"] = cache_key
        task["_ocr_brief_hash"] = brief_hash
        self._emit_progress(
            state,
            task_id,
            "ocr",
            progress=0.1,
            index=index,
            detail={
                "ocr_cache_key": cache_key,
                "ocr_cache_hit": hit,
                "ocr_input_image_count": len(refs),
                "ocr_ms": int((time.monotonic() - started_at) * 1000),
                "ocr_brief_hash": brief_hash,
                "ocr_provider": _ocr_provider_name(ocr_provider),
            },
        )

    def _emit_progress(
        self,
        state: _ImageJobState,
        task_id: str,
        status: str,
        *,
        progress: Optional[float] = None,
        index: int = 0,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if state.cancel_event.is_set() or state.status in {"completed", "failed", "cancelled"}:
                return {}
        safe_status = _safe_progress_status(status)
        payload: Dict[str, Any] = {
            "job_id": state.job_id,
            "task_id": task_id,
            "task_index": index,
            "status": safe_status,
        }
        if progress is not None:
            payload["progress"] = max(0.0, min(float(progress), 1.0))
        if detail:
            payload.update(_safe_metadata(detail))
        with self._lock:
            if state.cancel_event.is_set() or state.status in {"completed", "failed", "cancelled"}:
                return {}
            event = self._emit(state, "image_job.progress", payload, suffix=f"progress:{index}:{safe_status}:{time.monotonic_ns()}")
        if event:
            self._emit_observation_progress(state, status=safe_status, progress=progress, index=index)
        return event

    def _emit(self, state: _ImageJobState, event_type: str, payload: Dict[str, Any], *, suffix: str) -> Dict[str, Any]:
        return self.event_ledger.append_event(
            request_id=state.request_id,
            session_id=state.session_id,
            turn_id=state.turn_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=f"{state.request_id}:{state.job_id}:{suffix}",
            source="image_job_service",
        )


def _coerce_artifacts(result: Any) -> List[Dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, dict):
        if isinstance(result.get("artifacts"), list):
            return [item for item in result["artifacts"] if isinstance(item, dict)]
        if isinstance(result.get("files"), list):
            return [item for item in result["files"] if isinstance(item, dict)]
        return [result]
    if isinstance(result, (list, tuple)):
        return [item for item in result if isinstance(item, dict)]
    return []


def _artifact_sort_key(artifact: Dict[str, Any]) -> tuple[int, int, str]:
    def _safe_index(value: Any, fallback: int = 999_999) -> int:
        try:
            number = int(value)
            return number if number >= 0 else fallback
        except (TypeError, ValueError):
            return fallback

    return (
        _safe_index(artifact.get("task_index") if "task_index" in artifact else artifact.get("taskIndex")),
        _safe_index(artifact.get("artifact_index") if "artifact_index" in artifact else artifact.get("artifactIndex")),
        str(
            artifact.get("id")
            or artifact.get("path")
            or artifact.get("relativePath")
            or artifact.get("relative_path")
            or artifact.get("url")
            or artifact.get("title")
            or ""
        ),
    )


def _sort_artifacts_for_display(artifacts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted((dict(item) for item in artifacts if isinstance(item, dict)), key=_artifact_sort_key)


def _quality_retry_limit(task: Dict[str, Any]) -> int:
    for key in ("quality_retry_max", "max_quality_retries", "image_quality_retries"):
        if key not in task:
            continue
        try:
            return max(0, min(int(task.get(key)), 2))
        except (TypeError, ValueError):
            return 1
    return 0


def _finalize_safe_artifacts(
    artifacts: List[Dict[str, Any]],
    *,
    retry_count: int,
    max_retries: int,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    finalized: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    for artifact in artifacts:
        item = dict(artifact or {})
        evidence = item.get("qualityEvidence") if isinstance(item.get("qualityEvidence"), dict) else None
        if evidence:
            decision = build_image_finalization_decision(
                evidence,
                retry_count=retry_count,
                max_retries=max_retries,
            )
            annotated = attach_image_finalization_evidence(evidence, decision)
            if annotated:
                item["qualityEvidence"] = annotated
            decisions.append(decision)
        finalized.append(item)
    return finalized, aggregate_image_finalization_decisions(decisions)


class _ImageJobOcrBriefCache:
    def __init__(self, max_entries: int = 128) -> None:
        self._lock = threading.RLock()
        self._briefs: Dict[str, str] = {}
        self._max_entries = max(1, int(max_entries or 128))

    def get_or_create(self, cache_key: str, factory: Callable[[], str]) -> tuple[bool, str]:
        safe_key = str(cache_key or "").strip()
        if not safe_key:
            return False, ""
        with self._lock:
            cached = self._briefs.get(safe_key)
            if cached:
                return True, cached
            brief = str(factory() or "").strip()
            if not brief:
                return False, ""
            self._briefs[safe_key] = brief
            while len(self._briefs) > self._max_entries:
                oldest_key = next(iter(self._briefs))
                self._briefs.pop(oldest_key, None)
            return False, brief

    def size(self) -> int:
        with self._lock:
            return len(self._briefs)


_IMAGE_INPUT_REF_FIELDS = (
    "image_url",
    "image_urls",
    "image_path",
    "image_paths",
    "input_image",
    "input_images",
    "reference_image",
    "reference_images",
    "referenceImage",
    "referenceImages",
)


def _image_input_refs(task: Dict[str, Any]) -> List[str]:
    refs: List[str] = []
    for key in _IMAGE_INPUT_REF_FIELDS:
        value = task.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                refs.append(cleaned)
        elif isinstance(value, (list, tuple)):
            for item in value:
                cleaned = str(item or "").strip()
                if cleaned:
                    refs.append(cleaned)
    return refs


def _authorized_quality_reference_images(task: Dict[str, Any]) -> List[str]:
    refs: List[str] = []
    for ref in _image_input_refs(task):
        if _reference_is_remote_or_inline(ref):
            refs.append(ref)
            continue
        path = _resolve_reference_image_path(ref)
        if path and _authorize_reference_read(path):
            refs.append(str(path))
    return refs


def _reference_is_remote_or_inline(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    return lowered.startswith(("http://", "https://", "data:"))


def _resolve_reference_image_path(value: str) -> Optional[Path]:
    raw = str(value or "").strip()
    if not raw or _reference_is_remote_or_inline(raw):
        return None
    if raw.lower().startswith("file://"):
        raw = raw[7:]
    try:
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _authorize_reference_read(path: Path) -> bool:
    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker

        decision = get_tool_permission_broker().authorize_file_access("read", str(path), cwd=str(Path.cwd()))
    except Exception:
        return False
    return bool(decision.get("allowed"))


def _ocr_cache_key(refs: List[str]) -> str:
    digest = hashlib.sha256("\n".join(refs).encode("utf-8", errors="replace")).hexdigest()
    return f"ocr-{digest[:32]}"


def _ocr_brief_hash(brief: str) -> str:
    return hashlib.sha256(str(brief or "").encode("utf-8", errors="replace")).hexdigest()[:32]


def _ocr_provider_task_payload(task: Dict[str, Any], refs: List[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "image": refs[0] if refs else "",
        "image_url": refs[0] if refs else "",
        "image_urls": list(refs),
        "input_image_count": len(refs),
        "operation": _safe_operation(task.get("operation") or task.get("image_mode") or ""),
    }
    task_id = _safe_identifier(task.get("task_id"), fallback="")
    if task_id:
        payload["task_id"] = task_id
    return payload


def _coerce_ocr_brief(result: Any) -> str:
    if result is None:
        return ""
    if hasattr(result, "status") and hasattr(result, "result"):
        if str(getattr(result, "status", "")).lower() != "success":
            raise RuntimeError("ocr provider failed")
        return _coerce_ocr_brief(getattr(result, "result"))
    if isinstance(result, dict):
        for key in ("brief", "content", "text", "description", "summary"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        nested = result.get("result")
        if nested is not None:
            return _coerce_ocr_brief(nested)
        return ""
    if isinstance(result, str):
        return result.strip()
    return ""


def _ocr_provider_name(provider: ImageJobOcrProvider) -> str:
    raw = getattr(provider, "__name__", "") or provider.__class__.__name__ or "ocr_provider"
    return _safe_telemetry_token(raw) or "ocr_provider"


def _bounded_parallelism(value: Any, task_count: int) -> int:
    try:
        requested = int(value or 1)
    except (TypeError, ValueError):
        requested = 1
    return max(1, min(requested, max(1, int(task_count or 1))))


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def resolve_image_job_parallelism_policy(
    request: Dict[str, Any],
    task_count: int,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = config if isinstance(config, dict) else conf()
    safe_task_count = max(1, int(task_count or 1))
    raw_requested = _positive_int((request or {}).get("max_parallel") or (request or {}).get("maxParallel"))
    configured_default = _positive_int(
        cfg.get("image_job_default_max_parallel") or os.environ.get("ECOREX_IMAGE_JOB_DEFAULT_MAX_PARALLEL")
    )
    default_max_parallel = configured_default or (2 if safe_task_count > 1 else 1)
    parallelism_defaulted = raw_requested is None
    requested = raw_requested or min(safe_task_count, default_max_parallel)
    configured = _positive_int(cfg.get("image_job_max_parallel"))
    provider = _positive_int(cfg.get("image_provider_concurrency"))
    configured_hard = _positive_int(
        cfg.get("image_job_hard_max_parallel") or os.environ.get("ECOREX_IMAGE_JOB_HARD_MAX_PARALLEL")
    )
    hard = min(configured_hard or IMAGE_JOB_PRODUCTION_HARD_MAX_PARALLEL, IMAGE_JOB_PRODUCTION_HARD_MAX_PARALLEL)
    limits = {
        "task_count": safe_task_count,
        "requested_max_parallel": requested,
        "hard_max_parallel": hard,
    }
    if configured is not None:
        limits["configured_max_parallel"] = configured
    if provider is not None:
        limits["provider_max_parallel"] = provider
    effective = max(1, min(limits.values()))
    clamp_reason = "none"
    if effective < requested:
        for key in ("task_count", "configured_max_parallel", "provider_max_parallel", "hard_max_parallel"):
            if limits.get(key) == effective:
                clamp_reason = key
                break
        if clamp_reason == "none":
            clamp_reason = "policy"
    policy: Dict[str, Any] = {
        "parallelism_policy_version": "v1",
        "task_count": safe_task_count,
        "requested_max_parallel": requested,
        "default_max_parallel": default_max_parallel,
        "hard_max_parallel": hard,
        "effective_max_parallel": effective,
        "parallelism_defaulted": parallelism_defaulted,
        "parallelism_clamped": effective < requested,
        "parallelism_clamp_reason": clamp_reason,
    }
    if configured is not None:
        policy["configured_max_parallel"] = configured
    if provider is not None:
        policy["provider_max_parallel"] = provider
    return policy


def _apply_observation_policy(state: _ImageJobState, metadata: Dict[str, Any]) -> None:
    cfg = conf()
    baseline = _observation_seconds(
        metadata,
        ("observation_per_image_baseline_seconds", "per_image_baseline_seconds", "baseline_seconds"),
        config_key="image_job_observation_per_image_baseline_seconds",
        env_key="ECOREX_IMAGE_JOB_OBSERVATION_PER_IMAGE_BASELINE_SECONDS",
        default=IMAGE_JOB_BASELINE_SECONDS,
        cfg=cfg,
    )
    task_count = _positive_int(
        metadata.get("task_count")
        or metadata.get("output_count")
        or metadata.get("image_count")
    ) or 1
    parallelism = _positive_int(
        metadata.get("effective_max_parallel")
        or metadata.get("max_parallel")
        or metadata.get("maxParallel")
    ) or 1
    waves = max(1, ceil(task_count / max(1, parallelism)))
    expected_seconds = max(baseline, waves * baseline)
    soft = _observation_seconds(
        metadata,
        ("observation_soft_deadline_seconds", "soft_deadline_seconds"),
        config_key="image_job_observation_soft_deadline_seconds",
        env_key="ECOREX_IMAGE_JOB_OBSERVATION_SOFT_DEADLINE_SECONDS",
        default=expected_seconds,
        cfg=cfg,
    )
    stall = _observation_seconds(
        metadata,
        ("observation_stall_seconds", "stall_seconds"),
        config_key="image_job_observation_stall_seconds",
        env_key="ECOREX_IMAGE_JOB_OBSERVATION_STALL_SECONDS",
        default=baseline,
        cfg=cfg,
    )
    hard = _observation_seconds(
        metadata,
        ("observation_hard_deadline_seconds", "hard_deadline_seconds"),
        config_key="image_job_observation_hard_deadline_seconds",
        env_key="ECOREX_IMAGE_JOB_OBSERVATION_HARD_DEADLINE_SECONDS",
        default=max(soft + baseline, expected_seconds + baseline),
        cfg=cfg,
    )
    heartbeat = _observation_seconds(
        metadata,
        ("observation_heartbeat_seconds", "heartbeat_seconds"),
        config_key="image_job_observation_heartbeat_seconds",
        env_key="ECOREX_IMAGE_JOB_OBSERVATION_HEARTBEAT_SECONDS",
        default=min(30.0, baseline / 2.0),
        cfg=cfg,
        allow_zero=True,
    )
    interval = _observation_seconds(
        metadata,
        ("observation_watchdog_interval_seconds", "watchdog_interval_seconds"),
        config_key="image_job_observation_watchdog_interval_seconds",
        env_key="ECOREX_IMAGE_JOB_OBSERVATION_WATCHDOG_INTERVAL_SECONDS",
        default=min(5.0, max(0.05, heartbeat or 5.0)),
        cfg=cfg,
    )
    state.observation_per_image_baseline_seconds = baseline
    state.observation_soft_deadline_seconds = soft
    state.observation_stall_seconds = stall
    state.observation_hard_deadline_seconds = max(hard, soft)
    state.observation_heartbeat_seconds = heartbeat
    state.observation_watchdog_interval_seconds = interval


def _observation_seconds(
    metadata: Dict[str, Any],
    metadata_keys: tuple[str, ...],
    *,
    config_key: str,
    env_key: str,
    default: float,
    cfg: Dict[str, Any],
    allow_zero: bool = False,
) -> float:
    raw: Any = None
    for key in metadata_keys:
        if key in metadata:
            raw = metadata.get(key)
            break
    if raw in (None, ""):
        raw = cfg.get(config_key)
    if raw in (None, ""):
        raw = os.environ.get(env_key)
    try:
        value = float(raw if raw not in (None, "") else default)
    except (TypeError, ValueError):
        value = float(default)
    if allow_zero and value <= 0:
        return 0.0
    return max(0.05, min(value, 86400.0))


def _prepare_task_list(tasks: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prepared: List[Dict[str, Any]] = []
    used: set[str] = set()
    next_suffix_by_base: Dict[str, int] = {}
    for index, task in enumerate(tasks):
        item = dict(task or {})
        base_task_id = f"task-{index + 1}"
        item.pop("source_task_id", None)
        next_suffix = next_suffix_by_base.get(base_task_id, 1)
        candidate = base_task_id if next_suffix == 1 else f"{base_task_id}-{next_suffix}"
        while candidate in used:
            next_suffix += 1
            candidate = f"{base_task_id}-{next_suffix}"
        next_suffix_by_base[base_task_id] = next_suffix + 1
        used.add(candidate)
        item["task_id"] = candidate
        prepared.append(item)
    return prepared


def _task_id_for(task: Dict[str, Any], index: int) -> str:
    return _safe_identifier(task.get("task_id") or task.get("id"), fallback=f"task-{index + 1}")


def _safe_task_projection(task: Dict[str, Any], index: int) -> Dict[str, Any]:
    image_url = task.get("image_url")
    if task.get("input_image_count") is not None:
        input_image_count = int(task.get("input_image_count") or 0)
    elif isinstance(image_url, list):
        input_image_count = len(image_url)
    else:
        input_image_count = int(bool(image_url))
    projection = {
        "task_id": _task_id_for(task, index),
        "operation": _safe_operation(task.get("operation") or task.get("image_mode") or ""),
        "input_image_count": input_image_count,
        "output_count": int(task.get("output_count") or task.get("n") or 1),
    }
    source_task_id = _safe_identifier(task.get("source_task_id"), fallback="")
    if source_task_id and source_task_id != projection["task_id"]:
        projection["source_task_id"] = source_task_id
    return projection


def _safe_identifier(value: Any, *, fallback: str) -> str:
    raw = str(value or "").strip()
    if raw and len(raw) <= 128 and _is_safe_ascii_identifier(raw):
        return raw
    return fallback


def _safe_job_id(value: Any, *, fallback: str) -> str:
    raw = str(value or "").strip()
    if any(part in raw.lower() for part in ("private", "prompt", "secret", "token", "password")):
        return fallback
    if raw.startswith("image-job-") and len(raw) <= 128 and _is_safe_ascii_identifier(raw):
        return raw
    return fallback


def _is_safe_ascii_identifier(value: str) -> bool:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    return all(char in allowed for char in str(value or ""))


_ARTIFACT_DTO_FIELDS = {
    "id",
    "kind",
    "title",
    "name",
    "path",
    "relativePath",
    "relative_path",
    "url",
    "previewUrl",
    "preview_url",
    "fileName",
    "file_name",
    "fileType",
    "file_type",
    "mimeType",
    "mime_type",
    "sizeBytes",
    "size_bytes",
    "width",
    "height",
    "sha256",
    "safeArtifactId",
    "qualityEvidence",
}

_ARTIFACT_PATH_FIELDS = {
    "path",
    "relativePath",
    "relative_path",
    "url",
    "previewUrl",
    "preview_url",
}

_METADATA_DTO_FIELDS = {
    "api_base_host_hash",
    "api_key_source",
    "attempt",
    "elapsed_ms",
    "endpoint_host_hash",
    "error_taxonomy",
    "attempted_provider_count",
    "fallback_from_model",
    "fallback_provider",
    "fallback_reason",
    "fallback_to_model",
    "fallback_used",
    "finalization_latency_ms",
    "configured_max_parallel",
    "default_max_parallel",
    "deadline_extended",
    "effective_max_parallel",
    "extension_seconds",
    "hard_max_parallel",
    "image_mode",
    "input_image_count",
    "latency_ms",
    "max_retries",
    "model",
    "operation",
    "ocr_brief_hash",
    "ocr_cache_enabled",
    "ocr_cache_hit",
    "ocr_cache_key",
    "ocr_input_image_count",
    "ocr_ms",
    "ocr_provider",
    "output_count",
    "output_format",
    "parallelism_clamp_reason",
    "parallelism_clamped",
    "parallelism_defaulted",
    "parallelism_policy_version",
    "postprocess_latency_ms",
    "progress",
    "provider_latency_ms",
    "provider_max_parallel",
    "provider",
    "quality",
    "quality_latency_ms",
    "quality_status",
    "request_timeout_seconds",
    "requested_max_parallel",
    "resolved_model",
    "retry_count",
    "retry_gate",
    "retry_reason",
    "retry_after_cap_seconds",
    "retry_after_seconds",
    "retryable",
    "size",
    "source",
    "status_code",
    "taxonomy",
    "total_latency_ms",
}

_METADATA_NONNEGATIVE_INT_FIELDS = {
    "attempt",
    "attempted_provider_count",
    "configured_max_parallel",
    "default_max_parallel",
    "effective_max_parallel",
    "elapsed_ms",
    "extension_seconds",
    "finalization_latency_ms",
    "hard_max_parallel",
    "input_image_count",
    "latency_ms",
    "max_retries",
    "ocr_input_image_count",
    "ocr_ms",
    "output_count",
    "postprocess_latency_ms",
    "provider_latency_ms",
    "provider_max_parallel",
    "quality_latency_ms",
    "request_timeout_seconds",
    "requested_max_parallel",
    "retry_count",
    "status_code",
    "total_latency_ms",
}

_METADATA_NONNEGATIVE_NUMBER_FIELDS = {
    "retry_after_cap_seconds",
    "retry_after_seconds",
}

_PROGRESS_STATUSES = {
    "artifact",
    "cancelled",
    "completed",
    "download",
    "failed",
    "fallback",
    "ocr",
    "progress",
    "provider_request",
    "provider_polling",
    "provider_response",
    "provider_waiting",
    "quality_check",
    "qa",
    "qa_check",
    "queued",
    "rate_limited",
    "retry",
    "running",
    "saving",
    "started",
    "postprocess",
    "polling",
    "waiting",
}

_CANCEL_REASONS = {
    "cancel_requested",
    "cancelled",
    "deadline_exceeded",
    "provider_cancelled",
    "shutdown",
    "timeout",
    "user_cancelled",
    "user_stop",
}


def _safe_artifact(artifact: Dict[str, Any], reference_images: Any = None) -> Dict[str, Any]:
    raw = dict(artifact or {})
    normalized = dict(raw)
    normalized.pop("quality_evidence", None)
    normalized.pop("qualityEvidence", None)
    if "file_name" in normalized and "title" not in normalized:
        normalized["title"] = normalized.get("file_name")
    if "fileName" in normalized and "title" not in normalized:
        normalized["title"] = normalized.get("fileName")
    if "file_type" in normalized and "kind" not in normalized:
        normalized["kind"] = normalized.get("file_type")
    if "fileType" in normalized and "kind" not in normalized:
        normalized["kind"] = normalized.get("fileType")
    normalized.setdefault("kind", "image")
    quality_target = _image_quality_target(normalized)
    if _artifact_has_local_image_reference(quality_target):
        try:
            normalized["qualityEvidence"] = build_image_quality_evidence(
                quality_target,
                reference_images=reference_images,
            )
        except Exception:
            pass

    result: Dict[str, Any] = {}
    omitted_count = 0
    truncated = False
    for key, value in normalized.items():
        if key not in _ARTIFACT_DTO_FIELDS:
            omitted_count += 1
            continue
        safe_value, was_truncated = _safe_artifact_value(key, value)
        if safe_value is None:
            omitted_count += 1
            continue
        if was_truncated:
            truncated = True
        result[key] = safe_value
    if "kind" not in result:
        result["kind"] = "image"
    if omitted_count:
        result["artifact_sanitized"] = True
        result["omitted_field_count"] = omitted_count
    if truncated:
        result["metadata_truncated"] = True
    return result


def _image_quality_target(artifact: Dict[str, Any]) -> Dict[str, Any]:
    target: Dict[str, Any] = {"kind": "image"}
    for key in ("path", "url", "relativePath", "relative_path"):
        value = artifact.get(key)
        if isinstance(value, str) and value.strip():
            target[key] = value
    return target


def _safe_artifact_value(key: str, value: Any) -> tuple[Any, bool]:
    if value is None:
        return None, False
    if key == "qualityEvidence":
        return (value, True) if isinstance(value, dict) else (None, False)
    if isinstance(value, bool):
        return value, False
    if key in {"width", "height", "sizeBytes", "size_bytes"}:
        try:
            return max(0, int(value)), False
        except (TypeError, ValueError):
            return None, False
    if isinstance(value, (int, float)):
        return value, False
    if not isinstance(value, str):
        return None, False
    if key in _ARTIFACT_PATH_FIELDS and _looks_like_inline_data(value):
        return None, False
    if key in _ARTIFACT_PATH_FIELDS:
        safe_reference = _safe_artifact_path_reference(value)
        if safe_reference != value:
            return safe_reference, True
    if key in {"title", "name", "fileName", "file_name"} and _artifact_text_requires_redaction(value):
        return _stable_artifact_reference("artifact-label", value), True
    limit = 4096 if key in _ARTIFACT_PATH_FIELDS else 512
    if len(value) <= limit:
        return value, False
    return f"{value[:limit]}...[truncated {len(value) - limit} chars]", True


def _safe_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    omitted_count = 0
    truncated = False
    for key, value in dict(metadata or {}).items():
        normalized = str(key or "")
        lowered = normalized.lower()
        if any(part in lowered for part in ("raw", "response", "b64", "base64")):
            omitted_count += 1
            continue
        if any(part in lowered for part in ("api_key", "secret", "token", "authorization")):
            safe[normalized] = "[redacted]"
            continue
        if normalized not in _METADATA_DTO_FIELDS:
            omitted_count += 1
            continue
        safe_value, was_truncated = _safe_metadata_value(normalized, value)
        if safe_value is None:
            omitted_count += 1
        else:
            safe[normalized] = safe_value
            truncated = truncated or was_truncated
    if omitted_count:
        safe["metadata_sanitized"] = True
        safe["omitted_metadata_field_count"] = omitted_count
    if truncated:
        safe["metadata_truncated"] = True
    return safe


def _looks_like_inline_data(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith("data:") or ";base64," in lowered


def _artifact_has_local_image_reference(artifact: Dict[str, Any]) -> bool:
    kind = str(artifact.get("kind") or artifact.get("fileType") or artifact.get("file_type") or "").strip().lower()
    mime = str(artifact.get("mimeType") or artifact.get("mime_type") or "").strip().lower()
    for key in ("path", "url", "relativePath", "relative_path"):
        value = artifact.get(key)
        if not isinstance(value, str) or not value.strip() or _looks_like_inline_data(value):
            continue
        lowered = value.strip().lower()
        if lowered.startswith(("http://", "https://")):
            continue
        try:
            if not Path(value).exists():
                continue
        except OSError:
            continue
        suffix = Path(value).suffix.lower()
        if kind == "image" or mime.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            return True
    return False


def _stable_artifact_reference(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _artifact_text_requires_redaction(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return False
    sensitive_markers = (
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "cookie",
        "ocr",
        "password",
        "private",
        "prompt",
        "secret",
        "sk-",
        "token",
        "xoxb-",
        "xapp-",
    )
    return any(marker in lowered for marker in sensitive_markers)


def _safe_artifact_path_reference(value: str) -> str:
    raw = str(value or "").strip()
    lowered = raw.lower()
    if not raw:
        return raw
    if _artifact_text_requires_redaction(raw):
        return _stable_artifact_reference("artifact-ref", raw)
    if lowered.startswith(("http://", "https://")):
        if "?" in raw or "#" in raw:
            return _stable_artifact_reference("artifact-url", raw)
        return raw
    if lowered.startswith("file://"):
        return _stable_artifact_reference("artifact-path", raw)
    if os.path.isabs(raw):
        return _stable_artifact_reference("artifact-path", raw)
    if "\\" in raw and (":" in raw.split("\\", 1)[0] or raw.startswith("\\\\")):
        return _stable_artifact_reference("artifact-path", raw)
    return raw


def _safe_metadata_value(key: str, value: Any) -> tuple[Any, bool]:
    if value is None:
        return None, False
    if key in _METADATA_NONNEGATIVE_INT_FIELDS:
        return _safe_metadata_nonnegative_int(value), False
    if key in _METADATA_NONNEGATIVE_NUMBER_FIELDS:
        return _safe_metadata_nonnegative_number(value), False
    if key == "progress":
        return _safe_metadata_progress(value), False
    if key in {"retryable", "fallback_used", "ocr_cache_enabled", "ocr_cache_hit", "parallelism_defaulted"}:
        return _safe_metadata_bool(value), False
    if key == "operation":
        return _safe_operation(value), False
    if key == "source":
        return _safe_metadata_source(value), False
    if key in {
        "provider",
        "resolved_model",
        "model",
        "image_mode",
        "output_format",
        "quality",
        "quality_status",
        "size",
        "retry_gate",
        "retry_reason",
        "api_base_host_hash",
        "api_key_source",
        "endpoint_host_hash",
        "fallback_from_model",
        "fallback_provider",
        "fallback_reason",
        "fallback_to_model",
        "taxonomy",
        "error_taxonomy",
        "parallelism_clamp_reason",
        "parallelism_policy_version",
        "ocr_brief_hash",
        "ocr_cache_key",
        "ocr_provider",
    }:
        return _safe_telemetry_token(value), False
    if isinstance(value, bool):
        return value, False
    if isinstance(value, (int, float)):
        return value, False
    if isinstance(value, str):
        limit = 512
        if len(value) <= limit:
            return value, False
        return f"{value[:limit]}...[truncated {len(value) - limit} chars]", True
    if isinstance(value, dict):
        return {"type": "dict", "item_count": len(value)}, True
    if isinstance(value, (list, tuple)):
        return {"type": "list", "item_count": len(value)}, True
    return None, False


def _safe_error_message(exc: BaseException) -> str:
    return f"{_safe_error_type(exc)}: image job failed"


def _safe_error_type(exc: BaseException) -> str:
    raw = str(type(exc).__name__ or "").strip()
    if any(part in raw.lower() for part in ("private", "prompt", "secret", "token", "password")):
        return "Error"
    if not raw or len(raw) > 80 or raw[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
        return "Error"
    if all(char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._" for char in raw):
        return raw
    return "Error"


def _safe_operation(operation: Any) -> str:
    raw = str(operation or "").strip().lower()
    if raw in {"generate", "edit", "regenerate", "variation", "ocr", "upscale"}:
        return raw
    return "generate"


def _safe_telemetry_token(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw or len(raw) > 128:
        return None
    if _telemetry_token_has_sensitive_material(raw):
        return None
    if _is_safe_ascii_telemetry_token(raw):
        return raw
    return None


def _telemetry_token_has_sensitive_material(value: str) -> bool:
    lowered = str(value or "").lower()
    if any(marker in lowered for marker in ("http://", "https://", "file://", "data:")):
        return True
    if "/" in lowered or "\\" in lowered or ":" in lowered:
        return True
    sensitive_markers = (
        "api_key",
        "api-key",
        "apikey",
        "authorization",
        "bearer",
        "password",
        "private",
        "prompt",
        "secret",
        "sk-",
        "token",
    )
    return any(marker in lowered for marker in sensitive_markers)


def _is_safe_ascii_telemetry_token(value: str) -> bool:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    return all(char in allowed for char in str(value or ""))


def _safe_metadata_source(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if raw in {"image_job_service", "runtime", "web_channel", "tool", "test"}:
        return raw
    return None


def _safe_metadata_nonnegative_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _safe_metadata_nonnegative_number(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed)


def _safe_metadata_progress(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(parsed, 1.0))


def _safe_metadata_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw in {"false", "0", "no", "off"}:
        return False
    return None


def _safe_cancel_reason(reason: Any) -> str:
    raw = str(reason or "").strip()
    if raw in _CANCEL_REASONS:
        return raw
    return "cancelled"


def _safe_progress_status(status: Any) -> str:
    raw = str(status or "").strip().lower()
    if raw in _PROGRESS_STATUSES:
        return raw
    return "progress"


def _image_observation_status_policy(status: str) -> Dict[str, Any]:
    """Map provider progress states to observation lease behavior."""
    if status in {
        "provider_request",
        "provider_polling",
        "provider_waiting",
        "polling",
        "waiting",
        "rate_limited",
        "retry",
        "fallback",
    }:
        return {"extends_deadline": True, "extension_factor": 1.0}
    if status in {"download", "saving", "quality_check", "qa", "qa_check", "postprocess", "provider_response"}:
        return {"extends_deadline": True, "extension_factor": 0.5}
    return {"extends_deadline": False, "extension_factor": 0.0}


_image_job_service: Optional[ImageJobService] = None
_image_job_service_lock = threading.Lock()


def get_image_job_service() -> ImageJobService:
    global _image_job_service
    if _image_job_service is not None:
        return _image_job_service
    with _image_job_service_lock:
        if _image_job_service is None:
            _image_job_service = ImageJobService()
        return _image_job_service


def reset_image_job_service_for_tests(event_ledger: Optional[RunEventLedger] = None) -> ImageJobService:
    global _image_job_service
    with _image_job_service_lock:
        _image_job_service = ImageJobService(event_ledger)
        return _image_job_service
