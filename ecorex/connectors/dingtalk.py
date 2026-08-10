"""Built-in DingTalk Stream Mode transport for the product Runtime.

This implements the documented Stream wire contract with the already packaged
HTTP and WebSocket clients.  It opens no public listener and never creates a
second Agent Runtime.
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
import sqlite3
import stat
import threading
import time
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

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


_CALLBACK_TOPIC = "/v1.0/im/bot/messages/get"
_ID_RE = re.compile(r"^[^\x00\r\n]{1,512}$")
_ERROR_RE = re.compile(r"^dingtalk_[a-z0-9_]{1,124}$")
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_TEXT_CHARS = 4000


class _HTTPClient(Protocol):
    def post(self, path: str, *, json: Mapping[str, Any]) -> httpx.Response: ...

    def close(self) -> None: ...


class _Socket(Protocol):
    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def send(self, message: str) -> None: ...

    def close(self, code: int = 1000, reason: str = "") -> None: ...


@dataclass(frozen=True, slots=True)
class _ReplyTransport:
    adapter: "DingTalkStreamAdapter"
    reply_url: str

    def send_text(
        self,
        *,
        channel_id: str,
        conversation_id: str,
        text: str,
        idempotency_key: str,
    ) -> None:
        self.adapter._send_text_to(
            self.reply_url,
            channel_id=channel_id,
            conversation_id=conversation_id,
            text=text,
            idempotency_key=idempotency_key,
        )


class _DingTalkFailure(RuntimeError):
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


class _DingTalkStore:
    """Tenant journal; transport identifiers stay only in this private DB."""

    def __init__(self, path: str | os.PathLike[str], owner: ChannelCredentialOwner):
        self.path = Path(os.path.abspath(path))
        self.scope = hashlib.sha256(
            f"{owner.organization_id}\0{owner.account_id}".encode("utf-8")
        ).hexdigest()
        self._lock = threading.RLock()
        self._initialized = False

    def record(
        self,
        *,
        conversation_id: str,
        message_id: str,
        text: str,
        reply_url: str,
    ) -> None:
        event_key = hashlib.sha256(
            f"{conversation_id}\0{message_id}".encode("utf-8")
        ).hexdigest()
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO dingtalk_events(
                    scope, event_key, conversation_id, message_id, text,
                    reply_url, state
                ) VALUES (?, ?, ?, ?, ?, ?, 'received')
                """,
                (
                    self.scope,
                    event_key,
                    conversation_id,
                    message_id,
                    text,
                    reply_url,
                ),
            )

    def received(self) -> tuple[_JournalEvent, ...]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                """
                SELECT event_key, conversation_id, message_id, text
                FROM dingtalk_events
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
                UPDATE dingtalk_events
                SET state = 'outbound', channel_id = ?, thread_id = ?,
                    turn_id = ?, client_message_id = ?, conversation_sha256 = ?
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
                       conversation_sha256, conversation_id, reply_url
                FROM dingtalk_events
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
                str(row[6]),
            )
            for row in rows
        )

    def finish(
        self, turn_id: str, state: str, error_code: str | None = None
    ) -> None:
        if state not in {"completed", "failed", "uncertain"}:
            raise ValueError("DingTalk event state is invalid")
        if (state == "completed") != (error_code is None) or (
            error_code is not None and _ERROR_RE.fullmatch(error_code) is None
        ):
            raise ValueError("DingTalk event terminal error is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                UPDATE dingtalk_events
                SET state = ?, error_code = ?, conversation_id = '', message_id = '',
                    text = '', reply_url = '', channel_id = NULL, thread_id = NULL,
                    turn_id = NULL, client_message_id = NULL,
                    conversation_sha256 = NULL
                WHERE scope = ? AND turn_id = ? AND state = 'outbound'
                """,
                (state, error_code, self.scope, turn_id),
            )

    def terminal_error(self) -> tuple[str, bool] | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT state,error_code FROM dingtalk_events WHERE scope = ? "
                "AND state = 'uncertain' LIMIT 1",
                (self.scope,),
            ).fetchone()
            delivery = connection.execute(
                "SELECT state FROM dingtalk_deliveries WHERE scope=? "
                "AND state IN ('sending','uncertain') LIMIT 1",
                (self.scope,),
            ).fetchone()
        if row is None:
            return (
                ("dingtalk_delivery_uncertain", True)
                if delivery is not None
                else None
            )
        uncertain = str(row[0]) == "uncertain"
        code = str(row[1] or "")
        return (
            code if _ERROR_RE.fullmatch(code) else (
                "dingtalk_delivery_uncertain" if uncertain else "dingtalk_delivery_rejected"
            ),
            uncertain,
        )

    def resolve_uncertain(self) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "UPDATE dingtalk_events SET state='failed' "
                "WHERE scope=? AND state='uncertain'",
                (self.scope,),
            )
            connection.execute(
                "UPDATE dingtalk_deliveries SET state='failed' "
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
                """
                SELECT state FROM dingtalk_deliveries
                WHERE scope = ? AND delivery_key = ?
                """,
                (self.scope, key),
            ).fetchone()
            if row is not None:
                state = str(row[0])
                if state == "sending":
                    connection.execute(
                        """
                        UPDATE dingtalk_deliveries
                        SET state = 'uncertain', updated_at = ?
                        WHERE scope = ? AND delivery_key = ?
                        """,
                        (now, self.scope, key),
                    )
                    return "uncertain"
                return state
            connection.execute(
                """
                INSERT INTO dingtalk_deliveries(
                    scope, delivery_key, state, updated_at
                ) VALUES (?, ?, 'sending', ?)
                """,
                (self.scope, key, now),
            )
        return "send"

    def mark_delivery(self, key: str, state: str) -> None:
        if state not in {"sent", "failed", "uncertain"}:
            raise ValueError("DingTalk delivery state is invalid")
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                UPDATE dingtalk_deliveries SET state = ?, updated_at = ?
                WHERE scope = ? AND delivery_key = ?
                """,
                (state, int(time.time()), self.scope, key),
            )

    def release_delivery(self, key: str) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                DELETE FROM dingtalk_deliveries
                WHERE scope = ? AND delivery_key = ? AND state = 'sending'
                """,
                (self.scope, key),
            )

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            if self.path.exists() and stat.S_ISLNK(self.path.lstat().st_mode):
                raise RuntimeError("DingTalk state path is invalid")
            connection = sqlite3.connect(self.path, timeout=5)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA secure_delete = ON")
            if not self._initialized:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS dingtalk_events(
                        scope TEXT NOT NULL,
                        event_key TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        text TEXT NOT NULL,
                        reply_url TEXT NOT NULL,
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
                    CREATE TABLE IF NOT EXISTS dingtalk_deliveries(
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
                    for row in connection.execute("PRAGMA table_info(dingtalk_events)")
                }
                if "error_code" not in columns:
                    connection.execute(
                        "ALTER TABLE dingtalk_events ADD COLUMN error_code TEXT"
                    )
                delivery_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' "
                        "AND name='dingtalk_deliveries'"
                    ).fetchone()[0]
                )
                if "'failed'" not in delivery_sql:
                    connection.executescript(
                        """
                        ALTER TABLE dingtalk_deliveries RENAME TO dingtalk_deliveries_v1;
                        CREATE TABLE dingtalk_deliveries(
                            scope TEXT NOT NULL, delivery_key TEXT NOT NULL,
                            state TEXT NOT NULL CHECK(
                                state IN ('sending','sent','failed','uncertain')
                            ), updated_at INTEGER NOT NULL,
                            PRIMARY KEY(scope, delivery_key)
                        );
                        INSERT INTO dingtalk_deliveries
                        SELECT * FROM dingtalk_deliveries_v1;
                        DROP TABLE dingtalk_deliveries_v1;
                        """
                    )
                connection.commit()
                os.chmod(self.path, 0o600)
                self._initialized = True
            return connection


