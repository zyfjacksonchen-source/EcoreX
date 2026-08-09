"""Built-in QQ Bot Gateway transport for the product channel boundary.

The adapter uses QQ's outbound WebSocket Gateway and REST APIs.  It does not
open a public callback listener or create a second Agent Runtime.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as websocket_connect

from ecorex import __version__

from .channel_runtime import (
    ChannelInboundMessage,
    ChannelRuntimeDispatcher,
    ChannelTurnTerminalFailure,
    ChannelTurnReceipt,
)
from .channel_self_service import ChannelCredentialOwner
from .models import ConnectorHealth, ConnectorHealthResult


_GROUP_AND_C2C_INTENT = 1 << 25
_MAX_RESPONSE_BYTES = 1024 * 1024
# Tencent's maintained QQ channel locks each text request to 5,000 JavaScript
# string units.  Count UTF-16 units here so astral characters stay conservative.
_MAX_TEXT_UNITS = 5_000
# QQ's REST contract permits four C2C and five group passive replies per msg_id.
_MAX_PASSIVE_REPLIES = {"c2c": 4, "group": 5}
_VALUE_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,512}$")
_ID_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,256}$")
_ERROR_RE = re.compile(r"^qq_[a-z0-9_]{1,124}$")


class _HTTPClient(Protocol):
    def get(self, path: str, *, headers: Mapping[str, str]) -> httpx.Response: ...

    def post(
        self,
        path: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
    ) -> httpx.Response: ...

    def close(self) -> None: ...


class _Socket(Protocol):
    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def send(self, message: str) -> None: ...

    def close(self, code: int = 1000, reason: str = "") -> None: ...


class _QQFailure(RuntimeError):
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
    reply_message_id: str
    text: str


class _QQStore:
    """Tenant journal; QQ identifiers stay only in this transport-private DB."""

    def __init__(self, path: str | os.PathLike[str], owner: ChannelCredentialOwner):
        self.path = Path(os.path.abspath(path))
        self.scope = hashlib.sha256(
            f"{owner.organization_id}\0{owner.account_id}".encode()
        ).hexdigest()
        self._lock = threading.RLock()
        self._initialized = False

    def gateway(self) -> tuple[str | None, int | None]:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT session_id, seq FROM qq_gateway WHERE scope = ?",
                (self.scope,),
            ).fetchone()
        if row is None:
            return None, None
        return str(row[0]) or None, int(row[1]) if row[1] is not None else None

    def save_gateway(self, session_id: str, seq: int) -> None:
        _provider_id(session_id, "session")
        _sequence(seq)
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                INSERT INTO qq_gateway(scope, session_id, seq) VALUES (?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET session_id = excluded.session_id,
                    seq = excluded.seq
                """,
                (self.scope, session_id, seq),
            )

    def clear_gateway(self) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "DELETE FROM qq_gateway WHERE scope = ?", (self.scope,)
            )

    def advance(self, seq: int) -> None:
        _sequence(seq)
        with closing(self._connection()) as connection, connection:
            self._advance(connection, seq)

    def record(
        self,
        event: _JournalEvent,
        *,
        route: str,
        target_id: str,
        marker: str,
        seq: int,
    ) -> None:
        _sequence(seq)
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO qq_events(
                    scope, event_key, route, target_id, reply_message_id,
                    marker, text, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'received')
                """,
                (
                    self.scope,
                    event.event_key,
                    route,
                    target_id,
                    event.reply_message_id,
                    marker,
                    event.text,
                ),
            )
            self._advance(connection, seq)

    def received(self) -> tuple[_JournalEvent, ...]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                """
                SELECT event_key, route, target_id, reply_message_id, text
                FROM qq_events WHERE scope = ? AND state = 'received'
                ORDER BY rowid
                """,
                (self.scope,),
            ).fetchall()
        return tuple(
            _JournalEvent(
                event_key=str(row[0]),
                conversation_id=_conversation(str(row[1]), str(row[2])),
                reply_message_id=str(row[3]),
                text=str(row[4]),
            )
            for row in rows
        )

    def set_outbound(self, event_key: str, receipt: ChannelTurnReceipt) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                UPDATE qq_events SET state = 'outbound', channel_id = ?,
                    thread_id = ?, turn_id = ?, client_message_id = ?,
                    conversation_sha256 = ?
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

    def outbound(self) -> tuple[tuple[ChannelTurnReceipt, str, str], ...]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                """
                SELECT channel_id, thread_id, turn_id, client_message_id,
                       conversation_sha256, route, target_id, reply_message_id
                FROM qq_events WHERE scope = ? AND state = 'outbound'
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
                _conversation(str(row[5]), str(row[6])),
                str(row[7]),
            )
            for row in rows
        )

    def finish(
        self,
        turn_id: str,
        state: str,
        error_code: str | None = None,
    ) -> None:
        if state not in {"completed", "failed", "uncertain"}:
            raise ValueError("QQ event terminal state is invalid")
        if (state == "completed") != (error_code is None) or (
            error_code is not None and _ERROR_RE.fullmatch(error_code) is None
        ):
            raise ValueError("QQ event terminal error is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                UPDATE qq_events SET state = ?, error_code = ?, target_id = '',
                    reply_message_id = '', marker = '', text = '',
                    channel_id = NULL, thread_id = NULL, turn_id = NULL,
                    client_message_id = NULL, conversation_sha256 = NULL
                WHERE scope = ? AND turn_id = ? AND state = 'outbound'
                """,
                (state, error_code, self.scope, turn_id),
            )

    def terminal_error(self) -> str | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                """
                SELECT state, error_code FROM qq_events
                WHERE scope = ? AND state IN ('uncertain','failed')
                ORDER BY CASE state WHEN 'uncertain' THEN 0 ELSE 1 END LIMIT 1
                """,
                (self.scope,),
            ).fetchone()
        if row is None:
            return None
        error_code = row[1]
        if isinstance(error_code, str) and _ERROR_RE.fullmatch(error_code):
            return error_code
        return (
            "qq_delivery_uncertain"
            if str(row[0]) == "uncertain"
            else "qq_delivery_rejected"
        )

    def claim_delivery(self, key: str) -> str:
        now = int(time.time())
        with closing(self._connection()) as connection, connection:
            row = connection.execute(
                "SELECT state FROM qq_deliveries WHERE scope = ? AND delivery_key = ?",
                (self.scope, key),
            ).fetchone()
            if row is not None:
                state = str(row[0])
                if state == "sending":
                    connection.execute(
                        "UPDATE qq_deliveries SET state = 'uncertain', updated_at = ? "
                        "WHERE scope = ? AND delivery_key = ?",
                        (now, self.scope, key),
                    )
                    return "uncertain"
                return state
            connection.execute(
                "INSERT INTO qq_deliveries(scope, delivery_key, state, updated_at) "
                "VALUES (?, ?, 'sending', ?)",
                (self.scope, key, now),
            )
        return "send"

    def mark_delivery(self, key: str, state: str) -> None:
        if state not in {"sent", "failed", "uncertain"}:
            raise ValueError("QQ delivery state is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE qq_deliveries SET state = ?, updated_at = ? "
                "WHERE scope = ? AND delivery_key = ?",
                (state, int(time.time()), self.scope, key),
            )

    def release_delivery(self, key: str) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "DELETE FROM qq_deliveries WHERE scope = ? AND delivery_key = ? "
                "AND state = 'sending'",
                (self.scope, key),
            )

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            if self.path.exists() and stat.S_ISLNK(self.path.lstat().st_mode):
                raise RuntimeError("QQ state path is invalid")
            connection = sqlite3.connect(self.path, timeout=5)
            connection.execute("PRAGMA busy_timeout = 5000")
            if not self._initialized:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS qq_gateway(
                        scope TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        seq INTEGER NOT NULL CHECK(seq >= 0)
                    );
                    CREATE TABLE IF NOT EXISTS qq_events(
                        scope TEXT NOT NULL,
                        event_key TEXT NOT NULL,
                        route TEXT NOT NULL CHECK(route IN ('c2c','group')),
                        target_id TEXT NOT NULL,
                        reply_message_id TEXT NOT NULL,
                        marker TEXT NOT NULL,
                        text TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN (
                            'received','outbound','completed','failed','uncertain'
                        )),
                        channel_id TEXT,
                        thread_id TEXT,
                        turn_id TEXT,
                        client_message_id TEXT,
                        conversation_sha256 TEXT,
                        error_code TEXT,
                        PRIMARY KEY(scope, event_key)
                    );
                    CREATE TABLE IF NOT EXISTS qq_deliveries(
                        scope TEXT NOT NULL,
                        delivery_key TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN (
                            'sending','sent','failed','uncertain'
                        )),
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(scope, delivery_key)
                    );
                    """
                )
                connection.commit()
                os.chmod(self.path, 0o600)
                self._initialized = True
            return connection

    def _advance(self, connection: sqlite3.Connection, seq: int) -> None:
        connection.execute(
            "UPDATE qq_gateway SET seq = MAX(seq, ?) WHERE scope = ?",
            (seq, self.scope),
        )


