"""WeCom AI Bot WebSocket transport for the product Runtime.

The wire frames follow WeCom's public AI Bot SDK contract.  This adapter opens
only the outbound long connection and projects messages into the existing
``ChannelRuntimeDispatcher``; it never owns an Agent Runtime or HTTP listener.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from typing import Any, Protocol

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as websocket_connect

from .channel_runtime import (
    ChannelInboundMessage,
    ChannelRuntimeDispatcher,
    ChannelTurnTerminalFailure,
    ChannelTurnReceipt,
)
from .channel_self_service import ChannelCredentialOwner
from .models import ConnectorHealth, ConnectorHealthResult


_WS_URL = "wss://openws.work.weixin.qq.com"
_CALLBACK = "aibot_msg_callback"
_EVENT_CALLBACK = "aibot_event_callback"
_SUBSCRIBE = "aibot_subscribe"
_HEARTBEAT = "ping"
_SEND_MESSAGE = "aibot_send_msg"
_MAX_FRAME_BYTES = 1024 * 1024
_MAX_TEXT_BYTES = 20_000
_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")
_ERROR_RE = re.compile(r"^wecom_bot_[a-z0-9_]{1,124}$")


class _Socket(Protocol):
    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def send(self, message: str) -> None: ...

    def close(self, code: int = 1000, reason: str = "") -> None: ...


class _WeComFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        uncertain: bool = False,
        permanent: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class _JournalEvent:
    event_key: str
    conversation_id: str
    message_id: str
    text: str


class _WeComStore:
    """Tenant journal; provider identifiers stay in this private database."""

    def __init__(self, path: str | os.PathLike[str], owner: ChannelCredentialOwner):
        self.path = Path(os.path.abspath(path))
        self.scope = hashlib.sha256(
            f"{owner.organization_id}\0{owner.account_id}".encode("utf-8")
        ).hexdigest()
        self._lock = threading.RLock()
        self._initialized = False

    def record(self, *, conversation_id: str, message_id: str, text: str) -> None:
        event_key = hashlib.sha256(
            f"{conversation_id}\0{message_id}".encode("utf-8")
        ).hexdigest()
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO wecom_bot_events(
                    scope, event_key, conversation_id, message_id, text, state
                ) VALUES (?, ?, ?, ?, ?, 'received')
                """,
                (self.scope, event_key, conversation_id, message_id, text),
            )

    def received(self) -> tuple[_JournalEvent, ...]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                """
                SELECT event_key, conversation_id, message_id, text
                FROM wecom_bot_events
                WHERE scope = ? AND state = 'received'
                ORDER BY rowid
                """,
                (self.scope,),
            ).fetchall()
        return tuple(_JournalEvent(*(str(value) for value in row)) for row in rows)

    def set_outbound(self, event_key: str, receipt: ChannelTurnReceipt) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                UPDATE wecom_bot_events
                SET state = 'outbound', channel_id = ?, thread_id = ?, turn_id = ?,
                    client_message_id = ?, conversation_sha256 = ?
                WHERE scope = ? AND event_key = ? AND state = 'received'
                """,
                (
                    receipt.channel_id,
                    receipt.thread_id,
                    receipt.turn_id,
                    receipt.client_message_id,
                    receipt.conversation_sha256,
                    self.scope,
                    event_key,
                ),
            )

    def outbound(self) -> tuple[tuple[ChannelTurnReceipt, str], ...]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                """
                SELECT channel_id, thread_id, turn_id, client_message_id,
                       conversation_sha256, conversation_id
                FROM wecom_bot_events
                WHERE scope = ? AND state = 'outbound'
                ORDER BY rowid
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

    def finish(
        self, turn_id: str, state: str, error_code: str | None = None
    ) -> None:
        if state not in {"completed", "failed", "uncertain"}:
            raise ValueError("WeCom event terminal state is invalid")
        if (state == "completed") != (error_code is None) or (
            error_code is not None and _ERROR_RE.fullmatch(error_code) is None
        ):
            raise ValueError("WeCom event terminal error is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                UPDATE wecom_bot_events
                SET state = ?, error_code = ?, conversation_id = '', message_id = '', text = '',
                    channel_id = NULL, thread_id = NULL, turn_id = NULL,
                    client_message_id = NULL, conversation_sha256 = NULL
                WHERE scope = ? AND turn_id = ? AND state = 'outbound'
                """,
                (state, error_code, self.scope, turn_id),
            )

    def terminal_error(self) -> tuple[str, bool] | None:
        with closing(self._connection()) as connection:
            event = connection.execute(
                "SELECT state,error_code FROM wecom_bot_events WHERE scope = ? "
                "AND state = 'uncertain' LIMIT 1",
                (self.scope,),
            ).fetchone()
            if event is None:
                delivery = connection.execute(
                    "SELECT state FROM wecom_bot_deliveries WHERE scope = ? "
                    "AND state IN ('sending','uncertain') LIMIT 1",
                    (self.scope,),
                ).fetchone()
            else:
                delivery = None
        if event is not None:
            uncertain = str(event[0]) == "uncertain"
            code = str(event[1] or "")
        elif delivery is not None:
            uncertain = True
            code = ""
        else:
            return None
        return (
            code if _ERROR_RE.fullmatch(code) else (
                "wecom_bot_delivery_uncertain"
                if uncertain
                else "wecom_bot_delivery_rejected"
            ),
            uncertain,
        )

    def resolve_uncertain(self) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE wecom_bot_events SET state='failed' "
                "WHERE scope=? AND state='uncertain'",
                (self.scope,),
            )
            connection.execute(
                "UPDATE wecom_bot_deliveries SET state='failed' "
                "WHERE scope=? AND state IN ('sending','uncertain')",
                (self.scope,),
            )

    def has_uncertain(self) -> bool:
        terminal = self.terminal_error()
        return terminal is not None and terminal[1]

    def claim_delivery(self, key: str) -> str:
        now = int(time.time())
        with closing(self._connection()) as connection, connection:
            row = connection.execute(
                "SELECT state FROM wecom_bot_deliveries "
                "WHERE scope = ? AND delivery_key = ?",
                (self.scope, key),
            ).fetchone()
            if row is not None:
                state = str(row[0])
                if state == "sending":
                    connection.execute(
                        "UPDATE wecom_bot_deliveries "
                        "SET state = 'uncertain', updated_at = ? "
                        "WHERE scope = ? AND delivery_key = ?",
                        (now, self.scope, key),
                    )
                    return "uncertain"
                return state
            connection.execute(
                "INSERT INTO wecom_bot_deliveries("
                "scope, delivery_key, state, updated_at"
                ") VALUES (?, ?, 'sending', ?)",
                (self.scope, key, now),
            )
        return "send"

    def mark_delivery(self, key: str, state: str) -> None:
        if state not in {"sent", "failed", "uncertain"}:
            raise ValueError("WeCom delivery state is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE wecom_bot_deliveries SET state = ?, updated_at = ? "
                "WHERE scope = ? AND delivery_key = ?",
                (state, int(time.time()), self.scope, key),
            )

    def release_delivery(self, key: str) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "DELETE FROM wecom_bot_deliveries "
                "WHERE scope = ? AND delivery_key = ? AND state = 'sending'",
                (self.scope, key),
            )

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            if self.path.exists() and stat.S_ISLNK(self.path.lstat().st_mode):
                raise RuntimeError("WeCom state path is invalid")
            connection = sqlite3.connect(self.path, timeout=5)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA secure_delete = ON")
            if not self._initialized:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS wecom_bot_events(
                        scope TEXT NOT NULL,
                        event_key TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        text TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(
                            state IN ('received','outbound','completed','failed','uncertain')
                        ),
                        channel_id TEXT,
                        thread_id TEXT,
                        turn_id TEXT,
                        client_message_id TEXT,
                        conversation_sha256 TEXT,
                        error_code TEXT,
                        PRIMARY KEY(scope, event_key)
                    );
                    CREATE TABLE IF NOT EXISTS wecom_bot_deliveries(
                        scope TEXT NOT NULL,
                        delivery_key TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(
                            state IN ('sending','sent','failed','uncertain')
                        ),
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(scope, delivery_key)
                    );
                    """
                )
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(wecom_bot_events)")
                }
                if "error_code" not in columns:
                    connection.execute(
                        "ALTER TABLE wecom_bot_events ADD COLUMN error_code TEXT"
                    )
                delivery_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' "
                        "AND name='wecom_bot_deliveries'"
                    ).fetchone()[0]
                )
                if "'failed'" not in delivery_sql:
                    connection.executescript(
                        """
                        ALTER TABLE wecom_bot_deliveries RENAME TO wecom_bot_deliveries_v1;
                        CREATE TABLE wecom_bot_deliveries(
                            scope TEXT NOT NULL, delivery_key TEXT NOT NULL,
                            state TEXT NOT NULL CHECK(
                                state IN ('sending','sent','failed','uncertain')
                            ), updated_at INTEGER NOT NULL,
                            PRIMARY KEY(scope, delivery_key)
                        );
                        INSERT INTO wecom_bot_deliveries
                        SELECT * FROM wecom_bot_deliveries_v1;
                        DROP TABLE wecom_bot_deliveries_v1;
                        """
                    )
                connection.commit()
                os.chmod(self.path, 0o600)
                self._initialized = True
            return connection


