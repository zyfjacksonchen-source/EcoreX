"""Durable, backend-authoritative reasoning-summary Item lifecycle."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ecorex.protocol import (
    ItemKind,
    ItemProjection,
    ItemStatus,
    ReasoningItemContent,
    ReasoningPresentation,
    TERMINAL_TURN_STATUSES,
    TurnStatus,
)

from .database import SQLiteDatabase, json_dumps, json_loads
from .errors import ConflictError, NotFoundError
from .event_store import EventStore
from .ids import new_id


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _store_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _read_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _projection(row: sqlite3.Row) -> ItemProjection:
    return ItemProjection(
        item_id=row["item_id"],
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        kind=ItemKind(row["kind"]),
        status=ItemStatus(row["status"]),
        content=json_loads(row["content_json"], {}),
        created_at=_read_time(row["created_at"]),
        updated_at=_read_time(row["updated_at"]),
    )


def _content(row: sqlite3.Row) -> ReasoningItemContent:
    try:
        return ReasoningItemContent.model_validate(json_loads(row["content_json"], {}))
    except ValueError as error:
        raise ConflictError("reasoning Item content violates its durable contract") from error


def _visible_rows(
    connection: sqlite3.Connection, *, turn_id: str
) -> list[tuple[sqlite3.Row, ReasoningItemContent]]:
    rows = connection.execute(
        "SELECT * FROM items WHERE turn_id = ? AND kind = ? "
        "ORDER BY created_at, item_id",
        (turn_id, ItemKind.REASONING.value),
    ).fetchall()
    visible = [
        (row, content)
        for row in rows
        if (content := _content(row)).presentation is ReasoningPresentation.VISIBLE
    ]
    if len(visible) > 1:
        raise ConflictError("a Turn cannot expose more than one reasoning Item")
    return visible


def archive_visible_reasoning_in_transaction(
    connection: sqlite3.Connection,
    events: EventStore,
    *,
    thread_id: str,
    turn_id: str,
    terminal_event_id: str,
    terminal_status: TurnStatus,
    reason: str | None,
    now: datetime,
) -> None:
    """Collapse the visible summary after the matching terminal Turn fact.

    The terminal event is part of the user-visible ordering contract, not just
    an implementation detail.  Requiring its durable identity here prevents a
    future terminal path from making the summary disappear while the client
    still observes an active Turn.
    """

    if terminal_status not in TERMINAL_TURN_STATUSES:
        raise ValueError("reasoning terminal archive requires a terminal Turn status")
    terminal_event = connection.execute(
        "SELECT thread_id, turn_id, event_type, payload_json FROM events "
        "WHERE event_id = ?",
        (terminal_event_id,),
    ).fetchone()
    terminal_payload = (
        {}
        if terminal_event is None
        else json_loads(terminal_event["payload_json"], {})
    )
    if (
        terminal_event is None
        or terminal_event["thread_id"] != thread_id
        or terminal_event["turn_id"] != turn_id
        or terminal_event["event_type"] != "turn.status_changed"
        or terminal_payload.get("to") != terminal_status.value
    ):
        raise ConflictError(
            "reasoning terminal archive requires its preceding terminal Turn fact"
        )

    visible = _visible_rows(connection, turn_id=turn_id)
    if not visible:
        return
    row, current = visible[0]
    revision = current.revision + 1
    archived_reason = reason or terminal_status.value
    updated = current.model_copy(
        update={
            "revision": revision,
            "presentation": ReasoningPresentation.COLLAPSED,
            "archived_reason": archived_reason,
        }
    )
    events.append_in_transaction(
        connection,
        thread_id=thread_id,
        turn_id=turn_id,
        item_id=row["item_id"],
        event_type="reasoning.archived",
        payload={
            "revision": revision,
            "presentation": ReasoningPresentation.COLLAPSED.value,
            "reason": archived_reason,
            "terminal_status": terminal_status.value,
        },
        causation_id=terminal_event_id,
        idempotency_key=(
            f"{row['item_id']}:reasoning-terminal:{terminal_status.value}"
        ),
        created_at=now,
    )
    connection.execute(
        "UPDATE items SET content_json = ?, updated_at = ? WHERE item_id = ?",
        (
            json_dumps(updated.model_dump(mode="json")),
            _store_time(now),
            row["item_id"],
        ),
    )


class ReasoningItemStore:
    """Owns reasoning atom identity, revision and atomic replacement."""

    def __init__(self, database: SQLiteDatabase, events: EventStore):
        self.database = database
        self.events = events

    def apply_delta(
        self,
        *,
        turn_id: str,
        atom_id: str,
        delta: str,
        idempotency_key: str,
    ) -> ItemProjection:
        if not atom_id or len(atom_id) > 256:
            raise ValueError("reasoning atom_id is invalid")
        if not delta or not delta.strip():
            raise ValueError("the first-class reasoning delta must contain visible text")
        if len(delta) > 1_000_000:
            raise ValueError("reasoning delta is too large")
        if not idempotency_key:
            raise ValueError("reasoning delta idempotency_key is required")

        now = _utc_now()
        with self.database.transaction() as connection:
            turn = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if turn is None:
                raise NotFoundError(f"turn {turn_id!r} does not exist")
            if TurnStatus(turn["status"]) in TERMINAL_TURN_STATUSES:
                raise ConflictError("terminal turns cannot accept reasoning deltas")

            duplicate = connection.execute(
                "SELECT * FROM events WHERE thread_id = ? AND idempotency_key = ?",
                (turn["thread_id"], idempotency_key),
            ).fetchone()
            if duplicate is not None:
                payload = json_loads(duplicate["payload_json"], {})
                if (
                    duplicate["event_type"] not in {"reasoning.replaced", "reasoning.delta"}
                    or payload.get("atom_id") != atom_id
                    or payload.get("delta") != delta
                    or duplicate["item_id"] is None
                ):
                    raise ConflictError(
                        "reasoning idempotency key was reused with different content"
                    )
                row = connection.execute(
                    "SELECT * FROM items WHERE item_id = ?", (duplicate["item_id"],)
                ).fetchone()
                if row is None:
                    raise ConflictError("reasoning event has no durable Item")
                return _projection(row)

            visible = _visible_rows(connection, turn_id=turn_id)
            if visible and visible[0][1].atom_id == atom_id:
                row, current = visible[0]
                text = current.text + delta
                if len(text) > 1_000_000:
                    raise ConflictError("reasoning summary exceeded its durable size limit")
                revision = current.revision + 1
                updated = current.model_copy(update={"text": text, "revision": revision})
                self.events.append_in_transaction(
                    connection,
                    thread_id=turn["thread_id"],
                    turn_id=turn_id,
                    item_id=row["item_id"],
                    event_type="reasoning.delta",
                    payload={"atom_id": atom_id, "delta": delta, "revision": revision},
                    idempotency_key=idempotency_key,
                    created_at=now,
                )
                connection.execute(
                    "UPDATE items SET content_json = ?, updated_at = ? WHERE item_id = ?",
                    (
                        json_dumps(updated.model_dump(mode="json")),
                        _store_time(now),
                        row["item_id"],
                    ),
                )
                result = connection.execute(
                    "SELECT * FROM items WHERE item_id = ?", (row["item_id"],)
                ).fetchone()
                return _projection(result)

            previous_item_id: str | None = None
            previous_revision: int | None = None
            if visible:
                previous, prior_content = visible[0]
                previous_item_id = str(previous["item_id"])
                previous_revision = prior_content.revision + 1
                archived = prior_content.model_copy(
                    update={
                        "revision": previous_revision,
                        "presentation": ReasoningPresentation.ARCHIVED,
                        "archived_reason": "replaced_by_next_atom",
                    }
                )
                connection.execute(
                    "UPDATE items SET status = ?, content_json = ?, updated_at = ? "
                    "WHERE item_id = ?",
                    (
                        ItemStatus.COMPLETED.value,
                        json_dumps(archived.model_dump(mode="json")),
                        _store_time(now),
                        previous_item_id,
                    ),
                )

            item_id = new_id("itm")
            created = ReasoningItemContent(
                atom_id=atom_id,
                text=delta,
                revision=1,
                presentation=ReasoningPresentation.VISIBLE,
            )
            self.events.append_in_transaction(
                connection,
                thread_id=turn["thread_id"],
                turn_id=turn_id,
                item_id=item_id,
                event_type="reasoning.replaced",
                payload={
                    "atom_id": atom_id,
                    "delta": delta,
                    "revision": 1,
                    "presentation": ReasoningPresentation.VISIBLE.value,
                    "previous_item_id": previous_item_id,
                    "previous_revision": previous_revision,
                    "previous_presentation": (
                        ReasoningPresentation.ARCHIVED.value
                        if previous_item_id is not None
                        else None
                    ),
                },
                idempotency_key=idempotency_key,
                created_at=now,
            )
            timestamp = _store_time(now)
            connection.execute(
                """
                INSERT INTO items(
                    item_id, thread_id, turn_id, kind, status, content_json,
                    client_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    item_id,
                    turn["thread_id"],
                    turn_id,
                    ItemKind.REASONING.value,
                    ItemStatus.IN_PROGRESS.value,
                    json_dumps(created.model_dump(mode="json")),
                    timestamp,
                    timestamp,
                ),
            )
            result = connection.execute(
                "SELECT * FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
            return _projection(result)
