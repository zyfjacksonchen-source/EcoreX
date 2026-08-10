"""Provider-reported composer usage projection.

The event stream remains the fact source.  This module intentionally derives a
small read model from completed model-response facts instead of trusting client
counters, estimating tokens in JavaScript, or conflating usage with a signed
managed-service quota.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ecorex.capabilities import ManagedModelCatalog, UnknownModelError
from ecorex.protocol import (
    ContextUsageProjection,
    ConversationUsageProjection,
    TaskActivityDay,
    TaskActivityProjection,
    TERMINAL_TURN_STATUSES,
    TokenUsageWindow,
    TurnStatus,
    UsageDataQualityProjection,
)

from .database import SQLiteDatabase


_MAX_REPORTED_TOKENS = 10**12
_MAX_RECOVERY_RECEIPTS = 64
_MAX_RECOVERY_RECEIPT_BYTES = 64 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _storage_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("usage projection timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value <= _MAX_REPORTED_TOKENS:
        return None
    return value


@dataclass(frozen=True, slots=True)
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "_Usage") -> "_Usage":
        return _Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    def projection(self) -> TokenUsageWindow:
        return TokenUsageWindow(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
        )


def _usage_from_payload(raw: object) -> _Usage | None:
    if not isinstance(raw, Mapping):
        return None
    input_tokens = _nonnegative_int(raw.get("input_tokens"))
    output_tokens = _nonnegative_int(raw.get("output_tokens"))
    total_tokens = _nonnegative_int(raw.get("total_tokens"))
    # OpenAI-compatible providers may use prompt/completion terminology. It
    # remains provider-reported data, merely normalized at the local boundary.
    if input_tokens is None:
        input_tokens = _nonnegative_int(raw.get("prompt_tokens"))
    if output_tokens is None:
        output_tokens = _nonnegative_int(raw.get("completion_tokens"))
    if input_tokens is None:
        input_tokens = 0
    if output_tokens is None:
        output_tokens = 0
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    if total_tokens < input_tokens + output_tokens:
        # A contradictory provider value cannot become a smaller aggregate;
        # retain independently reported input/output values without guessing.
        total_tokens = input_tokens + output_tokens
    if input_tokens == output_tokens == total_tokens == 0 and not any(
        key in raw
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "prompt_tokens",
            "completion_tokens",
        )
    ):
        return None
    return _Usage(input_tokens, output_tokens, total_tokens)


class UsageProjectionService:
    """Derive calendar usage and latest context facts from immutable Events."""

    def __init__(
        self,
        database: SQLiteDatabase | str,
        *,
        model_catalog: ManagedModelCatalog,
        timezone_name: str = "Asia/Shanghai",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            raise ValueError("usage timezone is required")
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            raise ValueError("usage timezone is invalid") from None
        self.database = (
            database
            if isinstance(database, SQLiteDatabase)
            else SQLiteDatabase(database)
        )
        self.model_catalog = model_catalog
        self.timezone_name = timezone_name
        self._zone = zone
        self._clock = clock

    def _data_quality(self) -> UsageDataQualityProjection:
        root = self.database.path.parent / "observability-quarantine"
        try:
            metadata = root.lstat()
        except FileNotFoundError:
            return UsageDataQualityProjection()
        except OSError:
            return UsageDataQualityProjection(audit_continuity="uncertain")
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)
        ):
            return UsageDataQualityProjection(audit_continuity="uncertain")
        try:
            entries = sorted(os.scandir(root), key=lambda item: item.name)
        except OSError:
            return UsageDataQualityProjection(audit_continuity="uncertain")
        if len(entries) > _MAX_RECOVERY_RECEIPTS:
            return UsageDataQualityProjection(
                audit_continuity="uncertain",
                recovery_count=len(entries),
            )

        recovery_count = 0
        removed_audit_rows = 0
        removed_trace_rows = 0
        last_recovery_at: datetime | None = None
        uncertain = False
        for entry in entries:
            try:
                directory = entry.stat(follow_symlinks=False)
                if (
                    entry.is_symlink()
                    or not stat.S_ISDIR(directory.st_mode)
                    or bool(
                        getattr(directory, "st_file_attributes", 0)
                        & _REPARSE_POINT
                    )
                ):
                    uncertain = True
                    continue
                path = Path(entry.path) / "recovery-receipt.json"
                metadata = path.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or bool(
                        getattr(metadata, "st_file_attributes", 0)
                        & _REPARSE_POINT
                    )
                    or metadata.st_size > _MAX_RECOVERY_RECEIPT_BYTES
                ):
                    uncertain = True
                    continue
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(path, flags)
                with os.fdopen(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    payload = stream.read(_MAX_RECOVERY_RECEIPT_BYTES + 1)
                    opened_after = os.fstat(stream.fileno())
                current = path.lstat()
                identity = (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                )
                if (
                    len(payload) > _MAX_RECOVERY_RECEIPT_BYTES
                    or identity
                    != (
                        opened.st_dev,
                        opened.st_ino,
                        opened.st_size,
                        opened.st_mtime_ns,
                    )
                    or identity
                    != (
                        opened_after.st_dev,
                        opened_after.st_ino,
                        opened_after.st_size,
                        opened_after.st_mtime_ns,
                    )
                    or identity
                    != (
                        current.st_dev,
                        current.st_ino,
                        current.st_size,
                        current.st_mtime_ns,
                    )
                ):
                    uncertain = True
                    continue
                value = json.loads(payload.decode("utf-8"))
            except (OSError, UnicodeError, ValueError, RecursionError):
                uncertain = True
                continue
            if not isinstance(value, Mapping) or value.get("reason") != "audit_key_mismatch":
                uncertain = True
                continue
            state = value.get("state")
            if state not in {"pending", "completed"}:
                uncertain = True
                continue
            recovery_count += 1
            uncertain = uncertain or state != "completed"
            removed = value.get("removed_rows")
            if not isinstance(removed, Mapping):
                uncertain = True
                continue
            for name, raw_count in removed.items():
                count = _nonnegative_int(raw_count)
                if count is None:
                    uncertain = True
                    continue
                if str(name).startswith("observability_audit_"):
                    removed_audit_rows += count
                elif str(name).startswith("observability_trace_"):
                    removed_trace_rows += count
            recovered_at = _parse_time(value.get("created_at"))
            if recovered_at is None:
                uncertain = True
            elif last_recovery_at is None or recovered_at > last_recovery_at:
                last_recovery_at = recovered_at
        continuity = (
            "uncertain"
            if uncertain
            else "recovered_with_gap"
            if recovery_count
            else "complete"
        )
        return UsageDataQualityProjection(
            audit_continuity=continuity,
            recovery_count=recovery_count,
            removed_audit_rows=removed_audit_rows,
            removed_trace_rows=removed_trace_rows,
            last_recovery_at=last_recovery_at,
        )

    @staticmethod
    def _frozen_model_projection(
        connection,
        *,
        turn_id: str,
        job_id: str | None,
        model_id: str | None,
        fallback_catalog: ManagedModelCatalog,
    ) -> tuple[int | None, str | None, str | None]:
        """Resolve presentation facts from the Turn's immutable model snapshot.

        An administrator may reuse a stable local model ID while changing its
        name, upstream route or context policy.  Reading the process-current
        catalog here would silently rewrite history, so completed responses
        first resolve the exact catalog captured by their durable job/Turn.
        """

        snapshot_id: str | None = None
        if job_id:
            row = connection.execute(
                "SELECT model_catalog_snapshot_id FROM job_runtime_contexts "
                "WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is not None:
                snapshot_id = str(row["model_catalog_snapshot_id"])
        if snapshot_id is None:
            row = connection.execute(
                "SELECT config_snapshot_id FROM events "
                "WHERE turn_id = ? AND event_type = 'turn.accepted' "
                "ORDER BY seq ASC LIMIT 1",
                (turn_id,),
            ).fetchone()
            if row is not None and row["config_snapshot_id"]:
                config = connection.execute(
                    "SELECT payload_json FROM runtime_snapshots "
                    "WHERE snapshot_id = ? AND kind = 'config'",
                    (str(row["config_snapshot_id"]),),
                ).fetchone()
                if config is not None:
                    try:
                        payload = json.loads(str(config["payload_json"]))
                    except (TypeError, ValueError):
                        payload = None
                    if isinstance(payload, Mapping):
                        candidate = payload.get("model_catalog_snapshot_id")
                        if isinstance(candidate, str) and candidate:
                            snapshot_id = candidate

        if snapshot_id is not None:
            row = connection.execute(
                "SELECT payload_json FROM runtime_snapshots "
                "WHERE snapshot_id = ? AND kind = 'models'",
                (snapshot_id,),
            ).fetchone()
            if row is not None:
                try:
                    payload = json.loads(str(row["payload_json"]))
                except (TypeError, ValueError):
                    payload = None
                modalities = (
                    payload.get("modalities") if isinstance(payload, Mapping) else None
                )
                chat = (
                    modalities.get("chat") if isinstance(modalities, Mapping) else None
                )
                if isinstance(chat, list):
                    for item in chat:
                        if (
                            not isinstance(item, Mapping)
                            or item.get("model_id") != model_id
                        ):
                            continue
                        policy = item.get("model_policy")
                        context = (
                            policy.get("context_management")
                            if isinstance(policy, Mapping)
                            else None
                        )
                        threshold = (
                            _nonnegative_int(context.get("compact_threshold_tokens"))
                            if isinstance(context, Mapping)
                            else None
                        )
                        display_name = item.get("display_name")
                        return (
                            threshold if threshold and threshold >= 1_000 else None,
                            str(display_name)
                            if isinstance(display_name, str)
                            else None,
                            snapshot_id,
                        )

        # Compatibility fallback for imported/pre-snapshot history only.  New
        # v1 Turns always take the immutable branch above.
        if model_id:
            try:
                spec = fallback_catalog.get(model_id)
            except UnknownModelError:
                return None, None, snapshot_id
            threshold = (
                spec.model_policy.compact_threshold_tokens
                if spec.model_policy is not None
                else None
            )
            return threshold, spec.display_name, snapshot_id
        return None, None, snapshot_id

    def project(self, thread_id: str) -> ConversationUsageProjection:
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("usage projection thread identity is invalid")
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("usage projection clock must be timezone-aware")
        now = now.astimezone(UTC)
        local_now = now.astimezone(self._zone)
        day_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start_local = day_start_local - timedelta(days=day_start_local.weekday())
        day_start = day_start_local.astimezone(UTC)
        week_start = week_start_local.astimezone(UTC)

        today = _Usage()
        week = _Usage()
        latest_context_usage: _Usage | None = None
        latest_context_time: datetime | None = None
        activity_start_local = day_start_local - timedelta(days=6)
        activity_start = activity_start_local.astimezone(UTC)
        activity_end = now

        with self.database.reader() as connection:
            if thread_id != "account":
                exists = connection.execute(
                    "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
                ).fetchone()
                if exists is None:
                    raise KeyError("usage projection thread does not exist")
            rows = connection.execute(
                "SELECT thread_id, created_at, payload_json FROM events "
                "WHERE event_type = 'model.response_completed' AND created_at >= ? "
                "ORDER BY created_at ASC, event_id ASC",
                (_storage_time(week_start),),
            ).fetchall()
            context_row = connection.execute(
                "SELECT events.created_at, events.payload_json, events.job_id, "
                "events.turn_id, turns.agent_model_id "
                "FROM events JOIN turns ON turns.turn_id = events.turn_id "
                "WHERE events.thread_id = ? AND events.event_type = 'model.response_completed' "
                "ORDER BY events.seq DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            if context_row is None:
                latest_turn = connection.execute(
                    "SELECT turn_id, agent_model_id FROM turns WHERE thread_id = ? "
                    "ORDER BY created_at DESC, turn_id DESC LIMIT 1",
                    (thread_id,),
                ).fetchone()
                context_model_id = (
                    str(latest_turn["agent_model_id"])
                    if latest_turn is not None
                    else None
                )
                context_turn_id = (
                    str(latest_turn["turn_id"]) if latest_turn is not None else ""
                )
                context_job_id = None
            else:
                context_model_id = str(context_row["agent_model_id"])
                context_turn_id = str(context_row["turn_id"])
                context_job_id = (
                    str(context_row["job_id"]) if context_row["job_id"] else None
                )
            terminal_rows = connection.execute(
                "SELECT status, updated_at FROM turns WHERE updated_at >= ? AND updated_at < ? "
                f"AND status IN ({','.join('?' for _ in TERMINAL_TURN_STATUSES)})",
                (
                    _storage_time(activity_start),
                    _storage_time(activity_end),
                    *(status.value for status in TERMINAL_TURN_STATUSES),
                ),
            ).fetchall()
            waiting = int(
                connection.execute(
                    f"SELECT COUNT(*) AS count FROM turns WHERE status NOT IN "
                    f"({','.join('?' for _ in TERMINAL_TURN_STATUSES)})",
                    tuple(status.value for status in TERMINAL_TURN_STATUSES),
                ).fetchone()["count"]
            )

        for row in rows:
            created_at = _parse_time(row["created_at"])
            if created_at is None:
                continue
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError):
                continue
            usage = _usage_from_payload(
                payload.get("usage") if isinstance(payload, Mapping) else None
            )
            if usage is None:
                continue
            week = week.add(usage)
            if created_at >= day_start:
                today = today.add(usage)

        if context_row is not None:
            try:
                payload = json.loads(str(context_row["payload_json"]))
            except (TypeError, ValueError):
                payload = None
            latest_context_usage = _usage_from_payload(
                payload.get("usage") if isinstance(payload, Mapping) else None
            )
            latest_context_time = _parse_time(context_row["created_at"])

        activity = {
            (activity_start_local + timedelta(days=offset)).date(): [0, 0, 0]
            for offset in range(7)
        }
        for row in terminal_rows:
            updated_at = _parse_time(row["updated_at"])
            if updated_at is None:
                continue
            counts = activity.get(updated_at.astimezone(self._zone).date())
            if counts is None:
                continue
            counts[1] += 1
            status = TurnStatus(str(row["status"]))
            if status is TurnStatus.COMPLETED:
                counts[0] += 1
            elif status is TurnStatus.PARTIAL:
                counts[2] += 1
        today_counts = activity[day_start_local.date()]

        window_tokens: int | None = None
        model_display_name: str | None = None
        model_catalog_snapshot_id: str | None = None
        if context_model_id:
            with self.database.reader() as connection:
                window_tokens, model_display_name, model_catalog_snapshot_id = (
                    self._frozen_model_projection(
                        connection,
                        turn_id=context_turn_id,
                        job_id=context_job_id,
                        model_id=context_model_id,
                        fallback_catalog=self.model_catalog,
                    )
                )
        return ConversationUsageProjection(
            thread_id=thread_id,
            timezone=self.timezone_name,
            today=today.projection(),
            week=week.projection(),
            context=ContextUsageProjection(
                used_tokens=(
                    None
                    if latest_context_usage is None
                    else latest_context_usage.input_tokens
                ),
                window_tokens=window_tokens,
                model_id=context_model_id,
                model_display_name=model_display_name,
                model_catalog_snapshot_id=model_catalog_snapshot_id,
                measured_at=latest_context_time,
            ),
            task_activity=TaskActivityProjection(
                completed_today=today_counts[0],
                partial_today=today_counts[2],
                waiting=waiting,
                terminal_today=today_counts[1],
                days=[
                    TaskActivityDay(
                        date=day,
                        completed=counts[0],
                        partial=counts[2],
                        terminal=counts[1],
                    )
                    for day, counts in activity.items()
                ],
            ),
            data_quality=self._data_quality(),
            calculated_at=now,
        )


__all__ = ["UsageProjectionService"]
