"""Task observation event helpers for long-running agent work."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


TaskEventEmitter = Callable[[str, Dict[str, Any]], None]


@dataclass
class TaskObserver:
    """Emit additive task lifecycle events without replacing legacy tool events."""

    emit_event: Optional[TaskEventEmitter]
    task_id: str
    kind: str
    title: str
    request_id: str = ""
    parent_id: str = ""
    soft_deadline_seconds: int = 0
    hard_deadline_seconds: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    lease_count: int = 0
    health: str = "running"

    def _base_payload(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "title": self.title,
            "request_id": self.request_id,
            "parent_id": self.parent_id,
            "started_at": self.started_at,
            "elapsed_seconds": round(max(0.0, time.time() - self.started_at), 2),
            "soft_deadline_seconds": int(self.soft_deadline_seconds or 0),
            "hard_deadline_seconds": int(self.hard_deadline_seconds or 0),
            "lease_count": int(self.lease_count or 0),
            "health": self.health,
            **{key: value for key, value in (self.metadata or {}).items() if key not in {"prompt", "hidden_context", "token"}},
        }

    def emit(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        if not callable(self.emit_event):
            return
        data = self._base_payload()
        data.update(payload or {})
        self.emit_event(event_type, data)

    def start(self) -> None:
        self.emit("task.started", {"status": "running"})

    def heartbeat(self, **payload: Any) -> None:
        self.emit("task.heartbeat", {"status": "running", **payload})

    def health_changed(self, health: str, **payload: Any) -> None:
        self.health = str(health or self.health or "running")
        self.emit("task.health_changed", {"status": self.health, **payload})

    def extended(self, **payload: Any) -> None:
        self.lease_count += 1
        self.health_changed("extended", **payload)

    def intervention_requested(self, **payload: Any) -> None:
        self.health_changed("waiting_user_decision", **payload)
        self.emit("task.intervention_requested", {"status": "waiting_user_decision", **payload})

    def timeout(self, **payload: Any) -> None:
        self.health_changed("timeout", **payload)

    def end(self, status: str, **payload: Any) -> None:
        terminal_status = str(status or "completed").lower()
        event_type = {
            "success": "task.completed",
            "completed": "task.completed",
            "cancelled": "task.cancelled",
            "canceled": "task.cancelled",
            "timeout": "task.failed",
            "failed": "task.failed",
            "error": "task.failed",
        }.get(terminal_status, "task.completed")
        self.health = terminal_status
        self.emit(event_type, {"status": terminal_status, **payload})