class WeComBotLongConnectionAdapter:
    """One WeCom AI Bot long-connection worker."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        socket_factory: Callable[[str], _Socket] | None = None,
        heartbeat_seconds: float = 30.0,
        ack_timeout_seconds: float = 5.0,
    ) -> None:
        if not 1 <= heartbeat_seconds <= 300:
            raise ValueError("WeCom heartbeat interval is invalid")
        if not 0.1 <= ack_timeout_seconds <= 30:
            raise ValueError("WeCom acknowledgement timeout is invalid")
        self.database_path = Path(os.path.abspath(database_path))
        self.socket_factory = socket_factory or self._default_socket
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.ack_timeout_seconds = float(ack_timeout_seconds)
        self._owner: ChannelCredentialOwner | None = None
        self._dispatcher: ChannelRuntimeDispatcher | None = None
        self._store: _WeComStore | None = None
        self._socket: _Socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._health = ConnectorHealth.DISABLED
        self._last_error: str | None = None
        self._credentials: tuple[str, str] | None = None

    def bind_runtime(
        self,
        owner: ChannelCredentialOwner,
        dispatcher: ChannelRuntimeDispatcher,
    ) -> None:
        with self._lock:
            if self._dispatcher is not None and (
                self._owner != owner or self._dispatcher is not dispatcher
            ):
                raise RuntimeError("WeCom Runtime is already bound")
            self._owner = owner
            self._dispatcher = dispatcher
            self._store = _WeComStore(self.database_path, owner)

    def test(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        socket: _Socket | None = None
        try:
            socket = self._open_authenticated_socket(_credentials(config))
            return ConnectorHealthResult(ConnectorHealth.CONNECTED)
        except _WeComFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "wecom_bot_transport_unavailable"
            )
        finally:
            _close_socket(socket)

    def start(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        socket: _Socket | None = None
        try:
            credentials = _credentials(config)
            with self._lock:
                if self._dispatcher is None or self._store is None:
                    return ConnectorHealthResult(
                        ConnectorHealth.ERROR, "wecom_bot_runtime_unavailable"
                    )
                if self._thread is not None and self._thread.is_alive():
                    return ConnectorHealthResult(self._health, self._last_error)
            socket = self._open_authenticated_socket(credentials)
            store = self._required_store()
            store.has_uncertain()
            terminal = store.terminal_error()
            with self._lock:
                self._socket = socket
                self._credentials = credentials
                self._stop_event = threading.Event()
                self._health = (
                    ConnectorHealth.CONNECTED
                    if terminal is None
                    else ConnectorHealth.DEGRADED
                    if terminal[1]
                    else ConnectorHealth.ERROR
                )
                self._last_error = terminal[0] if terminal else None
                self._thread = threading.Thread(
                    target=self._run,
                    args=(socket, credentials),
                    name="emate-wecom-bot-channel",
                    daemon=True,
                )
                self._thread.start()
            return ConnectorHealthResult(self._health, self._last_error)
        except _WeComFailure as error:
            _close_socket(socket)
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            _close_socket(socket)
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "wecom_bot_transport_unavailable"
            )

    def health(self) -> ConnectorHealthResult:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return ConnectorHealthResult(self._health, self._last_error)
            return ConnectorHealthResult(
                self._health if self._last_error else ConnectorHealth.DISABLED,
                self._last_error,
            )

    def resolve_uncertain(self) -> None:
        self._required_store().resolve_uncertain()

    def stop(self, timeout_seconds: float) -> bool:
        with self._lock:
            thread = self._thread
            socket = self._socket
            self._stop_event.set()
        _close_socket(socket)
        if thread is not None:
            thread.join(min(float(timeout_seconds), 5.0))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                self._thread = None
                self._socket = None
                self._credentials = None
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
        if channel_id != "wecom_bot" or not isinstance(text, str) or not text:
            raise ValueError("WeCom delivery is invalid")
        _external_id(conversation_id, "conversation")
        with self._lock:
            socket = self._socket
            store = self._store
        if socket is None or store is None:
            raise _WeComFailure("wecom_bot_not_running", permanent=True)
        for index, chunk in enumerate(_chunks(text)):
            key = f"{idempotency_key}:{index + 1}"
            state = store.claim_delivery(key)
            if state == "sent":
                continue
            if state == "failed":
                raise _WeComFailure("wecom_bot_delivery_rejected", permanent=True)
            if state == "uncertain":
                raise _WeComFailure(
                    "wecom_bot_delivery_uncertain", uncertain=True
                )
            request_id = _request_id(_SEND_MESSAGE)
            try:
                socket.send(
                    _encode_frame(
                        {
                            "cmd": _SEND_MESSAGE,
                            "headers": {"req_id": request_id},
                            "body": {
                                "chatid": conversation_id,
                                "msgtype": "markdown",
                                "markdown": {"content": chunk},
                            },
                        }
                    )
                )
                self._await_ack(
                    socket,
                    request_id,
                    timeout_seconds=self.ack_timeout_seconds,
                    operation="delivery",
                )
            except _WeComFailure as error:
                if error.uncertain:
                    store.mark_delivery(key, "uncertain")
                elif error.permanent:
                    store.mark_delivery(key, "failed")
                else:
                    store.release_delivery(key)
                raise
            except Exception:
                store.mark_delivery(key, "uncertain")
                raise _WeComFailure(
                    "wecom_bot_delivery_uncertain", uncertain=True
                ) from None
            store.mark_delivery(key, "sent")

    def _run(self, socket: _Socket, credentials: tuple[str, str]) -> None:
        current = socket
        backoff = 1.0
        next_heartbeat = time.monotonic() + self.heartbeat_seconds
        try:
            while not self._stop_event.is_set():
                try:
                    self._drain_received()
                    self._drain_outbound()
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        request_id = _request_id(_HEARTBEAT)
                        try:
                            current.send(
                                _encode_frame(
                                    {
                                        "cmd": _HEARTBEAT,
                                        "headers": {"req_id": request_id},
                                    }
                                )
                            )
                        except Exception:
                            raise _WeComFailure(
                                "wecom_bot_transport_unavailable"
                            ) from None
                        self._await_ack(
                            current,
                            request_id,
                            timeout_seconds=self.ack_timeout_seconds,
                            operation="heartbeat",
                        )
                        next_heartbeat = time.monotonic() + self.heartbeat_seconds
                    try:
                        frame = current.recv(timeout=0.25)
                    except TimeoutError:
                        self._set_ready_health()
                        continue
                    except (ConnectionClosed, OSError, RuntimeError):
                        raise _WeComFailure(
                            "wecom_bot_transport_unavailable"
                        ) from None
                    self._handle_unsolicited(_decode_frame(frame))
                    self._set_ready_health()
                    backoff = 1.0
                except _WeComFailure as error:
                    self._set_health(
                        ConnectorHealth.DEGRADED
                        if error.uncertain
                        else ConnectorHealth.ERROR,
                        error.code,
                    )
                    if error.uncertain:
                        continue
                    if error.permanent or self._stop_event.is_set():
                        break
                    _close_socket(current)
                    if self._stop_event.wait(backoff):
                        break
                    backoff = min(backoff * 2, 30.0)
                    try:
                        current = self._open_authenticated_socket(credentials)
                    except _WeComFailure as connection_error:
                        if connection_error.permanent:
                            self._set_health(
                                ConnectorHealth.ERROR, connection_error.code
                            )
                            break
                        continue
                    with self._lock:
                        self._socket = current
                    next_heartbeat = time.monotonic() + self.heartbeat_seconds
                    self._set_ready_health()
                except Exception:
                    self._set_health(
                        ConnectorHealth.ERROR,
                        "wecom_bot_runtime_dispatch_failed",
                    )
                    if self._stop_event.wait(backoff):
                        break
                    backoff = min(backoff * 2, 30.0)
        finally:
            _close_socket(current)
            with self._lock:
                if self._socket is current:
                    self._socket = None

    def _open_authenticated_socket(
        self, credentials: tuple[str, str]
    ) -> _Socket:
        bot_id, secret = credentials
        socket: _Socket | None = None
        try:
            socket = self.socket_factory(_WS_URL)
            request_id = _request_id(_SUBSCRIBE)
            socket.send(
                _encode_frame(
                    {
                        "cmd": _SUBSCRIBE,
                        "headers": {"req_id": request_id},
                        "body": {"bot_id": bot_id, "secret": secret},
                    }
                )
            )
            self._await_ack(
                socket,
                request_id,
                timeout_seconds=self.ack_timeout_seconds,
                operation="authentication",
            )
            return socket
        except _WeComFailure:
            _close_socket(socket)
            raise
        except Exception:
            _close_socket(socket)
            raise _WeComFailure("wecom_bot_transport_unavailable") from None

    def _await_ack(
        self,
        socket: _Socket,
        request_id: str,
        *,
        timeout_seconds: float,
        operation: str,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _WeComFailure(
                    "wecom_bot_delivery_uncertain"
                    if operation == "delivery"
                    else "wecom_bot_transport_unavailable",
                    uncertain=operation == "delivery",
                )
            try:
                raw = socket.recv(timeout=min(remaining, 0.25))
            except TimeoutError:
                continue
            except (ConnectionClosed, OSError, RuntimeError):
                raise _WeComFailure(
                    "wecom_bot_delivery_uncertain"
                    if operation == "delivery"
                    else "wecom_bot_transport_unavailable",
                    uncertain=operation == "delivery",
                ) from None
            frame = _decode_frame(raw)
            headers = frame.get("headers")
            received_id = headers.get("req_id") if isinstance(headers, dict) else None
            if frame.get("cmd") in {None, ""} and received_id == request_id:
                errcode = frame.get("errcode")
                if errcode == 0:
                    return
                if not isinstance(errcode, int):
                    raise _WeComFailure("wecom_bot_provider_response_invalid")
                raise _WeComFailure(
                    "wecom_bot_credentials_rejected"
                    if operation == "authentication"
                    else "wecom_bot_delivery_rejected"
                    if operation == "delivery"
                    else "wecom_bot_transport_unavailable",
                    permanent=operation in {"authentication", "delivery"},
                )
            self._handle_unsolicited(frame)

    def _handle_unsolicited(self, frame: Mapping[str, Any]) -> None:
        command = frame.get("cmd")
        if command == _CALLBACK:
            incoming = _incoming_text(frame)
            if incoming is not None and self._store is not None:
                conversation_id, message_id, text = incoming
                self._store.record(
                    conversation_id=conversation_id,
                    message_id=message_id,
                    text=text,
                )
            return
        if command == _EVENT_CALLBACK:
            body = frame.get("body")
            event = body.get("event") if isinstance(body, Mapping) else None
            if (
                isinstance(event, Mapping)
                and event.get("eventtype") == "disconnected_event"
            ):
                raise _WeComFailure(
                    "wecom_bot_connection_replaced", permanent=True
                )

    def _drain_received(self) -> None:
        dispatcher = self._required_dispatcher()
        store = self._required_store()
        for event in store.received():
            receipt = dispatcher.dispatch(
                ChannelInboundMessage(
                    channel_id="wecom_bot",
                    conversation_id=event.conversation_id,
                    message_id=event.message_id,
                    text=event.text,
                )
            )
            store.set_outbound(event.event_key, receipt)

    def _drain_outbound(self) -> None:
        dispatcher = self._required_dispatcher()
        store = self._required_store()
        for receipt, conversation_id in store.outbound():
            try:
                delivered = dispatcher.deliver(
                    receipt,
                    conversation_id=conversation_id,
                    transport=self,
                )
            except ChannelTurnTerminalFailure as error:
                store.finish(
                    receipt.turn_id,
                    "failed",
                    error.code.replace("channel_", "wecom_bot_", 1),
                )
                continue
            except _WeComFailure as error:
                if error.uncertain:
                    store.finish(receipt.turn_id, "uncertain", error.code)
                    self._set_health(
                        ConnectorHealth.DEGRADED,
                        "wecom_bot_delivery_uncertain",
                    )
                    continue
                if error.permanent:
                    store.finish(receipt.turn_id, "failed", error.code)
                    self._set_health(ConnectorHealth.ERROR, error.code)
                    continue
                raise
            if delivered:
                store.finish(receipt.turn_id, "completed")

    def _required_dispatcher(self) -> ChannelRuntimeDispatcher:
        if self._dispatcher is None:
            raise _WeComFailure("wecom_bot_runtime_unavailable", permanent=True)
        return self._dispatcher

    def _required_store(self) -> _WeComStore:
        if self._store is None:
            raise _WeComFailure("wecom_bot_runtime_unavailable", permanent=True)
        return self._store

    def _set_ready_health(self) -> None:
        store = self._store
        terminal = store.terminal_error() if store is not None else None
        if terminal is None:
            self._set_health(ConnectorHealth.CONNECTED, None)
            return
        error, uncertain = terminal
        self._set_health(
            ConnectorHealth.DEGRADED if uncertain else ConnectorHealth.ERROR,
            error,
        )

    def _set_health(self, health: ConnectorHealth, error: str | None) -> None:
        with self._lock:
            self._health = health
            self._last_error = error

    @staticmethod
    def _default_socket(url: str) -> _Socket:
        return websocket_connect(
            url,
            open_timeout=10,
            close_timeout=2,
            compression=None,
            max_size=_MAX_FRAME_BYTES,
            ping_interval=None,
        )


def _credentials(config: Mapping[str, Any]) -> tuple[str, str]:
    bot_id = config.get("wecom_bot_id")
    secret = config.get("wecom_bot_secret")
    if (
        not isinstance(bot_id, str)
        or not isinstance(secret, str)
        or _ID_RE.fullmatch(bot_id) is None
        or _ID_RE.fullmatch(secret) is None
    ):
        raise _WeComFailure("wecom_bot_credentials_invalid", permanent=True)
    return bot_id, secret


def _incoming_text(frame: Mapping[str, Any]) -> tuple[str, str, str] | None:
    headers = frame.get("headers")
    body = frame.get("body")
    if not isinstance(headers, Mapping) or not isinstance(body, Mapping):
        raise _WeComFailure("wecom_bot_provider_response_invalid")
    _external_id(headers.get("req_id"), "request")
    message_id = _external_id(body.get("msgid"), "message")
    sender = body.get("from")
    if not isinstance(sender, Mapping):
        raise _WeComFailure("wecom_bot_provider_response_invalid")
    user_id = _external_id(sender.get("userid"), "user")
    chat_type = body.get("chattype")
    if chat_type == "group":
        conversation_id = _external_id(body.get("chatid"), "conversation")
    elif chat_type == "single":
        conversation_id = user_id
    else:
        raise _WeComFailure("wecom_bot_provider_response_invalid")
    message_type = body.get("msgtype")
    if message_type == "text":
        part = body.get("text")
        text = part.get("content") if isinstance(part, Mapping) else None
    elif message_type == "voice":
        part = body.get("voice")
        text = part.get("content") if isinstance(part, Mapping) else None
    elif message_type == "mixed":
        mixed = body.get("mixed")
        items = mixed.get("msg_item") if isinstance(mixed, Mapping) else None
        if not isinstance(items, list) or len(items) > 100:
            raise _WeComFailure("wecom_bot_provider_response_invalid")
        parts = [
            item["text"]["content"]
            for item in items
            if isinstance(item, Mapping)
            and item.get("msgtype") == "text"
            and isinstance(item.get("text"), Mapping)
            and isinstance(item["text"].get("content"), str)
        ]
        text = "\n".join(parts)
    else:
        return None
    if not isinstance(text, str):
        raise _WeComFailure("wecom_bot_provider_response_invalid")
    if chat_type == "group":
        text = re.sub(r"^(?:@\S+\s*)+", "", text).strip()
    else:
        text = text.strip()
    if not text:
        return None
    if len(text.encode("utf-8")) > _MAX_FRAME_BYTES:
        raise _WeComFailure("wecom_bot_provider_response_invalid")
    return conversation_id, message_id, text


def _external_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise _WeComFailure(f"wecom_bot_{label}_invalid")
    return value


def _request_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{secrets.token_hex(4)}"


def _chunks(text: str) -> tuple[str, ...]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for character in text:
        encoded_size = len(character.encode("utf-8"))
        if current and size + encoded_size > _MAX_TEXT_BYTES:
            chunks.append("".join(current))
            current = []
            size = 0
        current.append(character)
        size += encoded_size
    if current:
        chunks.append("".join(current))
    return tuple(chunks)


def _encode_frame(frame: Mapping[str, Any]) -> str:
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def _decode_frame(raw: str | bytes) -> Mapping[str, Any]:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _WeComFailure("wecom_bot_provider_response_invalid") from None
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_FRAME_BYTES:
        raise _WeComFailure("wecom_bot_provider_response_invalid")
    try:
        frame = json.loads(raw)
    except (ValueError, RecursionError):
        raise _WeComFailure("wecom_bot_provider_response_invalid") from None
    if not isinstance(frame, dict):
        raise _WeComFailure("wecom_bot_provider_response_invalid")
    return frame


def _close_socket(socket: _Socket | None) -> None:
    if socket is None:
        return
    try:
        socket.close()
    except Exception:
        pass
