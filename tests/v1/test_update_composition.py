from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.bootstrap import DelayedRestartRequester
from ecorex.update import (
    ProductUpdateSettings,
    ReleaseChannel,
    RuntimeUpdateService,
    UpdateServiceError,
    build_product_update_composition,
)


class Credentials:
    def bearer_token(self) -> str:
        return "control-plane-token-1234567890"


class ObservableCondition(threading.Condition):
    def __init__(self, waiting: threading.Event) -> None:
        super().__init__()
        self.waiting = waiting

    def wait(self, timeout: float | None = None) -> bool:
        self.waiting.set()
        return super().wait(timeout)


def _settings(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    rollback_public_key = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return ProductUpdateSettings(
        database_path=tmp_path / "runtime.db",
        install_root=tmp_path / "install",
        release_feed_endpoint="https://control.example/api/v1/releases/latest",
        update_signal_endpoint="wss://control.example/api/v1/client/updates/ws",
        trusted_public_keys={"release-key": public_key},
        rollback_public_keys={"rollback-key": rollback_public_key},
        credentials=Credentials(),
        control_plane_hosts=frozenset({"control.example"}),
        artifact_hosts=frozenset(
            {"mirror.example", "github.example", "cdn.example"}
        ),
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        health_checker=lambda _slot: True,
        drainer=lambda: True,
        migration_dry_run=lambda _slot: True,
    )


def test_product_composition_wires_real_trust_transport_install_and_restart(tmp_path) -> None:
    composition = build_product_update_composition(_settings(tmp_path))

    assert composition.service.coordinator is composition.coordinator
    assert composition.coordinator.fetcher is composition.fetcher
    assert composition.service.feed is composition.feed
    assert composition.service.signal_source is composition.signal_source
    assert composition.service.restart_requester == composition.restart_requester.request
    assert isinstance(composition.restart_requester, DelayedRestartRequester)
    assert composition.service.artifact_id == "core-windows-x64"
    assert composition.signal_source.url.endswith(
        "channel=stable&platform=windows&architecture=x64&current_version=1.0.0"
    )
    assert composition.feed.client.is_closed is False
    assert composition.fetcher.client.is_closed is False

    asyncio.run(composition.stop())

    assert composition.feed.client.is_closed is True
    assert composition.fetcher.client.is_closed is True
    assert composition.signal_source._closed is True
    asyncio.run(composition.stop())
    with pytest.raises(UpdateServiceError, match="closed"):
        asyncio.run(composition.start())


def test_product_lifecycle_readiness_waits_for_update_identity_reader(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix the update-poll/readiness race seen in the full product lifespan."""

    composition = build_product_update_composition(_settings(tmp_path))
    coordinator = composition.coordinator
    identity_provider = composition.feed.current_identity_provider
    assert identity_provider is not None
    assert coordinator.lock.timeout is None

    identity_holds_lock = threading.Event()
    release_identity = threading.Event()
    readiness_waiting = threading.Event()
    readiness_finished = threading.Event()
    failures: list[BaseException] = []
    identities: list[object] = []
    readiness_results: list[object] = []
    coordinator.lock._guard = ObservableCondition(readiness_waiting)
    original_pointers = coordinator.slots.pointers

    def blocked_pointers():
        identity_holds_lock.set()
        assert release_identity.wait(timeout=5.0)
        return original_pointers()

    monkeypatch.setattr(coordinator.slots, "pointers", blocked_pointers)

    def read_current_identity() -> None:
        try:
            identities.append(identity_provider())
        except BaseException as error:
            failures.append(error)

    def record_runtime_readiness() -> None:
        try:
            readiness_results.append(coordinator.mark_runtime_ready(None))
        except BaseException as error:
            failures.append(error)
        finally:
            readiness_finished.set()

    identity_thread = threading.Thread(
        target=read_current_identity,
        name="product-update-current-identity",
    )
    readiness_thread = threading.Thread(
        target=record_runtime_readiness,
        name="product-runtime-ready-recorder",
    )
    identity_thread.start()
    try:
        assert identity_holds_lock.wait(timeout=5.0)
        readiness_thread.start()
        assert readiness_waiting.wait(timeout=5.0)
        assert readiness_finished.is_set() is False
        release_identity.set()
        identity_thread.join(timeout=5.0)
        readiness_thread.join(timeout=5.0)
    finally:
        release_identity.set()
        identity_thread.join(timeout=5.0)
        if readiness_thread.ident is not None:
            readiness_thread.join(timeout=5.0)
        asyncio.run(composition.stop())

    assert identity_thread.is_alive() is False
    assert readiness_thread.is_alive() is False
    assert failures == []
    assert identities == [None]
    assert readiness_results == [False]


def _update_rows(path) -> tuple[tuple[str, tuple[tuple, ...]], ...]:
    with sqlite3.connect(path) as connection:
        return tuple(
            (
                table,
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    ).fetchall()
                ),
            )
            for table in (
                "runtime_update_activation_requests",
                "runtime_update_events",
                "runtime_update_signals",
                "runtime_update_state",
            )
        )


def _tree(root) -> tuple[str, ...]:
    if not root.exists():
        return ()
    return tuple(
        sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    )


def test_product_update_projection_only_build_then_converges_once(tmp_path) -> None:
    from ecorex.runtime import SQLiteDatabase

    settings = _settings(tmp_path)
    SQLiteDatabase(settings.database_path)
    before_rows = _update_rows(settings.database_path)
    before_tree = _tree(settings.install_root)

    composition = build_product_update_composition(
        settings,
        initialize=False,
        create_storage=False,
    )
    try:
        assert composition.service.startup_converged is False
        assert composition.service.snapshot().state == "idle"
        assert _update_rows(settings.database_path) == before_rows
        assert _tree(settings.install_root) == before_tree

        composition.service.converge_startup()
        assert composition.service.startup_converged is True
        assert (settings.install_root / "slots").is_dir()
        assert (settings.install_root / "transactions").is_dir()
        converged_rows = _update_rows(settings.database_path)
        converged_tree = _tree(settings.install_root)
        assert converged_rows != before_rows

        composition.service.converge_startup()
        assert _update_rows(settings.database_path) == converged_rows
        assert _tree(settings.install_root) == converged_tree
    finally:
        asyncio.run(composition.stop())


class CloseTracker:
    def __init__(self, *, asynchronous: bool = False) -> None:
        self.calls = 0
        self.asynchronous = asynchronous

    def close(self):
        self.calls += 1
        if self.asynchronous:
            return self._close_async()
        return None

    async def _close_async(self):
        await asyncio.sleep(0)


class Coordinator:
    def __init__(self, fetcher) -> None:
        self.fetcher = fetcher


class RecoverableCoordinator(Coordinator):
    latest_state = None

    def recover(self):
        return None


class BlockingFeed(CloseTracker):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def latest(self, **_kwargs):
        self.entered.set()
        assert self.release.wait(timeout=5)
        return None


def test_runtime_update_stop_closes_feed_signal_and_coordinator_fetcher_once(tmp_path) -> None:
    feed = CloseTracker()
    signal_source = CloseTracker(asynchronous=True)
    fetcher = CloseTracker()
    service = RuntimeUpdateService(
        tmp_path / "close.db",
        coordinator=Coordinator(fetcher),
        feed=feed,
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        signal_source=signal_source,
        restart_requester=lambda _transaction_id: None,
    )

    asyncio.run(service.stop())
    asyncio.run(service.stop())

    assert feed.calls == 1
    assert signal_source.calls == 1
    assert fetcher.calls == 1


def test_stop_waits_for_inflight_feed_before_closing_owned_resources(tmp_path) -> None:
    feed = BlockingFeed()
    fetcher = CloseTracker()
    coordinator = RecoverableCoordinator(fetcher)
    service = RuntimeUpdateService(
        tmp_path / "blocking-close.db",
        coordinator=coordinator,
        feed=feed,
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        restart_requester=lambda _transaction_id: None,
        poll_interval_seconds=5,
    )

    async def exercise():
        await service.start()
        assert await asyncio.to_thread(feed.entered.wait, 2)
        stopping = asyncio.create_task(service.stop())
        await asyncio.sleep(0.05)
        assert not stopping.done()
        assert feed.calls == 0
        feed.release.set()
        await asyncio.wait_for(stopping, timeout=2)

    asyncio.run(exercise())

    assert feed.calls == 1
    assert fetcher.calls == 1


def test_product_settings_reject_noncanonical_or_untrusted_configuration(tmp_path) -> None:
    base = _settings(tmp_path)
    with pytest.raises(ValueError, match="canonical"):
        ProductUpdateSettings(
            **{
                name: getattr(base, name)
                for name in base.__dataclass_fields__
                if name != "artifact_id"
            },
            artifact_id="custom-core",
        )
    with pytest.raises(ValueError, match="signing key"):
        ProductUpdateSettings(
            **{
                name: getattr(base, name)
                for name in base.__dataclass_fields__
                if name not in {"trusted_public_keys", "artifact_id"}
            },
            trusted_public_keys={},
        )
