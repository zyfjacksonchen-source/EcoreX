#!/usr/bin/env python3
"""Redacted R23-16P image artifact/OCR performance smoke."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HASH_SALT = b"ecorex-v023-image-artifact-ocr"
THREAD_PREFIXES = ("image-job-",)


def _hash(value: Any) -> str:
    digest = hmac.new(HASH_SALT, str(value or "").encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()
    return f"hmac:{digest[:16]}"


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def _resource_threads() -> List[str]:
    names: List[str] = []
    for thread in threading.enumerate():
        name = str(thread.name or "")
        if any(name.startswith(prefix) for prefix in THREAD_PREFIXES):
            names.append(name)
    return sorted(names)


def _event_payload_bytes(events: List[Dict[str, Any]]) -> int:
    total = 0
    for event in events:
        payload = event.get("payload")
        if isinstance(payload, dict):
            total += len(json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8"))
    return total


def _artifact_fingerprint(artifact: Dict[str, Any]) -> str:
    public_shape = {
        "id": str(artifact.get("id") or ""),
        "kind": str(artifact.get("kind") or ""),
        "title": str(artifact.get("title") or artifact.get("name") or ""),
        "size": int(artifact.get("sizeBytes") or artifact.get("size_bytes") or 0),
    }
    encoded = json.dumps(public_shape, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _artifact_shape_is_renderable(artifact: Dict[str, Any]) -> bool:
    if str(artifact.get("kind") or "").strip().lower() != "image":
        return False
    has_label = bool(str(artifact.get("title") or artifact.get("name") or "").strip())
    has_reference = any(
        str(artifact.get(key) or "").strip()
        for key in ("path", "relativePath", "relative_path", "url", "previewUrl", "preview_url")
    )
    return has_label or has_reference


def _queue_wait_ms(events: List[Dict[str, Any]], job_id: str) -> float:
    started_at = 0.0
    first_progress_at = 0.0
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload.get("job_id") != job_id:
            continue
        event_type = str(event.get("event_type") or "")
        created_at = float(event.get("created_at") or 0.0)
        if event_type == "image_job.started" and not started_at:
            started_at = created_at
        elif event_type == "image_job.progress" and not first_progress_at:
            first_progress_at = created_at
    if not started_at or not first_progress_at:
        return 0.0
    return max(0.0, (first_progress_at - started_at) * 1000.0)


def run(output: Path, *, task_count: int, artifacts_per_task: int, projection_iterations: int) -> Dict[str, Any]:
    from agent.protocol import ImageJobCancelled, ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

    task_count = max(2, int(task_count or 2))
    artifacts_per_task = max(1, int(artifacts_per_task or 1))
    projection_iterations = max(1, int(projection_iterations or 1))
    started = time.perf_counter()
    before_threads = _resource_threads()
    failure_codes: List[str] = []

    with tempfile.TemporaryDirectory() as root:
        ledger = reset_run_event_ledger_for_tests(Path(root) / "image-artifact-ocr.db")
        service = ImageJobService(ledger)
        session_id = "perf-image-artifact-session"
        completed_request_id = "perf-image-artifact-completed"
        failed_request_id = "perf-image-artifact-failed"
        cancelled_request_id = "perf-image-artifact-cancelled"

        provider_calls = 0
        provider_ms: List[float] = []

        def ocr_provider(_payload: Dict[str, Any]) -> Dict[str, str]:
            nonlocal provider_calls
            provider_calls += 1
            provider_started = time.perf_counter()
            time.sleep(0.001)
            provider_ms.append((time.perf_counter() - provider_started) * 1000.0)
            return {"text": "brief hash only"}

        def runner(task: Dict[str, Any], progress, cancel_event):
            if cancel_event.is_set():
                raise ImageJobCancelled("cancelled")
            if int(task.get("task_index") or 0) == 0:
                progress("retry", 0.42, {"attempt": 1, "retryable": True, "taxonomy": "synthetic_retry"})
            progress("provider_request", 0.55, {"provider": "synthetic", "attempt": 1})
            artifacts = []
            for index in range(artifacts_per_task):
                artifacts.append({
                    "id": f"artifact-{task.get('task_id')}-{index}",
                    "kind": "image",
                    "title": f"synthetic-{index}.png",
                    "relativePath": f"outputs/synthetic-{index}.png",
                    "sizeBytes": 128 + index,
                })
            progress("provider_response", 0.85, {"provider": "synthetic", "attempt": 1})
            return {"artifacts": artifacts}

        tasks = [
            {
                "operation": "edit",
                "task_index": index,
                "image_url": f"stable-image-ref-{index % 2}",
                "input_image_count": 1,
            }
            for index in range(task_count)
        ]
        completed_started = time.perf_counter()
        completed = service.start(
            request_id=completed_request_id,
            session_id=session_id,
            turn_id="perf-image-artifact-turn",
            operation="edit",
            tasks=tasks,
            runner=runner,
            job_id="image-job-perf-artifact-completed",
            max_parallel=1,
            ocr_provider=ocr_provider,
            ocr_reuse=True,
            synchronous=True,
        )
        completed_ms = (time.perf_counter() - completed_started) * 1000.0

        def failing_runner(_task: Dict[str, Any], progress, _cancel_event):
            progress("provider_request", 0.25, {"provider": "synthetic", "attempt": 1})
            raise RuntimeError("synthetic_provider_failure")

        failed = service.start(
            request_id=failed_request_id,
            session_id=session_id,
            turn_id="perf-image-artifact-turn-failed",
            operation="edit",
            tasks=[{"operation": "edit", "image_url": "stable-image-ref-failed", "input_image_count": 1}],
            runner=failing_runner,
            job_id="image-job-perf-artifact-failed",
            max_parallel=1,
            ocr_provider=ocr_provider,
            ocr_reuse=True,
            synchronous=True,
        )

        cancel_seen = threading.Event()

        def slow_runner(_task: Dict[str, Any], progress, cancel_event):
            progress("provider_request", 0.2, {"provider": "synthetic", "attempt": 1})
            cancel_seen.set()
            while not cancel_event.wait(0.01):
                pass
            raise ImageJobCancelled("cancel_requested")

        cancelled = service.start(
            request_id=cancelled_request_id,
            session_id=session_id,
            turn_id="perf-image-artifact-turn-cancelled",
            operation="edit",
            tasks=[{"operation": "edit", "image_url": "stable-image-ref-cancelled", "input_image_count": 1}],
            runner=slow_runner,
            job_id="image-job-perf-artifact-cancelled",
            max_parallel=1,
            ocr_provider=ocr_provider,
            ocr_reuse=True,
            synchronous=False,
        )
        cancel_seen.wait(timeout=2)
        cancel_result = service.cancel(cancelled["job_id"], reason="user_cancelled")
        service.collect(cancelled["job_id"], wait=True, timeout=3)

        projection_ms: List[float] = []
        projected_artifact_count = 0
        projected_artifact_fingerprints: set[str] = set()
        projected_artifact_shape_valid_count = 0
        for _ in range(projection_iterations):
            projection_started = time.perf_counter()
            projection = RuntimeProjectionService(ledger).request_projection(
                completed_request_id,
                expected_session_id=session_id,
                include_events=False,
            )
            projection_ms.append((time.perf_counter() - projection_started) * 1000.0)
            jobs = projection.get("image_jobs") or []
            if jobs:
                projected_artifacts = [
                    artifact for artifact in (jobs[0].get("artifacts") or []) if isinstance(artifact, dict)
                ]
                projected_artifact_count = len(projected_artifacts)
                projected_artifact_fingerprints = {
                    _artifact_fingerprint(artifact) for artifact in projected_artifacts
                }
                projected_artifact_shape_valid_count = sum(
                    1 for artifact in projected_artifacts if _artifact_shape_is_renderable(artifact)
                )

        completed_events = ledger.events_for_request(completed_request_id, limit=0)
        failed_events = ledger.events_for_request(failed_request_id, limit=0)
        cancelled_events = ledger.events_for_request(cancelled_request_id, limit=0)
        all_events = completed_events + failed_events + cancelled_events
        event_types = [str(event.get("event_type") or "") for event in all_events]
        completed_payloads = [
            event.get("payload") or {}
            for event in completed_events
            if event.get("event_type") == "image_job.progress"
        ]
        ocr_payloads = [payload for payload in completed_payloads if payload.get("status") == "ocr"]
        retry_events = [payload for payload in completed_payloads if payload.get("status") == "retry"]
        ocr_hits = sum(1 for payload in ocr_payloads if payload.get("ocr_cache_hit") is True)
        ocr_misses = sum(1 for payload in ocr_payloads if payload.get("ocr_cache_hit") is False)

        snapshot_before_cleanup = service.resource_snapshot()
        cleanup = service.cleanup_finished_jobs(max_age_seconds=0, max_jobs=0)
        snapshot_after_cleanup = cleanup.get("remaining") or {}

    time.sleep(0.05)
    after_threads = _resource_threads()
    expected_artifacts = task_count * artifacts_per_task
    metrics = {
        "ocrReuseP95Ms": round(_percentile(provider_ms, 95), 3),
        "ocrProviderCallCount": provider_calls,
        "ocrCacheHitCount": ocr_hits,
        "ocrCacheMissCount": ocr_misses,
        "imageJobQueueWaitP95Ms": round(_queue_wait_ms(completed_events, "image-job-perf-artifact-completed"), 3),
        "artifactMergeP95Ms": round(_percentile(projection_ms, 95), 3),
        "eventCount": len(all_events),
        "payloadBytes": _event_payload_bytes(all_events),
        "projectedArtifactCount": projected_artifact_count,
        "projectedArtifactFingerprintCount": len(projected_artifact_fingerprints),
        "projectedArtifactShapeValidCount": projected_artifact_shape_valid_count,
        "expectedArtifactCount": expected_artifacts,
        "retryEventCount": len(retry_events),
        "completedJobMs": round(completed_ms, 3),
        "threadDeltaAfterIdle": len(after_threads) - len(before_threads),
        "jobsBeforeCleanup": snapshot_before_cleanup.get("jobCount", 0),
        "runningJobsBeforeCleanup": snapshot_before_cleanup.get("runningJobCount", 0),
        "jobsAfterCleanup": snapshot_after_cleanup.get("jobCount", 0),
        "cleanupRemovedJobCount": cleanup.get("removedJobCount", 0),
    }
    coverage = {
        "completedPath": completed.get("status") == "completed",
        "failurePath": failed.get("status") == "failed",
        "cancelPath": cancel_result.get("cancelled") is True,
        "retryPath": metrics["retryEventCount"] >= 1,
        "projectionPath": projected_artifact_count == expected_artifacts,
        "cleanupPath": metrics["jobsAfterCleanup"] == 0,
    }

    if not coverage["completedPath"]:
        failure_codes.append("completed_path_not_completed")
    if not coverage["failurePath"]:
        failure_codes.append("failure_path_not_failed")
    if not coverage["cancelPath"]:
        failure_codes.append("cancel_path_not_cancelled")
    if not coverage["retryPath"]:
        failure_codes.append("retry_path_not_observed")
    if metrics["ocrCacheHitCount"] < task_count - 2:
        failure_codes.append("ocr_cache_hits_too_low")
    if metrics["ocrCacheMissCount"] != 2:
        failure_codes.append("ocr_cache_miss_count")
    if not coverage["projectionPath"]:
        failure_codes.append("artifact_projection_count")
    if metrics["projectedArtifactFingerprintCount"] != expected_artifacts:
        failure_codes.append("artifact_projection_unique_count")
    if metrics["projectedArtifactShapeValidCount"] != expected_artifacts:
        failure_codes.append("artifact_projection_shape")
    if metrics["eventCount"] > 160:
        failure_codes.append("event_count_threshold")
    if metrics["payloadBytes"] > 40000:
        failure_codes.append("payload_bytes_threshold")
    if metrics["artifactMergeP95Ms"] > 50:
        failure_codes.append("artifact_merge_p95_ms")
    if metrics["threadDeltaAfterIdle"] != 0:
        failure_codes.append("thread_delta_after_idle")
    if metrics["runningJobsBeforeCleanup"] != 0:
        failure_codes.append("running_jobs_before_cleanup")
    if metrics["jobsAfterCleanup"] != 0:
        failure_codes.append("jobs_after_cleanup")

    artifact = {
        "version": "0.2.3",
        "slice": "R23-16P-07",
        "scenario": "image-artifact-ocr",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "status": "fail" if failure_codes else "pass",
        "inputShape": {
            "taskCount": task_count,
            "artifactsPerTask": artifacts_per_task,
            "projectionIterations": projection_iterations,
        },
        "identity": {
            "sessionHash": _hash("perf-image-artifact-session"),
            "completedHash": _hash("perf-image-artifact-completed"),
            "failedHash": _hash("perf-image-artifact-failed"),
            "cancelledHash": _hash("perf-image-artifact-cancelled"),
        },
        "metrics": metrics,
        "coverage": coverage,
        "failureCodes": failure_codes,
        "redaction": {
            "fullPathsStored": False,
            "fullTextStored": False,
            "sensitivePatternStored": False,
            "eventBodiesStored": False,
        },
        "totalMs": round((time.perf_counter() - started) * 1000.0, 3),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description="Run R23-16P image artifact/OCR performance smoke.")
    parser.add_argument("--output", default="docs/v0.2.3/artifacts/perf-image-artifact-ocr.json")
    parser.add_argument("--task-count", type=int, default=12)
    parser.add_argument("--artifacts-per-task", type=int, default=2)
    parser.add_argument("--projection-iterations", type=int, default=8)
    args = parser.parse_args()
    artifact = run(
        Path(args.output),
        task_count=args.task_count,
        artifacts_per_task=args.artifacts_per_task,
        projection_iterations=args.projection_iterations,
    )
    print(json.dumps({"status": artifact["status"], **artifact["metrics"]}, ensure_ascii=False, sort_keys=True))
    return 0 if artifact["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
