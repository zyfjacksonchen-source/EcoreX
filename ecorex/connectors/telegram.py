"""Built-in Telegram Bot transport for the product channel boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from typing import Any, Protocol

import httpx

from ecorex import __version__

from .channel_runtime import (
    ChannelInboundMessage,
    ChannelRuntimeDispatcher,
    ChannelTurnTerminalFailure,
    ChannelTurnReceipt,
)
from .channel_self_service import ChannelCredentialOwner
from .models import ConnectorHealth, ConnectorHealthResult


_TOKEN_RE = re.compile(r"^[0-9]{5,20}:[A-Za-z0-9_-]{20,128}$")
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_TELEGRAM_TEXT = 4096


class _HTTPClient(Protocol):
    def post(self, path: str, *, json: Mapping[str, Any]) -> httpx.Response: ...

    def close(self) -> None: ...


class _TelegramFailure(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False, permanent: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain
        self.permanent = permanent


class _TelegramStore:
    """Tenant journal; raw chat IDs stay only in this transport-private 0600 DB."""

    def __init__(self, path: str | os.PathLike[str], owner: ChannelCredentialOwner):
        self.path = Path(os.path.abspath(path))
        self.scope = hashlib.sha256(
            f"{owner.organization_id}\0{owner.account_id}".encode()
        ).hexdigest()
        self._lock = threading.RLock()
        self._initialized = False

    def offset(self) -> int:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT next_offset FROM telegram_offsets WHERE scope = ?",
                (self.scope,),
            ).fetchone()
        return int(row[0]) if row else 0

    def advance(self, next_offset: int) -> None:
        with closing(self._connection()) as connection, connection:
            self._advance(connection, next_offset)

    def add_pending(
        self,
        receipt: ChannelTurnReceipt,
        conversation_id: str,
        next_offset: int,
    ) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO telegram_pending(
                    scope, turn_id, channel_id, thread_id, client_message_id,
                    conversation_sha256, conversation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.scope,
                    receipt.turn_id,
                    receipt.channel_id,
                    receipt.thread_id,
                    receipt.client_message_id,
                    receipt.conversation_sha256,
                    conversation_id,
                ),
            )
            self._advance(connection, next_offset)

    def pending(self) -> tuple[tuple[ChannelTurnReceipt, str], ...]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                """
                SELECT channel_id, thread_id, turn_id, client_message_id,
                       conversation_sha256, conversation_id
                FROM telegram_pending WHERE scope = ? ORDER BY rowid
                """,
                (self.scope,),
            ).fetchall()
        return tuple(
            (
                ChannelTurnReceipt(
                    channel_id=str(row[0]),
                    thread_id=str(row[1]),
                    turn_id=str(row[2]),
                    client_message_id=str(row[3]),
                    conversation_sha256=str(row[4]),
                ),
                str(row[5]),
            )
            for row in rows
        )

    def complete_pending(self, turn_id: str) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "DELETE FROM telegram_pending WHERE scope = ? AND turn_id = ?",
                (self.scope, turn_id),
            )

    def claim_delivery(self, key: str) -> str:
        now = int(time.time())
        with closing(self._connection()) as connection, connection:
            row = connection.execute(
                "SELECT state FROM telegram_deliveries WHERE scope = ? AND delivery_key = ?",
                (self.scope, key),
            ).fetchone()
            if row is not None:
                state = str(row[0])
                if state == "sending":
                    connection.execute(
                        "UPDATE telegram_deliveries SET state = 'uncertain', updated_at = ? "
                        "WHERE scope = ? AND delivery_key = ?",
                        (now, self.scope, key),
                    )
                    return "uncertain"
                return state
            connection.execute(
                "INSERT INTO telegram_deliveries(scope, delivery_key, state, updated_at) "
                "VALUES (?, ?, 'sending', ?)",
                (self.scope, key, now),
            )
        return "send"

    def mark_delivery(self, key: str, state: str) -> None:
        if state not in {"sent", "uncertain"}:
            raise ValueError("telegram delivery state is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE telegram_deliveries SET state = ?, updated_at = ? "
                "WHERE scope = ? AND delivery_key = ?",
                (state, int(time.time()), self.scope, key),
            )

    def release_delivery(self, key: str) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "DELETE FROM telegram_deliveries "
                "WHERE scope = ? AND delivery_key = ? AND state = 'sending'",
                (self.scope, key),
            )

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            if self.path.exists() and stat.S_ISLNK(self.path.lstat().st_mode):
                raise RuntimeError("telegram state path is invalid")
            connection = sqlite3.connect(self.path, timeout=5)
            connection.execute("PRAGMA busy_timeout = 5000")
            if not self._initialized:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS telegram_offsets(
                        scope TEXT PRIMARY KEY,
                        next_offset INTEGER NOT NULL CHECK(next_offset >= 0)
                    );
                    CREATE TABLE IF NOT EXISTS telegram_pending(
                        scope TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        client_message_id TEXT NOT NULL,
                        conversation_sha256 TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        PRIMARY KEY(scope, turn_id)
                    );
                    CREATE TABLE IF NOT EXISTS telegram_deliveries(
                        scope TEXT NOT NULL,
                        delivery_key TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('sending','sent','uncertain')),
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(scope, delivery_key)
                    );
                    """
                )
                connection.commit()
                os.chmod(self.path, 0o600)
                self._initialized = True
            return connection

    def _advance(self, connection: sqlite3.Connection, next_offset: int) -> None:
        if not isinstance(next_offset, int) or isinstance(next_offset, bool) or next_offset < 0:
            raise ValueError("telegram offset is invalid")
        connection.execute(
            """
            INSERT INTO telegram_offsets(scope, next_offset) VALUES (?, ?)
            ON CONFLICT(scope) DO UPDATE SET next_offset = MAX(next_offset, excluded.next_offset)
            """,
            (self.scope, next_offset),
        )


