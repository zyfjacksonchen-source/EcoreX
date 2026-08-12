"""CowAgent-compatible local scheduler.

The scheduler is a local user feature, not an enterprise policy surface.  It
stores the same create/list/get/delete/enable/disable task shape as CowAgent
and delegates due actions to the running Runtime.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import threading
import uuid
from typing import Any, Awaitable, Callable, Mapping

class CowSchedulerError(RuntimeError):
    code = "scheduler_failed"


class CowTaskStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()

    def load(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise CowSchedulerError("scheduler task store is invalid") from error
            tasks = payload.get("tasks")
            if not isinstance(tasks, dict):
                raise CowSchedulerError("scheduler task store is invalid")
            return {str(key): dict(value) for key, value in tasks.items()}

    def save(self, tasks: Mapping[str, Mapping[str, Any]]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "updated_at": datetime.now().isoformat(),
                "tasks": {key: dict(value) for key, value in tasks.items()},
            }
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)

    def add(self, task: Mapping[str, Any]) -> None:
        with self._lock:
            tasks = self.load()
            task_id = str(task.get("id") or "")
            if not task_id or task_id in tasks:
                raise CowSchedulerError("scheduler task identity is invalid")
            tasks[task_id] = dict(task)
            self.save(tasks)

    def update(self, task_id: str, updates: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            tasks = self.load()
            if task_id not in tasks:
                raise CowSchedulerError(f"task {task_id!r} was not found")
            tasks[task_id].update(dict(updates))
            tasks[task_id]["updated_at"] = datetime.now().isoformat()
            self.save(tasks)
            return dict(tasks[task_id])

    def delete(self, task_id: str) -> None:
        with self._lock:
            tasks = self.load()
            if task_id not in tasks:
                raise CowSchedulerError(f"task {task_id!r} was not found")
            del tasks[task_id]
            self.save(tasks)

    def get(self, task_id: str) -> dict[str, Any] | None:
        task = self.load().get(task_id)
        return dict(task) if task is not None else None

    def list(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        tasks = [dict(value) for value in self.load().values()]
        if enabled_only:
            tasks = [task for task in tasks if task.get("enabled", True)]
        return sorted(
            tasks,
            key=lambda task: (
                0 if task.get("enabled", True) else 1,
                task.get("next_run_at") or "9999-12-31",
            ),
        )


def _schedule(schedule_type: str, value: str) -> dict[str, Any]:
    now = datetime.now()
    if schedule_type == "cron":
        from croniter import croniter

        croniter(value, now)
        return {"type": "cron", "expression": value}
    if schedule_type == "interval":
        seconds = int(value)
        if seconds <= 0:
            raise ValueError("interval must be positive")
        return {"type": "interval", "seconds": seconds}
    if schedule_type != "once":
        raise ValueError("schedule type is invalid")
    match = re.fullmatch(r"\+(\d+)([smhd])", value)
    if match:
        amount = int(match.group(1))
        delta = {
            "s": timedelta(seconds=amount),
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
        }[match.group(2)]
        run_at = now + delta
    else:
        run_at = datetime.fromisoformat(value)
        if run_at.tzinfo is not None:
            run_at = run_at.astimezone().replace(tzinfo=None)
    return {"type": "once", "run_at": run_at.isoformat()}


def _next_run(schedule: Mapping[str, Any], now: datetime) -> datetime | None:
    kind = schedule.get("type")
    if kind == "cron":
        from croniter import croniter

        return croniter(str(schedule["expression"]), now).get_next(datetime)
    if kind == "interval":
        return now + timedelta(seconds=int(schedule["seconds"]))
    if kind == "once":
        value = datetime.fromisoformat(str(schedule["run_at"]))
        return value.astimezone().replace(tzinfo=None) if value.tzinfo else value
    return None


class CowSchedulerTool:
    def __init__(self, store: CowTaskStore) -> None:
        self.store = store

    def __call__(self, arguments: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        action = str(arguments.get("action") or "")
        if action == "create":
            return self._create(arguments, context)
        task_id = str(arguments.get("task_id") or "")
        if action == "list":
            return {"tasks": self.store.list()}
        if not task_id:
            raise CowSchedulerError("task_id is required")
        if action == "get":
            task = self.store.get(task_id)
            if task is None:
                raise CowSchedulerError(f"task {task_id!r} was not found")
            return {"task": task}
        if action == "delete":
            self.store.delete(task_id)
            return {"task_id": task_id, "deleted": True}
        if action in {"enable", "disable"}:
            task = self.store.update(task_id, {"enabled": action == "enable"})
            return {"task": task}
        raise CowSchedulerError(f"unknown scheduler action: {action}")

    def _create(self, arguments: Mapping[str, Any], context: Any) -> dict[str, Any]:
        name = str(arguments.get("name") or "").strip()
        message = str(arguments.get("message") or "").strip()
        ai_task = str(arguments.get("ai_task") or "").strip()
        schedule_type = str(arguments.get("schedule_type") or "")
        schedule_value = str(arguments.get("schedule_value") or "")
        if not name:
            raise CowSchedulerError("name is required")
        if bool(message) == bool(ai_task):
            raise CowSchedulerError("exactly one of message or ai_task is required")
        if not schedule_type or not schedule_value:
            raise CowSchedulerError("schedule_type and schedule_value are required")
        try:
            parsed = _schedule(schedule_type, schedule_value)
        except (TypeError, ValueError) as error:
            raise CowSchedulerError("schedule is invalid") from error
        scope = getattr(context, "execution_scope", None)
        if scope is None:
            raise CowSchedulerError("scheduler requires a conversation")
        now = datetime.now()
        task_id = uuid.uuid4().hex[:8]
        task = {
            "id": task_id,
            "name": name,
            "enabled": True,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "schedule": parsed,
            "action": {
                "type": "send_message" if message else "agent_task",
                "content": message or ai_task,
                "thread_id": scope.thread_id,
                "silent": bool(arguments.get("silent", False)) if ai_task else False,
            },
            "next_run_at": _next_run(parsed, now).isoformat(),
        }
        self.store.add(task)
        return {"task": task}


class CowSchedulerService:
    def __init__(
        self,
        store: CowTaskStore,
        execute: Callable[[Mapping[str, Any]], Awaitable[bool | None]],
        *,
        poll_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.execute = execute
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._active: set[str] = set()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            await self.run_due()
            await asyncio.sleep(self.poll_seconds)

    async def run_due(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now()
        for task in self.store.list(enabled_only=True):
            task_id = str(task["id"])
            if task_id in self._active:
                continue
            next_run = datetime.fromisoformat(str(task.get("next_run_at") or ""))
            if next_run.tzinfo is not None:
                next_run = next_run.astimezone().replace(tzinfo=None)
            if next_run > now:
                continue
            if (now - next_run).total_seconds() > 600:
                if task["schedule"].get("type") == "once":
                    self.store.delete(task_id)
                else:
                    following = _next_run(task["schedule"], now)
                    if following is not None:
                        self.store.update(task_id, {"next_run_at": following.isoformat()})
                continue
            self._active.add(task_id)
            try:
                try:
                    succeeded = await self.execute(task)
                except Exception:
                    succeeded = False
            finally:
                self._active.discard(task_id)
            if succeeded is False:
                continue
            if task["schedule"].get("type") == "once":
                self.store.delete(task_id)
            else:
                following = _next_run(task["schedule"], now)
                updates: dict[str, Any] = {"last_run_at": now.isoformat()}
                if following is not None:
                    updates["next_run_at"] = following.isoformat()
                self.store.update(task_id, updates)


__all__ = [
    "CowSchedulerError",
    "CowSchedulerService",
    "CowSchedulerTool",
    "CowTaskStore",
]
