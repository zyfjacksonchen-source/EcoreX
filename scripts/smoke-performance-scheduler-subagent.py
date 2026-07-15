#!/usr/bin/env python3
"""Redacted R23-16P scheduler/subagent performance smoke."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HASH_SALT = b"ecorex-v023-scheduler-subagent"


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


def _scheduler_threads() -> List[str]:
    return sorted(str(thread.name or "") for thread in threading.enumerate() if str(thread.name or "").startswith("SchedulerServiceThread"))


def _timer_threads() -> List[str]:
    return sorted(str(thread.name or "") for thread in threading.enumerate() if isinstance(thread, threading.Timer))


def _fingerprint(value: Dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()[:16]


def _subagent_tool_shape_is_renderable(tool: Dict[str, Any]) -> bool:
    if str(tool.get("name") or "") != "subagent":
        return False
    if not str(tool.get("id") or "").strip():
        return False
    if str(tool.get("status") or "") not in {"completed", "timeout", "cancelled", "failed"}:
        return False
    if not str(tool.get("child_request_id") or "").strip():
        return False
    if not str(tool.get("parent_request_id") or "").strip():
        return False
    if not str(tool.get("task_id") or "").strip():
        return False
    result = tool.get("result") if isinstance(tool.get("result"), dict) else {}
    task = result.get("task") if isinstance(result.get("task"), dict) else {}
    return bool(
        str(task.get("id") or "").strip()
        and str(task.get("requestId") or "").strip()
        and str(task.get("childSessionId") or "").strip()
        and str(task.get("status") or "").strip()
    )


def _scheduler_task_shape_is_renderable(task: Dict[str, Any]) -> bool:
    if not str(task.get("id") or "").strip():
        return False
    if not str(task.get("name") or "").strip():
        return False
    if str(task.get("state") or "") not in {"enabled", "disabled", "error"}:
        return False
    if not str(task.get("scheduleDescription") or "").strip():
        return False
    action = task.get("action") if isinstance(task.get("action"), dict) else {}
    return bool(str(action.get("type") or "").strip())


class EmptyTaskStore:
    def __init__(self) -> None:
        self.list_calls = 0

    def list_tasks(self, enabled_only: bool = True) -> List[Dict[str, Any]]:
        self.list_calls += 1
        return []


def _append_subagent_events(ledger: Any, *, request_id: str, session_id: str, count: int) -> None:
    now = time.time()
    ledger.append_event(
        request_id=request_id,
        session_id=session_id,
        event_type="run.accepted",
        payload={"turn_id": "turn-scheduler-subagent"},
        created_at=now,
    )
    ledger.append_event(
        request_id=request_id,
        session_id=session_id,
        event_type="message.user.accepted",
        payload={"content": "redacted synthetic scheduler/subagent workload"},
        created_at=now + 0.001,
    )
    ledger.append_event(
        request_id=request_id,
        session_id=session_id,
        event_type="message.assistant.created",
        payload={},
        created_at=now + 0.002,
    )
    terminal_by_index = ("subagent.completed", "subagent.timeout", "subagent.cancelled", "subagent.failed")
    status_by_type = {
        "subagent.completed": "completed",
        "subagent.timeout": "timeout",
        "subagent.cancelled": "cancelled",
        "subagent.failed": "failed",
    }
    for index in range(max(1, count)):
        task_id = f"task-subagent-perf-{index:03d}"
        child_request_id = f"subagent-child-perf-{index:03d}"
        tool_call_id = f"tool-subagent-perf-{index:03d}"
        base_payload = {
            "tool_call_id": tool_call_id,
            "task_id": task_id,
            "child_request_id": child_request_id,
            "parent_request_id": request_id,
            "parent_session_id": session_id,
            "name": "Perf Reviewer",
            "role": "worker" if index % 2 else "explorer",
            "summary": "redacted synthetic review",
            "deadline_at": 1782345600 + index,
            "timeout_seconds": 900,
            "last_heartbeat_at": 1782345000 + index,
        }
        ledger.append_event(
            request_id=request_id,
            session_id=session_id,
            event_type="subagent.started",
            payload={**base_payload, "status": "starting"},
            created_at=now + 0.01 + index * 0.01,
        )
        ledger.append_event(
            request_id=request_id,
            session_id=session_id,
            event_type="subagent.updated",
            payload={**base_payload, "status": "running", "last_heartbeat_at": 1782345060 + index},
            created_at=now + 0.012 + index * 0.01,
        )
        terminal_type = terminal_by_index[index % len(terminal_by_index)]
        terminal_payload = {
            **base_payload,
            "status": status_by_type[terminal_type],
            "last_heartbeat_at": 1782345120 + index,
        }
        if terminal_type == "subagent.completed":
            terminal_payload["result_preview"] = "completed"
        ledger.append_event(
            request_id=request_id,
            session_id=session_id,
            event_type=terminal_type,
            payload=terminal_payload,
            created_at=now + 0.014 + index * 0.01,
        )


def _write_scheduler_tasks(root: Path, task_count: int) -> None:
    from agent.tools.scheduler.task_store import TaskStore

    store = TaskStore(str(root / "scheduler" / "tasks.json"))
    now = datetime.now().replace(microsecond=0)
    tasks: Dict[str, Dict[str, Any]] = {}
    for index in range(max(1, task_count)):
        action_type = ("send_message", "agent_task", "tool_call", "skill_call")[index % 4]
        action: Dict[str, Any] = {
            "type": action_type,
            "channel_type": "web" if index % 3 else "feishu",
            "receiver": f"receiver-{index:03d}",
            "receiver_name": f"Receiver {index:03d}",
            "is_group": bool(index % 2),
        }
        if action_type == "send_message":
            action["content"] = "redacted scheduled message body"
        elif action_type == "agent_task":
            action["task_description"] = "redacted scheduled agent task"
        elif action_type == "tool_call":
            action["tool_name"] = "browser"
            action["tool_params"] = {"url": "https://example.invalid/redacted", "api_token": "sk-scheduler-subagent-redacted"}
        else:
            action["skill_name"] = "redacted-skill"
            action["skill_args"] = {"topic": "redacted", "token": "sk-scheduler-subagent-redacted"}
        task_id = f"scheduler-perf-{index:03d}"
        tasks[task_id] = {
            "id": task_id,
            "name": f"Perf schedule {index:03d}",
            "enabled": index % 5 != 0,
            "schedule": {"type": "interval", "seconds": 60 + index},
            "action": action,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "next_run_at": (now + timedelta(minutes=index + 1)).isoformat(),
            "last_error": "redacted prior error" if index % 11 == 0 else "",
        }
    store.save_tasks(tasks)


def run(output: Path, *, subagent_count: int, scheduler_task_count: int, projection_iterations: int) -> Dict[str, Any]:
    from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests
    from agent.tools.scheduler.projection import scheduler_projection
    from agent.tools.scheduler.scheduler_service import SchedulerService

    subagent_count = max(4, int(subagent_count or 4))
    scheduler_task_count = max(1, int(scheduler_task_count or 1))
    projection_iterations = max(1, int(projection_iterations or 1))
    started = time.perf_counter()
    before_scheduler_threads = _scheduler_threads()
    before_timer_threads = _timer_threads()
    failure_codes: List[str] = []

    with tempfile.TemporaryDirectory() as root_value:
        root = Path(root_value)
        ledger = reset_run_event_ledger_for_tests(root / "scheduler-subagent.db")
        request_id = "perf-scheduler-subagent-request"
        session_id = "perf-scheduler-subagent-session"
        _append_subagent_events(ledger, request_id=request_id, session_id=session_id, count=subagent_count)
        _write_scheduler_tasks(root, scheduler_task_count)

        service = SchedulerService(EmptyTaskStore(), lambda _task: True)
        service.start()
        time.sleep(0.02)
        stop_started = time.perf_counter()
        service.stop()
        scheduler_stop_ms = (time.perf_counter() - stop_started) * 1000.0

        subagent_projection_ms: List[float] = []
        scheduler_projection_ms: List[float] = []
        projected_tool_count = 0
        projected_tool_shape_valid_count = 0
        projected_tool_fingerprints: set[str] = set()
        status_counts: Dict[str, int] = {}
        scheduler_projected_task_count = 0
        scheduler_projected_shape_valid_count = 0
        scheduler_action_type_counts: Dict[str, int] = {}
        scheduler_projected_error_count = 0

        for _ in range(projection_iterations):
            projection_started = time.perf_counter()
            projection = RuntimeProjectionService(ledger).request_projection(
                request_id,
                expected_session_id=session_id,
                include_events=False,
            )
            subagent_projection_ms.append((time.perf_counter() - projection_started) * 1000.0)
            messages = projection.get("messages") or []
            assistant = messages[1] if len(messages) > 1 and isinstance(messages[1], dict) else {}
            tools = [item for item in (assistant.get("tool_calls") or []) if isinstance(item, dict)]
            projected_tool_count = len(tools)
            projected_tool_shape_valid_count = sum(1 for tool in tools if _subagent_tool_shape_is_renderable(tool))
            projected_tool_fingerprints = {
                _fingerprint({
                    "id": tool.get("id"),
                    "name": tool.get("name"),
                    "status": tool.get("status"),
                    "task": tool.get("task_id"),
                })
                for tool in tools
            }
            status_counts = {}
            for tool in tools:
                status = str(tool.get("status") or "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

            scheduler_started = time.perf_counter()
            scheduler = scheduler_projection(str(root))
            scheduler_projection_ms.append((time.perf_counter() - scheduler_started) * 1000.0)
            scheduler_projected_task_count = int(scheduler.get("taskCount") or 0)
            scheduler_tasks = [
                task for task in (scheduler.get("tasks") or []) if isinstance(task, dict)
            ]
            scheduler_projected_shape_valid_count = sum(1 for task in scheduler_tasks if _scheduler_task_shape_is_renderable(task))
            scheduler_action_type_counts = {}
            for task in scheduler_tasks:
                action = task.get("action") if isinstance(task.get("action"), dict) else {}
                action_type = str(action.get("type") or "unknown")
                scheduler_action_type_counts[action_type] = scheduler_action_type_counts.get(action_type, 0) + 1
            counts = scheduler.get("counts") if isinstance(scheduler.get("counts"), dict) else {}
            scheduler_projected_error_count = int(counts.get("error") or 0)

    time.sleep(0.05)
    after_scheduler_threads = _scheduler_threads()
    after_timer_threads = _timer_threads()
    orphan_thread_count = max(0, len(after_scheduler_threads) - len(before_scheduler_threads))
    orphan_timer_count = max(0, len(after_timer_threads) - len(before_timer_threads))
    expected_terminal_each = subagent_count // 4
    metrics = {
        "subagentProjectionP95Ms": round(_percentile(subagent_projection_ms, 95), 3),
        "schedulerProjectionP95Ms": round(_percentile(scheduler_projection_ms, 95), 3),
        "subagentTaskCount": subagent_count,
        "projectedSubagentToolCount": projected_tool_count,
        "projectedSubagentToolShapeValidCount": projected_tool_shape_valid_count,
        "projectedSubagentToolFingerprintCount": len(projected_tool_fingerprints),
        "completedSubagentCount": status_counts.get("completed", 0),
        "timeoutSubagentCount": status_counts.get("timeout", 0),
        "cancelledSubagentCount": status_counts.get("cancelled", 0),
        "failedSubagentCount": status_counts.get("failed", 0),
        "schedulerTaskCount": scheduler_task_count,
        "projectedSchedulerTaskCount": scheduler_projected_task_count,
        "projectedSchedulerTaskShapeValidCount": scheduler_projected_shape_valid_count,
        "schedulerSendMessageTaskCount": scheduler_action_type_counts.get("send_message", 0),
        "schedulerAgentTaskCount": scheduler_action_type_counts.get("agent_task", 0),
        "schedulerToolCallTaskCount": scheduler_action_type_counts.get("tool_call", 0),
        "schedulerSkillCallTaskCount": scheduler_action_type_counts.get("skill_call", 0),
        "schedulerErrorTaskCount": scheduler_projected_error_count,
        "schedulerStopMs": round(scheduler_stop_ms, 3),
        "orphanThreadCount": orphan_thread_count,
        "orphanTimerCount": orphan_timer_count,
        "totalMs": round((time.perf_counter() - started) * 1000.0, 3),
    }
    if metrics["subagentProjectionP95Ms"] > 120:
        failure_codes.append("subagent_projection_p95_ms")
    if metrics["schedulerProjectionP95Ms"] > 120:
        failure_codes.append("scheduler_projection_p95_ms")
    if projected_tool_count != subagent_count:
        failure_codes.append("projected_subagent_tool_count")
    if metrics["projectedSubagentToolShapeValidCount"] != subagent_count:
        failure_codes.append("projected_subagent_tool_shape")
    if metrics["projectedSubagentToolFingerprintCount"] != subagent_count:
        failure_codes.append("projected_subagent_tool_unique_count")
    for status_key in ("completedSubagentCount", "timeoutSubagentCount", "cancelledSubagentCount", "failedSubagentCount"):
        if metrics[status_key] < expected_terminal_each:
            failure_codes.append(status_key)
    if scheduler_projected_task_count != scheduler_task_count:
        failure_codes.append("projected_scheduler_task_count")
    if metrics["projectedSchedulerTaskShapeValidCount"] != scheduler_task_count:
        failure_codes.append("projected_scheduler_task_shape")
    expected_action_bucket = scheduler_task_count // 4
    for action_key in ("schedulerSendMessageTaskCount", "schedulerAgentTaskCount", "schedulerToolCallTaskCount", "schedulerSkillCallTaskCount"):
        if metrics[action_key] < expected_action_bucket:
            failure_codes.append(action_key)
    expected_scheduler_error_count = len(range(0, scheduler_task_count, 11))
    if metrics["schedulerErrorTaskCount"] != expected_scheduler_error_count:
        failure_codes.append("scheduler_error_task_count")
    if metrics["orphanThreadCount"] != 0:
        failure_codes.append("orphan_thread_count")
    if metrics["orphanTimerCount"] != 0:
        failure_codes.append("orphan_timer_count")

    artifact = {
        "version": "0.2.3",
        "slice": "R23-16P-08",
        "scenario": "scheduler-subagent",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "status": "fail" if failure_codes else "pass",
        "identity": {
            "requestHash": _hash("perf-scheduler-subagent-request"),
            "sessionHash": _hash("perf-scheduler-subagent-session"),
        },
        "inputShape": {
            "subagentCount": subagent_count,
            "schedulerTaskCount": scheduler_task_count,
            "projectionIterations": projection_iterations,
        },
        "metrics": metrics,
        "failureCodes": failure_codes,
        "redaction": {
            "fullPathsStored": False,
            "taskBodiesStored": False,
            "receiverValuesStored": False,
            "sensitivePatternStored": False,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description="Run R23-16P scheduler/subagent performance smoke.")
    parser.add_argument("--output", default="docs/v0.2.3/artifacts/perf-scheduler-subagent.json")
    parser.add_argument("--subagent-count", type=int, default=40)
    parser.add_argument("--scheduler-task-count", type=int, default=60)
    parser.add_argument("--projection-iterations", type=int, default=8)
    args = parser.parse_args()
    artifact = run(
        Path(args.output),
        subagent_count=args.subagent_count,
        scheduler_task_count=args.scheduler_task_count,
        projection_iterations=args.projection_iterations,
    )
    print(json.dumps({"status": artifact["status"], **artifact["metrics"]}, ensure_ascii=False, sort_keys=True))
    return 0 if artifact["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
