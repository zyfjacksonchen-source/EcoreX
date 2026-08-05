from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from ecorex.runtime import EventStore, RuntimeKernel, RuntimeSettings
from ecorex.runtime.api import _stream_events
from ecorex.observability import RuntimeSignalRegistry


def _append_fact(store: EventStore, thread_id: str, suffix: str) -> None:
    store.append(
        thread_id=thread_id,
        event_type="test.notification",
        payload={"suffix": suffix},
        idempotency_key=f"notification:{suffix}",
    )


def test_event_notification_is_after_commit_shared_and_rollback_safe(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    secondary = EventStore(kernel.database)
    baseline = kernel.events.notification_generation(thread.thread_id)

    with kernel.database.transaction() as connection:
        kernel.events.append_in_transaction(
            connection,
            thread_id=thread.thread_id,
            event_type="test.notification",
            payload={"suffix": "committed"},
            idempotency_key="notification:committed",
        )
        assert kernel.events.notification_generation(thread.thread_id) == baseline

    committed = kernel.events.notification_generation(thread.thread_id)
    assert committed > baseline
    _append_fact(secondary, thread.thread_id, "secondary-store")
    assert kernel.events.notification_generation(thread.thread_id) > committed
    rollback_generation = kernel.events.notification_generation(thread.thread_id)

    with pytest.raises(RuntimeError, match="rollback requested"):
        with kernel.database.transaction() as connection:
            kernel.events.append_in_transaction(
                connection,
                thread_id=thread.thread_id,
                event_type="test.notification",
                payload={"suffix": "rolled-back"},
                idempotency_key="notification:rolled-back",
            )
            raise RuntimeError("rollback requested")

    assert (
        kernel.events.notification_generation(thread.thread_id)
        == rollback_generation
    )
    assert not any(
        event.payload.get("suffix") == "rolled-back"
        for event in kernel.events.page(
            thread.thread_id, after_seq=0, limit=1000
        ).events
    )


def test_event_notification_closes_page_wait_gap_and_wakes_all_local_clients(
    tmp_path,
) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    first = kernel.create_thread()
    second = kernel.create_thread()

    async def scenario() -> None:
        first_generation = kernel.events.notification_generation(first.thread_id)
        second_generation = kernel.events.notification_generation(second.thread_id)

        # Publish before waiter registration. A generation comparison must
        # still complete immediately instead of losing this wakeup.
        await asyncio.to_thread(
            _append_fact,
            kernel.events,
            first.thread_id,
            "between-page-and-wait",
        )
        observed = await asyncio.wait_for(
            kernel.events.wait_for_notification(
                first.thread_id,
                first_generation,
                timeout=10,
            ),
            timeout=0.2,
        )
        assert observed > first_generation

        current = kernel.events.notification_generation(first.thread_id)
        waiters = [
            asyncio.create_task(
                kernel.events.wait_for_notification(
                    first.thread_id,
                    current,
                    timeout=1,
                )
            )
            for _ in range(24)
        ]
        other_thread = asyncio.create_task(
            kernel.events.wait_for_notification(
                second.thread_id,
                second_generation,
                timeout=0.05,
            )
        )
        await asyncio.sleep(0)
        publisher = threading.Thread(
            target=_append_fact,
            args=(kernel.events, first.thread_id, "threaded-publish"),
        )
        publisher.start()
        await asyncio.to_thread(publisher.join)
        results = await asyncio.gather(*waiters)
        assert all(result > current for result in results)
        assert await other_thread == second_generation

        cancelled = asyncio.create_task(
            kernel.events.wait_for_notification(
                first.thread_id,
                results[0],
                timeout=10,
            )
        )
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await asyncio.to_thread(
            _append_fact,
            kernel.events,
            first.thread_id,
            "after-disconnect",
        )

    asyncio.run(scenario())


def test_sse_clients_wait_on_commit_notifications_instead_of_fast_polling(
    tmp_path,
) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    cursor = kernel.events.watermark(thread.thread_id)
    settings = RuntimeSettings(
        database_path=tmp_path / "runtime.db",
        event_notification_fallback_seconds=1.0,
    )
    page_calls = 0
    page_lock = threading.Lock()
    original_page = kernel.events.page

    def counted_page(*args, **kwargs):
        nonlocal page_calls
        with page_lock:
            page_calls += 1
        return original_page(*args, **kwargs)

    kernel.events.page = counted_page  # type: ignore[method-assign]

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    async def scenario() -> None:
        client_count = 16
        streams = [
            _stream_events(
                ConnectedRequest(),
                kernel,
                settings,
                thread.thread_id,
                cursor,
                True,
            )
            for _ in range(client_count)
        ]
        initial = await asyncio.gather(*(anext(stream) for stream in streams))
        assert all("event: watermark" in chunk for chunk in initial)
        waiting = [asyncio.create_task(anext(stream)) for stream in streams]
        await asyncio.sleep(0.3)
        assert page_calls == client_count

        await asyncio.to_thread(
            _append_fact,
            kernel.events,
            thread.thread_id,
            "fanout",
        )
        delivered = await asyncio.wait_for(
            asyncio.gather(*waiting),
            timeout=1,
        )
        assert all("event: test.notification" in chunk for chunk in delivered)
        assert page_calls == client_count * 2
        await asyncio.gather(*(stream.aclose() for stream in streams))

    asyncio.run(scenario())


def test_sse_generator_close_releases_runtime_connection_counter(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    settings = RuntimeSettings(database_path=tmp_path / "runtime.db")
    registry = RuntimeSignalRegistry()

    class ConnectedRequest:
        app = SimpleNamespace(state=SimpleNamespace(runtime_signal_registry=registry))

        async def is_disconnected(self) -> bool:
            return False

    async def scenario() -> None:
        stream = _stream_events(
            ConnectedRequest(), kernel, settings, thread.thread_id, 0, True
        )
        await anext(stream)
        assert registry.snapshot().sse_connections == 1
        await stream.aclose()

    asyncio.run(scenario())
    snapshot = registry.snapshot()
    assert snapshot.sse_connections == 0
    assert snapshot.sse_disconnects == 1


def test_sse_page_to_wait_boundary_cannot_lose_a_committed_event(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    cursor = kernel.events.watermark(thread.thread_id)
    settings = RuntimeSettings(
        database_path=tmp_path / "runtime.db",
        event_notification_fallback_seconds=1.0,
    )
    original_page = kernel.events.page
    injected = False

    def page_then_commit(*args, **kwargs):
        nonlocal injected
        page = original_page(*args, **kwargs)
        if not injected:
            injected = True
            _append_fact(
                kernel.events,
                thread.thread_id,
                "page-wait-boundary",
            )
        return page

    kernel.events.page = page_then_commit  # type: ignore[method-assign]

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    async def scenario() -> None:
        stream = _stream_events(
            ConnectedRequest(),
            kernel,
            settings,
            thread.thread_id,
            cursor,
            True,
        )
        assert "event: watermark" in await anext(stream)
        delivered = await asyncio.wait_for(anext(stream), timeout=0.2)
        assert "event: test.notification" in delivered
        await stream.aclose()

    asyncio.run(scenario())


def test_sse_low_frequency_fallback_reads_cross_process_style_commit(
    tmp_path,
) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    cursor = kernel.events.watermark(thread.thread_id)
    settings = RuntimeSettings(
        database_path=tmp_path / "runtime.db",
        event_notification_fallback_seconds=0.05,
    )

    async def ignore_local_notification(
        thread_id: str,
        observed_generation: int,
        *,
        timeout: float,
    ) -> int:
        del thread_id
        await asyncio.sleep(timeout)
        return observed_generation

    kernel.events.wait_for_notification = ignore_local_notification  # type: ignore[method-assign]

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    async def scenario() -> None:
        stream = _stream_events(
            ConnectedRequest(),
            kernel,
            settings,
            thread.thread_id,
            cursor,
            True,
        )
        assert "event: watermark" in await anext(stream)
        waiting = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.01)
        await asyncio.to_thread(
            _append_fact,
            kernel.events,
            thread.thread_id,
            "fallback",
        )
        delivered = await asyncio.wait_for(waiting, timeout=0.3)
        assert "event: test.notification" in delivered
        await stream.aclose()

    asyncio.run(scenario())
