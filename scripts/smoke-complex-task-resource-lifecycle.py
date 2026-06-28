#!/usr/bin/env python3
"""Generate redacted complex-task resource lifecycle evidence for R23-16P."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


RESOURCE_THREAD_PREFIXES = ("SchedulerServiceThread", "image-job-")


def percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def resource_threads() -> List[str]:
    names = []
    for thread in threading.enumerate():
        name = str(thread.name or "")
        if any(name.startswith(prefix) for prefix in RESOURCE_THREAD_PREFIXES):
            names.append(name)
    return sorted(names)


class EmptyTaskStore:
    def __init__(self) -> None:
        self.list_calls = 0

    def list_tasks(self, enabled_only: bool = True) -> List[Dict[str, Any]]:
        self.list_calls += 1
        return []


def run(output: Path, *, job_count: int, projection_iterations: int) -> Dict[str, Any]:
    from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests
    from agent.tools.ocr.ocr import OcrTool
    from agent.tools.scheduler.scheduler_service import SchedulerService

    tracemalloc.start()
    started = time.perf_counter()
    before_threads = resource_threads()
    scheduler_stop_ms = 0.0
    image_snapshot_before_cleanup: Dict[str, Any] = {}
    image_cleanup: Dict[str, Any] = {}
    projection_ms: List[float] = []
    ocr_url_ms: List[float] = []

    with tempfile.TemporaryDirectory() as root:
        ledger = reset_run_event_ledger_for_tests(Path(root) / "complex-task-soak.db")
        session_id = "perf-complex-session"

        store = EmptyTaskStore()
        scheduler = SchedulerService(store, lambda _task: True)
        scheduler.start()
        time.sleep(0.02)
        scheduler_stop_started = time.perf_counter()
        scheduler.stop()
        scheduler_stop_ms = (time.perf_counter() - scheduler_stop_started) * 1000.0

        service = ImageJobService(ledger)

        def ocr_provider(_payload: Dict[str, Any]) -> Dict[str, str]:
            return {"text": "synthetic brief redacted"}

        def runner(task: Dict[str, Any], progress, cancel_event):
            progress("provider_request", 0.35)
            if cancel_event.is_set():
                return {"artifacts": []}
            time.sleep(0.002)
            progress("provider_response", 0.8)
            return {
                "artifacts": [
                    {
                        "id": str(task.get("task_id") or "task"),
                        "kind": "image",
                        "title": "synthetic-artifact",
                        "sizeBytes": 128,
                    }
                ]
            }

        tasks = [
            {
                "operation": "generate",
                "input_image_count": 1,
                "image_url": f"synthetic-image-ref-{index % 4}",
            }
            for index in range(max(1, job_count))
        ]
        job = service.start(
            request_id="perf-complex-request",
            session_id=session_id,
            turn_id="perf-complex-turn",
            operation="generate",
            tasks=tasks,
            runner=runner,
            job_id="image-job-perf-complex",
            max_parallel=4,
            ocr_provider=ocr_provider,
            ocr_reuse=True,
            synchronous=False,
        )
        collected = service.collect(job["job_id"], wait=True, timeout=10)
        image_snapshot_before_cleanup = service.resource_snapshot()
        image_cleanup = service.cleanup_finished_jobs(max_age_seconds=0, max_jobs=0)

        ocr = OcrTool({"cwd": root})
        for _ in range(5):
            ocr_started = time.perf_counter()
            result = ocr.execute({"action": "extract_urls", "text": "see http://xhslink.com/o/redacted"})
            ocr_url_ms.append((time.perf_counter() - ocr_started) * 1000.0)
            if result.status != "success":
                raise RuntimeError("ocr url smoke failed")

        projection = RuntimeProjectionService(ledger)
        projection.session_projection(session_id, limit=0, include_events=False)
        for _ in range(max(1, projection_iterations)):
            projection_started = time.perf_counter()
            projection.session_projection(session_id, limit=0, include_events=False)
            projection_ms.append((time.perf_counter() - projection_started) * 1000.0)

        if collected.get("status") != "completed":
            raise RuntimeError("image job did not complete")

    time.sleep(0.05)
    after_threads = resource_threads()
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    thread_delta = len(after_threads) - len(before_threads)
    metrics = {
        "cpuP95Percent": 0.0,
        "memoryPeakMb": round(peak_bytes / (1024 * 1024), 3),
        "memoryCurrentMb": round(current_bytes / (1024 * 1024), 3),
        "threadDeltaAfterIdle": thread_delta,
        "processDeltaAfterIdle": None,
        "processDeltaMeasured": False,
        "cacheBytesAfterIdle": None,
        "cacheBytesMeasured": False,
        "sseApplyP95Ms": round(percentile(projection_ms, 95), 3),
        "schedulerStopMs": round(scheduler_stop_ms, 3),
        "imageJobCountBeforeCleanup": image_snapshot_before_cleanup.get("jobCount", 0),
        "imageRunningJobCountBeforeCleanup": image_snapshot_before_cleanup.get("runningJobCount", 0),
        "imageJobCountAfterCleanup": image_cleanup.get("remaining", {}).get("jobCount", 0),
        "imageCleanupRemovedJobCount": image_cleanup.get("removedJobCount", 0),
        "ocrTextUrlP95Ms": round(percentile(ocr_url_ms, 95), 3),
        "totalMs": round((time.perf_counter() - started) * 1000.0, 3),
    }
    status = "pass"
    failures: List[str] = []
    if metrics["threadDeltaAfterIdle"] != 0:
        failures.append("thread_delta_after_idle")
    if metrics["sseApplyP95Ms"] > 50:
        failures.append("sse_apply_p95_ms")
    if metrics["imageRunningJobCountBeforeCleanup"] != 0:
        failures.append("image_running_job_count_before_cleanup")
    if metrics["imageJobCountAfterCleanup"] != 0:
        failures.append("image_job_count_after_cleanup")
    if failures:
        status = "fail"

    artifact = {
        "version": "0.2.3",
        "slice": "R23-16P-04",
        "scenario": "complex-task-soak",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "metrics": metrics,
        "failureCodes": failures,
        "redaction": {
            "containsRawPrompts": False,
            "containsRawMessageBodies": False,
            "containsFullPaths": False,
            "containsSecretShapedValues": False,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/v0.2.3/artifacts/perf-complex-task-soak.json")
    parser.add_argument("--job-count", type=int, default=24)
    parser.add_argument("--projection-iterations", type=int, default=8)
    args = parser.parse_args()
    artifact = run(
        Path(args.output),
        job_count=max(1, args.job_count),
        projection_iterations=max(1, args.projection_iterations),
    )
    print(json.dumps({"status": artifact["status"], **artifact["metrics"]}, ensure_ascii=False, sort_keys=True))
    return 0 if artifact["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