class DingTalkStreamAdapter:
    """One DingTalk Stream worker; no public listener or second Runtime."""

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
        self._store: _DingTalkStore | None = None
        self._client: _HTTPClient | None = None
        self._socket: _Socket | None = None
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
                raise RuntimeError("DingTalk Runtime is already bound")
            self._owner = owner
            self._dispatcher = dispatcher
            self._store = _DingTalkStore(self.database_path, owner)

    def test(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        client: _HTTPClient | None = None
        socket: _Socket | None = None
        try:
            credentials = _credentials(config)
            client = self.client_factory()
            socket = self._open_socket(client, credentials)
            return ConnectorHealthResult(ConnectorHealth.CONNECTED)
        except _DingTalkFailure as error:
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "dingtalk_transport_unavailable"
            )
        finally:
            _close_socket(socket)
            _close(client)

    def start(self, config: Mapping[str, Any]) -> ConnectorHealthResult:
        client: _HTTPClient | None = None
        socket: _Socket | None = None
        try:
            credentials = _credentials(config)
            with self._lock:
                if self._dispatcher is None or self._store is None:
                    return ConnectorHealthResult(
                        ConnectorHealth.ERROR, "dingtalk_runtime_unavailable"
                    )
                if self._thread is not None and self._thread.is_alive():
                    return ConnectorHealthResult(self._health, self._last_error)
            client = self.client_factory()
            socket = self._open_socket(client, credentials)
            store = self._required_store()
            store.has_uncertain()
            terminal = store.terminal_error()
            with self._lock:
                self._client = client
                self._socket = socket
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
                    args=(client, socket, credentials),
                    name="emate-dingtalk-channel",
                    daemon=True,
                )
                self._thread.start()
            return ConnectorHealthResult(self._health, self._last_error)
        except _DingTalkFailure as error:
            _close_socket(socket)
            _close(client)
            return ConnectorHealthResult(ConnectorHealth.ERROR, error.code)
        except Exception:
            _close_socket(socket)
            _close(client)
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "dingtalk_transport_unavailable"
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
                self._client = None
                self._socket = None
                self._health = ConnectorHealth.DISABLED
                self._last_error = None
        return stopped

    def _send_text_to(
        self,
        reply_url: str,
        *,
        channel_id: str,
        conversation_id: str,
        text: str,
        idempotency_key: str,
    ) -> None:
        if channel_id != "dingtalk" or not isinstance(text, str) or not text:
            raise ValueError("DingTalk delivery is invalid")
        if _ID_RE.fullmatch(conversation_id) is None:
            raise ValueError("DingTalk conversation is invalid")
        with self._lock:
            client = self._client
            store = self._store
        if client is None or store is None:
            raise _DingTalkFailure("dingtalk_not_running", permanent=True)
        _reply_url(reply_url)
        for index, chunk in enumerate(_chunks(text)):
            key = f"{idempotency_key}:{index + 1}"
            state = store.claim_delivery(key)
            if state == "sent":
                continue
            if state == "failed":
                raise _DingTalkFailure("dingtalk_delivery_rejected", permanent=True)
            if state == "uncertain":
                raise _DingTalkFailure(
                    "dingtalk_delivery_uncertain", uncertain=True
                )
            try:
                self._send_reply(client, reply_url, chunk)
            except _DingTalkFailure as error:
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
        credentials: tuple[str, str],
    ) -> None:
        current = socket
        backoff = 1.0
        try:
            while not self._stop_event.is_set():
                try:
                    self._drain_received()
                    self._drain_outbound()
                    try:
                        frame = current.recv(timeout=0.25)
                    except TimeoutError:
                        self._set_ready_health()
                        continue
                    except (ConnectionClosed, OSError, RuntimeError):
                        raise _DingTalkFailure(
                            "dingtalk_transport_unavailable"
                        ) from None
                    reconnect = self._handle_frame(current, frame)
                    self._set_ready_health()
                    backoff = 1.0
                    if reconnect:
                        raise _DingTalkFailure(
                            "dingtalk_transport_unavailable"
                        )
                except _DingTalkFailure as error:
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
                    backoff = min(backoff * 2, 16.0)
                    try:
                        current = self._open_socket(client, credentials)
                    except _DingTalkFailure as connection_error:
                        if connection_error.permanent:
                            self._set_health(
                                ConnectorHealth.ERROR, connection_error.code
                            )
                            break
                        continue
                    with self._lock:
                        self._socket = current
                    self._set_ready_health()
                except Exception:
                    self._set_health(
                        ConnectorHealth.ERROR,
                        "dingtalk_runtime_dispatch_failed",
                    )
                    break
        finally:
            _close_socket(current)
            _close(client)

    def _handle_frame(self, socket: _Socket, frame: str | bytes) -> bool:
        if not isinstance(frame, str) or len(frame.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise _DingTalkFailure("dingtalk_provider_response_invalid")
        try:
            payload = json.loads(frame)
        except (ValueError, RecursionError):
            raise _DingTalkFailure("dingtalk_provider_response_invalid") from None
        if not isinstance(payload, dict):
            raise _DingTalkFailure("dingtalk_provider_response_invalid")
        headers = payload.get("headers")
        if not isinstance(headers, dict):
            raise _DingTalkFailure("dingtalk_provider_response_invalid")
        message_id = headers.get("messageId")
        if not isinstance(message_id, str) or _ID_RE.fullmatch(message_id) is None:
            raise _DingTalkFailure("dingtalk_provider_response_invalid")
        message_type = payload.get("type")
        topic = headers.get("topic")
        if message_type == "SYSTEM":
            self._ack(
                socket,
                message_id,
                _frame_data(payload.get("data")),
                response=False,
            )
            return topic == "disconnect"
        if message_type != "CALLBACK" or topic != _CALLBACK_TOPIC:
            self._ack(socket, message_id, {}, code=404, response=False)
            return False
        data = payload.get("data")
        if not isinstance(data, str) or len(data.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            self._ack(socket, message_id, {}, code=400)
            return False
        try:
            incoming = json.loads(data)
        except (ValueError, RecursionError):
            self._ack(socket, message_id, {}, code=400)
            return False
        if not isinstance(incoming, dict):
            self._ack(socket, message_id, {}, code=400)
            return False
        try:
            normalized = _incoming_text(incoming)
        except _DingTalkFailure:
            self._ack(socket, message_id, {}, code=400)
            return False
        if normalized is not None:
            conversation_id, provider_message_id, text, reply_url = normalized
            self._required_store().record(
                conversation_id=conversation_id,
                message_id=provider_message_id,
                text=text,
                reply_url=reply_url,
            )
        self._ack(socket, message_id, {}, response=True)
        return False

    @staticmethod
    def _ack(
        socket: _Socket,
        message_id: str,
        data: Any,
        *,
        code: int = 200,
        response: bool,
    ) -> None:
        body = {"response": "OK"} if response else data
        try:
            socket.send(
                json.dumps(
                    {
                        "code": code,
                        "headers": {
                            "messageId": message_id,
                            "contentType": "application/json",
                        },
                        "message": "" if code == 200 else "invalid callback",
                        "data": json.dumps(
                            body,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        except Exception:
            raise _DingTalkFailure("dingtalk_transport_unavailable") from None

    def _drain_received(self) -> None:
        dispatcher = self._required_dispatcher()
        store = self._required_store()
        for event in store.received():
            receipt = dispatcher.dispatch(
                ChannelInboundMessage(
                    channel_id="dingtalk",
                    conversation_id=event.conversation_id,
                    message_id=event.message_id,
                    text=event.text,
                )
            )
            store.set_outbound(event.event_key, receipt)

    def _drain_outbound(self) -> None:
        dispatcher = self._required_dispatcher()
        store = self._required_store()
        for receipt, conversation_id, reply_url in store.outbound():
            try:
                delivered = dispatcher.deliver(
                    receipt,
                    conversation_id=conversation_id,
                    transport=_ReplyTransport(self, reply_url),
                )
            except ChannelTurnTerminalFailure as error:
                store.finish(
                    receipt.turn_id,
                    "failed",
                    error.code.replace("channel_", "dingtalk_", 1),
                )
                continue
            except _DingTalkFailure as error:
                if error.uncertain:
                    store.finish(receipt.turn_id, "uncertain", error.code)
                    self._set_health(
                        ConnectorHealth.DEGRADED,
                        "dingtalk_delivery_uncertain",
                    )
                    continue
                if error.permanent:
                    store.finish(receipt.turn_id, "failed", error.code)
                    self._set_health(ConnectorHealth.ERROR, error.code)
                    continue
                raise
            if delivered:
                store.finish(receipt.turn_id, "completed")

    def _open_socket(
        self,
        client: _HTTPClient,
        credentials: tuple[str, str],
    ) -> _Socket:
        client_id, client_secret = credentials
        payload = self._request(
            client,
            "/v1.0/gateway/connections/open",
            body={
                "clientId": client_id,
                "clientSecret": client_secret,
                "subscriptions": [
                    {"type": "CALLBACK", "topic": _CALLBACK_TOPIC}
                ],
                "ua": f"emate-python/v{__version__}-union",
                "localIp": "",
            },
            operation="connect",
        )
        endpoint = payload.get("endpoint")
        ticket = payload.get("ticket")
        if (
            not isinstance(endpoint, str)
            or len(endpoint) > 4096
            or not isinstance(ticket, str)
            or not ticket
            or len(ticket) > 4096
        ):
            raise _DingTalkFailure("dingtalk_provider_response_invalid")
        parsed = urlsplit(endpoint)
        host = (parsed.hostname or "").casefold()
        if (
            parsed.scheme != "wss"
            or (host != "dingtalk.com" and not host.endswith(".dingtalk.com"))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or bool(parsed.query)
            or bool(parsed.fragment)
        ):
            raise _DingTalkFailure("dingtalk_provider_response_invalid")
        url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode({"ticket": ticket}), "")
        )
        socket: _Socket | None = None
        try:
            socket = self.socket_factory(url)
            return socket
        except Exception:
            _close_socket(socket)
            raise _DingTalkFailure("dingtalk_transport_unavailable") from None

    def _send_reply(self, client: _HTTPClient, url: str, text: str) -> None:
        _reply_url(url)
        payload = self._request(
            client,
            url,
            body={"msgtype": "text", "text": {"content": text}},
            operation="delivery",
        )
        errcode = payload.get("errcode")
        if errcode not in {0, "0"}:
            raise _DingTalkFailure("dingtalk_delivery_rejected", permanent=True)

    def _request(
        self,
        client: _HTTPClient,
        path: str,
        *,
        body: Mapping[str, Any],
        operation: str,
    ) -> Mapping[str, Any]:
        delivery = operation == "delivery"
        try:
            response = client.post(path, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise _DingTalkFailure("dingtalk_transport_unavailable") from None
        except (httpx.WriteTimeout, httpx.ReadTimeout):
            raise _DingTalkFailure(
                "dingtalk_delivery_uncertain"
                if delivery
                else "dingtalk_transport_unavailable",
                uncertain=delivery,
            ) from None
        except (httpx.TimeoutException, httpx.TransportError, OSError):
            raise _DingTalkFailure(
                "dingtalk_delivery_uncertain"
                if delivery
                else "dingtalk_transport_unavailable",
                uncertain=delivery,
            ) from None
        except Exception:
            raise _DingTalkFailure(
                "dingtalk_delivery_uncertain"
                if delivery
                else "dingtalk_transport_unavailable",
                uncertain=delivery,
            ) from None
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise _DingTalkFailure(
                "dingtalk_delivery_uncertain"
                if delivery
                else "dingtalk_provider_response_invalid",
                uncertain=delivery,
            )
        if response.status_code in {401, 403} and not delivery:
            raise _DingTalkFailure("dingtalk_auth_rejected", permanent=True)
        if response.status_code == 429:
            raise _DingTalkFailure("dingtalk_rate_limited")
        if response.status_code >= 500:
            raise _DingTalkFailure(
                "dingtalk_delivery_uncertain"
                if delivery
                else "dingtalk_transport_unavailable",
                uncertain=delivery,
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise _DingTalkFailure(
                "dingtalk_delivery_rejected"
                if delivery
                else "dingtalk_stream_rejected",
                permanent=True,
            )
        try:
            payload = response.json()
        except (ValueError, RecursionError):
            raise _DingTalkFailure(
                "dingtalk_delivery_uncertain"
                if delivery
                else "dingtalk_provider_response_invalid",
                uncertain=delivery,
            ) from None
        if not isinstance(payload, dict):
            raise _DingTalkFailure(
                "dingtalk_delivery_uncertain"
                if delivery
                else "dingtalk_provider_response_invalid",
                uncertain=delivery,
            )
        return payload

    def _set_ready_health(self) -> None:
        store = self._required_store()
        terminal = store.terminal_error()
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

    def _required_dispatcher(self) -> ChannelRuntimeDispatcher:
        with self._lock:
            if self._dispatcher is None:
                raise _DingTalkFailure(
                    "dingtalk_runtime_unavailable", permanent=True
                )
            return self._dispatcher

    def _required_store(self) -> _DingTalkStore:
        with self._lock:
            if self._store is None:
                raise _DingTalkFailure(
                    "dingtalk_runtime_unavailable", permanent=True
                )
            return self._store

    @staticmethod
    def _default_client() -> httpx.Client:
        return httpx.Client(
            base_url="https://api.dingtalk.com",
            timeout=httpx.Timeout(connect=4, read=4, write=4, pool=4),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
            headers={"User-Agent": f"e-Mate/{__version__} DingTalkChannel/1"},
        )

    @staticmethod
    def _default_socket(url: str) -> _Socket:
        return websocket_connect(
            url,
            open_timeout=4,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=1,
            max_size=_MAX_RESPONSE_BYTES,
            max_queue=16,
            user_agent_header=f"e-Mate/{__version__} DingTalkChannel/1",
        )


def _credentials(config: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(config, Mapping) or set(config) != {
        "dingtalk_client_id",
        "dingtalk_client_secret",
    }:
        raise _DingTalkFailure("dingtalk_configuration_invalid", permanent=True)
    client_id = config.get("dingtalk_client_id")
    client_secret = config.get("dingtalk_client_secret")
    if (
        not isinstance(client_id, str)
        or not 4 <= len(client_id) <= 256
        or not isinstance(client_secret, str)
        or not 8 <= len(client_secret) <= 512
        or any(character.isspace() or ord(character) < 32 for character in client_id)
        or any(character.isspace() or ord(character) < 32 for character in client_secret)
    ):
        raise _DingTalkFailure("dingtalk_configuration_invalid", permanent=True)
    return client_id, client_secret


def _incoming_text(value: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    if value.get("msgtype") != "text":
        return None
    conversation_id = value.get("conversationId")
    message_id = value.get("msgId")
    text_value = value.get("text")
    text = text_value.get("content") if isinstance(text_value, dict) else None
    reply_url = value.get("sessionWebhook")
    if (
        not isinstance(conversation_id, str)
        or _ID_RE.fullmatch(conversation_id) is None
        or not isinstance(message_id, str)
        or _ID_RE.fullmatch(message_id) is None
        or not isinstance(text, str)
        or not text.strip()
        or len(text) > 32768
        or "\x00" in text
        or not isinstance(reply_url, str)
    ):
        return None
    _reply_url(reply_url)
    return conversation_id, message_id, text.strip(), reply_url


def _reply_url(value: str) -> None:
    if not isinstance(value, str) or len(value) > 4096:
        raise _DingTalkFailure("dingtalk_provider_response_invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold()
        not in {"api.dingtalk.com", "oapi.dingtalk.com"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or bool(parsed.fragment)
    ):
        raise _DingTalkFailure("dingtalk_provider_response_invalid")


def _frame_data(value: Any) -> Mapping[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _chunks(text: str) -> tuple[str, ...]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        split = min(len(remaining), _MAX_TEXT_CHARS)
        if split < len(remaining):
            newline = remaining.rfind("\n", _MAX_TEXT_CHARS // 2, split)
            if newline >= 0:
                split = newline + 1
        chunks.append(remaining[:split])
        remaining = remaining[split:]
    return tuple(chunks)


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


__all__ = ["DingTalkStreamAdapter"]