class QQBotGatewayAdapter:
    """One QQ Gateway worker; no public listener or second Runtime."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        client_factory: Callable[[], _HTTPClient] | None = None,
        socket_factory: Callable[[str], _Socket] | None = None,
    ) -> None:
        self.database_path = Path(os.path.abspath(database_path))
        self.client_factory = client_factory or self._default_client
        self.socket_factory = socket_factory or self._default_socket
        self._owner: ChannelCredentialOwner | None = None
        self._dispatcher: ChannelRuntimeDispatcher | None = None
        self._store: _QQStore | None = None
        self._client: _HTTPClient | None = None
        self._socket: _Socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._health = ConnectorHealth.DISABLED
        self._last_error: str | None = None
        self._app_id: str | None = None
        self._app_secret: str | None = None
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._reply_context = threading.local()

    def bind_runtime(
        self,
        owner: ChannelCredentialOwner,
        dispatcher: ChannelRuntimeDispatcher,
    ) -> None:
        with self._lock:
            if self._dispatcher is not None and (
                self._owner != owner or self._dispatcher is not dispatcher
            ):
                raise RuntimeError("QQ Runtime is already bound")
            self._owner = owner
            self._dispatcher = dispatcher
            self._store = _QQStore(self.database_path, owner)

    def test(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        try:
            app_id, app_secret = _credentials(config)
            client = self.client_factory()
            try:
                token, _ = self._issue_token(client, app_id, app_secret)
                self._gateway_url(client, token)
            finally:
                _close(client)
            return ConnectorHealthResult(ConnectorHealth.CONNECTED)
        except _QQFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(ConnectorHealth.ERROR, "qq_transport_unavailable")

    def start(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        try:
            app_id, app_secret = _credentials(config)
            with self._lock:
                if self._dispatcher is None or self._store is None:
                    return ConnectorHealthResult(
                        ConnectorHealth.ERROR, "qq_runtime_unavailable"
                    )
                if self._thread is not None and self._thread.is_alive():
                    return ConnectorHealthResult(self._health, self._last_error)
            client = self.client_factory()
            socket: _Socket | None = None
            try:
                token, expires_at = self._issue_token(client, app_id, app_secret)
                socket, heartbeat_seconds = self._new_socket(client, token)
                with self._lock:
                    self._client = client
                    self._socket = socket
                    self._app_id = app_id
                    self._app_secret = app_secret
                    self._access_token = token
                    self._access_token_expires_at = expires_at
                    self._stop_event = threading.Event()
                    terminal_error = self._required_store().terminal_error()
                    self._health = (
                        ConnectorHealth.DEGRADED
                        if terminal_error
                        else ConnectorHealth.CONNECTED
                    )
                    self._last_error = terminal_error
                    self._thread = threading.Thread(
                        target=self._run,
                        args=(client, socket, heartbeat_seconds),
                        name="emate-qq-channel",
                        daemon=True,
                    )
                    self._thread.start()
            except Exception:
                _close_socket(socket)
                _close(client)
                raise
            return ConnectorHealthResult(self._health, self._last_error)
        except _QQFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(ConnectorHealth.ERROR, "qq_transport_unavailable")

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
            socket = self._socket
            self._stop_event.set()
        _close_socket(socket)
        if thread is not None:
            thread.join(timeout_seconds)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                self._thread = None
                self._client = None
                self._socket = None
                self._clear_live_credentials()
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
        if channel_id != "qq" or not isinstance(text, str) or not text:
            raise ValueError("QQ delivery is invalid")
        route, target_id = _split_conversation(conversation_id)
        context = getattr(self._reply_context, "value", None)
        if (
            not isinstance(context, tuple)
            or len(context) != 3
            or context[0] != route
            or context[1] != target_id
        ):
            raise _QQFailure("qq_reply_context_missing", permanent=True)
        reply_message_id = _provider_id(context[2], "reply message")
        store = self._required_store()
        chunks = _chunks(text)
        if len(chunks) > _MAX_PASSIVE_REPLIES[route]:
            raise _QQFailure("qq_delivery_too_large", permanent=True)
        path = (
            f"v2/users/{quote(target_id, safe='')}/messages"
            if route == "c2c"
            else f"v2/groups/{quote(target_id, safe='')}/messages"
        )
        for index, chunk in enumerate(chunks):
            key = f"{idempotency_key}:{index + 1}:{len(chunks)}"
            state = store.claim_delivery(key)
            if state == "sent":
                continue
            if state == "failed":
                raise _QQFailure("qq_delivery_rejected", permanent=True)
            if state == "uncertain":
                raise _QQFailure("qq_delivery_uncertain", uncertain=True)
            body = {
                "content": chunk,
                "msg_type": 0,
                "msg_id": reply_message_id,
                "msg_seq": index + 1,
            }
            try:
                response = self._delivery_request(path, body)
                message_id = response.get("id")
                if (
                    not isinstance(message_id, str)
                    or _VALUE_RE.fullmatch(message_id) is None
                ):
                    raise _QQFailure("qq_delivery_uncertain", uncertain=True)
            except _QQFailure as error:
                if error.uncertain:
                    store.mark_delivery(key, "uncertain")
                elif error.permanent:
                    store.mark_delivery(key, "failed")
                else:
                    store.release_delivery(key)
                raise
            store.mark_delivery(key, "sent")

    def _run(
        self,
        client: _HTTPClient,
        socket: _Socket,
        heartbeat_seconds: float,
    ) -> None:
        current = socket
        heartbeat = heartbeat_seconds
        next_heartbeat = time.monotonic() + heartbeat
        heartbeat_pending = False
        backoff = 1.0
        try:
            while not self._stop_event.is_set():
                try:
                    self._refresh_token(client)
                    self._drain_received()
                    self._drain_outbound()
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        if heartbeat_pending:
                            raise _QQFailure("qq_heartbeat_unacknowledged")
                        self._send_heartbeat(current)
                        heartbeat_pending = True
                        next_heartbeat = now + heartbeat
                    try:
                        frame = current.recv(
                            timeout=max(0.01, min(0.25, next_heartbeat - now))
                        )
                    except (ConnectionClosed, OSError):
                        raise _QQFailure("qq_transport_unavailable") from None
                    opcode = self._handle_frame(current, frame)
                    if opcode == 11:
                        heartbeat_pending = False
                    self._set_ready_health()
                    backoff = 1.0
                except TimeoutError:
                    self._set_ready_health()
                except _QQFailure as error:
                    self._set_health(
                        ConnectorHealth.DEGRADED
                        if error.uncertain
                        else ConnectorHealth.ERROR,
                        error.code,
                    )
                    if error.permanent or self._stop_event.wait(backoff):
                        return
                    backoff = min(backoff * 2, 30.0)
                    _close_socket(current)
                    try:
                        token = self._refresh_token(client, force=True)
                        current, heartbeat = self._new_socket(client, token)
                    except _QQFailure as reconnect_error:
                        self._set_health(ConnectorHealth.ERROR, reconnect_error.code)
                        continue
                    heartbeat_pending = False
                    next_heartbeat = time.monotonic() + heartbeat
                    with self._lock:
                        self._socket = current
                except Exception:
                    self._set_health(
                        ConnectorHealth.ERROR, "qq_runtime_dispatch_failed"
                    )
                    if self._stop_event.wait(backoff):
                        return
                    backoff = min(backoff * 2, 30.0)
                    _close_socket(current)
                    try:
                        token = self._refresh_token(client, force=True)
                        current, heartbeat = self._new_socket(client, token)
                    except _QQFailure as reconnect_error:
                        self._set_health(ConnectorHealth.ERROR, reconnect_error.code)
                        continue
                    heartbeat_pending = False
                    next_heartbeat = time.monotonic() + heartbeat
                    with self._lock:
                        self._socket = current
        finally:
            _close_socket(current)
            _close(client)
            with self._lock:
                if self._client is client:
                    self._client = None
                    self._clear_live_credentials()
                if self._socket is current:
                    self._socket = None

    def _handle_frame(self, socket: _Socket, frame: str | bytes) -> int:
        payload = _frame(frame)
        opcode = payload.get("op")
        if not isinstance(opcode, int) or isinstance(opcode, bool):
            raise _QQFailure("qq_provider_response_invalid")
        if opcode == 0:
            seq = _sequence(payload.get("s"))
            event = self._event(payload)
            if event is None:
                self._required_store().advance(seq)
            else:
                journal, route, target_id, marker = event
                self._required_store().record(
                    journal,
                    route=route,
                    target_id=target_id,
                    marker=marker,
                    seq=seq,
                )
                self._drain_received()
            return opcode
        if opcode == 1:
            self._send_heartbeat(socket)
            return opcode
        if opcode == 7:
            raise _QQFailure("qq_reconnect_requested")
        if opcode == 9:
            self._required_store().clear_gateway()
            raise _QQFailure("qq_session_invalid")
        if opcode in {10, 11}:
            return opcode
        raise _QQFailure("qq_provider_response_invalid")

    def _event(
        self, payload: Mapping[str, Any]
    ) -> tuple[_JournalEvent, str, str, str] | None:
        event_type = payload.get("t")
        if event_type not in {"C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"}:
            return None
        data = payload.get("d")
        if not isinstance(data, Mapping):
            raise _QQFailure("qq_provider_response_invalid")
        reply_message_id = _provider_id(data.get("id"), "message")
        text = data.get("content")
        if (
            not isinstance(text, str)
            or not text.strip()
            or "\x00" in text
            or len(text) > 1_000_000
        ):
            return None
        if event_type == "C2C_MESSAGE_CREATE":
            author = data.get("author")
            if not isinstance(author, Mapping):
                raise _QQFailure("qq_provider_response_invalid")
            target_id = _provider_id(
                author.get("user_openid") or author.get("id"), "user"
            )
            route = "c2c"
        else:
            target_id = _provider_id(data.get("group_openid"), "group")
            route = "group"
        marker = _message_marker(data)
        event_key = hashlib.sha256(
            f"{route}\0{target_id}\0{reply_message_id}\0{marker}".encode()
        ).hexdigest()
        return (
            _JournalEvent(
                event_key=event_key,
                conversation_id=_conversation(route, target_id),
                reply_message_id=reply_message_id,
                text=text,
            ),
            route,
            target_id,
            marker,
        )

    def _drain_received(self) -> None:
        dispatcher = self._required_dispatcher()
        store = self._required_store()
        for event in store.received():
            receipt = dispatcher.dispatch(
                ChannelInboundMessage(
                    channel_id="qq",
                    conversation_id=event.conversation_id,
                    message_id=event.event_key,
                    text=event.text,
                )
            )
            store.set_outbound(event.event_key, receipt)

    def _drain_outbound(self) -> None:
        dispatcher = self._required_dispatcher()
        store = self._required_store()
        for receipt, conversation_id, reply_message_id in store.outbound():
            route, target_id = _split_conversation(conversation_id)
            self._reply_context.value = (route, target_id, reply_message_id)
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
                    error.code.replace("channel_", "qq_", 1),
                )
                continue
            except _QQFailure as error:
                if error.uncertain:
                    store.finish(receipt.turn_id, "uncertain", error.code)
                    self._set_health(
                        ConnectorHealth.DEGRADED, "qq_delivery_uncertain"
                    )
                    continue
                if error.permanent:
                    store.finish(receipt.turn_id, "failed", error.code)
                    self._set_health(ConnectorHealth.ERROR, error.code)
                    continue
                raise
            finally:
                self._reply_context.value = None
            if delivered:
                store.finish(receipt.turn_id, "completed")

    def _new_socket(
        self, client: _HTTPClient, token: str
    ) -> tuple[_Socket, float]:
        store = self._required_store()
        for attempt in range(2):
            socket: _Socket | None = None
            try:
                socket = self.socket_factory(self._gateway_url(client, token))
                hello = _frame(socket.recv(timeout=4))
                interval = hello.get("d")
                heartbeat_ms = (
                    interval.get("heartbeat_interval")
                    if isinstance(interval, Mapping)
                    else None
                )
                if (
                    hello.get("op") != 10
                    or not isinstance(heartbeat_ms, int)
                    or isinstance(heartbeat_ms, bool)
                    or not 1_000 <= heartbeat_ms <= 120_000
                ):
                    raise ValueError
                session_id, seq = store.gateway()
                resume = bool(session_id is not None and seq is not None)
                body: dict[str, Any]
                if resume:
                    body = {
                        "op": 6,
                        "d": {
                            "token": f"QQBot {token}",
                            "session_id": session_id,
                            "seq": seq,
                        },
                    }
                else:
                    body = {
                        "op": 2,
                        "d": {
                            "token": f"QQBot {token}",
                            "intents": _GROUP_AND_C2C_INTENT,
                            "shard": [0, 1],
                            "properties": {
                                "$os": os.name,
                                "$browser": "e-Mate",
                                "$device": "e-Mate",
                            },
                        },
                    }
                socket.send(json.dumps(body, sort_keys=True, separators=(",", ":")))
                ready = _frame(socket.recv(timeout=4))
                if ready.get("op") == 9 and resume and attempt == 0:
                    store.clear_gateway()
                    _close_socket(socket)
                    continue
                if ready.get("op") != 0 or ready.get("t") not in {
                    "READY",
                    "RESUMED",
                }:
                    raise ValueError
                ready_seq = _sequence(ready.get("s"))
                if ready.get("t") == "READY":
                    data = ready.get("d")
                    if not isinstance(data, Mapping):
                        raise ValueError
                    ready_session = _provider_id(data.get("session_id"), "session")
                    store.save_gateway(ready_session, ready_seq)
                else:
                    if not resume:
                        raise ValueError
                    store.advance(ready_seq)
                return socket, heartbeat_ms / 1000
            except _QQFailure:
                _close_socket(socket)
                raise
            except Exception:
                _close_socket(socket)
                raise _QQFailure("qq_transport_unavailable") from None
        raise _QQFailure("qq_gateway_rejected", permanent=True)

    def _send_heartbeat(self, socket: _Socket) -> None:
        _, seq = self._required_store().gateway()
        try:
            socket.send(json.dumps({"op": 1, "d": seq}, separators=(",", ":")))
        except Exception:
            raise _QQFailure("qq_transport_unavailable") from None

    def _issue_token(
        self,
        client: _HTTPClient,
        app_id: str,
        app_secret: str,
    ) -> tuple[str, float]:
        payload = self._request_json(
            lambda: client.post(
                "app/getAppAccessToken",
                headers={"Content-Type": "application/json"},
                json={"appId": app_id, "clientSecret": app_secret},
            ),
            operation="token",
        )
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, str) and expires_in.isdigit():
            expires_in = int(expires_in)
        if (
            not isinstance(token, str)
            or _VALUE_RE.fullmatch(token) is None
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or not 1 <= expires_in <= 86_400
        ):
            raise _QQFailure("qq_provider_response_invalid")
        return token, time.monotonic() + expires_in

    def _gateway_url(self, client: _HTTPClient, token: str) -> str:
        payload = self._request_json(
            lambda: client.get(
                "gateway", headers={"Authorization": f"QQBot {token}"}
            ),
            operation="gateway",
        )
        url = payload.get("url")
        if not isinstance(url, str) or len(url) > 4096:
            raise _QQFailure("qq_provider_response_invalid")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "wss"
            or (parsed.hostname or "").casefold() != "api.sgroup.qq.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or bool(parsed.fragment)
        ):
            raise _QQFailure("qq_provider_response_invalid")
        return url

    def _delivery_request(
        self, path: str, body: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        client = self._required_client()
        for attempt in range(2):
            token = self._refresh_token(client, force=attempt > 0)
            try:
                return self._request_json(
                    lambda: client.post(
                        path,
                        headers={"Authorization": f"QQBot {token}"},
                        json=body,
                    ),
                    operation="delivery",
                )
            except _QQFailure as error:
                if error.code == "qq_access_token_rejected" and attempt == 0:
                    continue
                if error.code == "qq_access_token_rejected":
                    raise _QQFailure(
                        "qq_delivery_rejected", permanent=True
                    ) from None
                raise
        raise _QQFailure("qq_delivery_rejected", permanent=True)

    def _request_json(
        self,
        request: Callable[[], httpx.Response],
        *,
        operation: str,
    ) -> Mapping[str, Any]:
        delivery = operation == "delivery"
        try:
            response = request()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise _QQFailure("qq_transport_unavailable") from None
        except (httpx.TimeoutException, httpx.TransportError):
            raise _QQFailure(
                "qq_delivery_uncertain" if delivery else "qq_transport_unavailable",
                uncertain=delivery,
            ) from None
        except Exception:
            raise _QQFailure(
                "qq_delivery_uncertain" if delivery else "qq_transport_unavailable",
                uncertain=delivery,
            ) from None
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise _QQFailure(
                "qq_delivery_uncertain" if delivery else "qq_provider_response_invalid",
                uncertain=delivery,
            )
        if response.status_code in {401, 403}:
            raise _QQFailure(
                "qq_access_token_rejected" if delivery else "qq_auth_rejected",
                permanent=not delivery,
            )
        if response.status_code >= 500:
            raise _QQFailure(
                "qq_delivery_uncertain" if delivery else "qq_transport_unavailable",
                uncertain=delivery,
            )
        if response.status_code == 429:
            raise _QQFailure("qq_rate_limited")
        if response.status_code not in {200, 201}:
            raise _QQFailure(
                "qq_delivery_rejected" if delivery else "qq_provider_rejected",
                permanent=delivery,
            )
        try:
            payload = response.json()
        except ValueError:
            raise _QQFailure(
                "qq_delivery_uncertain" if delivery else "qq_provider_response_invalid",
                uncertain=delivery,
            ) from None
        if not isinstance(payload, dict):
            raise _QQFailure(
                "qq_delivery_uncertain" if delivery else "qq_provider_response_invalid",
                uncertain=delivery,
            )
        code = payload.get("code")
        if code not in {None, 0, "0"}:
            raise _QQFailure(
                "qq_delivery_rejected" if delivery else "qq_provider_rejected",
                permanent=delivery,
            )
        return payload

    def _refresh_token(self, client: _HTTPClient, *, force: bool = False) -> str:
        with self._lock:
            if (
                not force
                and self._access_token is not None
                and self._access_token_expires_at > time.monotonic() + 60
            ):
                return self._access_token
            app_id = self._app_id
            app_secret = self._app_secret
        if app_id is None or app_secret is None:
            raise _QQFailure("qq_not_running", permanent=True)
        token, expires_at = self._issue_token(client, app_id, app_secret)
        with self._lock:
            self._access_token = token
            self._access_token_expires_at = expires_at
        return token

    def _set_ready_health(self) -> None:
        error = self._required_store().terminal_error()
        self._set_health(
            ConnectorHealth.DEGRADED if error else ConnectorHealth.CONNECTED,
            error,
        )

    def _set_health(self, health: ConnectorHealth, error: str | None) -> None:
        with self._lock:
            self._health = health
            self._last_error = error

    def _required_dispatcher(self) -> ChannelRuntimeDispatcher:
        with self._lock:
            if self._dispatcher is None:
                raise _QQFailure("qq_runtime_unavailable", permanent=True)
            return self._dispatcher

    def _required_store(self) -> _QQStore:
        with self._lock:
            if self._store is None:
                raise _QQFailure("qq_runtime_unavailable", permanent=True)
            return self._store

    def _required_client(self) -> _HTTPClient:
        with self._lock:
            if self._client is None:
                raise _QQFailure("qq_not_running", permanent=True)
            return self._client

    def _clear_live_credentials(self) -> None:
        self._app_id = None
        self._app_secret = None
        self._access_token = None
        self._access_token_expires_at = 0.0

    @staticmethod
    def _default_client() -> httpx.Client:
        return httpx.Client(
            base_url="https://api.bot.qq.com/",
            timeout=httpx.Timeout(connect=4, read=4, write=4, pool=4),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
            headers={"User-Agent": f"e-Mate/{__version__} QQChannel/1"},
        )

    @staticmethod
    def _default_socket(url: str) -> _Socket:
        return websocket_connect(
            url,
            open_timeout=4,
            ping_interval=None,
            close_timeout=1,
            max_size=_MAX_RESPONSE_BYTES,
            max_queue=16,
            user_agent_header=f"e-Mate/{__version__} QQChannel/1",
        )


def _credentials(config: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(config, Mapping) or set(config) != {
        "qq_app_id",
        "qq_app_secret",
    }:
        raise _QQFailure("qq_configuration_invalid", permanent=True)
    app_id = config.get("qq_app_id")
    secret = config.get("qq_app_secret")
    if (
        not isinstance(app_id, str)
        or _VALUE_RE.fullmatch(app_id) is None
        or not isinstance(secret, str)
        or _VALUE_RE.fullmatch(secret) is None
    ):
        raise _QQFailure("qq_configuration_invalid", permanent=True)
    return app_id, secret


def _frame(frame: str | bytes) -> Mapping[str, Any]:
    if not isinstance(frame, str) or len(frame.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise _QQFailure("qq_provider_response_invalid")
    try:
        payload = json.loads(frame)
    except ValueError:
        raise _QQFailure("qq_provider_response_invalid") from None
    if not isinstance(payload, dict):
        raise _QQFailure("qq_provider_response_invalid")
    return payload


def _sequence(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _QQFailure("qq_provider_response_invalid")
    return value


def _provider_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise _QQFailure(f"qq_{label.replace(' ', '_')}_invalid")
    return value


def _message_marker(data: Mapping[str, Any]) -> str:
    msg_seq = data.get("msg_seq")
    if isinstance(msg_seq, int) and not isinstance(msg_seq, bool) and msg_seq >= 0:
        return str(msg_seq)
    scene = data.get("message_scene")
    ext = scene.get("ext") if isinstance(scene, Mapping) else None
    if isinstance(ext, list):
        marker = next(
            (
                item.removeprefix("msg_idx=")
                for item in ext
                if isinstance(item, str) and item.startswith("msg_idx=")
            ),
            None,
        )
        if marker and _VALUE_RE.fullmatch(marker):
            return marker
    return "1"


def _chunks(text: str) -> tuple[str, ...]:
    if not text or "\x00" in text or any(
        0xD800 <= ord(character) <= 0xDFFF for character in text
    ):
        raise _QQFailure("qq_delivery_text_invalid", permanent=True)
    chunks: list[str] = []
    start = 0
    while start < len(text):
        units = 0
        end = start
        while end < len(text):
            character = text[end]
            width = 2 if ord(character) > 0xFFFF else 1
            if units + width > _MAX_TEXT_UNITS:
                break
            units += width
            end += 1
        chunks.append(text[start:end])
        start = end
    return tuple(chunks)


def _conversation(route: str, target_id: str) -> str:
    if route not in {"c2c", "group"}:
        raise ValueError("QQ route is invalid")
    _provider_id(target_id, "target")
    encoded = base64.urlsafe_b64encode(target_id.encode()).decode().rstrip("=")
    return f"{route}:{encoded}"


def _split_conversation(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError("QQ conversation is invalid")
    route, separator, encoded = value.partition(":")
    if route not in {"c2c", "group"} or not separator or not encoded:
        raise ValueError("QQ conversation is invalid")
    try:
        target = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode()
    except (ValueError, UnicodeDecodeError):
        raise ValueError("QQ conversation is invalid") from None
    _provider_id(target, "target")
    if _conversation(route, target) != value:
        raise ValueError("QQ conversation is invalid")
    return route, target


def _close(client: _HTTPClient | None) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception:
        pass


def _close_socket(socket: _Socket | None) -> None:
    if socket is None:
        return
    try:
        socket.close()
    except Exception:
        pass


__all__ = ["QQBotGatewayAdapter"]
