from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from ecorex.runtime import EventStore
from ecorex.runtime.errors import IdempotencyConflictError


def test_events_are_monotonic_cursor_safe_and_append_only(tmp_path):
    store = EventStore(tmp_path / "events.db")

    def append(index: int):
        return store.append(
            thread_id="thread-a",
            event_type="test.fact",
            payload={"index": index},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(80)))

    first = store.page("thread-a", after_seq=0, limit=31)
    second = store.page(
        "thread-a", after_seq=first.events[-1].seq, limit=100
    )
    events = first.events + second.events
    assert [event.seq for event in events] == list(range(1, 81))
    assert first.has_more is True
    assert second.has_more is False
    assert first.watermark == second.watermark == store.watermark("thread-a") == 80
    assert all(event.created_at.utcoffset().total_seconds() == 0 for event in events)

    with store.database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE events SET event_type = 'tampered' WHERE thread_id = 'thread-a'"
            )
    with store.database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events WHERE thread_id = 'thread-a'")


def test_event_idempotency_returns_same_fact_and_rejects_reuse(tmp_path):
    store = EventStore(tmp_path / "events.db")
    first = store.append(
        thread_id="thread-a",
        event_type="turn.accepted",
        payload={"value": 1},
        idempotency_key="accept-once",
    )
    duplicate = store.append(
        thread_id="thread-a",
        event_type="turn.accepted",
        payload={"value": 1},
        idempotency_key="accept-once",
    )
    assert duplicate == first
    assert store.watermark("thread-a") == 1

    with pytest.raises(IdempotencyConflictError):
        store.append(
            thread_id="thread-a",
            event_type="turn.accepted",
            payload={"value": 2},
            idempotency_key="accept-once",
        )
