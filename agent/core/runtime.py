from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List


@dataclass(frozen=True)
class RequestRuntimeAdmission:
    """Backend-owned admission semantics for active-turn control."""

    decision: str
    queue: str
    user_visible_queue: bool


class RequestRuntimeService:
    """Small v0.3.0 authority for request admission vocabulary.

    The legacy WebChannel still owns the physical queue while the migration is
    in progress. This service centralizes the public decisions so replace/amend
    are not surfaced as generic queue or retry failures.
    """

    EXPLICIT_QUEUE = RequestRuntimeAdmission("queued", "explicit", True)
    REPLACEMENT_PENDING = RequestRuntimeAdmission("replacement_pending", "internal", False)

    @classmethod
    def admission_for_interrupt_mode(cls, interrupt_mode: str) -> RequestRuntimeAdmission:
        mode = str(interrupt_mode or "").strip().lower()
        if mode == "queue":
            return cls.EXPLICIT_QUEUE
        if mode in {"replace", "amend"}:
            return cls.REPLACEMENT_PENDING
        return RequestRuntimeAdmission("accepted", "none", False)

    @staticmethod
    def projection_decision(
        decision: str,
        *,
        active_request_ids: Iterable[str] | None = None,
        replaced_request_ids: Iterable[str] | None = None,
        queued_request_id: str = "",
        queue_position: int = 0,
        reason: str = "",
        interrupt_mode: str = "",
    ) -> Dict[str, Any]:
        active = [str(item) for item in (active_request_ids or []) if str(item or "").strip()]
        replaced = [str(item) for item in (replaced_request_ids or []) if str(item or "").strip()]
        queue = "explicit" if decision == "queued" else ("internal" if decision == "replacement_pending" else "none")
        return {
            "policy": "active_turn_control",
            "queue": queue,
            "decision": decision,
            "active_request_ids": active,
            "replaced_request_ids": replaced,
            "cancelled_requests": 0,
            "cancelled_subagents": 0,
            "retry_after_ms": 0,
            "reason": reason,
            "queue_position": int(queue_position or 0),
            "queued_request_id": queued_request_id,
            "interrupt_mode": interrupt_mode,
        }

    @staticmethod
    def replacement_pending_message(interrupt_mode: str, queue_position: int = 0) -> str:
        mode = str(interrupt_mode or "").strip().lower()
        action = "补充说明" if mode == "amend" else "新消息"
        suffix = f"（等待位次 {queue_position}）" if queue_position else ""
        return f"已接收{action}，旧任务停止后会自动继续{suffix}。"
