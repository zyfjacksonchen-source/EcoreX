"""Bounded system-level Runtime health and metrics projection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import Any

from ecorex.runtime.database import SQLiteDatabase, json_dumps, json_loads
from ecorex.runtime.schema_catalog import validate_product_schema
from ecorex.runtime.ids import new_id

from .audit import AuditRedactor


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stored_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("system metric timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _file_size_if_present(path: Path) -> int:
    """Read one point-in-time size without an existence-check race.

    SQLite may unlink the WAL immediately after the last connection closes or
    during a checkpoint.  ``exists()`` followed by ``stat()`` therefore turns
    normal WAL lifecycle churn into a health-endpoint failure.  A single stat
    is the observation; disappearance before that syscall is a valid zero-byte
    sample, while other filesystem errors still surface as real storage
    failures.
    """

    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _safe_provider_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "omitted"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, Mapping):
        return {
            str(key)[:64]: _safe_provider_value(child, depth=depth + 1)
            for key, child in list(value.items())[:128]
        }
    if isinstance(value, (list, tuple)):
        return [_safe_provider_value(child, depth=depth + 1) for child in value[:128]]
    return type(value).__name__.casefold()


@dataclass(frozen=True, slots=True)
class RuntimeSignalSnapshot:
    sse_connections: int
    sse_peak_connections: int
    sse_events_sent: int
    sse_disconnects: int
    event_loop_lag_ms: float
    event_loop_lag_peak_ms: float

    def to_dict(self) -> dict[str, int | float]:
        return {
            "sse_connections": self.sse_connections,
            "sse_peak_connections": self.sse_peak_connections,
            "sse_events_sent": self.sse_events_sent,
            "sse_disconnects": self.sse_disconnects,
            "event_loop_lag_ms": round(self.event_loop_lag_ms, 3),
            "event_loop_lag_peak_ms": round(self.event_loop_lag_peak_ms, 3),
        }


class RuntimeSignalRegistry:
    """Low-cardinality, thread-safe live counters for the local process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sse_connections = 0
        self._sse_peak_connections = 0
        self._sse_events_sent = 0
        self._sse_disconnects = 0
        self._event_loop_lag_ms = 0.0
        self._event_loop_lag_peak_ms = 0.0

    def sse_connected(self) -> None:
        with self._lock:
            self._sse_connections += 1
            self._sse_peak_connections = max(
                self._sse_peak_connections, self._sse_connections
            )

    def sse_disconnected(self) -> None:
        with self._lock:
            if self._sse_connections > 0:
                self._sse_connections -= 1
            self._sse_disconnects += 1

    def sse_events_sent(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("SSE event count must be a non-negative integer")
        with self._lock:
            self._sse_events_sent += count

    def observe_event_loop_lag(self, milliseconds: float) -> None:
        value = max(0.0, min(float(milliseconds), 60_000.0))
        with self._lock:
            self._event_loop_lag_ms = value
            self._event_loop_lag_peak_ms = max(self._event_loop_lag_peak_ms, value)

    def snapshot(self) -> RuntimeSignalSnapshot:
        with self._lock:
            return RuntimeSignalSnapshot(
                sse_connections=self._sse_connections,
                sse_peak_connections=self._sse_peak_connections,
                sse_events_sent=self._sse_events_sent,
                sse_disconnects=self._sse_disconnects,
                event_loop_lag_ms=self._event_loop_lag_ms,
                event_loop_lag_peak_ms=self._event_loop_lag_peak_ms,
            )


@dataclass(frozen=True, slots=True)
class SystemHealthSample:
    sample_id: str
    overall: str
    summary: str
    components: tuple[dict[str, Any], ...]
    metrics: Mapping[str, Any]
    sampled_at: datetime

    def to_dict(self, *, technical: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "sample_id": self.sample_id,
            "overall": self.overall,
            "summary": self.summary,
            "components": [dict(component) for component in self.components],
            "sampled_at": _stored_time(self.sampled_at),
        }
        if technical:
            value["metrics"] = dict(self.metrics)
        return value


class SystemObservabilityService:
    def __init__(
        self,
        database: SQLiteDatabase | str | Path,
        *,
        registry: RuntimeSignalRegistry | None = None,
        providers: Mapping[str, Callable[[], Any]] | None = None,
        persistence_allowed: Callable[[], bool] | None = None,
        persistence_scope: Callable[[], AbstractContextManager[None]] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        max_samples: int = 1440,
    ) -> None:
        if not 60 <= max_samples <= 100_000:
            raise ValueError("system metric retention must be between 60 and 100000 samples")
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        self.registry = registry or RuntimeSignalRegistry()
        self.providers = dict(providers or {})
        self.persistence_allowed = persistence_allowed
        self.persistence_scope = persistence_scope
        self.clock = clock
        self.max_samples = max_samples
        self._process_started = time.monotonic()
        self._initialize()

    def _initialize(self) -> None:
        with self.database.reader() as connection:
            validate_product_schema(connection)

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    @staticmethod
    def _group_counts(
        connection: sqlite3.Connection,
        table: str,
        column: str,
    ) -> dict[str, int]:
        if not SystemObservabilityService._table_exists(connection, table):
            return {}
        return {
            str(row["value"]): int(row["count"])
            for row in connection.execute(
                f"SELECT {column} value,COUNT(*) count FROM {table} GROUP BY {column}"
            ).fetchall()
        }

    def _database_metrics(self) -> dict[str, Any]:
        path = self.database.path
        with self.database.reader() as connection:
            jobs = self._group_counts(connection, "jobs", "status")
            turns = self._group_counts(connection, "turns", "status")
            interactions = self._group_counts(connection, "interactions", "status")
            image_jobs = self._group_counts(connection, "image_jobs", "status")
            retouch_jobs = self._group_counts(connection, "retouch_jobs", "status")
            output_materializations = self._group_counts(
                connection, "output_materializations", "status"
            )
            event_row = connection.execute(
                "SELECT COUNT(*) count,MAX(created_at) latest FROM events"
            ).fetchone() if self._table_exists(connection, "events") else None
            artifact_row = connection.execute(
                "SELECT COUNT(*) count,"
                "SUM(CASE WHEN visibility='internal' THEN 1 ELSE 0 END) internal "
                "FROM artifact_entities"
            ).fetchone() if self._table_exists(connection, "artifact_entities") else None
            revision_row = connection.execute(
                "SELECT COUNT(*) count,COALESCE(SUM(size_bytes),0) bytes "
                "FROM artifact_revisions"
            ).fetchone() if self._table_exists(connection, "artifact_revisions") else None
            memory_row = connection.execute(
                "SELECT "
                "SUM(CASE WHEN memory_state='active' AND memory_origin!='factory' THEN 1 ELSE 0 END) active,"
                "SUM(CASE WHEN memory_state='tombstoned' THEN 1 ELSE 0 END) tombstoned "
                "FROM memory_canonical_records"
            ).fetchone() if self._table_exists(connection, "memory_canonical_records") else None
            queued = connection.execute(
                "SELECT MIN(created_at) oldest FROM jobs "
                "WHERE status IN ('queued','retry_scheduled')"
            ).fetchone() if self._table_exists(connection, "jobs") else None

        oldest_age = 0.0
        if queued and queued["oldest"]:
            try:
                oldest = datetime.fromisoformat(str(queued["oldest"]))
                if oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=UTC)
                oldest_age = max(0.0, (self.clock() - oldest.astimezone(UTC)).total_seconds())
            except ValueError:
                oldest_age = -1.0
        return {
            "database_bytes": _file_size_if_present(path),
            "wal_bytes": _file_size_if_present(Path(str(path) + "-wal")),
            "jobs": jobs,
            "turns": turns,
            "interactions": interactions,
            "image_jobs": image_jobs,
            "retouch_jobs": retouch_jobs,
            "output_materializations": output_materializations,
            "oldest_queued_seconds": round(oldest_age, 3),
            "events_total": int(event_row["count"] if event_row else 0),
            "latest_event_at": str(event_row["latest"] or "") if event_row else "",
            "artifacts_total": int(artifact_row["count"] if artifact_row else 0),
            "artifacts_internal": int(artifact_row["internal"] or 0) if artifact_row else 0,
            "artifact_revisions": int(revision_row["count"] if revision_row else 0),
            "artifact_bytes": int(revision_row["bytes"] if revision_row else 0),
            "memory_active": int(memory_row["active"] or 0) if memory_row else 0,
            "memory_tombstoned": int(memory_row["tombstoned"] or 0) if memory_row else 0,
        }

    @staticmethod
    def _rss_bytes() -> int:
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class Counters(ctypes.Structure):
                    _fields_ = [
                        ("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                    ]

                counters = Counters()
                counters.cb = ctypes.sizeof(Counters)
                handle = ctypes.windll.kernel32.GetCurrentProcess()
                if ctypes.windll.psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb
                ):
                    return int(counters.WorkingSetSize)
            except Exception:
                return 0
            return 0
        try:
            import resource

            value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            return value if sys.platform == "darwin" else value * 1024
        except Exception:
            return 0

    def _provider_metrics(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        redactor = AuditRedactor(max_depth=8, max_string_bytes=4096)
        for name, provider in sorted(self.providers.items()):
            try:
                values[name] = _safe_provider_value(redactor.redact(provider()))
            except Exception as error:
                values[name] = {"state": "unavailable", "error": type(error).__name__.casefold()}
        return values

    @staticmethod
    def _component(
        component_id: str,
        label: str,
        status: str,
        message: str,
    ) -> dict[str, str]:
        return {"component_id": component_id, "label": label, "status": status, "message": message}

    def collect(self, *, persist: bool = True) -> SystemHealthSample:
        sampled_at = self.clock()
        signals = self.registry.snapshot()
        database = self._database_metrics()
        providers = self._provider_metrics()
        metrics: dict[str, Any] = {
            "runtime": signals.to_dict(),
            "process": {
                "pid": os.getpid(),
                "uptime_seconds": round(max(0.0, time.monotonic() - self._process_started), 3),
                "cpu_seconds": round(time.process_time(), 3),
                "rss_bytes": self._rss_bytes(),
            },
            "storage": database,
            "services": providers,
        }

        components: list[dict[str, str]] = []
        response_status = "healthy"
        response_message = "界面和后台响应正常。"
        if signals.event_loop_lag_ms >= 1000:
            response_status = "attention"
            response_message = "后台响应明显变慢，正在保护当前任务。"
        elif signals.event_loop_lag_ms >= 250:
            response_status = "degraded"
            response_message = "后台响应有些变慢，但任务仍在运行。"
        components.append(self._component("responsiveness", "运行响应", response_status, response_message))

        dead = int(database["jobs"].get("dead_letter", 0))
        failed = int(database["jobs"].get("failed", 0))
        queued = int(database["jobs"].get("queued", 0)) + int(database["jobs"].get("retry_scheduled", 0))
        queue_status = "attention" if dead else "degraded" if failed or database["oldest_queued_seconds"] > 60 else "healthy"
        if dead:
            queue_message = f"有 {dead} 个任务需要检查，其他任务不会被它阻塞。"
        elif failed:
            queue_message = f"有 {failed} 个任务未完成，可在对应任务中重试。"
        elif queued:
            queue_message = f"有 {queued} 个任务正在排队。"
        else:
            queue_message = "任务队列运行正常。"
        components.append(self._component("jobs", "任务队列", queue_status, queue_message))

        wal_bytes = int(database["wal_bytes"])
        storage_status = "degraded" if wal_bytes >= 512 * 1024 * 1024 else "healthy"
        storage_message = "本地记录和产物索引正常。" if storage_status == "healthy" else "本地写入积压较多，EcoreX 将在空闲时整理。"
        components.append(self._component("storage", "本地数据", storage_status, storage_message))

        service_status = "healthy"
        if any(
            isinstance(value, Mapping) and value.get("state") in {"failed", "unavailable", "degraded"}
            for value in providers.values()
        ):
            service_status = "degraded"
        components.append(self._component(
            "services",
            "扩展与连接",
            service_status,
            "扩展和连接服务可用。" if service_status == "healthy" else "部分扩展或连接暂不可用，核心任务仍可继续。",
        ))

        if "invariant" in providers:
            invariant = providers.get("invariant")
            invariant_status = (
                str(invariant.get("status"))
                if isinstance(invariant, Mapping)
                else "unavailable"
            )
            if invariant_status == "critical":
                integrity_status = "critical"
                integrity_message = (
                    "检测到本地运行状态不一致，e-Mate 已切换为只读保护；"
                    "历史记录和诊断信息仍可查看。"
                )
            elif invariant_status == "healthy":
                integrity_status = "healthy"
                integrity_message = "运行状态校验正常。"
            else:
                integrity_status = "degraded"
                integrity_message = "运行状态校验暂不可用，后台将继续重试。"
            components.append(
                self._component(
                    "runtime_integrity",
                    "运行安全",
                    integrity_status,
                    integrity_message,
                )
            )

        order = {"healthy": 0, "degraded": 1, "attention": 2, "critical": 3}
        overall = max((item["status"] for item in components), key=order.__getitem__)
        summary = {
            "healthy": "e-Mate 运行正常",
            "degraded": "e-Mate 可以继续工作，但有部分项目需要留意",
            "attention": "e-Mate 已保护当前数据，有项目需要处理",
            "critical": "e-Mate 已进入只读保护，历史和诊断仍可查看",
        }[overall]
        sample = SystemHealthSample(
            sample_id=new_id("syssample"),
            overall=overall,
            summary=summary,
            components=tuple(components),
            metrics=metrics,
            sampled_at=sampled_at,
        )
        if persist and self._persistence_is_allowed():
            if self.persistence_scope is None:
                self._persist(sample)
            else:
                # The scope owns the Runtime admission permit and transaction
                # commit guard for the complete write. A critical gate closure
                # after sampling but before commit therefore rolls the sample
                # back instead of accepting a late observability write.
                with self.persistence_scope():
                    self._persist(sample)
        return sample

    def _persistence_is_allowed(self) -> bool:
        if self.persistence_allowed is None:
            return True
        try:
            return bool(self.persistence_allowed())
        except Exception:
            return False

    def _persist(self, sample: SystemHealthSample) -> None:
        payload = json_dumps(sample.to_dict(technical=True))
        if len(payload.encode("utf-8")) > 128 * 1024:
            raise ValueError("system metric sample exceeded the bounded payload size")
        timestamp = _stored_time(sample.sampled_at)
        with self.database.transaction() as connection:
            previous = connection.execute(
                "SELECT overall FROM system_health_state WHERE singleton=1"
            ).fetchone()
            connection.execute(
                "INSERT INTO system_metric_samples(sample_id,overall,payload_json,created_at) "
                "VALUES(?,?,?,?)",
                (sample.sample_id, sample.overall, payload, timestamp),
            )
            connection.execute(
                "INSERT INTO system_health_state(singleton,overall,updated_at) VALUES(1,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET overall=excluded.overall,updated_at=excluded.updated_at",
                (sample.overall, timestamp),
            )
            if previous is None or previous["overall"] != sample.overall:
                connection.execute(
                    "INSERT INTO system_health_events(event_id,from_status,to_status,payload_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        new_id("syshealth"),
                        previous["overall"] if previous else None,
                        sample.overall,
                        json_dumps({"summary": sample.summary}),
                        timestamp,
                    ),
                )
            connection.execute(
                "DELETE FROM system_metric_samples WHERE sample_id IN ("
                "SELECT sample_id FROM system_metric_samples ORDER BY created_at DESC,sample_id DESC "
                "LIMIT -1 OFFSET ?)",
                (self.max_samples,),
            )

    @staticmethod
    def _from_payload(payload: Mapping[str, Any]) -> SystemHealthSample:
        sampled_at = datetime.fromisoformat(str(payload["sampled_at"]))
        if sampled_at.tzinfo is None:
            sampled_at = sampled_at.replace(tzinfo=UTC)
        return SystemHealthSample(
            sample_id=str(payload["sample_id"]),
            overall=str(payload["overall"]),
            summary=str(payload["summary"]),
            components=tuple(dict(item) for item in payload.get("components", [])),
            metrics=dict(payload.get("metrics", {})),
            sampled_at=sampled_at.astimezone(UTC),
        )

    def latest(self, *, collect_if_missing: bool = True) -> SystemHealthSample | None:
        if not self._persistence_is_allowed():
            return self.collect(persist=False)
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT payload_json FROM system_metric_samples "
                "ORDER BY created_at DESC,sample_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return self.collect(persist=False) if collect_if_missing else None
        return self._from_payload(json_loads(row["payload_json"], {}))

    def history(self, *, limit: int = 60) -> list[SystemHealthSample]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("system metric history limit must be between 1 and 200")
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM system_metric_samples "
                "ORDER BY created_at DESC,sample_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._from_payload(json_loads(row["payload_json"], {})) for row in rows]


class SystemObservabilitySupervisor:
    def __init__(
        self,
        service: SystemObservabilityService,
        *,
        collect_interval_seconds: float = 10.0,
        lag_interval_seconds: float = 1.0,
    ) -> None:
        if not 1 <= collect_interval_seconds <= 3600:
            raise ValueError("system collection interval must be between 1 and 3600 seconds")
        if not 0.05 <= lag_interval_seconds <= 10:
            raise ValueError("event-loop lag interval must be between 0.05 and 10 seconds")
        self.service = service
        self.collect_interval_seconds = collect_interval_seconds
        self.lag_interval_seconds = lag_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ecorex-system-observability")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_collection = loop.time()
        while not self._stop.is_set():
            expected = loop.time() + self.lag_interval_seconds
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.lag_interval_seconds)
                return
            except TimeoutError:
                pass
            self.service.registry.observe_event_loop_lag(
                max(0.0, (loop.time() - expected) * 1000.0)
            )
            if loop.time() >= next_collection:
                try:
                    await asyncio.to_thread(self.service.collect)
                except Exception:
                    # Health collection must never terminate the Runtime. The
                    # next bounded interval retries and provider failures are
                    # represented inside the sample where possible.
                    pass
                next_collection = loop.time() + self.collect_interval_seconds


__all__ = [
    "RuntimeSignalRegistry",
    "RuntimeSignalSnapshot",
    "SystemHealthSample",
    "SystemObservabilityService",
    "SystemObservabilitySupervisor",
]