class TelegramBotAdapter:
    """One Telegram long-polling worker; no listener or second Runtime."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        client_factory: Callable[[str], _HTTPClient] | None = None,
        poll_seconds: int = 2,
    ) -> None:
        if not 1 <= poll_seconds <= 2:
            raise ValueError("telegram poll interval is invalid")
        self.database_path = Path(os.path.abspath(database_path))
        self.client_factory = client_factory or self._default_client
        self.poll_seconds = poll_seconds
        self._owner: ChannelCredentialOwner | None = None
        self._dispatcher: ChannelRuntimeDispatcher | None = None
        self._store: _TelegramStore | None = None
        self._client: _HTTPClient | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._health = ConnectorHealth.DISABLED
        self._last_error: str | None = None

    def bind_runtime(
        self,
        owner: ChannelCredentialOwner,
        dispatcher: ChannelRuntimeDispatcher,
    ) -> None:
        with self._lock:
            if self._dispatcher is not None and (
                self._owner != owner or self._dispatcher is not dispatcher
            ):
                raise RuntimeError("telegram Runtime is already bound")
            self._owner = owner
            self._dispatcher = dispatcher
            self._store = _TelegramStore(self.database_path, owner)

    def test(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        try:
            token = _token(config)
            client = self.client_factory(token)
            try:
                self._preflight(client)
            finally:
                _close(client)
            return ConnectorHealthResult(ConnectorHealth.CONNECTED)
        except _TelegramFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "telegram_transport_unavailable"
            )

    def start(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        try:
            token = _token(config)
            with self._lock:
                if self._dispatcher is None or self._store is None:
                    return ConnectorHealthResult(
                        ConnectorHealth.ERROR, "telegram_runtime_unavailable"
                    )
                if self._thread is not None and self._thread.is_alive():
                    return ConnectorHealthResult(self._health, self._last_error)
                client = self.client_factory(token)
                try:
                    self._preflight(client)
                    self._set_product_name(client)
                except Exception:
                    _close(client)
                    raise
                self._client = client
                self._stop_event = threading.Event()
                self._health = ConnectorHealth.CONNECTED
                self._last_error = None
                self._thread = threading.Thread(
                    target=self._run,
                    args=(client,),
                    name="emate-telegram-channel",
                    daemon=True,
                )
                self._thread.start()
            return ConnectorHealthResult(ConnectorHealth.CONNECTED)
        except _TelegramFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "telegram_transport_unavailable"
            )

    def health(self) -> ConnectorHealthResult:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return ConnectorHealthResult(self._health, self._last_error)
            return ConnectorHealthResult(
                self._health if self._last_error else ConnectorHealth.DISABLED,
                self._last_error,
            )

    def stop(self, timeout_seconds: float) -> bool:
        with self._lock:
            thread = self._thread
            self._stop_event.set()
        if thread is not None:
            thread.join(timeout_seconds)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                self._thread = None
                self._client = None
                self._health = ConnectorHealth.DISABLED
                self._last_error = None
        return stopped

    def send_text(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        text: str,
        idempotency_key: str,
    ) -> None:
        if channel_id != "telegram" or not isinstance(text, str) or not text:
            raise ValueError("telegram delivery is invalid")
        try:
            chat_id = int(conversation_id)
        except (TypeError, ValueError):
            raise ValueError("telegram conversation is invalid") from None
        with self._lock:
            client = self._client
            store = self._store
        if client is None or store is None:
            raise _TelegramFailure("telegram_not_running")
        chunks = _chunks(text)
        for index, chunk in enumerate(chunks):
            key = f"{idempotency_key}:{index + 1}:{len(chunks)}"
            state = store.claim_delivery(key)
            if state == "sent":
                continue
            if state == "uncertain":
                raise _TelegramFailure("telegram_delivery_uncertain", uncertain=True)
            try:
                payload = self._request(
                    client,
                    "sendMessage",
                    {"chat_id": chat_id, "text": chunk},
                    delivery=True,
                )
                result = payload.get("result")
                if not isinstance(result, dict) or not isinstance(
                    result.get("message_id"), int
                ):
                    raise _TelegramFailure(
                        "telegram_delivery_uncertain", uncertain=True
                    )
            except _TelegramFailure as error:
                if error.uncertain:
                    store.mark_delivery(key, "uncertain")
                else:
                    store.release_delivery(key)
                raise
            store.mark_delivery(key, "sent")

    def _run(self, client: _HTTPClient) -> None:
        backoff = 1.0
        try:
            while not self._stop_event.is_set():
                try:
                    self._drain_pending()
                    offset = self._required_store().offset()
                    for update in self._updates(client, offset):
                        self._accept_update(update)
                    self._drain_pending()
                    self._set_health(ConnectorHealth.CONNECTED, None)
                    backoff = 1.0
                except _TelegramFailure as error:
                    self._set_health(
                        ConnectorHealth.DEGRADED
                        if error.uncertain
                        else ConnectorHealth.ERROR,
                        error.code,
                    )
                    if error.permanent:
                        return
                    self._stop_event.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                except Exception:
                    self._set_health(
                        ConnectorHealth.ERROR, "telegram_runtime_dispatch_failed"
                    )
                    self._stop_event.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
        finally:
            _close(client)
            with self._lock:
                if self._client is client:
                    self._client = None

    def _accept_update(self, update: Mapping[str, Any]) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
            raise _TelegramFailure("telegram_provider_response_invalid")
        next_offset = update_id + 1
        message = update.get("message")
        if not isinstance(message, dict):
            self._required_store().advance(next_offset)
            return
        message_id = message.get("message_id")
        chat = message.get("chat")
        text = message.get("text")
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or not isinstance(chat, dict)
            or not isinstance(chat.get("id"), int)
            or not isinstance(text, str)
            or not text.strip()
        ):
            self._required_store().advance(next_offset)
            return
        conversation_id = str(chat["id"])
        dispatcher = self._required_dispatcher()
        receipt = dispatcher.dispatch(
            ChannelInboundMessage(
                channel_id="telegram",
                conversation_id=conversation_id,
                message_id=str(message_id),
                text=text,
            )
        )
        self._required_store().add_pending(receipt, conversation_id, next_offset)

    def _drain_pending(self) -> None:
        dispatcher = self._required_dispatcher()
        store = self._required_store()
        for receipt, conversation_id in store.pending():
            try:
                delivered = dispatcher.deliver(
                    receipt,
                    conversation_id=conversation_id,
                    transport=self,
                )
            except ChannelTurnTerminalFailure:
                store.complete_pending(receipt.turn_id)
                continue
            if delivered:
                store.complete_pending(receipt.turn_id)

    def _updates(self, client: _HTTPClient, offset: int) -> tuple[Mapping[str, Any], ...]:
        payload = self._request(
            client,
            "getUpdates",
            {
                "offset": offset,
                "timeout": self.poll_seconds,
                "allowed_updates": ["message"],
            },
        )
        result = payload.get("result")
        if not isinstance(result, list) or len(result) > 100:
            raise _TelegramFailure("telegram_provider_response_invalid")
        if any(not isinstance(item, dict) for item in result):
            raise _TelegramFailure("telegram_provider_response_invalid")
        return tuple(result)

    def _get_me(self, client: _HTTPClient) -> None:
        result = self._request(client, "getMe", {}).get("result")
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("id"), int)
            or result.get("is_bot") is not True
        ):
            raise _TelegramFailure("telegram_provider_response_invalid")

    def _preflight(self, client: _HTTPClient) -> None:
        self._get_me(client)
        result = self._request(client, "getWebhookInfo", {}).get("result")
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            raise _TelegramFailure("telegram_provider_response_invalid")
        if result["url"]:
            raise _TelegramFailure("telegram_webhook_active", permanent=True)

    def _set_product_name(self, client: _HTTPClient) -> None:
        if self._request(client, "setMyName", {"name": "e-Mate"}).get("result") is not True:
            raise _TelegramFailure("telegram_provider_response_invalid")

    def _request(
        self,
        client: _HTTPClient,
        path: str,
        body: Mapping[str, Any],
        *,
        delivery: bool = False,
    ) -> Mapping[str, Any]:
        try:
            response = client.post(path, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise _TelegramFailure("telegram_transport_unavailable") from None
        except (httpx.TimeoutException, httpx.TransportError):
            raise _TelegramFailure(
                "telegram_delivery_uncertain" if delivery else "telegram_transport_unavailable",
                uncertain=delivery,
            ) from None
        except Exception:
            raise _TelegramFailure(
                "telegram_delivery_uncertain" if delivery else "telegram_transport_unavailable",
                uncertain=delivery,
            ) from None
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise _TelegramFailure(
                "telegram_delivery_uncertain" if delivery else "telegram_provider_response_invalid",
                uncertain=delivery,
            )
        if response.status_code in {401, 403}:
            raise _TelegramFailure("telegram_auth_rejected", permanent=True)
        if response.status_code >= 500:
            raise _TelegramFailure(
                "telegram_delivery_uncertain" if delivery else "telegram_transport_unavailable",
                uncertain=delivery,
            )
        if response.status_code != 200:
            raise _TelegramFailure("telegram_provider_rejected")
        try:
            payload = response.json()
        except ValueError:
            raise _TelegramFailure(
                "telegram_delivery_uncertain" if delivery else "telegram_provider_response_invalid",
                uncertain=delivery,
            ) from None
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            error_code = payload.get("error_code") if isinstance(payload, dict) else None
            if error_code in {401, 403}:
                raise _TelegramFailure("telegram_auth_rejected", permanent=True)
            raise _TelegramFailure("telegram_provider_rejected")
        return payload

    def _set_health(self, health: ConnectorHealth, error: str | None) -> None:
        with self._lock:
            self._health = health
            self._last_error = error

    def _required_dispatcher(self) -> ChannelRuntimeDispatcher:
        with self._lock:
            if self._dispatcher is None:
                raise _TelegramFailure("telegram_runtime_unavailable", permanent=True)
            return self._dispatcher

    def _required_store(self) -> _TelegramStore:
        with self._lock:
            if self._store is None:
                raise _TelegramFailure("telegram_runtime_unavailable", permanent=True)
            return self._store

    @staticmethod
    def _default_client(token: str) -> httpx.Client:
        return httpx.Client(
            base_url=f"https://api.telegram.org/bot{token}/",
            # The synchronous long poll must always unwind inside the Runtime's
            # five-second channel shutdown fence.
            timeout=httpx.Timeout(connect=4, read=4, write=4, pool=4),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
            headers={"User-Agent": f"e-Mate/{__version__} TelegramChannel/1"},
        )


def _token(config: Mapping[str, Any]) -> str:
    if not isinstance(config, Mapping) or set(config) != {"telegram_token"}:
        raise _TelegramFailure("telegram_configuration_invalid", permanent=True)
    token = config.get("telegram_token")
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        raise _TelegramFailure("telegram_configuration_invalid", permanent=True)
    return token


def _chunks(text: str) -> tuple[str, ...]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        split = min(len(remaining), _MAX_TELEGRAM_TEXT)
        if split < len(remaining):
            newline = remaining.rfind("\n", _MAX_TELEGRAM_TEXT // 2, split)
            if newline >= 0:
                split = newline + 1
        chunks.append(remaining[:split])
        remaining = remaining[split:]
    return tuple(chunks)


def _close(client: _HTTPClient) -> None:
    try:
        client.close()
    except Exception:
        pass


__all__ = ["TelegramBotAdapter"]
